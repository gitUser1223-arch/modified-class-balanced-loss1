# Import from third library
import torch

# Import from pod
from eod.models.losses.loss import _reduce
from eod.tasks.det.models.losses.entropy_loss import GeneralizedCrossEntropyLoss
from eod.utils.env.dist_helper import allreduce
from eod.utils.general.log_helper import default_logger as logger
from eod.utils.general.registry_factory import LOSSES_REGISTRY


@LOSSES_REGISTRY.register('equalized_focal_loss')
class EqualizedFocalLoss(GeneralizedCrossEntropyLoss):
    def __init__(self,
                 name='equalized_focal_loss',
                 reduction='mean',
                 loss_weight=1.0,
                 ignore_index=-1,
                 num_classes=1204,
                 focal_gamma=2.0,
                 focal_alpha=0.25,
                 scale_factor=8.0,
                 fpn_levels=5,
                 class_weight=None # new add code on 240902
                 ):
        activation_type = 'sigmoid'
        GeneralizedCrossEntropyLoss.__init__(self,
                                             name=name,
                                             reduction=reduction,
                                             loss_weight=loss_weight,
                                             activation_type=activation_type,
                                             ignore_index=ignore_index)
        
        # cfg for class_weight # new add code on 240902
        if class_weight is not None:
            class_weight = torch.load(class_weight) # new add code on 240902
            self.class_weight = class_weight # new add code on 240902
        else:
            self.class_weight = class_weight # new add code on 240902   

        # cfg for focal loss
        self.focal_gamma = focal_gamma
        self.focal_alpha = focal_alpha

        # ignore bg class and ignore idx
        self.num_classes = num_classes - 1 
        # mmdetection 中 0 - 1202 是前景类别；1203是背景类别  
        # eod/up 中 0是背景类别；1-1203 是前景类别

        # cfg for efl loss
        self.scale_factor = scale_factor
        # initial variables
        self.register_buffer('pos_grad', torch.zeros(self.num_classes))
        self.register_buffer('neg_grad', torch.zeros(self.num_classes))
        self.register_buffer('pos_neg', torch.ones(self.num_classes))

        # grad collect
        self.grad_buffer = []
        self.fpn_levels = fpn_levels

        logger.info(f"build EqualizedFocalLoss, focal_alpha: {focal_alpha}, focal_gamma: {focal_gamma}, \
                    scale_factor: {scale_factor}")

    def forward(self, input, target, reduction, normalizer=None):
        self.n_c = input.shape[-1]
        self.input = input.reshape(-1, self.n_c)
        self.target = target.reshape(-1)
        self.n_i, _ = self.input.size()

        def expand_label(pred, gt_classes):
            target = pred.new_zeros(self.n_i, self.n_c + 1)
            target[torch.arange(self.n_i), gt_classes] = 1
            return target[:, 1:]

        expand_target = expand_label(self.input, self.target)
        sample_mask = (self.target != self.ignore_index)

        inputs = self.input[sample_mask]
        targets = expand_target[sample_mask]
        self.cache_mask = sample_mask
        self.cache_target = expand_target

        pred = torch.sigmoid(inputs)
        pred_t = pred * targets + (1 - pred) * (1 - targets)

        map_val = 1 - self.pos_neg.detach()
        dy_gamma = self.focal_gamma + self.scale_factor * map_val
        # focusing factor
        ff = dy_gamma.view(1, -1).expand(self.n_i, self.n_c)[sample_mask]
        # weighting factor
        wf = ff / self.focal_gamma

        # ce_loss
        ce_loss = -torch.log(pred_t)
        cls_loss = ce_loss * torch.pow((1 - pred_t), ff.detach()) * wf.detach()

        # to avoid an OOM error
        # torch.cuda.empty_cache()

        if self.focal_alpha >= 0:
            alpha_t = self.focal_alpha * targets + (1 - self.focal_alpha) * (1 - targets)
            cls_loss = alpha_t * cls_loss

        if normalizer is None:
            normalizer = 1.0
        
        # new add code on 240902 # 应用类别权重
        if self.class_weight is not None:
            target = self.target.clone()
            class_weights = self.class_weight.clone()
            class_weights = class_weights.to(target.device)
            pos_target = target[target!=0] # 获得前景类别的索引: 1-1203
            pos_target = pos_target -1 # 符合python以0开始的特性，使得索引与class_weight中class的索引相对应
            weights = torch.ones_like(target).to(target.device)
            weights = weights.float()
            weights[target==0] = 1.0
            weights[target!=0] = class_weights[pos_target] + 1.0  # 根据目标类别选择权重
            #print(f'cls_loss:{cls_loss.shape}')
            #print(f'weights:{weights.shape}')
            cls_loss = cls_loss * weights.unsqueeze(1)  # 应用类别权重

        return _reduce(cls_loss, reduction, normalizer=normalizer)

    def collect_grad(self, grad_in):
        bs = grad_in.shape[0]
        self.grad_buffer.append(grad_in.detach().permute(0, 2, 3, 1).reshape(bs, -1, self.num_classes))
        if len(self.grad_buffer) == self.fpn_levels:
            target = self.cache_target[self.cache_mask]
            grad = torch.cat(self.grad_buffer[::-1], dim=1).reshape(-1, self.num_classes)

            grad = torch.abs(grad)[self.cache_mask]
            pos_grad = torch.sum(grad * target, dim=0)
            neg_grad = torch.sum(grad * (1 - target), dim=0)

            allreduce(pos_grad)
            allreduce(neg_grad)

            self.pos_grad += pos_grad
            self.neg_grad += neg_grad
            self.pos_neg = torch.clamp(self.pos_grad / (self.neg_grad + 1e-10), min=0, max=1)

            self.grad_buffer = []

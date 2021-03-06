import torch
import torch.nn as nn
import torch.nn.functional as F
import math


def _compute_cross_entropy_loss(input, target):
    input = torch.log_softmax(input, dim=1)
    loss = torch.sum(input * target, dim=1, keepdim=False)
    loss = -torch.mean(loss, dim=0, keepdim=False)
    return loss


# distillation loss as described by Hinton et. al
def _compute_smt_loss(input, target):
    T = 2
    input = torch.log_softmax(input / T, dim=1)
    target = torch.softmax(target / T, dim=1)
    loss = torch.sum(input * target, dim=1, keepdim=False)
    loss = -torch.mean(loss, dim=0, keepdim=False)
    return loss


# bce with hard target: classification loss in iCaRL
def _compute_bce_loss(input, target):
    crit = nn.BCEWithLogitsLoss(reduction="mean")
    return crit(input, target)


def _compute_kldiv_loss(input, target):
    crit = nn.KLDivLoss(reduction="mean")
    input = torch.log_softmax(input, dim=1)
    target = torch.softmax(target, dim=1)
    return crit(input, target)


def _compute_l2_loss(input, target):
    input = torch.softmax(input, dim=1)
    target = torch.softmax(target, dim=1)
    crit = nn.MSELoss(reduction='mean')
    return crit(input, target)


# loss described in the "Learning a Unified Classifier Incrementally via Rebalancing"
# measures the cosine similarity of the previous and new features representation (normalized)
def _compute_lfc_loss(input, target):
    input = F.normalize(input, p=2)
    target = F.normalize(target, p=2)
    crit = nn.CosineEmbeddingLoss()
    loss = crit(input, target, torch.ones(input.shape[0], ).cuda())
    return loss


class ClassificationDistillationLosses:
    def __init__(self, classification, distillation, num_classes=100):
        self.num_classes = num_classes
        self.classification = classification
        self.distillation = distillation
        self.loss_computer = {
            "bce": _compute_bce_loss,
            "ce": _compute_cross_entropy_loss,
            "smt": _compute_smt_loss,
            "kldiv": _compute_kldiv_loss,
            "lfc": _compute_lfc_loss,
            "l2": _compute_l2_loss,
        }

    def __call__(self, class_input, class_target, dist_input, dist_target, class_ratio):
        class_loss = self.loss_computer[self.classification](class_input, class_target)

        if self.distillation is not None and dist_input is not None and dist_target is not None:
            dist_ratio = 1 - class_ratio
            if self.distillation == "lfc":
                # lfc requires its own class-dist ratio
                dist_ratio = 5 * math.sqrt(class_ratio / (1 - class_ratio))
                class_ratio = 1
            elif self.distillation == "bce" or self.distillation == "ce":
                # if bce or ce are chosen for distillation, targets (old_outputs) must be first turned into logits
                dist_target = torch.softmax(dist_target, dim=1)

            dist_loss = self.loss_computer[self.distillation](dist_input, dist_target)
            dist_correction = dist_input.shape[1] / 100  # rescale distillation loss just for old classes
            tot_loss = class_ratio * class_loss + dist_ratio * dist_correction * dist_loss
            return tot_loss
        else:
            return class_loss

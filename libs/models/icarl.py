import copy
from typing import Iterator

import numpy as np
import torch
import torch.nn as nn
from torch.nn import Parameter

import libs.utils as utils
from libs.resnet import resnet32
from libs.utils import get_one_hot


class iCaRLModel(nn.Module):

    def __init__(self, num_classes=100, memory=2000):
        super(iCaRLModel, self).__init__()
        self.num_classes = num_classes
        self.memory = memory
        self.known_classes = 0
        self.old_net = None

        self.net = resnet32(num_classes=num_classes)

        self.bce_loss = nn.BCEWithLogitsLoss(reduction='mean')
        self.exemplar_sets = [{'indexes': [], 'features': []} for label in range(0, num_classes)]

    def before_train(self, device):
        self.net.to(device)
        if self.old_net is not None:
            self.old_net.to(device)
            self.old_net.eval()

        indexes = [diz['indexes'] for diz in self.exemplar_sets[:self.known_classes]]
        flatten_idx = []
        for idx in indexes:
            flatten_idx.extend(idx)
        return flatten_idx

    def after_train(self, class_batch_size, train_subsets_per_class, labels, device, herding=True):
        self.known_classes += class_batch_size
        self.old_net = copy.deepcopy(self)

        self.net = self.net.to(device)

        min_memory = int(self.memory / self.known_classes)
        class_memories = [min_memory] * self.known_classes
        empty_memory = self.memory % self.known_classes
        if empty_memory > 0:
            for i in range(empty_memory):
                class_memories[i] += 1

        assert sum(class_memories) == 2000

        for i, m in enumerate(class_memories[: self.known_classes - class_batch_size]):
            self.reduce_exemplar_set(m, i)

        for curr_subset, label, m in zip(train_subsets_per_class, labels,
                                         class_memories[self.known_classes - class_batch_size: self.known_classes]):
            print(label)
            self.construct_exemplar_set(curr_subset, label, m, device, herding=herding)

    def increment_class(self, num_classes=10):
        weight = self.net.fc.weight.data
        bias = self.net.fc.bias.data
        in_feature = self.net.fc.in_features
        out_feature = self.net.fc.out_features

        self.net.fc = nn.Linear(in_feature, num_classes, bias=True)
        self.net.fc.weight.data[:out_feature] = weight
        self.net.fc.bias.data[:out_feature] = bias

    def forward(self, x, features=False):
        return self.net(x, features)

    def compute_exemplars_means(self):
        means = []
        for diz in self.exemplar_sets[:self.known_classes]:
            sum_features = sum(diz['features'])
            class_mean = sum_features / len(diz['features'])
            class_mean = class_mean / class_mean.norm(p=2)
            means.append(class_mean)

        return means

    def classify(self, images, method='nearest-mean'):
        if method == 'nearest-mean':
            return self._nearest_mean(images)
        elif method == 'fc':
            return self.net(images)
        elif method == 'knn':
            return self._k_nearest_neighbours(images)

    def _nearest_mean(self, images):
        print("**************** NEAREST MEAN**************")
        means = self.compute_exemplars_means()
        targets = np.zeros(len(images))

        self.net.eval()
        with torch.no_grad():
            for i, img in enumerate(images):
                pred = None
                min_dist = float('inf')
                feature = self._extract_feature(img)
                for label, mean in enumerate(means):
                    dist = torch.dist(feature, mean, p=2)
                    if min_dist > dist:
                        min_dist = dist
                        pred = label

                targets[i] = pred

        return torch.from_numpy(targets)

    def _k_nearest_neighbours(self, images):
        self.net.eval()

    def compute_distillation_loss(self, images, labels, new_outputs, device, num_classes=10):

        if self.known_classes == 0:
            return self.bce_loss(new_outputs, get_one_hot(labels, self.num_classes, device))

        sigmoid = nn.Sigmoid()
        n_old_classes = self.known_classes
        # n_new_classes = self.known_classes + num_classes
        old_outputs = self.old_net(images)

        targets = get_one_hot(labels, self.num_classes, device)
        targets[:, :n_old_classes] = sigmoid(old_outputs)[:, :n_old_classes]
        tot_loss = self.bce_loss(new_outputs, targets)

        return tot_loss

    def parameters(self, recurse: bool = ...) -> Iterator[Parameter]:
        return self.net.parameters()

    def _extract_feature(self, x):
        return self.net(x, features=True)

    def construct_exemplar_set(self, single_class_dataset, label, m, device, herding=True):
        if len(single_class_dataset) < m:
            raise ValueError("Number of images can't be less than m")

        if herding:
            loader = utils.get_eval_loader(single_class_dataset, batch_size=256)
            features = []
            map_subset_to_cifar = single_class_dataset.indices

            self.net.eval()
            with torch.no_grad():
                for images, _ in loader:
                    images = images.to(device)
                    feat = self._extract_feature(images)
                    features.append(feat)

                flatten_features = torch.cat(features)
                class_mean = flatten_features.mean(0)
                class_mean = class_mean / class_mean.norm(p=2)

            for k in range(m):
                min_index = -1
                min_dist = .0
                exemplars = self.exemplar_sets[label]['indexes']
                for i, feature in enumerate(flatten_features):
                    if i in exemplars:
                        continue
                    sum_exemplars = 0 if k == 0 else sum(exemplars[:k])
                    sum_exemplars = (feature + sum_exemplars) / (k + 1)
                    curr_mean_exemplars = sum_exemplars / sum_exemplars.norm(p=2)
                    curr_dist = torch.dist(class_mean, curr_mean_exemplars)

                    if min_index == -1 or min_dist > curr_dist:
                        min_index = i
                        min_dist = curr_dist

                self.exemplar_sets[label]['indexes'].append(map_subset_to_cifar[min_index])
                self.exemplar_sets[label]['features'].append(flatten_features[min_index])

    def reduce_exemplar_set(self, m, label):
        if len(self.exemplar_sets[label]['indexes']) < m:
            raise ValueError(f"m must be lower than current size of current exemplar set for class {label}")

        self.exemplar_sets[label]['indexes'] = self.exemplar_sets[label]['indexes'][:m]
        self.exemplar_sets[label]['features'] = self.exemplar_sets[label]['features'][:m]

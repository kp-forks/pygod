# -*- coding: utf-8 -*-
"""Deep Anomaly Detection on Attributed Networks (DOMINANT)"""
# Author: Kay Liu <zliu234@uic.edu>
# License: BSD 2 clause

import torch
from torch_geometric.nn import GCN

from .base import DeepDetector
from ..nn import DOMINANTBase


class DOMINANT(DeepDetector):
    """
    Deep Anomaly Detection on Attributed Networks

    DOMINANT is an anomaly detector consisting of a shared graph
    convolutional encoder, a structure reconstruction decoder, and an
    attribute reconstruction decoder. The reconstruction mean squared
    error of the decoders are defined as structure anomaly score and
    attribute anomaly score, respectively.

    See :cite:`ding2019deep` for details.

    Parameters
    ----------
    hid_dim :  int, optional
        Hidden dimension of model. Default: ``64``.
    num_layers : int, optional
       Total number of layers in model. A half (floor) of the layers
       are for the encoder, the other half (ceil) of the layers are
       for decoders. Default: ``4``.
    dropout : float, optional
        Dropout rate. Default: ``0.``.
    weight_decay : float, optional
        Weight decay (L2 penalty). Default: ``0.``.
    act : callable activation function or None, optional
        Activation function if not None.
        Default: ``torch.nn.functional.relu``.
    sigmoid_s : bool, optional
        Whether to use sigmoid function to scale the reconstructed
        structure. Default: ``False``.
    backbone : torch.nn.Module, optional
        The backbone of the deep detector implemented in PyG.
        Default: ``torch_geometric.nn.GCN``.
    contamination : float, optional
        The amount of contamination of the dataset in (0., 0.5], i.e.,
        the proportion of outliers in the dataset. Used when fitting to
        define the threshold on the decision function. Default: ``0.1``.
    lr : float, optional
        Learning rate. Default: ``0.004``.
    epoch : int, optional
        Maximum number of training epoch. Default: ``100``.
    gpu : int
        GPU Index, -1 for using CPU. Default: ``-1``.
    batch_size : int, optional
        Minibatch size, 0 for full batch training. Default: ``0``.
    num_neigh : int, optional
        Number of neighbors in sampling, -1 for all neighbors.
        Default: ``-1``.
    weight : float, optional
        Weight between reconstruction of node feature and structure.
        Default: ``0.5``.
    verbose : int, optional
        Verbosity mode. Range in [0, 3]. Larger value for printing out
        more log information. Default: ``0``.
    save_emb : bool, optional
        Whether to save the embedding. Default: ``False``.
    compile_model : bool, optional
        Whether to compile the model with ``torch_geometric.compile``.
        Default: ``False``.
    **kwargs : optional
        Additional arguments for the backbone.

    Attributes
    ----------
    decision_score_ : torch.Tensor
        The outlier scores of the training data. Outliers tend to have
        higher scores. This value is available once the detector is
        fitted.
    threshold_ : float
        The threshold is based on ``contamination``. It is the
        :math:`N`*``contamination`` most abnormal samples in
        ``decision_score_``. The threshold is calculated for generating
        binary outlier labels.
    label_ : torch.Tensor
        The binary labels of the training data. 0 stands for inliers
        and 1 for outliers. It is generated by applying
        ``threshold_`` on ``decision_score_``.
    emb : torch.Tensor or tuple of torch.Tensor or None
        The learned node hidden embeddings of shape
        :math:`N \\times` ``hid_dim``. Only available when ``save_emb``
        is ``True``. When the detector has not been fitted, ``emb`` is
        ``None``. When the detector has multiple embeddings,
        ``emb`` is a tuple of torch.Tensor.
    """

    def __init__(self,
                 hid_dim=64,
                 num_layers=4,
                 dropout=0.,
                 weight_decay=0.,
                 act=torch.nn.functional.relu,
                 sigmoid_s=False,
                 backbone=GCN,
                 contamination=0.1,
                 lr=4e-3,
                 epoch=100,
                 gpu=-1,
                 batch_size=0,
                 num_neigh=-1,
                 weight=0.5,
                 verbose=0,
                 save_emb=False,
                 compile_model=False,
                 **kwargs):

        super(DOMINANT, self).__init__(hid_dim=hid_dim,
                                       num_layers=num_layers,
                                       dropout=dropout,
                                       weight_decay=weight_decay,
                                       act=act,
                                       backbone=backbone,
                                       contamination=contamination,
                                       lr=lr,
                                       epoch=epoch,
                                       gpu=gpu,
                                       batch_size=batch_size,
                                       num_neigh=num_neigh,
                                       verbose=verbose,
                                       save_emb=save_emb,
                                       compile_model=compile_model,
                                       **kwargs)

        self.weight = weight
        self.sigmoid_s = sigmoid_s

    def process_graph(self, data):
        DOMINANTBase.process_graph(data)

    def init_model(self, **kwargs):
        if self.save_emb:
            self.emb = torch.zeros(self.num_nodes, self.hid_dim)
        return DOMINANTBase(in_dim=self.in_dim,
                            hid_dim=self.hid_dim,
                            num_layers=self.num_layers,
                            dropout=self.dropout,
                            act=self.act,
                            sigmoid_s=self.sigmoid_s,
                            backbone=self.backbone,
                            **kwargs).to(self.device)

    def forward_model(self, data):
        batch_size = data.batch_size
        node_idx = data.n_id

        x = data.x.to(self.device)
        s = data.s.to(self.device)
        edge_index = data.edge_index.to(self.device)

        x_, s_ = self.model(x, edge_index)
        score = self.model.loss_func(x[:batch_size],
                                         x_[:batch_size],
                                         s[:batch_size, node_idx],
                                         s_[:batch_size],
                                         self.weight)

        loss = torch.mean(score)

        return loss, score.detach().cpu()

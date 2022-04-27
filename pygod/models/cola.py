# -*- coding: utf-8 -*-
"""Anomaly Detection on Attributed Networks via Contrastive
Self-Supervised Learning (CoLA)"""
# Author: Canyu Chen <cchen151@hawk.iit.edu>
# License: BSD 2 clause

from telnetlib import GA
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import scipy.sparse as sp
from sklearn.metrics import roc_auc_score
import random
import os
import argparse
from tqdm import tqdm
import networkx as nx
import scipy.io as sio

from torch_geometric.utils import to_dense_adj
from torch_sparse import SparseTensor
from torch_geometric.utils.num_nodes import maybe_num_nodes
from torch_cluster import random_walk

from . import BaseDetector
from ..utils.metric import eval_roc_auc

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"


def sparse_to_tuple(sparse_mx, insert_batch=False):
    """Convert sparse matrix to tuple representation."""
    """Set insert_batch=True if you want to insert a batch dimension."""

    def to_tuple(mx):
        if not sp.isspmatrix_coo(mx):
            mx = mx.tocoo()
        if insert_batch:
            coords = np.vstack((np.zeros(mx.row.shape[0]), mx.row, mx.col)).T
            values = mx.data
            shape = (1,) + mx.shape
        else:
            coords = np.vstack((mx.row, mx.col)).transpose()
            values = mx.data
            shape = mx.shape
        return coords, values, shape

    if isinstance(sparse_mx, list):
        for i in range(len(sparse_mx)):
            sparse_mx[i] = to_tuple(sparse_mx[i])
    else:
        sparse_mx = to_tuple(sparse_mx)

    return sparse_mx


def preprocess_features(features):
    """Row-normalize feature matrix and convert to tuple representation"""
    rowsum = np.array(features.sum(1))
    r_inv = np.power(rowsum, -1).flatten()
    r_inv[np.isinf(r_inv)] = 0.
    r_mat_inv = sp.diags(r_inv)
    features = r_mat_inv.dot(features)
    return features.todense(), sparse_to_tuple(features)


def normalize_adj(adj):
    """Symmetrically normalize adjacency matrix."""
    adj = sp.coo_matrix(adj)
    rowsum = np.array(adj.sum(1))
    d_inv_sqrt = np.power(rowsum, -0.5).flatten()
    d_inv_sqrt[np.isinf(d_inv_sqrt)] = 0.
    d_mat_inv_sqrt = sp.diags(d_inv_sqrt)
    return adj.dot(d_mat_inv_sqrt).transpose().dot(d_mat_inv_sqrt).tocoo()


def dense_to_one_hot(labels_dense, num_classes):
    """Convert class labels from scalars to one-hot vectors."""
    num_labels = labels_dense.shape[0]
    index_offset = np.arange(num_labels) * num_classes
    labels_one_hot = np.zeros((num_labels, num_classes))
    labels_one_hot.flat[index_offset + labels_dense.ravel()] = 1
    return labels_one_hot


def generate_rw_subgraph(pyg_graph, nb_nodes, subgraph_size):
    """Generate subgraph with random walk algorithm."""
    row, col = pyg_graph.edge_index
    all_idx = torch.tensor(list(range(nb_nodes)))
    traces = random_walk(row, col, all_idx, walk_length=3)
    subv = traces.tolist()
    return subv


class CoLA(BaseDetector):
    """
    CoLA (Anomaly Detection on Attributed Networks via
    Contrastive Self-Supervised Learning)
    CoLA is a contrastive self-supervised learning-based method
    for graph anomaly detection. (beta)

    Parameters
    ----------
    lr : float, optional
        Learning rate. 
    verbose : bool
        Verbosity mode. Turn on to print out log information.
        Default: ``False``.
    epoch : int, optional
        Maximum number of training epoch. 
    embedding_dim : 
    negsamp_ratio : 
    readout : 
    dataset : 
    weight_decay : 
    batch_size : 
    subgraph_size : 
    auc_test_rounds : 
    contamination : float, optional
        Valid in (0., 0.5). The proportion of outliers in the data set.
        Used when fitting to define the threshold on the decision
        function. Default: ``0.1``.
    gpu : int
        GPU Index, -1 for using CPU. Default: ``0``.

    Examples
    --------
    >>> from pygod.models import CoLA
    >>> model = CoLA()
    >>> model.fit(data)
    >>> prediction = model.predict(data)
    """

    def __init__(self,
                 lr=None,
                 verbose=False,
                 epoch=None,
                 embedding_dim=64,
                 negsamp_ratio=1,
                 readout='avg',
                 dataset='cora',
                 weight_decay=0.0,
                 batch_size=300,
                 subgraph_size=4,
                 auc_test_rounds=256,
                 contamination=0.1,
                 gpu=0):
        super(CoLA, self).__init__(contamination=contamination)

        self.dataset = dataset
        self.embedding_dim = embedding_dim
        self.negsamp_ratio = negsamp_ratio
        self.readout = readout
        self.weight_decay = weight_decay
        self.batch_size = batch_size
        self.subgraph_size = subgraph_size
        self.auc_test_rounds = auc_test_rounds
        if gpu >= 0 and torch.cuda.is_available():
            self.device = 'cuda:{}'.format(gpu)
        else:
            self.device = 'cpu'

        if lr is None:
            if self.dataset in ['cora', 'citeseer', 'pubmed', 'Flickr']:
                self.lr = 1e-3
            elif self.dataset == 'ACM':
                self.lr = 5e-4
            elif self.dataset == 'BlogCatalog':
                self.lr = 3e-3
            else:
                self.lr = 1e-3

        if epoch is None:
            if self.dataset in ['cora', 'citeseer', 'pubmed']:
                self.num_epoch = 100
            elif self.dataset in ['BlogCatalog', 'Flickr', 'ACM']:
                self.num_epoch = 400
            else:
                self.num_epoch = 100

        self.verbose = verbose
        self.model = None

    def fit(self, G):
        """
        Fit detector with input data.

        Parameters
        ----------
        G : PyTorch Geometric Data instance (torch_geometric.data.Data)
            The input data.

        Returns
        -------
        self : object
            Fitted estimator.
        """

        x, adj, edge_index, labels = self.process_graph(G)

        adj = adj.cpu().numpy()
        x = x.cpu().numpy()

        nb_nodes = x.shape[0]
        ft_size = x.shape[1]

        adj = normalize_adj(adj)
        adj = (adj + sp.eye(adj.shape[0])).todense()

        x = torch.FloatTensor(x[np.newaxis])
        adj = torch.FloatTensor(adj[np.newaxis])

        # Initialize model and optimiser
        self.model = COLA_Base(ft_size,
                               self.embedding_dim,
                               'prelu',
                               self.negsamp_ratio,
                               self.readout)

        optimiser = torch.optim.Adam(self.model.parameters(),
                                     lr=self.lr,
                                     weight_decay=self.weight_decay)

        b_xent = nn.BCEWithLogitsLoss(reduction='none',
                                      pos_weight=torch.tensor(
                                          [self.negsamp_ratio]))

        batch_num = nb_nodes // self.batch_size + 1

        multi_epoch_ano_score = np.zeros((self.num_epoch, nb_nodes))

        for epoch in range(self.num_epoch):

            self.model.train()

            all_idx = list(range(nb_nodes))
            random.shuffle(all_idx)

            subgraphs = generate_rw_subgraph(G, nb_nodes, self.subgraph_size)

            for batch_idx in range(batch_num):

                optimiser.zero_grad()

                is_final_batch = (batch_idx == (batch_num - 1))

                if not is_final_batch:
                    idx = all_idx[batch_idx * self.batch_size: (
                        batch_idx + 1) * self.batch_size]
                else:
                    idx = all_idx[batch_idx * self.batch_size:]

                cur_batch_size = len(idx)

                lbl = torch.unsqueeze(torch.cat((torch.ones(cur_batch_size),
                    torch.zeros(cur_batch_size * self.negsamp_ratio))), 1)

                ba = []
                bf = []
                added_adj_zero_row = torch.zeros(
                    (cur_batch_size, 1, self.subgraph_size))
                added_adj_zero_col = torch.zeros(
                    (cur_batch_size, self.subgraph_size + 1, 1))
                added_adj_zero_col[:, -1, :] = 1.
                added_feat_zero_row = torch.zeros((cur_batch_size, 1, ft_size))

                for i in idx:
                    cur_adj = adj[:, subgraphs[i], :][:, :, subgraphs[i]]
                    cur_feat = x[:, subgraphs[i], :]
                    ba.append(cur_adj)
                    bf.append(cur_feat)

                ba = torch.cat(ba)
                ba = torch.cat((ba, added_adj_zero_row), dim=1)
                ba = torch.cat((ba, added_adj_zero_col), dim=2)
                bf = torch.cat(bf)
                bf = torch.cat(
                    (bf[:, :-1, :], added_feat_zero_row, bf[:, -1:, :]), dim=1)

                logits = self.model(bf, ba)
                loss_all = b_xent(logits.cpu(), lbl.cpu())

                loss = torch.mean(loss_all)

                loss.backward()
                optimiser.step()

                logits = torch.squeeze(logits)

                logits = torch.sigmoid(logits)

                ano_score = - (logits[:cur_batch_size] - logits[
                    cur_batch_size:]).detach().cpu().numpy()
                multi_epoch_ano_score[epoch, idx] = ano_score

            if self.verbose:
                print("Epoch {:04d}: Loss {:.4f}"
                      .format(epoch, loss.item()), end='')
                if labels is not None:
                    auc = eval_roc_auc(labels, loss_all.detach().cpu().numpy())
                    print(" | AUC {:.4f}".format(auc), end='')
                print()

        ano_score_final = np.mean(multi_epoch_ano_score, axis=0)

        self.decision_scores_ = ano_score_final
        self._process_decision_scores()
        return self

    def decision_function(self, G):
        """
        Predict raw anomaly score using the fitted detector. Outliers
        are assigned with larger anomaly scores.

        Parameters
        ----------
        G : PyTorch Geometric Data instance (torch_geometric.data.Data)
            The input data.

        Returns
        -------
        outlier_scores : numpy.ndarray
            The anomaly score of shape :math:`N`.
        """

        x, adj, edge_index, _ = self.process_graph(G)

        adj = adj.cpu().numpy()
        x = x.cpu().numpy()

        nb_nodes = x.shape[0]
        ft_size = x.shape[1]

        adj = normalize_adj(adj)
        adj = (adj + sp.eye(adj.shape[0])).todense()

        x = torch.FloatTensor(x[np.newaxis])
        adj = torch.FloatTensor(adj[np.newaxis])

        batch_num = nb_nodes // self.batch_size + 1

        multi_round_ano_score = np.zeros((self.auc_test_rounds, nb_nodes))

        # enable the evaluation mode
        self.model.eval()

        for round in range(self.auc_test_rounds):

            all_idx = list(range(nb_nodes))
            random.shuffle(all_idx)

            subgraphs = generate_rw_subgraph(G, nb_nodes, self.subgraph_size)

            for batch_idx in range(batch_num):

                is_final_batch = (batch_idx == (batch_num - 1))

                if not is_final_batch:
                    idx = all_idx[batch_idx * self.batch_size: (
                        batch_idx + 1) * self.batch_size]
                else:
                    idx = all_idx[batch_idx * self.batch_size:]

                cur_batch_size = len(idx)

                ba = []
                bf = []
                added_adj_zero_row = torch.zeros(
                    (cur_batch_size, 1, self.subgraph_size))
                added_adj_zero_col = torch.zeros(
                    (cur_batch_size, self.subgraph_size + 1, 1))
                added_adj_zero_col[:, -1, :] = 1.
                added_feat_zero_row = torch.zeros((cur_batch_size, 1, ft_size))

                for i in idx:
                    cur_adj = adj[:, subgraphs[i], :][:, :, subgraphs[i]]
                    cur_feat = x[:, subgraphs[i], :]
                    ba.append(cur_adj)
                    bf.append(cur_feat)

                ba = torch.cat(ba)
                ba = torch.cat((ba, added_adj_zero_row), dim=1)
                ba = torch.cat((ba, added_adj_zero_col), dim=2)
                bf = torch.cat(bf)
                bf = torch.cat(
                    (bf[:, :-1, :], added_feat_zero_row, bf[:, -1:, :]), dim=1)

                with torch.no_grad():
                    model_output = self.model(bf, ba)

                    logits = torch.squeeze(model_output)

                    logits = torch.sigmoid(logits)

                ano_score = - (logits[:cur_batch_size] - logits[
                    cur_batch_size:]).cpu().numpy()

                multi_round_ano_score[round, idx] = ano_score

        ano_score_final = np.mean(multi_round_ano_score, axis=0)

        return ano_score_final

    def process_graph(self, G):
        """
        Description
        -----------
        Process the raw PyG data object into a tuple of sub data
        objects needed for the model.

        Parameters
        ----------
        G : PyTorch Geometric Data instance (torch_geometric.data.Data)
            The input data.

        Returns
        -------
        x : torch.Tensor
            Attribute (feature) of nodes.
        adj : torch.Tensor
            Adjacency matrix of the graph.
        edge_index : torch.Tensor
            Edge list of the graph.
        y : torch.Tensor
            Labels of nodes.
        """
        edge_index = G.edge_index

        adj = to_dense_adj(edge_index)[0].to(self.device)

        edge_index = edge_index.to(self.device)
        adj = adj.to(self.device)
        x = G.x.to(self.device)

        if hasattr(G, 'y'):
            y = G.y
        else:
            y = None

        # return data objects needed for the network
        return x, adj, edge_index, y


class COLA_Base(nn.Module):
    def __init__(self,
                 n_in,
                 n_h,
                 activation,
                 negsamp_round,
                 readout):

        super(COLA_Base, self).__init__()
        self.read_mode = readout
        self.gcn = GCN(n_in, n_h, activation)
        if readout == 'max':
            self.read = MaxReadout()
        elif readout == 'min':
            self.read = MinReadout()
        elif readout == 'avg':
            self.read = AvgReadout()
        elif readout == 'weighted_sum':
            self.read = WSReadout()

        self.disc = Discriminator(n_h, negsamp_round)

    def forward(self, seq1, adj, sparse=False):
        h_1 = self.gcn(seq1, adj, sparse)

        if self.read_mode != 'weighted_sum':
            c = self.read(h_1[:, : -1, :])
            h_mv = h_1[:, -1, :]
        else:
            h_mv = h_1[:, -1, :]
            c = self.read(h_1[:, : -1, :], h_1[:, -2: -1, :])

        ret = self.disc(c, h_mv)

        return ret


class GCN(nn.Module):
    def __init__(self, in_ft, out_ft, act, bias=True):
        super(GCN, self).__init__()
        self.fc = nn.Linear(in_ft, out_ft, bias=False)
        self.act = nn.PReLU() if act == 'prelu' else act

        if bias:
            self.bias = nn.Parameter(torch.FloatTensor(out_ft))
            self.bias.data.fill_(0.0)
        else:
            self.register_parameter('bias', None)

        for m in self.modules():
            self.weights_init(m)

    def weights_init(self, m):
        if isinstance(m, nn.Linear):
            torch.nn.init.xavier_uniform_(m.weight.data)
            if m.bias is not None:
                m.bias.data.fill_(0.0)

    def forward(self, seq, adj, sparse=False):
        seq_fts = self.fc(seq)
        if sparse:
            out = torch.unsqueeze(torch.spmm(adj, torch.squeeze(seq_fts, 0)),
                                  0)
        else:
            out = torch.bmm(adj, seq_fts)
        if self.bias is not None:
            out += self.bias

        return self.act(out)


class AvgReadout(nn.Module):
    def __init__(self):
        super(AvgReadout, self).__init__()

    def forward(self, seq):
        return torch.mean(seq, 1)


class MaxReadout(nn.Module):
    def __init__(self):
        super(MaxReadout, self).__init__()

    def forward(self, seq):
        return torch.max(seq, 1).values


class MinReadout(nn.Module):
    def __init__(self):
        super(MinReadout, self).__init__()

    def forward(self, seq):
        return torch.min(seq, 1).values


class WSReadout(nn.Module):
    def __init__(self):
        super(WSReadout, self).__init__()

    def forward(self, seq, query):
        query = query.permute(0, 2, 1)
        sim = torch.matmul(seq, query)
        sim = F.softmax(sim, dim=1)
        sim = sim.repeat(1, 1, 64)
        out = torch.mul(seq, sim)
        out = torch.sum(out, 1)
        return out


class Discriminator(nn.Module):
    def __init__(self, n_h, negsamp_round):
        super(Discriminator, self).__init__()
        self.f_k = nn.Bilinear(n_h, n_h, 1)

        for m in self.modules():
            self.weights_init(m)

        self.negsamp_round = negsamp_round

    def weights_init(self, m):
        if isinstance(m, nn.Bilinear):
            torch.nn.init.xavier_uniform_(m.weight.data)
            if m.bias is not None:
                m.bias.data.fill_(0.0)

    def forward(self, c, h_pl):
        scs = []
        # positive
        scs.append(self.f_k(h_pl, c))

        # negative
        c_mi = c
        for _ in range(self.negsamp_round):
            c_mi = torch.cat((c_mi[-2:-1, :], c_mi[:-1, :]), 0)
            scs.append(self.f_k(h_pl, c_mi))

        logits = torch.cat(tuple(scs))

        return logits

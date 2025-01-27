"""Perform masking on inputs"""
import numpy as np
import torch
from scipy.stats import rankdata
from torch_geometric.data import Data

from point_vs.models.geometric.egnn_satorras import SartorrasEGNN
from point_vs.models.geometric.pnn_geometric_base import PNNGeometricBase
from point_vs.models.point_neural_network_base import to_numpy
from point_vs.preprocessing.pyg_single_item_dataset import \
    get_pyg_single_graph_for_inference


def find_max_scores(edge_attrs, edge_indices, num_nodes, edge_scores,
                    include_intra_bonds=False):
    edges = to_numpy(edge_indices)
    if not include_intra_bonds:
        lig_rec_indices = np.where(edge_attrs[:, 1])
        edges = edges[:, lig_rec_indices].squeeze()
    scores = []

    for node in range(num_nodes):
        indices_of_edges = np.where(edges == node)[1]
        print(node, max(edge_scores[indices_of_edges]))
        if len(indices_of_edges):
            scores.append(float(np.mean(edge_scores[indices_of_edges])))
        else:
            scores.append(0)

    return scores


def edge_attention(
        model, p, v, m=None, edge_indices=None, edge_attrs=None, gnn_layer=-1,
        **kwargs):
    assert isinstance(model, PNNGeometricBase), \
        'Attention based attribution only compatable with SartorrasEGNN'
    graph = get_pyg_single_graph_for_inference(Data(
        x=v.squeeze(),
        edge_index=edge_indices,
        edge_attr=edge_attrs,
        pos=p.squeeze(),
    ))

    model(graph)
    if isinstance(gnn_layer, int):
        return to_numpy(
            model.layers[gnn_layer].att_val).reshape((-1,))
    else:
        all_attention_weights = to_numpy(torch.cat([
            layer.att_val.reshape((-1, 1)) for layer in model.layers if
            hasattr(layer, 'att_val')], dim=1))
        print(all_attention_weights.shape)
        if gnn_layer == 'max':
            return np.max(all_attention_weights, axis=1)
        else:  # 'mean'
            ranks = []
            n_bonds, n_layers = all_attention_weights.shape
            for i in range(n_layers):
                weights = all_attention_weights[:, i]
                ranks.append((n_bonds - rankdata(weights)) / n_bonds)
            ranks = np.array(ranks)
            return np.mean(ranks, axis=0)
            return np.mean(all_attention_weights, axis=1)


def node_attention(
        model, p, v, m=None, edge_indices=None, edge_attrs=None, **kwargs):
    assert isinstance(model, SartorrasEGNN), \
        'Attention based attribution only compatable with SartorrasEGNN'
    graph = get_pyg_single_graph_for_inference(Data(
        x=v.squeeze(),
        edge_index=edge_indices,
        edge_attr=edge_attrs,
        pos=p.squeeze(),
    ))

    num_nodes = graph.x.shape[0]
    model(graph)
    attention_weights = to_numpy(model.final_attention_weights).reshape((-1,))

    return find_max_scores(
        edge_attrs, edge_indices, num_nodes, attention_weights, True)


def edge_embedding_attribution(
        model, p, v, m=None, edge_indices=None, edge_attrs=None, **kwargs):
    assert isinstance(model, SartorrasEGNN), \
        'Edge based attribution only compatable with SartorrasEGNN'
    graph = get_pyg_single_graph_for_inference(Data(
        x=v.squeeze(),
        edge_index=edge_indices,
        edge_attr=edge_attrs,
        pos=p.squeeze(),
    ))

    feats, edges, coords, edge_attributes, batch = model.unpack_graph(
        graph)
    _, edge_embeddings = model.get_embeddings(
        feats, edges, coords, edge_attributes, batch)
    edge_scores = to_numpy(model.edges_linear_layers(edge_embeddings))

    return edge_scores


def cam(model, p, v, m, edge_indices=None, edge_attrs=None, **kwargs):
    """Perform class activation mapping (CAM) on input.

    Arguments:
        p: matrix of size (1, n, 3) with atom positions
        v: matrix of size (1, n, d) with atom features
        m: matrix of ones of size (1, n)
        edge_indices: (EGNN) indices of connected atoms
        edge_attrs: (EGNN) type of bond (inter/intra ligand/receptor)

    Returns:
        Numpy array containing CAM score attributions for each atom
    """
    if isinstance(model, PNNGeometricBase):
        graph = get_pyg_single_graph_for_inference(Data(
            x=v.squeeze(),
            edge_index=edge_indices,
            edge_attr=edge_attrs,
            pos=p.squeeze(),
        ))
        feats, edges, coords, edge_attributes, batch = model.unpack_graph(
            graph)

        feats, _ = model.get_embeddings(
            feats, edges, coords, edge_attributes, batch)
        x = to_numpy(model.feats_linear_layers(feats))

    else:
        if hasattr(model, 'group') and hasattr(model.group, 'lift'):
            x = model.group.lift((p, v, m), model.liftsamples)
            liftsamples = model.liftsamples
        else:
            x = p, v, m
            liftsamples = 1
        for layer in model.layers:
            if layer.__class__.__name__.find('GlobalPool') != -1:
                break
            x = layer(x)
        x = to_numpy(x[1].squeeze())
        if not model.linear_gap:
            # We can directly look at the contribution of each node by taking
            # the
            # dot product between each node's features and the final FC layer
            final_layer_weights = to_numpy(model.layers[-1].weight).T
            x = x @ final_layer_weights
            if liftsamples == 1:
                return x
            x = [np.mean(x[n:n + liftsamples]) for n in
                 range(len(x) // liftsamples)]
    return np.array(x)


def masking(
        model, p, v, m, bs=16, edge_indices=None, edge_attrs=None, **kwargs):
    """Perform masking on each point in the input.

    Scores are calculated by taking the difference between the original
    (unmasked) score and the score with each point masked.

    Arguments:
        p: matrix of size (1, n, 3) with atom positions
        v: matrix of size (1, n, d) with atom features
        m: matrix of ones of size (1, n)
        bs: batch size to use (larger is faster but requires more GPU memory)

    Returns:
        Numpy array containing masking score attributions for each atom
    """
    scores = np.zeros((m.size(1),))

    if isinstance(model, PNNGeometricBase):
        graph = get_pyg_single_graph_for_inference(Data(
            x=v.squeeze(),
            edge_index=edge_indices,
            edge_attr=edge_attrs,
            pos=p.squeeze(),
        ))
        original_score = float(to_numpy(torch.sigmoid(model(graph))))
        for i in range(len(scores)):
            p_input_matrix = torch.zeros(p.size(1) - 1, p.size(2)).cuda()
            v_input_matrix = torch.zeros(v.size(1) - 1, v.size(2)).cuda()

            p_input_matrix[:i, :] = p[:, :i, :]
            p_input_matrix[i:, :] = p[:, i + 1:, :]
            v_input_matrix[:i, :] = v[:, :i, :]
            v_input_matrix[i:, :] = v[:, i + 1:, :]

            edge_minus_idx_prod = torch.prod(edge_indices - i, dim=0)
            mask = torch.where(edge_minus_idx_prod)
            e_attrs_input_matrix = edge_attrs[mask]
            e_indices_input_matrix = edge_indices.T[mask].T

            e_indices_input_matrix[np.where(e_indices_input_matrix > i)] -= 1

            graph = get_pyg_single_graph_for_inference(Data(
                x=v_input_matrix.squeeze(),
                edge_index=e_indices_input_matrix,
                edge_attr=e_attrs_input_matrix,
                pos=p_input_matrix.squeeze(),
            ))
            x = model(graph)
            scores[i] = original_score - float(to_numpy(torch.sigmoid(x)))
            print(scores[i])
    else:
        original_score = float(to_numpy(torch.sigmoid(model((p, v, m)))))
        p_input_matrix = torch.zeros(bs, p.size(1) - 1, p.size(2)).cuda()
        v_input_matrix = torch.zeros(bs, v.size(1) - 1, v.size(2)).cuda()
        m_input_matrix = torch.ones(bs, m.size(1) - 1).bool().cuda()
        for i in range(p.size(1) // bs):
            print(i * bs)
            for j in range(bs):
                global_idx = bs * i + j
                p_input_matrix[j, :, :] = p[0,
                                          torch.arange(p.size(1)) != global_idx,
                                          :]
                v_input_matrix[j, :, :] = v[0,
                                          torch.arange(v.size(1)) != global_idx,
                                          :]
            scores[i * bs:(i + 1) * bs] = to_numpy(torch.sigmoid(model((
                p_input_matrix, v_input_matrix,
                m_input_matrix)))).squeeze() - original_score
        for i in range(bs * (p.size(1) // bs), p.size(1)):
            masked_p = p[:, torch.arange(p.size(1)) != i, :].cuda()
            masked_v = v[:, torch.arange(v.size(1)) != i, :].cuda()
            masked_m = m[:, torch.arange(m.size(1)) != i].cuda()
            scores[i] = original_score - float(to_numpy(torch.sigmoid(
                model((masked_p, masked_v, masked_m)))))
    return scores

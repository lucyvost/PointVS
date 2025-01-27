from abc import abstractmethod

import torch
from torch import nn
from torch_geometric.nn import global_mean_pool

from point_vs.models.point_neural_network_base import PointNeuralNetworkBase


def unsorted_segment_sum(data, segment_ids, num_segments):
    result_shape = (num_segments, data.size(1))
    result = data.new_full(result_shape, 0)  # Init empty result tensor.
    segment_ids = segment_ids.unsqueeze(-1).expand(-1, data.size(1))
    result.scatter_add_(0, segment_ids, data)
    return result


def unsorted_segment_mean(data, segment_ids, num_segments):
    result_shape = (num_segments, data.size(1))
    segment_ids = segment_ids.unsqueeze(-1).expand(-1, data.size(1))
    result = data.new_full(result_shape, 0)  # Init empty result tensor.
    count = data.new_full(result_shape, 0)
    result.scatter_add_(0, segment_ids, data)
    count.scatter_add_(0, segment_ids, torch.ones_like(data))
    return result / count.clamp(min=1)


class PygLinearPass(nn.Module):
    """Helper class for neater forward passes.

    Gives a linear layer with the same semantic behaviour as the E_GCL and
    EGNN_Sparse layers.

    Arguments:
        module: nn.Module (usually a linear layer)
        feats_appended_to_coords: does the input include coordinates in the
            first three columns of the node feature vector
        return_coords_and_edges: return a tuple containing the node features,
            the coords and the edges rather than just the node features
    """

    def __init__(self, module, feats_appended_to_coords=False,
                 return_coords_and_edges=False):
        super().__init__()
        self.m = module
        self.feats_appended_to_coords = feats_appended_to_coords
        self.return_coords_and_edges = return_coords_and_edges

    def forward(self, h, *args, **kwargs):
        if self.feats_appended_to_coords:
            feats = h[:, 3:]
            res = torch.hstack([h[:, :3], self.m(feats)])
        else:
            res = self.m(h)
        if self.return_coords_and_edges:
            return res, kwargs['coord'], kwargs['edge_attr'], kwargs.get(
                'edge_messages', None)
        return res


class PNNGeometricBase(PointNeuralNetworkBase):
    """Base (abstract) class for all pytorch geometric point neural networks."""

    def forward(self, graph):
        feats, edges, coords, edge_attributes, batch = self.unpack_graph(graph)
        feats, messages = self.get_embeddings(
            feats, edges, coords, edge_attributes, batch)
        size = feats.size(0)
        row, col = edges
        if self.linear_gap:
            if self.feats_linear_layers is not None:
                feats = self.feats_linear_layers(feats)
                feats = global_mean_pool(feats, batch)
            if self.edges_linear_layers is not None:
                agg = unsorted_segment_sum(
                    messages, row, num_segments=size)
                messages = self.edges_linear_layers(agg)
                messages = global_mean_pool(messages, batch)
        else:
            if self.feats_linear_layers is not None:
                feats = global_mean_pool(feats, batch)
                feats = self.feats_linear_layers(feats)
            if self.edges_linear_layers is not None:
                agg = unsorted_segment_sum(
                    messages, row, num_segments=size)
                messages = global_mean_pool(agg, batch)
                messages = self.edges_linear_layers(messages)
        if self.feats_linear_layers is not None and \
                self.edges_linear_layers is not None:
            return torch.add(feats.squeeze(), messages.squeeze())
        elif self.feats_linear_layers is not None:
            return feats
        elif self.edges_linear_layers is not None:
            return messages
        raise RuntimeError('We must either classify on feats, edges or both.')

    def process_graph(self, graph):
        y_true = graph.y.float()
        y_pred = self(graph).reshape(-1, )
        ligands = graph.lig_fname
        receptors = graph.rec_fname
        return y_pred, y_true, ligands, receptors

    @abstractmethod
    def get_embeddings(self, feats, edges, coords, edge_attributes, batch):
        """Implement code to go from input features to final node embeddings."""
        pass

    def prepare_input(self, x):
        return x.cuda()

    @staticmethod
    def unpack_graph(graph):
        return (graph.x.float().cuda(), graph.edge_index.cuda(),
                graph.pos.float().cuda(), graph.edge_attr.cuda(),
                graph.batch.cuda())

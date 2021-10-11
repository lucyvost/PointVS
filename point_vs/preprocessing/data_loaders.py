"""
DataLoaders to take parquet directories and create feature vectors suitable
for use by models found in this project.
"""
import multiprocessing as mp
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch
from torch.nn.functional import one_hot
from torch.utils.data import DataLoader
from torch.utils.data import WeightedRandomSampler
from torch_geometric.data import DataLoader as GeoDataLoader, Data

from point_vs.preprocessing.preprocessing import make_box, \
    concat_structs, make_bit_vector, uniform_random_rotation, generate_edges
from point_vs.utils import load_yaml


class PointCloudDataset(torch.utils.data.Dataset):
    """Class for feeding structure parquets into network."""

    def __init__(
            self, base_path, radius=12, receptors=None,
            polar_hydrogens=True, use_atomic_numbers=False,
            compact=True, rot=False, augmented_active_count=0,
            augmented_active_min_angle=90, max_active_rms_distance=None,
            min_inactive_rms_distance=None, fname_suffix='parquet',
            types_fname=None, edge_radius=None, **kwargs):
        """Initialise dataset.

        Arguments:
            base_path: path containing the 'receptors' and 'ligands'
                directories, which in turn contain <rec_name>.parquets files
                and folders called <rec_name>_[active|decoy] which in turn
                contain <ligand_name>.parquets files. All parquets files from
                this directory are recursively loaded into the dataset.
            radius: size of the bounding box; all atoms further than <radius>
                Angstroms from the mean ligand atom position are discarded.
            receptors: iterable of strings denoting receptors to include in
                the dataset. if None, all receptors found in base_path are
                included.
            polar_hydrogens: include polar hydrogens as input
            use_atomic_numbers: use atomic numbers rather than sminatypes
            compact: compress 1hot vectors by using a single bit to
                signify whether atoms are from the receptor or ligand rather
                than using two input bits per atom type
            rot: random rotation of inputs
            augmented_active_count: number of actives to be rotated randomly
                and used as decoys (per active in the training set)
            augmented_active_min_angle: minimum angle of rotation for each
                augmented active (as specified in augmented_active_count)
            max_active_rms_distance: (pose selection) maximum rmsd between
                relaxed crystal structure and crystal structure
            min_inactive_rms_distance: (pose selection) minimum rmsd between
                redocked and relaxed crystal structure
            include_relaxed: (pose selection) include the relaxed crystal
                structure as an active
            types_fname:
            edge_radius:
            kwargs: keyword arguments passed to the parent class (Dataset).
        """

        assert not ((max_active_rms_distance is None) != (
                min_inactive_rms_distance is None))
        super().__init__(**kwargs)
        self.radius = radius
        self.base_path = Path(base_path).expanduser()
        if edge_radius > 0:
            self.edge_radius = edge_radius

        self.fname_suffix = fname_suffix
        if not self.base_path.exists():
            raise FileNotFoundError(
                'Dataset {} does not exist.'.format(self.base_path))
        self.polar_hydrogens = polar_hydrogens
        self.use_atomic_numbers = use_atomic_numbers
        self.compact = compact

        labels = []
        self.use_types = False if types_fname is None else True
        if max_active_rms_distance is not None \
                or min_inactive_rms_distance is not None:
            label_by_rmsd = True
        else:
            label_by_rmsd = False

        aug_recs, aug_ligs = [], []
        confirmed_ligs = []
        confirmed_recs = []
        if self.use_types:
            _labels, rmsds, receptor_fnames, ligand_fnames = \
                types_to_list(types_fname)

            # Do we use provided labels or do we generate our own using rmsds?
            labels = [] if label_by_rmsd else _labels
            for path_idx, (receptor_fname, ligand_fname) in enumerate(
                    zip(receptor_fnames, ligand_fnames)):
                if label_by_rmsd:
                    # Pose selection, filter by max/min active/inactive rmsd
                    # from xtal poses
                    rmsd = rmsds[path_idx]
                    if rmsd < 0:
                        continue
                    elif rmsd < max_active_rms_distance:
                        labels.append(1)
                        aug_ligs += [ligand_fname] * augmented_active_count
                        aug_recs += [receptor_fname] * augmented_active_count
                    elif rmsd >= min_inactive_rms_distance:
                        labels.append(0)
                    else:  # discard this entry (do not add to confirmed_ligs)
                        continue
                elif labels[path_idx]:
                    aug_ligs += [ligand_fname] * augmented_active_count
                    aug_recs += [receptor_fname] * augmented_active_count
                confirmed_ligs.append(ligand_fname)
                confirmed_recs.append(receptor_fname)
            self.receptor_fnames = confirmed_recs + aug_recs
        else:
            if receptors is None:
                print('Loading all structures in', self.base_path)
                ligand_fnames = list(
                    (self.base_path / 'ligands').glob('**/*.' + fname_suffix))
            else:
                print('Loading receptors:')
                ligand_fnames = []
                for receptor in receptors:
                    print(receptor)
                    ligand_fnames += list((self.base_path / 'ligands').glob(
                        '{0}*/*.{1}'.format(receptor, fname_suffix)))
                print('Loading all structures in', self.base_path)
                ligand_fnames = sorted(ligand_fnames)
            if label_by_rmsd:
                rmsd_info_fname = Path(self.base_path, 'rmsd_info.yaml')
                rmsd_info = load_yaml(rmsd_info_fname)

            for path_idx, ligand_fname in enumerate(ligand_fnames):
                if label_by_rmsd:
                    if str(ligand_fname.parent.name).find('active') != -1:
                        continue
                    pdbid = ligand_fname.parent.name.split('_')[0]
                    idx = int(Path(ligand_fname.name).stem.split('_')[-1])
                    try:
                        rmsd = rmsd_info[pdbid]['docked_wrt_crystal'][idx]
                    except KeyError:
                        continue
                    if rmsd < 0:
                        continue
                    if rmsd < max_active_rms_distance:
                        labels.append(1)
                        aug_ligs += [ligand_fname] * augmented_active_count
                    elif rmsd >= min_inactive_rms_distance:
                        labels.append(0)
                    else:  # discard this entry (do not add to confirmed_ligs)
                        continue
                else:
                    if str(ligand_fname.parent.name).find('active') == -1:
                        labels.append(0)
                    else:
                        labels.append(1)
                        aug_ligs += [ligand_fname] * augmented_active_count
                confirmed_ligs.append(ligand_fname)
                self.receptor_fnames = None

        self.pre_aug_ds_len = len(ligand_fnames)
        self.ligand_fnames = confirmed_ligs + aug_ligs

        labels += [0] * len(aug_ligs)
        labels = np.array(labels)
        active_count = np.sum(labels)
        class_sample_count = np.array(
            [len(labels) - active_count, active_count])
        if np.sum(labels) == len(labels) or np.sum(labels) == 0:
            self.sampler = None
        else:
            weights = 1. / class_sample_count
            self.sample_weights = torch.from_numpy(
                np.array([weights[i] for i in labels]))
            self.sampler = torch.utils.data.WeightedRandomSampler(
                self.sample_weights, len(self.sample_weights)
            )
        self.labels = labels
        print('There are', len(labels), 'training points in', base_path)

        # apply random rotations to ALL coordinates?
        self.transformation = uniform_random_rotation if rot else lambda x: x

        if use_atomic_numbers:
            # H C N O F P S Cl
            recognised_atomic_numbers = (6, 7, 8, 9, 15, 16, 17)
            # various metal ions/halogens which share valence properties
            other_groupings = ((35, 53), (3, 11, 19), (4, 12, 20), (26, 29, 30))
            atomic_number_to_index = {
                num: idx for idx, num in enumerate(recognised_atomic_numbers)
            }
            for grouping in other_groupings:
                atomic_number_to_index.update({elem: max(
                    atomic_number_to_index.values()) + 1 for elem in grouping})
            if self.polar_hydrogens:
                atomic_number_to_index.update({
                    1: max(atomic_number_to_index.values()) + 1
                })

            # +1 to accommodate for unmapped elements
            self.max_feature_id = max(atomic_number_to_index.values()) + 1

            # Any other elements not accounted for given a category of their own
            self.atomic_number_to_index = defaultdict(
                lambda: self.max_feature_id)
            self.atomic_number_to_index.update(atomic_number_to_index)

        elif polar_hydrogens:
            self.max_feature_id = 11  # FID = 10 if polar hydrogen
        else:
            self.max_feature_id = 10  # No polar hydrogens

        if compact:
            self.feature_dim = self.max_feature_id + 2
        else:
            self.feature_dim = (self.max_feature_id + 1) * 2

        self.augmented_active_min_angle = augmented_active_min_angle

    def __len__(self):
        """Return the total size of the dataset."""
        return len(self.ligand_fnames)

    def get_item(self, item):
        label = self.labels[item]
        if self.use_types:
            lig_fname = Path(self.base_path, self.ligand_fnames[item])
            rec_fname = Path(self.base_path, self.receptor_fnames[item])
        else:
            lig_fname = self.ligand_fnames[item]
            rec_name = lig_fname.parent.name.split('_')[0]
            try:
                rec_fname = next((self.base_path / 'receptors').glob(
                    '{0}*.{1}'.format(rec_name, self.fname_suffix)))
            except StopIteration:
                raise RuntimeError(
                    'Receptor for ligand {0} not found. Looking for file '
                    'named {1}'.format(
                        lig_fname, rec_name + '.' + self.fname_suffix))

        # Are we using an active and labelling it as a decoy through random
        # rotation? This determination is made using the index, made possible
        # due to the construction of the filenames class variable in the
        # constructor: all actives labelled as decoys are found at the end.
        if item < self.pre_aug_ds_len:
            aug_angle = 0
        else:
            aug_angle = self.augmented_active_min_angle

        struct = make_box(concat_structs(
            rec_fname, lig_fname, min_lig_rotation=aug_angle),
            radius=self.radius, relative_to_ligand=True)

        if not self.polar_hydrogens:
            struct = struct[struct['atomic_number'] > 1]

        if self.use_atomic_numbers:
            struct.types = struct['atomic_number'].map(
                self.atomic_number_to_index) + struct.bp * (
                                   self.max_feature_id + 1)

        p = torch.from_numpy(
            self.transformation(
                struct[struct.columns[:3]].to_numpy()))

        v = make_bit_vector(
            struct.types.to_numpy(), self.max_feature_id + 1, self.compact)

        return p, v, label, lig_fname, rec_fname, struct

    def __getitem__(self, item):
        """Given an index, locate and preprocess relevant parquet file.

        Arguments:
            item: index in the list of filenames denoting which ligand and
                receptor to fetch

        Returns:
            Tuple containing (a) a tuple with a list of tensors: cartesian
            coordinates, feature vectors and masks for each point, as well as
            the number of points in the structure and (b) the label \in \{0, 1\}
            denoting whether the structure is an active or a decoy.
        """
        p, v, label, lig_fname, rec_fname, struct = self.get_item(item)

        return (p, v, len(struct)), lig_fname, rec_fname, label


class PygPointCloudDataset(PointCloudDataset):
    """Class for feeding structure parquets into network."""

    def __getitem__(self, item):
        """Given an index, locate and preprocess relevant parquet file.

        Arguments:
            item: index in the list of filenames denoting which ligand and
                receptor to fetch

        Returns:
            Tuple containing (a) a tuple with a list of tensors: cartesian
            coordinates, feature vectors and masks for each point, as well as
            the number of points in the structure and (b) the label \in \{0, 1\}
            denoting whether the structure is an active or a decoy.
        """
        p, v, label, lig_fname, rec_fname, struct = self.get_item(item)

        if hasattr(self, 'edge_radius'):
            edge_radius = self.edge_radius
        else:
            edge_radius = 4
        edge_indices, edge_attrs = generate_edges(struct, radius=edge_radius)

        edge_indices = torch.from_numpy(np.vstack(edge_indices)).long()
        edge_attrs = one_hot(torch.from_numpy(edge_attrs).long(), 3)

        return Data(
            x=v,
            edge_index=edge_indices,
            edge_attr=edge_attrs,
            pos=p,
            y=torch.from_numpy(np.array(label)).long(),
            m=torch.ones(len(struct)),
            rec_fname=rec_fname,
            lig_fname=lig_fname,
        )


def get_data_loader(
        *data_roots, dataset_class, receptors=None, batch_size=32, compact=True,
        use_atomic_numbers=False, radius=6, rot=True,
        augmented_actives=0, min_aug_angle=30,
        polar_hydrogens=True, mode='train',
        max_active_rms_distance=None, fname_suffix='parquet',
        min_inactive_rms_distance=None, types_fname=None, edge_radius=None):
    """Give a DataLoader from a list of receptors and data roots."""
    ds_kwargs = {
        'receptors': receptors,
        'radius': radius,
        'rot': rot
    }
    ds = multiple_source_dataset(
        dataset_class, *data_roots, balanced=True, compact=compact,
        polar_hydrogens=polar_hydrogens, augmented_actives=augmented_actives,
        min_aug_angle=min_aug_angle, use_atomic_numbers=use_atomic_numbers,
        max_active_rms_distance=max_active_rms_distance,
        min_inactive_rms_distance=min_inactive_rms_distance,
        fname_suffix=fname_suffix, types_fname=types_fname,
        edge_radius=edge_radius, **ds_kwargs)
    sampler = ds.sampler if mode == 'train' else None
    if dataset_class == PointCloudDataset:
        collate = get_collate_fn(ds.feature_dim)
        return DataLoader(
            ds, batch_size, False, sampler=sampler,
            collate_fn=collate, drop_last=False, pin_memory=True)
    else:
        return GeoDataLoader(
            ds, batch_size, False, sampler=sampler,
            drop_last=False, pin_memory=True)


def multiple_source_dataset(
        loader_class, *base_paths, receptors=None, polar_hydrogens=True,
        augmented_actives=0, min_aug_angle=30, max_active_rms_distance=None,
        min_inactive_rms_distance=None, compact=True, use_atomic_numbers=False,
        fname_suffix='parquet', balanced=True, types_fname=None,
        edge_radius=None, **kwargs):
    """Concatenate mulitple datasets into one, preserving balanced sampling.

    Arguments:
        loader_class: one of either PointCloudDataset or SE3TransformerLoader,
            inheriting from Datset. This class should have in its constructor a
            list of binary labels associated with each item, stored in
            <class>.labels.
        base_paths: locations of parquets files, one for each dataset.
        receptors: receptors to include. If None, all receptors found are used.
        polar_hydrogens:
        augmented_actives: number of actives to be rotated randomly and used as
            decoys (per active in the training set)
        min_aug_angle: minimum angle of rotation for each augmented active (as
            specified in augmented_active_count)
        max_active_rms_distance: (pose selection) maximum rmsd between active
            docked and crystal structures
        min_inactive_rms_distance: (pose selection) minimum rmsd between
            inactive docked and crystal structures
        compact: compress 1hot vectors by using a single bit to
            signify whether atoms are from the receptor or ligand rather
            than using two input bits per atom type
        use_atomic_numbers: use atomic numbers rather than sminatypes
        fname_suffix:
        balanced: whether to sample probabailistically based on class imbalance.
        types_fname:
        kwargs: other keyword arguments for loader_class.

    Returns:
        Concatenated dataset including balanced sampler.
    """
    datasets = []
    labels = []
    filenames = []
    base_paths = sorted(
        [Path(bp).expanduser() for bp in base_paths if bp is not None])
    for base_path in base_paths:
        if base_path is not None:
            dataset = loader_class(
                base_path, compact=compact, receptors=receptors,
                augmented_active_count=augmented_actives,
                augmented_active_min_angle=min_aug_angle,
                polar_hydrogens=polar_hydrogens,
                max_active_rms_distance=max_active_rms_distance,
                min_inactive_rms_distance=min_inactive_rms_distance,
                use_atomic_numbers=use_atomic_numbers,
                fname_suffix=fname_suffix,
                types_fname=types_fname,
                edge_radius=edge_radius,
                **kwargs)
            labels += list(dataset.labels)
            filenames += dataset.ligand_fnames
            datasets.append(dataset)
    labels = np.array(labels)
    active_count = np.sum(labels)
    class_sample_count = np.array(
        [len(labels) - active_count, active_count])
    if np.sum(labels) == len(labels) or np.sum(labels) == 0 or not balanced:
        sampler = None
    else:
        weights = 1. / class_sample_count
        sample_weights = torch.from_numpy(
            np.array([weights[i] for i in labels]))
        sampler = WeightedRandomSampler(sample_weights, len(sample_weights))
    multi_source_dataset = torch.utils.data.ConcatDataset(datasets)
    multi_source_dataset.sampler = sampler
    multi_source_dataset.class_sample_count = class_sample_count
    multi_source_dataset.labels = labels
    multi_source_dataset.filenames = filenames
    multi_source_dataset.base_path = ', '.join([str(bp) for bp in base_paths])
    multi_source_dataset.feature_dim = datasets[0].feature_dim
    return multi_source_dataset


def types_to_list(types_fname):
    """Take a types file and returns four lists containing paths and labels.

    Types files should be of the format:
        <label> <...> <rmsd> <receptor_filename> <ligand_filename> <...>
    where:
        <label> is a label \in \{0, 1\}
        <...> is 0 or more other fields
        <rmsd> is the root mean squared distance of the pose from the crystal
            pose
        <receptor_fname> is the location of the receptor file
        <ligand_fname> is the location of the ligand file

    Arguments:
        types_fname: location of types file

    Returns:
        Four lists containing the labels, rmsds, receptor paths and ligand
        paths.
    """

    def find_paths(types_line):
        recpath, ligpath = None, None
        chunks = types_line.strip().split()
        label = int(chunks[0])
        for idx, chunk in enumerate(chunks):
            if chunk.startswith('#'):
                continue
            try:
                float(chunk)
            except ValueError:
                if recpath is None:
                    recpath = chunk
                    rmsd = float(chunks[idx - 1])
                else:
                    ligpath = chunk
                    break
        return label, rmsd, recpath, ligpath

    labels, rmsds, recs, ligs = [], [], [], []
    with open(types_fname, 'r') as f:
        for line in f.readlines():
            label, rmsd, rec, lig = find_paths(line)
            if rec is not None and lig is not None:
                labels.append(label)
                rmsds.append(rmsd)
                recs.append(rec)
                ligs.append(lig)
    return labels, rmsds, recs, ligs


def get_collate_fn(dim):
    """Processing of inputs which takes place after batch is selected.

    LieConv networks take tuples of torch tensors (p, v, m), which are:
        p, (batch_size, n_atoms, 3): coordinates of each atom
        v, (batch_size, n_atoms, n_features): features for each atom
        m, (batch_size, n_atoms): mask for each coordinate slot

    Note that n_atoms is the largest number of atoms in a structure in
    each batch.

    Arguments:
        dim: size of input (node) features

    Returns:
        Pytorch collate function
    """

    def collate(batch):
        max_len = max([b[0][-1] for b in batch])
        batch_size = len(batch)
        p_batch = torch.zeros(batch_size, max_len, 3)
        v_batch = torch.zeros(batch_size, max_len, dim)
        m_batch = torch.zeros(batch_size, max_len)
        label_batch = torch.zeros(batch_size, )
        ligands, receptors = [], []
        if max_len > 300:
            print('\n\n\n\n\n\n')
        for batch_index, ((p, v, size), ligand, receptor, label) in enumerate(
                batch):
            p_batch[batch_index, :size, :] = p
            v_batch[batch_index, :size, :] = v
            m_batch[batch_index, :size] = 1
            label_batch[batch_index] = label
            ligands.append(ligand)
            receptors.append(receptor)
            if max_len > 300:
                print(receptor, ligand, size)
        if max_len > 300:
            print('\n\n\n\n\n\n')
        return (p_batch, v_batch,
                m_batch.bool()), label_batch.float(), ligands, receptors

    return collate

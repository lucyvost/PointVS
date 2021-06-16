"""Driver script for various graph attribution methods."""

import argparse
import urllib
from pathlib import Path

import yaml
from plip.basic.supplemental import extract_pdbid
from plip.exchange.webservices import fetch_pdb

from point_vs.attribution.attribution_fns import masking, cam
from point_vs.attribution.process_pdb import score_and_colour_pdb
from point_vs.models.egnn_network import EGNN
from point_vs.models.lie_conv import LieResNet
from point_vs.models.lie_transformer import EquivariantTransformer
from point_vs.utils import mkdir

ALLOWED_METHODS = ('masking', 'cam')


def download_pdb_file(pdbid, output_dir):
    """Given a PDB ID, downloads the corresponding PDB structure.
    Checks for validity of ID and handles error while downloading.
    Returns the path of the downloaded file (From PLIP)"""
    pdbid = pdbid.lower()
    output_dir = Path(output_dir).expanduser()
    pdbpath = output_dir / '{}.pdb'.format(pdbid)
    if pdbpath.is_file():
        print(pdbpath, 'already exists.')
        return pdbpath
    if len(pdbid) != 4 or extract_pdbid(
            pdbid.lower()) == 'UnknownProtein':
        raise RuntimeError('Unknown protein ' + pdbid)
    while True:
        try:
            pdbfile, pdbid = fetch_pdb(pdbid.lower())
        except urllib.error.URLError:
            print('Fetching pdb {} failed, retrying...'.format(
                pdbid))
        else:
            break
    if pdbfile is None:
        return 'none'
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(pdbpath, 'w') as g:
        g.write(pdbfile)
    print('File downloaded as', pdbpath)
    return pdbpath


def load_model(weights_file):
    model_path = Path(weights_file).expanduser()
    with open(model_path.parents[1] / 'model_kwargs.yaml', 'r') as f:
        model_kwargs = yaml.load(f, Loader=yaml.Loader)
    with open(model_path.parents[1] / 'cmd_args.yaml', 'r') as f:
        cmd_line_args = yaml.load(f, Loader=yaml.Loader)

    model_type = cmd_line_args['model']

    model_class = {
        'lietransformer': EquivariantTransformer,
        'lieconv': LieResNet,
        'egnn': EGNN
    }

    model_class = model_class[model_type]
    model = model_class(Path(), learning_rate=0, weight_decay=0,
                        silent=True, **model_kwargs)

    model.load_weights(model_path)
    model.eval()

    return model, model_kwargs, cmd_line_args


def attribute(args):
    if args.pdbid is not None:
        output_dir = mkdir(
            Path(args.output_dir, args.attribution_type, args.pdbid))
        pdbpath = download_pdb_file(args.pdbid, output_dir)
    else:
        leaf_dir = Path(Path(args.input_file).name).stem
        output_dir = mkdir(
            Path(args.output_dir, args.attribution_type, leaf_dir))
        pdbpath = Path(args.input_file).expanduser()

    model, model_kwargs, cmd_line_args = load_model(args.model)

    attribution_fn = {'masking': masking, 'cam': cam}[args.attribution_type]

    score_and_colour_pdb(model, attribution_fn,
                         str(pdbpath), str(output_dir),
                         model_args=cmd_line_args,
                         only_process=args.only_process)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('attribution_type', type=str,
                        help='Method of graph attribution; just {} for '
                             'now.'.format(', '.join(ALLOWED_METHODS)))
    parser.add_argument('model', type=str, help='Saved pytorch model')
    parser.add_argument('output_dir', type=str,
                        help='Directory in which to store results')
    parser.add_argument('--pdbid', '-p', type=str,
                        help='PDB ID for structure to analyse')
    parser.add_argument('--input_file', '-i', type=str,
                        help='Input PDB file')
    parser.add_argument('--only_process', '-o', type=str, nargs='?', default=[],
                        help='Only process ligands with the given 3 letter '
                             'residue codes (UNK, for example)')
    args = parser.parse_args()
    if args.pdbid is not None and args.input_file is not None:
        raise RuntimeError('--pdbid and --input_file are mutually exclusive')
    attribute(args)

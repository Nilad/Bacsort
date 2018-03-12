#!/usr/bin/env python3

import argparse
import edlib
import glob
import gzip
import os
import pathlib
import re
import sys
from multiprocessing.pool import ThreadPool


def get_arguments():
    parser = argparse.ArgumentParser(description='Distance matrix from rMLST gene identity')

    parser.add_argument('assembly_dir', type=str,
                        help='Directory containing assembly fasta files and rMLST files')
    parser.add_argument('out_file', type=str,
                        help='Filename for distance matrix output')

    parser.add_argument('--search_tool', type=str, required=False, default='minimap',
                        help='Which tool to use for finding rMLST genes (must be "blast" or "minimap"')
    parser.add_argument('--threads', type=int, required=False, default=8,
                        help='Number of CPU threads to use')
    parser.add_argument('--min_cov', type=float, required=False, default=95.0,
                        help='Minimum coverage to use in gene search')
    parser.add_argument('--min_id', type=float, required=False, default=90.0,
                        help='Minimum identity to use in gene search')

    args = parser.parse_args()
    return args


def main():
    args = get_arguments()

    assembly_files = get_fastas(args.assembly_dir)

    gene_seqs = {}
    print('\nLoading rMLST genes:')
    for assembly in assembly_files:
        rmlst_file = assembly + '.rmlst'
        assembly_name = os.path.basename(assembly)
        if not pathlib.Path(rmlst_file).is_file():
            sys.exit('Error: {} is missing'.format(rmlst_file))
        print('  {}'.format(rmlst_file))
        gene_seqs[assembly_name] = load_fasta(rmlst_file)

    print('\nCalculating pairwise distances', end='', flush=True)
    distances = {}
    for i in range(len(assembly_files)):
        assembly_1 = os.path.basename(assembly_files[i])

        if args.threads == 1:
            for j in range(i, len(assembly_files)):
                assembly_2 = os.path.basename(assembly_files[j])
                get_assembly_distance(assembly_1, assembly_2, gene_seqs, distances)
        else:
            pool = ThreadPool(args.threads)
            assembly_2s = [os.path.basename(assembly_files[j]) for j in range(i, len(assembly_files))]
            pool.map(lambda assembly_2: get_assembly_distance(assembly_1, assembly_2, gene_seqs, distances), assembly_2s)
        print('.', end='', flush=True)

    print('done')
    print('\nWriting distance matrix to file')
    write_phylip_distance_matrix(assembly_files, distances, args.out_file)


def find_gene_seq_for_assembly(assembly, db_name, gene, gene_seqs, assembly_seqs, args):
    query_name = query_name_from_filename(gene)
    hit = get_best_match(db_name, gene, args.search_tool, assembly_seqs, args.min_cov, args.min_id)
    if hit is None:
        print('    {}: none'.format(query_name))
        gene_seqs[assembly][query_name] = None
    else:
        print('    {}: {}, {:.2f}% cov, {:.2f}% id, {} bp '
              '({}...{})'.format(query_name, hit.name, hit.coverage,hit.identity, len(hit.seq),
                                 hit.seq[:6], hit.seq[-6:]))
        gene_seqs[assembly][query_name] = hit.seq


def get_assembly_distance(assembly_1, assembly_2, gene_seqs, distances):
    if assembly_1 == assembly_2:
        distances[(assembly_1, assembly_2)] = 0.0
        return

    pattern = re.compile(r'\d+[\w=]')
    common_genes = set(gene_seqs[assembly_1]) & set(gene_seqs[assembly_2])
    if not common_genes:
        distances[(assembly_1, assembly_2)] = 1.0
        distances[(assembly_2, assembly_1)] = 1.0
        return

    alignments = []
    for gene in common_genes:
        gene_1 = gene_seqs[assembly_1][gene]
        gene_2 = gene_seqs[assembly_2][gene]
        result = edlib.align(gene_1, gene_2, 'NW', 'path')
        cigar = [(int(x[:-1]), x[-1]) for x in pattern.findall(result['cigar'])]
        alignment_length = sum(x[0] for x in cigar)
        match_count = sum(x[0] for x in cigar if x[1] == '=')
        identity = match_count / alignment_length
        alignments.append((identity, match_count, alignment_length))

    # alignments = sorted(alignments, key=lambda x: x[0])
    # discard_count = 0
    # while True:
    #     if (discard_count + 2) / len(alignments) < discard_best_worst_alignments:
    #         discard_count += 2
    #     else:
    #         break
    # discard_each_end = discard_count // 2
    # alignments = alignments[discard_count:-discard_count]

    total_identity = sum(x[1] for x in alignments) / sum(x[2] for x in alignments)
    distance = 1.0 - total_identity

    distances[(assembly_1, assembly_2)] = distance
    distances[(assembly_2, assembly_1)] = distance


def write_phylip_distance_matrix(assembly_files, distances, output_filename):
    assemblies = [os.path.basename(a) for a in assembly_files]
    with open(output_filename, 'wt') as distance_matrix:
        distance_matrix.write(str(len(assembly_files)))
        distance_matrix.write('\n')
        for i in assemblies:
            distance_matrix.write(i)
            for j in assemblies:
                distance_matrix.write('\t')
                try:
                    distance = distances[(i, j)]
                except KeyError:
                    distance = 1.0
                distance_matrix.write('%.6f' % distance)
            distance_matrix.write('\n')


def get_fastas(fasta_dir):
    fastas = glob.glob(fasta_dir + '/*.fna.gz')
    fastas += glob.glob(fasta_dir + '/*.fa.gz')
    fastas += glob.glob(fasta_dir + '/*.fas.gz')
    fastas += glob.glob(fasta_dir + '/*.fasta.gz')
    fastas += glob.glob(fasta_dir + '/*.fna')
    fastas += glob.glob(fasta_dir + '/*.fa')
    fastas += glob.glob(fasta_dir + '/*.fas')
    fastas += glob.glob(fasta_dir + '/*.fasta')
    return sorted(fastas)


def query_name_from_filename(query_filename):
    return os.path.basename(os.path.splitext(os.path.basename(query_filename))[0])


class BlastHit(object):
    def __init__(self, blast_line):
        parts = blast_line.split('\t')
        self.bitscore = float(parts[0])
        self.identity = float(parts[1])
        self.coverage = float(parts[2])
        self.seq = parts[3]
        self.name = parts[4]


class MinimapHit(object):
    def __init__(self, minimap_line):
        parts = minimap_line.split('\t')

        self.name = parts[0]

        q_length = int(parts[1])
        q_start = int(parts[2])
        q_end = int(parts[3])
        self.coverage = 100.0 * (q_end - q_start) / q_length

        self.strand = parts[4]
        self.contig_name = parts[5]
        self.start = int(parts[7])
        self.end = int(parts[8])

        matches = int(parts[9])
        alignment_length = int(parts[10])
        self.identity = 100.0 * matches / alignment_length

        self.score = int([x for x in parts if x.startswith('AS:i:')][0][5:])

        self.seq = ''


def get_compression_type(filename):
    magic_dict = {'gz': (b'\x1f', b'\x8b', b'\x08'),
                  'bz2': (b'\x42', b'\x5a', b'\x68'),
                  'zip': (b'\x50', b'\x4b', b'\x03', b'\x04')}
    max_len = max(len(x) for x in magic_dict)
    with open(filename, 'rb') as unknown_file:
        file_start = unknown_file.read(max_len)
    compression_type = 'plain'
    for file_type, magic_bytes in magic_dict.items():
        if file_start.startswith(magic_bytes):
            compression_type = file_type
    if compression_type == 'bz2':
        quit_with_error('cannot use bzip2 format - use gzip instead')
    if compression_type == 'zip':
        quit_with_error('cannot use zip format - use gzip instead')
    return compression_type


def get_open_function(filename):
    if get_compression_type(filename) == 'gz':
        return gzip.open
    else:  # plain text
        return open


def load_fasta(filename):
    fasta_seqs = {}
    open_func = get_open_function(filename)
    with open_func(filename, 'rt') as fasta_file:
        name = ''
        sequence = ''
        for line in fasta_file:
            line = line.strip()
            if not line:
                continue
            if line[0] == '>':  # Header line = start of new contig
                if name:
                    contig_name = name.split()[0]
                    if contig_name in fasta_seqs:
                        sys.exit('Error: duplicate contig names in {}'.format(filename))
                    fasta_seqs[contig_name] = sequence
                    sequence = ''
                name = line[1:]
            else:
                sequence += line
        if name:
            contig_name = name.split()[0]
            if contig_name in fasta_seqs:
                sys.exit('Error: duplicate contig names in {}'.format(filename))
            fasta_seqs[contig_name] = sequence
    contig_names = set(fasta_seqs.keys())
    return fasta_seqs


REV_COMP_DICT = {'A': 'T', 'T': 'A', 'G': 'C', 'C': 'G',
                 'a': 't', 't': 'a', 'g': 'c', 'c': 'g',
                 'R': 'Y', 'Y': 'R', 'S': 'S', 'W': 'W',
                 'K': 'M', 'M': 'K', 'B': 'V', 'V': 'B',
                 'D': 'H', 'H': 'D', 'N': 'N',
                 'r': 'y', 'y': 'r', 's': 's', 'w': 'w',
                 'k': 'm', 'm': 'k', 'b': 'v', 'v': 'b',
                 'd': 'h', 'h': 'd', 'n': 'n',
                 '.': '.', '-': '-', '?': '?'}


def reverse_complement(seq):
    return ''.join([complement_base(x) for x in seq][::-1])


def complement_base(base):
    try:
        return REV_COMP_DICT[base]
    except KeyError:
        return 'N'


if __name__ == '__main__':
    main()

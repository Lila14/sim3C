#!/usr/bin/env python2

import itertools
import logging
import random
import os
from collections import OrderedDict

import community as com
import networkx as nx
import numpy as np
import pysam
from Bio import SeqIO
from Bio.Restriction import Restriction
from deap import creator, base, tools
from deap.algorithms import varOr
from intervaltree import IntervalTree, Interval
from numba import jit, int64, float64, boolean
from numba import vectorize, int32
from numpy import log, pi
from scoop import futures, shared


logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                    datefmt='%m-%d %H:%M',
                    filename='map_ordering.log',
                    filemode='w')
console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter('%(name)-12s: %(levelname)-8s %(message)s')
console.setFormatter(formatter)
logger = logging.getLogger('map_ordering')
logger.addHandler(console)


def get_enzyme_instance(enz_name):
    """
    Fetch an instance of a given restriction enzyme by its name.
    :param enz_name: the case-sensitive name of the enzyme
    :return: RestrictionType the enzyme instance
    """
    return getattr(Restriction, enz_name)


def upstream_dist(read, cut_sites, ref_length, linear=False):
    """
    Upstream distance to nearest cut-site from the end of this
    reads alignment. When linear, it is possible that a cutsite will not
    exist upstream. In these situations, None is returned. For circular
    references, the separation is measured from the nearest cut-site
    that cross the origin.
    :param read: read in question
    :param cut_sites: the cut-sites for the aligning reference
    :param ref_length: the reference length
    :param linear: treat reference as linear
    :return: None or separation between end of read and nearest upstream cutsite.     
    """
    d = None
    if read.is_reverse:
        # reverse reads look left
        r_end = read.pos
        xi = np.searchsorted(cut_sites, r_end, side='left')
        if xi > 0:
            d = r_end - cut_sites[xi - 1]
        elif r_end == cut_sites[0]:
            d = 0
        elif not linear:
            # crossing the origin
            d = ref_length - cut_sites[-1] + r_end
    else:
        # forward reads look right
        r_end = read.pos + read.alen
        xi = np.searchsorted(cut_sites, r_end, side='right')
        if xi < len(cut_sites):
            d = cut_sites[xi] - r_end
        elif r_end == cut_sites[-1]:
            d = 0
        elif not linear:
            # crossing the origin
            d = cut_sites[0] - (r_end - ref_length)
    return d


# Cigar codes that are not permitted.
NOT_ALLOWED = {2, 3, 6, 8}


def good_match(cigartuples, min_match=None, match_start=True):
    """
    A confident mapped read.
    :param cigartuples: a read's CIGAR in tuple form
    :param min_match: the minimum number of matched base positions
    :param match_start: alignment must begin at read's first base position
    :return:
    """

    # restrict tuples to a subset of possible conditions
    if len(NOT_ALLOWED & set([t[0] for t in cigartuples])) != 0:
        return False

    # match the first N bases if requested
    elif match_start and cigartuples[0][0] != 0:
        return False

    # impose minimum number of matches
    elif min_match:
        n_matches = sum([t[1] for t in cigartuples if t[0] == 0])
        if n_matches < min_match:
            return False

    return True


def strong_match(mr, min_match=None, match_start=True, min_mapq=None):
    """
    Augment a good_match() by also checking for whether a read is secondary or
    supplementary.
    :param mr: mapped read to test
    :param min_match: the minimum number of matched base positions
    :param match_start: alignment must begin at read's first base position
    :param min_mapq: the minimum acceptable mapping quality. This can be mapper dependent. For BWA MEM
    any read which perfectly aligns in two distinct places, as mapq=0.
    """
    if min_mapq and mr.mapping_quality < min_mapq:
        return False

    elif mr.is_secondary or mr.is_supplementary:
        return False

    # reverse the tuple if this read is reversed, as we're testing from 0-base on read.
    cigtup = mr.cigartuples[::-1] if mr.is_reverse else mr.cigartuples

    return good_match(cigtup, min_match, match_start)


def inverse_edge_weights(g):
    """
    Invert the weights on a graph's edges
    :param g: the graph
    """
    for u, v in g.edges():
        g.edge[u][v]['weight'] = 1.0 / g[u][v]['weight']


def edgeiter_to_nodelist(edge_iter):
    """
    Create a list of nodes from an edge iterator
    :param edge_iter: edge iterator
    :return: list of node ids
    """
    nlist = []
    for ei in edge_iter:
        for ni in ei:
            if ni not in nlist:
                nlist.append(ni)
    return nlist


def dfs_weighted(g, source=None):
    """
    Depth first search
    :param g: 
    :param source: 
    :return: 
    """
    if source is None:
        # produce edges for all components
        nodes = g
    else:
        # produce edges for components with source
        nodes = [source]
    visited = set()
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(g[start], key=lambda x: -g[start][x]['weight'])))]
        while stack:
            parent, children = stack[-1]
            try:
                child = next(children)
                if child not in visited:
                    yield parent, child
                    visited.add(child)
                    stack.append((child, iter(sorted(g[child], key=lambda x: -g[child][x]['weight']))))
            except StopIteration:
                stack.pop()


def decompose_graph(g):
    """
    Using the Louvain algorithm for community detection, as
    implemented in the community module, determine the partitioning
    which maximises the modularity. For each individual partition
    create the sub-graph of g

    :param g: the graph to decompose
    :return: the set of sub-graphs which form the best partitioning of g
    """
    decomposed = []
    part = com.best_partition(g)
    part_labels = np.unique(part.values())

    # for each partition, create the sub-graph
    for pi in part_labels:
        # start with a complete copy of the graph
        gi = g.copy()
        # build the list of nodes not in this partition and remove them
        to_remove = [n for n in g.nodes_iter() if part[n] != pi]
        gi.remove_nodes_from(to_remove)
        decomposed.append(gi)

    return decomposed


class Grouping:

    def __init__(self, seq_info, bin_size, min_sites):
        self.bins = []
        self.map = OrderedDict()
        self.index = OrderedDict()
        self.bin_size = bin_size
        self.min_sites = min_sites

        total_sites = 0

        for n, ix in enumerate(seq_info.keys()):

            num_sites = len(seq_info[ix]['sites'])

            if num_sites == 0:
                raise RuntimeError('Sequence has no sites')

            # required bins is the quotient, but the least number
            # of bins is 1.
            required_bins = num_sites / bin_size if num_sites >= bin_size else 1
            if num_sites > bin_size and float(num_sites % bin_size) / bin_size >= 0.5:
                # add an extra bin when there is a sufficient number of left-overs
                # but do not split a single bin sequence.
                required_bins += 1

            if required_bins == 1 and num_sites < min_sites:
                logger.debug('Sequence had only {0} sites, which is less '
                              'than the minimum threshold {1}'.format(required_bins, min_sites))
                # remove the sequence from seq_info
                del seq_info[ix]
                continue

            # starting with integer indices, apply a scale factor to adjust
            # them matching the desired binning. This can then be digitised
            # to determine bin membership
            sclidx = np.arange(num_sites, dtype=np.int32) * float(required_bins) / num_sites

            # determine bin membership, but subtract one from each to get 0-based
            grp = np.digitize(sclidx, np.arange(required_bins)) - 1

            self.index[ix] = n
            self.bins.append(required_bins)
            self.map[ix] = np.column_stack((seq_info[ix]['sites'], grp))

            total_sites += num_sites

            logger.debug('{0} sites in {1} bins. Bins: {2}'.format(num_sites, required_bins, np.bincount(grp)))

        self.bins = np.array(self.bins)

        self.total = total_sites

        # calculate centers of each fragment groupings. This will be used
        # when measuring separation.
        self.centers = {}
        for ix, locs in self.map.iteritems():
            # centers with original input orientation
            fwd = np.array([locs[:, 0][np.where(locs[:, 1] == gi)].mean() for gi in np.unique(locs[:, 1])])
            # flipped orientation (i.e. seqlen - original centers)
            rev = seq_info[ix]['length'] - fwd
            self.centers[ix] = {True: rev, False: fwd}

    def total_bins(self):
        return np.sum(self.bins)

    def find_nearest(self, ctg_idx, x):
        """
        Find the nearest site from a given position on a contig.
        :param ctg_idx: contig index
        :param x: query position
        :return: tuple of site and group number
        """
        group_sites = self.map[ctg_idx]
        ix = np.searchsorted(group_sites[:, 0], x)
        if ix == 0:
            return group_sites[0, :]
        elif ix == group_sites.shape[0]:
            return group_sites[-1, :]
        else:
            x1 = group_sites[ix - 1, :]
            x2 = group_sites[ix, :]
            if x - x1[0] <= x2[0] - x:
                return x1
            else:
                return x2


@jit(int64[:](int64[:,:], int64), nopython=True)
def find_nearest_jit(group_sites, x):
    """
    Find the nearest site from a given position on a contig.
    :param group_sites:
    :param x: query position
    :return: tuple of site and group number
    """
    ix = np.searchsorted(group_sites[:, 0], x)
    if ix == 0:
        return group_sites[0, :]
    elif ix == group_sites.shape[0]:
        return group_sites[-1, :]
    else:
        x1 = group_sites[ix - 1, :]
        x2 = group_sites[ix, :]
        if x - x1[0] <= x2[0] - x:
            return x1
        else:
            return x2


class SeqOrder:

    def __init__(self, seq_info):
        """
        Initial order is determined by the order of supplied sequence information dictionary. Sequenes
        are given new surrogate ids of consecutive integers. Member functions expect surrogate ids
        not original names.

        :param seq_info: sequence information dictionary
        """
        self.names = [k for k in seq_info]
        self.lengths = np.array([v['length'] for v in seq_info.values()])
        _ord = np.arange(len(self.names))
        _ori = np.zeros(len(self.names), dtype=np.int)
        self.order = np.vstack((_ord, _ori)).T

    def set_only_order(self, _ord):
        """
        Set only the order, assuming orientation is ignored
        :param _ord: 1d ordering
        """
        assert len(_ord) == len(self.order), 'new order was a different length'
        self.order[:, 0] = _ord

    def set_ord_and_ori(self, _ord):
        """
        Set an order where _ord is a 2d (N x 2) array of numeric id and orientation (0,1)
        :param _ord: 2d ordering and orientation (N x 2)
        """
        assert isinstance(_ord, np.ndarray), 'ord/ori was not a numpy array'
        assert _ord.shape == self.order.shape, 'new ord/ori has different dimensions'
        self.order = _ord

    def get_name(self, _id):
        """
        :param _id: surrogate id of sequence
        :return: original name
        """
        return self.names[_id]

    def get_length(self, _id):
        """
        :param _id: surrogate id of sequence
        :return: sequence length
        """
        return self.lengths[_id]

    def before(self, a, b):
        """
        Test if a comes before another sequence in order.
        :param a: surrogate id of sequence a
        :param b: surrogate id of sequence b
        :return: True if a comes before b
        """
        assert a != b, 'Surrogate ids must be different'

        _o = self.order[:, 0]
        ia = np.argwhere(_o == a)
        ib = np.argwhere(_o == b)
        return ia < ib

    def intervening(self, a, b):
        """
        For the current order, calculate the length of intervening
        sequences between sequence a and sequence b.

        :param a: surrogate id of sequence a
        :param b: surrogate id of sequence b
        :return: total length of sequences between a and b.
        """
        assert a != b, 'Surrogate ids must be different'

        _o = self.order[:, 0]
        ix1, ix2 = np.where((_o == a) | (_o == b))[0]
        if ix1 > ix2:
            ix1, ix2 = ix2, ix1
        return np.sum(self.lengths[_o[ix1 + 1:ix2]])

    def shuffle(self):
        """
        Randomize order
        """
        np.random.shuffle(self.order)


@jit(boolean(int64[:, :], int64, int64), nopython=True)
def before(_ord, a, b):

    ix1 = None
    for i in xrange(len(_ord)):
        if a == _ord[i, 0]:
            ix1 = i
            break

    ix2 = None
    for i in xrange(len(_ord)):
        if b == _ord[i, 0]:
            ix2 = i
            break

    return ix1 < ix2


@jit(int64(int64[:, :], int64[:], int64, int64), nopython=True)
def intervening(_ord, _lengths, a, b):

    ix1 = None
    for i in xrange(len(_ord)):
        if a == _ord[i, 0]:
            ix1 = i
            break

    ix2 = None
    for i in xrange(len(_ord)):
        if b == _ord[i, 0]:
            ix2 = i
            break

    if ix1 == ix2:
        return 0

    if ix1 > ix2:
        ix1, ix2 = ix2, ix1

    return np.sum(_lengths[_ord[ix1 + 1:ix2, 0]])


@jit(float64(float64[:, :]))
def _calc_map_weight(raw_map):
    w = 0
    for i in raw_map:
        for j in raw_map[i]:
            w += np.sum(raw_map[i][j])
    map_weight = w

class ContactMap:

    def __init__(self, bam_file, enz_name, seq_file, bin_size, ins_mean, ins_sd, min_mapq, ref_min_len=0,
                 subsample=None, random_seed=None, strong=None, max_site_dist=None, spacing_factor=1.0,
                 linear=True, min_sites=1):

        self.strong = strong
        self.map_weight = None
        self.bin_size = bin_size
        self.ins_mean = ins_mean
        self.ins_sd = ins_sd
        self.min_mapq = min_mapq
        self.subsample = subsample
        self.random_state = np.random.RandomState(random_seed)
        self.strong = strong
        self.max_site_dist = max_site_dist
        self.spacing_factor = spacing_factor
        self.linear = linear
        self.min_sites = min_sites
        self.total_length = 0

        with pysam.AlignmentFile(bam_file, 'rb') as bam:

            self.total_seq = len(bam.references)

            # test that BAM file is the corret sort order
            if 'SO' not in bam.header['HD'] or bam.header['HD']['SO'] != 'queryname':
                raise IOError('BAM file must be sorted by read name')

            if enz_name:
                self.enzyme = get_enzyme_instance(enz_name)
                logger.debug('Cut-site spacing naive expectation: {0}'.format(4 ** self.enzyme.size))
            else:
                # non-RE based ligation-pairs
                self.enzyme = None
                logger.debug('Warning: no enzyme was specified, therefore ligation '
                                  'pairs are considered unconstrained in position.')
                raise RuntimeError('not implemented')

            self.unrestricted = False if enz_name else True

            # we'll pull sequences from a store as we go through the bam metadata

            # ':memory:'
            idxfile = '{}.db'.format(seq_file)
            if os.path.exists(idxfile):
                logging.warn('Found existing fasta index')
            seq_db = SeqIO.index_db(idxfile, seq_file, 'fasta')

            try:
                # determine the set of active sequences
                # where the first filtration step is by length
                self.seq_info = OrderedDict()
                offset = 0
                logger.info('Reading sequences...')
                for n, li in enumerate(bam.lengths):

                    # minimum length threshold
                    if li < ref_min_len:
                        continue

                    seq_id = bam.references[n]
                    seq = seq_db[seq_id].seq
                    if self.enzyme:

                        sites = np.array(self.enzyme.search(seq, linear=self.linear))

                        # must have at least one site
                        if len(sites) < min_sites:
                            continue

                        # don't apply spacing analysis to single site sequences
                        medspc = 0.0
                        if len(sites) > 1:
                            sites.sort()  # pedantic check for order
                            medspc = np.median(np.diff(sites))

                        self.seq_info[n] = {'sites': sites,
                                            'invtree': IntervalTree(Interval(si, si + 1) for si in sites),
                                            'max_dist': self.spacing_factor * medspc,
                                            'offset': offset,
                                            'name': seq_id,
                                            'length': li}

                        logger.debug('Found {0} cut-sites for {1} med_spc: {2:.1f} max_dist: {3:.1f}'.format(
                            len(sites), seq_id, medspc, self.spacing_factor * medspc))
                    else:
                        self.seq_info[n] = {'offset': self.total_len}

                    offset += li
            finally:
                seq_db.close()

            self.total_len = offset

            logger.info('Initially: {0} sequences and {1} bp'.format(
                len(self.seq_info), self.sum_active()))

            logger.info('Determining binning...')
            self.grouping = Grouping(self.seq_info, self.bin_size, self.min_sites)
            logger.info('After grouping: {0} sequences and {1} bp'.format(
                len(self.seq_info), self.sum_active()))

            self.raw_map = None

            logger.info('Counting reads in bam file...')
            self.total_reads = bam.count(until_eof=True)

            logger.info('BAM file contains: {0} references over {1} bp, {2} alignments'.format(
                self.total_seq, self.total_len, self.total_reads))

            # a simple associative map for binning on whole sequences
            #id_set = set(self.seq_info)
            #self.seq_map = OrderedDict((n, OrderedDict(zip(id_set, [0] * len(id_set)))) for n in id_set)

            # initialise the ordercm.order.order
            self.order = SeqOrder(self.seq_info)

            self._bin_map(bam)

    def sum_active(self):
        sum_len = 0
        for si, info in self.seq_info.iteritems():
            sum_len += info['length']
        return sum_len

    def num_active(self):
        return len(self.seq_info)

    def _init_map(self):
        n_bins = self.grouping.total_bins()

        logger.info('Initialising contact map of {0}x{0} fragment bins, '
                         'representing {1} bp over {2} sequences'.format(n_bins, self.sum_active(), self.num_active()))

        _m = {}
        for bi, seq_i in enumerate(self.grouping.map):
            _m[seq_i] = {}
            for bj, seq_j in enumerate(self.grouping.map):
                _m[seq_i][seq_j] = np.zeros((self.grouping.bins[bi], self.grouping.bins[bj]), dtype=np.int32)

        return _m

    def _calc_map_weight(self):
        w = 0
        for i in self.raw_map:
            for j in self.raw_map[i]:
                w += np.sum(self.raw_map[i][j])
        self.map_weight = w

    def _bin_map(self, bam):
        import tqdm

        def _simple_match(r):
            return not r.is_unmapped and r.mapq >= _mapq

        def _strong_match(r):
            return strong_match(r, self.strong, True, _mapq)

        # set-up match call
        _matcher = _strong_match if self.strong else _simple_match

        def _is_toofar_absolute(_x, _abs_max, _medspc):
            return _abs_max and _x > _abs_max

        def _is_toofar_medspc(_x, _abs_max, _medspc):
            return _x > _medspc

        @jit
        def _is_toofar_dummy(_x, _abs_max, _medspc):
            return False

        # set-up the test for site proximity
        if self.unrestricted:
            # all positions can participate, therefore restrictions are ignored.
            # TODO this would make more sense to be handled at arg parsing, since user should
            # be informed, rather tha silently ignoring.
            is_toofar = _is_toofar_dummy
        else:
            if self.max_site_dist:
                is_toofar = _is_toofar_absolute
            else:
                # default to spacing factor
                is_toofar = _is_toofar_medspc

        # initialise a map matrix for fine-binning and seq-binning
        self.raw_map = self._init_map()

        with tqdm.tqdm(total=self.total_reads) as pbar:

            # maximum separation being 3 std from mean
            _wgs_max = self.ins_mean + 2.0 * self.ins_sd

            _hic_max = self.max_site_dist

            # used when subsampling
            uniform = self.random_state.uniform
            sub_thres = self.subsample

            _mapq = self.min_mapq
            _seq_info = self.seq_info
            _active_ids = set(self.seq_info)

            counts = OrderedDict({
                'accepted': 0,
                'assumed_wgs': 0,
                'site_toofar': 0,
                'ref_excluded': 0,
                'skipped': 0,
                'poor_match': 0})

            bam.reset()
            bam_iter = bam.fetch(until_eof=True)
            while True:

                try:
                    r1 = bam_iter.next()
                    pbar.update()
                    while True:
                        # read records until we get a pair
                        r2 = bam_iter.next()
                        pbar.update()
                        if r1.query_name == r2.query_name:
                            break
                        r1 = r2
                except StopIteration:
                    break

                if r1.reference_id not in _active_ids or r2.reference_id not in _active_ids:
                    counts['ref_excluded'] += 1
                    continue

                if sub_thres and sub_thres < uniform():
                    counts['skipped'] += 1
                    continue

                if not _matcher(r1) or not _matcher(r2):
                    counts['poor_match'] += 1
                    continue

                if r1.is_read2:
                    r1, r2 = r2, r1

                assume_wgs = False
                if r1.is_proper_pair:

                    fwd, rev = (r2, r1) if r1.is_reverse else (r1, r2)
                    ins_len = rev.pos + rev.alen - fwd.pos

                    if fwd.pos <= rev.pos and ins_len < _wgs_max:
                        # assume this read-pair is a WGS read-pair, not HiC
                        counts['assumed_wgs'] += 1
                        continue

                if not assume_wgs:

                    if not self.unrestricted:

                        r1_dist = upstream_dist(r1, _seq_info[r1.reference_id]['sites'], r1.reference_length, self.linear)
                        if not r1_dist or is_toofar(r1_dist, _hic_max, _seq_info[r1.reference_id]['max_dist']):
                            counts['site_toofar'] += 1
                            continue

                        r2_dist = upstream_dist(r2, _seq_info[r2.reference_id]['sites'], r2.reference_length, self.linear)
                        if not r2_dist or is_toofar(r2_dist, _hic_max, _seq_info[r2.reference_id]['max_dist']):
                            counts['site_toofar'] += 1
                            continue

                    counts['accepted'] += 1
                    #self.seq_map[r1.reference_id][r2.reference_id] += 1

                r1pos = r1.pos if not r1.is_reverse else r1.pos + r1.alen
                r2pos = r2.pos if not r2.is_reverse else r2.pos + r2.alen

                # r1_bin = self.grouping.find_nearest(r1.reference_id, r1pos)
                # r2_bin = self.grouping.find_nearest(r2.reference_id, r2pos)
                r1_bin = find_nearest_jit(self.grouping.map[r1.reference_id], r1pos)
                r2_bin = find_nearest_jit(self.grouping.map[r2.reference_id], r2pos)

                s1, s2 = r1.reference_id, r2.reference_id
                bi, bj = r1_bin[1], r2_bin[1]
                if s1 == s2:
                    # same sequence, make sure we apply the same order for diagonal matrix
                    if bi > bj:
                        bi, bj = bj, bi
                elif s1 > s2:
                    # different sequences, always use upper triangle
                    s1, s2 = s2, s1
                    bi, bj = bj, bi

                self.raw_map[s1][s2][bi, bj] += 1

        self._calc_map_weight()

        logger.info('Pair accounting: {}'.format(counts))
        logger.info('Total raw map weight {0}'.format(self.map_weight))

    def to_dense(self, norm=False):
        """
        Create a dense matrix from the internal representation (dict of dict of submatrices)
        :param norm: normalise matrix by bin cardinality 
        :return: dense matrix, indexes of sequence boundaries
        """
        m = np.vstack(np.hstack(self.raw_map[si].values()) for si in self.raw_map)
        if norm:
            bc = np.hstack(np.bincount(bci[:, 1]) for bci in self.grouping.map.values()).astype(np.float)
            m = np.divide(m, bc)
        return m

    def get_ordered_bins(self):
        _order = self.order.order[:, 0]
        _bins = self.grouping.bins
        return _bins[_order]

    def _determine_block_shifts(self):
        """
        For the present ordering, calculate the block (whole-group) shifts necessary
        to permute the contact matrix to match the order.
        :return: list of tuples (start, stop, shift)
        """
        _order = self.order.order[:, 0]
        _bins = self.grouping.bins
        _shuf_bins = _bins[_order]

        shifts = []
        # iterate through current order, determine necessary block shifts
        curr_bin = 0
        for shuff_i, org_i in enumerate(_order):
            intervening_bins = _bins[:org_i]
            shft = -(curr_bin - np.sum(intervening_bins))
            shifts.append((curr_bin, curr_bin + _bins[org_i], shft))
            curr_bin += _shuf_bins[shuff_i]
        return shifts

    def _make_permutation_matrix(self):
        """
        Create the permutation matrix required to reorder the contact matrix to
        match the present ordering.
        :return: permutation matrix
        """
        # as a permutation matrix, the identity matrix causes no change
        perm_mat = np.identity(self.grouping.total_bins())
        block_shifts = self._determine_block_shifts()
        for si in block_shifts:
            if si[2] == 0:
                # ignore blocks which are zero shift.
                # as we started with identity, these are
                # already defined.
                continue
            # roll along columns, those rows matching each block with
            # a shift.
            pi = np.roll(perm_mat, si[2], 1)[si[0]:si[1], :]
            # put the result back into P, overwriting previous identity diagonal
            perm_mat[si[0]:si[1], :] = pi
        return perm_mat

    def get_ordered_map(self, norm=False):
        """
        Reorder the contact matrix to reflect the present order.
        :return: reordered contact matrix
        """
        # this may not be necessary, but we construct the full symmetrical map
        # before reordering, just in case something is reflected over the diagonal
        # and ends up copying empty space
        dm = self.to_dense(norm)
        full_map = np.tril(dm.transpose(), -1) + dm
        pm = self._make_permutation_matrix()
        # two applications of P is required to fully reorder the matrix
        # -- both on rows and columns
        # finally, convert the result back into a triangular matrix.
        return np.triu(np.dot(np.dot(pm, full_map), pm.T))

    def create_contig_graph(self, norm=True, scale=False, extern_ids=False):
        """
        Create a graph where contigs are nodes and edges linking
        nodes are weighted by the cumulative weight of contacts shared
        between them, normalized by the product of the number of fragments
        involved.
        
        :param norm: normalize weights by site count, Ni x Nj
        :param scale: scale weights (max_w = 1)
        :param extern_ids: use the original external sequence identifiers for node ids
        :return: graph of contigs
        """
        _order = self.order.order[:, 0]
        _id = self.order.names
        _len = self.order.lengths

        if extern_ids:
            _nn = lambda x: self.seq_info[_id[x]]['name']
        else:
            _nn = lambda x: x

        g = nx.Graph()
        for u in _order:
            # as networkx chokes serialising numpy types, explicitly type cast
            g.add_node(_nn(u), length=int(_len[u]))

        max_w = 0
        for i in xrange(len(_order)):
            ni = len(self.grouping.map[_id[i]])
            for j in xrange(i + 1, len(_order)):
                # as networkx chokes serialising numpy types, explicitly type cast
                obs = float(np.sum(self.raw_map[_id[i]][_id[j]]))
                if obs == 0:
                    continue

                if norm:
                    nsq = ni * len(self.grouping.map[_id[j]])
                    if obs != 0:
                        obs /= nsq

                g.add_edge(_nn(i), _nn(j), weight=obs)
                if obs > max_w:
                    max_w = obs

        if scale:
            for u, v in g.edges_iter():
                if g[u][v] > 0:
                    g[u][v]['weight'] /= max_w

        return g

    def get_hc_order(self):
        from scipy.cluster.hierarchy import complete
        from scipy.spatial.distance import squareform
        from scipy.cluster.hierarchy import dendrogram
        g = self.create_contig_graph()
        inverse_edge_weights(g)
        D = squareform(nx.adjacency_matrix(g).todense())
        Z = complete(D)
        return np.array(dendrogram(Z)['leaves'])

    def get_adhoc_order(self):
        """
        Attempt to determine an initial starting order of contigs based
        only upon the cross terms (linking contacts) between each using
        graphical techniques.
    
        Beginning with a graph of contigs, where edges are weighted by
        contact weight, it is decomposed using Louvain modularity. Taking
        inverse edge weights, the shortest path of the minimum spanning
        tree of each subgraph is used to define an order. The subgraph
        orderings are then concatenated together to define a full
        ordering of the sample.
    
        Those with no edges, are included by appear in an indeterminate
        order.
    
        :return: order of contigs
        """
        g = self.create_contig_graph(norm=True, scale=True)

        decomposed_subgraphs = decompose_graph(g)

        isolates = []
        new_order = []
        for gi in decomposed_subgraphs:
            if gi.order() > 1:
                inverse_edge_weights(gi)
                mst = nx.minimum_spanning_tree(gi)
                inverse_edge_weights(gi)
                new_order.extend(edgeiter_to_nodelist(dfs_weighted(mst)))
            else:
                isolates.extend(gi.nodes())

        return np.array(new_order + isolates)

    def plot(self, fname, norm=False, with_indexes=False):
        import matplotlib.pyplot as plt
        m = self.get_ordered_map(norm)
        m = m.astype(np.float)
        m += 0.01
        fig = plt.figure()
        img_h = 8
        fig.set_size_inches(img_h, img_h)
        ax = plt.Axes(fig, [0., 0., 1., 1.])
        if with_indexes:
            indexes = self.get_ordered_bins()
            ax.set_xticks(indexes.cumsum()-0.5)
            ax.set_yticks(indexes.cumsum()-0.5)
            ax.set_yticklabels([info['name'] for info in self.seq_info.values()])
            ax.set_xticklabels([info['name'] for info in self.seq_info.values()],
                               rotation=45, horizontalalignment='right')
            ax.grid(color='black', linestyle='-', linewidth=1.5)
        fig.add_axes(ax)
        ax.imshow(np.log(m), interpolation='none')
        plt.savefig(fname, dpi=180)

    def save(self, fname):
        import cPickle
        with open(fname, 'wb') as out_h:
            cPickle.dump(self, out_h)

    @staticmethod
    def load(fname):
        import cPickle
        with open(fname, 'rb') as in_h:
            return cPickle.load(in_h)


@jit(float64(int32[:, :], float64[:, :]), nopython=True)
def poisson_lpmf(ob, ex):
    s = 0.0
    for i in xrange(ob.shape[0]):
        for j in xrange(ob.shape[1]):
            aij = ob[i, j]
            bij = ex[i, j]
            if aij == 0:
                s += -bij
            else:
                s += aij * log(aij/bij) + bij - aij + 0.5 * log(2.0 * pi * aij)
    return -s

GEOM_SCALE = 3e-6
@vectorize([float64(float64)])
def piecewise_3c(s):
    pr = 1./3.e6/10.
    if s < 1000e3:
        # pr = 0.5 * (GEOM_SCALE * (1 - GEOM_SCALE)**s + 1/3e6)
        pr = GEOM_SCALE * (1 - GEOM_SCALE)**s
    return pr


def scoop_calc_likelihood(an_order):
    cm = shared.getConst('cm')
    return calc_likelihood(an_order, cm)


def calc_likelihood(an_order, cm):
    """
    Calculate the logLikelihood of a given sequence configuration. The model is adapted from
    GRAAL. Counts are Poisson, with lambda parameter dependent on expected observed contact
    rate as a function of inter-fragment separation. This has been shown experimentally to be
    modelled effectively by a power-law (used here).

    :param an_order: an order with which to calculate the likelihood
    :return:
    """

    Nd = cm.map_weight

    sumL = 0.0

    ids = cm.order.names
    centers = cm.grouping.centers
    lengths = cm.order.lengths
    raw_map = cm.raw_map
    indexes = cm.grouping.index

    for i, j in itertools.combinations(indexes.values(), 2):

        # inter-contig separation defined by cumulative
        # intervening contig length.
        # L = cm.order.intervening(i, j)
        L = intervening(an_order, lengths, i, j)

        # bin centers
        centers_i = centers[ids[i]]
        centers_j = centers[ids[j]]

        # determine relative origin for measuring separation
        # between sequences. If i comes before j, then distances
        # to j will be measured from the end of i -- and visa versa
        if before(an_order, i, j):
            s_i = lengths[i] - centers_i[an_order[i, 1]]
            s_j = centers_j[an_order[j, 1]]
        else:
            s_i = centers_i[an_order[i, 1]]
            s_j = lengths[j] - centers_j[an_order[j, 1]]

        # separations between contigs in this ordering
        d_ij = np.abs(L + s_i[:, np.newaxis] - s_j)

        q_ij = piecewise_3c(d_ij)

        n_ij = raw_map[ids[i]][ids[j]]
        sumL += poisson_lpmf(n_ij, Nd * q_ij)

    return float(sumL),


def mutate(individual, psnv, pflip, plarge, inv_size, inv_pb=0.5):

    for _p in [psnv, pflip, plarge]:
        assert _p <= 1.0, 'a mutation probability is greater than 1'

    n = len(individual)
    x = individual

    rnd = random.random
    sample = random.sample

    # inversions and transposition

    if n > 2 * inv_size and rnd() < plarge:
        # invert a range.
        if rnd() < 0.5:
            d = max(1, np.random.binomial(inv_size, inv_pb))
            i1 = np.random.randint(0, n-d)
            x[i1:i1+d, :] = x[i1:i1+d, :][::-1]

        # transpose
        else:
            # segment size
            d = max(1, np.random.binomial(inv_size, inv_pb))

            # positions
            i1 = np.random.randint(0, n-d+1)
            while True:
                i2 = np.random.randint(0, n-d+1)
                if i1 != i2:
                    break
            if i2 < i1:
                i2, i1 = i1, i2

            assert i2 + d <= len(x), 'shifted segment will not fit in array bounds'
            x[i1:i2+d, :] = np.roll(x[i1:i2+d, :], -d, axis=0)
    else:
        # point mutations instead
        xset = set(range(n))

        if n > 2:
            for i1 in xrange(n):
                if rnd() < psnv:
                    i2 = sample(xset - {i1}, 1)[0]
                    if rnd() < 0.5:
                        # swap two elements
                        # seems we need to use a temp for this array structure
                        t0, t1 = x[i1, :]
                        x[i1, :] = x[i2, :]
                        x[i2, :] = t0, t1
                    else:
                        # move an element
                        if i1 > i2:
                            i1, i2 = i2, i1
                        x[i1:i2+1, :] = np.roll(x[i1:i2+1, :], -1, axis=0)

        # flip orientations
        for i1 in xrange(n):
            if rnd() < pflip:
                # flip orientation
                x[i1, 1] = not x[i1, 1]

    return x,


def crossover(ind1, ind2):

    # a random segment
    size = len(ind1)
    a, b = random.sample(range(size), 2)
    if a > b:
        a, b = b, a

    # receiving arrays
    mut1 = np.empty_like(ind1)
    mut2 = np.empty_like(ind2)
    mut1.fill(-1)
    mut2.fill(-1)

    # place swapped regions
    mut1[a:b+1, :] = ind2[a:b+1, :]
    mut2[a:b+1, :] = ind1[a:b+1, :]

    # what was not part of the region in each, order retained
    extra1 = ind1[np.invert(np.isin(ind1[:, 0], ind2[a:b+1, 0])), :]
    extra2 = ind2[np.invert(np.isin(ind2[:, 0], ind1[a:b+1, 0])), :]

    # fill in these extras, where placed segment does not occupy
    n1, n2 = 0, 0
    for i in xrange(size):
        if mut1[i, 0] < 0:
            mut1[i, :] = extra1[n1, :]
            n1 += 1
        if mut2[i, 0] < 0:
            mut2[i, :] = extra2[n2, :]
            n2 += 1

    ind1[:, :] = mut1[:, :]
    ind2[:, :] = mut2[:, :]

    return ind1, ind2


def ea_mu_plus_lambda(population, toolbox, genlog, mu, lambda_, cxpb, mutpb, ngen,
                      stats=None, halloffame=None):

    logbook = tools.Logbook()
    logbook.header = ['gen', 'nevals'] + (stats.fields if stats else [])

    # Evaluate the individuals with an invalid fitness
    invalid_ind = [ind for ind in population if not ind.fitness.valid]
    fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
    for ind, fit in zip(invalid_ind, fitnesses):
        ind.fitness.values = fit

    if halloffame is not None:
        halloffame.update(population)

    genlog.append(halloffame[0])

    record = stats.compile(population) if stats is not None else {}
    logbook.record(gen=0, nevals=len(invalid_ind), **record)

    print logbook.stream

    # Begin the generational process
    for gen in range(1, ngen + 1):

        # Vary the population
        offspring = varOr(population, toolbox, lambda_, cxpb, mutpb)

        # Evaluate the individuals with an invalid fitness
        invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
        for ind, fit in zip(invalid_ind, fitnesses):
            ind.fitness.values = fit

        # Update the hall of fame with the generated individuals
        if halloffame is not None:
            halloffame.update(offspring)

        # Select the next generation population
        population[:] = toolbox.select(population + offspring, mu)

        # Update the statistics with the new population
        record = stats.compile(population) if stats is not None else {}
        logbook.record(gen=gen, nevals=len(invalid_ind), **record)
        print logbook.stream

        genlog.append(tools.selBest(population, 1)[0])

    return population, logbook


def numpy_similar(a, b):
    return np.all(a == b)

creator.create('FitnessML', base.Fitness, weights=(1.0,))
creator.create('Individual', np.ndarray, fitness=creator.FitnessML)

if __name__ == '__main__':
    import traceback, sys, pdb
    import argparse
    import pickle
    import mapio

    from datetime import timedelta
    from monotonic import monotonic

    class Timer:
        def __init__(self):
            self.start_time = monotonic()

        def start(self):
            self.start_time = monotonic()

        def elapsed(self):
            return timedelta(seconds=monotonic() - self.start_time)

    parser = argparse.ArgumentParser(description='Create a 3C fragment map from a BAM file')
    parser.add_argument('-v', '--verbose', default=False, action='store_true', help='Verbose output')
    parser.add_argument('-f', '--format', choices=['csv', 'h5'], default='csv',
                        help='Input contact map format')
    parser.add_argument('--circular', default=False, action='store_true', help='Treat all references as circular')
    parser.add_argument('--strong', type=int, default=None,
                        help='Using strong matching constraint (minimum matches in alignments).')
    parser.add_argument('--bin-size', type=int, required=True, help='Size of bins in numbers of enzymatic sites')
    parser.add_argument('--enzyme', required=True, help='Enzyme used (case-sensitive)')
    parser.add_argument('--insert-mean', type=int, required=True, help='Expected mean of sequencing insert')
    parser.add_argument('--insert-sd', type=int, required=True, help='Expected standard deviation of insert')
    parser.add_argument('--min-sites', type=int, required=True, help='Minimum acceptable number of sites in a bin')
    parser.add_argument('--min-mapq', type=int, default=0, help='Minimum acceptable mapping quality [0]')
    parser.add_argument('--min-reflen', type=int, default=0, help='Minimum acceptable reference length [0]')
    parser.add_argument('--spc-factor', type=float, default=3.0,
                        help='Maximum sigma factor separation from read to nearest site [3]')
    parser.add_argument('--pickle', help='Picked contact map')
    parser.add_argument('--ngen', default=0, type=int, help='Number of ES generations')
    parser.add_argument('--lambda', dest='lamb', default=50, type=int, help='Number of offspring')
    parser.add_argument('--mu', type=int, default=50, help='Number of parents')
    parser.add_argument('fasta', help='Reference fasta sequence')
    parser.add_argument('bam', help='Input bam file')

    args = parser.parse_args()

    try:
        t = Timer()

        if args.pickle:
            import pickle
            with open(args.pickle, 'rb') as input_h:
                cm = pickle.load(input_h)
        else:
            t.start()
            cm = ContactMap(args.bam,
                            args.enzyme,
                            args.fasta,
                            args.bin_size,
                            args.insert_mean, args.insert_sd,
                            args.min_mapq,
                            spacing_factor=args.spc_factor,
                            min_sites=args.min_sites,
                            ref_min_len=args.min_reflen,
                            strong=args.strong,
                            linear=not args.circular)

            logger.info('Saving contact map instance...')
            cm.save('cm.p')
            logger.info('Contact map took: {}'.format(t.elapsed()))

        # logger.info('Saving graph...')
        # nx.write_graphml(cm.create_contig_graph(norm=True, scale=True, extern_ids=True), 'cm.graphml')
        logger.info('Plotting starting image...')
        cm.plot('start.png', norm=True)

        logger.info('Saving raw contact map...')
        mapio.write_map(cm.to_dense(), 'raw', args.format, np.int)

        # logL = calc_likelihood(cm.order.order, cm)
        # logger.info('Initial logL {}'.format(logL[0]))

        # print 'Ordering by HC...'
        # # hc order
        # t.reset()
        # o = cm.get_hc_order()
        # cm.order.set_only_order(o)
        # print 'HC logL {}'.format(calc_likelihood(cm.order.order)[0])
        # m = cm.get_ordered_map()
        # print t.elapsed()
        # print 'Saving hc contact maps as csv...'
        # t.reset()
        # mapio.write_map(m, 'hc', args.format, np.int)
        # print t.elapsed()

        # adhoc order
        # logger.info('Beginning adhoc ordering...')
        # t.start()
        # o = cm.get_adhoc_order()
        # cm.order.set_only_order(o)
        # logL = calc_likelihood(cm.order.order, cm)
        # logger.info('Adhoc logL {}'.format(logL[0]))
        # m = cm.get_ordered_map()
        # logger.debug(t.elapsed())
        # logger.info('Saving adhoc contact maps as csv...')
        # np.savetxt('adhoc.csv', m, fmt='%d', delimiter=',')
        # mapio.write_map(m, 'adhoc', args.format, np.int)
        # logger.info('Plotting adhoc image...')
        # cm.plot('adhoc.png', norm=True)
        # logger.info('Adhoc took: {}'.format(t.elapsed()))
        #
        # with open('adhoc-order.csv', 'w') as out_h:
        #     ord_str = str([(-1)**d * o for o, d in cm.order.order])
        #     out_h.write('{}\n'.format(ord_str))

    except:
        type, value, tb = sys.exc_info()
        traceback.print_exc()
        pdb.post_mortem(tb)

    if args.ngen > 0:

        logger.info('Beginning EA ordering...')

        shared.setConst(cm=cm)

        history = tools.History()
        toolbox = base.Toolbox()
        active = list(cm.order.order[:, 0])

        def create_ordering(names):
            ord = random.sample(names, len(names))
            ori = np.zeros(len(ord), dtype=np.int)
            return np.vstack((ord, ori)).T

        toolbox.register('indices', create_ordering, active)
        toolbox.register('individual', tools.initIterate, creator.Individual, toolbox.indices)
        toolbox.register('population', tools.initRepeat, list, toolbox.individual)

        toolbox.register('evaluate', scoop_calc_likelihood)
        toolbox.register("mate", crossover)

        toolbox.register("mutate", mutate, psnv=0.05, pflip=0.05, plarge=0.2, inv_size=10)
        toolbox.register("select", tools.selNSGA2)

        toolbox.decorate("mate", history.decorator)
        toolbox.decorate("mutate", history.decorator)

        toolbox.register('map', futures.map)

        try:
            MU, LAMBDA, NGEN = args.mu, args.lamb, args.ngen

            population = toolbox.population(n=MU)
            hof = tools.ParetoFront(similar=numpy_similar)
            stats = tools.Statistics(lambda ind: ind.fitness.values)
            stats.register("avg", np.mean, axis=0)
            stats.register("std", np.std, axis=0)
            stats.register("min", np.min, axis=0)
            stats.register("max", np.max, axis=0)

            genlog = []

            t.start()
            pop, logbook = ea_mu_plus_lambda(population, toolbox, genlog, mu=MU, lambda_=LAMBDA,
                                             cxpb=0.5, mutpb=0.3, ngen=NGEN,
                                             stats=stats, halloffame=hof)
            logger.info('EA took: {}'.format(t.elapsed()))

            with open('gen_best.csv', 'w') as out_h:
                for n, order_i in enumerate(genlog):
                    ord_str = str([(-1)**d * o for o, d in order_i])
                    out_h.write('{0}: {1}\n'.format(n, ord_str))

            cm.order.set_ord_and_ori(np.array(hof[0]))
            m = cm.get_ordered_map()

            logger.info('Saving es contact maps as csv...')
            mapio.write_map(m, 'es', args.format, np.int)
            logger.info('Plotting final EA image...')
            cm.plot('es.png', norm=True)

        except:

            type, value, tb = sys.exc_info()
            traceback.print_exc()
            pdb.post_mortem(tb)
#!/usr/bin/env python3
"""
Read a VCF and a BAM file and phase the variants. The phased VCF is written to
standard output.
"""
"""
 0: ref allele
 1: alt allele
 -: unphasable: no coverage of read that covers at least 2 SNPs
 X: unphasable: there is coverage, but still not phasable (tie)

TODO
* Perhaps simplify slice_reads() such that it only creates and returns one slice
* it would be cleaner to not open the input VCF twice
* convert parse_vcf to a class so that we can access VCF header info before
  starting to iterate (sample names)
"""
import os
import logging
import sys
import random
import gzip
import time
import itertools
from collections import defaultdict
from contextlib import ExitStack, closing
import pysam

from ..vcf import parse_vcf, PhasedVcfWriter
from .. import __version__
from ..args import HelpfulArgumentParser as ArgumentParser
from ..phase import ReadVariantList, ReadVariant, read_to_coreread
from ..core import Read, ReadSet, DPTable

__author__ = "Murray Patterson, Alexander Schönhuth, Tobias Marschall, Marcel Martin"

logger = logging.getLogger(__name__)


def find_alleles(variants, start, read):
	"""

	"""
	pos = read.pos
	cigar = read.cigar

	c = 0  # hit count
	j = start  # index into variants list
	p = pos
	s = 0  # absolute index into the read string [0..len(read)]
	read_variants = []
	for cigar_op, length in cigar:
		# The mapping of CIGAR operators to numbers is:
		# MIDNSHPX= => 012345678
		if cigar_op in (0, 7, 8):  # we're in a matching subregion
			s_next = s + length
			p_next = p + length
			r = p + length  # size of this subregion
			# skip over all SNPs that come before this region
			while j < len(variants) and variants[j].position < p:
				j += 1
			# iterate over all positions in this subregion and
			# check whether any of them coincide with one of the SNPs ('hit')
			while j < len(variants) and p < r:
				if variants[j].position == p:  # we have a hit
					base = read.seq[s:s+1]
					if base == variants[j].reference_allele:
						al = '0'  # REF allele
					elif base == variants[j].alternative_allele:
						al = '1'  # ALT allele
					else:
						al = 'E' # for "error" (keep for stats purposes)
					rv = ReadVariant(position=p, base=base, allele=al, quality=ord(read.qual[s:s+1])-33)
					read_variants.append(rv)
					c += 1
					j += 1
				s += 1 # advance both read and reference
				p += 1
			s = s_next
			p = p_next
		elif cigar_op == 1:  # an insertion
			s += length
		elif cigar_op == 2 or cigar_op == 3:  # a deletion or a reference skip
			p += length
		elif cigar_op == 4:  # soft clipping
			s += length
		elif cigar_op == 5 or cigar_op == 6:  # hard clipping or padding
			pass
		else:
			logger.error("Unsupported CIGAR operation: %d", cigar_op)
			sys.exit(1)
	return read_variants


class BamReader:
	"""
	Associate variants with reads.
	"""
	def __init__(self, path, mapq_threshold=20):
		"""
		path -- path to BAM file
		"""
		bai1 = path + '.bai'
		bai2 = os.path.splitext(path)[0] + '.bai'
		if not os.path.exists(bai1) and not os.path.exists(bai2):
			logger.info('BAM index not found, creating it now.')
			pysam.index(path)
		self._samfile = pysam.Samfile(path)
		self._mapq_threshold = mapq_threshold
		self._initialize_sample_to_group_ids()

	def _initialize_sample_to_group_ids(self):
		"""
		Return a dictionary that maps a sample name to a set of read group ids.
		"""
		read_groups = self._samfile.header['RG']  # a list of dicts
		logger.debug('Read groups in SAM header: %s', read_groups)
		samples = defaultdict(list)
		for read_group in read_groups:
			samples[read_group['SM']].append(read_group['ID'])
		self._sample_to_group_ids = {
			id: frozenset(values) for id, values in samples.items() }

	def read(self, chromosome, variants, sample):
		"""
		chromosome -- name of chromosome to work on
		variants -- list of Variant objects (obtained from VCF with parse_vcf)
		sample -- name of sample to work on. If None, read group information is
			ignored and all reads in the file are used.

		Return a list of ReadVariantList objects.
		"""
		if sample is not None:
			read_groups = self._sample_to_group_ids[sample]

		# resulting list of ReadVariantList objects
		result = []

		i = 0  # keep track of position in variants array (which is in order)
		for read in self._samfile.fetch(chromosome):
			if sample is not None and not read.opt('RG') in read_groups:
				continue
			# TODO: handle additional alignments correctly! find out why they are sometimes overlapping/redundant
			if read.flag & 2048 != 0:
				# print('Skipping additional alignment for read ', read.qname)
				continue
			if read.is_secondary:
				continue
			if read.is_unmapped:
				continue
			if read.mapq < self._mapq_threshold:
				continue
			if not read.cigar:
				continue

			# since reads are ordered by position, we need not consider
			# positions that are too small
			while i < len(variants) and variants[i].position < read.pos:
				i += 1
			read_variants = find_alleles(variants, i, read)
			if read_variants:
				rvl = ReadVariantList(name=read.qname, mapq=read.mapq, variants=read_variants)
				result.append(rvl)
		return result

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def close(self):
		self._samfile.close()


def grouped_by_name(reads_with_variants):
	"""
	Group an input list of reads into a list of lists where each
	sublist is a slice of the input list that contains reads with the same name.

	Example (only read names are shown here):
	['A', 'A', 'B', 'C', 'C']
	->
	[['A', 'A'], ['B'], ['C', 'C']]
	"""
	result = []
	prev = None
	current = []
	for read in reads_with_variants:
		if prev and read.name != prev.name:
			result.append(current)
			current = []
		current.append(read)
		prev = read
	result.append(current)
	return result


def merge_paired_reads(reads):
	"""
	Merge reads that occur twice (according to their name) into a single read.
	This is relevant for paired-end or mate pair reads.

	The ``variants`` attribute of a merged read contains the variants lists of
	both reads, separated by "None". For a merged read, the ``mapq`` attribute
	is a tuple consisting of the two original mapping qualities.

	Reads that occur only once are returned unchanged.
	"""
	result = []
	for group in grouped_by_name(reads):
		if len(group) == 1:
			result.append(group[0])
		elif len(group) == 2:
			merged_variants = group[0].variants + [None] + group[1].variants
			merged_read = ReadVariantList(name=group[0].name, mapq=(group[0].mapq, group[1].mapq), variants=merged_variants)
			result.append(merged_read)
		else:
			assert len(group) <= 2, "More than two reads with the same name found"
	return result


def filter_reads(reads, mincount=2):
	"""
	Return a new list in which reads are omitted that fulfill at least one of
	these conditions:

	- one of the read's variants' alleles is 'E'

	- variant positions are not strictly monotonically increasing.

	mincount -- If the number of variants on a read occurring once or the
		total number of variants on a paired-end read is lower than this
		value, the read (or read pair) is discarded.
	"""
	result = []
	for read in reads:
		prev_pos = -1
		for variant in read.variants:
			if variant is None:
				continue
			if not prev_pos < variant.position:
				break
			if variant.allele == 'E':
				break
			assert variant.base in 'ACGT01-X', 'variant.base={!r}'.format(variant.base)
			prev_pos = variant.position
		else:
			# executed when no break occurred above
			if len(read.variants) >= mincount:
				result.append(read)
	return result


class CoverageMonitor:
	'''TODO: This is a most simple, naive implementation. Could do this smarter.'''
	def __init__(self, length):
		self.coverage = [0] * length

	def max_coverage_in_range(self, begin, end):
		return max(self.coverage[begin:end])

	def add_read(self, begin, end):
		for i in range(begin, end):
			self.coverage[i] += 1


def position_set(reads):
	"""
	Return a set of all variant positions that occur within a list of reads.

	reads -- a list of ReadVariantList objects
	"""
	positions = set()
	for read in reads:
		positions.update(variant.position for variant in read.variants if variant is not None)
	return positions


def slice_reads(reads, max_coverage):
	"""
	TODO document this
	TODO document that fragment (not read) coverage is used

	max_coverage --
	reads -- a list of ReadVariantList objects
	"""
	position_list = sorted(position_set(reads))
	logger.info('Found %d SNP positions', len(position_list))

	# dictionary to map SNP position to its index
	position_to_index = { position: index for index, position in enumerate(position_list) }

	# List of slices, start with one empty slice ...
	slices = [[]]
	# ... and the corresponding coverages along each slice
	slice_coverages = [CoverageMonitor(len(position_list))]
	skipped_reads = 0
	accessible_positions = set()
	for read in reads:
		# Skip reads that cover only one SNP
		if len(read.variants) < 2:
			skipped_reads += 1
			continue
		for variant in read.variants:
			if variant is None:
				continue
			accessible_positions.add(variant.position)
		begin = position_to_index[read.variants[0].position]
		end = position_to_index[read.variants[-1].position] + 1
		slice_id = 0
		while True:
			# Does current read fit into this slice?
			if slice_coverages[slice_id].max_coverage_in_range(begin, end) < max_coverage:
				slice_coverages[slice_id].add_read(begin, end)
				slices[slice_id].append(read)
				break
			else:
				slice_id += 1
				# do we have to create a new slice?
				if slice_id == len(slices):
					slices.append([])
					slice_coverages.append(CoverageMonitor(len(position_list)))
	logger.info('Skipped %d reads that only cover one SNP', skipped_reads)

	unphasable_snps = len(position_list) - len(accessible_positions)
	if position_list:
		logger.info('%d out of %d variant positions (%.1d%%) do not have a read '
			'connecting them to another variant and are thus unphasable',
			unphasable_snps, len(position_list),
			100. * unphasable_snps / len(position_list))

	# Sort each slice
	for read_list in slices:
		read_list.sort(key=lambda r: r.variants[0].position)
	# Print stats
	for slice_id, read_list in enumerate(slices):
		positions_covered = len(position_set(read_list))
		if position_list:
			logger.info('Slice %d contains %d reads and covers %d of %d SNP positions (%.1f%%)',
				slice_id, len(read_list), positions_covered, len(position_list),
				positions_covered * 100.0 / len(position_list))

	return slices


class Node:
	def __init__(self, value, parent):
		self.value = value
		self.parent = parent

	def __repr__(self):
		return "Node(value={}, parent={})".format(self.value, self.parent)


class ComponentFinder:
	"""
	This implements a variant of the Union-Find algorithm, but without the
	"union by rank" strategy since we want the smallest node to be the
	representative.

	TODO
	It's probably possible to use union by rank and still have the
	representative be the smallest node (possibly need to keep track of the
	minimum somewhere).

	We probably should not worry too much since there is already logarithmic
	overhead due to dictionary lookups.
	"""
	def __init__(self, values):
		self.nodes = { x: Node(x, None) for x in values }

	def merge(self, x, y):
		assert x != y
		x_root = self._find_node(x)
		y_root = self._find_node(y)

		if x_root is y_root:
			return

		# Merge, making sure that the node with the smaller value is the
		# new parent.
		if x_root.value < y_root.value:
			y_root.parent = x_root
		else:
			x_root.parent = y_root

	def _find_node(self, x):
		node = root = self.nodes[x]
		while root.parent is not None:
			root = root.parent

		# compression path
		while node.parent is not None:
			node.parent, node = root, node.parent
		return root

	def find(self, x):
		"""
		Return which component x belongs to, identified by the smallest value.
		"""
		return self._find_node(x).value

	def print(self):
		for x in sorted(self.nodes):
			print(x, ':', self.nodes[x], 'is represented by', self._find_node(x))


def find_components(superreads, reads):
	"""
	"""
	logger.info('Finding components ...')
	assert len(superreads) == 2
	assert len(superreads[0]) == len(superreads[1])

	phased_positions = [ position for position, base, allele, quality in superreads[0] if allele in [0,1] ]  # TODO set()
	assert phased_positions == sorted(phased_positions)

	# Find connected components.
	# A component is identified by the position of its leftmost variant.
	component_finder = ComponentFinder(phased_positions)
	phased_positions = set(phased_positions)
	for read in reads:
		positions = [ position for position, base, allele, quality in read if position in phased_positions ]
		for position in positions[1:]:
			component_finder.merge(positions[0], position)
	components = { position : component_finder.find(position) for position in phased_positions }
	logger.info('No. of variants considered for phasing: %d', len(superreads[0]))
	logger.info('No. of variants that were phased: %d', len(phased_positions))
	logger.info('No. of components: %d', len(set(components.values())))
	return components


def ensure_pysam_version():
	from pysam import __version__ as pysam_version
	from distutils.version import LooseVersion
	if LooseVersion(pysam_version) < LooseVersion("0.8.1"):
		sys.exit("You need to use pysam >= 0.8.1 with WhatsHap")


def main():
	ensure_pysam_version()
	logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
	parser = ArgumentParser(prog='whatshap', description=__doc__)
	parser.add_argument('--version', action='version', version=__version__)
	parser.add_argument('-o', '--output', default=None,
		help='Output VCF file. If omitted, use standard output.')
	parser.add_argument('--max-coverage', '-H', metavar='MAXCOV', default=15, type=int,
		help='Reduce coverage to at most MAXCOV (default: %(default)s).')
	parser.add_argument('--mapping-quality', '--mapq', metavar='QUAL',
		default=20, type=int, help='Minimum mapping quality')
	parser.add_argument('--seed', default=123, type=int, help='Random seed (default: %(default)s)')
	parser.add_argument('--all-het', action='store_true', default=False,
		help='Assume all positions to be heterozygous (that is, fully trust SNP calls).')
	parser.add_argument('--ignore-read-groups', default=False, action='store_true',
		help='Ignore read groups in BAM header and assume all reads come '
		'from the same sample.')
	parser.add_argument('--sample', metavar='SAMPLE', default=None,
		help='Name of a sample to phase. If not given, only the first sample '
			'in the input VCF is phased.')
	parser.add_argument('vcf', metavar='VCF', help='VCF file')
	parser.add_argument('bam', metavar='BAM', help='BAM file')
	args = parser.parse_args()
	random.seed(args.seed)

	start_time = time.time()
	with ExitStack() as stack:
		try:
			bam_reader = stack.enter_context(closing(BamReader(args.bam, mapq_threshold=args.mapping_quality)))
		except OSError as e:
			logging.error(e)
			sys.exit(1)
		if args.output is not None:
			out_file = stack.enter_context(open(args.output, 'w'))
		else:
			out_file = sys.stdout
		command_line = ' '.join(sys.argv[1:])
		vcf_writer = PhasedVcfWriter(command_line=command_line, in_path=args.vcf, out_file=out_file)
		for sample, chromosome, variants in parse_vcf(args.vcf, args.sample):
			logger.info('Read %d variants on chromosome %s', len(variants), chromosome)
			if args.ignore_read_groups:
				sample = None
			logger.info('Reading the BAM file ...')
			reads_with_variants = bam_reader.read(chromosome, variants, sample)
			reads_with_variants.sort(key=lambda read: read.name)
			reads = merge_paired_reads(reads_with_variants)

			# sort by position of first variant
			#reads.sort(key=lambda read: read.variants[0].position)

			random.shuffle(reads)
			unfiltered_length = len(reads)
			reads = filter_reads(reads)
			logger.info('Filtered reads: %d', unfiltered_length - len(reads))
			reads = slice_reads(reads, args.max_coverage)[0]
			logger.info('Phasing the variants ...')
			
			# Transform reads into core reads
			core_reads = ReadSet()
			for read in reads:
				core_reads.add(read_to_coreread(read))
			# Finalizing a read set will sort reads, variants within reads and assign unique read IDs.
			core_reads.finalize()
			# Run the core algorithm: construct DP table ...
			dp_table = DPTable(core_reads, args.all_het)
			# ... and do the backtrace to get the solution
			superreads = dp_table.getSuperReads()

			components = find_components(superreads, core_reads)
			logger.info('Writing chromosome %s ...', chromosome)
			vcf_writer.write(chromosome, sample, superreads, components)

	logger.info('Elapsed time: %.1fs', time.time() - start_time)
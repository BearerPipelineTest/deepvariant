# Copyright 2020 Google LLC.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""Tests for deepvariant.make_examples."""
# assertLen isn't part of unittest externally, so disable warnings that we are
# using assertEqual(len(...), ...) instead of assertLen(..., ...).
# pylint: disable=g-generic-assert

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import sys
if 'google' in sys.modules and 'google.protobuf' not in sys.modules:
  del sys.modules['google']


import copy
import errno
import platform
import sys


from absl import flags
from absl import logging
from absl.testing import absltest
from absl.testing import flagsaver
from absl.testing import parameterized
import mock
import six

from deeptrio import make_examples
from deeptrio import testdata
from deepvariant import make_examples_core
from deepvariant import make_examples_utils
from deepvariant import tf_utils
from deepvariant.labeler import variant_labeler
from deepvariant.protos import deepvariant_pb2
from third_party.nucleus.io import fasta
from third_party.nucleus.io import tfrecord
from third_party.nucleus.io import vcf
from third_party.nucleus.protos import reads_pb2
from third_party.nucleus.protos import reference_pb2
from third_party.nucleus.protos import variants_pb2
from third_party.nucleus.testing import test_utils
from third_party.nucleus.util import ranges
from third_party.nucleus.util import variant_utils
from third_party.nucleus.util import variantcall_utils
from third_party.nucleus.util import vcf_constants

FLAGS = flags.FLAGS

# Dictionary mapping keys to decoders for decode_example function.
_EXAMPLE_DECODERS = {
    'locus': tf_utils.example_locus,
    'alt_allele_indices/encoded': tf_utils.example_alt_alleles_indices,
    'image/encoded': tf_utils.example_encoded_image,
    'variant/encoded': tf_utils.example_variant,
    'variant_type': tf_utils.example_variant_type,
    'label': tf_utils.example_label,
    'image/format': tf_utils.example_image_format,
    'image/shape': tf_utils.example_image_shape,
    'sequencing_type': tf_utils.example_sequencing_type,
}


def decode_example(example):
  """Decodes a tf.Example from DeepVariant into a dict of Pythonic structures.

  Args:
    example: tf.Example proto. The example to make into a dictionary.

  Returns:
    A python dictionary with key/value pairs for each of the fields of example,
    with each value decoded as needed into Python structures like protos, list,
    etc.

  Raises:
    KeyError: If example contains a feature without a known decoder.
  """
  as_dict = {}
  for key in example.features.feature:
    if key not in _EXAMPLE_DECODERS:
      raise KeyError('Unexpected example key', key)
    as_dict[key] = _EXAMPLE_DECODERS[key](example)
  return as_dict


def setUpModule():
  testdata.init()


def _make_contigs(specs):
  """Makes ContigInfo protos from specs.

  Args:
    specs: A list of 2- or 3-tuples. All tuples should be of the same length. If
      2-element, these should be the name and length in basepairs of each
      contig, and their pos_in_fasta will be set to their index in the list. If
      the 3-element, the tuple should contain name, length, and pos_in_fasta.

  Returns:
    A list of ContigInfo protos, one for each spec in specs.
  """
  if specs and len(specs[0]) == 3:
    return [
        reference_pb2.ContigInfo(name=name, n_bases=length, pos_in_fasta=i)
        for name, length, i in specs
    ]
  else:
    return [
        reference_pb2.ContigInfo(name=name, n_bases=length, pos_in_fasta=i)
        for i, (name, length) in enumerate(specs)
    ]


def _from_literals_list(literals, contig_map=None):
  """Makes a list of Range objects from literals."""
  return ranges.parse_literals(literals, contig_map)


def _from_literals(literals, contig_map=None):
  """Makes a RangeSet of intervals from literals."""
  return ranges.RangeSet.from_regions(literals, contig_map)


def _sharded(basename, num_shards=None):
  if num_shards:
    return basename + '@' + str(num_shards)
  else:
    return basename


class MakeExamplesEnd2EndTest(parameterized.TestCase):

  # Golden sets are created with learning/genomics/internal/create_golden.sh
  @parameterized.parameters(
      # All tests are run with fast_pass_aligner enabled. There are no
      # golden sets version for ssw realigner.
      dict(mode='calling', num_shards=0),
      dict(mode='calling', num_shards=3),
      dict(
          mode='training', num_shards=0, labeler_algorithm='haplotype_labeler'),
      dict(
          mode='training', num_shards=3, labeler_algorithm='haplotype_labeler'),
      dict(
          mode='training', num_shards=0,
          labeler_algorithm='positional_labeler'),
      dict(
          mode='training', num_shards=3,
          labeler_algorithm='positional_labeler'),
  )
  @flagsaver.flagsaver
  def test_make_examples_end2end(self,
                                 mode,
                                 num_shards,
                                 labeler_algorithm=None,
                                 use_fast_pass_aligner=True):
    self.assertIn(mode, {'calling', 'training'})
    region = ranges.parse_literal('20:10,000,000-10,010,000')
    FLAGS.write_run_info = True
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.candidates = test_utils.test_tmpfile(
        _sharded('vsc.tfrecord', num_shards))
    FLAGS.examples = test_utils.test_tmpfile(
        _sharded('examples.tfrecord', num_shards))
    child_examples = test_utils.test_tmpfile(
        _sharded('examples_child.tfrecord', num_shards))
    FLAGS.regions = [ranges.to_literal(region)]
    FLAGS.partition_size = 1000
    FLAGS.mode = mode
    FLAGS.gvcf_gq_binsize = 5
    FLAGS.use_fast_pass_aligner = use_fast_pass_aligner
    if labeler_algorithm is not None:
      FLAGS.labeler_algorithm = labeler_algorithm

    if mode == 'calling':
      FLAGS.gvcf = test_utils.test_tmpfile(
          _sharded('gvcf.tfrecord', num_shards))
      child_gvcf = test_utils.test_tmpfile(
          _sharded('gvcf_child.tfrecord', num_shards))
      child_candidates = test_utils.test_tmpfile(
          _sharded('vsc_child.tfrecord', num_shards))
    else:
      FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
      FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
      child_candidates = test_utils.test_tmpfile(
          _sharded('vsc.tfrecord', num_shards))

    for task_id in range(max(num_shards, 1)):
      FLAGS.task = task_id
      options = make_examples.default_options(add_flags=True)
      samples = make_examples.samples_from_options(options)
      make_examples.make_examples_runner(options, samples=samples)

      # Check that our run_info proto contains the basic fields we'd expect:
      # (a) our options are written to the run_info.options field.
      run_info = make_examples_core.read_make_examples_run_info(
          options.run_info_filename)
      self.assertEqual(run_info.options, options)
      # (b) run_info.resource_metrics is present and contains our hostname.
      self.assertTrue(run_info.HasField('resource_metrics'))
      self.assertEqual(run_info.resource_metrics.host_name, platform.node())

    # Test that our candidates are reasonable, calling specific helper functions
    # to check lots of properties of the output.
    candidates = sorted(
        tfrecord.read_tfrecords(
            child_candidates, proto=deepvariant_pb2.DeepVariantCall),
        key=lambda c: variant_utils.variant_range_tuple(c.variant))
    self.verify_deepvariant_calls(candidates, options)
    self.verify_variants([call.variant for call in candidates],
                         region,
                         options,
                         is_gvcf=False)

    # Verify that the variants in the examples are all good.
    if mode == 'calling':
      examples = self.verify_examples(
          child_examples, region, options, verify_labels=False)
    else:
      examples = self.verify_examples(
          FLAGS.examples, region, options, verify_labels=True)
    example_variants = [tf_utils.example_variant(ex) for ex in examples]
    self.verify_variants(example_variants, region, options, is_gvcf=False)

    # Verify the integrity of the examples and then check that they match our
    # golden labeled examples. Note we expect the order for both training and
    # calling modes to produce deterministic order because we fix the random
    # seed.
    if mode == 'calling':
      golden_file = _sharded(testdata.GOLDEN_CALLING_EXAMPLES, num_shards)
    else:
      golden_file = _sharded(testdata.GOLDEN_TRAINING_EXAMPLES, num_shards)
    self.assertDeepVariantExamplesEqual(
        examples, list(tfrecord.read_tfrecords(golden_file)))

    if mode == 'calling':
      nist_reader = vcf.VcfReader(testdata.TRUTH_VARIANTS_VCF)
      nist_variants = list(nist_reader.query(region))
      self.verify_nist_concordance(example_variants, nist_variants)

      # Check the quality of our generated gvcf file.
      gvcfs = variant_utils.sorted_variants(
          tfrecord.read_tfrecords(child_gvcf, proto=variants_pb2.Variant))
      self.verify_variants(gvcfs, region, options, is_gvcf=True)
      self.verify_contiguity(gvcfs, region)
      gvcf_golden_file = _sharded(testdata.GOLDEN_POSTPROCESS_GVCF_INPUT,
                                  num_shards)
      expected_gvcfs = list(
          tfrecord.read_tfrecords(gvcf_golden_file, proto=variants_pb2.Variant))
      # Despite its name, assertCountEqual checks that all items are equal.
      self.assertCountEqual(gvcfs, expected_gvcfs)

    if (mode == 'training' and num_shards == 0 and
        labeler_algorithm != 'positional_labeler'):
      # The positional labeler doesn't track metrics, so don't try to read them
      # in when that's the mode.
      self.assertEqual(
          make_examples_core.read_make_examples_run_info(
              testdata.GOLDEN_MAKE_EXAMPLES_RUN_INFO).labeling_metrics,
          run_info.labeling_metrics)

  # Golden sets are created with learning/genomics/internal/create_golden.sh
  @flagsaver.flagsaver
  def test_make_examples_training_end2end_with_customized_classes_labeler(self):
    FLAGS.labeler_algorithm = 'customized_classes_labeler'
    FLAGS.customized_classes_labeler_classes_list = 'ref,class1,class2'
    FLAGS.customized_classes_labeler_info_field_name = 'type'
    region = ranges.parse_literal('20:10,000,000-10,004,000')
    FLAGS.regions = [ranges.to_literal(region)]
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.candidates = test_utils.test_tmpfile(_sharded('vsc.tfrecord'))
    FLAGS.examples = test_utils.test_tmpfile(_sharded('examples.tfrecord'))
    FLAGS.partition_size = 1000
    FLAGS.mode = 'training'
    FLAGS.gvcf_gq_binsize = 5
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF_WITH_TYPES
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    options = make_examples.default_options(add_flags=True)
    samples = make_examples.samples_from_options(options)
    make_examples.make_examples_runner(options, samples=samples)
    golden_file = _sharded(testdata.CUSTOMIZED_CLASSES_GOLDEN_TRAINING_EXAMPLES)
    # Verify that the variants in the examples are all good.
    examples = self.verify_examples(
        FLAGS.examples, region, options, verify_labels=True)
    self.assertDeepVariantExamplesEqual(
        examples, list(tfrecord.read_tfrecords(golden_file)))

  # Golden sets are created with learning/genomics/internal/create_golden.sh
  @flagsaver.flagsaver
  def test_make_examples_training_end2end_with_alt_aligned_pileup(self):
    region = ranges.parse_literal('20:10,000,000-10,010,000')
    FLAGS.regions = [ranges.to_literal(region)]
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.candidates = test_utils.test_tmpfile(_sharded('vsc.tfrecord'))
    FLAGS.examples = test_utils.test_tmpfile(_sharded('examples.tfrecord'))
    FLAGS.partition_size = 1000
    FLAGS.mode = 'training'
    FLAGS.gvcf_gq_binsize = 5

    # The following 3 lines are added.
    FLAGS.alt_aligned_pileup = 'diff_channels'
    FLAGS.pileup_image_height_child = 60
    FLAGS.pileup_image_height_parent = 40

    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    options = make_examples.default_options(add_flags=True)
    samples = make_examples.samples_from_options(options)
    make_examples.make_examples_runner(options, samples=samples)
    golden_file = _sharded(testdata.ALT_ALIGNED_PILEUP_GOLDEN_TRAINING_EXAMPLES)
    # Verify that the variants in the examples are all good.
    examples = self.verify_examples(
        FLAGS.examples, region, options, verify_labels=True)
    self.assertDeepVariantExamplesEqual(
        examples, list(tfrecord.read_tfrecords(golden_file)))
    # Pileup image should now have 8 channels.
    # Height should be 60 + 40 * 2 = 140.
    self.assertEqual(decode_example(examples[0])['image/shape'], [140, 221, 8])

  @parameterized.parameters(
      dict(select_types=None, expected_count=79),
      dict(select_types='all', expected_count=79),
      dict(select_types='snps', expected_count=64),
      dict(select_types='indels', expected_count=11),
      dict(select_types='snps indels', expected_count=75),
      dict(select_types='multi-allelics', expected_count=4),
  )
  @flagsaver.flagsaver
  def test_make_examples_with_variant_selection(self, select_types,
                                                expected_count):
    if select_types is not None:
      FLAGS.select_variant_types = select_types
    region = ranges.parse_literal('20:10,000,000-10,010,000')
    FLAGS.regions = [ranges.to_literal(region)]
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.candidates = test_utils.test_tmpfile(_sharded('vsc.tfrecord'))
    child_candidates = test_utils.test_tmpfile(_sharded('vsc_child.tfrecord'))
    FLAGS.examples = test_utils.test_tmpfile(_sharded('examples.tfrecord'))
    FLAGS.partition_size = 1000
    FLAGS.mode = 'calling'

    options = make_examples.default_options(add_flags=True)
    samples = make_examples.samples_from_options(options)
    make_examples.make_examples_runner(options, samples=samples)

    candidates = list(tfrecord.read_tfrecords(child_candidates))
    self.assertEqual(len(candidates), expected_count)

  def verify_nist_concordance(self, candidates, nist_variants):
    # Tests that we call almost all of the real variants (according to NIST's
    # Genome in a Bottle callset for NA12878) in our candidate callset.
    # Tests that we don't have an enormous number of FP calls. We should have
    # no more than 5x (arbitrary) more candidate calls than real calls. If we
    # have more it's likely due to some major pipeline problem.
    self.assertLess(len(candidates), 5 * len(nist_variants))
    tp_count = 0
    for nist_variant in nist_variants:
      if self.assertVariantIsPresent(nist_variant, candidates):
        tp_count = tp_count + 1

    self.assertGreater(
        tp_count / len(nist_variants), 0.9705,
        'Recall must be greater than 0.9705. TP={}, Truth variants={}'.format(
            tp_count, len(nist_variants)))

  def assertDeepVariantExamplesEqual(self, actual, expected):
    """Asserts that actual and expected tf.Examples from DeepVariant are equal.

    Args:
      actual: iterable of tf.Examples from DeepVariant. DeepVariant examples
        that we want to check.
      expected: iterable of tf.Examples. Expected results for actual.
    """
    self.assertEqual(len(actual), len(expected))
    for i in range(len(actual)):
      self.assertEqual(decode_example(actual[i]), decode_example(expected[i]))

  def assertVariantIsPresent(self, to_find, variants):

    def variant_key(v):
      return (v.reference_bases, v.start, v.end)

    # Finds a call in our actual call set for each NIST variant, asserting
    # that we found exactly one.
    matches = [
        variant for variant in variants
        if variant_key(to_find) == variant_key(variant)
    ]
    if not matches:
      return False

    # Verify that every alt allele appears in the call (but the call might)
    # have more than just those calls.
    for alt in to_find.alternate_bases:
      if alt not in matches[0].alternate_bases:
        return False

    return True

  def verify_variants(self, variants, region, options, is_gvcf):
    # Verifies simple properties of the Variant protos in variants. For example,
    # checks that the reference_name() is our expected chromosome. The flag
    # is_gvcf determines how we check the VariantCall field of each variant,
    # enforcing expectations for gVCF records if true or variant calls if false.
    for variant in variants:
      if region:
        self.assertEqual(variant.reference_name, region.reference_name)
        self.assertGreaterEqual(variant.start, region.start)
        self.assertLessEqual(variant.start, region.end)
      self.assertNotEqual(variant.reference_bases, '')
      self.assertGreater(len(variant.alternate_bases), 0)
      self.assertEqual(len(variant.calls), 1)

      call = variant_utils.only_call(variant)
      self.assertEqual(
          call.call_set_name,
          options.sample_options[1].variant_caller_options.sample_name)
      if is_gvcf:
        # GVCF records should have 0/0 or ./. (un-called) genotypes as they are
        # reference sites, have genotype likelihoods and a GQ value.
        self.assertIn(list(call.genotype), [[0, 0], [-1, -1]])
        self.assertEqual(len(call.genotype_likelihood), 3)
        self.assertGreaterEqual(variantcall_utils.get_gq(call), 0)

  def verify_contiguity(self, contiguous_variants, region):
    """Verifies region is fully covered by gvcf records."""
    # We expect that the intervals cover every base, so the first variant should
    # be at our interval start and the last one should end at our interval end.
    self.assertGreater(len(contiguous_variants), 0)
    self.assertEqual(region.start, contiguous_variants[0].start)
    self.assertEqual(region.end, contiguous_variants[-1].end)

    # After this loop completes successfully we know that together the GVCF and
    # Variants form a fully contiguous cover of our calling interval, as
    # expected.
    for v1, v2 in zip(contiguous_variants, contiguous_variants[1:]):
      # Sequential variants should be contiguous, meaning that v2.start should
      # be v1's end, as the end is exclusive and the start is inclusive.
      if v1.start == v2.start and v1.end == v2.end:
        # Skip duplicates here as we may have multi-allelic variants turning
        # into multiple bi-allelic variants at the same site.
        continue
      # We expect to immediately follow the end of a gvcf record but to occur
      # at the base immediately after a variant, since the variant's end can
      # span over a larger interval when it's a deletion and we still produce
      # gvcf records under the deletion.
      expected_start = v1.end if v1.alternate_bases == ['<*>'] else v1.start + 1
      self.assertEqual(v2.start, expected_start)

  def verify_deepvariant_calls(self, dv_calls, options):
    # Verifies simple structural properties of the DeepVariantCall objects
    # emitted by the VerySensitiveCaller, such as that the AlleleCount and
    # Variant both have the same position.
    for call in dv_calls:
      for alt_allele in call.variant.alternate_bases:
        # Skip ref calls.
        if alt_allele == vcf_constants.NO_ALT_ALLELE:
          continue
        # Make sure allele appears in our allele_support field and that at
        # least our min number of reads to call an alt allele are present in
        # the supporting reads list for that allele.
        self.assertIn(alt_allele, list(call.allele_support))
        self.assertGreaterEqual(
            len(call.allele_support[alt_allele].read_names),
            options.sample_options[1].variant_caller_options.min_count_snps)

  def verify_examples(self, examples_filename, region, options, verify_labels):
    # Do some simple structural checks on the tf.Examples in the file.
    expected_features = [
        'variant/encoded', 'locus', 'image/format', 'image/encoded',
        'alt_allele_indices/encoded'
    ]
    if verify_labels:
      expected_features += ['label']

    examples = list(tfrecord.read_tfrecords(examples_filename))
    for example in examples:
      for label_feature in expected_features:
        self.assertIn(label_feature, example.features.feature)
      # pylint: disable=g-explicit-length-test
      self.assertGreater(len(tf_utils.example_alt_alleles_indices(example)), 0)

    # Check that the variants in the examples are good.
    variants = [tf_utils.example_variant(x) for x in examples]
    self.verify_variants(variants, region, options, is_gvcf=False)

    return examples


class MakeExamplesUnitTest(parameterized.TestCase):

  def test_read_write_run_info(self):

    def _read_lines(path):
      with open(path) as fin:
        return list(fin.readlines())

    golden_actual = make_examples_core.read_make_examples_run_info(
        testdata.GOLDEN_MAKE_EXAMPLES_RUN_INFO)
    # We don't really want to inject too much knowledge about the golden right
    # here, so we only use a minimal test that (a) the run_info_filename is
    # a non-empty string and (b) the number of candidates sites in the labeling
    # metrics field is greater than 0. Any reasonable golden output will have at
    # least one candidate variant, and the reader should have filled in the
    # value.
    self.assertGreater(len(golden_actual.options.run_info_filename), 0)
    self.assertEqual(golden_actual.labeling_metrics.n_candidate_variant_sites,
                     testdata.N_GOLDEN_TRAINING_EXAMPLES)

    # Check that reading + writing the data produces the same lines:
    tmp_output = test_utils.test_tmpfile('written_run_info.pbtxt')
    make_examples_core.write_make_examples_run_info(golden_actual, tmp_output)
    self.assertEqual(
        _read_lines(testdata.GOLDEN_MAKE_EXAMPLES_RUN_INFO),
        _read_lines(tmp_output))

  @parameterized.parameters(
      dict(
          flag_value='CALLING',
          expected=deepvariant_pb2.MakeExamplesOptions.CALLING,
      ),
      dict(
          flag_value='TRAINING',
          expected=deepvariant_pb2.MakeExamplesOptions.TRAINING,
      ),
  )
  def test_parse_proto_enum_flag(self, flag_value, expected):
    enum_pb2 = deepvariant_pb2.MakeExamplesOptions.Mode
    self.assertEqual(
        make_examples.parse_proto_enum_flag(enum_pb2, flag_value), expected)

  def test_parse_proto_enum_flag_error_handling(self):
    with six.assertRaisesRegex(
        self, ValueError,
        'Unknown enum option "foo". Allowed options are CALLING,TRAINING'):
      make_examples.parse_proto_enum_flag(
          deepvariant_pb2.MakeExamplesOptions.Mode, 'foo')

  @flagsaver.flagsaver
  def test_keep_duplicates(self):
    FLAGS.keep_duplicates = True
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    FLAGS.mode = 'training'
    FLAGS.examples = ''
    options = make_examples.default_options(add_flags=True)
    self.assertEqual(options.pic_options.read_requirements.keep_duplicates,
                     True)

  @flagsaver.flagsaver
  def test_keep_supplementary_alignments(self):
    FLAGS.keep_supplementary_alignments = True
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    FLAGS.mode = 'training'
    FLAGS.examples = ''
    options = make_examples.default_options(add_flags=True)
    self.assertEqual(
        options.pic_options.read_requirements.keep_supplementary_alignments,
        True)

  @flagsaver.flagsaver
  def test_keep_secondary_alignments(self):
    FLAGS.keep_secondary_alignments = True
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    FLAGS.mode = 'training'
    FLAGS.examples = ''
    options = make_examples.default_options(add_flags=True)
    self.assertEqual(
        options.pic_options.read_requirements.keep_secondary_alignments, True)

  @flagsaver.flagsaver
  def test_min_base_quality(self):
    FLAGS.min_base_quality = 5
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    FLAGS.mode = 'training'
    FLAGS.examples = ''
    options = make_examples.default_options(add_flags=True)
    self.assertEqual(options.pic_options.read_requirements.min_base_quality, 5)

  @flagsaver.flagsaver
  def test_min_mapping_quality(self):
    FLAGS.min_mapping_quality = 15
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    FLAGS.mode = 'training'
    FLAGS.examples = ''
    options = make_examples.default_options(add_flags=True)
    self.assertEqual(options.pic_options.read_requirements.min_mapping_quality,
                     15)

  @flagsaver.flagsaver
  def test_default_options_with_training_random_emit_ref_sites(self):
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    FLAGS.mode = 'training'
    FLAGS.examples = ''

    FLAGS.training_random_emit_ref_sites = 0.3
    options = make_examples.default_options(add_flags=True)
    self.assertAlmostEqual(
        options.sample_options[1].variant_caller_options
        .fraction_reference_sites_to_emit, 0.3)

  @flagsaver.flagsaver
  def test_default_options_without_training_random_emit_ref_sites(self):
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    FLAGS.mode = 'training'
    FLAGS.examples = ''

    options = make_examples.default_options(add_flags=True)
    # In proto3, there is no way to check presence of scalar field:
    # redacted
    # As an approximation, we directly check that the value should be exactly 0.
    self.assertEqual(
        options.sample_options[1].variant_caller_options
        .fraction_reference_sites_to_emit, 0.0)

  def test_extract_sample_name_from_reads_single_sample(self):
    mock_sample_reader = mock.Mock()
    mock_sample_reader.header = reads_pb2.SamHeader(
        read_groups=[reads_pb2.ReadGroup(sample_id='sample_name')])
    self.assertEqual(
        make_examples.extract_sample_name_from_sam_reader(mock_sample_reader),
        'sample_name')

  @parameterized.parameters(
      # No samples could be found in the reads.
      dict(
          samples=[],
          expected_error_message='No non-empty sample name found in the input '
          'reads. Please provide the name of the sample with the --sample_name '
          'argument.'),
      # Check that we detect an empty sample name and raise an exception.
      dict(
          samples=[''],
          expected_error_message='No non-empty sample name found in the input '
          'reads. Please provide the name of the sample '
          'with the --sample_name argument.'),
      # We have more than one sample in the reads.
      dict(
          samples=['sample1', 'sample2'],
          expected_error_message=r'Multiple samples \(sample1, sample2\) were found in the input '
          'reads. DeepVariant can only call variants from a BAM file '
          'containing a single sample.'),
  )
  def test_extract_sample_name_from_reads_detects_bad_samples(
      self, samples, expected_error_message):
    mock_sample_reader = mock.Mock()
    mock_sample_reader.header = reads_pb2.SamHeader(read_groups=[
        reads_pb2.ReadGroup(sample_id=sample) for sample in samples
    ])
    with six.assertRaisesRegex(self, ValueError, expected_error_message):
      make_examples.extract_sample_name_from_sam_reader(mock_sample_reader)

  @flagsaver.flagsaver
  def test_confident_regions(self):
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    FLAGS.confident_regions = testdata.CONFIDENT_REGIONS_BED
    FLAGS.mode = 'training'
    FLAGS.examples = ''

    options = make_examples.default_options(add_flags=True)
    confident_regions = make_examples.read_confident_regions(options)

    # Our expected intervals, inlined from CONFIDENT_REGIONS_BED.
    expected = _from_literals_list([
        '20:10000847-10002407', '20:10002521-10004171', '20:10004274-10004964',
        '20:10004995-10006386', '20:10006410-10007800', '20:10007825-10008018',
        '20:10008044-10008079', '20:10008101-10008707', '20:10008809-10008897',
        '20:10009003-10009791', '20:10009934-10010531'
    ])
    # Our confident regions should be exactly those found in the BED file.
    self.assertCountEqual(expected, list(confident_regions))

  @parameterized.parameters(
      ({
          'examples': ('foo', 'foo')
      },),
      ({
          'examples': ('foo', 'foo'),
          'gvcf': ('bar', 'bar')
      },),
      ({
          'examples': ('foo@10', 'foo-00000-of-00010')
      },),
      ({
          'task': (0, 0),
          'examples': ('foo@10', 'foo-00000-of-00010')
      },),
      ({
          'task': (1, 1),
          'examples': ('foo@10', 'foo-00001-of-00010')
      },),
      ({
          'task': (1, 1),
          'examples': ('foo@10', 'foo-00001-of-00010'),
          'gvcf': ('bar@10', 'bar-00001-of-00010')
      },),
      ({
          'task': (1, 1),
          'examples': ('foo@10', 'foo-00001-of-00010'),
          'gvcf': ('bar@10', 'bar-00001-of-00010'),
          'candidates': ('baz@10', 'baz-00001-of-00010')
      },),
  )
  @flagsaver.flagsaver
  def test_sharded_outputs1(self, settings):
    # Set all of the requested flag values.
    for name, (flag_val, _) in settings.items():
      setattr(FLAGS, name, flag_val)

    FLAGS.mode = 'training'
    FLAGS.reads = ''
    FLAGS.ref = ''
    options = make_examples.default_options(add_flags=True)

    # Check all of the flags.
    for name, option_val in [('examples', options.examples_filename),
                             ('candidates', options.candidates_filename),
                             ('gvcf', options.gvcf_filename)]:
      expected = settings[name][1] if name in settings else ''
      self.assertEqual(expected, option_val)

  @flagsaver.flagsaver
  def test_gvcf_output_enabled_is_false_without_gvcf_flag(self):
    FLAGS.mode = 'training'
    FLAGS.gvcf = ''
    FLAGS.reads = ''
    FLAGS.ref = ''
    FLAGS.examples = ''
    options = make_examples.default_options(add_flags=True)
    self.assertFalse(make_examples.gvcf_output_enabled(options))

  @flagsaver.flagsaver
  def test_gvcf_output_enabled_is_true_with_gvcf_flag(self):
    FLAGS.mode = 'training'
    FLAGS.gvcf = '/tmp/foo.vcf'
    FLAGS.reads = ''
    FLAGS.ref = ''
    FLAGS.examples = ''
    options = make_examples.default_options(add_flags=True)
    self.assertTrue(make_examples.gvcf_output_enabled(options))

  def test_validate_ref_contig_coverage(self):
    ref_contigs = _make_contigs([('1', 100), ('2', 100)])

    # Fully covered reference contigs don't trigger an error.
    for threshold in [0.5, 0.9, 1.0]:
      self.assertIsNone(
          make_examples.validate_reference_contig_coverage(
              ref_contigs, ref_contigs, threshold))

    # No common contigs always blows up.
    for threshold in [0.0, 0.1, 0.5, 0.9, 1.0]:
      with six.assertRaisesRegex(self, ValueError, 'span 200'):
        make_examples.validate_reference_contig_coverage(
            ref_contigs, [], threshold)

    # Dropping either contig brings up below our 0.9 threshold.
    with six.assertRaisesRegex(self, ValueError, 'span 200'):
      make_examples.validate_reference_contig_coverage(
          ref_contigs, _make_contigs([('1', 100)]), 0.9)

    with six.assertRaisesRegex(self, ValueError, 'span 200'):
      make_examples.validate_reference_contig_coverage(
          ref_contigs, _make_contigs([('2', 100)]), 0.9)

    # Our actual overlap is 50%, so check that we raise when appropriate.
    with six.assertRaisesRegex(self, ValueError, 'span 200'):
      make_examples.validate_reference_contig_coverage(
          ref_contigs, _make_contigs([('2', 100)]), 0.6)
    self.assertIsNone(
        make_examples.validate_reference_contig_coverage(
            ref_contigs, _make_contigs([('2', 100)]), 0.4))

  @parameterized.parameters(
      # all intervals are shared.
      ([[('chrM', 10)], [('chrM', 10)]], [('chrM', 10)]),
      # No common intervals.
      ([[('chrM', 10)], [('chr1', 10)]], []),
      # The names are the same but sizes are different, so not common.
      ([[('chrM', 10)], [('chrM', 20)]], []),
      # One common interval and one not.
      ([[('chrM', 10), ('chr1', 20)], [('chrM', 10),
                                       ('chr2', 30)]], [('chrM', 10)]),
      # Check that the order doesn't matter.
      ([[('chr1', 20), ('chrM', 10)], [('chrM', 10),
                                       ('chr2', 30)]], [('chrM', 10, 1)]),
      # Three-way merges.
      ([
          [('chr1', 20), ('chrM', 10)],
          [('chrM', 10), ('chr2', 30)],
          [('chr2', 30), ('chr3', 30)],
      ], []),
  )
  def test_common_contigs(self, contigs_list, expected):
    self.assertEqual(
        _make_contigs(expected),
        make_examples.common_contigs(
            [_make_contigs(contigs) for contigs in contigs_list]))

  @parameterized.parameters(
      # Note that these tests aren't so comprehensive as we are trusting that
      # the intersection code logic itself is good and well-tested elsewhere.
      # Here we are focusing on some basic tests and handling of missing
      # calling_region and confident_region data.
      (['1:1-10'], ['1:1-10']),
      (['1:1-100'], ['1:1-100']),
      (['1:50-150'], ['1:50-100']),
      (None, ['1:1-100', '2:1-200']),
      (['1:20-50'], ['1:20-50']),
      # Chr3 isn't part of our contigs; make sure we tolerate it.
      (['1:20-30', '1:40-60', '3:10-50'], ['1:20-30', '1:40-60']),
      # Check that we handle overlapping calling or confident regions.
      (['1:25-30', '1:20-40'], ['1:20-40']),
  )
  def test_regions_to_process(self, calling_regions, expected):
    contigs = _make_contigs([('1', 100), ('2', 200)])
    self.assertCountEqual(
        _from_literals_list(expected),
        make_examples.regions_to_process(
            contigs, 1000, calling_regions=_from_literals(calling_regions)))

  @parameterized.parameters(
      (50, None, [
          '1:1-50', '1:51-100', '2:1-50', '2:51-76', '3:1-50', '3:51-100',
          '3:101-121'
      ]),
      (120, None, ['1:1-100', '2:1-76', '3:1-120', '3:121']),
      (500, None, ['1:1-100', '2:1-76', '3:1-121']),
      (10, ['1:1-20', '1:30-35'], ['1:1-10', '1:11-20', '1:30-35']),
      (8, ['1:1-20', '1:30-35'], ['1:1-8', '1:9-16', '1:17-20', '1:30-35']),
  )
  def test_regions_to_process_partition(self, max_size, calling_regions,
                                        expected):
    contigs = _make_contigs([('1', 100), ('2', 76), ('3', 121)])
    self.assertCountEqual(
        _from_literals_list(expected),
        make_examples.regions_to_process(
            contigs, max_size, calling_regions=_from_literals(calling_regions)))

  @parameterized.parameters(
      dict(includes=[], excludes=[], expected=['1:1-100', '2:1-200']),
      dict(includes=['1'], excludes=[], expected=['1:1-100']),
      # Check that excludes work as expected.
      dict(includes=[], excludes=['1'], expected=['2:1-200']),
      dict(includes=[], excludes=['2'], expected=['1:1-100']),
      dict(includes=[], excludes=['1', '2'], expected=[]),
      # Check that excluding pieces works. The main checks on taking the
      # difference between two RangeSets live in ranges.py so here we are just
      # making sure some basic logic works.
      dict(includes=['1'], excludes=['1:1-10'], expected=['1:11-100']),
      # Check that includes and excludes work together.
      dict(
          includes=['1', '2'],
          excludes=['1:5-10', '1:20-50', '2:10-20'],
          expected=['1:1-4', '1:11-19', '1:51-100', '2:1-9', '2:21-200']),
      dict(
          includes=['1'],
          excludes=['1:5-10', '1:20-50', '2:10-20'],
          expected=['1:1-4', '1:11-19', '1:51-100']),
      dict(
          includes=['2'],
          excludes=['1:5-10', '1:20-50', '2:10-20'],
          expected=['2:1-9', '2:21-200']),
      # A complex example of including and excluding.
      dict(
          includes=['1:10-20', '2:50-60', '2:70-80'],
          excludes=['1:1-13', '1:19-50', '2:10-65'],
          expected=['1:14-18', '2:70-80']),
  )
  def test_build_calling_regions(self, includes, excludes, expected):
    contigs = _make_contigs([('1', 100), ('2', 200)])
    actual = make_examples.build_calling_regions(contigs, includes, excludes)
    self.assertCountEqual(actual, _from_literals_list(expected))

  def test_regions_to_process_sorted_within_contig(self):
    # These regions are out of order but within a single contig.
    contigs = _make_contigs([('z', 100)])
    in_regions = _from_literals(['z:15', 'z:20', 'z:6', 'z:25-30', 'z:3-4'])
    sorted_regions = _from_literals_list(
        ['z:3-4', 'z:6', 'z:15', 'z:20', 'z:25-30'])
    actual_regions = list(
        make_examples.regions_to_process(
            contigs, 100, calling_regions=in_regions))
    # The assertEqual here is checking the order is exactly what we expect.
    self.assertEqual(sorted_regions, actual_regions)

  def test_regions_to_process_sorted_contigs(self):
    # These contig names are out of order lexicographically.
    contigs = _make_contigs([('z', 100), ('a', 100), ('n', 100)])
    in_regions = _from_literals(['a:10', 'n:1', 'z:20', 'z:5'])
    sorted_regions = _from_literals_list(['z:5', 'z:20', 'a:10', 'n:1'])
    actual_regions = list(
        make_examples.regions_to_process(
            contigs, 100, calling_regions=in_regions))
    # The assertEqual here is checking the order is exactly what we expect.
    self.assertEqual(sorted_regions, actual_regions)

  @parameterized.parameters([2, 3, 4, 5, 50])
  def test_regions_to_process_sharding(self, num_shards):
    """Makes sure we deterministically split up regions."""

    def get_regions(task_id, num_shards):
      return make_examples.regions_to_process(
          contigs=_make_contigs([('z', 100), ('a', 100), ('n', 100)]),
          partition_size=5,
          task_id=task_id,
          num_shards=num_shards)

    # Check that the regions are the same unsharded vs. sharded.
    unsharded_regions = get_regions(0, 0)
    sharded_regions = []
    for task_id in range(num_shards):
      task_regions = get_regions(task_id, num_shards)
      sharded_regions.extend(task_regions)
    self.assertCountEqual(unsharded_regions, sharded_regions)

  @parameterized.parameters(
      # Providing one of task id and num_shards but not the other is bad.
      (None, 0),
      (None, 2),
      (2, None),
      (0, None),
      # Negative values are illegal.
      (-1, 2),
      (0, -2),
      # task_id >= num_shards is bad.
      (2, 2),
      (3, 2),
  )
  def test_regions_to_process_fails_with_bad_shard_args(self, task, num_shards):
    with self.assertRaises(ValueError):
      make_examples.regions_to_process(
          contigs=_make_contigs([('z', 100), ('a', 100), ('n', 100)]),
          partition_size=10,
          task_id=task,
          num_shards=num_shards)

  def test_catches_bad_argv(self):
    with mock.patch.object(logging, 'error') as mock_logging,\
        mock.patch.object(sys, 'exit') as mock_exit:
      make_examples.main(['make_examples.py', 'extra_arg'])
    mock_logging.assert_called_once_with(
        'Command line parsing failure: make_examples does not accept '
        'positional arguments but some are present on the command line: '
        '"[\'make_examples.py\', \'extra_arg\']".')
    mock_exit.assert_called_once_with(errno.ENOENT)

  @flagsaver.flagsaver
  def test_catches_bad_flags(self):
    # Set all of the requested flag values.
    region = ranges.parse_literal('20:10,000,000-10,010,000')
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.candidates = test_utils.test_tmpfile('vsc.tfrecord')
    FLAGS.examples = test_utils.test_tmpfile('examples.tfrecord')
    FLAGS.regions = [ranges.to_literal(region)]
    FLAGS.partition_size = 1000
    FLAGS.mode = 'training'
    FLAGS.truth_variants = testdata.TRUTH_VARIANTS_VCF
    # This is the bad flag.
    FLAGS.confident_regions = ''

    with mock.patch.object(logging, 'error') as mock_logging,\
        mock.patch.object(sys, 'exit') as mock_exit:
      make_examples.main(['make_examples.py'])
    mock_logging.assert_called_once_with(
        'confident_regions is required when in training mode.')
    mock_exit.assert_called_once_with(errno.ENOENT)

  @parameterized.parameters(
      dict(
          ref_names=['1', '2', '3'],
          sam_names=['1', '2', '3'],
          vcf_names=None,
          names_to_exclude=[],
          min_coverage_fraction=1.0,
          expected_names=['1', '2', '3']),
      dict(
          ref_names=['1', '2', '3'],
          sam_names=['1', '2'],
          vcf_names=None,
          names_to_exclude=[],
          min_coverage_fraction=0.66,
          expected_names=['1', '2']),
      dict(
          ref_names=['1', '2', '3'],
          sam_names=['1', '2'],
          vcf_names=['1', '3'],
          names_to_exclude=[],
          min_coverage_fraction=0.33,
          expected_names=['1']),
      dict(
          ref_names=['1', '2', '3', '4', '5'],
          sam_names=['1', '2', '3'],
          vcf_names=None,
          names_to_exclude=['4', '5'],
          min_coverage_fraction=1.0,
          expected_names=['1', '2', '3']),
  )
  def test_ensure_consistent_contigs(self, ref_names, sam_names, vcf_names,
                                     names_to_exclude, min_coverage_fraction,
                                     expected_names):
    ref_contigs = _make_contigs([(name, 100) for name in ref_names])
    sam_contigs = _make_contigs([(name, 100) for name in sam_names])
    if vcf_names is not None:
      vcf_contigs = _make_contigs([(name, 100) for name in vcf_names])
    else:
      vcf_contigs = None
    actual = make_examples._ensure_consistent_contigs(ref_contigs, sam_contigs,
                                                      vcf_contigs,
                                                      names_to_exclude,
                                                      min_coverage_fraction)
    self.assertEqual([a.name for a in actual], expected_names)

  @parameterized.parameters(
      dict(
          ref_names=['1', '2', '3'],
          sam_names=['1', '2'],
          vcf_names=None,
          names_to_exclude=[],
          min_coverage_fraction=0.67),
      dict(
          ref_names=['1', '2', '3'],
          sam_names=['1', '2'],
          vcf_names=['1', '3'],
          names_to_exclude=[],
          min_coverage_fraction=0.34),
  )
  def test_ensure_inconsistent_contigs(self, ref_names, sam_names, vcf_names,
                                       names_to_exclude, min_coverage_fraction):
    ref_contigs = _make_contigs([(name, 100) for name in ref_names])
    sam_contigs = _make_contigs([(name, 100) for name in sam_names])
    if vcf_names is not None:
      vcf_contigs = _make_contigs([(name, 100) for name in vcf_names])
    else:
      vcf_contigs = None
    with six.assertRaisesRegex(self, ValueError, 'Reference contigs span'):
      make_examples._ensure_consistent_contigs(ref_contigs, sam_contigs,
                                               vcf_contigs, names_to_exclude,
                                               min_coverage_fraction)

  @flagsaver.flagsaver
  def test_regions_and_exclude_regions_flags(self):
    FLAGS.mode = 'calling'
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.regions = '20:10,000,000-11,000,000'
    FLAGS.examples = 'examples.tfrecord'
    FLAGS.exclude_regions = '20:10,010,000-10,100,000'

    options = make_examples.default_options(add_flags=True)
    self.assertCountEqual(
        list(
            ranges.RangeSet(
                make_examples.processing_regions_from_options(options))),
        _from_literals_list(
            ['20:10,000,000-10,009,999', '20:10,100,001-11,000,000']))

  @flagsaver.flagsaver
  def test_incorrect_empty_regions(self):
    FLAGS.mode = 'calling'
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    # Deliberately incorrect contig name.
    FLAGS.regions = 'xxx20:10,000,000-11,000,000'
    FLAGS.examples = 'examples.tfrecord'

    options = make_examples.default_options(add_flags=True)
    with six.assertRaisesRegex(self, ValueError,
                               'The regions to call is empty.'):
      make_examples.processing_regions_from_options(options)


class RegionProcessorTest(parameterized.TestCase):

  def setUp(self):
    super(RegionProcessorTest, self).setUp()
    self.region = ranges.parse_literal('20:10,000,000-10,000,100')

    FLAGS.reads = ''
    self.options = make_examples.default_options(add_flags=False)
    self.options.reference_filename = testdata.CHR20_FASTA
    self.options.truth_variants_filename = testdata.TRUTH_VARIANTS_VCF
    self.options.mode = deepvariant_pb2.MakeExamplesOptions.TRAINING

    self.ref_reader = fasta.IndexedFastaReader(self.options.reference_filename)
    self.default_shape = [5, 5, 7]
    self.default_format = 'raw'
    parent1 = make_examples_utils.Sample(
        role='parent1', in_memory_sam_reader=mock.Mock(), order=[0, 1, 2])
    child = make_examples_utils.Sample(
        role='child', in_memory_sam_reader=mock.Mock(), order=[0, 1, 2])
    parent2 = make_examples_utils.Sample(
        role='parent2', in_memory_sam_reader=mock.Mock(), order=[2, 1, 0])

    self.processor = make_examples.RegionProcessor(
        self.options, samples=[parent1, child, parent2])
    self.mock_init = self.add_mock('_initialize')

  def add_mock(self, name, retval='dontadd', side_effect='dontadd'):
    patcher = mock.patch.object(self.processor, name, autospec=True)
    self.addCleanup(patcher.stop)
    mocked = patcher.start()
    if retval != 'dontadd':
      mocked.return_value = retval
    if side_effect != 'dontadd':
      mocked.side_effect = side_effect
    return mocked

  @parameterized.parameters([
      deepvariant_pb2.MakeExamplesOptions.TRAINING,
      deepvariant_pb2.MakeExamplesOptions.CALLING
  ])
  def test_process_keeps_ordering_of_candidates_and_examples(self, mode):
    self.processor.options.mode = mode

    r1, r2 = mock.Mock(), mock.Mock()
    c1, c2 = mock.Mock(), mock.Mock()
    l1, l2 = mock.Mock(), mock.Mock()
    e1, e2, e3 = mock.Mock(), mock.Mock(), mock.Mock()
    self.add_mock('region_reads', retval=[r1, r2])
    self.add_mock('candidates_in_region', retval=({'child': [c1, c2]}, {}))
    mock_cpe = self.add_mock(
        'create_pileup_examples', side_effect=[[e1], [e2, e3]])
    mock_lc = self.add_mock('label_candidates', retval=[(c1, l1), (c2, l2)])
    mock_alte = self.add_mock('add_label_to_example', side_effect=[e1, e2, e3])
    candidates_dict, examples_dict, gvcfs_dict = self.processor.process(
        self.region)
    self.assertEqual({'child': [c1, c2]}, candidates_dict)
    self.assertEqual({'child': [e1, e2, e3]}, examples_dict)
    self.assertEqual({}, gvcfs_dict)

    in_memory_sam_reader = self.processor.samples[1].in_memory_sam_reader
    in_memory_sam_reader.replace_reads.assert_called_once_with([r1, r2])
    sample_order_for_child = [0, 1, 2]

    # We don't try to label variants when in calling mode.
    self.assertEqual([
        mock.call(c1, sample_order=sample_order_for_child),
        mock.call(c2, sample_order=sample_order_for_child)
    ], mock_cpe.call_args_list)

    if mode == deepvariant_pb2.MakeExamplesOptions.CALLING:
      # In calling mode, we never try to label.
      test_utils.assert_not_called_workaround(mock_lc)
      test_utils.assert_not_called_workaround(mock_alte)
    else:
      mock_lc.assert_called_once_with([c1, c2], self.region)
      self.assertEqual([
          mock.call(e1, l1),
          mock.call(e2, l2),
          mock.call(e3, l2),
      ], mock_alte.call_args_list)

  def test_create_pileup_examples_handles_none(self):
    self.processor.pic = mock.Mock()
    dv_call = mock.Mock()
    self.processor.pic.create_pileup_images.return_value = None
    self.assertEqual([],
                     self.processor.create_pileup_examples(dv_call, 'child'))
    self.processor.pic.create_pileup_images.assert_called_once()

  def test_create_pileup_examples(self):
    self.processor.pic = mock.Mock()
    self.add_mock(
        '_encode_tensor',
        side_effect=[
            (six.b('tensor1'), self.default_shape, self.default_format),
            (six.b('tensor2'), self.default_shape, self.default_format)
        ])
    dv_call = mock.Mock()
    dv_call.variant = test_utils.make_variant(start=10, alleles=['A', 'C', 'G'])
    ex = mock.Mock()
    alt1, alt2 = ['C'], ['G']
    self.processor.pic.create_pileup_images.return_value = [
        (alt1, six.b('tensor1')), (alt2, six.b('tensor2'))
    ]

    actual = self.processor.create_pileup_examples(dv_call, 'child')

    self.processor.pic.create_pileup_images.assert_called_once()

    self.assertLen(actual, 2)
    for ex, (alt, img) in zip(actual, [(alt1, six.b('tensor1')),
                                       (alt2, six.b('tensor2'))]):
      self.assertEqual(tf_utils.example_alt_alleles(ex), alt)
      self.assertEqual(tf_utils.example_variant(ex), dv_call.variant)
      self.assertEqual(tf_utils.example_encoded_image(ex), img)
      self.assertEqual(tf_utils.example_image_shape(ex), self.default_shape)
      self.assertEqual(
          tf_utils.example_image_format(ex), six.b(self.default_format))

  @parameterized.parameters(
      # Test that a het variant gets a label value of 1 assigned to the example.
      dict(
          label=variant_labeler.VariantLabel(
              is_confident=True,
              variant=test_utils.make_variant(start=10, alleles=['A', 'C']),
              genotype=(0, 1)),
          expected_label_value=1,
      ),
      # Test that a reference variant gets a label value of 0 in the example.
      dict(
          label=variant_labeler.VariantLabel(
              is_confident=True,
              variant=test_utils.make_variant(start=10, alleles=['A', '.']),
              genotype=(0, 0)),
          expected_label_value=0,
      ),
  )
  def test_add_label_to_example(self, label, expected_label_value):
    example = self._example_for_variant(label.variant)
    labeled = copy.deepcopy(example)
    actual = self.processor.add_label_to_example(labeled, label)

    # The add_label_to_example command modifies labeled and returns it.
    self.assertIs(actual, labeled)

    # Check that all keys from example are present in labeled.
    for key, value in example.features.feature.items():
      if key != 'variant/encoded':  # Special case tested below.
        self.assertEqual(value, labeled.features.feature[key])

    # The genotype of our example_variant should be set to the true genotype
    # according to our label.
    self.assertEqual(expected_label_value, tf_utils.example_label(labeled))
    labeled_variant = tf_utils.example_variant(labeled)
    call = variant_utils.only_call(labeled_variant)
    self.assertEqual(tuple(call.genotype), label.genotype)

    # The original variant and labeled_variant from out tf.Example should be
    # equal except for the genotype field, since this is set by
    # add_label_to_example.
    label.variant.calls[0].genotype[:] = []
    call.genotype[:] = []
    self.assertEqual(label.variant, labeled_variant)

  def test_label_variant_raises_for_non_confident_variant(self):
    label = variant_labeler.VariantLabel(
        is_confident=False,
        variant=test_utils.make_variant(start=10, alleles=['A', 'C']),
        genotype=(0, 1))
    example = self._example_for_variant(label.variant)
    with six.assertRaisesRegex(
        self, ValueError, 'Cannot add a non-confident label to an example'):
      self.processor.add_label_to_example(example, label)

  def _example_for_variant(self, variant):
    return tf_utils.make_example(variant, list(variant.alternate_bases),
                                 six.b('foo'), self.default_shape,
                                 self.default_format)

  def test_use_original_quality_scores_without_parse_sam_aux_fields(self):
    FLAGS.mode = 'calling'
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.examples = 'examples.tfrecord'
    FLAGS.use_original_quality_scores = True

    with six.assertRaisesRegex(
        self, Exception,
        'If use_original_quality_scores is set then parse_sam_aux_fields must be set too.'
    ):
      make_examples.default_options(add_flags=True)

  @parameterized.parameters(
      dict(height_parent=10, height_child=9),
      dict(height_parent=9, height_child=10),
      dict(height_parent=100, height_child=101),
      dict(height_parent=101, height_child=100),
  )
  @flagsaver.flagsaver
  def test_image_heights(self, height_parent, height_child):
    FLAGS.pileup_image_height_parent = height_parent
    FLAGS.pileup_image_height_child = height_child
    FLAGS.mode = 'calling'
    FLAGS.ref = testdata.CHR20_FASTA
    FLAGS.reads = testdata.HG001_CHR20_BAM
    FLAGS.reads_parent1 = testdata.NA12891_CHR20_BAM
    FLAGS.reads_parent2 = testdata.NA12892_CHR20_BAM
    FLAGS.sample_name = 'child'
    FLAGS.sample_name_to_call = 'child'
    FLAGS.sample_name_parent1 = 'parent1'
    FLAGS.sample_name_parent2 = 'parent2'
    FLAGS.examples = 'examples.tfrecord'

    options = make_examples.default_options(add_flags=True)
    with self.assertRaisesRegex(
        Exception, 'Pileup image heights must be between 10 and 100.'):
      make_examples.check_options_are_valid(options)

  @parameterized.parameters(
      [
          dict(window_width=221),
          dict(window_width=1001),
      ],)
  def test_align_to_all_haplotypes(self, window_width):
    # align_to_all_haplotypes() will pull from the reference, so choose a
    # real variant.
    region = ranges.parse_literal('20:10,046,000-10,046,400')
    nist_reader = vcf.VcfReader(testdata.TRUTH_VARIANTS_VCF)
    nist_variants = list(nist_reader.query(region))
    # We picked this region to have exactly one known variant:
    # reference_bases: "AAGAAAGAAAG"
    # alternate_bases: "A", a deletion of 10 bp
    # start: 10046177
    # end: 10046188
    # reference_name: "chr20"

    variant = nist_variants[0]

    self.processor.pic = mock.Mock()
    self.processor.pic.width = window_width
    self.processor.pic.half_width = int((window_width - 1) / 2)

    self.processor.realigner = mock.Mock()
    # Using a real ref_reader to test that the reference allele matches
    # between the variant and the reference at the variant's coordinates.
    self.processor.realigner.ref_reader = self.ref_reader

    read = test_utils.make_read(
        'A' * 101, start=10046100, cigar='101M', quals=[30] * 101)

    self.processor.realigner.align_to_haplotype = mock.Mock()
    alt_info = self.processor.align_to_all_haplotypes(variant, [read])
    hap_alignments = alt_info['alt_alignments']
    hap_sequences = alt_info['alt_sequences']

    # Both outputs are keyed by alt allele.
    self.assertCountEqual(hap_alignments.keys(), ['A'])
    self.assertCountEqual(hap_sequences.keys(), ['A'])

    # Sequence must be the length of the window.
    self.assertLen(hap_sequences['A'], self.processor.pic.width)

    # align_to_haplotype should be called once for each alt (1 alt here).
    self.processor.realigner.align_to_haplotype.assert_called_once()

    # If variant reference_bases are wrong, it should raise a ValueError.
    variant.reference_bases = 'G'
    with six.assertRaisesRegex(self, ValueError,
                               'does not match the bases in the reference'):
      self.processor.align_to_all_haplotypes(variant, [read])


if __name__ == '__main__':
  absltest.main()

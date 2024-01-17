from xopen import xopen
import pytest
import pysam

from whatshap.cli.haplotag import run_haplotag
from whatshap.cli.split import run_split


def test_split_bam(tmp_path):
    h1 = tmp_path / "h1.bam"
    h2 = tmp_path / "h2.bam"

    run_split(
        "tests/data/pacbio/pacbio.bam",
        "tests/data/pacbio/haplotags.txt",
        output_h1=h1,
        output_h2=h2,
    )
    with pysam.AlignmentFile(h1) as f:
        assert 15 == len(list(f))
    with pysam.AlignmentFile(h2) as f:
        assert 10 == len(list(f))


def test_split_bam_no_sequence(tmp_path):
    """
    Test that a BAM file w/o sequence records
    can be processed - see issue 215
    """

    expected_output = [
        "205\t1\t0\t0\n",
        "716\t1\t0\t0\n",
        "1613\t0\t0\t1\n",
        "2250\t1\t0\t0\n",
        "3551\t1\t0\t0\n",
        "4385\t1\t0\t0\n",
        "6750\t1\t0\t0\n",
        "11263\t1\t0\t0\n",
        "12930\t0\t1\t0\n",
        "23225\t0\t1\t0\n",
    ]
    rlen_hist = tmp_path / "rlenhist.tsv"
    input_bam = "tests/data/reads-no-sequence.bam"
    input_list = "tests/data/reads-no-sequence.haplotags.tsv"
    run_split(
        input_bam,
        input_list,
        output_h1="/dev/null",
        output_h2="/dev/null",
        output_untagged="/dev/null",
        read_lengths_histogram=rlen_hist,
    )

    with open(rlen_hist, "r") as dump:
        produced_output = dump.readlines()[1:]  # skip header line
        for e, p in zip(expected_output, produced_output):
            assert e == p


def test_split_fastq(tmp_path):
    # issue #371
    fastq_path = tmp_path / "reads.fastq.gz"
    list_path = tmp_path / "readlist.txt"
    with xopen(fastq_path, "w") as f:
        f.write("@r\nACGT\n+\n####\n")
    list_path.write_text("hello\tH1")
    run_split(
        str(fastq_path),
        str(list_path),
        output_h1="/dev/null",
        output_h2="/dev/null",
    )


@pytest.mark.parametrize("add_untagged", (False, True))
def test_split_tetraploid(tmp_path, add_untagged):
    outlist = tmp_path / "outlist.txt"
    outbam = tmp_path / "outbam.bam"
    # produce a list of read assignments using haplotag
    run_haplotag(
        variant_file="tests/data/haplotag_poly.vcf.gz",
        alignment_file="tests/data/haplotag_poly.bam",
        ploidy=4,
        output=outbam,
        haplotag_list=outlist,
    )
    # use list as input for split
    outbams = [ tmp_path / f"outbamh{i}.bam" for i in (1, 2, 3, 4)]
    run_split(
        reads_file=outbam,
        list_file=outlist,
        outputs=outbams,
        add_untagged=add_untagged,
    )

    expected_splits = {
        0: "S1_248595_HG00514_HAP1",
        1: "S1_103518_HG00514_HAP2",
        2: "S1_284251_NA19240_HAP1",
        3: "S1_31286_NA19240_HAP2",
    }
    for hap, bam in enumerate(outbams):
        with pysam.AlignmentFile(bam) as af:
            names = [record.query_name for record in af]
        if add_untagged:
            assert names == [expected_splits[hap], "chr1:2000000-2000099"]
        else:
            assert names == [expected_splits[hap]]

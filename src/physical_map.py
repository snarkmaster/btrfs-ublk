'''
Given a file on a btrfs volume, read its file/logical/physical extent map, and
validate whether it is suitable for use as a "virtual data" file in the sense of
`btrfs-ublk`. In particular, `validate_virtual_data_physical_map()` computes
the longest file segment which is continuously mapped onto the physical device.

To understand btrfs extent mapping, start with
  osandov-linux/scripts/btrfs_map_physical --help

Additionally, this presentation by @osandov is quite helpful:
https://events.static.linuxfound.org/sites/events/files/slides/vault2016_0.pdf
'''

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .common import SZ, get_logger, strip_nl

log = get_logger()

(
    COL_FILE_OFFSET,
    COL_FILE_SIZE,
    COL_EXTENT_OFFSET,
    COL_EXTENT_TYPE,
    COL_LOGICAL_SIZE,
    COL_LOGICAL_OFFSET,
    COL_PHYSICAL_SIZE,
    COL_DEVICE_ID,
    COL_PHYSICAL_OFFSET,
) = COL_ORDER = (
    'FILE OFFSET',
    'FILE SIZE',
    'EXTENT OFFSET',
    'EXTENT TYPE',
    'LOGICAL SIZE',
    'LOGICAL OFFSET',
    'PHYSICAL SIZE',
    'DEVID',
    'PHYSICAL OFFSET',
)
# Keys are columns as above. Values are `int` except for `COL_EXTENT_TYPE`.
PhysicalMap = List[Dict[str, Any]]


def read_raw(btrfs_ublk_dir: Path, path: Path) -> str:
    'CAUTION: Changes may not be visible until after a remount'
    log.info(f'Reading btrfs physical map for {path}')
    return subprocess.check_output(
        [
            btrfs_ublk_dir / 'osandov-linux/scripts/btrfs_map_physical',
            path,
        ],
        text=True,
    )


def parse(raw_map: str) -> PhysicalMap:
    raw_rows = [tuple(row.split('\t')) for row in strip_nl(raw_map).split('\n')]
    assert raw_rows[0] == COL_ORDER, (raw_rows[0], COL_ORDER)
    return [
        {
            col: val if col == COL_EXTENT_TYPE else int(val)
            for col, val in zip(COL_ORDER, raw_row)
        }
        for raw_row in raw_rows[1:]
    ]


def gen_continuous_parts(
    phys_map: PhysicalMap, col_offset: str, col_size: str
) -> List[Tuple[int, int]]:
    'Generates non-overlapping (offset, size) intervals'
    # Pairs can be repeated, e.g. one logical extent can back multiple file
    # extents.
    uniq_pairs = sorted({(row[col_offset], row[col_size]) for row in phys_map})

    # (start, end) is always half-open, i.e. start <= pos < end
    prev_end = None
    cur_start = None
    for offset, size in uniq_pairs:
        if cur_start is None:
            cur_start = offset

        new_end = offset + size
        if prev_end is not None:
            assert (
                prev_end <= offset
            ), f'{col_offset} / {col_size} overlap in {phys_map}'
            if prev_end != offset:
                yield (cur_start, prev_end - cur_start)
                cur_start = offset
        prev_end = new_end

    if cur_start is not None:
        yield (cur_start, new_end - cur_start)


def validate_virtual_data(
    phys_map: PhysicalMap,
) -> Tuple[int, int, int]:
    '''
    Returns (file offset, physical offset, size) of the largest continuous
    segment of file & physical bytes that correspond 1:1.

    IMPORTANT: With some methods of creating "virtual data", like
    `fallocate`, it may be that `size` is less than the size of the file.
    The caller should assert that the returned segment size is acceptable.

    Asserts all the conditions we expect to be true of our "virtual data"
    file with regards to its file, logical, and disk extents. Review
    `./osandov-linux/scripts/btrfs_map_physical --help` for the jargon.
    '''

    # While it seems like `btrfs_map_physical` already sorts this way, we
    # assume it later, so just make sure it's correct.
    #
    # `get_contigs` below will assert that logical & physical offsets are
    # co-sorted with file ones.
    phys_map = sorted(phys_map, key=lambda r: r[COL_FILE_OFFSET])

    # Multiple rows can reference the same logical extent, so dedupe.
    logical_extents = sorted(
        {(r[COL_LOGICAL_OFFSET], r[COL_LOGICAL_SIZE]) for r in phys_map}
    )
    physical_extents = sorted(
        {(r[COL_PHYSICAL_OFFSET], r[COL_PHYSICAL_SIZE]) for r in phys_map}
    )

    # NB: This **also** asserts that no extents overlap.
    def get_contigs(phys_map, col_offset, col_size):
        # Ensure that the logical & physical offsets are co-sorted with file
        # offsets (sorted above).  If they were not, this would mean that
        # the file-logical-physical map is not monotonic, which would mess
        # up the rest of the continuity testing below.
        offsets = [r[col_offset] for r in phys_map]
        assert offsets == sorted(
            offsets
        ), f'{col_offset} is not co-sorted with {COL_FILE_OFFSET}: {phys_map}'
        return list(gen_continuous_parts(phys_map, col_offset, col_size))

    # This assertion could fail if our file had:
    #  - Any sparse components, since the "no hole" option is the default in
    #    newer `btrfs-progs` releases.
    #  - Any inline extents.
    # Our "virtual data" file should never have either condition.
    file_contigs = get_contigs(phys_map, COL_FILE_OFFSET, COL_FILE_SIZE)
    assert len(file_contigs) == 1, f'File extents not continuous in {phys_map}'

    phys_contigs = get_contigs(phys_map, COL_PHYSICAL_OFFSET, COL_PHYSICAL_SIZE)

    # The code computing contigs ensures they don't overlap.  We actually
    # want to be sure that their bytes correspond 1:1, so let's confirm that
    # all the total sizes are equal.  We do two additional checks on the
    # FILE -> LOGICAL, and LOGICAL -> PHYSICAL maps below.
    chunk_lists = [
        file_contigs,
        get_contigs(phys_map, COL_LOGICAL_OFFSET, COL_LOGICAL_SIZE),
        phys_contigs,
        # The next 3 are here to cross-check that `get_contigs` is computing
        # sizes correctly.
        [(r[COL_FILE_OFFSET], r[COL_FILE_SIZE]) for r in phys_map],
        logical_extents,
        physical_extents,
    ]
    chunk_list_sizes = [sum(sz for (_, sz) in c) for c in chunk_lists]
    assert 1 == len(set(chunk_list_sizes)), (
        'File/logical/physical extents vary in total length: '
        f'{chunk_list_sizes} -- {chunk_lists}'
    )

    # There's a many:one correspondence from FILE to LOGICAL extents, and
    # EXTENT OFFSET describes the mapping. Check that each logical extent
    # is exactly covered by its own file extents.
    #
    # In arbitrary files, btrfs **only** guarantees that each file extents
    # is a subset of its logical extent.  The "exactly covered" condition
    # can be violated due to due to hole-punching / cloning / deduping.
    # However, our "virtual data" file never encounters these scenarios.
    #
    # No need to check FILE OFFSET here because above, we've already
    # asserted that file extents are continuous.
    file_ext_sizes = []  # These all map to the logical extent at `log_ext_idx`
    log_ext_idx = None
    for row in phys_map:
        file_sz = row[COL_FILE_SIZE]
        ext_off = row[COL_EXTENT_OFFSET]

        if ext_off == 0:  # New logical extent
            if log_ext_idx is None:
                log_ext_idx = 0
                assert not file_ext_sizes
            else:
                _, log_sz = logical_extents[log_ext_idx]
                assert sum(file_ext_sizes) == log_sz, (
                    f'File extent group {file_ext_sizes} before {ext_off} '
                    f'did not map 1:1 onto logical extent of size {log_sz}'
                )
            file_ext_sizes = []

        assert ext_off == sum(file_ext_sizes)
        file_ext_sizes.append(file_sz)

    for row in phys_map:
        assert 1 == row[COL_DEVICE_ID], f'Unexpected DEVID in {phys_map}'
        assert (
            'regular' == row[COL_EXTENT_TYPE]
        ), f'Not all extents are "regular" in {phys_map}'
        # Per above, "logical" and "physical" bytes should correspond 1:1,
        # and moreover we can expect individual extents to correspond 1:1.
        #
        # This implies, in particular, that "virtual data" cannot be
        # inline-compressed.  This is OK since our `ublk` driver can equally
        # well handle the compression.
        assert (
            row[COL_PHYSICAL_SIZE] == row[COL_LOGICAL_SIZE]
        ), f'Physical size differs from logical: {row}'

    # Figure out the largest usable address space within the file.  Above, we
    # already checked that:
    #  - file extents continuously cover the whole file
    #  - each logical extent is sequentially covered by file extents
    #  - each logical extent maps 1:1 to a physical extent
    # So, it is enough to take the larges physical contig, and find its file
    # extents.
    big_phys_off, big_phys_size = max(phys_contigs, key=lambda x: x[1])
    file_offset_matches = [
        (row[COL_FILE_OFFSET], row[COL_EXTENT_OFFSET])
        for row in phys_map
        if row[COL_PHYSICAL_OFFSET] == big_phys_off
    ]
    assert file_offset_matches == sorted(file_offset_matches)  # Sorted above
    assert (
        file_offset_matches
    ), f'No file offset match for physical offset {big_phys_off} in {phys_map}'
    assert file_offset_matches[0][1] == 0, (
        f'Nonzero extent offset for first-in-physical-extent file extent at '
        f'{file_offset_matches[0][0]}, in {phys_map}'
    )

    return file_offset_matches[0][0], big_phys_off, big_phys_size


def test_single_extent():
    '''
    The simple one-line test input is the actual "virtual data" file
    produced by `read-unwritten-block-via-mega-extent.sh` and
    `temp_mega_extent_seed_device()` via a modified `mkfs.btrfs`.  While it
    deviates from standard btrfs extent/chunk sizing, is the simplest
    "virtual data" layout that works for `btrfs-ublk`.
    '''
    with open('testdata/physical_map_single.tsv') as _f:
        pm = parse(_f.read())
    assert (
        0,
        274877972480,
        4611686018427387904,
    ) == validate_virtual_data(pm)


def test_complex_fallocated_extents():
    '''
    Tests a complex physical map was made by `testdata/physical_map_gen.sh`,
    please read the docs there. The main point of this test is to see
    what happens if we follow a "standard" btrfs extent allocation strategy,
    instead of hacking up `mkfs.btrfs` to emit a file with one mega-extent.

    Apologies: parsing 300k extents for a measly 73TiB of address space
    makes for a 4-second test, thanks to Python's poor perf.
    '''
    with subprocess.Popen(
        ["zstd", "-cd", "testdata/physical_map_75T.tsv.zst"],
        text=True,
        stdout=subprocess.PIPE,
    ) as proc:
        pm = parse(proc.stdout.read())

    # The first 6 file extents map to 3x 256MiB logical/physical extents.
    assert (
        0,
        2186280960,
        3 * 256 * SZ.M,
    ) == validate_virtual_data(pm[:6])

    # There is a discontinuity after 1012 256 MiB logical & physical
    # extents, row 1016 of the file.
    assert (
        0,
        2186280960,
        1015 * 256 * SZ.M,
    ) == validate_virtual_data(pm[:1100])

    # The final discontinuity is after 73T (301303 256MiB extents), and it
    # marks the largest contig.
    assert (
        272461987840,
        274916704256,
        300288 * 256 * SZ.M,
    ) == validate_virtual_data(pm)

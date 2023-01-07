#!/usr/bin/env python3
'''
This is a Python clone of `demo-via-mega-extent.sh` -- refer to the docs
at the top of that shell script.

Unlike the shell variant, you can pass `--use-fallocate-seed` to try the
seed device creation strategy from `bad-make-seed-via-fallocate.sh`.
'''

import argparse
import os
import sys
from contextlib import ExitStack

from src import physical_map
from src.btrfs_ublk import (
    copy_full_range,
    loop_dev,
    set_up_temp_seed_backed_mount,
    temp_fallocate_seed_device,
    temp_mega_extent_seed_device,
)
from src.cli import init_cli
from src.common import (
    SZ,
    Path,
    assert_file_smaller_than,
    get_logger,
    suffixed_byte_size,
)

log = get_logger()


def logical_reads_of_physical_writes(
    stack: ExitStack,
    opts: argparse.Namespace,
    seed: Path,
    # With the `fallocate` strategy, not all sizes will result in a
    # reasonably large continuous file-physical mapping.  Assert we got at
    # least this much.
    min_continuous_size: int,
):
    seed_loop = stack.enter_context(loop_dev(seed))
    vol, rw_backing = set_up_temp_seed_backed_mount(stack, opts, seed_loop)
    virtual_data = vol / opts.virtual_data_filename
    file_offset, phys_offset, size = physical_map.validate_virtual_data(
        physical_map.parse(
            physical_map.read_raw(opts.btrfs_ublk_dir, virtual_data)
        )
    )
    assert size >= min_continuous_size, size
    offset_75pct = 3 * (size // 4)

    virt_data_fd = stack.enter_context(open(virtual_data, 'r')).fileno()

    log.info(f'Cloning into {vol}/{{start,end,mind}}...')

    start_fd = stack.enter_context(open(vol / 'start', 'w+')).fileno()
    mid_fd = stack.enter_context(open(vol / 'mid', 'w+')).fileno()
    end_fd = stack.enter_context(open(vol / 'end', 'w+')).fileno()

    # Clone in 40G chunks so that the speed of the script proves we do CoW
    clone_sz = 40 * SZ.G
    copy_full_range(virt_data_fd, start_fd, clone_sz, 0, 0)
    copy_full_range(virt_data_fd, mid_fd, clone_sz, offset_75pct, 0)
    copy_full_range(virt_data_fd, end_fd, clone_sz, size - clone_sz, 0)

    log.info(f'Finished cloning {3 * clone_sz / SZ.G} GiB of data')

    # Make some writes directly to the seed device, bypassing `btrfs`.
    with open(seed, 'w') as f:
        os.pwrite(f.fileno(), b'clownstart', phys_offset)
        os.pwrite(f.fileno(), b'clown75pct', phys_offset + offset_75pct)
        os.pwrite(f.fileno(), b'clown100pct', phys_offset + size - 11)

    log.info(f'Wrote data directly to {seed}')

    # Read back the just-written data via the `btrfs` cloned files.
    assert b'clownstart' == os.pread(start_fd, 10, 0)
    assert b'clown75pct' == os.pread(mid_fd, 10, 0)
    assert b'clown100pct' == os.pread(end_fd, 11, clone_sz - 11)

    log.info('Read back the same data via cloned btrfs files')

    assert_file_smaller_than(rw_backing, 10 * SZ.M)


def main(stack: ExitStack, opts: argparse.Namespace):
    if opts.use_fallocate_seed:
        if opts.virtual_data_size > SZ.T:
            sys.exit(
                '''\
`--use-fallocate-seed` can only be used with `--virtual-data-size` smaller \
than 1T.  Otherwise, filesystem setup will be VERY slow due to the fact that \
we use `btrfs_corrupt_block` internally.  This could be improved, but isn't \
worthwhile today.  Keep in mind that `fallocate -l 10P` itself hits further \
perf bottlenecks -- in one experiment, I let it run for > 1000 minutes \
before killing.\
'''
            )
        seed = stack.enter_context(
            temp_fallocate_seed_device(opts, opts.virtual_data_size)
        )
        # Asserting that at least 80% of the file map continuously WILL fail
        # for some corner-case sizes, but ...  300G seems to work fine.
        min_continuous_size = 4 * (opts.virtual_data_size / 5)
    else:
        seed = stack.enter_context(
            temp_mega_extent_seed_device(opts, opts.virtual_data_size)
        )
        # The mega-extent guarantees the whole file is continuous.
        min_continuous_size = opts.virtual_data_size
    logical_reads_of_physical_writes(stack, opts, seed, min_continuous_size)


if __name__ == '__main__':
    with init_cli(__doc__) as cli:
        cli.parser.add_argument(
            '--virtual-data-size', default='4E', type=suffixed_byte_size
        )
        cli.parser.add_argument('--use-fallocate-seed', action='store_true')
    with ExitStack() as stack:
        main(stack, cli.args)

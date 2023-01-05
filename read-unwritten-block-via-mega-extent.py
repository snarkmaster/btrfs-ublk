#!/usr/bin/env python3
'''
This is a Python clone of `read-unwritten-block-via-mega-extent.sh`, just to
demonstrate library usage. The docs are at the top of the shell script.
'''

import argparse
import os
from contextlib import ExitStack

from src import physical_map
from src.btrfs_ublk import (
    loop_dev,
    set_up_temp_seed_backed_mount,
    temp_mega_extent_seed_device,
)
from src.cli import init_cli
from src.common import SZ, assert_file_smaller_than, get_logger

log = get_logger()


def copy_full_range(src, dst, count, offset_src, offset_dst):
    while count:
        ret = os.copy_file_range(src, dst, count, offset_src, offset_dst)
        count -= ret
        offset_src += ret
        offset_dst += ret


def main(stack: ExitStack, opts: argparse.Namespace):
    seed = stack.enter_context(temp_mega_extent_seed_device(opts))
    seed_loop = stack.enter_context(loop_dev(seed))
    vol, rw_backing = set_up_temp_seed_backed_mount(stack, opts, seed_loop)
    virtual_data = vol / opts.virtual_data_filename
    file_offset, phys_offset, size = physical_map.validate_virtual_data(
        physical_map.parse(
            physical_map.read_raw(opts.btrfs_ublk_dir, virtual_data)
        )
    )
    assert file_offset == 0, file_offset
    assert size == 4611686018427387904, size
    offset_75pct = 3 * (size // 4)

    virt_data_fd = stack.enter_context(open(virtual_data, 'r')).fileno()

    log.info(f'Starting to clone into {vol}/{{start,end,mind}}...')

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


if __name__ == '__main__':
    with init_cli(__doc__) as cli:
        pass
    with ExitStack() as stack:
        main(stack, cli.args)

#!/usr/bin/env python3
'''
Configurable benchmarking tool for `btrfs-ublk`. Sample usage:

sudo ./isolate.sh ./benchmark.py --json-opts "$(<sample_bench.json)" \
  >> bench-log.json

You may want to use `benchmark_matrix.py`.
'''

import argparse
import json
import os
import random
import shlex
import subprocess
from contextlib import ExitStack
from typing import Tuple

from src import physical_map
from src.btrfs_ublk import (
    copy_full_range,
    mount,
    set_up_temp_seed_backed_mount,
    temp_fallocate_seed_device,
    temp_mega_extent_seed_device,
    ublk_loop_dev,
)
from src.cli import init_cli
from src.common import (
    SZ,
    Path,
    assert_file_smaller_than,
    get_logger,
    suffixed_byte_size,
    temp_dir,
)

log = get_logger()


def temp_mount_validate_virtual_data(
    opts: argparse.Namespace, seed: Path
) -> Tuple[int, int, int]:
    with temp_dir() as td, mount(seed, td, ['ro']):
        return physical_map.validate_virtual_data(
            physical_map.parse(
                physical_map.read_raw(
                    opts.btrfs_ublk_dir, td / opts.virtual_data_filename
                )
            )
        )


# Note: we don't do defaults because these benchmark specs should either be
# code-generated, or when human-edited -- painfully explicit.
#
# Future: Try varying other ublk options as noted in `ublk_loop_dev()`.
OPTION_KEYS = {
    'btrfs.seed',  # Formatting strategy: `fallocate` or `mega-extent`
    # Keep this < 1T for `fallocate`, since our poor, quick-and-dirty
    # implementation causes formatting to become much slower with size.
    'btrfs.virtual_data_size',
    # The benchmark reads from a file of this size, concatenated from clones
    # from several parts of "virtual data".  Affects page cache hit rate.
    'btrfs.total_clone_size',
    'fio.depth',  # Only relevant for async engines
    'fio.direct',  # Only direct IO is async, buffered IO is not
    'fio.engine',
    'fio.jobs',
    'fio.runtime',
    'ublk.num_queues',  # Number of `ublk` server threads available
}


def main(stack: ExitStack, opts: argparse.Namespace):
    o = opts.json_opts
    mismatch_keys = set(o.keys()).symmetric_difference(OPTION_KEYS)
    assert not mismatch_keys, mismatch_keys

    virtual_data_size = suffixed_byte_size(o['btrfs.virtual_data_size'])
    total_clone_sz = suffixed_byte_size(o['btrfs.total_clone_size'])

    if o['btrfs.seed'] == 'fallocate':
        seed = stack.enter_context(
            temp_fallocate_seed_device(opts, virtual_data_size)
        )
    elif o['btrfs.seed'] == 'mega-extent':
        seed = stack.enter_context(
            temp_mega_extent_seed_device(opts, virtual_data_size)
        )
    else:
        raise AssertionError(f'Unknown btrfs.seed choice: {o["btrfs.seed"]}')

    virt_file_off, virt_phys_off, virt_size = temp_mount_validate_virtual_data(
        opts, seed
    )
    if o['btrfs.seed'] == 'fallocate':
        # With the `fallocate` strategy, not all sizes will result in a
        # reasonably large continuous file-physical mapping.
        #
        # Asserting that at least 80% of the file map continuously WILL fail
        # for some corner-case sizes, but ...  300G seems to work fine.
        assert virt_size >= 4 * (virtual_data_size // 5), virt_size
    elif o['btrfs.seed'] == 'mega-extent':
        assert virt_file_off == 0, virt_file_off
        # The mega-extent guarantees the whole file is continuous.
        assert virt_size == virtual_data_size, virt_size

    # Clone from the start (1x), middle (2x), and end (1x) of "virtual data"
    assert total_clone_sz % (4096 * 4) == 0, total_clone_sz
    clone_chunk_sz = total_clone_sz // 4
    assert virt_size >= 4096 + 2 * clone_chunk_sz, (virt_size, clone_chunk_sz)
    clone_chunk_offset_sizes = [
        (virt_file_off, clone_chunk_sz),
        (
            virt_file_off + (virt_size // 8192) * 4096 - clone_chunk_sz,
            2 * clone_chunk_sz,
        ),
        (virt_file_off + virt_size - clone_chunk_sz, clone_chunk_sz),
    ]

    assert virt_phys_off % 512 == 0, virt_phys_off
    assert virt_size % 512 == 0, virt_size
    seed_loop = stack.enter_context(
        ublk_loop_dev(
            opts,
            seed,
            num_queues=o['ublk.num_queues'],
            magic_sectors_start=virt_phys_off // 512,
            magic_sectors_count=virt_size // 512,
        )
    )

    vol, rw_backing = set_up_temp_seed_backed_mount(stack, opts, seed_loop)

    virtual_data = vol / opts.virtual_data_filename
    log.info(f'Cloning from {virtual_data} into {vol}/cloned...')
    cloned = vol / 'cloned'
    cloned_fd = stack.enter_context(open(cloned, 'w+')).fileno()
    with open(virtual_data, 'r') as virt_f:
        dst_chunk_off = 0
        for src_chunk_off, chunk_sz in clone_chunk_offset_sizes:
            copy_full_range(
                virt_f.fileno(),
                cloned_fd,
                chunk_sz,
                src_chunk_off,
                dst_chunk_off,
            )
            dst_chunk_off += chunk_sz
    log.info(f'Finished cloning {dst_chunk_off / SZ.G} GiB of data')
    assert dst_chunk_off == total_clone_sz, total_clone_sz

    fio_cmd = [
        'fio',
        # Core job semantics
        '--name=randread-4k',
        f'--filename={cloned}',
        '--bs=4k',
        '--output-format=json',
        '--rw=randread',
        # Perf tweaks -- keep these alphabetical
        f'--direct={o["fio.direct"]}',
        f'--iodepth={o["fio.depth"]}',
        f'--ioengine={o["fio.engine"]}',
        '--loop=1000000000',  # Let's be capped on runtime, not file size
        '--norandommap',  # The random map causes `fio` to be slower
        f'--numjobs={o["fio.jobs"]}',
        f'--runtime={o["fio.runtime"]}',
    ]
    log.info(f'Running `{" ".join(shlex.quote(c) for c in fio_cmd)}`...')
    # Future: Additionally aggregate and record:
    #   - 1-10-50-90-95-99-99.9-99.99 latency
    #   - Maybe: iodepth_level / iodepth_submit / iodepth_complete
    #   - usr_cpu / sys_cpu
    fio_jobs = json.loads(subprocess.check_output(fio_cmd, text=True))['jobs']
    total_ios = 0
    total_runtime = 0
    for fio_job in fio_jobs:
        total_ios += fio_job["read"]["total_ios"]
        total_runtime += fio_job["read"]["runtime"] / 1000.0  # ms -> sec
    job_iops = total_ios / total_runtime
    total_iops = job_iops * len(fio_jobs)
    log.info(f'{total_iops} IOPS ({job_iops} per job)')
    json_log = {
        **o,
        'total_iops': total_iops,
        'job_iops': job_iops,  # denormal, but convenient
    }
    print(json.dumps(json_log))

    # Check that we get the right data back from ublk
    for sz in [1, 3, 511, 512, 1023, 1337]:
        for off in [0, total_clone_sz - sz, None, None, None]:
            off = random.randint(0, total_clone_sz - sz) if off is None else off
            assert b'a' * sz == os.pread(cloned_fd, sz, off)
    log.info('Confirmed that ublk driver is returning "a" bytes')

    assert_file_smaller_than(rw_backing, 10 * SZ.M)


if __name__ == '__main__':
    with init_cli(__doc__) as cli:
        cli.parser.add_argument('--json-opts', type=json.loads)
    with ExitStack() as stack:
        main(stack, cli.args)

import argparse
import os
import re
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Optional, Sequence, Tuple

from . import physical_map
from .common import SZ, assert_file_smaller_than, get_logger, strip_nl, temp_dir

log = get_logger()


def _check_call_quiet(*args, **kwargs):
    'Do not spam stdout.'
    return subprocess.check_call(*args, **kwargs, stdout=2)


def copy_full_range(src, dst, count, offset_src, offset_dst):
    '''
    Intended to be analogous to:
        xfs_io -c 'reflink src offset_src offset_dst count' dst
    On non-CoW filesystems this could fall back to eager copy, but **shrug**.
    '''
    while count:
        ret = os.copy_file_range(src, dst, count, offset_src, offset_dst)
        count -= ret
        offset_src += ret
        offset_dst += ret


def btrfs_scan_device(path: Path):
    '''
    This can be needed if we're switching a seed device between plain and
    ublk loop afor one multi-device setup, since btrfs might decide to
    look for the seed device at the old device path.
    '''
    _check_call_quiet(['btrfs', 'device', 'scan', path])


@contextmanager
def loop_dev(path: Path) -> Path:
    loop = Path(
        strip_nl(
            subprocess.check_output(
                ['losetup', '--show', '-f', path], text=True
            )
        )
    )
    try:
        yield loop
    finally:
        _check_call_quiet(['losetup', '-d', loop])


@contextmanager
def ublk_loop_dev(
    opts: argparse.Namespace,
    path: Path,
    *,
    num_queues: int,
    magic_sectors_start: int,
    magic_sectors_count: int,
) -> Path:
    '''
    `num_queues`: Start with 2x the number of client processes.  For a
    single `fio` job, setting `num_queues` to 2 seems to slightly improve
    perf (per my lazy benchmarks).  IIUC this results in `ublk` serving with
    2 threads instead of 1.
    '''
    'Returns the path to the (hacked-up) `ublk` loop device.'
    ublk = opts.btrfs_ublk_dir / 'ubdsrv/ublk'
    #
    # Future:
    #  - Is it helpful to play with queue depth `-d`?
    #  - Should we set `-u 1`? It's supposed to slightly improve IOPS,
    #    but I wasn't able to measure it in my lazy benchmark.
    #    https://www.spinics.net/lists/linux-block/msg86692.html
    ublk_cmd = [ublk, 'add', '-t', 'loop', '-q', str(num_queues), '-f', path]
    ublk_cmd.extend(
        [
            f'--magic_sectors_start={magic_sectors_start}',
            f'--magic_sectors_count={magic_sectors_count}',
        ]
    )
    out = subprocess.check_output(ublk_cmd, text=True)
    dev_id = re.match('dev id ([0-9]+): .*', out).group(1)
    try:
        yield Path(f'/dev/ublkb{dev_id}')
    finally:
        # FIXME: If the device is still mounted / in use, this will deadlock.
        _check_call_quiet([ublk, 'del', '-n', dev_id])


@contextmanager
def mount(src: Path, dest: Path, opts: Optional[Sequence[str]] = None):
    _check_call_quiet(['mount', '-o', ','.join(opts or []), src, dest])
    try:
        yield
    finally:
        _check_call_quiet(['umount', '-l', dest])


def remount(dest: Path, opts: Sequence[str]):
    _check_call_quiet(['mount', '-o', ','.join(['remount', *opts]), dest])


def _allocate_temp_seed(td: Path, opts: argparse.Namespace):
    seed = td / 'seed.btrfs'
    # FIXME: The way that `kernel-shared/volumes.c` is hacked up right now,
    # we need a humongous filesystem to be able to allocate chunks.
    size = 6 * SZ.E
    with open(seed, 'w') as f:
        f.truncate(size)
    log.info(f'Allocated {size / SZ.T} TiB in {seed}')
    return seed


def make_seed_device(seed):
    _check_call_quiet(['btrfstune', '-S', '1', seed])


@contextmanager
def temp_mega_extent_seed_device(
    opts: argparse.Namespace, virtual_data_size: int
) -> Path:
    'HACK: Relies on `mkfs.btrfs` special-casing `opts.virtual_data_filename`'
    with temp_dir() as td:
        seed = _allocate_temp_seed(td, opts)

        # Lay out the filesystem for the seed device
        src_dir = td / 'src'
        src_dir.mkdir()
        with open(src_dir / opts.virtual_data_filename, 'w') as f:
            f.truncate(virtual_data_size)

        # Format / populate the seed device
        _check_call_quiet(
            [
                opts.btrfs_ublk_dir / 'btrfs-progs/mkfs.btrfs',
                f'--rootdir={src_dir}',
                seed,
            ]
        )

        make_seed_device(seed)
        assert_file_smaller_than(seed, 10 * SZ.M)
        log.info(f'Formatted mega-extent seed device: {seed}')

        yield seed


@contextmanager
def temp_fallocate_seed_device(
    opts: argparse.Namespace, virtual_data_size: int
) -> Path:
    '''
    This works with stock `btrfs-progs`, but requires the hacky usage of
    `btrfs-corrupt-block` to convert `preallocated` extents to `regular`.

    Read the doc in `bad-make-seed-via-fallocate.sh` for more context.
    '''
    with temp_dir() as td:
        seed = _allocate_temp_seed(td, opts)

        # Format the seed device with stock `btrfs-progs` to avoid our
        # hacked-up extent / chunk sizing.
        _check_call_quiet(['mkfs.btrfs', seed])

        log.info(f'Prepared empty btrfs at {seed}')

        vol = td / 'vol'
        vol.mkdir()
        with mount(seed, vol, ['nodatasum']):
            virtual_data = vol / opts.virtual_data_filename
            with open(virtual_data, 'w') as f:
                os.posix_fallocate(f.fileno(), 0, virtual_data_size)
                virtual_data_ino = os.stat(f.fileno()).st_ino
            log.info(
                f'fallocated {virtual_data_size / SZ.T} TiB at {virtual_data}'
            )

            phys_map = physical_map.parse(
                physical_map.read_raw(opts.btrfs_ublk_dir, virtual_data)
            )
            # Unmount before using `btrfs_corrupt_block`

        # NB: Above ~500G this is SUPER SLOW because we spawn thousands of
        # subprocesses that constantly reparse the extent tree (i.e.  worse
        # than linear slowdown), but it's not worthwhile to optimize this
        # for the sake of a demo.
        log.info('Converting all extents from `preallocated` to `regular`...')
        for row in phys_map:
            _check_call_quiet(
                [
                    opts.btrfs_ublk_dir / 'btrfs-progs/btrfs-corrupt-block',
                    f'--inode={virtual_data_ino}',
                    f'--file-extent={row[physical_map.COL_FILE_OFFSET]}',
                    '--field=type',
                    '--value=1',
                    seed,
                ]
            )

        make_seed_device(seed)
        assert_file_smaller_than(seed, 10 * SZ.M)
        log.info(
            f'Prepared `fallocate` / `btrfs_corrupt_block` seed device: {seed}'
        )

        yield seed


def set_up_temp_seed_backed_mount(
    stack: ExitStack,
    opts: argparse.Namespace,
    seed_loop: Path,
) -> Tuple[Path, Path]:
    td = stack.enter_context(temp_dir())

    # Allocate the read-write portion and make its loop device
    rw_backing = td / 'rw.btrfs'
    with open(rw_backing, 'w') as f:
        f.truncate(opts.rw_fs_size)
    rw_loop = stack.enter_context(loop_dev(rw_backing))

    # Mount the seed device, and add RW data to it.
    vol = td / 'vol'
    vol.mkdir()
    stack.enter_context(mount(seed_loop, vol))
    _check_call_quiet(['btrfs', 'device', 'add', rw_loop, vol])
    # `nodatasum` is required to clone from `.btrfs-ublk-virtual-data` since
    # this huge, lazy-loaded area cannot have precomputed checksums.
    remount(vol, ['rw', 'nodatasum'])
    log.info(f'Mounted read-write volume {vol} based on seed')

    assert_file_smaller_than(rw_backing, 10 * SZ.M)

    return vol, rw_backing

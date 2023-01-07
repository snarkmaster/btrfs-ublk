import argparse
import os
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Optional, Sequence, Tuple

from . import physical_map
from .common import SZ, assert_file_smaller_than, get_logger, strip_nl, temp_dir

log = get_logger()


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
        subprocess.check_call(['losetup', '-d', loop])


@contextmanager
def mount(src: Path, dest: Path, opts: Optional[Sequence[str]] = None):
    subprocess.check_call(['mount', '-o', ','.join(opts or []), src, dest])
    try:
        yield
    finally:
        subprocess.check_call(['umount', '-l', dest])


def remount(dest: Path, opts: Sequence[str]):
    subprocess.check_call(['mount', '-o', ','.join(['remount', *opts]), dest])


def allocate_temp_seed(td: Path, opts: argparse.Namespace):
    seed = td / 'seed.btrfs'
    # FIXME: The way that `kernel-shared/volumes.c` is hacked up right now,
    # we need a humongous filesystem to be able to allocate chunks.
    size = 6 * SZ.E
    with open(seed, 'w') as f:
        f.truncate(size)
    log.info(f'Allocated {size / SZ.T} TiB in {seed}')
    return seed


def make_seed_device(seed):
    subprocess.check_call(['btrfstune', '-S', '1', seed])


@contextmanager
def temp_mega_extent_seed_device(opts: argparse.Namespace) -> Path:
    'HACK: Relies on `mkfs.btrfs` special-casing `opts.virtual_data_filename`'
    with temp_dir() as td:
        seed = allocate_temp_seed(td, opts)

        # Lay out the filesystem for the seed device
        src_dir = td / 'src'
        src_dir.mkdir()
        with open(src_dir / opts.virtual_data_filename, 'w') as f:
            f.truncate(opts.virtual_data_size)

        # Format / populate the seed device
        subprocess.check_call(
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
def temp_fallocate_seed_device(opts: argparse.Namespace) -> Path:
    '''
    This works with stock `btrfs-progs`, but requires the hacky usage of
    `btrfs-corrupt-block` to convert `preallocated` extents to `regular`.

    Read the doc in `bad-make-seed-via-fallocate.sh` for more context.
    '''
    with temp_dir() as td:
        seed = allocate_temp_seed(td, opts)

        # Format the seed device with stock `btrfs-progs` to avoid our
        # hacked-up extent / chunk sizing.
        subprocess.check_call(['mkfs.btrfs', seed])

        log.info(f'Prepared empty btrfs at {seed}')

        vol = td / 'vol'
        vol.mkdir()
        with mount(seed, vol, ['nodatasum']):
            virtual_data = vol / opts.virtual_data_filename
            with open(virtual_data, 'w') as f:
                os.posix_fallocate(f.fileno(), 0, opts.virtual_data_size)
                virtual_data_ino = os.stat(f.fileno()).st_ino
            log.info(
                f'`fallocate`d {opts.virtual_data_size / SZ.T} TiB '
                f'at {virtual_data}'
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
            subprocess.check_call(
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
    subprocess.check_call(['btrfs', 'device', 'add', rw_loop, vol])
    # `nodatasum` is required to clone from `.btrfs-ublk-virtual-data` since
    # this huge, lazy-loaded area cannot have precomputed checksums.
    remount(vol, ['rw', 'nodatasum'])
    log.info(f'Mounted read-write volume {vol} based on seed')

    assert_file_smaller_than(rw_backing, 10 * SZ.M)

    return vol, rw_backing

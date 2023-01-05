import argparse
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Sequence, Tuple

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
def mount(src: Path, dest: Path):
    subprocess.check_call(['mount', src, dest])
    try:
        yield
    finally:
        subprocess.check_call(['umount', '-l', dest])


def remount(dest: Path, opts: Sequence[str]):
    subprocess.check_call(['mount', '-o', ','.join(['remount', *opts]), dest])


@contextmanager
def temp_mega_extent_seed_device(opts: argparse.Namespace) -> Path:
    with temp_dir() as td:
        seed = td / 'seed.btrfs'

        # Allocate the seed device backing file
        with open(seed, 'w') as f:
            # The `seed.btrfs` sparse file needs to be a bit bigger than the
            # "virtual data" file inside it.
            f.truncate(opts.virtual_data_size + 10 * SZ.G)

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

        subprocess.check_call(['btrfstune', '-S', '1', seed])
        assert_file_smaller_than(seed, 10 * SZ.M)
        log.info(f'Formatted mega-extent seed device: {seed}')

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

import argparse
import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from .common import get_logger, suffixed_byte_size

log = get_logger()


class CLI:
    parser: argparse.ArgumentParser
    args: argparse.Namespace


@contextmanager
def init_cli(description: str, argv: Optional[List[str]] = None):
    if argv is None:
        argv = sys.argv

    logging.basicConfig(
        level=logging.INFO,
        format='\x1b[1m%(asctime)s %(levelname)s:\x1b[0m %(message)s',
    )

    if 0 != subprocess.run(['capsh', '--has-p=CAP_SYS_ADMIN']).returncode:
        log.error(f'Please run this via `sudo ./isolate.sh {argv[0]}`')
        sys.exit(1)

    cli = CLI()

    my_dir = Path(argv[0]).resolve().parent

    p = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument(
        '--virtual-data-filename', default='.btrfs-ublk-virtual-data'
    )
    p.add_argument('--virtual-data-size', default='4E', type=suffixed_byte_size)
    p.add_argument('--rw-fs-size', default='1G', type=suffixed_byte_size)
    p.add_argument('--btrfs-ublk-dir', default=my_dir)

    cli.parser = p
    yield cli  # Allow calling CLI to add its own args to the parser.

    cli.args = p.parse_args(argv[1:])

import decimal
import inspect
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


def strip_suffix(s: str, suffix: str) -> str:
    if not suffix:
        return s  # Below, `s[:-0] != s`
    assert s.endswith(suffix)
    return s[: -len(suffix)]


def strip_nl(s: str) -> str:
    "Strip the last newline to match bash's `$()` behavior. Sigh."
    return strip_suffix(s, '\n')


def get_logger():
    return logging.getLogger(
        strip_suffix(os.path.basename(inspect.stack()[1].filename), '.py')
    )


def init_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='\x1b[1m%(asctime)s %(levelname)s:\x1b[0m %(message)s',
    )


class SZ:
    K = 2**10
    M = 2**20
    G = 2**30
    T = 2**40
    P = 2**50
    E = 2**60


def assert_file_smaller_than(path: Path, sz: int):
    num_bytes = 512 * path.stat().st_blocks
    assert num_bytes < 10 * SZ.M, f'{path} too big: {num_bytes}'


_SUFFIX_TO_BYTE_FACTOR = {
    '': 1,
    'k': SZ.K,
    'kib': SZ.K,
    'm': SZ.M,
    'mib': SZ.M,
    'g': SZ.G,
    'gib': SZ.G,
    't': SZ.T,
    'tib': SZ.T,
    'p': SZ.P,
    'pib': SZ.P,
    'e': SZ.E,
    'eib': SZ.E,
}

# No exponential notation, or NaN, etc, because this is just for byte sizes.
_FLOAT_PLUS_SUFFIX_RE = re.compile(r'(-?[0-9]+(\.[0-9]*)?)([^0-9]?.*)')


def suffixed_byte_size(s: str) -> int:
    "Convert '4EiB' to 2**62. Case-insensitive because I'm a rebel"
    num, _, suffix = _FLOAT_PLUS_SUFFIX_RE.match(s).groups()
    factor = _SUFFIX_TO_BYTE_FACTOR.get(suffix.lower())
    assert factor, f'Unknown byte size suffix in: {s}'
    # Regular 64-bit `float` is wrong (and can thus mess up alignment)
    # for exabyte-sized values.
    with decimal.localcontext() as ctx:
        ctx.prec = len(str(2**128)) + 10  # 128 bits ought to be enough
        return int((decimal.Decimal(num) * factor).to_integral())


def test_suffixed_byte_size():
    assert suffixed_byte_size('1KiB') == SZ.K
    assert suffixed_byte_size('1m') == SZ.M
    assert suffixed_byte_size('1GiB') == SZ.G
    assert suffixed_byte_size('1T') == SZ.T
    assert suffixed_byte_size('3P') == 3 * SZ.P
    assert suffixed_byte_size('4E') == 4 * SZ.E
    assert suffixed_byte_size('123E') == 123 * SZ.E
    # float gets this wrong: int(0.1070816951805893162 * 2 ** 60)
    assert suffixed_byte_size('0.1070816951805893162E') == 123456789123456789


@contextmanager
def temp_dir(**kwargs) -> Path:
    with tempfile.TemporaryDirectory(**kwargs) as td:
        yield Path(td)

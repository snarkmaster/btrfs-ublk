#!/usr/bin/env python3
'''
Prints a shell script that repeatedly runs `benchmark.py` with a matrix of
options.  Usage:

    ./benchmark_matrix.py > bench-script
    sudo ./isolate.sh bash -f bench-script >> bench-log.json

This follows an <shuffled batch 1> <shuffled batch 2> ...  repeat pattern
with many short-runtime jobs.  The aim here is to get some sense of variance
across runs, while time-averaging away effects on individual runs from CPU
heat management, OS background activities, etc.  In `mmap` mode, keeping
the runs short also reduces page cache effects on `SMALL_CLONE` runs.

TODO: Maybe the matrix to cover non-direct IO (drop caches, use a single
reader, drop `--norandommap` and `--loop`).

TODO: For each unique set of `fio` options it'd be cool to also
automatically benchmark comparatives as in README.md#Benchmarks.
'''
import argparse
import json
import random
import shlex
import sys
from pathlib import Path

# My CPU has 12 HT cores.  In my ad-hoc testing, even at 12 fio jobs, we
# barely pushed 2 cores worth of `ublk` load.  For 1 job, adding an extra
# queue did seem to slightly help with async IO.  On the other hand, 12
# queues seemed to add slight overhead.
JOBS__NUM_QUEUES = [
    (1, 2),
    (2, 2),
    (6, 3),
    (12, 3),
]

SMALL_CLONE = '2G'  # Expect page-cache effects with `mmap`
BIG_CLONE = '200G'  # Not much page-cache, the benchmark host had 32GiB RAM

SEED__VIRT_SZ__CLONE_SZ = (
    # Smaller "virtual data" is silly, larger means very slow formatting due
    # to my unoptimized implementation.
    ('fallocate', '275G', SMALL_CLONE),
    ('fallocate', '275G', BIG_CLONE),
    # Overlap on a "virtual data" size with `fallocate` to get
    # apples-to-apples.  Also try a huge 4E address space since that makes
    # blob allocation qualitatively easier.
    ('mega-extent', '275G', SMALL_CLONE),
    ('mega-extent', '275G', BIG_CLONE),
    ('mega-extent', '4E', SMALL_CLONE),
    ('mega-extent', '4E', BIG_CLONE),
)


def gen_opts_matrix():
    for (seed, virt_size, clone_size) in SEED__VIRT_SZ__CLONE_SZ:
        # Omitting `sync` since it's just a tad worse than `psync` due to
        # the extra syscall (about 10% in my quick-and-dirty check).
        for engine, depth in (
            ('io_uring', 4),
            ('io_uring', 16),
            ('mmap', 8),  # iodepth not used
            ('psync', 8),  # iodepth not used
        ):
            for jobs, ublk_queues in JOBS__NUM_QUEUES:
                yield {
                    'btrfs.virtual_data_size': virt_size,
                    'btrfs.total_clone_size': clone_size,
                    'btrfs.seed': seed,
                    # Short so we can get many runs, but not too short since
                    # the `fallocate` mode has ~4s formatting overhead @
                    # 275G.  Note that higher values increase page-cache
                    # effects for `mmap` + SMALL_CLONE, but the analysis in
                    # `benchmark_matrix_summarize.py` splits these out.
                    'fio.runtime': 10,
                    'fio.direct': 1,
                    'fio.depth': depth,
                    'fio.engine': engine,
                    'fio.jobs': jobs,
                    'ublk.num_queues': ublk_queues,
                }


def main():
    my_dir = Path(sys.argv[0]).resolve().parent
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--repeat', default=30)
    args = p.parse_args()

    opts_matrix = list(gen_opts_matrix())
    for _ in range(args.repeat):
        random.shuffle(opts_matrix)
        for opts in opts_matrix:
            cmd = [
                str(my_dir / 'benchmark.py'),
                '--json-opts',
                json.dumps(opts),
            ]
            print(' '.join(shlex.quote(c) for c in cmd))


if __name__ == '__main__':
    main()

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
things short also reduces "mostly page cache" measurements on smaller file
sizes like 5G.

TODO: Expand the matrix to cover non-direct IO (drop caches, use a single
reader, drop `--norandommap`), and to vary IO depths in async modes (in my
limited tests, IO depth > 4 makes little difference).

TODO: For each unique set of `fio` options it'd be cool to also
automatically benchmark comparatives as in README.md#Benchmarks.
'''
import argparse
import json
import random
import shlex
import sys
from pathlib import Path


def gen_opts_matrix():
    for (seed, virt_size, clone_size) in (
        # Smaller is silly, larger means very slow formatting due to
        # my unoptimized implementation.
        ('fallocate', '275G', '5G'),
        ('fallocate', '275G', '200G'),
        # Checking a few orders of magnitude.  Overlap on at least one size
        # with `fallocate` to get apples-to-apples.
        ('mega-extent', '275G', '5G'),
        ('mega-extent', '500T', '5G'),
        ('mega-extent', '4E', '5G'),
        ('mega-extent', '4E', '200G'),
    ):
        # Omitting `sync` since it's just a tad worse than `psync` due to
        # the extra syscall (about 10% in my quick-and-dirty check).
        for engine in ('io_uring', 'mmap', 'psync'):
            for jobs in (1, 2, 6, 12):  # My CPU has 12 HT cores.
                # In my ad-hoc testing, even at 12 fio jobs, we barely
                # pushed 2 cores worth of `ublk` load.  For 1 job, adding an
                # extra queue did seem to slightly help with async IO.  On
                # the other hand, 12 queues seemed to add slight overhead.
                ublk_queues = jobs + 1
                yield {
                    'btrfs.virtual_data_size': virt_size,
                    'btrfs.total_clone_size': clone_size,
                    'btrfs.seed': seed,
                    # Short so we can get many runs, but not too short since the
                    # `fallocate` mode has ~4s formatting overhead @ 275G.
                    'fio.runtime': 10,
                    'fio.direct': 1,
                    'fio.depth': 8,
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

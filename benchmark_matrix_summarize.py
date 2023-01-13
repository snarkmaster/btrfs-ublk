#!/usr/bin/env python3
'''
Prints a human-friendly summary of the result of running the tests from
`benchmark_matrix.py`.  This aims to answer some questions discussed in
README.md#Benchmarks.

The sample results were generated thus:

./benchmark_matrix_summarize.py \
    < benchmark_matrix_results_raw.json &> benchmark_matrix_results.txt
'''
import json
import sys
from collections import defaultdict

import numpy

from benchmark import OPTION_KEYS
from benchmark_matrix import JOBS__NUM_QUEUES, SEED__VIRT_SZ__CLONE_SZ
from src.common import get_logger, init_logging
from src.hashable_dict import hashable_dict

log = get_logger()


def set_item_once(d, k, v):
    if k in d:
        raise KeyError(k)
    d[k] = v


def compare_cfgs(cfg_to_iops, keys, val_tuples, group_by):
    num_other_val = 0
    num_incomplete = 0
    grp_to_val_to_cfg_to_iops = defaultdict(
        lambda: {vs: {} for vs in val_tuples}
    )
    for cfg, iops in cfg_to_iops.items():
        if not all(
            hashable_dict({**cfg, **dict(zip(keys, vs))}) in cfg_to_iops
            for vs in val_tuples
        ):
            num_incomplete += 1
            continue

        c_to_i = grp_to_val_to_cfg_to_iops[
            hashable_dict({k: cfg[k] for k in group_by})
        ].get(tuple(cfg[k] for k in keys))
        if c_to_i is None:
            num_other_val += 1
            continue

        set_item_once(
            c_to_i,
            hashable_dict({k: v for k, v in cfg.items() if k not in keys}),
            iops,
        )
    summary = '\n'.join(
        f'    {[grp[k] for k in group_by]}:\n'
        + '\n'.join(
            '      {}:\t{:,.0f}'.format(
                list(v),
                # Pick the best configuration.  Within a configuration, pick
                # by run percentile -- not too low since the middle is
                # pretty sensitive to OS jitter and moment-to-moment thermal
                # throttling, not too high to avoid outliers.
                max(numpy.percentile(iops, 75) for iops in c_to_i.values()),
            )
            for v, c_to_i in v_to_c_to_i.items()
        )
        for grp, v_to_c_to_i in sorted(
            grp_to_val_to_cfg_to_iops.items(),
            key=lambda k_v: [k_v[0][k] for k in group_by],
        )
    )
    log.info(
        f'''\
Comparing selected values for {keys}:
  - Best-config IOPS by {group_by}:
{summary}
  - Ignored {num_other_val} configs due to values not requested in comparison
  - Ignored {num_incomplete} configs where not all variants were tested
'''
    )


def main():
    cfg_to_iops = defaultdict(list)
    for line in sys.stdin:
        d = json.loads(line)
        d.pop("job_iops")  # don't care, can recompute
        total_iops = d.pop("total_iops")
        assert set(d.keys()) == OPTION_KEYS, d
        cfg_to_iops[hashable_dict(d)].append(total_iops)
    num_rows = sum(len(v) for v in cfg_to_iops.values())
    log.info(f'Tested {len(cfg_to_iops)} cfgs, {num_rows} times\n')

    # Compare these jointly to see if there's any relationship.
    compare_cfgs(
        cfg_to_iops,
        ['btrfs.seed', 'btrfs.virtual_data_size', 'btrfs.total_clone_size'],
        SEED__VIRT_SZ__CLONE_SZ,
        # It's important to break `mmap` out since
        group_by=['fio.engine'],
    )

    # These are subsets of the first comparison, but it's clearer this way.
    compare_cfgs(
        cfg_to_iops,
        ['btrfs.virtual_data_size'],
        sorted({(vsz,) for _, vsz, _ in SEED__VIRT_SZ__CLONE_SZ}),
        group_by=['fio.engine'],
    )
    compare_cfgs(
        cfg_to_iops,
        ['btrfs.seed'],
        sorted({(sd,) for sd, _, _ in SEED__VIRT_SZ__CLONE_SZ}),
        group_by=['fio.engine'],
    )
    compare_cfgs(
        cfg_to_iops,
        ['btrfs.total_clone_size'],
        sorted({(cs,) for _, _, cs in SEED__VIRT_SZ__CLONE_SZ}),
        group_by=['fio.engine'],
    )

    # See if IO depth matters
    compare_cfgs(
        cfg_to_iops,
        ['fio.depth'],
        [(4,), (16,)],
        group_by=['fio.engine', 'fio.jobs'],
    )

    # Speedup from concurrency. Breaking down by clone size is interesting
    # for `mmap` (due to page cache effects) but not for the others.
    compare_cfgs(
        cfg_to_iops,
        ['fio.jobs', 'ublk.num_queues'],
        JOBS__NUM_QUEUES,
        group_by=['fio.engine', 'btrfs.total_clone_size'],
    )


if __name__ == '__main__':
    init_logging()
    main()

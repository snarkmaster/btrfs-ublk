# What is this?

This repo uses [`ublk`](https://docs.kernel.org/block/ublk.html) to
demonstrates the concept of "lazy data block materialization" for
[`btrfs`](https://btrfs.wiki.kernel.org/index.php/Main_Page).  A specially
formatted btrfs filesystem ends up being backed by a combination of "local
disk" and "lazy-materialized virtual block device" -- e.g.  network data or
other blocks that are computed on-the-fly.
  - All metadata is local.
  - Any new data writes are stored on a local block device. NB: Technically,
    nothing prevents you from using a non-physical block device for writes. 
    I don't demonstrate this here since it's not helpful for my application.
  - **Special sauce :** Data blocks can additionally be copy-on-write (CoW)
    cloned (reflinked) from a special file whose blocks are provided by a
    `ublk`. 

These `ublk` data blocks can be lazy-materialized, as long as they obey the
**immutability rule** -- one accessed, a block must always be readable with
the same data, as long as the filesystem exists -- even across host
restarts, or other changes.
 
Not all lazy data blocks have to be defined from the get-go.  In fact, in
typical usage, some out-of-band communication will tell the `ublk` driver to
map a certain range of blocks.  Reading unmapped blocks can be an error.

The preferred setup for these lazy blocks is to have a large (exabyte-scale)
address space, so that any new data can just be mapped to new offsets,
without ever needing to deal with deallocating old offsets (which would
violate immutability, causing complications with caches).

One application of this idea is lazily provisioning an immutable base layer
for container filesystems, while retaining the ability to write on top.  In
this setup, the cost of setting up the filesystem is O(size of metadata +
size of data to pre-fetch eagerly) -- this can be dramatially lower than the
cost of downloading the entire filesystem.


# Available demos & benchmarks

Each program has a top-of-file docblock explaining the details. 

  - [`demo-via-mega-extent.sh`](demo-via-mega-extent.sh) and
    [`demo-generic.py`](demo-generic.py): Demonstrates the basic data flow,
    with "lazy" blocks successfully being read from CoW-cloned files on a
    btrfs filesystem, after being written directly to the underlying seed
    device.  The Python variant shows two seed device formatting strategies:
      - a performant "mega-extent" approach via hacked-up `btrfs-progs`, and
      - the very kludgy `fallocate` + `btrfs_corrupt_block` hack, which also
        works, kind of.

  - [`bad-make-seed-via-fallocate.sh`](bad-make-seed-via-fallocate.sh):
    Initially, I tried making the special `btrfs` via stock `mkfs.btrfs`
    plus `fallocate`, but that turned out to be a bad idea. That said,
    it does have a working implementation as [`temp_fallocate_seed_device`](
    https://github.com/snarkmaster/btrfs-ublk/blob/main/src/btrfs_ublk.py#L155),
    and a [corresponding benchmark](
    https://github.com/snarkmaster/btrfs-ublk/blob/main/benchmark_matrix.py#L36).

  - [`benchmark.py`](benchmark.py), [`benchmark_matrix.py`](
    benchmark_matrix.py), [`benchmark_matrix_summarize.py`](
    benchmark_matrix_summarize.py): Exercises `btrfs-ublk` with 4KiB random
    reads (the type of IO most sensitive to "plumbing overhead") in a
    variety of settings, and produces a human-readable summary.  See the
    "Benchmarks" section.


# Development scenarios

You have three choices for where to build & run the code, best to worst:

  - Build and run on the host. Easiest if you `uname -a` shows a kernel
    version >= 6.0.  Carries the theoretical risk that the hacked-up btrfs
    seed device will trigger some in-kernel asserts, but I haven't seen any.

  - Build and run in the VM.  Safest.  Requires provisioning a VM with build
    tooling, but overall much easier than dealing with statically compiled
    binaries.

  - Build on the host, copy static binaries and/or `.btrfs` files into
    the VM.

You will want to apply subsequent sections to the environment is doing the
build, and/or the execution.  But, before you get started with a VM, first
review "Tips on working in a VM".


# Isn't it fragile to provide logical files as physical blocks?

In most settings, it would be.  Specifically, `btrfs` is a CoW filesystem,
so for any writable filesystem it is permitted to replace any logical
content with pointers to new physical blocks with the same data.  There is
no **general** guarantee that the mapping from logical to physical bloks
remains stable or continuous.

However, we use a very special setup that *is* safe. 
 - The "virtual data" file is on a seed device. Seed devices are intended
   for read-only media, and thus will not change after `btrfstune -S 1`.
 - Before putting the seed device into use, we can -- [and do](
   https://github.com/snarkmaster/btrfs-ublk/blob/main/src/physical_map.py#L101)
   -- assert (via `btrfs_map_physical`) that the logical<->physical mapping
   is as expected.
 - In-kernel `btrfs` is of course required to maintain format compatibility
   with older filesystems, so a once-valid, immutable seed device should
   remain valid forever. 

There is a further wrinkle, which is that normally, btrfs checksums every
block, and stores the checksums as part of filesystem metadata.  Our virtual
blocks are not known in advance, and it's not reasonable to build a block
device that tries to hallucinate the right checksum metadata blocks as lazy
blocks get mapped.  So instead, the "virtual data" inode is marked
`nodatasum`, which implies certain limitations on how it can be used.

TODO: Link to discussion of the limitations, and what to do about them.


# Build requirements

  - `git clone` this repo.
  - Get a Linux kernel that includes [`ublk_drv` (normally >= 6.0)](
    https://docs.kernel.org/block/ublk.html) and `btrfs`.
  - Install various build tooling, per "initial setup" below. For VM-based 
    development, you'll also need `qemu`.
  - The dependency list in this README is incomplete, you will want to
    follow docs from the dependencies. PRs are welcome.


# OS-speficic initial setup

## Fedora

```
dnf install -y automake autoconf libtool e2fsprogs-devel libzstd-devel black \
  libudev-devel python3.10-devel gettext-devel python3-pytest python3-flake8 \
  python3-isort ShellCheck
```

If you want to build statically linked binaries for VMs, also run:

```
dnf install -y libzstd-static e2fsprogs-static zlib-static glibc-static
```

# Building & running

The first time, you'll want to run `./build.sh --full`, since e.g.
`ubdsrv` relies on `liburing` being installed. 

Once the first build succeeds, `./build.sh --fast` will skip the autoconf &
automake steps, and will avoid reinstalling `liburing`.

Now, you are ready to run individual demos.  If you run a demo as an
unprivileged user, it should tell you how to properly launch it.  For
example, a common setup is `sudo isolate.sh demo.sh` -- this avoids leaking
mounts and processes (caveat: this runs `bash` as PID 1, in a PID namespace,
if this causes you problems, please send a patch).  

Keep in mind that if a demo crashes or is interrupted, it might still leak
some resources.  Most notably, loopback devices (`losetup -l`) or ublk
devices (`sudo ./ubdsrv/ublk list`), are not automatically reaped by the
above.


# Contributing

First off, thank you! The best practices are:

```
./apply-lint.sh || echo "Please make your pull request lint-clean!"
pytest
```

Also, if you add new functionality, please do add unit tests.

Keep in mind that this is a "demo" project at this point, so it may take a
while for your pull request to get triaged.


# Benchmarks

Our goal in benchmarking is to find the "speed of light" for the
`btrfs-ublk` plumbing.  I.e. we're interested how fast we can pass in-RAM
pages to VFS clients, and not in **how** the implementation of how `ublk`
comes in possession of these pages.

The benchmark's dataflow is: (ublk server `memset()`) -> (kernel ublk
API) -> (ublk seed device) -> (btrfs mount seed & RW loop) -> VFS -> client.

If we were to operate with large IOs, the cost of plumbing would amortize
away, and we would see performance bounded by (memory bandwidth / number of
copies) -- not likely to be a bottleneck in real applications.

Small IOs put much more pressure on the plumbing.  Since a potential
application for `btrfs-ublk` is container filesystems, a sensible benchmark
is **4KiB random reads**, which is roughly what happens when a large binary
(or interpreted program) is being started.

Unless otherwise mentioned, all benchmarks are on a **i7-9750H laptop**
(Coffee Lake 2.6Ghz base, 4.5Ghz max; 2x 16GiB DDR4) running Fedora 37 with
a 6.0.18 kernel.  The Gnome "power management" setting was set to "Balanced"
for the benchmark.  Since the laptop has 12 hyperthreaded cores, we use 12
as the max reader concurrency in all the tests below.  As an estimate of RAM
bandwidth, this outputs around 20GiB/s.

```
dd if=/dev/zero of=/dev/null bs=1M count=100000
```

TODO: Add a `ublk` mode where virtual reads are backed by `O_DIRECT` reads
from local SSD to see how much worse things get.


## `btrfs-ublk` results

Here is a simple benchmark of `btrfs-ublk` serving data generated by a
`memset` in a modified `tgt_loop.cpp` server from `ubdsrv`.  The `fio`
settings are intended to be apples-to-apples with all the other "laptop"
comparisons below -- in particular, `engine=io_uring` + `direct=1` avoids
the confounding effects of page-cache.  Performance under sustained load
is proportionally lower due to thermal throttling.

I made note of the number of cores used in `top` while the benchmark was
running, since this is an important differentiator from FUSE.  For example
for 12-reader `psync`, CPU usage is spread across `ublk` (1.5 cores), 12
`fio` processes (0.35 core each), and 8 `kworker/uNUM:0+btrfs-endio` kernel
threads (0.2 core each).

 - 1 reader `io_uring`: 215k IOPS -- 1.8 cores used
 - 12 readers `io_uring`: 680k IOPS -- 11.5 cores used
 - 1 reader `psync`: 77k IOPS -- 0.8 cores used
 - 12 readers `psync`: 345k IOPS -- 7.3 cores used

```
sudo ./isolate.sh ./benchmark.py --json-opts '{
    "btrfs.seed": "mega-extent",
    "btrfs.total_clone_size": "5G",
    "btrfs.virtual_data_size": "275G",
    "fio.depth": 64,
    "fio.direct": 1,
    "fio.engine": "io_uring",
    "fio.jobs": 1,
    "fio.runtime": 20,
    "ublk.num_queues": 2
}'
```


## What affects `btrfs-ublk` performance?

This section summarizes a 10-hour benchmak with 2,880 10-second runs,
covering 96 different configurations.  For the underlying data, refer to
[`benchmark_matrix_results.txt`]( benchmark_matrix_results.txt), generated
via [`benchmark_matrix_summarize.py`]( benchmark_matrix_summarize.py) from
[`benchmark_matrix_results_raw.json`]( benchmark_matrix_results_raw.json).

The IOPS differ slightly from prior section.  First off, some settings
differ (e.g.  IO depth for `io_uring`).  Second, the "matrix" runs were run
back-to-back, resulting in significant thermal throttling.

It looks like "virtual data file" size (275G vs 4E) has no effect on any of
the read modalities -- this is expected.

Comparing seed device formatting strategies -- "fallocate" with smaller
extents vs "mega-extent" with one giant extent, it appears that this has
either no effect on performance, or a very minor one.  A priori, I would
have expected smaller extents to be faster, since that reduces extent lock
contention in the kernel, but if there's any such effect, it is negligible.

TODO: When productionizing, it would be worthwhile to also benchmark
concurrently cloning a million files from "virtual data", to see if lock
contention on the mega-extent causes any slowdown while cloning.

What affects performance the most is the number of concurrent readers (fio
`numjobs`), and the read modality (fio `ioengine`):

  - `psync` issues 4KiB `pread()`s at random offsets, using `O_DIRECT` to
    avoid page-cache effects.
      - Best IOPS for (1, 2, 6, 12) readers: (78k, 126k, 246k, 311k)

  - `io_uring` is a high-throughput IO mechanism for Linux, designed for
    high-performance clients that can accept API complexity in exchange for
    speed.  It is asynchronous and queue-based, so fio `iodepth` affects its
    performance, but only at 6+ jobs, when the system is loaded enough to
    see contention -- then, allowing depth of 16 adds about 5-10% throughput
    compared to depth 4.  We only run this test with direct IO, since Linux
    buffered IO is not asynchronous, and we do not want to be timing page
    cache.
      - Best IOPS for (1, 2, 6, 12) readers, depth 4:
        (224k, 361k, 566k, 577k)
      - Best IOPS for (1, 2, 6, 12) readers, depth 16:
        (224k, 347k, 601k, 643k)

  - `mmap` is how ELF binaries are loaded on Linux, so it's a proxy
    for binary startup speed.  It always goes through page-cache, so
    repeated reads from a small cloned file (2G) appear very fast.  In
    contrast, random reads from a 200G file on a test machine with 32GiB
    RAM, and reporting only 8GiB of buffers / cache during the benchmark,
    would encounter > 95% faults -- this approximates "cold startup".
      - 2G file IOPS for (1, 2, 6, 12) readers: (151k, 714k, 2514k, 3022k)
      - 200G file IOPS for (1, 2, 6, 12) readers: (56k, 95k, 176k, 205k)


### What is the bottleneck for `btrfs-ublk`?

Our components are: `ublk` + block IO, `btrfs`, VFS, and `io_uring`.  Which
subsystem causes the slowdown -- or is it a combination of the above?
 - `io_uring` benchmarks show over 1.5M QPS-per-core on stock hardware.
 - `ublk` [demonstrated 1.2M IOPS](
    https://github.com/ming1/ubdsrv/blob/master/doc/ublk_intro.pdf).

Looking at the comparisons below, it's pretty clear that `btrfs` is the slowest
piece of the puzzle. And, the performance of `btrfs-ublk` is comparable to
`btrfs` on ramdisk -- at most 27% slower on `psync`, and on-par/faster with
`io_uring`.


## Comparison: FUSE

The [Direct-FUSE paper](https://www.osti.gov/servlets/purl/1458703) has the
fastest published benchmark I've found showing a "speed-of-light" for FUSE. 
Measuring 4KiB random reads via FUSE on top of tmpfs, they [report ~117MiB/s
or 29k IOPS](
https://github.com/LLNL/direct-fuse/blob/master/results/rand_read.dat). 
Unfortunately, the paper does not make it not clear whether this is "per
core" or "maximum throughput achievable on the 10-core Xeon machine under
test".  Other benchmarks from my search were even less convincing -- one
reported as low as [25 MiB/s 4KiB random reads (6400 IOPS) with a single
reader](https://lwn.net/Articles/843873/).

We benchmarked EdenFS (FUSE) on a **server**, whose Skylake CPU had 20
physical cores (2Ghz regular, 3.7Ghz turbo), and 256GiB of DDR4.  RAM
bandwidth estimated at 17GiB/sec per `dd` as above.  The test was repeated
so as to effectively be serving from Eden's RAM cache, so this test measures
the overhead of a reasonably well-optimized, production FUSE filesystem.

As with `btrfs-ublk`, I took note of the total number of cores used by `fio`
and `edenfs` in `top`, during the middle of the benchmark.

The results:
  - 1 reader: 68k IOPS -- 2.5 cores
  - 18 readers: 195k IOPS -- 7 cores

```
cd ~/eden-repo
dd iflag=fullblock if=/dev/urandom of=5G-rand bs=1G count=5
fio --name=rand-4k --bs=4k --ioengine=io_uring --rw=randread --runtime=20 \
  --iodepth=16 --filename=5G-rand --norandommap --loop=1000 --direct=1 \
  --numjobs=18 --numa_cpu_nodes=0-0
```

Doing `perf record` against the running Eden FUSE server, it looks plausible
that we're bottlenecked on some in-kernel contention.  Most of the perf
trace is kernel code, and locks.

So, while FUSE isn't as slow as other literature suggests, you need 7 cores
worth of CPU to reach half the throughput of a consumer SSD.  Whereas
`btrfs-ublk` reaches these IOPS with a single reader and < 2 cores.

Notes:
  - `--numa_cpu_nodes=0-0` is applied since this was a 2-socket system, and
    IOPS was about 10% lower if `fio` jobs were not CPU-pinned.
  - The reason for using a different benchmark host was that compiling Eden
    for the laptop OS would cost more effort.


## Comparison: local btrfs SSD

This is not directly pertinent to the `btrfs-ublk` or FUSE benchmarks above,
but it shows the performance of a consumer SSD.

The underlying SSD is a decent, if somewhat older, Samsung 970 EVO Plus M.2.
  - 1 reader, VFS + LUKS encryption: 102k IOPS
  - 12 readers, VFS + LUKS encryption: 315k IOPS
  - 12 readers: raw device: 410k IOPS

```
dd if=/dev/urandom of=5G-rand bs=1G count=5
fio --name=randread-4k --bs=4k --ioengine=io_uring --rw=randread \
  --iodepth=64 --norandommap --loop=1000000000 --runtime=20 --filename=5G-rand \
  --direct=1 --numjobs=1
```


## Comparison: btrfs on `brd` ramdisk

This is meant as the "speed of light for btrfs + VFS" -- although, per
`tmpfs` tests below, it's really mostly `btrfs`.
  - 1 `io_uring` reader: 175k IOPS
  - 12 `io_uring` readers: 680k IOPS
  - 1 `psync` reader: 107k IOPS
  - 12 `psync` readers: 420k IOPS

```
unshare -m
modprobe brd rd_size=5242880 rd_nr=1  # 5GiB
mkfs.btrfs /dev/ram0
vol=$(mktemp -d)
mount /dev/ram0 "$vol"
dd if=/dev/urandom of="$vol"/5G-rand bs=1M count=5120
# 4814012416 bytes (4.8 GB, 4.5 GiB) copied, 13.9912 s, 344 MB/s
fio --name=randread-4k --bs=4k --ioengine=io_uring --rw=randread \
  --iodepth=64 --norandommap --loop=1000000000 --runtime=20 \
  --filename="$vol"/5G-rand --direct=1 --numjobs=1
umount "$vol"
rmmod brd
```

## Comparison: `tmpfs`

This is meant to benchmark the "speed of light for the VFS layer".  No
`--direct=1` since it's not supported on `tmpfs`.  Don't do `--loop=1
--norandommap --numjobs=12` to avoid exercising page-cache.
  - 1 reader: 554k IOPS

```
unshare -m
vol=$(mktemp -d)
mount -t tmpfs -o size=5G tmpfs "$vol"
dd if=/dev/urandom of="$vol"/5G-rand bs=1G count=5
echo 3 > /proc/sys/vm/drop_caches 
fio --name=randread-4k --bs=4k --ioengine=io_uring --rw=randread \
  --iodepth=64 --runtime=20 --filename="$vol"/5G-rand
```


## Comparison: `/dev/zero`

Intended as a "block IO speed-of-light" test, but I wouldn't overinterpret
it since `/dev/zero` isn't quite a real block device.  Single reader, no
`--direct=1` since `/dev/zero` doesn't support that.  Avoiding `io_uring`
since it's slower in this setting for some reason.
 - laptop, 1 reader: 1000k 4KiB IOPS
 - server, 1 reader: 1150k IOPS

```
fio --name=rand-4k --bs=4k --ioengine=psync --rw=randread --runtime=20 \
    --iodepth=16 --filename=/dev/zero --norandommap --size=5G --loop=1000
```


# Related solutions

This is neither the first, nor the last idea for providing lazy-fetched
filesystems.  However, it is somewhat different from the prior work known to
the author.

TODO: Write some comparative words about Nydus + EROFS + fscache, [incfs](
https://source.android.com/docs/core/architecture/kernel/incfs), DADI, virtiofs,
plan9 & [LISAFS](https://gvisor.dev/docs/user_guide/filesystem/), FUSE 
(including the as-yet-unmerged `FUSE_PASSTHROUGH` patches, OverlayFS, and [this
discussion](https://www.spinics.net/lists/linux-unionfs/msg08972.html)).


## Technologies not considered

If any rationale given is bad, please file an issue or a PR.

  * NFS, SMB, & other read-write network filesystem protocols.

    * These are quite complex to integrate and maintain, because they’re
      oriented primarily towards read-write workloads, including
      multi-writer support.  Diatribes against R/W network POSIX to read:
      [1](https://www.nextplatform.com/2017/09/11/whats-bad-posix-io/),
      [2](https://www.time-travellers.org/shane/papers/NFS_considered_harmful.html),
      [3](https://www.kernel.org/doc/ols/2006/ols2006v2-pages-59-72.pdf) —
      note that IIRC this genre dates back to the 1980s.  The root cause of
      the badness is that multi-writer network POSIX runs afoul of the CAP
      theorem.

    * Multi-writer support isn't really useful for cloud container
      filesystem, the bulk of such use-cases only wants lazy reads.

  * Other non-local block devices.  The fundamental reason they are omitted is
    that `ublk` is roughly as fast and simple as possible ([1.2M IOPS on a
    laptop VM](https://github.com/ming1/ubdsrv/blob/master/doc/ublk_intro.pdf)).
    So, for the present btrfs seed device hack, the various other block
    device interfaces offer no benefit.

    * [NBD](https://nbd.sourceforge.io/): `ublk` is a more modern version of
      the idea that essentially supersedes NBD — `ublk` is simpler and
      faster.

    * iSCSI: Another remote block solution that is even more messy to
      integrate than NBD.  Its main differentiator is that it’s more
      cross-platform than anything else on the list.  On the flip-side, in
      some informal experiments on Linux + iSCSI, it only achieved tolerable
      performance on fast, wired local network.


# Tips on working in a VM

These are **partial** notes on how to use @osandov's [`vm.py` script](
https://github.com/osandov/osandov-linux#vm-setup) to develop against a VM
instead of a bare-metal host.  When in doubt, refer to the upstream
documentation.

For now, I'm sticking to bare-metal development, so this section is stale.

  - Configure the VM location
```
cat <<'EOF' >> ~/.config/vmpy.conf 
[Paths]
# Top-level VM directory. Defaults to "~/vms".
VMs=~/osandov-vms/
EOF
```

  - Create a VM and install an OS into it. NB: You can omit `--mkfs-cmd`.

```
./bin/vm.py create test1
./bin/vm.py archinstall --mkfs-cmd mkfs.btrfs test1
```

  - Boot the VM, exposing one host directory as R/O, another as R/W.

```
out_vmdir=~/osandov-vms/test1/out
mkdir -p "$out_vmdir"
./bin/vm.py run test1 -- \
  -virtfs \
  local,path=/PATH/TO/btrfs-ublk,security_model=none,readonly=on,mount_tag=vmdir \
  -virtfs \
  local,path="$out_vmdir",security_model=none,readonly=off,mount_tag=out-vmdir
```

  - Do some initial setup, setting up the above `mount_tag`s to auto-mount.
    Tweak `TERM` to match your normal `printenv TERM`.

```
echo export TERM=xterm-256color >> ~/.bashrc
cat <<'EOF' | sudo tee /etc/systemd/system/vm-dir-mounter.service
[Unit]
Description=Mount vmdirs
DefaultDependencies=no
After=systemd-remount-fs.service
Before=local-fs-pre.target umount.target
Conflicts=umount.target
RefuseManualStop=true

[Install]
WantedBy=local-fs-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/mount -t 9p -o trans=virtio,ro,x-mount.mkdir vmdir /vmdir
ExecStart=/bin/mount -t 9p -o trans=virtio,rw,x-mount.mkdir out-vmdir /out-vmdir
ExecStopPost=/bin/sh -c 'if mountpoint -q /out-vmdir; then umount -l /out-vmdir; fi'
ExecStopPost=/bin/sh -c 'if mountpoint -q /vmdir; then umount -l /vmdir; fi'
EOF
sudo reboot  # Apply the new settings
```

  - Every time you boot the VM, set the window size as per the outer
    terminal's `stty -a | grep rows`. E.g.

```
stty columns 119 && stty rows 63
stty cols 155 && stty rows 85
```

  - If you want to do in-VM builds, you could `git clone` from `/vmdir` into
    `~/btrfs-ublk` and run `./build.sh` as usual.

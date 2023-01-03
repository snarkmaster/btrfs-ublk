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

  - [`check-read-unwritten-block.sh`](check-read-unwritten-block.sh):
    Demonstrates the basic data flow, with lazy blocks getting read as part
    of a local btrfs filesystem.

  - [`bad-make-seed-via-fallocate.sh`](bad-make-seed-via-fallocate.sh):
    Initially, I tried making the special `btrfs` via stock `mkfs.btrfs`
    plus `fallocate`, but that turned out to be a bad idea.

TODO: Currently adding more demos / benchmarks.

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

# Build requirements

  - `git clone` this repo.
  - Get a Linux kernel that includes [`ublk_drv` (normally >= 6.0)](
    https://docs.kernel.org/block/ublk.html) and `btrfs`.
  - Install various build tooling, per "initial setup" below. For VM-based 
    development, you'll also need `qemu`.
  - The dependency list in this README is incomplete, you will want to
    follow docs from the dependencies. PRs are welcome.

# OS-speficic initial setup

## Fedora 36

```
dnf install -y automake autoconf libtool e2fsprogs-devel libzstd-devel \
  libudev-devel python3.10-devel gettext-devel
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

# Benchmarks

These are on a i7-9750H laptop with Fedora 36.  Note, this is far from the
final word.  For example both `io_uring` publish benchmarks showing over
1.5M QPS on stock hardware, so this benchmark is probably not correct, but
at least it sets a baseline.

Baseline on `/dev/zero`.

```
fio --name=rand-4k --bs=4k --ioengine=io_uring --rw=randread --runtime=20 \
    --iodepth=16 --filename=/dev/zero --norandommap --size=2G --loop=50
  read: IOPS=734k, BW=2867MiB/s (3006MB/s)(56.0GiB/20000msec)
```

TODO: Add real benchmarks here.

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

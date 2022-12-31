#!/bin/bash
set -uex -o pipefail

#
# This script is an example of what not to do. Its approach has two issues:
# 
#  (a) This allocates a LOT of chunks and extents, and they are not all 
#      contiguous. Sure, one can get a pretty large contiguous range
#      by parsing `btrfs_map_physical`, but there's also the fact that
#      `fallocate -l 10P` seems to hit a perf bug in btrfs and doesn't
#      finish even after burning 1000 minutes of CPU.
#
#  (b) `fallocate` produces so-called "preallocated" extents, which never
#      actually read from the underlying physiscal disk, unless written first.
#      This is a data-confidentiality measure to allow "lazy deletes", but 
#      here we actually do want to get dirty "regular" extents. One
#      can patch up the extents using `btrfs-progs/btrfs-corrupt-lock` after
#      finding the inode # via `stat` and the extent #s of interest via
#      `btrfs_map_physical`, but it's ugly.
#        btrfs-corrupt-block -i 257 -x 1342177280 -f type --value 1 d.btrfs
#

seed=${1:?Arg 1 must be filename of seed device to create}
fa_size=${2:?Arg 2 must be the fallocate size}

truncate -s 4E "$seed"
mkfs.btrfs "$seed"
du -sh "$seed"
sudo seed="$seed" fa_size="$fa_size" unshare -m bash -c '
set -uex -o pipefail
td=$(mktemp -d)
mount -o nodatasum -t btrfs "$seed" "$td"
touch "$td"/virtual_data
time fallocate -l "$fa_size" "$td"/virtual_data
umount "$td"
rmdir "$td"
'
btrfstune -S 1 "$seed"
du -sh "$seed"

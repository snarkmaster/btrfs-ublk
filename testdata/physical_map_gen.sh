#!/bin/bash
set -uex -o pipefail

# Update real-world test data for `physical_map.py`.
# 
# Usage:
#   sudo ./isolate.sh ./tests/data/physical_map_gen.sh 75T |
#     zstd -19 -c > tests/data/physical_map_75T.tsv.zst
#
# The reason 75T is selected is that this adds an extra contig -- and thus
# extra coverage -- to the test-case, one that starts shortly after 73.56T. 
# On kernel 6.0.12, the structure of the file is:
#  - Almost all file/logical/physical extents are 256MiB, and
#    in strict correspondence.
#  - The first 2 logical & physical extents contain 5 file extents, because
#    our `helloworld` writes below had split each of them.
#  - There is a 256MiB logical & physical gap after row 1018 in the file.
#  - Another 32MiB logical & 64MiB physical gap exists around 73.56T, after
#    row 301307.
#
# This means the longest contig is 80607946211328 bytes long, at offsets: 
# file - 272461987840, logical 273834573824, physical - 274916704256.

falloc_sz=${1?argv[1] must be size for fallocate}

my_path=$(readlink -f "$0")
btrfs_ublk_dir=$(dirname "$my_path")/../

# Recommend a mount namespace so that the mount doesn't leak.
if ! capsh --has-p=CAP_SYS_ADMIN ; then
  echo "Please run this via \`sudo ./isolate.sh $0\`" >&2
  exit 1
fi

td=$(mktemp -d)

function cleanup() {
  mountpoint "$td/vol" && umount -l "$td/vol" || echo "vol/ not mounted"
  rm -rf "$td"
}

trap cleanup EXIT

cd "$td"

truncate -s 4E s.btrfs
mkfs.btrfs s.btrfs >&2  # spams stdout with log messages
mkdir vol
mount s.btrfs vol

fallocate -l "$falloc_sz" vol/data

# Split the first couple of file extents to exercise the behavior of
# "multiple file extents per logical extent".
echo helloworld | dd conv=notrunc of=vol/data seek=300000000 bs=1 count=10
echo helloworld | dd conv=notrunc of=vol/data bs=1 count=10

mount -o remount vol/  # Required to update `btrfs_map_physical` output.

# Unlike `btrfs_map_physical`, this only shows file & logical extents, but
# the nice condensed output is great for debugging / development.  This
# prints 512-byte blocks, not bytes.
xfs_io -r -c 'fiemap -v' vol/data >&2

# CAUTION: I `sed` the output to "regular" extents so this can pass
# `validate_virtual_data_physical_map`, but the real deal would use
# `btrfs_corrupt_block` to actually change the filesystem.
"$btrfs_ublk_dir"/osandov-linux/scripts/btrfs_map_physical vol/data |
    sed 's/prealloc/regular/'

#!/bin/bash
set -uex -o pipefail
# Please keep this `shellcheck`-clean, or rewrite it in a better language.

# Demonstrates the basic `btrfs-ublk` data flow:
#
#  - Sets up a scratch directory. If you wish to interact with it, add
#    `sleep 10000` in this script, then `nsenter -a -t <PID>`. 
#
#  - Formats a btrfs filesystem containing a special file with a single
#    regular 4EiB extent (backed by a sparse file, of course). 
#
#  - Set this special fs as a btrfs seed device (`s.btrfs`), which would
#    in real production be served via `ublk` -- and partly virtualized.
#
#  - Set up a mount at `vol/` combining that seed device with a second
#    blank device for local writes (`rw.btrfs`).
#
#  - Show that we can write to the physical area of `s.btrfs` that is 
#    intended for `ublk` to virtualize, then CoW reflink from the
#    corresponding file (`vol/.btrfs-ublk-virtual-data`), and read back
#    the written data.
#    
# Look for "SUCCESS" towards the end -- this demo completes in a couple
# hundred milliseconds, and writes ~10 MiB of data to local disk.  The
# exabyte-sized range is just a "hole" in a sparse files, intended as an
# address space for lazy blocks.

my_path=$(readlink -f "$0")
my_dir=$(dirname "$my_path")


function assert_eq() {
  if [[ "$1" != "$2" ]] ; then
    echo "FAILURE: $1 != $2: ${3:-}" >&2
    exit 1
  fi
}
# Self-test for `assert_eq`
assert_eq 0 0 equality_ok
(assert_eq 0 1 inequality_should_fail &> /dev/null) && ( 
  echo assert_eq is buggy
  exit 1
)


# Recommend a mount namespace so that the mount doesn't leak.
if ! capsh --has-p=CAP_SYS_ADMIN ; then
  echo "Please run this via \`sudo ./isolate.sh $0\`"
  exit 1
fi


# Switch into a scratch directory and configure cleanup
td=$(mktemp -d)

rw_loop=
function cleanup() {
  if [ -n "$rw_loop" ] ; then
    losetup -d "$rw_loop"
  fi
  rm -f "rw.btrfs"
  rm "$td"/s.btrfs
  rm "$td"/src/.btrfs-ublk-virtual-data
  mountpoint "$td/vol" && umount -l "$td/vol" || echo "vol/ not mounted" 
  rmdir "$td"/src "$td"/vol "$td" 
}

trap cleanup EXIT

cd "$td"


# Set up a btrfs seed fs with one file consisting of one 4 EiB extent.
mkdir src
truncate -s 4E src/.btrfs-ublk-virtual-data
truncate -s 5E s.btrfs
"$my_dir"/btrfs-progs/mkfs.btrfs -r src s.btrfs
btrfstune -S 1 s.btrfs
du -sh s.btrfs


# Mount it as a seed, with a separate R/W area to test cloning
mkdir vol/
mount s.btrfs vol/
truncate -s 1G rw.btrfs
rw_loop=$(losetup --show -f rw.btrfs)
btrfs device add "$rw_loop" vol/
losetup -d "$rw_loop"
# `nodatasum` is to clone from `.btrfs-ublk-virtual-data` since this huge,
# lazy-loaded area cannot have precomputed checksums.
mount -o remount,rw,nodatasum vol/


# Parse & verify the physical extent map of our mega-file
phys_map=$("$my_dir"/osandov-linux/scripts/btrfs_map_physical vol/.btrfs-ublk-virtual-data)
expect_header=$'FILE OFFSET\tFILE SIZE\tEXTENT OFFSET\tEXTENT TYPE\tLOGICAL SIZE\tLOGICAL OFFSET\tPHYSICAL SIZE\tDEVID\tPHYSICAL OFFSET'

if [[
  "$(echo "$phys_map" | head -n 1)" != "$expect_header"
  || "$(echo "$phys_map" | wc -l)" -ne 2
]] ; then
  echo "Unexpected form of physical map:"$'\n\n'"$phys_map"
  exit 1
fi

assert_eq 0 "$(echo "$phys_map" | tail -n 1 | cut -f 3)" \
  "A nonzero extent offset would break our 'physical' arithmetic"
assert_eq \
  "$(echo "$phys_map" | tail -n 1 | cut -f 2)" \
  "$(echo "$phys_map" | tail -n 1 | cut -f 5)" \
  "file size != logical size"
assert_eq \
  "$(echo "$phys_map" | tail -n 1 | cut -f 2)" \
  "$(echo "$phys_map" | tail -n 1 | cut -f 7)" \
  "file size != physical size"
assert_eq \
  "$(echo "$phys_map" | tail -n 1 | cut -f 6)" \
  "$(echo "$phys_map" | tail -n 1 | cut -f 9)" \
  "logical offset  != physical offset"

phys_offset=$(echo "$phys_map" | tail -n 1 | cut -f 9)
phys_size=$(echo "$phys_map" | tail -n 1 | cut -f 7)
offset_75pct=$(( (phys_size / 4) * 3 ))

# Write some data directly to the physical mega-extent
echo clownstart |
  sudo dd conv=notrunc bs=1 seek="$phys_offset" count=10 of=s.btrfs
echo clown75pct |
  sudo dd conv=notrunc bs=1 seek=$((phys_offset + offset_75pct)) count=10 of=s.btrfs
echo clown100pct |
  sudo dd conv=notrunc bs=1 seek=$((phys_offset + phys_size - 11)) count=11 of=s.btrfs


# Clone the areas we just wrote, use 40G chunks to make it clear we are
# doing CoW.
clone_sz=40960000000
touch vol/start vol/mid vol/end
xfs_io -c "reflink vol/.btrfs-ublk-virtual-data 0 0 $clone_sz" vol/start
assert_eq 0 $(( offset_75pct % 4096 )) "offset_75pct not 4096-aligned"
xfs_io -c "reflink vol/.btrfs-ublk-virtual-data $offset_75pct 0 $clone_sz" vol/mid
xfs_io -c "reflink vol/.btrfs-ublk-virtual-data $((phys_size - clone_sz)) 0 $clone_sz" vol/end


# Read back the above writes from the cloned logical files
assert_eq clownstart "$(dd bs=1 count=10 status=none if=vol/start)"
assert_eq clown75pct "$(dd bs=1 count=10 status=none if=vol/mid)"
assert_eq clown100pct "$(
  dd skip=$((clone_sz - 11)) bs=1 count=11 status=none if=vol/end
)"

# Smoke check: a reflink copy shouldn't have used much space.
rw_size=$(du -BM rw.btrfs | cut -f 1 -d M)
if [ "$rw_size" -ge 10 ] ; then
  echo "ERROR: rw.btrfs unexpectedly large at ${rw_size}M" >&2
  exit 1
fi

echo "SUCCESS: Wrote data to physical device, and read it via btrfs"

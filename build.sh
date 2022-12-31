#!/bin/bash
set -uex -o pipefail
# Please keep this `shellcheck`-clean, or rewrite it in a better language.

# Environment variables that affect the build, besides whatever `configure`
# and `make` look for anyway.
: "${ubdsrv_opts:=-O3 -g -Wall}"

my_path=$(readlink -f "$0")
my_dir=$(dirname "$my_path")

static_btrfs_progs=0
while [ $# -ne 0 ] ; do
    case "$1" in
    "--full")
        shift
        conf_and_install=1
        ;;
    "--fast")
        shift
        conf_and_install=0
        ;;
    "--static-btrfs-progs")
        shift
        static_btrfs_progs=1
        ;;
    esac
done

: "${conf_and_install:?Must pass --full (initial builds) or --fast (iteration)}"


### liburing ###

cd "$my_dir/liburing"
if [ "$conf_and_install" -eq 1 ] ; then
    ./configure
fi
make
if [ "$conf_and_install" -eq 1 ] ; then
    sudo make install
fi


### ubdsrv ###

cd "$my_dir/ubdsrv"
if [ "$conf_and_install" -eq 1 ] ; then
    autoreconf -i
    PKG_CONFIG_PATH="$my_dir/liburing" \
    ./configure \
        --enable-gcc-warnings \
        CFLAGS="-I$my_dir/liburing/src/include $ubdsrv_opts" \
        CXXFLAGS="-I$my_dir/liburing/src/include $ubdsrv_opts" \
        LDFLAGS="-L$my_dir/liburing/src"
fi
make -j"$(nproc)"


### util-linux ###

# This is ONLY used on Fedora 36 to allow building static `btrfs-progs`
# binaries for VMs.
cd "$my_dir/util-linux"

# The checkout in this tree is v2.38, which matches Fedora 36.  The
# assertions check that our headers match the static libs.  Obviously, it
# would be classier to just build against my custom `util-linux`, but that's
# far too much hassle.
UTIL_LINUX_RE="^Version: 2\.38\."
grep -q "$UTIL_LINUX_RE" /usr/lib64/pkgconfig/uuid.pc
grep -q "$UTIL_LINUX_RE" /usr/lib64/pkgconfig/blkid.pc
git log --format="%D" HEAD^..HEAD | grep -q 'tag: v2.38'

if [ "$conf_and_install" -eq 1 ] ; then
    ./autogen.sh
    ./configure
fi
if [ "$static_btrfs_progs" -eq 1 ] ; then
    make libuuid.la libblkid.la
fi


### btrfs-progs ###

cd "$my_dir/btrfs-progs"
if [ "$conf_and_install" -eq 1 ] ; then
    ./autogen.sh
    ./configure --disable-documentation --disable-lzo
fi
if [ "$static_btrfs_progs" -eq 1 ] ; then
    make mkfs.btrfs.static btrfs.static btrfstune.static \
        EXTRA_LDFLAGS=-L"$my_dir/util-linux/.libs"
else
    make mkfs.btrfs
fi


### osandov-linux ###

cd "$my_dir/osandov-linux/scripts"
make btrfs_map_physical

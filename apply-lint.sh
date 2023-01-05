#!/bin/bash
set -ue -o pipefail

my_path=$(readlink -f "$0")
my_dir=$(dirname "$my_path")
cd "$my_dir"

# Shellcheck `.sh` files, ignoring third-party submodules & tempfiles.
find_prune_args=( )
for ignore_path in \
    ./.git \
    ./.pytest_cache \
    ./btrfs-progs \
    ./liburing \
    ./osandov-linux \
    ./ubdsrv \
    ./util-linux \
    ; do
  find_prune_args+=( -path "$ignore_path" -prune -o )
done

set -x
find . "${find_prune_args[@]}" -name '*.sh' -print0 | xargs -0 shellcheck

# Auto-format and lint Python
isort .
black .
flake8 .

set +x
echo $'\033[0;32m\033[1m-- LINT CLEAN --\033[0m'

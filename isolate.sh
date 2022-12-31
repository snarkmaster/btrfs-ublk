#!/bin/bash
set -uex -o pipefail
# TODO: Maybe update this to run a real `init` like `systemd-stubinit`
# instead of having the target script be PID 1.
exec unshare --mount-proc -fpm "$@"

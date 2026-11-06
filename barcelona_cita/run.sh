#!/bin/sh
set -eu
umask 077
mkdir -p /data /tmp/cita-runtime
chmod 700 /data /tmp/cita-runtime
chown cita:cita /data /tmp/cita-runtime
# /data/options.json belongs to Supervisor; never recursively change it.
export XDG_RUNTIME_DIR=/tmp/cita-runtime
exec gosu cita python -m src.main

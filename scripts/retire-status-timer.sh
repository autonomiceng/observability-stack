#!/bin/sh
# Retire the version 1 status timer and its records (Status v2 upgrade step). Safe to rerun.
# Usage: retire-status-timer.sh [env-file], the env file bootstrap used (default: .env).
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
units=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
name=observability-status
# The records live under the installation's OB_STATE_DIR, as bootstrap saved it.
state=$(sed -n 's/^OB_STATE_DIR=//p' "${1:-$root/.env}" 2>/dev/null | tail -n 1)
state=${state#[\"\']}
state=${state%[\"\']}
state=${state:-./data}
case $state in /*) ;; *) state=$root/$state ;; esac
# Name only units whose files exist; systemctl fails on a missing one.
set --
for unit in "$name.timer" "$name.service"; do
  [ ! -e "$units/$unit" ] || set -- "$@" "$unit"
done
if [ "$#" -gt 0 ]; then
  systemctl --user disable --now "$@"
  for unit in "$@"; do
    rm -f -- "$units/$unit"
    echo "disabled and removed $unit"
  done
  systemctl --user daemon-reload
else
  echo "no $name units in $units"
fi
for file in "$state/status/bootstrap.json" "$state/status/observer.lock"; do
  if [ -e "$file" ]; then
    rm -f -- "$file"
    echo "removed $file"
  fi
done
if [ -d "$state/status" ] && rmdir -- "$state/status" 2>/dev/null; then
  echo "removed $state/status"
fi

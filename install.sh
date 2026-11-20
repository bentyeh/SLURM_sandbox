#!/bin/bash
# Install confine and its agent_slurm package read-only into PREFIX (default ~/bin).
# Usage: ./install.sh [PREFIX] [CONFIG_JSON]
#   CONFIG_JSON defaults to src/agent_slurm/config.json. Run it yourself: agents must not be able to change the
#   installed files, so PREFIX must lie outside every folder an agent can write.
set -euo pipefail
src=$(cd "$(dirname "$0")/src" && pwd)
prefix=${1:-$HOME/bin}
config=${2:-$src/agent_slurm/config.json}

/usr/bin/python3 -c 'import sys, socket; assert sys.version_info >= (3, 9) and hasattr(socket, "send_fds")' ||
    { echo "install.sh: /usr/bin/python3 must be 3.9 or newer" >&2; exit 1; }
bwrap=$(/usr/bin/python3 -c 'import json, os, sys; print(os.path.expanduser(json.load(open(sys.argv[1]))["bwrap"]))' "$config")
"$bwrap" --help | grep -q -- --disable-userns ||
    { echo "install.sh: $bwrap is too old (needs --disable-userns, bubblewrap >= 0.8)" >&2; exit 1; }

pkg=$prefix/agent_slurm
mkdir -p "$prefix"
[[ -d $pkg ]] && chmod -R u+w "$pkg"
install -d -m 0755 "$pkg"
install -m 0555 "$src/confine" "$prefix/confine"
install -m 0555 "$src/agent_slurm/stand-in" "$pkg/stand-in"
install -m 0444 "$src"/agent_slurm/*.py "$pkg/"
install -m 0444 "$config" "$pkg/config.json"
chmod 0555 "$pkg"
echo "Installed $prefix/confine with config:"
cat "$pkg/config.json"

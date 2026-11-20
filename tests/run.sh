#!/bin/bash
# Run the tests. Levels:
#   unit   pure Python; also runs inside Claude's sandbox
#   local  unit + bubblewrap and broker tests with a fake SLURM (needs to run outside Claude's sandbox)
#   slurm  tests that submit small real jobs
#   all    everything
# Usage: ./tests/run.sh LEVEL [pytest arguments]
set -euo pipefail
cd "$(dirname "$0")/.."
level=${1:?usage: ./tests/run.sh unit|local|slurm|all [pytest arguments]}
shift
case $level in
    unit) marks="not local and not slurm" ;;
    local) marks="not slurm" ;;
    slurm) marks="slurm" ;;
    all) marks="" ;;
    *) echo "unknown level: $level" >&2; exit 2 ;;
esac
base=/resnick/scratch/btyeh/playground/slurm_sandbox/pytest/$level
mkdir -p "$(dirname "$base")"
# The base installation's conda: in a script, `conda` on PATH can be a copy inside an activated environment.
exec "${CONDA_EXE:-conda}" run --no-capture-output -n snakemake9 python -m pytest -m "$marks" --basetemp="$base" "$@"

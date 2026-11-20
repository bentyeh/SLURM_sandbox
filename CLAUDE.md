# CLAUDE.md

Source of `confine` and its SLURM broker; see README.md for the design.

- Code in `src/` runs under `/usr/bin/python3` 3.9 (`-I`): standard library only, no 3.10+ syntax.
- Tests: `conda run -n snakemake9 python -m pytest -m "not local and not slurm"` runs inside the sandbox. Bubblewrap, broker and SLURM tests need `./tests/run.sh local|slurm` (exactly this form; it runs outside the sandbox after approval).
- Temporary files go to `tmp/` (`/resnick/scratch/btyeh/playground/slurm_sandbox`).
- Never install into `~/bin`; the user runs `./install.sh`.

# Confining agent-launched SLURM jobs and Snakemake pipelines

## Goal

Let Claude launch Snakemake pipelines that use the whole cluster (the SLURM executor, many jobs on many nodes) and one-off SLURM jobs. Every process those runs start, on any node, may write only to the same folders as Claude's own sandbox: the project, the folders listed in the project's Claude settings (for example its scratch folder), and the shared agent scratch folder `/resnick/scratch/btyeh/claude`. This must hold even if the agent deliberately tries to get around it.

## Why this needs more than wrapping the job script

Claude's sandbox blocks SLURM, so jobs have to be launched from outside it, and a SLURM job runs with all of your file permissions. Wrapping each job script in a confinement is not enough. On the compute node, every Snakemake job starts its real work through a second SLURM call (`srun …`), which SLURM starts outside any wrapper. And any code inside a confinement that can reach SLURM can submit a new, unconfined job.

SLURM checks who is asking with munge: a service on every node that hands any program running as you a signed ticket through the file `/run/munge/munge.socket.2`. `sbatch` is just one program that uses it. So stand-in `sbatch`/`srun` scripts inside the confinement only stop honest mistakes: the stand-in has to call the real command, so the real command (and munge) are reachable to every other program in the confinement too, for example a line in a job script, Python in a Snakefile, `libslurm` loaded with `ctypes`, or an `sbatch` built from source.

## How it works

1. **`confine COMMAND`** runs the command in a bubblewrap confinement (the tool Claude's own sandbox uses). The whole file system is read-only except the approved folders; the operating system enforces it (`Read-only file system`). Inside, `/run/munge` is hidden, so nothing in the confinement can talk to SLURM directly, whatever program it uses.
2. **The broker.** `confine` keeps a small helper running outside the confinement for as long as the command runs. Inside, `sbatch`, `srun`, `scancel`, `squeue`, `sacct`, `sinfo`, `sshare`, `sstat`, `scontrol` and `sacctmgr` are replaced by a stand-in that hands each call to the broker through a socket. The broker checks the call, rewrites it so that every new job and job step also starts under `confine`, runs the real command, and passes back its output and exit status.
3. **Every level gets its own confinement and broker.** A job's script runs under `confine` on the compute node, which starts that node's broker. When the job (or Snakemake) calls `sbatch` or `srun`, the same happens again, however deep the chain.

```
login node:    Claude ─ confine ─┬─ [inside] sbatch job.sh ──socket──> broker (outside): checks, submits "confine, then job.sh"
compute node:  SLURM starts ─ confine ─┬─ [inside] job.sh
                                        ├─ sbatch other.sh ──socket──> broker on this node ─> another confined job
                                        └─ srun my_tool    ──socket──> broker on this node ─> confined job step
```

Nothing has to keep running between jobs. The broker ends when its `confine` ends.

### What the broker checks

The broker never judges the job's code; the code is made harmless by running inside `confine`. It checks only what SLURM itself does outside the confinement:

- **Options:** only listed options are accepted, written out in full (SLURM also accepts abbreviations such as `--outp`, which is how simple checks are fooled). The broker passes SLURM only its own canonical `--name=value` form of what it understood. `#SBATCH` lines are read the way sbatch reads them and passed the same way; the job script SLURM receives is a two-line wrapper with no `#SBATCH` lines.
- **Refused:**
  - **sbatch:** `--get-user-env`, `--export` other than `ALL`/`NONE`, `--export-file`, `--nodefile`, `--uid`/`--gid`, containers, burst buffers, `--profile`, and heterogeneous jobs.
  - **srun:** `--prolog`/`--epilog`, `--task-prolog`/`--task-epilog`, `--output`/`--error`/`--input`, `--bcast`, `--send-libs`, `--multi-prog`, and `--x11`.
  - **scontrol** other than `show`, **sacctmgr** other than `list`/`show`, **`sacct --file`**, and **`salloc`, `sattach`, `sbcast`, `scrontab`, `strigger`**.
- **Log files:** SLURM's own output for a job goes to the broker's spool folder. The job's requested `--output`, `--error`, `--input` and `--open-mode` (with `%j`, `%x`, `%A`, `%a`, …) are opened inside the confinement on the compute node. A log path swapped for a symlink to your home folder then just fails. Messages from SLURM itself (for example a time-limit kill) are in the spool log, `~/.local/state/agent-slurm/<project>/jobs/<…>/slurm-<jobid>.out`.
- **Environment:** variables that make programs load or run code (`LD_*`, `BASH_ENV`, `ENV`, exported bash functions, `PYTHON*`, glibc's list such as `GCONV_PATH`) are removed before anything runs unconfined, and set again inside the confinement. The caller's `SLURM_CONF` and SLURM input variables (`SBATCH_*`, `SRUN_*`, and `SLURM_*` such as `SLURM_TASK_PROLOG`) are dropped; `srun` keeps the same `SLURM_*` variables Snakemake's jobstep plugin keeps.
- **`scancel`, own jobs only:** each job ID is looked up with `scontrol show job`; it is cancelled only if SLURM records its batch script in this project's spool folder, where only the broker writes. Your own jobs, and other projects' agent jobs, are refused; filters such as `-u` or `--name` are refused. `srun --jobid` follows the same rule.

### Which folders: the project's Claude settings

`confine` finds the project as the nearest folder above the working directory (never your home folder itself) that has `.claude/settings.json` or `.claude/settings.local.json`, and mirrors Claude's sandbox:

- **Writable:**
  - the project folder;
  - every `sandbox.filesystem.allowWrite` entry, from the managed, user (`~/.claude/settings.json`), project and local settings;
  - the installation's `shared_writable` folders (`/resnick/scratch/btyeh/claude`).
- **Read-only inside those:**
  - the paths Claude's sandbox always protects: `.git/hooks`, `.git/config`, `.claude`, `.vscode`, `.idea`, `.bashrc`, `.mcp.json`, `.gitmodules`, … Missing ones in the project folder are occupied by an empty, read-only placeholder file. The placeholder is removed when the last confinement of the project ends, on whichever node.
  - every `denyWrite` entry.
- **Hidden (empty):** `/run/munge`, `~/.ssh`, `~/.claude`, `~/.local/share/jupyter`, `~/.vscode-server`, `/run/user/<uid>`, and every `denyRead` entry.

Paths in settings: `//x` and `/x` are absolute, `~/x` is in your home folder, other paths are relative to the project folder. Patterns (`*`) are not supported.

`confine` refuses to start if a writable folder is `/`, contains your home folder, `~/.claude`, the installation or the spool, or lies inside one of them. The settings files are themselves read-only inside `confine` and in Claude's sandbox, so an agent can't widen its own policy. Jobs and steps carry a copy of the policy in the spool (`~/.local/state/agent-slurm`), which is read-only to agents everywhere.

`confine --print-policy` shows the policy for the current folder.

## Install (once)

```bash
./install.sh             # into ~/bin; or ./install.sh PREFIX [CONFIG_JSON]
```

This copies `confine` and `agent_slurm/` read-only into `~/bin`, with `src/agent_slurm/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `bwrap` | `~/miniconda3/envs/sandbox/bin/bwrap` | bubblewrap ≥ 0.8 (the system's 0.4.1 lacks `--disable-userns`) |
| `spool` | `~/.local/state/agent-slurm` | job wrappers, script copies, policy snapshots, SLURM's own logs |
| `shared_writable` | `/resnick/scratch/btyeh/claude` | writable in every project |
| `env_dir` | `/resnick/scratch/btyeh/claude/confine-env` | `TMPDIR`, `XDG_CACHE_HOME`, `CONDA_PKGS_DIRS` inside (home is read-only) |
| `hide` | see above | folders that appear empty inside |
| `socket_dir` | `/dev/shm` | broker sockets (private 0700 folders) |

Re-run `install.sh` after changing the code. The spool grows by a few KB per job; clean it now and then, for example `find ~/.local/state/agent-slurm -mindepth 3 -maxdepth 3 -mtime +30 -exec rm -rf {} +`.

## Use in a project

In the project's `.claude/settings.local.json` (you edit this; Claude can't):

```json
{
  "permissions": {
    "ask": ["Bash(/home/btyeh/bin/confine *)"],
    "deny": ["Bash(sbatch *)", "Bash(srun *)", "Bash(salloc *)", "Bash(scancel *)"]
  },
  "sandbox": {
    "filesystem": {
      "allowWrite": ["//resnick/scratch/btyeh/<project>", "//resnick/scratch/btyeh/claude"],
      "denyWrite": ["./bin"]
    },
    "excludedCommands": ["squeue *", "sinfo *", "sacct *", "/home/btyeh/bin/confine *"]
  }
}
```

- `confine` is the only way out of Claude's sandbox. Claude must call it by its full path, as the whole command; `excludedCommands` matches the command text, and every part of a compound command must match.
- The `ask` rule makes Claude ask before each launch. Approve only commands that start with `/home/btyeh/bin/confine`, with nothing before it (such as `FOO=1 …`).
- The `deny` rules make a plain `sbatch` fail with a clear message instead of a sandbox error.
- The Snakemake profile needs no change; both `--wrap` and `--slurm-pass-command-as-script` work.

Add to the project's AGENTS.md: agents launch jobs and pipelines only as `/home/btyeh/bin/confine …`, cancel only with `/home/btyeh/bin/confine scancel <ids>`, dry-run Snakemake first, and ask before expensive runs.

Example commands:

```bash
/home/btyeh/bin/confine sbatch tmp/claude/jobs/count_reads.sh
/home/btyeh/bin/confine conda run -n snakemake9 snakemake --workflow-profile workflow/profiles/slurm --no-hooks <targets>
/home/btyeh/bin/confine scancel 7313084
squeue -u btyeh
```

For a long pipeline, submit the Snakemake driver itself as a job (`confine sbatch driver.sh`); it then runs confined on a compute node and keeps running after the Claude session ends.

## Escape routes considered

| Route | How it is closed |
|---|---|
| Call the real `sbatch`/`srun`, or any other program that uses munge (`libslurm`, a self-built `sbatch`, `scrontab`, `strigger`) | munge is hidden inside; the real commands are not in the confinement |
| Abbreviated or clustered options (`--outp=~/.bashrc`, `-vo`), unchecked options, `#SBATCH` parsing differences | allowlist of full names; only the broker's canonical form reaches SLURM; the submitted script has no `#SBATCH` lines |
| `BASH_ENV`, `LD_PRELOAD`, exported functions, `PATH` tricks running code before `confine` on the compute node | variables removed before anything runs unconfined; `confine` runs as `python3 -I` with absolute paths |
| `SLURM_CONF` pointing at a config that loads plugins | the caller's value is never used |
| Log path swapped for a symlink between check and job start | SLURM writes logs only into the spool; the job's own logs are opened inside the confinement |
| `--task-prolog`, `--prolog`, `--get-user-env`, `--input` of a hidden file | refused |
| SSH keys, Jupyter or IDE tokens used to reach an unconfined shell | hidden; setuid helpers (hostbased SSH's `ssh-keysign`) get no privileges (`no_new_privs`) |
| `.git/config` (`core.fsmonitor`), `.git/hooks`, `.claude/settings*.json`, `.vscode` written to run code later | read-only inside |
| `/proc/<pid>/root`, `/proc/<pid>/fd/*`, `ptrace` of your other processes | blocked: confined processes are in their own user namespace |
| Nested namespaces to undo the rules | `--disable-userns` |
| Keystroke injection into the calling terminal | `--new-session` |

## Remaining limits

- **Reading and network:** confined code can read every file that isn't hidden and has unrestricted network access (jobs need it), so it could send data elsewhere. Claude's own sandbox restricts network; `confine` does not.
- **Your own runs:** code the agent changes in the project (Snakefile, scripts, conda environments in `.snakemake/conda`) runs unconfined when *you* run the pipeline yourself.
- **Signals:** confined code can send signals to your other processes (it can stop them, not escape through them).
- **Upgrades:** after a SLURM or Snakemake plugin upgrade, a new option may be refused. Submissions then fail with `agent-slurm: … unknown option`; add the option to `src/agent_slurm/options.py` and re-install. Run `./tests/run.sh unit` after a SLURM upgrade: it checks the option tables against the installed man pages and flags options SLURM no longer documents or whose argument changed.
- **Other user namespaces:** `confine` treats any user namespace as an existing confinement and runs the command directly, because bwrap can't start there. Inside a container that uses one (e.g. unprivileged Apptainer), the command would run unconfined; don't approve `confine` behind such a wrapper.
- **Nested repositories:** `.git/hooks` and `.git/config` are read-only only at the top of each writable folder. In a nested repository or submodule, a hook written by the agent runs when you next commit there.
- **`scancel` coverage:** jobs that `srun` creates outside an allocation can't be cancelled through `confine scancel` (stop the `srun` instead).
- **The approval prompt is the gate for `confine` itself.**
- Kernel bugs are out of scope.

## Tests

```bash
./tests/run.sh unit     # pure Python; also inside Claude's sandbox
./tests/run.sh local    # + bubblewrap and broker with a fake SLURM; outside Claude's sandbox
./tests/run.sh slurm    # small real jobs and a Snakemake run on the cluster
```

The unit tests include a check of the option tables against the installed man pages of `sbatch`, `srun`, `scancel`, `scontrol` and `sacctmgr`. The local tests reproduce each escape route above as an attack that must fail. The SLURM tests run one-off, nested and `srun` jobs, check `scancel` ownership, and run a two-rule Snakemake workflow with the SLURM executor, with and without `--slurm-pass-command-as-script`. Test files go to `/resnick/scratch/btyeh/playground/slurm_sandbox/pytest/`.

## Files

| Path | Purpose |
|---|---|
| `src/confine` | Entry point (`#!/usr/bin/python3 -I`) |
| `src/agent_slurm/launcher.py` | Starts the broker and bwrap; modes for jobs (`--job`) and steps (`--step`) |
| `src/agent_slurm/policy.py` | Policy from Claude settings; bwrap arguments |
| `src/agent_slurm/placeholders.py` | Creates the placeholder files and removes them after the last confinement of the project |
| `src/agent_slurm/broker.py` | Checks and runs SLURM calls; job wrappers; `scancel` ownership |
| `src/agent_slurm/options.py` | Option allowlists and parsers |
| `src/agent_slurm/envclean.py` | Environment cleaning |
| `src/agent_slurm/stand-in` | Replaces the SLURM commands inside the confinement |
| `src/agent_slurm/jobentry.py` | Opens the job's log files inside the confinement and starts the script |
| `src/agent_slurm/config.json` | Installation settings |
| `install.sh` | Installs read-only into `~/bin` |
| `tests/` | Tests; `tests/fake_slurm/fake.py` stands in for SLURM in the local tests |
| `walkthrough.md` | Step-by-step walkthrough of the code, with Q&A and possible extensions |

## Alternatives considered

- **Stand-ins inside the confinement that call the real `sbatch`/`srun`** (the previous version of this README): simpler, but any code in the confinement can call the real commands or use munge, so it only stops mistakes, and `scancel` ownership can't be enforced.
- **One confined job with Snakemake's local executor:** no stand-ins needed, but a run is limited to one node.
- **A long-running broker started by you:** the same checks, but a program to keep running; the per-`confine` broker needs none.
- **Snakemake's Apptainer support:** confines rule commands, but not Snakemake itself, the Snakefile's code, or `run:` blocks.
- **A Claude Code hook rewriting `sbatch` commands:** sees only Claude's own commands, not those made by Snakemake or jobs.

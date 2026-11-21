# A walkthrough of `confine` and its SLURM broker

This walkthrough follows the code in the order it runs, from typing `confine CMD` on a login node to a job script running confined on a compute node. It assumes you are comfortable in Python and have met systems programming once (fork, exec, file descriptors, pipes), but not Linux system administration. Links point into `src/` and are current as of commit `461bb36`.

1. [The problem](#step-1-the-problem)
2. [Entry point and config](#step-2-entry-point-and-config)
3. [The policy: deciding what's writable](#step-3-the-policy-deciding-whats-writable)
4. [Building the confinement: what bubblewrap does](#step-4-building-the-confinement-what-bubblewrap-does)
5. [Starting the broker and launching the command](#step-5-starting-the-broker-and-launching-the-command)
6. [The stand-in: how a confined `sbatch` gets out](#step-6-the-stand-in-how-a-confined-sbatch-gets-out)
7. [The broker checks the call](#step-7-the-broker-checks-the-call)
8. [Submitting the job: the spool, `job.json`, `wrapper.sh`](#step-8-submitting-the-job-the-spool-jobjson-wrappersh)
9. [On the compute node: `confine --job` and `jobentry.py`](#step-9-on-the-compute-node-confine---job-and-jobentrypy)
10. [`srun`, `scancel`, and jobs that submit jobs](#step-10-srun-scancel-and-jobs-that-submit-jobs)
11. [Possible extensions](#possible-extensions)

---

## Step 1: The problem

**SLURM basics.** The cluster has *login nodes*, where you type commands, and *compute nodes*, where work runs. `sbatch job.sh` on a login node queues the script; SLURM later runs it on some compute node **as you**, with all your file permissions. Inside a job, `srun my_tool` launches a *job step*, another process started through SLURM.

**How SLURM knows it's you: munge.** Every node runs a background service called munge. When `sbatch` wants to submit, it connects to munge through a special file, `/run/munge/munge.socket.2`, and gets back a signed ticket saying "this request is from uid btyeh." The cluster trusts that ticket.

- A *Unix socket* is a file-system path that two processes on the same machine can connect through, like a local network connection. Think of a pipe that you can find by name and that carries traffic both ways.
- The key point is that **munge hands a ticket to any program running as you**. `sbatch` isn't special: a Python script, or any program that speaks munge's protocol, can get one the same way.

**What we want.** Claude should be able to submit jobs and run Snakemake pipelines across the cluster. But every process it starts, on any node, must only be able to write to approved folders: the project, its scratch folder, and the shared agent scratch. That must hold even if the agent actively tries to get around it.

**Why the obvious fix fails.** The obvious fix is to wrap each job script in a sandbox. That breaks in two ways:

1. Inside a job, `srun my_tool` asks SLURM to start a process, and SLURM starts it **outside** your wrapper.
2. Code inside the sandbox can still reach munge, so it can submit a brand-new, unsandboxed job. A fake `sbatch` script that checks arguments doesn't help, because the code can talk to munge directly.

**The idea behind this codebase.** Think of a prisoner and a guard:

- **The prisoner** is the confined command. It sees a read-only file system except for the approved folders, and `/run/munge` is **hidden**, so it can't call SLURM at all, whatever program it uses.
- **The guard** is the *broker*. It runs outside the confinement and can reach munge. The prisoner passes SLURM requests to the guard, which checks each one, rewrites it so the new job also starts confined, and makes the real call.
- **Every compute node repeats this.** When a job starts there, it gets its own prisoner and guard, so the rule holds at every level.

**What "hidden" means.** Inside the confinement, `/run/munge` is an **empty folder**; the real munge socket is still there for every other process on the machine. Linux lets a process have its own private view of the file system, and in that view a folder can be laid *on top of* an existing path, like taping a blank sheet over a page. bubblewrap, the sandboxing tool `confine` uses, lays an empty, read-only folder over `/run/munge` ([policy.py:231-233](src/agent_slurm/policy.py#L231-L233)). Step 4 explains the mechanism, and why the confined command can't peel the sheet off.

---

## Step 2: Entry point and config

**The trust split.** One idea runs through the whole walkthrough: `confine` itself runs **outside** the confinement, because it is the code that builds it. So `confine` is the trusted part, and an agent must not be able to influence it. Most of the odd-looking details below follow from that.

### `src/confine`: the 15-line entry script

[src/confine](src/confine):

```python
#!/usr/bin/python3 -I
...
sys.path.append(os.path.dirname(os.path.realpath(__file__)))
from agent_slurm import launcher
sys.exit(launcher.main(sys.argv[1:]))
```

**The `#!` line.** When you run a file with a `#!` first line, the kernel runs the program named there and hands it the file. Here that is the **system Python, `/usr/bin/python3`**, not a conda Python: it sits where no agent can modify it, and it exists on every compute node. That is why the project's CLAUDE.md requires Python 3.9 and the standard library only.

**The `-I` flag** ("isolated mode") tells Python to ignore environment variables like `PYTHONPATH` and `PYTHONSTARTUP`, and to skip your user `site-packages`. The agent controls the environment of whatever it launches. Without `-I`, `PYTHONPATH=/evil confine ...` would make Python import the agent's code *inside the trusted launcher, before any confinement exists*.

**The `sys.path` line.** `-I` also stops Python from adding the script's own folder to `sys.path`, so the script adds it back by hand. `realpath` follows symlinks, so `agent_slurm/` is found next to the real file even if `confine` was reached through a link.

### `launcher.main`: four modes

[launcher.py:99-131](src/agent_slurm/launcher.py#L99-L131) decides what to do from its arguments:

| Invocation | Who uses it | Covered in |
|---|---|---|
| `confine CMD ...` | you or Claude | Steps 3-5 |
| `confine --print-policy` | you, for debugging | Step 3 |
| `confine --job JOBDIR` | `wrapper.sh` on a compute node | Step 9 |
| `confine --step STEPFILE -- CMD` | `srun` on a compute node | Step 10 |

Any expected error (`PolicyError`, `OSError`, ...) becomes a one-line `confine: ...` message and exit code 1.

### `config.py`: installation settings

`install.sh` copies the code to `~/bin`, all of it read-only (mode `0444`/`0555`):

```
~/bin/confine
~/bin/agent_slurm/   launcher.py, broker.py, ..., stand-in, config.json
```

[config.load](src/agent_slurm/config.py#L47-L49) reads `config.json` into a plain `Config` object ([config.py:25-44](src/agent_slurm/config.py#L25-L44)) that only holds paths. The main fields:

| Field | Meaning (current value) |
|---|---|
| `bwrap` | the bubblewrap program (a conda build, because the system one is too old) |
| `spool` | `~/.local/state/agent-slurm`: the broker's private records (Step 8) |
| `shared_writable` | folders writable in every project (`/resnick/scratch/btyeh/claude`) |
| `env_dir` | where `TMPDIR` and caches go inside, since `$HOME` is read-only there |
| `hide` | folders that appear empty inside (`~/.ssh`, `~/.claude`, ...) |
| `socket_dir` | `/dev/shm`, where broker sockets are created |
| `real_bin_dir` | where the real `sbatch` etc. live (`/usr/bin`) |

**Why read-only and outside the project?** If the agent could edit `config.json` or `policy.py`, it could rewrite its own jail, for example by adding `/` to `shared_writable`. Step 3 adds a second guard: `confine` refuses to start if the install folder would be writable.

A few constants matter in later steps ([config.py:11-18](src/agent_slurm/config.py#L11-L18)):

- `PROXIED`: the 10 SLURM commands replaced by the stand-in inside the confinement (Step 6).
- `REFUSED`: `salloc` and others, also replaced but always refused.
- `SOCKET_INSIDE = "/tmp/.agent-slurm.sock"`: where the confined command finds the broker.
- `MANDATORY_HIDE = ("/run/munge",)`: the one folder hidden no matter what `config.json` says.

### Q&A

**What is a spool?** An old Unix word for a folder where a program keeps its own records of queued work, as with print queues and outgoing mail. Here it is `~/.local/state/agent-slurm/<project>/`, holding the saved policy for each run, a folder per submitted job (script copy, `wrapper.sh`, SLURM's own log), and a small record per `srun` step. Only `confine` and its broker write there; nothing inside a confinement can, because `~` is read-only inside and Step 3's check refuses to make it writable. Step 8 shows why the broker relies on that.

**Is `/tmp/.agent-slurm.sock` guaranteed unused? What if it is already in use?** It can't be: the path only exists inside a confinement, and each confinement gets its own empty `/tmp`.

- bwrap gives the confined command a **fresh, empty, private `/tmp`** ([policy.py:216](src/agent_slurm/policy.py#L216)), new for every `confine` run.
- The real socket lives outside, at `/dev/shm/agent-slurm-XXXXXX/s`. `mkdtemp` picks a random, unused folder name and makes it private (mode 0700), so two runs never collide ([launcher.py:40-44](src/agent_slurm/launcher.py#L40-L44)).
- bwrap makes that real socket appear at `/tmp/.agent-slurm.sock` inside ([policy.py:236](src/agent_slurm/policy.py#L236)), the same "lay one path on top of another" mechanism as hiding.
- Nothing is ever created at `/tmp/.agent-slurm.sock` on the real machine.

If the confined command tries to delete or rename the socket path, it fails with "device busy," because something is mounted there. The worst it can do is cut off its own SLURM access.

**Do concurrent `confine` commands overwrite each other's files in `shared_writable` and `env_dir`?** They can, exactly as two ordinary programs can. `confine` controls **which folders are writable**; it does not give each run a private copy. The design goal is to protect everything *outside* the approved folders, not to isolate agents from each other. In practice the shared folders are mostly safe:

| Shared thing | Why it's usually fine |
|---|---|
| `env_dir/tmp` (`TMPDIR`) | Well-behaved programs create temp files with random unique names (`mkstemp`) |
| `env_dir/cache`, `env_dir/conda_pkgs` | Built to be shared caches; conda uses locks for this |
| `shared_writable`, the project | Your responsibility: use distinct output paths, as you would anyway |

`confine`'s own bookkeeping never collides. Socket and job folders come from `mkdtemp`, step files from `mkstemp`. Policy snapshots are named by a hash of their contents: two runs with the same policy write the same file, and `os.replace` makes each write atomic.

---

## Step 3: The policy: deciding what's writable

**Goal:** produce one dict that says which folders the confined command may write, which spots inside those stay read-only, and which folders appear empty. All of this happens in [policy.build](src/agent_slurm/policy.py#L104-L145).

**Where the rules come from.** You already tell Claude Code which folders it may write, in `.claude/settings.local.json` (`sandbox.filesystem.allowWrite`, etc.). `confine` reuses those settings rather than inventing a second config, so a job Claude launches can write exactly where Claude itself can.

### 3a. Find the project

[find_project_root](src/agent_slurm/policy.py#L41-L50) walks upward from the current folder until it finds one containing `.claude/settings.json` or `.claude/settings.local.json`. It **skips `$HOME`**, because `~/.claude/settings.json` is your *user-wide* settings file; without the skip, running from anywhere under your home folder could make your entire home folder "the project," and so writable.

### 3b. Merge the settings files

[filesystem_entries](src/agent_slurm/policy.py#L89-L101) reads four files, in Claude Code's order (managed, user, project, project-local), and collects `allowWrite`, `denyWrite` and `denyRead`. Paths follow Claude's conventions ([resolve](src/agent_slurm/policy.py#L69-L77)): `//x` and `/x` are absolute, `~/x` is in your home folder, anything else is relative to the project.

Wildcard patterns like `*.log` are handled asymmetrically. When a rule can't be honored, `confine` picks whichever outcome is *more* restrictive:

- An `allowWrite` pattern is **silently skipped**. That folder just stays read-only, which is the safe outcome.
- A `denyWrite` or `denyRead` pattern is a **hard error**. Silently ignoring a "deny" would leave open something you meant to close.

### 3c. Build the lists

**Writable:** the project folder, plus `allowWrite`, plus `shared_writable`. Folders that don't exist are skipped with a warning; each path goes through `realpath`, and duplicates are removed.

**Protect (read-only, even inside writable folders).** Some files are harmless to write *now* but get **executed later, outside any confinement**:

| File | Why it's dangerous |
|---|---|
| `.bashrc`, `.profile`, `.zshrc`, ... | Your shell runs it the next time *you* log in |
| `.git/hooks/` | git runs these scripts when *you* commit outside the confinement |
| `.git/config` | can point git at an arbitrary program to run (e.g. `core.fsmonitor`) |
| `.claude/` | the settings this policy comes from; the agent could widen its own permissions |
| `.vscode/`, `.idea/`, `.mcp.json` | editor and tool configs that can launch programs |

In the other writable folders, the ones that exist go on the `protect` list ([policy.py:115-129](src/agent_slurm/policy.py#L115-L129)), along with your `denyWrite` entries.

**Placeholders.** If one of these is *missing* from the project root, the agent could simply create it (a new `.mcp.json`, a new `.git/hooks/pre-commit`). So in the project root, every protected name goes on a `placeholders` list, whether it exists or not. A name that exists now may be another confinement's placeholder, gone by the time a job uses this policy, so the list can't be decided here. Step 5 puts an empty, unwritable file at each missing one while the command runs, and removes it afterwards.

**Hide:** `config.hide` (always including `/run/munge`) plus your `denyRead` entries.

### 3d. Validate: refuse dangerous policies

[validate](src/agent_slurm/policy.py#L152-L166) refuses to start if a writable folder:

- is `/`;
- **contains** your home folder, the spool, the install folder or `~/.claude` (e.g. `allowWrite: ["/home"]`);
- **lies inside** the spool, the install folder or `~/.claude`.

These places hold `confine`'s own code, its records and its rules, and the confined command must never be able to change them.

### 3e. Save a snapshot

[save](src/agent_slurm/policy.py#L186-L193) writes the dict as JSON to `spool/<project_key>/policies/<hash>.json`, read-only. A job submitted now may start an hour later on another machine, and it should run under *this* policy, not whatever the settings say by then. The job carries the snapshot's path (Step 8), and loading it runs `validate` again ([policy.load](src/agent_slurm/policy.py#L205-L208)).

`project_key` is the folder name plus a short hash of the full path, e.g. `myproj-3fa9c1e2b4d0`, so two projects both called `analysis` get separate spool folders.

The resulting dict looks like this (illustrative):

```json
{
 "project_root": "/resnick/groups/.../myproj",
 "project_key": "myproj-3fa9c1e2b4d0",
 "writable":     ["/resnick/groups/.../myproj", "/resnick/scratch/btyeh/myproj", "/resnick/scratch/btyeh/claude"],
 "protect":      ["/resnick/scratch/btyeh/myproj/.git/config", "/resnick/groups/.../myproj/bin", "..."],
 "placeholders": ["/resnick/groups/.../myproj/.bashrc", "/resnick/groups/.../myproj/.claude", "..."],
 "hide":         ["/run/munge", "/home/btyeh/.ssh", "/home/btyeh/.claude", "..."],
 "env_dir":      "/resnick/scratch/btyeh/claude/confine-env"
}
```

This dict is only a **description**; Step 4 turns it into an actual confinement. To see the real one for a project, run `confine --print-policy` in it (outside Claude's sandbox; see below).

### Q&A

**Why does `confine --print-policy` fail inside Claude's sandbox with "Permission denied" on `.claude/settings.json`, when that file doesn't exist?** That is a live example of placeholders, from Claude's own sandbox. Inside it, `.claude/` lists entries like:

```
crw-rw-rw-. 1 nobody nobody 1, 3  settings.json
crw-rw-rw-. 1 nobody nobody 1, 3  hooks
crw-rw-rw-. 1 nobody nobody 1, 3  skills
-rw-r-----. 1 btyeh  nobody 1376  settings.local.json   <- the only real file
```

- The `c` with `1, 3` marks a character device with device numbers 1, 3, which is `/dev/null`. Each entry is `/dev/null` laid over a name that doesn't exist on disk, so the agent can't create it. Your real `.claude/` is untouched.
- The mount table shows the placeholder is mounted `ro,nosuid,nodev`. `nodev` means device files there can't be opened, so opening fails with `EACCES` (`PermissionError`). That isn't the `FileNotFoundError` that [read_settings](src/agent_slurm/policy.py#L57-L60) treats as "absent," so `confine` stops with an error.
- `confine`'s placeholders look different. A mount needs an existing file to sit on, so `confine` creates an empty file at each missing name, mounts it read-only on itself, and removes it once no confinement of the project is left (Step 5f). Inside a `confine` run, a missing `.bashrc` in the project root looks like an empty file that can't be written.

This only matters inside Claude's sandbox; Claude is meant to call `confine` as an excluded command, which runs outside it.

**Does the hash guarantee a unique `project_key`?** Strictly, no hash can. There are two hashes, with different stakes:

| Hash | Where | Input | Length kept |
|---|---|---|---|
| `project_key` | [project_key()](src/agent_slurm/policy.py#L148-L149) | the project's full real path | 48 bits, plus the folder name as a prefix |
| snapshot filename | [save()](src/agent_slurm/policy.py#L186-L193) | the policy's JSON text | 64 bits |

Infinitely many paths map onto 2^48 prefixes, so some must share one (the pigeonhole principle). By accident, collisions are astronomically unlikely: two projects must have the same folder name *and* the same 48-bit prefix (about 10^-9 among 1,000 projects). If one did happen:

- **Two projects sharing a `project_key`** would share a spool folder. That only weakens the "own jobs only" rule for `scancel` and `srun --jobid` (Step 10): one project's agent could cancel the other's *agent* jobs. It would not escape the confinement, because write access comes from each project's own policy.
- **Two policies sharing a snapshot name** would be worse: `save` writes only if the file doesn't exist, so the second policy would silently reuse the first one's file.

A deliberate collision would require the agent to create a folder with your other project's name and search about 2^48 (3x10^14) paths for a matching prefix, which is feasible on a GPU but buys little. Forging a snapshot name would take about 2^64 tries, and the agent can't choose the policy contents anyway, because it can't edit the settings.

**Do `build` and `save` run every time `confine` is called? Do jobs share policies? What if the settings change between jobs?** Every `confine CMD` builds a fresh policy; the internal modes (`--job`, `--step`) only load a snapshot ([launcher.py:105-128](src/agent_slurm/launcher.py#L105-L128)).

- **One `confine` call that submits many jobs** (e.g. `confine snakemake ...` submitting 50) makes one snapshot, and the broker writes the same path into every job's `job.json` ([broker.py:204](src/agent_slurm/broker.py#L204)).
- **Many separate `confine` calls** each rebuild the policy. If nothing changed, the JSON is byte-for-byte identical, so it hashes to the same filename and `save` finds the file already there. Identical policies share one snapshot automatically.
- **Jobs that submit jobs:** on the compute node, `confine --job` hands its broker the snapshot path it was given ([launcher.py:108-109](src/agent_slurm/launcher.py#L108-L109)), so every descendant inherits the original snapshot.

If the settings change:

```
10:00  confine sbatch a.sh   -> policy P1 (snapshot 3fa9...json); job A queued
10:05  you edit settings.local.json (add a writable folder)
10:06  confine sbatch b.sh   -> policy P2 (snapshot 81c0...json); job B queued
11:00  job A starts          -> runs under P1   (the new folder is NOT writable)
11:01  job B starts          -> runs under P2   (it is)
       job A runs sbatch c.sh -> job C also gets P1
```

A job runs under the policy in force **when it was submitted**, deliberately pinned to the moment you approved the launch. Loading re-runs the *current* `validate`, so a `config.json` change that makes an old snapshot unsafe stops the old job. Removing a folder from the settings does not take write access away from jobs already queued or running; cancel them to revoke it.

**How does `policy.py` handle `permissions.additionalDirectories`?** It ignores it; only `sandbox.filesystem.{allowWrite, denyWrite, denyRead}` are read ([policy.py:93](src/agent_slurm/policy.py#L93)). In Claude Code, `additionalDirectories` extends where the *file tools* (Read, Edit) work without asking, while `allowWrite` controls what the *Bash sandbox* may write, which is the closer match to what `confine` enforces. This project lists the same scratch folders in both. If Claude's Bash sandbox also made `additionalDirectories` writable (not verified), a folder listed only there would be writable for Claude's Bash commands but read-only under `confine`. That is the safe direction: the job fails with "Read-only file system" rather than writing somewhere unapproved.

---

## Step 4: Building the confinement: what bubblewrap does

[policy.bwrap_args](src/agent_slurm/policy.py#L211-L241) turns the Step 3 dict into command-line arguments for **bubblewrap** (`bwrap`), the same tool Claude's own sandbox uses.

### Three concepts

**1. Mount namespace: a private view of the file system.** Normally every process on a machine sees the same directory tree. Linux can give a process its own *copy of the mount table*, the list of "this storage appears at that path." Changes to that copy affect only that process and its children.

**2. Bind mount: make one path appear at another.** `bind A B` means "whatever is at A, also show it at B," like a hard link for whole directory trees, optionally read-only (`ro-bind`). **A later mount covers whatever was at that path before**, like stacking sheets of paper, and bwrap applies its arguments in order.

**3. tmpfs: a fresh, empty, in-memory folder.** Mount one over `/tmp` and you get an empty `/tmp` that disappears when the process ends.

**Why bwrap can do this without root.** bwrap first creates a **user namespace**, a private bubble in which your process temporarily has admin powers over *its own* namespaces only. It sets up the mounts, then **drops those powers** before starting your command, which runs as plain you with no ability to change mounts.

### The arguments, in order

| bwrap argument | Effect inside |
|---|---|
| `--ro-bind / /` | **Base layer:** the entire file system, read-only |
| `--dev /dev`, `--proc /proc` | Minimal fresh `/dev` (null, tty, ...) and process info |
| `--tmpfs /tmp` | Empty private `/tmp` (why the socket path never collides) |
| `--unshare-user --disable-userns` | Create the user-namespace bubble, and forbid the command from creating its own |
| `--die-with-parent` | If `confine` dies, the command is killed too |
| `--new-session` | Detach from your terminal (below) |
| `--bind W W` for each writable folder | Lay a **read-write** sheet over the read-only base |
| `--ro-bind P P` for each protected path | Lay a **read-only** sheet back over `.git/hooks`, `.claude`, ... |
| `--ro-bind P P` for each placeholder | Occupy the name so it can't be created (`run_confined` made the empty file at P first; Step 5f) |
| `--tmpfs H --remount-ro H` for each hidden folder | Lay an **empty, read-only** sheet over `/run/munge`, `~/.ssh`, ... (a hidden *file* gets `/dev/null`) |
| `--bind <real socket> /tmp/.agent-slurm.sock` | The broker's socket appears inside |
| `--ro-bind stand-in /usr/bin/sbatch` (and the others) | `sbatch` etc. are now the stand-in (Step 6) |

The stacking for a concrete project:

```
path inside                          what you get                  layer
/                                    read-only                     1
/tmp                                 empty, writable               2
/resnick/.../myproj                  writable                      3  (covers 1)
/resnick/.../myproj/.git/hooks       read-only                     4  (covers 3)
/resnick/.../myproj/.bashrc          empty, read-only              4  (placeholder)
/run/munge                           empty, read-only              5  (covers 1)
/usr/bin/sbatch                      the stand-in                  6  (covers 1)
```

If the writable bind came *after* the protect binds, it would cover them and `.git/hooks` would be writable again. That is why the loops go writable, then protect, then hide.

### Why can't the command peel the sheets off?

- **No powers.** Unmounting or remounting needs an admin capability (`CAP_SYS_ADMIN`), which bwrap dropped, so `umount /run/munge` fails with "Operation not permitted."
- **Locked mounts.** The kernel marks mounts set up this way as *locked*: they can't be removed one by one from inside, even by a process that somehow regains powers.
- **No new bubble.** `--disable-userns` stops the command from creating a *new* user namespace with fresh admin powers. The kernel's locks would probably still hold, but this removes the whole category of tricks (defense in depth).

### `--new-session`: an old terminal trick

A process attached to your terminal can use the `TIOCSTI` ioctl to **push fake keystrokes** into the terminal's input. After `confine` exits, your shell, which is *outside* the confinement, would read and run them. `--new-session` detaches the command from your terminal (`setsid()`), so the trick has nothing to target. Output still reaches your screen through the inherited stdout and stderr.

### "Checked now, at start"

`bwrap_args` looks at the file system as it is *right now* ([policy.py:221-235](src/agent_slurm/policy.py#L221-L235)). A writable folder that has since been deleted is skipped. A protected path that is a symlink is resolved, and its target is made read-only only if it lies inside a writable folder (if not, it is read-only already). This matters because a compute node may see things differently an hour later.

Inside the bubble, `/proc/self/uid_map` no longer shows the identity mapping a normal process has; Step 5's Q&A explains how `confine` uses that to notice it is already confined.

### Q&A

**`PROTECTED_NAMES` only protects the files it lists, so other, less common files could still be created. But only inside writable folders, right?** Yes on both, with two nuances.

1. **Only the listed names are protected.** The list (`PROTECTED_NAMES` plus `.git/hooks` and `.git/config`) mirrors Claude's own sandbox. Other files that some tool runs automatically could still be created, for example:

   | File | Runs when *you*, outside any confinement... |
   |---|---|
   | `.Rprofile` | start R in that folder |
   | `conftest.py` | run `pytest` there |
   | `.envrc` | `cd` into it, if you use direnv |
   | `.pre-commit-config.yaml` | commit, if pre-commit is installed (the hook is protected, its config isn't) |
   | `Snakefile`, `Makefile`, any script | run them, which you do on purpose |

   The last row is the fundamental limit: the agent can write code in the project, and anything you later run from the project outside `confine` runs that code. The list only blocks files that run **implicitly**.

2. **Yes, only inside writable folders.** The base layer is `--ro-bind / /`, so creating a file anywhere else fails with "Read-only file system." The private `/tmp` is the one other writable spot, and it vanishes when the command ends.

**Nuance A: protection covers only the top level of each writable folder.** The code checks `os.path.join(w, name)` for each writable folder `w` ([policy.py:116-129](src/agent_slurm/policy.py#L116-L129)). `myproj/sub/.bashrc` is unprotected but harmless, since nothing runs a `.bashrc` from a subfolder. **`myproj/sub/.git/hooks/` is also unprotected**: in a nested git repository or submodule, the agent can plant a hook that runs the next time you commit there outside the confinement. This is the one real gap in this class.

**Nuance B: placeholders only exist in the project root.** In other writable folders, such as scratch, a *missing* protected name isn't occupied, so the agent could create `/resnick/scratch/btyeh/claude/.git/config` or similar. That matters only if you later run git or another tool in that folder outside the confinement.

---

## Step 5: Starting the broker and launching the command

For `confine CMD`, `main` does three things ([launcher.py:125-128](src/agent_slurm/launcher.py#L125-L128)): if already confined, run CMD directly (see the Q&A); build and save the policy (Step 3); and call **`run_confined`** ([launcher.py:36-96](src/agent_slurm/launcher.py#L36-L96)).

### What `run_confined` ends up with

```
confine  (one Python process, outside the confinement)
 ├─ main thread ........ waits for bwrap; forwards signals
 ├─ info-reader thread . learns the command's pid from bwrap
 └─ broker threads ..... _serve (accepts connections) + one per SLURM call   (Steps 6-8)
      │
      └─ child process: bwrap ──> CMD   (inside the confinement)
```

The broker isn't a separate server; it is **threads inside the same `confine` process**, living exactly as long as this `confine` does.

### 5a. Folders and the socket

```python
os.makedirs(env_dir/tmp, env_dir/cache, env_dir/conda_pkgs)
sockdir = tempfile.mkdtemp(prefix="agent-slurm-", dir="/dev/shm")
broker = Broker(cfg, pol, policy_path, sockdir + "/s")
broker.start()
```

- **`/dev/shm`** is a memory-backed folder every Linux machine has. It is local to each node, which a Unix socket needs. `mkdtemp` creates a uniquely named folder only you can enter (mode 0700).
- **[Broker.start](src/agent_slurm/broker.py#L102-L107)** is the standard server pattern: `socket()`, `bind()` (which creates the socket file), `chmod 0600`, `listen()`, then a daemon thread running the `accept()` loop.

### 5b. Learning the command's pid: `--info-fd`

Signal forwarding (5d) needs the command's pid, and only bwrap knows it, because bwrap forks the command. **bwrap reports it**; `confine`'s code never writes to the pipe. `confine` only creates the pipe, tells bwrap which descriptor to write to, and reads the result ([launcher.py:48-72](src/agent_slurm/launcher.py#L48-L72)).

```python
info_r, info_w = os.pipe()
```

The kernel creates a pipe and returns two descriptor numbers in `confine`'s fd table, say `info_r = 5` (read end) and `info_w = 6` (write end).

```python
args = [...] + ["--info-fd", str(info_w)]
```

This only builds the text arguments `"--info-fd", "6"`. `--info-fd FD` is a bwrap option meaning "once the sandbox is running, write a JSON description of it to your descriptor FD, then close it." Passing a number works because a child inherits descriptors under the **same numbers** (`fork` copies the fd table, `exec` keeps it).

```python
proc = subprocess.Popen(args + ["--"] + command, pass_fds=(info_w,))
```

`Popen` forks, and the child execs bwrap. By default Python closes every descriptor except 0, 1 and 2 in the child (Python 3 marks new descriptors non-inheritable); `pass_fds=(6,)` keeps the write end open, so bwrap's fd 6 is the pipe. **bwrap** then sets up the namespaces and mounts, forks the command, writes something like `{"child-pid": 12345}` to fd 6, and closes it. Newer bwrap versions add namespace IDs; `confine` only uses `child-pid`, the pid as seen from outside, which is what `os.killpg` needs.

```python
os.close(info_w)
```

A pipe reader sees end-of-file only when **every** copy of the write end is closed. After the fork there were two, the parent's and bwrap's; without this line the reader would wait forever.

```python
def read_info():
    with os.fdopen(info_r) as f:
        child.update(json.loads(f.read() or "{}"))
```

On a separate thread, `f.read()` **blocks** until bwrap has written and closed fd 6, then the JSON fills the `child` dict. It is a thread because the main thread must go on to install signal handlers and call `proc.wait()`.

```
confine (parent)                          bwrap (child)
----------------                          -------------
pipe() -> r=5, w=6
fork+exec bwrap, keeping fd 6  ─────────> starts with fd 6 open
close(6)                                  builds namespaces + mounts
reader thread: read(5) ... blocks         forks CMD (pid 12345)
                                          write(6, '{"child-pid": 12345}')
                                          close(6)
read returns; child = {"child-pid": 12345}
```

### 5c. The environment inside

```python
for name, value in policy.inner_env(pol, restore).items():
    args += ["--setenv", name, value]
```

The command inherits `confine`'s environment, plus these from [inner_env](src/agent_slurm/policy.py#L244-L253):

| Variable | Why |
|---|---|
| `TMPDIR`, `XDG_CACHE_HOME`, `CONDA_PKGS_DIRS` | Point into `env_dir`, because `$HOME` is read-only inside |
| `PYTHONNOUSERSITE=1` | Ignore `~/.local/lib/python...` packages |
| `AGENT_SLURM_PROJECT` | Tells code inside which project it's confined to |
| `restore` | Empty here. On a compute node, the variables the broker removed for safety (Step 7) |

### 5d. Signal forwarding

`--new-session` detached CMD from your terminal, so **Ctrl-C** sends SIGINT to *`confine`*, not to CMD. The same happens when SLURM cancels a job and sends SIGTERM. `confine` passes the signal on itself ([launcher.py:74-85](src/agent_slurm/launcher.py#L74-L85)):

```python
def forward(signum, frame):
    pid = child.get("child-pid")
    if pid:
        os.killpg(pid, signum)   # CMD leads its own session, so its pid is also its process-group id
    else:
        proc.send_signal(signum) # pid not known yet: signal bwrap instead
```

`killpg` signals CMD's whole **process group**, i.e. CMD plus every process it started (unless they moved to another group), so Ctrl-C on `confine snakemake ...` reaches Snakemake and its children.

### 5e. Waiting and cleanup

```python
rc = proc.wait()
return rc if rc >= 0 else 128 - rc
...
finally:
    broker.stop()
    shutil.rmtree(sockdir)
```

- Python reports "killed by signal N" as `-N`; `128 + N` is the shell convention, so a SIGTERM'd command makes `confine` exit with 143.
- [broker.stop](src/agent_slurm/broker.py#L109-L113) closes the listening socket, so the `accept()` loop errors out and its thread ends; then the socket folder is deleted. Remaining broker threads are daemon threads and die with the process.
- **`--die-with-parent`** covers the reverse case: if `confine` itself is killed, even by an uncatchable SIGKILL, the kernel kills CMD too. It reaches only CMD itself: a background process CMD left running (`sleep 60 &`) outlives `confine`, still confined but without a broker, so it can't reach SLURM. In a job, SLURM kills such processes when the job ends.

### 5f. Placeholder files

Step 3 listed the protected names in the project root. For each one that is missing, `run_confined` creates an empty, read-only file before starting bwrap, and removes it when the confinement is over ([placeholders.py](src/agent_slurm/placeholders.py)). bwrap could create the file itself, since a mount needs something to sit on, but it would never remove it, and the files would pile up in your project.

The removal is the delicate part. Unlinking a file that is a mount point in *another* mount namespace detaches the mount there. So if one `confine` removed `.mcp.json` while a second one, or a Snakemake job on a compute node, still ran in the same project, the second one's command could then create `.mcp.json`. Hence:

- **Register.** Every confinement writes a registration to `spool/<project_key>/placeholders/users/` before it starts. The registration records the host, boot, `confine`'s pid and start time, and, once bwrap reports it, the confinement's mount namespace. It then creates the missing files and records their inode numbers in `created.json`.
- **Release.** When the confinement ends, it removes its registration. If no live registration is left, it removes the recorded files: only those still empty, regular, and with the same inode, so a file you created in the meantime stays.
- **Live** means `confine` still runs, or some process is still in the confinement's mount namespace (the background `sleep` above). A confinement that finds such a process keeps its registration. Registrations from other nodes can't be checked and count as live. A crashed node leaves the files in place until a later confinement of the project ends on that node.
- **Lock.** Registering and releasing happen under a POSIX lock (`fcntl.lockf`) on `placeholders/lock`. On the NFS home, such locks are kept by the server, so they work across nodes. If the lock can't be taken, `confine` refuses to run, because an unregistered confinement could lose its placeholders to another one's release.

### Q&A

**What does `already_confined()` do, and what is `/proc/self/uid_map`?** [already_confined](src/agent_slurm/launcher.py#L28-L33) answers "am I already inside a user namespace?" by reading one kernel file.

`/proc` isn't stored on disk; it is a **virtual file system** whose contents the kernel generates when you read them, and `/proc/self/` means "the process doing the reading." `/proc/self/uid_map` shows the uid translation table of the process's user namespace, one line per range:

```
<first uid inside>   <first uid in parent>   <how many uids>
```

A normal process, in the original "initial" namespace, has the identity table over every possible uid:

```
         0          0 4294967295
```

Inside a bubble, the table maps only a few uids. In Claude's sandbox (also bubblewrap) it was:

```
     26022          0          1
```

That is one uid, 26022 (you), mapped to uid 0 in the parent namespace, itself an intermediate layer of Claude's sandbox. The exact numbers vary; what matters is that it isn't the identity line.

```python
def already_confined():
    with open("/proc/self/uid_map") as f:
        return f.read().split() != ["0", "0", "4294967295"]
```

When it returns true, `main` skips building a confinement and `execvp`s the command ([launcher.py:125-126](src/agent_slurm/launcher.py#L125-L126)), replacing the current process (`exec` without `fork`):

- **bwrap couldn't start anyway**, because `--disable-userns` forbids creating new user namespaces inside a `confine`.
- **It's harmless**: the command stays in whatever confinement it was in, with munge still hidden and the stand-ins in place. So `confine` can appear in job scripts or Snakemake rules that might run either inside or outside a confinement.

Two caveats came up:

1. **"In a user namespace" isn't quite "confined by us."** The check can't tell `confine`'s bubble from any other user namespace. Inside a container that uses one (e.g. Apptainer in its unprivileged mode), `confine CMD` would run CMD with no confinement; if munge were reachable there, CMD could reach SLURM directly. Getting there would mean approving something like `apptainer exec ... confine ...`, which breaks the README's rule to approve only commands that *start* with `/home/btyeh/bin/confine`. This is still open; see [Possible extensions](#possible-extensions).
2. **An unreadable `uid_map` used to fail open.** The function originally caught `OSError` and returned `True`, so `confine` would run the command unconfined. Commit `9be582d` removed that branch: the `OSError` now reaches `main`, which prints e.g. `confine: [Errno 2] No such file or directory: '/proc/self/uid_map'` and exits 1. [tests/test_launcher.py](tests/test_launcher.py) checks this.

**Can a signal arrive before `confine` knows the command's pid? Is there a simple way to close that gap?** Yes. In a test with the same bwrap flags, a command that traps SIGTERM was started, and SIGTERM was sent to **bwrap**, which is what `forward` does while `child-pid` is unknown:

```
bwrap exit: 143                                  <- bwrap died from SIGTERM
ticks right after kill: 10, one second later: 10 <- the command stopped too
command's TERM trap did NOT run                  <- but it never saw SIGTERM
```

The signal turns into a **hard kill**: bwrap dies, and `--die-with-parent` makes the kernel SIGKILL the command, which can't catch it. Nothing escapes; the command just loses its chance to clean up, or to treat SIGUSR1 as a warning. There are two windows:

| Window | Duration | What a signal does |
|---|---|---|
| A: `Popen` until `signal.signal(...)` runs ([launcher.py:52-85](src/agent_slurm/launcher.py#L52-L85)) | microseconds | Hits `confine` with no handler. SIGINT raises `KeyboardInterrupt`; SIGTERM, SIGHUP and SIGUSR1/2 kill `confine` outright, skipping `finally` (the `/dev/shm` socket folder is left behind) |
| B: handlers installed, pid not yet reported | milliseconds | Goes to bwrap, so the command is SIGKILLed |

The fix is to install the handlers *before* `Popen` and queue signals until the pid is known; it is sketched under [Possible extensions](#possible-extensions). Today's behavior is harsh but safe, and both windows are tiny.

---

## Step 6: The stand-in: how a confined `sbatch` gets out

Inside the confinement, CMD runs `sbatch job.sh`. Because of the bind mounts from Step 4, `/usr/bin/sbatch` is [stand-in](src/agent_slurm/stand-in), and so are `srun`, `squeue`, `scancel` and the rest: **one script mounted at ten paths**. It learns which command it is impersonating from the name it was run under:

```python
name = os.path.basename(sys.argv[0])     # "/usr/bin/sbatch" -> "sbatch"
```

The stand-in is **self-contained**: it doesn't import `agent_slurm` and duplicates the small message helpers, because it runs as `/usr/bin/sbatch`, far from the package folder.

**The stand-in only ever runs inside the bubble.** From there it sees:

- **`/tmp/.agent-slurm.sock`**: the only way out;
- **`/usr/bin/sbatch`**: itself, because the bind mount covers the real binary at that path;
- **`/run/munge`**: an empty folder.

It sends **one request** over the socket, `{"argv": [...], "cwd": ..., "env": {...}}` with its fds 0, 1 and 2 attached. The broker, outside the bubble, can see the real binaries and munge; it checks the request, runs the real command on those fds, and replies `{"exit": rc}` (for `sbatch job.sh`, after first asking the stand-in to open the script).

```
         inside the bubble                    │                outside
                                              │
 CMD ──exec──> /usr/bin/sbatch (= stand-in)   │
                 │  {argv, cwd, env} + fds 0,1,2
                 └──── /tmp/.agent-slurm.sock ═══ /dev/shm/agent-slurm-XXXX/s ────> broker
                                              │                                     │ check, rewrite
                 <──── {exit: rc} ════════════════════════════════════════════════  │
                                              │                         real /usr/bin/sbatch
                                              │                                     │
                                              │                              /run/munge (real)
```

**Hiding the real binaries is not what confines.** The real `sbatch` file is still readable inside (under another path, or as a copy), and the agent could compile its own. None of that helps, because every SLURM client needs a munge ticket and `/run/munge` is empty. The stand-in mount is a convenience that routes plain `sbatch` calls to the broker; **hiding munge** is what confines.

**The stand-in is untrusted, too.** The agent could ignore it and write its own client for `/tmp/.agent-slurm.sock`. That's fine: the stand-in has no privileges and makes no decisions, and the **broker** treats every byte it receives (argv, env, cwd) as attacker-controlled input. All checking happens there (Step 7).

### What the stand-in does ([stand-in:46-90](src/agent_slurm/stand-in#L46-L90))

**1. Make sure fds 0, 1 and 2 exist.** A program can be started with stdin closed; the broker expects three descriptors, so any missing one is filled with `/dev/null`.

```python
for fd in (0, 1, 2):
    try: os.fstat(fd)
    except OSError: os.dup2(os.open(os.devnull, os.O_RDWR), fd)
```

**2. Connect and send the request.**

```python
sock.connect("/tmp/.agent-slurm.sock")
send_msg(sock, {"argv": [name] + sys.argv[1:], "cwd": cwd, "env": dict(os.environ)}, [0, 1, 2])
```

That last argument, `[0, 1, 2]`, involves two concepts.

**Message framing.** A Unix stream socket is a **byte stream with no message boundaries**: `read` may return half a message, or two glued together. So every message is `[4-byte big-endian length][that many bytes of JSON]`, and the receiver reads exactly 4 bytes, then exactly that many more ([recv_exact](src/agent_slurm/stand-in#L28-L35)). The broker caps the length at 16 MiB so a hostile client can't make it allocate unbounded memory ([broker.py:62-63](src/agent_slurm/broker.py#L62-L63)).

**Passing file descriptors over a socket (`SCM_RIGHTS`).** Unix sockets can carry **open file descriptors** alongside data. `socket.send_fds` attaches them, and the kernel *installs duplicates in the receiving process*, like a `dup()` across processes. The broker gets new numbers, say 9, 10 and 11, referring to **the same open files** as the stand-in's 0, 1 and 2: your terminal, a pipe to Snakemake, a log file.

```
inside confinement                          outside
stand-in fd 1 ──┐                           broker fd 10 ──┐
                └──> [ the same open pipe/terminal/file ] <┘
                                                            └─ real sbatch's stdout (Step 8)
```

- **No copying output back and forth.** In Step 8 the broker starts the real `sbatch` with those fds as its stdio, so `Submitted batch job 123` lands *directly* in CMD's stdout. Only the exit code comes back as a message.
- **No added power.** A passed fd carries the access it was opened with, and nothing more: access to *one already-open file*, not to a path. The real command can only write where the confined command already could.

**3. Relay signals while waiting.** If you Ctrl-C a confined `srun`, or Snakemake kills one, the stand-in forwards the signal as `{"signal": n}` on the same socket, and the broker's [_relay](src/agent_slurm/broker.py#L280-L301) thread sends it to the real `srun`. If the stand-in is killed outright, the socket closes; the broker sees end-of-file and stops the real command (SIGTERM, then SIGKILL after 10 seconds). A real `srun` can't outlive its caller.

```python
def relay(signum, frame):
    send_msg(sock, {"signal": signum})
```

**4. Wait for the reply.** The broker sends one of two messages:

- `{"open": "job.sh"}`: open this file and send back the fd, so the broker reads the job script *as the confinement sees the file system* (Step 8).
- `{"exit": rc}`: the real command finished; exit with its code.

If the broker can't be reached, the stand-in prints `sbatch (confined): cannot reach the agent-slurm broker: ...` and exits 1.

### On the broker's side: who is calling?

When a connection arrives, [_serve](src/agent_slurm/broker.py#L115-L126) asks the kernel who is on the other end:

```python
_, uid, _ = struct.unpack("3i", conn.getsockopt(SOL_SOCKET, SO_PEERCRED, ...))
if uid != os.getuid(): conn.close()
```

`SO_PEERCRED` returns the connecting process's pid, uid and gid **as recorded by the kernel**, so they can't be faked. The socket is already owner-only in a private folder; this is a second lock on the same door. Each accepted connection gets its own [_handle](src/agent_slurm/broker.py#L128-L151) thread, which receives the message and its three fds and hands off to `_prepare`.

---

## Step 7: The broker checks the call

[_prepare](src/agent_slurm/broker.py#L153-L171) dispatches on the command name:

| Command | Handling |
|---|---|
| `sbatch` | parse and check, then build a job folder (this step + Step 8) |
| `srun`, `scancel` | parse and check, plus ownership rules (Step 10) |
| `sacct`, `scontrol`, `sacctmgr` | small checks, then run as-is |
| `squeue`, `sinfo`, `sshare`, `sstat` | run as-is (read-only) |
| anything else (`salloc`, ...) | refused: "not available in the confinement" |

**What the broker worries about.** It doesn't judge the job's code, which will run confined. It worries only about **what SLURM itself does outside the confinement** on your behalf:

- **write a file you name:** `--output=~/.bashrc`;
- **run a program:** `--prolog`, `--task-prolog`;
- **read a file:** `--export-file`, `--nodefile`;
- **load your login environment:** `--get-user-env`;
- **start something that skips `confine`:** `srun --multi-prog`, heterogeneous jobs.

There are two channels for this: **options** (options.py) and **environment variables** (envclean.py).

### 7a. options.py: allowlist parsing

**Allowlist, not denylist.** `sbatch` has about 150 options, and each SLURM release adds more. A denylist silently allows whatever it forgot, so the broker lists every option it accepts, and **anything not listed is refused**. The tables are compact text ([options.py:36-203](src/agent_slurm/options.py#L36-L203)):

```
name        short  kind   action
time        t      req              <- takes a required value; passed on
output      o      req    output    <- handled specially by the broker
prolog      -      req    refuse    <- always refused, with a reason
overcommit  O      none             <- a flag with no value
```

The *kind* is about the option's **value**, not about whether you must use the option; every option in these tables is optional to use. The kinds copy the C function `getopt_long`, which SLURM uses:

| Kind | `getopt_long` name | Meaning | Examples |
|---|---|---|---|
| `req` | `required_argument` | Must have a value | `--mcs-label=foo`, `--mcs-label foo`, `-t 10`, `-t10` |
| `none` | `no_argument` | A flag; a value is an error | `--hold` (`--hold=1` is refused) |
| `opt` | `optional_argument` | Value allowed, but only attached with `=` | `--exclusive` or `--exclusive=user` |

The kind decides whether the **next** word is consumed ([options.py:241-252](src/agent_slurm/options.py#L241-L252)):

```
sbatch --time 10 job.sh       time is req  -> "10" is its value; job.sh is the script
sbatch --hold job.sh          hold is none -> job.sh is the script
sbatch --nice job.sh          nice is opt  -> no "=", so no value; job.sh is the script
sbatch --nice 5 job.sh        nice is opt  -> "5" is NOT consumed; it becomes the script name
```

The last case is exactly how real `sbatch` behaves, and the broker must match it, or it would think a different file is the script than SLURM does.

**The abbreviation trick.** SLURM, like most `getopt_long` programs, accepts any *unambiguous prefix* of a long option: `--outp=/home/btyeh/.bashrc` means `--output=...`. A check like `"--output" in args` misses it. [parse_options](src/agent_slurm/options.py#L222-L287) demands **exact full names**, so `--outp` is refused as unknown.

**Never forward the caller's raw arguments.** After parsing, the broker rebuilds the arguments as `--name=value` ([canonical](src/agent_slurm/options.py#L290-L292)). This guards against a **parser differential**, two parsers reading the same input differently: if raw text were forwarded, any quirk where SLURM's parser disagrees with Python's would let SLURM act on something the broker never checked. Rebuilding guarantees **SLURM receives exactly what the broker understood**.

Other parsing details:

- `-t 10` and `-t10`, `--time 10` and `--time=10` are all accepted.
- **Combined short flags like `-Hv` are refused** (write them separately); `-vv` is the exception.
- Parsing **stops at the first positional argument**, like real `sbatch`: in `sbatch job.sh --foo`, `--foo` belongs to the script.
- A `refuse` action produces a message with the reason, e.g. `srun: --prolog is refused: the program would run outside the confinement`.

**Value checks** ([check_common_values](src/agent_slurm/options.py#L295-L301)): `--export` only accepts `ALL` or `NONE` (the `VAR=value` form would let the caller inject variables, bypassing envclean), and `--nodelist` must not contain `/` (SLURM treats that as a *file* to read).

**`#SBATCH` lines are options too.** `sbatch` reads options from comment lines at the top of the script. The broker reads them exactly as sbatch does, only the comment block right after `#!` ([sbatch_header_args](src/agent_slurm/options.py#L304-L324)), and puts them through the same parser. In Step 8 the script SLURM actually receives has **no `#SBATCH` lines at all**.

**The small commands.** `scontrol` is allowed only as `scontrol show ...` and `sacctmgr` only as `list`/`show`, so both can only read. `sacct` refuses `--file` in any spelling, including `--fi` (abbreviations again) and any short-flag bundle containing `f`.

### 7b. envclean.py: the environment

**Why the environment is dangerous.** By default `sbatch` copies the caller's environment into the job. On the compute node, three programs then run **before the confinement exists** with that environment: the job's `wrapper.sh` (`sh`), `confine` and bwrap. Some variables make programs load or run code:

| Variable | What it does |
|---|---|
| `LD_PRELOAD`, `LD_LIBRARY_PATH` | Makes *every* program load a chosen shared library, i.e. arbitrary code |
| `BASH_ENV`, `ENV` | The shell runs this file at startup |
| `BASH_FUNC_name%%` | bash imports function definitions from the environment, which can redefine commands |
| `PYTHONPATH`, `PYTHONSTARTUP` | Python imports or runs chosen code (`-I` blocks this for `confine`, not for other Pythons) |
| `GCONV_PATH`, `LOCPATH`, ... | glibc's own "unsafe for privileged programs" list; they load libraries or read files |

`LD_PRELOAD=/project/evil.so` before `sbatch` would load that library into bwrap on the compute node, **outside the confinement**.

**[split_env](src/agent_slurm/envclean.py#L38-L58) splits the environment into `clean` and `restore`.** `clean` goes to the real SLURM command. `restore` holds the code-loading variables, removed and saved in the spool; `confine` sets them again **inside** the confinement with `--setenv` (Step 5c), where they are harmless. Your job still gets the `LD_LIBRARY_PATH` or `PYTHONPATH` your conda environment needs; it just doesn't apply to the programs that build the confinement.

**SLURM's own input variables are dropped and never restored:**

- `SBATCH_*`, `SRUN_*`, `SALLOC_*`: SLURM reads these as **options**; `SBATCH_OUTPUT=~/.bashrc` equals `--output=~/.bashrc`, walking straight around the option parser.
- `SLURM_CONF`: points SLURM at another config file, which can load plugins (code). The broker uses only its *own* `SLURM_CONF`, added back by [trusted()](src/agent_slurm/broker.py#L82-L88).
- other `SLURM_*` variables (e.g. `SLURM_TASK_PROLOG`), except that `srun` keeps the handful identifying its job allocation (`SLURM_JOB_ID`, ...).

**Query mode is stricter still.** Read-only commands like `squeue` run immediately on the login node, outside the confinement, and would also load `LD_PRELOAD`. They get only `HOME`, `USER`, `LOGNAME`, `LANG`, `TZ`, `SLURM_TIME_FORMAT`, `LC_*`, and `PATH=/usr/bin:/bin`.

### Q&A

**Was the option parsing built from memory or by inspecting the installed `sbatch`, `srun`, etc.?** From memory. The repo has no generator script, and the session that wrote options.py confirmed it wrote the tables from what it knew of SLURM's man pages. Three parts were checked against real sources: Snakemake's command lines (from the installed plugin source, with unit tests on those exact lines), the file-reading options (from `--help` of `sacct`, `scontrol`, `sacctmgr`), and real jobs and Snakemake runs in the live tests.

Commit `461bb36` checked the tables against SLURM 25.05.2 and added a test that keeps them honest. `sbatch --help` turned out to be too loosely formatted to trust (it lists `--container` as a flag, but it takes a path), so the test reads the **man pages'** roff source, which spells out `--opt=<value>`, `--opt[=<value>]`, `--opt`, or both a bare and a `=value` form:

- [test_tables_match_man_pages](tests/test_options.py#L179) reads the installed man pages of `sbatch`, `srun`, `scancel`, `scontrol` and `sacctmgr`, and checks every option the broker passes on: it must be documented, with the same short letter and argument kind. Refused options are skipped, since SLURM never sees them. The test skips where the man pages aren't installed.
- Memory got every argument kind right. It went wrong in two ways, both fixed: `sbatch --immediate`, `sbatch --cluster` and `scancel --cluster` aren't documented in 25.05 and are now refused (`--immediate` stays for `srun`, which has it), and `srun --pty` takes an optional file descriptor (`--pty, --pty=<File Descriptor>`), so it is now `opt`. `--pty=N` is safe because N names one of `srun`'s own descriptors, and the broker starts `srun` with only the caller's stdio open.
- Options SLURM has but the tables don't (`--oom-kill-step`, `--tres-bind`, `srun --preserve-env`, ...) are refused as unknown, the safe direction.

**Why a table mistake fails safely.** Because of `canonical()`, SLURM acts on the broker's interpretation, which the broker has already checked. If `--export` were wrongly `req`, `sbatch --export job.sh` would make `job.sh` the value, which `check_common_values` refuses. If an option that takes a value were marked `none`, the broker would fetch the value as the script, and SLURM would reject the bare flag. The result is a confusing error, never an unchecked action outside the confinement.

---

## Step 8: Submitting the job: the spool, `job.json`, `wrapper.sh`

Step 7 checked the options and environment. Now [_sbatch](src/agent_slurm/broker.py#L173-L224) turns the request into a real submission. The guiding idea:

> **SLURM itself is given almost nothing to act on: one fixed two-line script and options it has already checked. Everything the agent chose is recorded in a file and acted on later, inside the confinement.**

### 8a. Get the job script

| Form | How the broker gets the script |
|---|---|
| `sbatch --wrap="cmd"` | Builds the same tiny `#!/bin/sh` script real sbatch would generate ([wrap_script](src/agent_slurm/options.py#L327-L329)) |
| `sbatch < job.sh` or `sbatch` with no script | Reads it from the caller's stdin, fd 0 passed by the stand-in |
| `sbatch job.sh` | **Asks the stand-in to open it**, and reads the fd it sends back |

Why not `open(job.sh)` in the broker? **The broker sees a different file system than the agent does.**

- The agent writes `/tmp/job.sh`. Inside, `/tmp` is the private tmpfs; the broker's `/tmp` is the real, shared one, so it would read the wrong file, or none.
- **The attack:** the agent makes `job.sh` a symlink to `~/.ssh/id_ed25519`. Inside, `~/.ssh` is hidden, so the link leads nowhere. Outside, the broker *can* read your SSH key; it would submit the key as the "job script," and a job running `cat "$0"` could print it into the project.

Letting the stand-in open the file ([broker.py:181-191](src/agent_slurm/broker.py#L181-L191)) resolves the path **with the confinement's view and permissions**: if the agent can't read it, neither can the broker. All three paths stop reading at 4 MiB, SLURM's own limit for batch scripts.

### 8b. Sort the options

```python
options = parse_sbatch_header(script) + cli      # the command line wins, via last()
workdir = normpath(join(req["cwd"], last(options, "chdir") or "."))
passed  = [all options except output, error, input, open-mode, chdir, export, wrap]
```

On the compute node, SLURM's helper process (`slurmstepd`) opens `--output` **as you, outside any confinement**, so `--output=~/.bashrc` would overwrite your login script. SLURM therefore never sees the agent's paths; they go into `job.json`, and Step 9 opens them *inside* the confinement. `--export` comes back only as `ALL` or `NONE`, the working folder is given to SLURM explicitly as `--chdir`, and `--job-name` defaults to the script's file name, as in real sbatch.

### 8c. Write the job folder in the spool

```
~/.local/state/agent-slurm/myproj-3fa9c1e2b4d0/        <- the spool for this project
├── policies/1a2b3c4d5e6f7a8b.json                      <- Step 3's snapshot
└── jobs/20261121-101500-k3j2x9q1/                      <- mkdtemp: unique, mode 0700
    ├── script       (0500)  the job script, byte for byte
    ├── job.json     (0400)  everything to apply later, inside the confinement
    ├── wrapper.sh   (0500)  what SLURM will actually run
    └── slurm-4812.out       SLURM's own log (created by SLURM later)
```

`0500` is owner read and execute; `0400` is read-only. Each file is written with [write_atomic](src/agent_slurm/policy.py#L178-L183) (write a temporary file, then `os.replace` it into place), so a reader never sees a half-written file.

`job.json` ([broker.py:203-213](src/agent_slurm/broker.py#L203-L213)):

```json
{
 "policy": "/home/btyeh/.local/state/agent-slurm/myproj-.../policies/1a2b3c4d5e6f7a8b.json",
 "workdir": "/resnick/groups/.../myproj",
 "stdout": "logs/%x-%j.out",
 "stderr": null,
 "stdin": null,
 "open_mode": "truncate",
 "array": false,
 "args": ["sample1"],
 "env_restore": {"LD_LIBRARY_PATH": "...", "PYTHONPATH": "..."}
}
```

`stdout` is the agent's `--output`, applied in Step 9; `args` are the arguments after the script name; `env_restore` comes from Step 7.

`wrapper.sh` ([broker.py:214-218](src/agent_slurm/broker.py#L214-L218)):

```sh
#!/bin/sh
# Submitted by the agent-slurm broker: the job script runs confined.
exec /home/btyeh/bin/confine --job /home/btyeh/.local/state/agent-slurm/myproj-.../jobs/20261121-101500-k3j2x9q1
```

`shlex.quote` keeps both paths safely quoted, and neither is agent-chosen: one comes from the config, the other from `mkdtemp`.

### 8d. The real `sbatch` call

```python
argv = [real sbatch] + canonical(passed) + [--export=...] + [f"--chdir={workdir}", f"--output={jobdir}/slurm-%j.out", wrapper]
return argv, clean_env, jobdir          # run with cwd = the job folder
```

which produces something like:

```
/usr/bin/sbatch --time=60 --mem=4G --job-name=count_reads --chdir=/resnick/.../myproj \
    --output=/home/btyeh/.local/state/.../jobs/20261121-.../slurm-%j.out \
    /home/btyeh/.local/state/.../jobs/20261121-.../wrapper.sh
```

[_run](src/agent_slurm/broker.py#L269-L278) starts it with the caller's fds as stdio, the cleaned environment and its own session. `Submitted batch job 4812`, or the `--parsable` output Snakemake reads, goes straight to the agent, and the exit code goes back to the stand-in.

### What SLURM ends up holding

- **The script SLURM receives is `wrapper.sh`: two fixed lines and no `#SBATCH` lines.** Header tricks were already parsed by the broker; SLURM never reads them.
- **Every path SLURM writes to is in the spool:** only its own `slurm-%j.out`.
- **SLURM records `Command=<jobdir>/wrapper.sh`.** Only the broker can create files in the spool, so that path *proves* this project's broker submitted the job. Step 10 uses this for `scancel`.

### Why the agent can't tamper with the job folder

The spool sits under `~`, read-only inside every confinement, and `validate` refuses any policy that would make it writable. The folder *does* hold agent-chosen content (the script, output paths, `env_restore`), but all of it is only **used inside a confinement**. The one file that runs unconfined, `wrapper.sh`, is written entirely by the broker. The spool lives on the shared home file system, so the compute node that eventually runs the job can read it.

---

## Step 9: On the compute node: `confine --job` and `jobentry.py`

Later, SLURM picks a compute node for job 4812:

```
slurmstepd (SLURM, as you, UNCONFINED)
  │  env = the cleaned env from Step 7 + SLURM_JOB_ID=4812, SLURM_JOB_NAME=..., ...
  │  cwd = workdir (--chdir);  stdout/stderr -> spool/.../slurm-4812.out
  └─ runs wrapper.sh
       └─ exec confine --job JOBDIR          (still unconfined; sh is replaced by confine)
            ├─ broker threads (this node)
            └─ bwrap ──> python3 -I jobentry.py JOBDIR     (CONFINED from here on)
                           └─ exec script sample1          (your job script)
```

### 9a. The unconfined stretch is as short as possible

Between SLURM starting the job and bwrap creating the confinement, three programs run with your full permissions: `sh` (for `wrapper.sh`), `confine` and bwrap. That is why Step 7 removed code-loading variables, Step 8 kept agent-chosen text out of `wrapper.sh`, and Step 2 runs `confine` as `python3 -I`. `wrapper.sh` uses `exec`, so `sh` is *replaced* by `confine`: SLURM's signals (cancel, time limit) go straight to `confine`, and `confine`'s exit code becomes the job's.

### 9b. `confine --job` loads only from the spool

[launcher.py:105-110](src/agent_slurm/launcher.py#L105-L110):

```python
jobdir = os.path.realpath(argv[1])
job = policy.load_spool_file(cfg, jobdir + "/job.json")   # must be inside the spool
pol = policy.load(cfg, job["policy"])                     # also in the spool; validated again
return run_confined(cfg, pol, job["policy"], [cfg.python, "-I", cfg.jobentry, jobdir], job["env_restore"])
```

Anyone can type `confine --job /some/folder`, including an agent asking you to approve it. Without the check, the agent could write its own `job.json` pointing at its own "policy" with `writable: ["/"]`. [load_spool_file](src/agent_slurm/policy.py#L196-L202) refuses any file whose real path is outside the spool, and `policy.load` applies the same rule to the snapshot and re-runs `validate`. A job can only run under a policy `confine` itself saved.

`run_confined` is the same function as in Step 5, so this node gets its own socket folder in *its* `/dev/shm`, its own broker given the **same policy path** (so jobs this job submits inherit it), and a confinement whose environment includes `env_restore`. Your `LD_LIBRARY_PATH` and friends are back, now only inside; the `SLURM_*` variables SLURM set pass through too.

### 9c. `jobentry.py` does inside what SLURM would have done outside

[jobentry.main](src/agent_slurm/jobentry.py#L55-L74) is the first program **inside** the confinement. It does the three things a batch job normally does before running your script, which the broker withheld from SLURM in Step 8.

**1. Change to the working folder** (`os.chdir(job["workdir"])`; on failure, print an error and exit 1).

**2. Expand SLURM's file-name patterns.** SLURM would turn `logs/%x-%j.out` into `logs/count_reads-4812.out`, but SLURM never saw the path, so [expand](src/agent_slurm/jobentry.py#L19-L44) re-implements the patterns from the `SLURM_*` variables:

| Pattern | Value |
|---|---|
| `%j` | job ID (`SLURM_JOB_ID`) |
| `%x` | job name |
| `%A` / `%a` | array master job ID / array task ID |
| `%u`, `%N` | user, node name |
| `%4j` | zero-padded to 4 digits |
| `%%` | a literal `%` |

The defaults match SLURM's: `slurm-%j.out` (or `slurm-%A_%a.out` for arrays), with stderr going to the same file as stdout.

**3. Open stdin, stdout and stderr, then hand over to the script.**

```python
out_fd = os.open(out, O_WRONLY | O_CREAT | (O_APPEND or O_TRUNC))
...
os.dup2(in_fd, 0); os.dup2(out_fd, 1); os.dup2(err_fd, 2)
os.execv(jobdir + "/script", [script] + job["args"])
```

This is the classic redirection pattern: open, `dup2` onto 0, 1 and 2, `exec`. The opens happen **inside the confinement**, so `--output=~/.bashrc`, or a log path that is a symlink to it, fails with "Read-only file system." The script that runs is the **spool copy**, matching SLURM: editing `job.sh` after submitting doesn't change a queued job.

### 9d. Where output goes: two logs

| Log | Contains |
|---|---|
| Your `--output` file, e.g. `logs/count_reads-4812.out` | everything your script prints |
| `spool/.../jobs/<...>/slurm-4812.out` | messages from SLURM itself (e.g. "CANCELLED ... DUE TO TIME LIMIT") and errors from before your script started (e.g. `agent-slurm: cannot open logs/x.out for the job: ...`) |

If a job "produced no output," look in the spool log first.

### 9e. The end of the job

- **Normal end:** the script exits, so bwrap exits, so `confine` returns the exit code (Step 5e); the node's broker and socket folder are cleaned up, and SLURM records the code.
- **Cancel or time limit:** SLURM sends SIGTERM; `confine` forwards it to the script's process group (Step 5d); anything left after SLURM's grace period gets SIGKILL, and `--die-with-parent` takes the confined processes down with `confine`.

---

## Step 10: `srun`, `scancel`, and jobs that submit jobs

### 10a. `srun`: starting a job step

Inside a running job, `srun my_tool` asks SLURM to launch `my_tool` as a *job step*, possibly on several nodes. Snakemake's SLURM executor relies on this: each rule job runs its real work via `srun ... snakemake --single-rule ...`. On each node, `slurmstepd` starts the task **outside** any confinement; that was problem #1 in Step 1.

[_srun](src/agent_slurm/broker.py#L226-L239):

```python
options, command = parse_srun(args)                       # Step 7's allowlist, SRUN table
clean, restore = envclean.split_env(req["env"], "srun")
stepfile = spool/<key>/steps/<timestamp>-xxxx.json        # {"policy": ..., "env_restore": ...}, mode 0400
argv = [real srun] + canonical(options) + [confine, "--step", stepfile, "--"] + command
```

The command is **rewritten**:

```
agent asked for:   srun -n 4 --mem=2G my_tool x
broker runs:       /usr/bin/srun --ntasks=4 --mem=2G /home/btyeh/bin/confine --step <stepfile> -- my_tool x
```

SLURM launches `confine` on each node instead of `my_tool`. There, [`confine --step`](src/agent_slurm/launcher.py#L111-L117) loads the step file and policy (both must be in the spool, as in Step 9), builds a confinement with its own broker, and runs `my_tool` inside. No `jobentry.py` is needed: `srun` carries the tasks' stdio back to itself, and from there into the caller's fds.

Differences from `sbatch`:

- **`--output/--error/--input` are refused**, not emulated: `slurmstepd` would open them outside, and there is no `jobentry` step to reroute them. Redirect inside the command instead (`srun ... sh -c 'my_tool > out.txt'`).
- **Also refused:** `--prolog/--epilog/--task-prolog/--task-epilog` (run programs outside), `--bcast/--send-libs` (copy files outside), and `--multi-prog` (its config file lists a program per task that the broker can't wrap).
- **The environment** keeps the few `SLURM_*` variables that tell `srun` which allocation it belongs to; otherwise `srun` would try to create a new job.
- **`--jobid=N`** attaches the step to an existing job. The step would still run confined, but inside, and using the resources of, a job that might be yours rather than the agent's (e.g. your interactive session), so it follows the same ownership rule as `scancel`.

### 10b. `scancel`: own jobs only

An unrestricted `scancel` would let an agent cancel *your* jobs. [_scancel](src/agent_slurm/broker.py#L241-L253) accepts only explicit job IDs: filters like `-u btyeh` or `--name` aren't in the table, and each ID must look like one (`123`, `123_4`, `123.0`, ...). Each ID then gets an ownership check, [_owner](src/agent_slurm/broker.py#L255-L267):

```python
result = subprocess.run([scontrol, "show", "job", "-o", job], env=<query env>, capture_output=True)
commands = re.findall(r"Command=(\S*)", result.stdout)
"own"     if every Command= starts with  spool/<this project_key>/jobs/
"unknown" if scontrol failed or found nothing      (job already finished, so it's skipped)
"foreign" otherwise                                (the whole request is refused; nothing is cancelled)
```

This is the payoff from Step 8: SLURM records each job's script path, and for broker-submitted jobs that is `spool/<key>/jobs/<...>/wrapper.sh`, which only the broker can create.

| Job | Its `Command=` | Result |
|---|---|---|
| submitted by an agent in this project | `.../agent-slurm/myproj-3fa9.../jobs/.../wrapper.sh` | cancelled |
| your own `sbatch job.sh` | `/resnick/.../job.sh` | refused |
| another project's agent job | `.../agent-slurm/otherproj-81c0.../jobs/...` | refused |

For an array job, `scontrol` prints one record per task, and **all** must match. Step IDs (`123.0`) are checked as their parent job via [job_query_id](src/agent_slurm/options.py#L386-L390). The only cluster option is `-M/--clusters`, which `_owner` passes on to `scontrol`.

### 10c. Jobs that submit jobs

The README's pattern for a long pipeline submits the Snakemake driver itself as a job:

```
LOGIN NODE      confine sbatch driver.sh
                  └ broker A ──> job 100 = wrapper.sh               (policy P, snapshot S)

NODE n1         confine --job 100  +  broker B (policy S)
                  └ [inside] snakemake driver
                       ├ sbatch rule_a ──> broker B ──> job 101     (policy S)
                       └ sbatch rule_b ──> broker B ──> job 102     (policy S)

NODE n2         confine --job 101  +  broker C (policy S)
                  └ [inside] snakemake --single-rule a
                       └ srun ... ──> broker C ──> srun confine --step ... (policy S)

NODES n2,n3     confine --step  +  broker D, E (policy S)
                  └ [inside] the rule's actual command
```

Four properties hold at every level:

1. **Every process the agent's work starts is inside a confinement**, on whatever node it runs.
2. **Every SLURM call goes through that level's broker**, which hands the next level the same policy snapshot.
3. **Nothing keeps running between levels.** Each broker is threads in its own `confine` and ends with it; the login node's broker is gone long before job 102 starts.
4. **Agent-chosen paths and text are only acted on inside a confinement**, while SLURM acts only on spool paths and checked options.

---

## Possible extensions

Open items that came up during the walkthrough. None is an escape from the confinement; each tightens an edge.

**Protect `.git/hooks` and `.git/config` in nested repositories** (Step 4). Protection covers only the top level of each writable folder, so in a nested repository or submodule (`myproj/sub/.git/hooks/`) the agent can plant a hook that runs when you next commit there outside the confinement. `build` could walk the writable folders for nested `.git` directories and add their `hooks` and `config` to `protect`. The cost is the walk itself, which can be large for scratch folders; limiting it to the project root, or to a fixed depth, keeps it cheap. Until then, treat nested repositories in agent-writable folders with suspicion.

**Queue signals that arrive before bwrap reports the command's pid** (Step 5). Today an early signal turns into a hard kill (window B), or kills `confine` before its handlers exist (window A). Installing the handlers before `Popen` and queueing signals until the pid is known closes both, in about 15 lines:

```python
child, pending = {}, collections.deque()

def deliver():
    """Send queued signals once the command's pid is known (called from both threads)."""
    pid = child.get("child-pid")
    while pid:
        try:
            signum = pending.popleft()
        except IndexError:
            return
        try:
            os.killpg(pid, signum)
        except OSError:
            pass

def forward(signum, frame):
    pending.append(signum)
    deliver()

for s in FORWARDED:                 # now *before* Popen: closes window A
    signal.signal(s, forward)
proc = subprocess.Popen(...)
...
def read_info():
    ...  child.update(json.loads(...))
    deliver()                       # flush anything that arrived early: closes window B
```

Installing before `Popen` is fine because `exec` resets caught signals to their defaults, so bwrap and the command don't inherit `confine`'s handlers. No signal slips through between the two threads: the reader sets the pid *before* draining, the handler appends *before* checking, and `deque.popleft()` is atomic. The trade-off: if bwrap stalled during setup and never reported a pid, Ctrl-C would wait in the queue (`kill -9` on `confine` still works, and `--die-with-parent` cleans up). A local test could send SIGTERM immediately after start and check that the command's trap ran.

**Make `already_confined()` recognize only `confine`'s own bubble** (Step 5). Any user namespace, such as an unprivileged Apptainer container, currently counts as "already confined," and `confine` would then run the command with no confinement. The function could also require a marker only `confine` sets up, for example `AGENT_SLURM_PROJECT` in the environment together with the socket at `/tmp/.agent-slurm.sock`, and refuse with an error when it is in a user namespace without them. The environment variable alone isn't enough, since anyone can set it; the socket check ties the marker to the bind mount that only bwrap creates.

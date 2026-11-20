"""Allowlists and parsers for the SLURM command lines the broker accepts.

The broker never passes the caller's raw arguments to sbatch or srun. It parses them here, refuses anything not
listed, and passes on only the canonical `--name=value` form of what it understood. Long options must be written out
in full: SLURM itself accepts unambiguous abbreviations (`--outp` for `--output`), which is how simple stand-in checks
are fooled.
"""
import re
import shlex

REQ, NONE, OPT = "required", "none", "optional"


class Refused(Exception):
    """A request the broker will not run. The message is shown to the caller."""


def _table(spec):
    """Build {long name: (short letter or None, argument kind, action)} from a compact multi-line spec.

    Each line: `name short kind [action]`, with `-` for no short letter. Actions: `pass` (default), `refuse`, or a
    keyword that the caller handles itself.
    """
    table = {}
    for line in spec.strip().splitlines():
        fields = line.split()
        if not fields:
            continue
        name, short, kind = fields[:3]
        action = fields[3] if len(fields) > 3 else "pass"
        table[name] = (None if short == "-" else short, {"req": REQ, "none": NONE, "opt": OPT}[kind], action)
    return table


# Options shared by sbatch and srun that only request resources or scheduling.
_COMMON = """
account A req
acctg-freq - req
begin b req
chdir D req chdir
cluster-constraint - req
clusters M req
comment - req
constraint C req
contiguous - none
core-spec S req
cores-per-socket - req
cpu-freq - req
cpus-per-gpu - req
cpus-per-task c req
deadline - req
delay-boot - req
dependency d req
distribution m req
exclude x req
exclusive - opt
export - req export
extra - req
extra-node-info B req
gpu-bind - req
gpu-freq - req
gpus G req
gpus-per-node - req
gpus-per-socket - req
gpus-per-task - req
gres - req
gres-flags - req
hint - req
immediate I opt
job-name J req
licenses L req
mail-type - req
mail-user - req
mcs-label - req
mem - req
mem-bind - req
mem-per-cpu - req
mem-per-gpu - req
mincpus - req
nice - opt
no-kill k opt
nodelist w req nodelist
nodes N req
ntasks n req
ntasks-per-core - req
ntasks-per-gpu - req
ntasks-per-node - req
ntasks-per-socket - req
overcommit O none
oversubscribe s none
partition p req
prefer - req
priority - req
propagate - opt
qos q req
quiet Q none
reboot - none
reservation - req
signal - req
sockets-per-node - req
spread-job - none
switches - req
thread-spec - req
threads-per-core - req
time t req
time-min - req
tmp - req
tres-per-task - req
use-min-nodes - none
verbose v none
wckey - req
bb - req refuse
bbf - req refuse
container - req refuse
container-id - req refuse
get-user-env - opt refuse
gid - req refuse
network - req refuse
profile - req refuse
uid - req refuse
"""

SBATCH = _table(_COMMON + """
array a req
batch - req
cluster - req
error e req error
export-file - req refuse
hold H none
ignore-pbs - none
input i req input
kill-on-invalid-dep - req
no-requeue - none
nodefile F req refuse
open-mode - req open_mode
output o req output
parsable - none
requeue - none
test-only - none
wait W none
wait-all-nodes - req
wrap - req wrap
""")

SRUN = _table(_COMMON + """
cpu-bind - req
exact - none
jobid - req jobid
kill-on-bad-exit K opt
label l none
mpi - req
overlap - none
pty - none
unbuffered u none
wait W req
bcast - opt refuse
bcast-exclude - req refuse
epilog - req refuse
error e req refuse
external-launcher - none refuse
het-group - req refuse
input i req refuse
multi-prog - none refuse
no-allocate Z none refuse
open-mode - req refuse
output o req refuse
prolog - req refuse
send-libs - opt refuse
task-epilog - req refuse
task-prolog - req refuse
x11 - opt refuse
""")

SCONTROL = _table("""
all a none
clusters M req
details d none
json - opt
oneliner o none
quiet Q none
verbose v none
yaml - opt
""")

SACCTMGR = _table("""
associations s none
immediate i none
noheader n none
parsable p none
parsable2 P none
quiet Q none
readonly r none
verbose v none
""")

SCANCEL = _table("""
batch b none
cluster - req
clusters M req
full f none
quiet Q none
signal s req
verbose v none
""")

# Why each refused option is refused, for the error message.
REFUSE_REASONS = {
    "get-user-env": "it loads your login environment outside the confinement",
    "uid": "it changes the user", "gid": "it changes the group",
    "export-file": "it reads a file outside the confinement",
    "nodefile": "it reads a file outside the confinement",
    "prolog": "the program would run outside the confinement",
    "epilog": "the program would run outside the confinement",
    "task-prolog": "the program would run unconfined on the compute node",
    "task-epilog": "the program would run unconfined on the compute node",
    "output": "SLURM would write the file outside the confinement; redirect inside the command instead",
    "error": "SLURM would write the file outside the confinement; redirect inside the command instead",
    "input": "SLURM would read the file outside the confinement; redirect inside the command instead",
    "bcast": "it copies files outside the confinement",
    "multi-prog": "its programs would not start under confine",
}


def parse_options(argv, table, command, interleaved=False):
    """Parse `argv` against `table`.

    Returns (options, positionals), with options as a list of (name, value or None). Parsing stops at the first
    positional argument or `--`, as in sbatch and srun, unless `interleaved` (scancel accepts options after job IDs).
    """
    shorts = {spec[0]: name for name, spec in table.items() if spec[0]}
    options, positionals = [], []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positionals.extend(argv[i + 1:])
            break
        if arg.startswith("--"):
            name, has_value, value = arg[2:].partition("=")
            if name not in table:
                raise Refused(f"{command}: unknown option --{name} (options must be written out in full; "
                              "abbreviations and unlisted options are refused)")
            kind = table[name][1]
            if kind == NONE:
                if has_value:
                    raise Refused(f"{command}: --{name} takes no value")
                value = None
            elif kind == OPT:
                value = value if has_value else None
            elif not has_value:
                i += 1
                if i == len(argv):
                    raise Refused(f"{command}: --{name} needs a value")
                value = argv[i]
            options.append((name, value))
        elif arg.startswith("-") and len(arg) > 1:
            if set(arg[1:]) == {"v"} and "verbose" in table:   # -vv
                options.extend(("verbose", None) for _ in arg[1:])
                i += 1
                continue
            name = shorts.get(arg[1])
            if name is None:
                raise Refused(f"{command}: unknown option {arg[:2]}")
            kind, rest = table[name][1], arg[2:]
            if kind == NONE:
                if rest:
                    raise Refused(f"{command}: combined short options like {arg} are refused; write each separately")
                value = None
            elif kind == OPT:
                value = rest or None
            elif rest:
                value = rest
            else:
                i += 1
                if i == len(argv):
                    raise Refused(f"{command}: -{arg[1]} needs a value")
                value = argv[i]
            options.append((name, value))
        elif interleaved:
            positionals.append(arg)
        else:
            positionals.extend(argv[i:])
            break
        i += 1
    for name, _ in options:
        if table[name][2] == "refuse":
            reason = REFUSE_REASONS.get(name, "it is not supported in the confinement")
            raise Refused(f"{command}: --{name} is refused: {reason}")
    return options, positionals


def canonical(options):
    """Options as `--name=value` / `--name` arguments."""
    return [f"--{name}" if value is None else f"--{name}={value}" for name, value in options]


def check_common_values(options, command):
    for name, value in options:
        if name == "export" and value not in ("ALL", "NONE"):
            raise Refused(f"{command}: only --export=ALL or --export=NONE is accepted (got {value!r}); "
                          "set variables in the job script instead")
        if name == "nodelist" and "/" in value:
            raise Refused(f"{command}: --nodelist must list nodes, not name a file")


def sbatch_header_args(script):
    """Arguments from the `#SBATCH` lines of a job script, read the way sbatch does.

    Only the block of comment and blank lines after the `#!` line counts; the first other line ends it.
    """
    lines = script.splitlines()
    if not lines or not lines[0].startswith("#!"):
        raise Refused("sbatch: the job script must start with a #! line")
    args = []
    for line in lines[1:]:
        if line.startswith("#SBATCH"):
            rest = line[len("#SBATCH"):]
            if rest and not rest[0].isspace():
                raise Refused(f"sbatch: unrecognized directive {line!r}")
            try:
                args.extend(shlex.split(rest, comments=True))
            except ValueError as e:
                raise Refused(f"sbatch: cannot parse {line!r}: {e}")
        elif line.strip() and not line.lstrip().startswith("#"):
            break
    return args


def wrap_script(command):
    """The job script sbatch itself generates for --wrap."""
    return f"#!/bin/sh\n# This script was created by sbatch --wrap.\n\n{command}\n"


def parse_sbatch(argv):
    """Command-line part of an sbatch request: (options, script path or None, script arguments).

    The script path is None for --wrap. `/dev/stdin` (or no script at all) means the script arrives on stdin.
    """
    options, positionals = parse_options(argv, SBATCH, "sbatch")
    check_common_values(options, "sbatch")
    wrap = any(name == "wrap" for name, _ in options)
    if wrap:
        if positionals:
            raise Refused("sbatch: --wrap cannot be combined with a script")
        return options, None, []
    if not positionals:
        return options, "/dev/stdin", []
    if positionals[0] == ":":
        raise Refused("sbatch: heterogeneous jobs are refused")
    return options, positionals[0], positionals[1:]


def parse_sbatch_header(script):
    options, positionals = parse_options(sbatch_header_args(script), SBATCH, "sbatch (#SBATCH line)")
    if positionals:
        raise Refused(f"sbatch: unexpected #SBATCH argument {positionals[0]!r} (heterogeneous jobs are refused)")
    check_common_values(options, "sbatch (#SBATCH line)")
    if any(name == "wrap" for name, _ in options):
        raise Refused("sbatch: --wrap cannot be used in an #SBATCH line")
    return options


def parse_srun(argv):
    """(options, command) of an srun request."""
    options, command = parse_options(argv, SRUN, "srun")
    check_common_values(options, "srun")
    if not command:
        raise Refused("srun: no command given")
    if ":" in command[:1]:
        raise Refused("srun: heterogeneous steps are refused")
    return options, command


JOB_ID = re.compile(r"^(\d+)(_(\d+|\[[0-9,\-%]+\]))?(\.(\d+|batch|extern))?$")


def parse_scancel(argv):
    """(options, job IDs) of an scancel request. Filters such as --user or --name are refused: only explicit IDs."""
    options, ids = parse_options(argv, SCANCEL, "scancel", interleaved=True)
    if not ids:
        raise Refused("scancel: give the job IDs to cancel (filters such as --user or --name are refused)")
    for job in ids:
        if not JOB_ID.match(job):
            raise Refused(f"scancel: not a job ID: {job!r}")
    return options, ids


def job_query_id(job):
    """The ID to look up for an scancel target: steps belong to their job, a bracketed array range to its array."""
    m = JOB_ID.match(job)
    base, task = m.group(1), m.group(3)
    return f"{base}_{task}" if task and task.isdigit() else base


def check_scontrol(argv):
    _, rest = parse_options(argv, SCONTROL, "scontrol")
    if not rest or rest[0] != "show":
        raise Refused("scontrol: only 'scontrol show ...' is allowed in the confinement")


def check_sacctmgr(argv):
    _, rest = parse_options(argv, SACCTMGR, "sacctmgr")
    if not rest or rest[0] not in ("list", "show"):
        raise Refused("sacctmgr: only 'sacctmgr list|show ...' is allowed in the confinement")


def check_sacct(argv):
    for arg in argv:
        if arg.startswith("--fi") or (arg.startswith("-") and not arg.startswith("--") and "f" in arg):
            raise Refused("sacct: --file is refused (it reads a file outside the confinement)")

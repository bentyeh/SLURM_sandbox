"""Option allowlists and parsers (pure Python)."""
import gzip
import os
import re
import shlex

import pytest

from agent_slurm.options import (NONE, OPT, REQ, SACCTMGR, SBATCH, SCANCEL, SCONTROL, SRUN, Refused, canonical,
                                 check_sacct, check_sacctmgr, check_scontrol, job_query_id, parse_sbatch,
                                 parse_sbatch_header, parse_scancel, parse_srun, wrap_script)


def test_sbatch_forms_and_canonical_output():
    opts, script, args = parse_sbatch(["--mem", "4G", "-c2", "-J", "x", "--hold", "job.sh", "a", "--mem=1"])
    assert opts == [("mem", "4G"), ("cpus-per-task", "2"), ("job-name", "x"), ("hold", None)]
    assert (script, args) == ("job.sh", ["a", "--mem=1"])   # options after the script are the script's
    assert canonical(opts) == ["--mem=4G", "--cpus-per-task=2", "--job-name=x", "--hold"]


def test_sbatch_stdin_and_wrap():
    assert parse_sbatch(["--parsable"])[1] == "/dev/stdin"
    assert parse_sbatch(["/dev/stdin"])[1] == "/dev/stdin"
    opts, script, _ = parse_sbatch(["--wrap=echo hi"])
    assert script is None and opts == [("wrap", "echo hi")]
    assert wrap_script("echo hi").startswith("#!/bin/sh\n") and "echo hi\n" in wrap_script("echo hi")
    with pytest.raises(Refused):
        parse_sbatch(["--wrap=echo", "job.sh"])


# The command lines snakemake-executor-plugin-slurm 2.8.0 and -jobstep 0.6.1 build.
SNAKEMAKE_SBATCH = [
    "sbatch --parsable --job-name 'b2c3-uuid' --output '/p/.snakemake/slurm_logs/rule_a/%j.log' --export=ALL "
    "--comment 'rule_a_wildcards_x' -A 'acct' -p 'expansion' -t 10 -C 'skylake' --qos=normal --mem 1000 "
    "--ntasks=1 --cpus-per-task=2 -D '/p' /dev/stdin",
    "sbatch --parsable --job-name u --output l --export=ALL --comment c --clusters 'central' --mem-per-cpu 100 "
    "--nodes=1 --requeue --reservation=r --exclude=hpc-1,hpc-2 --ntasks-per-gpu=1 --gres=gpu:1 -D /p "
    "--array=1-3 --wrap=\"python -m snakemake --target-jobs 'a:x=1' --slurm-jobstep-array-execs 'eyJ9'\"",
]
SNAKEMAKE_SRUN = ["srun -n1 --cpu-bind=q  --cpus-per-task=2  --gpus=1  --nodes=1  sh -s",
                  "srun -n1 --cpu-bind=q  --cpus-per-gpu=4    python -m snakemake --target-jobs 'a:x=1'"]


@pytest.mark.parametrize("line", SNAKEMAKE_SBATCH)
def test_snakemake_sbatch_accepted(line):
    parse_sbatch(shlex.split(line)[1:])


@pytest.mark.parametrize("line", SNAKEMAKE_SRUN)
def test_snakemake_srun_accepted(line):
    opts, command = parse_srun(shlex.split(line)[1:])
    assert command[0] in ("sh", "python")
    assert ("cpu-bind", "q") in opts


@pytest.mark.parametrize("argv", [
    ["--outp=/home/u/.bashrc", "j.sh"],        # abbreviation of --output
    ["--chd=/x", "j.sh"],                       # abbreviation of --chdir
    ["--wra=echo"],                             # abbreviation of --wrap
    ["-vo", "/x", "j.sh"],                      # clustered short options
    ["-Hv", "j.sh"],
    ["--foo=1", "j.sh"],
    ["--get-user-env", "j.sh"],
    ["--uid=0", "j.sh"],
    ["--export-file=/x", "j.sh"],
    ["--nodefile=/x", "j.sh"],
    ["--export=ALL,LD_PRELOAD=/x.so", "j.sh"],
    ["--export=BASH_ENV=/x", "j.sh"],
    ["--nodelist=/home/u/nodes", "j.sh"],
    ["--container=/x", "j.sh"],
    ["--bb=x", "j.sh"],
    ["--mem"],
    [":", "j.sh"],
])
def test_sbatch_refusals(argv):
    with pytest.raises(Refused):
        parse_sbatch(argv)


def test_verbose_repeat_allowed():
    assert parse_sbatch(["-vv", "j.sh"])[0] == [("verbose", None), ("verbose", None)]


def test_sbatch_header():
    script = "\n".join([
        "#!/bin/bash",
        "#SBATCH --job-name=count  # comment",
        '#SBATCH --output="logs/a b %j.out"',
        "",
        "# an ordinary comment",
        "#SBATCH -c 4",
        "echo start",
        "#SBATCH --mem=999G",        # after the first command: ignored, as sbatch does
    ])
    assert parse_sbatch_header(script) == [("job-name", "count"), ("output", "logs/a b %j.out"),
                                           ("cpus-per-task", "4")]


@pytest.mark.parametrize("script", [
    "echo no shebang\n",
    "#!/bin/sh\n#SBATCH --outp=/home/u/x\n",
    "#!/bin/sh\n#SBATCH --get-user-env\n",
    "#!/bin/sh\n#SBATCH hetjob\n",
    "#!/bin/sh\n#SBATCH --wrap=echo\n",
    "#!/bin/sh\n#SBATCHX --mem=1\n",
    "#!/bin/sh\n#SBATCH --output='unclosed\n",
    "#!/bin/sh\n#SBATCH --export=ALL,LD_PRELOAD=/x\n",
])
def test_sbatch_header_refusals(script):
    with pytest.raises(Refused):
        parse_sbatch_header(script)


@pytest.mark.parametrize("argv", [
    ["--task-prolog=/x", "sh"], ["--task-epilog=/x", "sh"], ["--prolog=/x", "sh"], ["--epilog=/x", "sh"],
    ["--output=/x", "sh"], ["-o", "/x", "sh"], ["-e/x", "sh"], ["--input=/x", "sh"], ["--bcast", "sh"],
    ["--send-libs", "sh"], ["--multi-prog", "conf"], ["-Z", "sh"], ["--x11", "sh"], ["--export=ALL,X=1", "sh"],
    ["-n1"], [":", "sh"], ["--outp=/x", "sh"],
])
def test_srun_refusals(argv):
    with pytest.raises(Refused):
        parse_srun(argv)


def test_scancel():
    opts, ids = parse_scancel(["123", "456_7", "--clusters=all", "789.batch"])
    assert ids == ["123", "456_7", "789.batch"] and opts == [("clusters", "all")]
    for argv in (["-u", "btyeh"], ["--name=x"], ["--state=PENDING", "1"], ["abc"], []):
        with pytest.raises(Refused):
            parse_scancel(argv)
    assert [job_query_id(j) for j in ("12", "12_3", "12_[1-3]", "12.batch", "12_3.0")] == \
        ["12", "12_3", "12", "12", "12_3"]


def test_read_only_command_checks():
    check_scontrol(["show", "config"])
    check_scontrol(["-o", "show", "job", "1"])
    check_sacctmgr(["-n", "-s", "list", "user", "u", "format=account%256"])
    check_sacct(["-X", "--parsable2", "--clusters", "all", "--noheader", "--format=JobIdRaw,State",
                 "--starttime", "2026-11-17T14:00", "--endtime", "now", "--name", "u"])
    for check, argv in [(check_scontrol, ["update", "job=1", "StdOut=/x"]), (check_scontrol, ["write",
                        "batch_script", "1", "/x"]), (check_scontrol, []), (check_sacctmgr, ["add", "user"]),
                        (check_sacctmgr, ["dump", "central", "file=/x"]), (check_sacct, ["--file=/x"]),
                        (check_sacct, ["-f", "/x"]), (check_sacct, ["-Xf/x"])]:
        with pytest.raises(Refused):
            check(argv)


MAN_DIR = "/usr/share/man/man1"
# An option's entry in the roff source of a SLURM man page: a ".TP" line, then a header such as
# `\fB\-A\fR, \fB\-\-account\fR=<...>`, `\fB\-H, \-\-hold\fR`, `\fB\-\-no\-kill\fR[=off]` or
# `\f3\-\-json\fP, \f3\-\-json\fP=\fIlist\fR` (bold is \fB or \f3, back to normal \fR or \fP).
MAN_HEADER = re.compile(r"\\f[B3](?:\\-(\w)(?:\\f[RP])?, (?:\\f[B3])?)?\\-\\-([\w\\-]+?)\\f[RP]")


def documented_options(command):
    """{long name: (short letter or None, argument kind)} from the installed man page of `command`."""
    path = os.path.join(MAN_DIR, f"{command}.1.gz")
    if not os.path.exists(path):
        pytest.skip(f"{path} is not installed")
    with gzip.open(path, "rt", errors="replace") as f:
        lines = f.read().splitlines()
    found = {}
    for prev, line in zip(lines, lines[1:]):
        m = MAN_HEADER.match(line) if prev.startswith(".TP") else None
        if not m:
            continue
        short, raw = m.groups()
        # every form the header shows: `--json, --json=list` means the value is optional, like `--no-kill[=off]`
        values = set(re.findall(r"\\-\\-" + re.escape(raw) + r"\\f[RP](\[?=)?", line))
        kind = OPT if "[=" in values or values == {"", "="} else REQ if values == {"="} else NONE
        # the first entry is the option's own; later ones describe values, as `--export=NONE` does
        found.setdefault(raw.replace("\\-", "-"), (short, kind))
    return found


@pytest.mark.parametrize("command, table", [("sbatch", SBATCH), ("srun", SRUN), ("scancel", SCANCEL),
                                            ("scontrol", SCONTROL), ("sacctmgr", SACCTMGR)])
def test_tables_match_man_pages(command, table):
    """Every option the broker passes on is one the installed SLURM documents, with the same short letter and
    argument kind. Refused options are left out: they are refused whatever SLURM would make of them."""
    documented = documented_options(command)
    problems = []
    for name, (short, kind, action) in sorted(table.items()):
        if action == "refuse":
            continue
        if name not in documented:
            problems.append(f"--{name}: not in man {command}")
        elif documented[name] != (short, kind):
            problems.append(f"--{name}: table has {(short, kind)}, man {command} has {documented[name]}")
    assert not problems, "\n".join(problems)

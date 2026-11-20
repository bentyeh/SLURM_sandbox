"""Option allowlists and parsers (pure Python)."""
import shlex

import pytest

from agent_slurm.options import (Refused, canonical, check_sacct, check_sacctmgr, check_scontrol, job_query_id,
                                 parse_sbatch, parse_sbatch_header, parse_scancel, parse_srun, wrap_script)


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

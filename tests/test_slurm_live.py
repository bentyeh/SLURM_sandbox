"""Live tests: small real jobs on the cluster (5 minutes, 200-500 MB, 1 CPU each). Run with ./tests/run.sh slurm."""
import os
import subprocess

import pytest

from conftest import World, assert_ok, assert_refused

pytestmark = pytest.mark.slurm

RESOURCES = "#SBATCH --time=5\n#SBATCH --mem=200M\n#SBATCH --cpus-per-task=1\n"
CHECKS = """
hostname
touch "$OUTSIDE/{tag}" 2>/dev/null || echo {tag}-outside-refused
touch "$HOME/.agent-slurm-canary" 2>/dev/null || echo {tag}-home-refused
munge -n >/dev/null 2>&1 || echo {tag}-munge-refused
scrontab -l >/dev/null 2>&1 || echo {tag}-scrontab-refused
head -c 300 /usr/bin/sbatch | grep -q "Stand-in for SLURM" && echo {tag}-standin
"""
TIMEOUT = 1800   # includes queue time


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path, real_slurm=True)
    world.env = {"OUTSIDE": str(world.outside)}
    yield world
    canary = os.path.expanduser("~/.agent-slurm-canary")
    assert not os.path.exists(canary)


def run(w, *command, **kw):
    kw.setdefault("env", w.env)
    kw.setdefault("timeout", TIMEOUT)
    return w.confine(*command, **kw)


def expect_checks(text, tag):
    for check in ("outside-refused", "home-refused", "munge-refused", "scrontab-refused", "standin"):
        assert f"{tag}-{check}" in text, f"{tag}-{check} missing from:\n{text}"


def test_job_nested_job_and_step_are_confined(w):
    (w.project / "inner.sh").write_text("#!/bin/sh\n" + RESOURCES + "#SBATCH --output=logs/inner-%j.out\n"
                                        + CHECKS.format(tag="inner"))
    (w.project / "job.sh").write_text("#!/bin/sh\n" + RESOURCES + "#SBATCH --output=logs/outer-%j.out\n"
                                      + CHECKS.format(tag="outer") + """
srun -n1 sh -c 'touch "$OUTSIDE/step" 2>/dev/null || echo step-outside-refused; echo step-on-$(hostname)'
sbatch --wait inner.sh && echo inner-done
""")
    assert_ok(run(w, "sbatch", "--wait", "job.sh"))
    outer = "".join(p.read_text() for p in (w.project / "logs").glob("outer-*.out"))
    inner = "".join(p.read_text() for p in (w.project / "logs").glob("inner-*.out"))
    expect_checks(outer, "outer")
    expect_checks(inner, "inner")
    assert "step-outside-refused" in outer and "step-on-" in outer and "inner-done" in outer
    assert list(w.outside.iterdir()) == []


def test_scancel_own_jobs_only(w):
    hold = ["--parsable", "--hold", "--time=1", "--mem=100M", "--wrap=true"]
    own = assert_ok(run(w, "sbatch", *hold)).stdout.strip()
    foreign = subprocess.run(["/usr/bin/sbatch", *hold], capture_output=True, text=True, check=True).stdout.strip()
    try:
        assert_refused(run(w, "scancel", foreign), "not submitted by an agent in this project")
        assert_ok(run(w, "scancel", own))
        state = subprocess.run(["/usr/bin/scontrol", "show", "job", "-o", own], capture_output=True, text=True)
        assert "JobState=CANCELLED" in state.stdout
        state = subprocess.run(["/usr/bin/scontrol", "show", "job", "-o", foreign], capture_output=True, text=True)
        assert "JobState=PENDING" in state.stdout
    finally:
        subprocess.run(["/usr/bin/scancel", own, foreign])


SNAKEFILE = """
import os
OUTSIDE = os.environ["OUTSIDE"]

def try_write(path):
    try:
        open(path, "w").close()
        return "written"
    except OSError as e:
        return e.strerror

TOP = try_write(os.path.join(OUTSIDE, "snakefile_top"))

rule all:
    input: "out/shell.txt", "out/run.txt"

rule shell_rule:
    output: "out/shell.txt"
    params: outside=OUTSIDE, top=TOP
    shell: 'hostname > {output}; touch "{params.outside}/shell" 2>> {output} || true; echo "top: {params.top}" >> {output}'

rule run_rule:
    output: "out/run.txt"
    run:
        with open(output[0], "w") as f:
            f.write("run: " + try_write(os.path.join(OUTSIDE, "run")) + "\\n")
"""


@pytest.mark.parametrize("as_script", [False, True], ids=["wrap", "pass-command-as-script"])
def test_snakemake_slurm_executor(w, as_script):
    (w.project / "Snakefile").write_text(SNAKEFILE)
    command = [os.environ.get("CONDA_EXE", "conda"), "run", "--no-capture-output", "-n", "snakemake9",
               "snakemake", "--executor", "slurm", "--jobs", "2", "--latency-wait", "60", "--no-hooks",
               "--default-resources", "runtime=5", "mem_mb=500"]
    if as_script:
        command.append("--slurm-pass-command-as-script")
    r = run(w, *command)
    assert r.returncode == 0, f"stdout:\n{r.stdout[-5000:]}\nstderr:\n{r.stderr[-5000:]}"
    shell = (w.project / "out" / "shell.txt").read_text()
    assert "Read-only file system" in shell and "top: Read-only file system" in shell
    assert (w.project / "out" / "run.txt").read_text() == "run: Read-only file system\n"
    assert list(w.outside.iterdir()) == []
    assert len(list(w.jobs_dir().iterdir())) == 2   # both rule jobs went through the broker

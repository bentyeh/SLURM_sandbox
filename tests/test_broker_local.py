"""The broker, with a fake SLURM that runs batch jobs synchronously on this node. Run outside Claude's sandbox.

Each escape route from the README design is reproduced here as an attack that must fail.
"""
import os
import shutil
import subprocess

import pytest

from conftest import assert_ok, assert_refused

pytestmark = pytest.mark.local

JOB = """#!/bin/sh
#SBATCH --job-name=count
#SBATCH --output=logs/%x_%j.out
#SBATCH --mem=1G
echo "job $SLURM_JOB_ID in $(pwd)"
echo hello > result.txt
touch "$OUTSIDE/job" 2>/dev/null || echo "outside refused"
"""


@pytest.fixture
def w(world):
    (world.project / "job.sh").write_text(JOB)
    world.env = {"OUTSIDE": str(world.outside)}
    return world


def run(w, *command, **kw):
    kw.setdefault("env", w.env)
    return w.confine(*command, **kw)


def test_sbatch_job_runs_confined(w):
    r = assert_ok(run(w, "sbatch", "job.sh"))
    assert r.stdout.strip() == "Submitted batch job 1001"
    log = (w.project / "logs" / "count_1001.out").read_text()
    assert f"job 1001 in {os.path.realpath(w.project)}" in log and "outside refused" in log
    assert (w.project / "result.txt").read_text() == "hello\n"
    assert not (w.outside / "job").exists()

    call = w.calls("sbatch")[0]
    jobs_dir = str(w.jobs_dir())
    wrapper = call["argv"][-1]
    assert wrapper.startswith(jobs_dir + "/") and wrapper.endswith("/wrapper.sh")
    assert "--job-name=count" in call["argv"] and "--mem=1G" in call["argv"]
    assert f"--chdir={os.path.realpath(w.project)}" in call["argv"]
    assert [a for a in call["argv"] if a.startswith("--output=")] == \
        [f"--output={os.path.dirname(wrapper)}/slurm-%j.out"]   # SLURM itself writes only into the spool
    assert "#SBATCH" not in call["script"] and "--job" in call["script"]


def test_sbatch_stdin_as_snakemake_does(w):
    r = assert_ok(run(w, "sbatch", "--parsable", "--job-name", "uuid", "--output", "logs/s.%j.log",
                      "--export=ALL", "--comment", "rule_a", "-t", "5", "--ntasks=1", "-D", str(w.project),
                      "/dev/stdin", input="#!/bin/sh\necho via-stdin\n"))
    assert r.stdout.strip() == "1001"
    assert (w.project / "logs" / "s.1001.log").read_text() == "via-stdin\n"


def test_sbatch_wrap_and_default_log(w):
    assert_ok(run(w, "sbatch", "--wrap=echo wrapped > wrapped.txt; echo to-log"))
    assert (w.project / "wrapped.txt").read_text() == "wrapped\n"
    assert (w.project / "slurm-1001.out").read_text() == "to-log\n"


def test_nested_submission_is_confined(w):
    (w.project / "inner.sh").write_text('#!/bin/sh\necho inner > inner.txt\ntouch "$OUTSIDE/inner" || true\n')
    (w.project / "outer.sh").write_text("#!/bin/sh\nsbatch inner.sh\n")
    assert_ok(run(w, "sbatch", "outer.sh"))
    assert (w.project / "inner.txt").read_text() == "inner\n"
    assert not (w.outside / "inner").exists()
    calls = w.calls("sbatch")
    assert len(calls) == 2 and all(c["argv"][-1].startswith(str(w.jobs_dir())) for c in calls)


def test_srun_streams_and_returns_exit_code(w):
    r = assert_ok(run(w, "sh", "-c", 'printf "%s\\n" "echo step-out; touch \\"$OUTSIDE/step\\" 2>/dev/null '
                      '|| echo step-confined; exit 3" | srun -n1 --cpu-bind=q sh -s; echo rc=$?'))
    assert r.stdout.split() == ["step-out", "step-confined", "rc=3"]
    argv = w.calls("srun")[0]["argv"]
    assert argv[:2] == ["--ntasks=1", "--cpu-bind=q"]
    assert argv[2] == w.confine_path and argv[3] == "--step" and argv[5:] == ["--", "sh", "-s"]


def test_srun_relays_signals(w):
    (w.project / "sig.sh").write_text("""#!/bin/sh
srun sh -c 'trap "echo got-term > step-term.txt; exit 0" TERM; echo up > step-up.txt; sleep 60 & wait' &
pid=$!
while [ ! -e step-up.txt ]; do sleep 0.2; done
sleep 0.5
kill -TERM $pid
wait $pid
""")
    assert_ok(run(w, "sh", "sig.sh", timeout=60))
    assert (w.project / "step-term.txt").read_text() == "got-term\n"


@pytest.mark.parametrize("command", [
    ["sbatch", "--outp=/x", "job.sh"],                  # abbreviated --output
    ["sbatch", "--get-user-env", "job.sh"],
    ["sbatch", "--export=ALL,LD_PRELOAD=/x.so", "job.sh"],
    ["sbatch", "-vo", "/x", "job.sh"],
    ["srun", "--task-prolog=/x", "true"],
    ["srun", "--output=/x", "true"],
    ["srun", "--bcast", "true"],
    ["scontrol", "update", "jobid=1", "StdOut=/x"],
    ["scontrol", "write", "batch_script", "1", "/x"],
    ["sacctmgr", "add", "user", "x"],
    ["sacct", "--file=/x"],
    ["salloc", "true"],
    ["scrontab", "-l"],
    ["strigger", "--set"],
    ["scancel", "-u", "someone"],
])
def test_refusals(w, command):
    assert_refused(run(w, *command), "agent-slurm:")
    assert w.calls("sbatch") == [] and w.calls("srun") == [] and w.calls("scancel") == []


def test_refused_header_option(w):
    (w.project / "bad.sh").write_text("#!/bin/sh\n#SBATCH --outp=/home/x/.bashrc\necho hi\n")
    assert_refused(run(w, "sbatch", "bad.sh"), "unknown option --outp")
    assert w.calls("sbatch") == []


def test_environment_injection(w):
    (w.project / "envjob.sh").write_text(
        '#!/bin/sh\necho "LDLP=$LD_LIBRARY_PATH SC=${SLURM_CONF:-none} TP=${SLURM_TASK_PROLOG:-none}"\n')
    evil = {
        "BASH_ENV": str(w.project / "evil.sh"), "LD_PRELOAD": "/nonexistent/evil.so",
        "LD_LIBRARY_PATH": "/opt/fake/lib", "BASH_FUNC_mkdir%%": "() { touch /x; }", "GCONV_PATH": "/x",
        "SLURM_CONF": str(w.project / "slurm.conf"), "SBATCH_EXPORT": "ALL", "SLURM_TASK_PROLOG": "/x",
        "PYTHONPATH": "/x",
    }
    # The agent sets these inside the confinement; confine itself runs with your SLURM_CONF.
    assert_ok(run(w, "env", *[f"{k}={v}" for k, v in evil.items()], "sbatch", "--output=env.out", "envjob.sh",
                  env=dict(w.env, SLURM_CONF="/etc/trusted/slurm.conf")))
    reached_slurm = w.calls("sbatch")[0]["env"]
    assert not (set(evil) - {"SLURM_CONF"}) & set(reached_slurm), set(evil) & set(reached_slurm)
    assert reached_slurm["SLURM_CONF"] == "/etc/trusted/slurm.conf"
    # Inside the confinement the program-loading variables are back (the job's sh warns about the missing
    # LD_PRELOAD library); SLURM's input variables are not, and SLURM_CONF is the trusted one.
    lines = (w.project / "env.out").read_text().splitlines()
    assert "cannot be preloaded" in lines[0]
    assert lines[-1] == "LDLP=/opt/fake/lib SC=/etc/trusted/slurm.conf TP=none"


def test_log_symlink_swap(w):
    """Swap the log folder for a symlink to an outside folder between submission and job start."""
    r = assert_ok(run(w, "sbatch", "--parsable", "--hold", "--output=logs/out.txt", "job.sh"))
    jid = r.stdout.strip()
    shutil.rmtree(w.project / "logs")
    os.symlink(w.outside, w.project / "logs")
    w.release(jid)
    assert not (w.outside / "out.txt").exists()
    assert not (w.project / "result.txt").exists()   # the job did not start without its log
    slurm_log = next(w.jobs_dir().glob("*/slurm-*.out")).read_text()
    assert "Read-only file system" in slurm_log


def test_input_from_hidden_file(w):
    (w.project / "in.sh").write_text(f"#!/bin/sh\n#SBATCH --input={w.root}/secret/key\ncat > got.txt\n")
    run(w, "sbatch", "in.sh")
    for p in list(w.project.rglob("*")) + list(w.jobs_dir().rglob("*")):
        if p.is_file():
            assert b"SECRET-KEY" not in p.read_bytes(), p


def test_scancel_own_jobs_only(w):
    jid = assert_ok(run(w, "sbatch", "--parsable", "--hold", "job.sh")).stdout.strip()
    w.add_fake_job("555", "/home/someone/their_job.sh")
    w.add_fake_job("556", str(w.root / "spool" / "other-0123456789ab" / "jobs" / "x" / "wrapper.sh"))

    assert_ok(run(w, "scancel", jid, "--clusters=all"))   # Snakemake's form
    assert w.calls("scancel")[-1]["argv"] == ["--clusters=all", jid]
    for target in (["555"], ["556"], [jid, "555"]):
        assert_refused(run(w, "scancel", *target), "not submitted by an agent in this project")
    assert len(w.calls("scancel")) == 1
    assert_ok(run(w, "scancel", "777"))                    # unknown to SLURM: nothing to cancel
    assert len(w.calls("scancel")) == 1

    assert_refused(run(w, "srun", "--jobid=555", "true"), "not a job submitted by an agent")
    assert_ok(run(w, "srun", f"--jobid={jid}", "true"))


def test_read_only_commands_pass_through(w):
    r = assert_ok(run(w, "squeue", "--format=%i|%T", "--states=all", "--noheader", "--name", "x",
                      env=dict(w.env, LD_LIBRARY_PATH="/x")))
    assert r.stdout.strip() == "fake squeue --format=%i|%T --states=all --noheader --name x"
    call = w.calls("squeue")[0]
    assert call["env"]["PATH"] == "/usr/bin:/bin" and "LD_LIBRARY_PATH" not in call["env"]
    for command in (["sacct", "-X", "--parsable2", "--name", "x"], ["sinfo", "-o", "%P"],
                    ["scontrol", "show", "config"], ["sacctmgr", "-n", "-s", "list", "user", "u"],
                    ["sshare", "-U"]):
        assert_ok(run(w, *command))


def test_broker_is_gone_after_confine_exits(w):
    def socket_dirs():
        return {p for p in os.listdir("/dev/shm") if p.startswith("agent-slurm-")}

    before = socket_dirs()
    assert_ok(run(w, "sbatch", "job.sh"))
    assert socket_dirs() <= before
    procs = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", w.confine_path], capture_output=True, text=True)
    assert procs.stdout.strip() == "", procs.stdout

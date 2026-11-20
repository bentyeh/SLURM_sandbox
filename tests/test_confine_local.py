"""The confinement itself (bubblewrap on this node; no SLURM). Run outside Claude's sandbox."""
import os
import signal
import subprocess
import time

import pytest

from conftest import assert_ok

pytestmark = pytest.mark.local


def test_writes_inside_and_outside(world):
    r = world.confine("sh", "-c", 'touch inside && touch "$1/extra/x" "$1/shared/x" && touch "$1/outside/x"',
                      "sh", str(world.root))
    assert r.returncode != 0 and "Read-only file system" in r.stderr
    for p in ("project/inside", "extra/x", "shared/x"):
        assert (world.root / p).exists() and (world.root / p).stat().st_uid == os.getuid()
    assert not (world.outside / "x").exists()


def test_home_is_read_only(world):
    canary = os.path.expanduser("~/.agent-slurm-canary")
    try:
        r = world.confine("touch", canary)
        assert r.returncode != 0 and "Read-only file system" in r.stderr
        assert not os.path.exists(canary)
    finally:
        if os.path.exists(canary):
            os.remove(canary)


@pytest.mark.parametrize("path", [".git/config", ".git/hooks/pre-commit", ".claude/settings.json",
                                  ".claude/settings.local.json", "bin/tool.sh", "bin/new", ".vscode",
                                  ".vscode/tasks.json", ".bashrc", ".mcp.json"])
def test_protected_paths(world, path):
    before = {p: p.read_bytes() for p in world.project.rglob("*") if p.is_file()}
    r = world.sh(f'echo pwned >> "{path}" || mkdir -p "{path}"')
    assert r.returncode != 0, r.stderr
    after = {p: p.read_bytes() for p in world.project.rglob("*") if p.is_file()}
    assert {p: after[p] for p in before} == before
    # bwrap leaves an empty placeholder for missing protected paths, as Claude's sandbox does; nothing else
    assert all(after[p] == b"" for p in set(after) - set(before))


def test_hidden_paths(world):
    r = assert_ok(world.confine("sh", "-c", 'ls -A "$1/secret" "$1/secret2" /run/munge; ls -A ~/.ssh; '
                                "ls -A ~/.claude", "sh", str(world.root)))
    assert "key" not in r.stdout and "data" not in r.stdout and "munge.socket" not in r.stdout
    assert "id_" not in r.stdout and "settings.json" not in r.stdout


def test_munge_unreachable_inside(world):
    """Without munge credentials, nothing inside can authenticate to SLURM, whatever program it uses."""
    if subprocess.run(["munge", "-n"], capture_output=True).returncode != 0:
        pytest.skip("munge does not work outside the confinement on this node")
    r = world.confine("munge", "-n")
    assert r.returncode != 0
    r = world.confine("/usr/bin/python3", "-c",
                      "import socket; s = socket.socket(socket.AF_UNIX); s.connect('/run/munge/munge.socket.2')")
    assert r.returncode != 0


def test_kernel_isolation(world):
    r = assert_ok(world.confine("grep", "NoNewPrivs", "/proc/self/status"))
    assert r.stdout.split()[1] == "1"   # setuid programs (ssh-keysign, crontab) get no extra privileges
    assert world.confine("unshare", "-U", "true").returncode != 0   # no nested user namespaces


def test_other_processes_unreachable(world):
    outside = subprocess.Popen(["sleep", "60"])
    try:
        pid = outside.pid
        # Listing /proc/PID/fd shows only numbers; using the open files behind them is what must fail.
        for cmd in (f"ls /proc/{pid}/root/", f"cat /proc/{pid}/environ", f"readlink /proc/{pid}/fd/1",
                    f"echo x > /proc/{pid}/fd/1", f"cat /proc/{pid}/cwd/x"):
            r = world.sh(cmd)
            assert r.returncode != 0, cmd
        ptrace = ("import ctypes, sys; libc = ctypes.CDLL(None, use_errno=True); "
                  f"sys.exit(0 if libc.ptrace(16, {pid}, 0, 0) == 0 else 1)")   # PTRACE_ATTACH
        assert world.confine("/usr/bin/python3", "-c", ptrace).returncode != 0
    finally:
        outside.kill()


def test_standins_replace_slurm_commands(world):
    r = assert_ok(world.sh("head -c 400 /usr/bin/sbatch; head -c 400 /usr/bin/srun"))
    assert r.stdout.count("Stand-in for SLURM commands") == 2


def test_environment_inside(world):
    r = assert_ok(world.confine("sh", "-c", 'echo "$TMPDIR|$CONDA_PKGS_DIRS|$PYTHONNOUSERSITE"; touch "$TMPDIR/t"'))
    env_dir = str(world.root / "shared" / "env")
    assert r.stdout.strip() == f"{env_dir}/tmp|{env_dir}/conda_pkgs|1"


def test_exit_code_and_signal_forwarding(world):
    assert world.sh("exit 7").returncode == 7
    proc = subprocess.Popen([world.confine_path, "sh", "-c",
                             'trap "echo got-term > term.txt; exit 5" TERM; echo ready; sleep 60 & wait'],
                            cwd=world.project, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 5
    assert (world.project / "term.txt").read_text().strip() == "got-term"


def test_confine_inside_confine_runs_directly(world):
    r = assert_ok(world.confine(world.confine_path, "sh", "-c", "touch nested && echo nested-ok"))
    assert "nested-ok" in r.stdout


def test_refusals(world):
    assert world.confine("true", cwd=world.outside).returncode != 0   # no project
    assert world.confine("--job", str(world.outside)).returncode != 0
    world.write_settings({"sandbox": {"filesystem": {"allowWrite": ["~"]}}})
    r = world.confine("true")
    assert r.returncode != 0 and "refusing" in r.stderr

"""Shared fixtures: a temporary installation of confine, a test project, and folders outside the confinement."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
FAKE = REPO / "tests" / "fake_slurm" / "fake.py"
BWRAP = os.path.expanduser("~/miniconda3/envs/sandbox/bin/bwrap")
SLURM_COMMANDS = ("sbatch", "srun", "scancel", "squeue", "sacct", "sinfo", "sshare", "sstat", "scontrol", "sacctmgr")

sys.path.insert(0, str(SRC))


class World:
    """A confine installation with its own spool, a project, writable and hidden folders, and a canary folder.

    Layout under `root`: install/ (confine), spool/, project/ (with .claude/settings.json, .git, bin/), extra/
    (allowWrite), shared/ (shared_writable), secret/ (hidden by config), secret2/ (denyRead), outside/ (read-only
    inside), fakebin/ (fake SLURM, unless real SLURM is used).
    """

    def __init__(self, root, real_slurm=False):
        self.root = Path(root)
        for d in ("project/.claude", "project/.git/hooks", "project/bin", "project/logs", "extra", "shared",
                  "secret", "secret2", "outside"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.project = self.root / "project"
        self.outside = self.root / "outside"
        (self.project / ".git" / "config").write_text("[core]\n")
        (self.project / "bin" / "tool.sh").write_text("#!/bin/sh\n")
        (self.root / "secret" / "key").write_text("SECRET-KEY\n")
        (self.root / "secret2" / "data").write_text("SECRET-DATA\n")
        self.write_settings({"sandbox": {"filesystem": {
            "allowWrite": ["/" + str(self.root / "extra")],
            "denyWrite": ["./bin"],
            "denyRead": ["/" + str(self.root / "secret2")],
        }}})
        (self.root / "user-settings.json").write_text("{}")

        if real_slurm:
            self.real_bin_dir = "/usr/bin"
        else:
            fakebin = self.root / "fakebin"
            fakebin.mkdir()
            for c in SLURM_COMMANDS:
                os.symlink(FAKE, fakebin / c)
            self.real_bin_dir = str(fakebin)

        self.install = self.root / "install"
        pkg = self.install / "agent_slurm"
        pkg.mkdir(parents=True)
        shutil.copy2(SRC / "confine", self.install / "confine")
        for f in (SRC / "agent_slurm").iterdir():
            if f.suffix == ".py" or f.name == "stand-in":
                shutil.copy2(f, pkg / f.name)
        with open(SRC / "agent_slurm" / "config.json") as f:
            default_hide = json.load(f)["hide"]
        self.config = {
            "bwrap": BWRAP,
            "python": "/usr/bin/python3",
            "real_bin_dir": self.real_bin_dir,
            "spool": str(self.root / "spool"),
            "socket_dir": "/dev/shm",
            "shared_writable": [str(self.root / "shared")],
            "env_dir": str(self.root / "shared" / "env"),
            "hide": default_hide + [str(self.root / "secret")],
            "user_settings": str(self.root / "user-settings.json"),
            "managed_settings": str(self.root / "managed-settings.json"),
        }
        self.write_config()
        self.confine_path = str(self.install / "confine")

    def write_config(self, **changes):
        self.config.update(changes)
        (self.install / "agent_slurm" / "config.json").write_text(json.dumps(self.config, indent=1))

    def write_settings(self, settings, name="settings.json"):
        (self.project / ".claude" / name).write_text(json.dumps(settings))

    def confine(self, *command, cwd=None, input=None, env=None, timeout=120):
        full_env = dict(os.environ)
        full_env.update(env or {})
        return subprocess.run([self.confine_path, *command], cwd=cwd or self.project, input=input, env=full_env,
                              capture_output=True, text=True, timeout=timeout)

    def sh(self, script, **kw):
        return self.confine("sh", "-c", script, **kw)

    def calls(self, cmd=None):
        path = Path(self.real_bin_dir) / "state" / "calls.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        return [e for e in entries if cmd is None or e["cmd"] == cmd]

    def fake_jobs(self):
        path = Path(self.real_bin_dir) / "state" / "jobs.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def add_fake_job(self, jid, command):
        state = Path(self.real_bin_dir) / "state"
        state.mkdir(exist_ok=True)
        jobs = self.fake_jobs()
        jobs[jid] = {"command": command, "args": [], "env": {}, "state": "held"}
        (state / "jobs.json").write_text(json.dumps(jobs))

    def release(self, jid):
        subprocess.run([str(Path(self.real_bin_dir) / "sbatch"), "--fake-release", jid], check=True)

    def jobs_dir(self):
        keys = list((self.root / "spool").iterdir())
        assert len(keys) == 1
        return keys[0] / "jobs"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def assert_ok(result):
    assert result.returncode == 0, f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def assert_refused(result, text="refused"):
    assert result.returncode != 0, f"expected a refusal\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert text in result.stderr, result.stderr
    return result

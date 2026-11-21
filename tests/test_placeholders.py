"""Placeholder files: created while a confinement runs, removed when the last confinement of the project ends."""
import json
import os

import pytest

from agent_slurm import config, placeholders, policy
from conftest import World


@pytest.fixture
def setup(tmp_path):
    w = World(tmp_path)
    cfg = config.load(str(w.install))
    pol = policy.build(cfg, str(w.project), lambda message: None)
    return w, cfg, pol


def new_files(w):
    return sorted(p.name for p in w.project.iterdir() if p.is_file() and p.stat().st_size == 0)


def write_registration(cfg, pol, **changes):
    """A registration as another confinement would leave it; by default one of this process (so: live)."""
    reg = dict(placeholders._node(), pid=os.getpid(), start=placeholders._start_time(os.getpid()), mnt=None)
    reg.update(changes)
    users = policy.private_dir(policy.spool_dir(cfg, pol, "placeholders", "users"))
    path = os.path.join(users, "other.json")
    with open(path, "w") as f:
        json.dump(reg, f)
    return path


def test_created_then_removed(setup):
    w, cfg, pol = setup
    assert new_files(w) == []
    r = placeholders.Registration(cfg, pol)
    r.register()
    assert ".mcp.json" in new_files(w) and ".vscode" in new_files(w)
    assert (w.project / ".vscode").stat().st_mode & 0o222 == 0
    assert (w.project / ".git" / "config").read_text() == "[core]\n"     # existing ones untouched
    r.release()
    assert new_files(w) == []
    assert os.listdir(policy.spool_dir(cfg, pol, "placeholders", "users")) == []


def test_last_one_out_removes(setup):
    w, cfg, pol = setup
    a, b = placeholders.Registration(cfg, pol), placeholders.Registration(cfg, pol)
    a.register()
    before = new_files(w)
    b.register()                 # finds them in place; creates nothing new
    a.release()
    assert new_files(w) == before   # b still runs: removing them would lift its protection
    b.release()
    assert new_files(w) == []


def test_user_files_are_kept(setup):
    w, cfg, pol = setup
    (w.project / ".gitmodules").write_text("")    # yours, empty, from before
    r = placeholders.Registration(cfg, pol)
    r.register()
    os.remove(w.project / ".mcp.json")            # replaced while confined (from outside)
    (w.project / ".mcp.json").write_text("")
    r.release()
    assert new_files(w) == [".gitmodules", ".mcp.json"]


@pytest.mark.parametrize("changes, removed", [
    ({}, False),                                       # its confine still runs
    ({"start": "0"}, True),                            # its confine has exited
    ({"start": "0", "mnt": "MOUNT_NS_OF_THIS_PROCESS"}, False),   # ...but a process is left in its namespace
    ({"start": "0", "host": "elsewhere"}, False),      # on another node: can't be checked from here
    ({"start": "0", "boot": "before-reboot"}, True),   # this node has rebooted since
])
def test_other_registrations(setup, changes, removed):
    w, cfg, pol = setup
    if changes.get("mnt"):
        changes["mnt"] = os.readlink("/proc/self/ns/mnt")
    r = placeholders.Registration(cfg, pol)
    r.register()
    other = write_registration(cfg, pol, **changes)
    r.release()
    assert (new_files(w) == []) == removed
    assert os.path.exists(other) != removed   # a dead registration is dropped


def test_namespace_keeps_own_registration(setup):
    w, cfg, pol = setup
    r = placeholders.Registration(cfg, pol)
    r.register()
    r.set_namespace(os.readlink("/proc/self/ns/mnt"))   # as if a background process from inside still ran
    r.release()
    assert ".mcp.json" in new_files(w)
    r.set_namespace("mnt:[1]")                          # it has ended
    r.release()
    assert new_files(w) == []

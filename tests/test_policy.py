"""Policy from Claude Code settings (pure Python; builds bwrap arguments without running bwrap)."""
import json
import os

import pytest

from agent_slurm import config, policy
from conftest import World


@pytest.fixture
def setup(tmp_path):
    w = World(tmp_path)
    return w, config.load(str(w.install))


def build(w, cfg, cwd=None):
    warnings = []
    return policy.build(cfg, str(cwd or w.project), warnings.append), warnings


def test_resolve():
    assert policy.resolve("//abs/x", "/base") == "/abs/x"
    assert policy.resolve("/abs/x", "/base") == "/abs/x"
    assert policy.resolve("~/x", "/base") == os.path.expanduser("~/x")
    assert policy.resolve("./bin", "/base") == "/base/bin"
    assert policy.resolve("rel/../y", "/base") == "/base/y"


def test_policy_from_settings(setup):
    w, cfg = setup
    sub = w.project / "deep" / "er"
    sub.mkdir(parents=True)
    pol, warnings = build(w, cfg, cwd=sub)   # found from a subfolder
    r = os.path.realpath
    assert pol["project_root"] == r(w.project)
    assert pol["writable"] == [r(w.project), r(w.root / "extra"), r(w.root / "shared")]
    # protected names in the project root, existing or not, are placeholders: missing ones get an empty file
    for name in (".git/config", ".git/hooks", ".claude", ".vscode"):
        assert r(w.project / name) in pol["placeholders"]
    assert r(w.project / "bin") in pol["protect"]                 # denyWrite ./bin
    assert not any(p.startswith(r(w.project) + "/.") for p in pol["protect"])
    assert r(w.root / "extra" / ".vscode") not in pol["placeholders"] + pol["protect"]   # missing, not the root
    assert "/run/munge" in pol["hide"] and str(w.root / "secret2") in pol["hide"]
    assert warnings == []


def test_settings_are_merged(setup):
    w, cfg = setup
    (w.root / "local-extra").mkdir()
    w.write_settings({"sandbox": {"filesystem": {"allowWrite": ["//" + str(w.root / "local-extra")[1:]]}}},
                     "settings.local.json")
    (w.root / "user-extra").mkdir()
    (w.root / "user-settings.json").write_text(json.dumps(
        {"sandbox": {"filesystem": {"allowWrite": [str(w.root / "user-extra")], "denyRead": ["~/.netrc"]}}}))
    pol, _ = build(w, cfg)
    assert os.path.realpath(w.root / "local-extra") in pol["writable"]
    assert os.path.realpath(w.root / "user-extra") in pol["writable"]
    assert os.path.expanduser("~/.netrc") in pol["hide"]


def test_missing_allow_write_is_skipped(setup):
    w, cfg = setup
    w.write_settings({"sandbox": {"filesystem": {"allowWrite": [str(w.root / "nope")]}}})
    pol, warnings = build(w, cfg)
    assert str(w.root / "nope") not in pol["writable"] and "does not exist" in warnings[0]


@pytest.mark.parametrize("entry", ["~", "//", "SPOOL", "INSTALL", "~/.claude", "SPOOLSUB"])
def test_dangerous_writable_refused(setup, entry):
    w, cfg = setup
    sub = w.root / "spool" / "sub"
    sub.mkdir(parents=True)
    entry = {"SPOOL": str(w.root / "spool"), "INSTALL": str(w.install), "SPOOLSUB": str(sub)}.get(entry, entry)
    w.write_settings({"sandbox": {"filesystem": {"allowWrite": [entry]}}})
    with pytest.raises(policy.PolicyError):
        build(w, cfg)


def test_no_project_and_home_is_not_a_project(setup):
    w, cfg = setup
    with pytest.raises(policy.PolicyError):
        build(w, cfg, cwd=w.outside)


def test_bad_settings(setup):
    w, cfg = setup
    (w.project / ".claude" / "settings.local.json").write_text("{not json")
    with pytest.raises(policy.PolicyError):
        build(w, cfg)
    (w.project / ".claude" / "settings.local.json").write_text("")   # Claude's empty placeholder: fine
    build(w, cfg)
    w.write_settings({"sandbox": {"filesystem": {"denyRead": ["~/.aws/*"]}}})
    with pytest.raises(policy.PolicyError):
        build(w, cfg)


def test_snapshot_and_spool_check(setup):
    w, cfg = setup
    pol, _ = build(w, cfg)
    path = policy.save(cfg, pol)
    assert path.startswith(str(w.root / "spool")) and policy.load(cfg, path) == pol
    assert policy.save(cfg, pol) == path
    other = w.outside / "policy.json"
    other.write_text(json.dumps(pol))
    with pytest.raises(policy.PolicyError):
        policy.load(cfg, str(other))
    link = w.root / "spool" / "link.json"
    os.symlink(other, link)
    with pytest.raises(policy.PolicyError):
        policy.load(cfg, str(link))


def test_bwrap_args(setup):
    w, cfg = setup
    pol, _ = build(w, cfg)
    args = policy.bwrap_args(cfg, pol, "/dev/shm/x/s")
    pairs = [tuple(args[i:i + 3]) for i in range(len(args) - 2)]
    p = os.path.realpath(w.project)
    assert ("--bind", p, p) in pairs
    assert ("--ro-bind", p + "/.git/config", p + "/.git/config") in pairs
    assert ("--ro-bind", "/dev/null", str(w.project / ".vscode")) in pairs
    assert ("--tmpfs", "/run/munge", "--remount-ro") in pairs
    assert ("--bind", "/dev/shm/x/s", "/tmp/.agent-slurm.sock") in pairs
    assert ("--ro-bind", cfg.standin, "/usr/bin/sbatch") in pairs
    for flag in ("--unshare-user", "--disable-userns", "--die-with-parent", "--new-session"):
        assert flag in args
    # writable binds come before the read-only overlays inside them
    assert args.index(p) < args.index(p + "/.git/config")

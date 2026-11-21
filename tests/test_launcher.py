"""confine's command line (pure Python; never starts bwrap)."""
import os

import pytest

from agent_slurm import launcher


def test_unreadable_uid_map_refuses(monkeypatch, capsys):
    def no_uid_map(path, *args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", path)

    monkeypatch.setattr(launcher, "open", no_uid_map, raising=False)   # shadows the builtin in launcher only
    monkeypatch.setattr(os, "execvp", lambda *args: pytest.fail("ran the command unconfined"))
    assert launcher.main(["true"]) == 1
    assert "/proc/self/uid_map" in capsys.readouterr().err

"""Installation-wide settings, read from config.json next to this package.

The file is installed read-only alongside `confine`, outside every writable folder, so an agent cannot change it.
"""
import json
import os

PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))
INSTALL_DIR = os.path.dirname(PACKAGE_DIR)

# Commands whose /usr/bin entries are replaced by the stand-in inside the confinement.
PROXIED = ("sbatch", "srun", "scancel", "squeue", "sacct", "sinfo", "sshare", "sstat", "scontrol", "sacctmgr")
REFUSED = ("salloc", "sattach", "sbcast", "scrontab", "strigger")
STANDIN_TARGET_DIR = "/usr/bin"
# Inside the confinement, the broker's socket is always here.
SOCKET_INSIDE = "/tmp/.agent-slurm.sock"
# Always hidden inside: without munge credentials nothing inside can talk to SLURM directly.
MANDATORY_HIDE = ("/run/munge",)


def expand(path):
    return os.path.expanduser(path.replace("{uid}", str(os.getuid())))


class Config:
    def __init__(self, data, install_dir=INSTALL_DIR):
        self.install_dir = install_dir
        self.confine = os.path.join(install_dir, "confine")
        self.package_dir = os.path.join(install_dir, "agent_slurm")
        self.standin = os.path.join(self.package_dir, "stand-in")
        self.jobentry = os.path.join(self.package_dir, "jobentry.py")
        self.bwrap = expand(data["bwrap"])
        self.python = expand(data.get("python", "/usr/bin/python3"))
        self.real_bin_dir = expand(data.get("real_bin_dir", "/usr/bin"))
        self.spool = os.path.realpath(expand(data["spool"]))
        self.socket_dir = expand(data.get("socket_dir", "/dev/shm"))
        self.shared_writable = [expand(p) for p in data.get("shared_writable", [])]
        self.env_dir = expand(data["env_dir"]) if data.get("env_dir") else None
        self.hide = list(MANDATORY_HIDE) + [expand(p) for p in data.get("hide", [])]
        self.user_settings = expand(data.get("user_settings", "~/.claude/settings.json"))
        self.managed_settings = expand(data.get("managed_settings", "/etc/claude-code/managed-settings.json"))

    def real(self, command):
        return os.path.join(self.real_bin_dir, command)


def load(install_dir=INSTALL_DIR):
    with open(os.path.join(install_dir, "agent_slurm", "config.json")) as f:
        return Config(json.load(f), install_dir)

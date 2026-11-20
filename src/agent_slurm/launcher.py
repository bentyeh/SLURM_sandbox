"""`confine`: run a command confined, with a broker outside the confinement for its SLURM calls."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

from . import config, policy
from .broker import Broker

USAGE = """\
usage: confine COMMAND [ARGS...]     run COMMAND confined, using the policy of the project around $PWD
       confine --print-policy        show that policy
internal (used by jobs and job steps that the broker submits):
       confine --job JOBDIR
       confine --step STEPFILE -- COMMAND [ARGS...]
"""
FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2)


def warn(message):
    print(f"confine: {message}", file=sys.stderr)


def already_confined():
    """True in a non-initial user namespace (inside confine, or inside Claude's own sandbox), where the kernel's
    uid map is not the full identity map. bwrap could not start there anyway; the command is confined already."""
    try:
        with open("/proc/self/uid_map") as f:
            return f.read().split() != ["0", "0", "4294967295"]
    except OSError:
        return True


def run_confined(cfg, pol, policy_path, command, restore):
    if pol["env_dir"]:
        for sub in ("tmp", "cache", "conda_pkgs"):
            os.makedirs(os.path.join(pol["env_dir"], sub), exist_ok=True)
    os.makedirs(cfg.socket_dir, exist_ok=True)
    sockdir = tempfile.mkdtemp(prefix="agent-slurm-", dir=cfg.socket_dir)
    broker = Broker(cfg, pol, policy_path, os.path.join(sockdir, "s"))
    try:
        broker.start()
        info_r, info_w = os.pipe()
        args = [cfg.bwrap] + policy.bwrap_args(cfg, pol, broker.socket_path) + ["--info-fd", str(info_w)]
        for name, value in policy.inner_env(pol, restore).items():
            args += ["--setenv", name, value]
        proc = subprocess.Popen(args + ["--"] + command, pass_fds=(info_w,))
        os.close(info_w)
        child = {}

        def read_info():
            with os.fdopen(info_r) as f:
                try:
                    child.update(json.loads(f.read() or "{}"))
                except ValueError:
                    pass

        reader = threading.Thread(target=read_info, daemon=True)
        reader.start()

        def forward(signum, frame):
            pid = child.get("child-pid")
            try:
                if pid:
                    os.killpg(pid, signum)   # the command leads its own session (--new-session)
                else:
                    proc.send_signal(signum)
            except OSError:
                pass

        for s in FORWARDED:
            signal.signal(s, forward)
        rc = proc.wait()
        return rc if rc >= 0 else 128 - rc
    finally:
        broker.stop()
        shutil.rmtree(sockdir, ignore_errors=True)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0 if argv else 2
    try:
        cfg = config.load()
        if argv[0] == "--job":
            jobdir = os.path.realpath(argv[1])
            job = policy.load_spool_file(cfg, os.path.join(jobdir, "job.json"))
            pol = policy.load(cfg, job["policy"])
            return run_confined(cfg, pol, job["policy"], [cfg.python, "-I", cfg.jobentry, jobdir],
                                job["env_restore"])
        if argv[0] == "--step":
            if len(argv) < 4 or argv[2] != "--":
                warn("usage: confine --step STEPFILE -- COMMAND [ARGS...]")
                return 2
            step = policy.load_spool_file(cfg, argv[1])
            pol = policy.load(cfg, step["policy"])
            return run_confined(cfg, pol, step["policy"], argv[3:], step["env_restore"])
        if argv[0] == "--print-policy":
            print(json.dumps(policy.build(cfg, os.getcwd(), warn), indent=1))
            return 0
        command = argv[1:] if argv[0] == "--" else argv
        if not command:
            warn("no command given")
            return 2
        if already_confined():
            os.execvp(command[0], command)
        pol = policy.build(cfg, os.getcwd(), warn)
        return run_confined(cfg, pol, policy.save(cfg, pol), command, {})
    except (policy.PolicyError, OSError, KeyError, ValueError) as e:
        warn(str(e))
        return 1

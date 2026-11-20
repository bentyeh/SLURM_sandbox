"""Start a batch job's script inside the confinement on the compute node.

The broker submits every job with SLURM's own output going to the spool, so SLURM (which runs unconfined) never
writes to a path the agent chose. This applies the job's requested --output, --error, --input and --open-mode here
instead, inside the confinement, where a symlink to a protected place only leads to a read-only file system.

Usage (by `confine --job`): python3 -I jobentry.py JOBDIR
"""
import getpass
import json
import os
import re
import socket
import sys

NO_ARRAY_TASK = "4294967294"   # what SLURM puts in %a for jobs that are not array jobs


def expand(pattern, env):
    """SLURM's filename patterns: %A %a %b %J %j %N %n %s %t %u %x %%, with optional zero-padding (%4j)."""
    job = env.get("SLURM_JOB_ID", "0")
    task = env.get("SLURM_ARRAY_TASK_ID", NO_ARRAY_TASK)
    values = {
        "A": env.get("SLURM_ARRAY_JOB_ID", job),
        "a": task,
        "b": str(int(task) % 10) if task.isdigit() else "0",
        "J": f"{job}.batch",
        "j": job,
        "N": socket.gethostname().split(".")[0],
        "n": env.get("SLURM_NODEID", "0"),
        "s": "batch",
        "t": "0",
        "u": env.get("USER") or getpass.getuser(),
        "x": env.get("SLURM_JOB_NAME", ""),
    }

    def sub(m):
        width, letter = m.group(1), m.group(2)
        if letter == "%":
            return "%"
        value = values[letter]
        return value.zfill(int(width)) if width and value.isdigit() else value

    return re.sub(r"%(\d*)([AabJjNnstux%])", sub, pattern)


def open_or_die(path, flags):
    try:
        return os.open(path, flags, 0o666)
    except OSError as e:
        print(f"agent-slurm: cannot open {path} for the job: {e}", file=sys.stderr)
        sys.exit(1)


def main(jobdir):
    with open(os.path.join(jobdir, "job.json")) as f:
        job = json.load(f)
    try:
        os.chdir(job["workdir"])
    except OSError as e:
        print(f"agent-slurm: cannot change to the job's working folder: {e}", file=sys.stderr)
        return 1
    env = os.environ
    out = expand(job["stdout"] or ("slurm-%A_%a.out" if job["array"] else "slurm-%j.out"), env)
    err = expand(job["stderr"], env) if job["stderr"] else out
    mode = os.O_APPEND if job["open_mode"] == "append" else os.O_TRUNC
    write = os.O_WRONLY | os.O_CREAT | mode
    out_fd = open_or_die(out, write)
    err_fd = out_fd if err == out else open_or_die(err, write)
    in_fd = open_or_die(expand(job["stdin"], env) if job["stdin"] else os.devnull, os.O_RDONLY)
    for fd, target in ((in_fd, 0), (out_fd, 1), (err_fd, 2)):
        os.dup2(fd, target)
    script = os.path.join(jobdir, "script")
    os.execv(script, [script] + job["args"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

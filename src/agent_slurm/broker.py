"""The broker: runs outside the confinement, as threads of `confine`, and makes every SLURM call for it.

Inside the confinement /run/munge is hidden, so nothing there can authenticate to SLURM. The SLURM commands are
replaced by the stand-in, which sends each call here over a Unix socket together with its stdin, stdout and stderr.
The broker checks the call, rewrites it so that new jobs and job steps start under `confine` too, runs the real
command with a cleaned environment, and returns its exit status.
"""
import json
import os
import re
import shlex
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time

from . import envclean, policy
from .options import (Refused, canonical, check_sacct, check_sacctmgr, check_scontrol, job_query_id,
                      parse_sbatch, parse_sbatch_header, parse_scancel, parse_srun, wrap_script)

MAX_SCRIPT = 4 * 1024 * 1024   # SLURM's default limit for batch scripts
MAX_MESSAGE = 16 * 1024 * 1024
STOP_GRACE = 10                # seconds between SIGTERM and SIGKILL when the caller disappears
PASS_THROUGH = ("squeue", "sinfo", "sshare", "sstat")


# Messages: 4-byte length + JSON, with file descriptors attached to the length bytes. The stand-in has its own copy.
def send_msg(sock, obj, fds=()):
    data = json.dumps(obj).encode()
    header = struct.pack(">I", len(data))
    if fds:
        socket.send_fds(sock, [header], list(fds))
    else:
        sock.sendall(header)
    sock.sendall(data)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def recv_msg(sock):
    """(message, fds), or (None, []) when the other side has closed the connection."""
    header, fds, _, _ = socket.recv_fds(sock, 4, 8)
    if not header:
        return None, []
    if len(header) < 4:
        rest = _recv_exact(sock, 4 - len(header))
        if rest is None:
            return None, fds
        header += rest
    (length,) = struct.unpack(">I", header)
    if length > MAX_MESSAGE:
        raise ValueError("message too large")
    data = _recv_exact(sock, length)
    if data is None:
        return None, fds
    return json.loads(data), fds


def read_fd(fd, limit):
    chunks, size = [], 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise Refused(f"sbatch: job script larger than {limit} bytes")
        chunks.append(chunk)


def trusted(env):
    """`env` plus SLURM_CONF from the broker's own environment (set by you or by slurmd in configless mode); the
    caller's SLURM_CONF is never used, since a config file can make SLURM commands load plugins."""
    env = dict(env)
    if "SLURM_CONF" in os.environ:
        env["SLURM_CONF"] = os.environ["SLURM_CONF"]
    return env


def last(options, name):
    values = [v for n, v in options if n == name]
    return values[-1] if values else None


class Broker:
    def __init__(self, cfg, pol, policy_path, socket_path):
        self.cfg, self.pol, self.policy_path, self.socket_path = cfg, pol, policy_path, socket_path
        self.jobs_dir = policy.spool_dir(cfg, pol, "jobs")
        self.listener = None

    def start(self):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.listener.listen(64)
        threading.Thread(target=self._serve, daemon=True).start()

    def stop(self):
        try:
            self.listener.close()
        except OSError:
            pass

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                            struct.calcsize("3i")))
            if uid != os.getuid():
                conn.close()
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        fds = []
        try:
            req, fds = recv_msg(conn)
            if req is None or len(fds) < 3:
                return
            stdio = fds[:3]
            command = os.path.basename(req["argv"][0])
            try:
                argv, env, cwd = self._prepare(command, req["argv"][1:], req, stdio[0], conn)
            except Refused as e:
                os.write(stdio[2], f"agent-slurm: {e}\n".encode())
                send_msg(conn, {"exit": 1})
                return
            if argv is None:
                send_msg(conn, {"exit": 0})
                return
            send_msg(conn, {"exit": self._run(argv, env, cwd, stdio, conn)})
        except (OSError, ValueError):
            pass
        finally:
            for fd in fds:
                os.close(fd)
            conn.close()

    def _prepare(self, command, args, req, stdin, conn):
        """(real argv, environment, working folder) for a request; argv None means nothing to run."""
        env, cwd = req["env"], req["cwd"] if os.path.isdir(req["cwd"]) else "/"
        query_env = envclean.split_env(env, "query")[0]
        if command == "sbatch":
            return self._sbatch(args, req, stdin, conn)
        if command == "srun":
            return self._srun(args, req, cwd)
        if command == "scancel":
            return self._scancel(args, req, query_env)
        if command == "sacct":
            check_sacct(args)
        elif command == "scontrol":
            check_scontrol(args)
        elif command == "sacctmgr":
            check_sacctmgr(args)
        elif command not in PASS_THROUGH:
            raise Refused(f"{command} is not available in the confinement")
        return [self.cfg.real(command)] + args, query_env, cwd

    def _sbatch(self, args, req, stdin, conn):
        cli, script_path, script_args = parse_sbatch(args)
        if script_path is None:
            script = wrap_script(last(cli, "wrap")).encode()
            name = "wrap"
        elif script_path in ("/dev/stdin", "-"):
            script = read_fd(stdin, MAX_SCRIPT)
            name = "sbatch"
        else:
            send_msg(conn, {"open": script_path})
            reply, fds = recv_msg(conn)
            if not reply or "error" in reply or not fds:
                raise Refused(f"sbatch: cannot open {script_path}: {(reply or {}).get('error', 'no reply')}")
            try:
                script = read_fd(fds[0], MAX_SCRIPT)
            finally:
                for fd in fds:
                    os.close(fd)
            name = os.path.basename(script_path)
        options = parse_sbatch_header(script.decode(errors="surrogateescape")) + cli   # the command line wins
        workdir = os.path.normpath(os.path.join(req["cwd"], last(options, "chdir") or "."))
        array = last(options, "array") is not None
        passed = [(n, v) for n, v in options
                  if n not in ("output", "error", "input", "open-mode", "chdir", "export", "wrap")]
        if last(options, "job-name") is None:
            passed.append(("job-name", name))
        clean, restore = envclean.split_env(req["env"], "sbatch")

        jobdir = tempfile.mkdtemp(dir=policy.private_dir(self.jobs_dir), prefix=time.strftime("%Y%m%d-%H%M%S-"))
        policy.write_atomic(os.path.join(jobdir, "script"), script, 0o500)
        policy.write_atomic(os.path.join(jobdir, "job.json"), json.dumps({
            "policy": self.policy_path,
            "workdir": workdir,
            "stdout": last(options, "output"),
            "stderr": last(options, "error"),
            "stdin": last(options, "input"),
            "open_mode": last(options, "open-mode") or "truncate",
            "array": array,
            "args": script_args,
            "env_restore": restore,
        }, indent=1).encode(), 0o400)
        wrapper = os.path.join(jobdir, "wrapper.sh")
        policy.write_atomic(wrapper, (
            "#!/bin/sh\n"
            "# Submitted by the agent-slurm broker: the job script runs confined.\n"
            f"exec {shlex.quote(self.cfg.confine)} --job {shlex.quote(jobdir)}\n").encode(), 0o500)

        export = last(options, "export")
        log = os.path.join(jobdir, "slurm-%A_%a.out" if array else "slurm-%j.out")
        argv = ([self.cfg.real("sbatch")] + canonical(passed) + ([f"--export={export}"] if export else [])
                + [f"--chdir={workdir}", f"--output={log}", wrapper])
        return argv, clean, jobdir

    def _srun(self, args, req, cwd):
        options, command = parse_srun(args)
        jobid = last(options, "jobid")
        if jobid is not None and self._owner(jobid, None) != "own":
            raise Refused(f"srun: --jobid={jobid} is not a job submitted by an agent in this project")
        clean, restore = envclean.split_env(req["env"], "srun")
        fd, stepfile = tempfile.mkstemp(dir=policy.private_dir(policy.spool_dir(self.cfg, self.pol, "steps")),
                                        prefix=time.strftime("%Y%m%d-%H%M%S-"), suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"policy": self.policy_path, "env_restore": restore}, f)
        os.chmod(stepfile, 0o400)
        argv = ([self.cfg.real("srun")] + canonical(options)
                + [self.cfg.confine, "--step", stepfile, "--"] + command)
        return argv, clean, cwd

    def _scancel(self, args, req, env):
        options, ids = parse_scancel(args)
        clusters = last(options, "clusters") or last(options, "cluster")
        keep = []
        for job in ids:
            owner = self._owner(job_query_id(job), clusters)
            if owner == "foreign":
                raise Refused(f"scancel: job {job} was not submitted by an agent in this project; nothing cancelled")
            if owner == "own":
                keep.append(job)
        if not keep:
            return None, None, None
        return [self.cfg.real("scancel")] + canonical(options) + keep, env, "/"

    def _owner(self, job, clusters):
        """"own" if SLURM records the job's batch script in this project's spool, "unknown" if SLURM doesn't
        know the job, else "foreign"."""
        cmd = [self.cfg.real("scontrol")]
        if clusters and clusters != "all":
            cmd += ["-M", clusters]
        env = trusted(envclean.split_env(os.environ, "query")[0])
        result = subprocess.run(cmd + ["show", "job", "-o", job], capture_output=True, text=True, env=env)
        commands = re.findall(r"(?:^|\s)Command=(\S*)", result.stdout, re.M)
        if result.returncode != 0 or not commands:
            return "unknown"
        prefix = self.jobs_dir + "/"
        return "own" if all(c.startswith(prefix) for c in commands) else "foreign"

    def _run(self, argv, env, cwd, stdio, conn):
        try:
            proc = subprocess.Popen(argv, stdin=stdio[0], stdout=stdio[1], stderr=stdio[2], env=trusted(env), cwd=cwd,
                                    start_new_session=True)
        except OSError as e:
            os.write(stdio[2], f"agent-slurm: cannot run {argv[0]}: {e}\n".encode())
            return 127
        threading.Thread(target=self._relay, args=(conn, proc), daemon=True).start()
        rc = proc.wait()
        return rc if rc >= 0 else 128 - rc

    def _relay(self, conn, proc):
        """Pass the caller's signals on; stop the command if the caller disappears."""
        while True:
            try:
                msg, fds = recv_msg(conn)
            except (OSError, ValueError):
                msg, fds = None, []
            for fd in fds:
                os.close(fd)
            if msg is None:
                break
            if "signal" in msg and proc.poll() is None:
                try:
                    proc.send_signal(int(msg["signal"]))
                except (OSError, ValueError, TypeError):
                    pass
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(STOP_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()

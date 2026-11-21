"""Empty files that occupy protected names missing from the project root, while a confinement runs.

If the project root has no `.mcp.json`, `.vscode` or `.git/hooks`, confined code could create one, so the
confinement mounts a read-only file at each such name. A mount needs an existing file to sit on, and the project
root is the real folder, so that file has to exist on the real file system. This module creates those files before
the confinement starts and removes them once no confinement of the project is left, on any node.

Removing them any earlier would lift the protection elsewhere: unlinking a file that is a mount point in another
mount namespace detaches that mount there. Another confinement of the same project (a second `confine` on the
login node, or Snakemake jobs on compute nodes) would then let its command create the name. So every confinement
registers in the spool while it runs, and the one that ends last removes the files. Registering, creating and
removing happen under a POSIX lock on a file in the spool; on NFS such locks are held by the server, so they
exclude each other across nodes.

A registration counts as live while its `confine` runs, and afterwards while any process is left in its mount
namespace (a background process started inside can outlive the command). Only the node that made a registration
can check it, so after a node crash the files stay until a later confinement of the project ends on that node.
Only files this module created, recorded by inode, are ever removed.
"""
import contextlib
import fcntl
import json
import os
import socket
import stat
import tempfile

from . import policy


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        return None


def _start_time(pid):
    """Start time of a process in clock ticks since boot (field 22 of /proc/PID/stat); with the pid, it
    identifies the process even after the pid is reused."""
    with open(f"/proc/{pid}/stat") as f:
        return f.read().rsplit(")", 1)[1].split()[19]


def _node():
    """Where pids and mount namespaces from a registration can be checked: same host, same boot, same pid
    namespace."""
    with open("/proc/sys/kernel/random/boot_id") as f:
        boot = f.read().strip()
    return {"host": socket.gethostname(), "boot": boot, "pidns": os.readlink("/proc/self/ns/pid")}


def mount_namespaces():
    """Mount namespaces (as `mnt:[N]`) of all your processes on this node."""
    found, uid = set(), os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid == uid:
                found.add(os.readlink(f"/proc/{pid}/ns/mnt"))
        except OSError:
            pass
    return found


def _live(reg, node, namespaces):
    if reg.get("host") != node["host"]:
        return True   # made on another node: can't be checked here
    if reg.get("boot") != node["boot"]:
        return False  # this node has rebooted since: nothing from then still runs
    if reg.get("pidns") != node["pidns"]:
        return True   # its pids mean something else here
    try:
        if _start_time(reg["pid"]) == reg["start"]:
            return True
    except (OSError, KeyError, IndexError):
        pass
    return reg.get("mnt") in namespaces


class Registration:
    """The placeholders of one confinement: `register` before bwrap starts, `release` after it has ended."""

    def __init__(self, cfg, pol):
        self.paths = pol["placeholders"]
        self.dir = policy.spool_dir(cfg, pol, "placeholders")
        self.users = os.path.join(self.dir, "users")
        self.created = os.path.join(self.dir, "created.json")
        self.token = self.me = None

    @contextlib.contextmanager
    def _locked(self):
        fd = os.open(os.path.join(self.dir, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)   # releases the lock

    def register(self):
        """Record this confinement and create the missing placeholders. Raises OSError if the lock can't be taken:
        running unregistered could let another confinement remove the placeholders this one relies on."""
        policy.private_dir(self.users)
        pid = os.getpid()
        self.me = dict(_node(), pid=pid, start=_start_time(pid), mnt=None)
        with self._locked():
            fd, self.token = tempfile.mkstemp(dir=self.users, suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(self.me, f)
            created = _read_json(self.created, []) or []
            for p in self.paths:
                try:
                    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
                except OSError:
                    continue   # exists already; if it can't be made at all, bwrap tries itself
                created.append({"path": p, "ino": os.fstat(fd).st_ino})
                os.close(fd)
            policy.write_atomic(self.created, json.dumps(created).encode())

    def set_namespace(self, mnt):
        """Record the confinement's mount namespace (`mnt:[N]`), so the registration stays live while anything
        started inside is still running, even after `confine` has exited."""
        if self.token and mnt:
            self.me["mnt"] = mnt
            policy.write_atomic(self.token, json.dumps(self.me).encode())

    def release(self):
        """Drop this registration; if no live one is left, remove the placeholders created for the project."""
        if self.token is None:
            return
        with self._locked():
            namespaces = mount_namespaces()
            if self.me["mnt"] in namespaces:
                return   # something started inside still runs; the registration stays until it ends
            os.remove(self.token)
            self.token = None
            node = _node()
            for name in os.listdir(self.users):
                if name.startswith(".") or not name.endswith(".json"):
                    continue   # a write in progress
                path = os.path.join(self.users, name)
                reg = _read_json(path, None)
                if reg is None or _live(reg, node, namespaces):
                    return     # unreadable registrations count as live
                os.remove(path)
            for entry in _read_json(self.created, []) or []:
                try:
                    st = os.lstat(entry["path"])
                    if stat.S_ISREG(st.st_mode) and st.st_size == 0 and st.st_ino == entry["ino"]:
                        os.remove(entry["path"])
                except (OSError, KeyError, TypeError):
                    pass
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.created)

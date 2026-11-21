"""Which folders a confined command may write, read, or not see, taken from the project's Claude Code settings.

The project root is the nearest folder above the working directory (never $HOME itself) that has
`.claude/settings.json` or `.claude/settings.local.json`. The confinement then mirrors Claude's own sandbox:

- writable: the project root, every `sandbox.filesystem.allowWrite` entry (managed, user, project and local
  settings), and the installation's `shared_writable` folders;
- read-only inside those: the files and folders Claude's sandbox always protects (`.git/hooks`, `.git/config`,
  `.claude`, `.vscode`, `.bashrc`, ...) and every `denyWrite` entry;
- hidden: `/run/munge`, the installation's `hide` list, and every `denyRead` entry.

A snapshot of the policy is saved in the spool, which only `confine` writes, so jobs and job steps started later on
other nodes use exactly the same policy.
"""
import hashlib
import json
import os
import tempfile

from . import config

PROTECTED_NAMES = (
    ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile", ".ripgreprc",
    ".mcp.json", ".claude", ".vscode", ".idea",
)
PROTECTED_IN_GIT = ("hooks", "config")


class PolicyError(Exception):
    pass


def inside(path, folder):
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def home():
    return os.path.realpath(os.path.expanduser("~"))


def find_project_root(cwd):
    d, h = os.path.realpath(cwd), home()
    while True:
        if d != h and any(os.path.exists(os.path.join(d, ".claude", f))
                          for f in ("settings.json", "settings.local.json")):
            return d
        if d == "/":
            raise PolicyError(f"no project found: no folder above {cwd} (other than your home folder) has "
                              ".claude/settings.json or .claude/settings.local.json")
        d = os.path.dirname(d)


def read_settings(path):
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise PolicyError(f"cannot read {path}: {e}")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError as e:
        raise PolicyError(f"{path} is not valid JSON: {e}")


def resolve(entry, base):
    """A path from settings, with Claude Code's conventions: `//x` and `/x` absolute, `~/x` home, else under base."""
    if entry.startswith("//"):
        entry = entry[1:]
    elif entry == "~" or entry.startswith("~/"):
        entry = os.path.expanduser(entry)
    elif not entry.startswith("/"):
        entry = os.path.join(base, entry)
    return os.path.normpath(entry)


def settings_paths(cfg, root):
    return [
        (cfg.managed_settings, "/"),
        (cfg.user_settings, os.path.dirname(cfg.user_settings)),
        (os.path.join(root, ".claude", "settings.json"), root),
        (os.path.join(root, ".claude", "settings.local.json"), root),
    ]


def filesystem_entries(cfg, root):
    """{"allowWrite": [...], "denyWrite": [...], "denyRead": [...]} merged over all settings files, resolved."""
    merged = {"allowWrite": [], "denyWrite": [], "denyRead": []}
    for path, base in settings_paths(cfg, root):
        fs = read_settings(path).get("sandbox", {}).get("filesystem", {})
        for key in merged:
            for entry in fs.get(key, []):
                if any(c in entry for c in "*?["):
                    if key == "allowWrite":
                        continue   # not mirrored: a pattern can't be bound; the folder simply stays read-only
                    raise PolicyError(f"{path}: patterns are not supported in sandbox.filesystem.{key} ({entry})")
                merged[key].append(resolve(entry, base))
    return merged


def build(cfg, cwd, warn):
    root = find_project_root(cwd)
    entries = filesystem_entries(cfg, root)
    writable = []
    for p in [root] + entries["allowWrite"] + cfg.shared_writable:
        if not os.path.isdir(p):
            warn(f"writable folder {p} does not exist; skipped")
            continue
        p = os.path.realpath(p)
        if p not in writable:
            writable.append(p)
    protect, placeholders = [], []
    for w in writable:
        candidates = [os.path.join(w, n) for n in PROTECTED_NAMES]
        git = os.path.join(w, ".git")
        if os.path.isdir(git) and not os.path.islink(git):
            candidates += [os.path.join(git, n) for n in PROTECTED_IN_GIT]
        elif os.path.lexists(git):
            candidates.append(git)
        for c in candidates:
            if w == root:
                # Existing or not: one that exists now may be another confinement's placeholder, gone by the
                # time this policy is used. Missing ones get an empty file while confined (placeholders.py).
                placeholders.append(c)
            elif os.path.lexists(c):
                protect.append(c)
    for p in entries["denyWrite"]:
        if os.path.lexists(p):
            protect.append(p)
        else:
            warn(f"denyWrite path {p} does not exist; skipped")
    pol = {
        "project_root": root,
        "project_key": project_key(root),
        "writable": writable,
        "protect": protect,
        "placeholders": placeholders,
        "hide": cfg.hide + entries["denyRead"],
        "env_dir": cfg.env_dir,
    }
    validate(cfg, pol)
    return pol


def project_key(root):
    return f"{os.path.basename(root) or 'root'}-{hashlib.sha256(root.encode()).hexdigest()[:12]}"


def validate(cfg, pol):
    """Refuse policies that would let confined code change its own confinement or your login files."""
    h = home()
    guarded = [h, cfg.spool, os.path.realpath(cfg.install_dir), os.path.join(h, ".claude")]
    for w in pol["writable"]:
        if w == "/":
            raise PolicyError("refusing to make / writable")
        for g in guarded:
            if inside(g, w):
                raise PolicyError(f"refusing to make {w} writable: it contains {g}")
        for g in guarded[1:]:
            if inside(w, g):
                raise PolicyError(f"refusing to make {w} writable: it is inside {g}")
    if pol["env_dir"] and not any(inside(os.path.realpath(pol["env_dir"]), w) for w in pol["writable"]):
        raise PolicyError(f"env_dir {pol['env_dir']} is not inside a writable folder")


def spool_dir(cfg, pol, *parts):
    return os.path.join(cfg.spool, pol["project_key"], *parts)


def private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def write_atomic(path, data, mode=0o600):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def save(cfg, pol):
    """Save a snapshot of the policy in the spool; returns its path. Snapshots are content-addressed."""
    data = json.dumps(pol, sort_keys=True, indent=1).encode()
    folder = private_dir(spool_dir(cfg, pol, "policies"))
    path = os.path.join(folder, hashlib.sha256(data).hexdigest()[:16] + ".json")
    if not os.path.exists(path):
        write_atomic(path, data, 0o400)
    return path


def load_spool_file(cfg, path):
    """Read a JSON file that must be in the spool (written by confine or its broker, read-only to agents)."""
    real = os.path.realpath(path)
    if not inside(real, cfg.spool) or real == cfg.spool:
        raise PolicyError(f"{path} is not in the spool folder {cfg.spool}")
    with open(real) as f:
        return json.load(f)


def load(cfg, path):
    pol = load_spool_file(cfg, path)
    validate(cfg, pol)
    return pol


def bwrap_args(cfg, pol, socket_path):
    """bwrap arguments (without the command) for this policy. File system state is checked now, at start."""
    args = [
        "--ro-bind", "/", "/",            # everything read-only...
        "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp",                # ...with an empty private /tmp
        "--unshare-user", "--disable-userns",   # no nested namespaces that could undo these rules
        "--die-with-parent",
        "--new-session",                  # no keystroke injection into the calling terminal (TIOCSTI)
    ]
    for w in pol["writable"]:
        if os.path.isdir(w):
            args += ["--bind", w, w]
    for p in pol["protect"] + pol["placeholders"]:
        if os.path.lexists(p):
            real = os.path.realpath(p)
            if os.path.exists(real) and any(inside(real, w) for w in pol["writable"]):
                args += ["--ro-bind", real, real]
        elif p in pol["placeholders"] and os.path.isdir(os.path.dirname(p)):
            args += ["--ro-bind", "/dev/null", p]
    for h in pol["hide"]:
        if os.path.isdir(h):
            args += ["--tmpfs", h, "--remount-ro", h]
        elif os.path.exists(h):
            args += ["--ro-bind", "/dev/null", h]
    args += ["--bind", socket_path, config.SOCKET_INSIDE]
    for c in config.PROXIED + config.REFUSED:
        target = os.path.join(config.STANDIN_TARGET_DIR, c)
        if os.path.exists(target):
            args += ["--ro-bind", cfg.standin, target]
    return args


def inner_env(pol, restore):
    """Variables to set inside the confinement: those the broker removed, then confine's own."""
    env = dict(restore)
    if pol["env_dir"]:
        d = pol["env_dir"]
        env.update(TMPDIR=os.path.join(d, "tmp"), XDG_CACHE_HOME=os.path.join(d, "cache"),
                   CONDA_PKGS_DIRS=os.path.join(d, "conda_pkgs"))
    env["PYTHONNOUSERSITE"] = "1"
    env["AGENT_SLURM_PROJECT"] = pol["project_key"]
    return env

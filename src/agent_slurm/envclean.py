"""Environment cleaning for the real SLURM commands the broker runs.

A job's environment is copied from the (confined) process that submitted it. On the compute node, the job's wrapper
script, `confine` and bwrap run *before* the confinement exists, so variables that make programs load or run other
code (LD_PRELOAD, BASH_ENV, exported bash functions, ...) must not reach them. The broker removes these variables
from the environment of the real command and stores them in the spool; `confine` sets them again inside the
confinement, where they are harmless.
"""

# glibc ignores these for setuid programs (its "unsecure" list); they load code or read files.
GLIBC_UNSECURE = {
    "GCONV_PATH", "GETCONF_DIR", "GLIBC_TUNABLES", "HOSTALIASES", "LOCALDOMAIN", "LOCPATH", "MALLOC_TRACE",
    "NIS_PATH", "NLSPATH", "RESOLV_HOST_CONF", "RES_OPTIONS", "TMPDIR", "TZDIR",
}
# Shells and interpreters that read code or options from these.
CODE_VARS = {
    "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4", "IFS", "CDPATH", "GLOBIGNORE", "PROMPT_COMMAND",
    "PERL5LIB", "PERL5OPT", "PERLLIB", "RUBYLIB", "RUBYOPT", "NODE_OPTIONS",
}
CODE_PREFIXES = ("LD_", "BASH_FUNC_", "PYTHON", "MALLOC_")

# Input variables of SLURM commands: removed and not restored (the confinement has its own SLURM context).
SLURM_INPUT_PREFIXES = ("SBATCH_", "SALLOC_", "SRUN_")
# The SLURM_* variables a nested srun keeps; the same list as snakemake-executor-plugin-slurm-jobstep.
SRUN_KEEP_SLURM = {
    "SLURM_JOB_ID", "SLURM_JOBID", "SLURM_ARRAY_JOB_ID", "SLURM_ARRAY_TASK_ID", "SLURM_ARRAY_TASK_MIN",
    "SLURM_ARRAY_TASK_MAX", "SLURM_ARRAY_TASK_STEP", "SLURM_CLUSTER_NAME", "SLURM_SUBMIT_DIR", "SLURM_SUBMIT_HOST",
}
# Read-only commands get only these.
QUERY_KEEP = {"HOME", "USER", "LOGNAME", "LANG", "TZ", "SLURM_TIME_FORMAT"}
QUERY_PATH = "/usr/bin:/bin"


def is_code_var(name):
    return name in GLIBC_UNSECURE or name in CODE_VARS or name.startswith(CODE_PREFIXES)


def split_env(env, mode):
    """Split `env` for a real command run in `mode` ("sbatch", "srun" or "query").

    Returns (clean, restore): the environment for the real command, and the removed variables to set again inside
    the confinement that the command starts.
    """
    if mode == "query":
        clean = {k: v for k, v in env.items() if k in QUERY_KEEP or k.startswith("LC_")}
        clean["PATH"] = QUERY_PATH
        return clean, {}
    clean, restore = {}, {}
    for name, value in env.items():
        if is_code_var(name):
            restore[name] = value
        elif name.startswith(SLURM_INPUT_PREFIXES) or name == "SLURM_CONF":
            continue
        elif name.startswith("SLURM_") and (mode == "sbatch" or name not in SRUN_KEEP_SLURM):
            continue
        else:
            clean[name] = value
    return clean, restore

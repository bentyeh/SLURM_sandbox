"""Environment cleaning and log-name patterns (pure Python)."""
from agent_slurm.envclean import split_env
from agent_slurm.jobentry import expand

ENV = {
    "PATH": "/opt/conda/bin:/usr/bin", "HOME": "/home/u", "USER": "u", "LANG": "C.UTF-8", "LC_ALL": "C",
    "LD_PRELOAD": "/repo/evil.so", "LD_LIBRARY_PATH": "/opt/lib", "BASH_ENV": "/repo/evil.sh", "ENV": "/x",
    "BASH_FUNC_mkdir%%": "() { evil; }", "GCONV_PATH": "/repo/gconv", "PYTHONPATH": "/repo", "TMPDIR": "/t",
    "SLURM_CONF": "/repo/slurm.conf", "SLURM_JOB_ID": "5", "SLURM_CPUS_ON_NODE": "8",
    "SLURM_TASK_PROLOG": "/repo/p.sh", "SBATCH_EXPORT": "ALL,LD_PRELOAD=/x", "SRUN_CPUS_PER_TASK": "2",
    "MY_VAR": "kept",
}
CODE = {"LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV", "BASH_FUNC_mkdir%%", "GCONV_PATH", "PYTHONPATH",
        "TMPDIR"}


def test_sbatch_env():
    clean, restore = split_env(ENV, "sbatch")
    assert set(restore) == CODE   # set again inside the confinement
    assert not set(clean) & CODE
    assert not any(k.startswith(("SLURM_", "SBATCH_", "SRUN_")) for k in clean)
    assert clean["PATH"] == ENV["PATH"] and clean["MY_VAR"] == "kept"


def test_srun_env():
    clean, restore = split_env(ENV, "srun")
    assert set(restore) == CODE
    assert clean["SLURM_JOB_ID"] == "5"
    for gone in ("SLURM_CPUS_ON_NODE", "SLURM_TASK_PROLOG", "SLURM_CONF", "SRUN_CPUS_PER_TASK"):
        assert gone not in clean and gone not in restore


def test_query_env():
    clean, restore = split_env(ENV, "query")
    assert restore == {}
    assert clean == {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "USER": "u", "LANG": "C.UTF-8", "LC_ALL": "C"}


def test_log_patterns():
    env = {"SLURM_JOB_ID": "42", "SLURM_JOB_NAME": "count", "USER": "u"}
    assert expand("logs/%x_%j.out", env) == "logs/count_42.out"
    assert expand("%6j-%u-%%-%a", env) == "000042-u-%-4294967294"
    arr = dict(env, SLURM_ARRAY_JOB_ID="40", SLURM_ARRAY_TASK_ID="13")
    assert expand("slurm-%A_%a-%b.out", arr) == "slurm-40_13-3.out"
    assert expand("%q stays", env) == "%q stays"

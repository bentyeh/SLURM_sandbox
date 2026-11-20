#!/usr/bin/python3
"""Fake SLURM commands for the local tests, linked as sbatch, srun, scancel, scontrol, squeue, ...

Every call is appended to state/calls.jsonl next to the links. `sbatch` runs the batch script synchronously, as
slurmstepd would (environment as submitted, working folder from --chdir, output to --output), unless the job is
held (--hold); `sbatch --fake-release ID` runs a held job later. `scontrol show job -o ID` reports the job's
Command; tests register foreign jobs in state/jobs.json.
"""
import json
import os
import subprocess
import sys

BIN = os.path.dirname(os.path.abspath(sys.argv[0]))
STATE = os.path.join(BIN, "state")
CMD = os.path.basename(sys.argv[0])
ARGS = sys.argv[1:]


def load(name, default):
    try:
        with open(os.path.join(STATE, name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(name, data):
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, name), "w") as f:
        json.dump(data, f, indent=1)


def record(**extra):
    os.makedirs(STATE, exist_ok=True)
    entry = {"cmd": CMD, "argv": ARGS, "env": dict(os.environ), "cwd": os.getcwd(), **extra}
    with open(os.path.join(STATE, "calls.jsonl"), "a") as f:
        f.write(json.dumps(entry) + "\n")


def opt(args, name):
    values = [a.split("=", 1)[1] for a in args if a.startswith(f"--{name}=")]
    return values[-1] if values else None


def run_job(jobs, jid):
    job = jobs[jid]
    env = dict(job["env"], SLURM_JOB_ID=jid, SLURM_JOBID=jid, SLURM_JOB_NAME=opt(job["args"], "job-name") or "",
               USER=os.environ.get("USER", ""))
    out = (opt(job["args"], "output") or "slurm-%j.out").replace("%j", jid).replace("%A", jid)
    out = out.replace("%a", "4294967294")
    with open(out, "w") as f:
        rc = subprocess.call([job["command"]], cwd=opt(job["args"], "chdir"), env=env, stdout=f, stderr=f,
                             stdin=subprocess.DEVNULL)
    job["state"] = f"done:{rc}"
    save("jobs.json", jobs)


def sbatch():
    jobs = load("jobs.json", {})
    if ARGS[:1] == ["--fake-release"]:
        run_job(jobs, ARGS[1])
        return 0
    jid = str(load("counter.json", 1000) + 1)
    save("counter.json", int(jid))
    script = ARGS[-1]
    with open(script) as f:
        content = f.read()
    record(script=content)
    jobs[jid] = {"command": script, "args": ARGS, "env": dict(os.environ), "state": "pending"}
    save("jobs.json", jobs)
    if "--hold" in ARGS:
        jobs[jid]["state"] = "held"
        save("jobs.json", jobs)
    else:
        run_job(jobs, jid)
    print(jid if "--parsable" in ARGS else f"Submitted batch job {jid}")
    return 0


def srun():
    record()
    i = 0
    while i < len(ARGS) and ARGS[i].startswith("-"):
        i += 1
    env = dict(os.environ)
    env.setdefault("SLURM_JOB_ID", "999")
    env["SLURM_STEP_ID"] = "0"
    os.execve(ARGS[i], ARGS[i:], env)


def scontrol():
    record()
    if "show" in ARGS and "job" in ARGS:
        jid = ARGS[-1]
        jobs = load("jobs.json", {})
        base = jid.split("_")[0]
        job = jobs.get(jid) or jobs.get(base)
        if job is None:
            print("slurm_load_jobs error: Invalid job id specified", file=sys.stderr)
            return 1
        print(f"JobId={jid} JobName=x UserId=u(1) JobState=PENDING Command={job['command']} WorkDir=/")
        return 0
    print("fake scontrol " + " ".join(ARGS))
    return 0


def main():
    if CMD == "sbatch":
        return sbatch()
    if CMD == "srun":
        return srun()
    if CMD == "scontrol":
        return scontrol()
    record()
    print(f"fake {CMD} " + " ".join(ARGS))
    return 0


if __name__ == "__main__":
    sys.exit(main())

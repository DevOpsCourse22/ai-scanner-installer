"""Simulated end-to-end runs of the install and destroy plans.

Every plan goes through installer.runner (its thread, its log, its redactor,
its wait_for_user pauses) with the outside world faked:

  Ctx.sh            records every command; answers kubectl/helm/aws/terraform/
                    ssh with canned output. Three things run for real:
                      - `kubectl kustomize` on environments/<env>/<app> as the
                        plan wrote it (base/ copy of the deployment repo + overlay),
                      - git on the installer repository: its "GitHub" clone URL is
                        mapped to a bare repository in tmp, so clone, ls-remote,
                        checkout, add, commit, pull and push are real,
                      - `kubectl exec -i deploy/gateway -- python -`: the default-AI
                        script runs in a local python against a stub of the
                        gateway's review_gateway.site_settings / db.
                    The three *-deployment clones are copies of the real working
                    trees; any git command on them fails the test.
  kube.flavour      returns the cluster kind for the scenario
  kube.subprocess   (ssh scenario) the `ssh ... cat k3s.yaml` call
  github.*          webhooks, the smee channel, and file_text, which serves the
                    real Jenkinsfile / ci/pod.yaml of the three code repositories
  httpx             a fake Jenkins (crumb, jobs, builds) via MockTransport
  subprocess.Popen  the kubectl port-forward of PortForward
  time.sleep        a fake clock, so timeouts and polls cost nothing

Nothing here talks to AWS, GitHub or a cluster.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import yaml

REPOS_SRC = Path(r"C:\Projects\Final-Project-Repos")
REAL_POPEN = subprocess.Popen
REAL_CLIENT = httpx.Client

CODE_REPOS = ("ai-scanner-backend", "ai-scanner-gateway", "ai-scanner-frontend")
DEPLOY_REPOS = tuple(r + "-deployment" for r in CODE_REPOS)
APPS = ("backend", "gateway", "frontend")
DEFAULT_INSTALLER = "DevOpsCourse22/ai-scanner-installer"

KUBECTL = shutil.which("kubectl")
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(
    KUBECTL is None or GIT is None
    or not all((REPOS_SRC / r).is_dir() for r in DEPLOY_REPOS)
    or not all((REPOS_SRC / r / "Jenkinsfile").is_file() and (REPOS_SRC / r / "ci" / "pod.yaml").is_file()
               for r in CODE_REPOS),
    reason="needs kubectl and git on PATH and the six project repositories next to this one")

# --- secrets the logs must never show ---------------------------------------------
TOKEN = "ghp_SIMTOKEN0123456789abcdef"
SUDO = "sudo-Pa55-word-sim"
AWS_KEY_ID = "AKIASIMULATED000001"
AWS_SECRET = "awsSECRETkey/0123456789abcdefSIM"
API_KEY = "sk-SIMKEY-abcdef1234567890"
TS_ID = "kSIMclientid123"
TS_SECRET = "tskey-client-SIMSECRET-0987654321"
OAUTH_ID = "Ov23liSIMclientid"
OAUTH_SECRET = "oauth-SIM-secret-98765432100abcdef"
ARGO_PW = "argo-initial-pw-SIM123"
PRIVATE_KEY = ("-----BEGIN OPENSSH PRIVATE KEY-----\r\n"
               "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\r\n"
               "QyNTUxOQAAACBSIMULATEDKEYSIMULATEDKEYSIMULATEDKEYSIMULATEDKEYAAAA\r\n"
               "-----END OPENSSH PRIVATE KEY-----\r\n")

ECR = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
EFS = "fs-0123456789abcdef0"
ROLE = "arn:aws:iam::123456789012:role/ai-scan-repos-jenkins"
CERT = "arn:aws:acm:us-east-1:123456789012:certificate/abcd-1234"
LB = "a1b2c3-123456.elb.us-east-1.amazonaws.com"
JENKINS_LB = "j9k8l7-654321.elb.us-east-1.amazonaws.com"
SMEE = "https://smee.io/SimChannel123"
DOMAIN = "scan.example.com"
SSH_HOST = "k3s.example.lan"

# A stand-in for the gateway's own settings code, which the default-AI step drives
# through `kubectl exec -i deploy/gateway -- python -`. The "database" is a JSON file.
STUB_DB = '''
class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def get_session():
    return _Session()
'''
STUB_SETTINGS = '''
import json, os
from pathlib import Path

_DB = Path(os.environ["SIM_AI_DB"])


def ai_public(db):
    if not _DB.exists():
        return {"configured": False}
    return {"configured": True, "preset": json.loads(_DB.read_text())["preset"]}


def save_ai(db, *, preset, endpoint, model, api_key, clear_key, who):
    _DB.write_text(json.dumps({"preset": preset, "endpoint": endpoint, "model": model,
                               "api_key": api_key, "clear_key": clear_key, "who": who}))
    return ai_public(db)
'''


# --- the simulation ---------------------------------------------------------------

@dataclass
class Call:
    args: list[str]
    input: str | None
    env: dict[str, str]
    cwd: str | None
    quiet: bool
    step: str


@dataclass
class Sim:
    tmp: Path
    flavour: dict[str, Any]
    origin: Path = Path()                                         # bare "GitHub" installer repo
    git_env: dict[str, str] = field(default_factory=dict)
    stub: Path = Path()
    ai_db: Path = Path()
    installer_dir: Path | None = None
    calls: list[Call] = field(default_factory=list)
    clones: list[list[str]] = field(default_factory=list)
    rendered: dict[str, str] = field(default_factory=dict)        # app -> kustomize output
    rendered_dirs: dict[str, Path] = field(default_factory=dict)
    overlay_files: dict[str, dict[str, str]] = field(default_factory=dict)
    file_reads: list[tuple] = field(default_factory=list)
    live_secrets: dict[tuple[str, str], str] = field(default_factory=dict)
    webhooks: list[tuple] = field(default_factory=list)
    deleted_hooks: list[tuple] = field(default_factory=list)
    smee_calls: int = 0
    portforwards: list[list[str]] = field(default_factory=list)
    jenkins: list[tuple[str, str, str]] = field(default_factory=list)  # (method, path, auth header)
    ssh_runs: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    clock: float = 1_000_000.0
    extra: Callable[[list[str], "Call"], tuple[int, str] | None] | None = None

    # --- real git, on tmp repositories only ---
    def real_git(self, args: list[str], cwd: Path | None = None) -> tuple[int, str]:
        p = REAL_POPEN([GIT, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       env=self.git_env, cwd=str(cwd) if cwd else None)
        out, err = p.communicate(timeout=120)
        return p.returncode, out if p.returncode == 0 else out + err

    def git_out(self, *args: str) -> str:
        code, out = self.real_git(list(args))
        assert code == 0, out
        return out

    def origin_git(self, *args: str) -> str:
        return self.git_out("--git-dir", str(self.origin), *args)

    def fake_git(self, args: list[str], call: Call) -> tuple[int, str]:
        assert TOKEN not in " ".join(args)
        if args[1] == "clone":
            url, dest = args[-2], Path(args[-1])
            assert url.startswith("https://x-access-token@github.com/") and url.endswith(".git"), url
            self.clones.append(args)
            name = url.rsplit("/", 1)[1].removesuffix(".git")
            if name == "ai-scanner-installer":
                self.installer_dir = dest
                return self.real_git(["clone", "--quiet", str(self.origin), str(dest)])
            assert name in DEPLOY_REPOS, f"unexpected clone {url}"
            shutil.copytree(REPOS_SRC / name, dest, ignore=shutil.ignore_patterns(".git"))
            return 0, ""
        # every other git command must run inside the installer clone; a commit or
        # push in a *-deployment clone would fail here
        assert args[1] == "-C", args
        assert self.installer_dir is not None and Path(args[2]) == self.installer_dir, \
            f"git on something other than the installer repository: {args}"
        return self.real_git(args[1:])

    # --- Ctx.sh ---
    def respond(self, args: list[str], call: Call) -> tuple[int, str]:
        if self.extra:
            got = self.extra(args, call)
            if got is not None:
                return got
        if args[:2] == ["kubectl", "kustomize"]:
            d = Path(args[2])
            assert d.parent.parent.name == "environments" and d.name in APPS, d
            p = REAL_POPEN([KUBECTL, "kustomize", str(d)], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
            out, _ = p.communicate(timeout=120)
            self.rendered[d.name] = out
            self.rendered_dirs[d.name] = d
            self.overlay_files[d.name] = {f.name: f.read_text(encoding="utf-8") for f in d.iterdir() if f.is_file()}
            return p.returncode, out
        if args[0] == "git":
            return self.fake_git(args, call)
        if args[0] == "terraform" and args[1] == "output":
            return 0, json.dumps({k: {"value": v, "type": "x", "sensitive": False} for k, v in {
                "cluster_name": "ai-scan-repos", "region": "us-east-1", "ecr_registry": ECR,
                "efs_id": EFS, "jenkins_role_arn": ROLE, "acm_certificate_arn": CERT,
                "acm_validation": [{"name": f"_abc.{DOMAIN}.", "type": "CNAME",
                                    "value": "_xyz.acm-validations.aws."}]}.items()})
        if args[0] == "aws":
            if args[1] == "sts":
                return 0, "arn:aws:iam::123456789012:user/sim\n"
            return 0, "Updated context in kubeconfig\n"
        if args[0] == "ssh":
            return 0, ""          # `cat registries.yaml` -> empty: not configured yet
        if args[0] == "kubectl":
            rest = args[3:] if args[1] == "--kubeconfig" else args[1:]
            if rest[:2] == ["get", "storageclass"]:
                return 0, json.dumps({"items": [{"metadata": {"name": "standard", "annotations": {
                    "storageclass.kubernetes.io/is-default-class": "true"}},
                    "provisioner": "k8s.io/minikube-hostpath"}]})
            if "applications" in rest and "get" in rest:
                return 0, json.dumps({"items": [
                    {"metadata": {"name": n}, "status": {"sync": {"status": "Synced"},
                                                         "health": {"status": "Healthy"}}}
                    for n in ("ai-scan-backend", "ai-scan-gateway", "ai-scan-frontend")]})
            if "exec" in rest:
                if rest[-2:] == ["python", "-"]:
                    return self.gateway_python(call)
                return 0, '{"status":"ok"}\n'
            if "argocd-initial-admin-secret" in rest:
                return 0, base64.b64encode(ARGO_PW.encode()).decode()
            if "get" in rest and "secret" in rest:
                name = rest[rest.index("secret") + 1]
                jp = next((x for x in rest if x.startswith("jsonpath=")), "")
                key = jp.removeprefix("jsonpath={.data.").removesuffix("}")
                if (name, key) in self.live_secrets:
                    return 0, base64.b64encode(self.live_secrets[(name, key)].encode()).decode()
                return 1, f'Error from server (NotFound): secrets "{name}" not found\n'
            if rest[2:4] == ["get", "serviceaccount"] or rest[:2] == ["get", "serviceaccount"]:
                return 0, "NAME      SECRETS   AGE\ndefault   0         1s\n"
            if rest[:2] == ["get", "nodes"]:
                return 0, "NAME    STATUS   ROLES    AGE   VERSION\nnode1   Ready    master   1d    v1\n"
            if "svc" in rest and "get" in rest:
                return 0, JENKINS_LB if "jenkins" in rest else LB
            return 0, ""
        return 0, ""

    def gateway_python(self, call: Call) -> tuple[int, str]:
        """`kubectl exec -i deploy/gateway -- python -`: run the script on stdin locally,
        against the stub review_gateway package."""
        assert call.input, "the script must come in on stdin"
        env = {**os.environ, "PYTHONPATH": str(self.stub), "SIM_AI_DB": str(self.ai_db)}
        p = REAL_POPEN([sys.executable, "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, env=env, cwd=str(self.tmp))
        out, _ = p.communicate(call.input, timeout=60)
        return p.returncode, out

    def cmds(self) -> list[str]:
        return [" ".join(c.args) for c in self.calls]

    def index(self, pred: Callable[[Call], bool], start: int = 0) -> int:
        for i, c in enumerate(self.calls[start:], start):
            if pred(c):
                return i
        raise AssertionError("no such command; ran:\n" + "\n".join(self.cmds()))

    def assert_order(self, *preds: Callable[[Call], bool]) -> None:
        pos = -1
        for p in preds:
            pos = self.index(p, pos + 1)

    def sleep(self, s: float) -> None:
        self.clock += s

    def time(self) -> float:
        return self.clock


def has(*words: str) -> Callable[[Call], bool]:
    return lambda c: all(any(w == a or w in a for a in c.args) for w in words)


def applies(text: str) -> Callable[[Call], bool]:
    return lambda c: "apply" in c.args and c.input is not None and text in c.input


def is_ai_exec(c: Call) -> bool:
    return c.args[0] == "kubectl" and "exec" in c.args and c.args[-2:] == ["python", "-"]


def secret(name: str) -> Callable[[Call], bool]:
    def p(c: Call) -> bool:
        if "apply" not in c.args or not c.input:
            return False
        try:
            obj = json.loads(c.input)
        except ValueError:
            return False
        return obj.get("kind") == "Secret" and obj["metadata"]["name"] == name
    return p


def secret_data(sim: Sim, name: str) -> dict[str, str]:
    c = sim.calls[sim.index(secret(name))]
    return {k: base64.b64decode(v).decode() for k, v in json.loads(c.input)["data"].items()}


def docs(text: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(text) if d]


def find(objs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    return next((o for o in objs if o["kind"] == kind and o["metadata"]["name"] == name), None)


def env_of(container: dict[str, Any]) -> dict[str, Any]:
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in container.get("env", [])}


@pytest.fixture()
def sim(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("INSTALLER_DATA", str(data))
    monkeypatch.delenv("INSTALLER_REPO", raising=False)
    from installer import github, kube, plans, runner
    importlib.reload(plans)                 # picks up INSTALLER_DATA
    tf = tmp_path / "tf"
    tf.mkdir()
    (tf / "main.tf").write_text("# fake\n", encoding="utf-8")
    (tf / "outputs.tf").write_text("# fake\n", encoding="utf-8")
    monkeypatch.setattr(plans, "TF_DIR", tf)
    assert plans.DATA == data
    monkeypatch.setattr(runner, "_current", None)

    s = Sim(tmp=tmp_path, flavour={})

    # git: no user/system config (no hooks, signing or autocrlf), a fixed identity
    gitcfg = tmp_path / "gitconfig"
    gitcfg.write_text("", encoding="utf-8")
    s.git_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(gitcfg),
                 "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "sim", "GIT_AUTHOR_EMAIL": "sim@example.invalid",
                 "GIT_COMMITTER_NAME": "sim", "GIT_COMMITTER_EMAIL": "sim@example.invalid"}
    # the installer repository on "GitHub": a bare repo whose main holds just a README
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("ai-scanner-installer\n", encoding="utf-8")
    s.git_out("-C", str(seed), "init", "--quiet", "-b", "main")
    s.git_out("-C", str(seed), "add", "README.md")
    s.git_out("-C", str(seed), "commit", "--quiet", "-m", "init")
    s.origin = tmp_path / "origin.git"
    s.git_out("clone", "--quiet", "--bare", str(seed), str(s.origin))

    # the gateway stub for the default-AI step
    s.stub = tmp_path / "stub"
    (s.stub / "review_gateway").mkdir(parents=True)
    (s.stub / "review_gateway" / "__init__.py").write_text("", encoding="utf-8")
    (s.stub / "review_gateway" / "db.py").write_text(STUB_DB, encoding="utf-8")
    (s.stub / "review_gateway" / "site_settings.py").write_text(STUB_SETTINGS, encoding="utf-8")
    s.ai_db = tmp_path / "ai-setting.json"

    def fake_sh(self, args, *, input=None, check=True, env=None, quiet=False, cwd=None, timeout=None):
        args = [str(a) for a in args]
        call = Call(args, input, {**(env or {})}, cwd, quiet, self.step_id)
        s.calls.append(call)
        if not quiet:
            self.log("$ " + " ".join(args), "cmd")
        code, out = s.respond(args, call)
        if not quiet:
            for line in out.splitlines():
                self.log(line, "out")
        if check and code != 0:
            tail = self.run.redact(out.strip().splitlines()[-1] if out.strip() else "")
            raise runner.StepFailed(f"`{' '.join(args[:3])}` failed (exit {code}). {tail}")
        return out

    monkeypatch.setattr(runner.Ctx, "sh", fake_sh)
    monkeypatch.setattr(kube, "flavour", lambda *a, **kw: dict(s.flavour))

    def ensure(token, owner, repo, url, *rest, **kw):
        s.webhooks.append((token, owner, repo, url))
        return 1000 + len(s.webhooks)

    def delete(token, owner, repo, *rest, **kw):
        s.deleted_hooks.append((token, owner, repo, *rest, *kw.values()))
        return "removed"
    monkeypatch.setattr(github, "ensure_webhook", ensure)
    monkeypatch.setattr(github, "delete_webhook", delete)

    def file_text(token, owner, repo, path, ref="main"):
        s.file_reads.append((token, owner, repo, path, ref))
        f = REPOS_SRC / repo / path
        return f.read_text(encoding="utf-8") if f.is_file() else ""
    monkeypatch.setattr(github, "file_text", file_text)

    def smee():
        s.smee_calls += 1
        return SMEE
    monkeypatch.setattr(github, "new_smee_channel", smee)

    class FakePopen:
        def __init__(self, args, **kw):
            s.portforwards.append([str(a) for a in args])

        def terminate(self):
            pass

    monkeypatch.setattr(plans, "subprocess", types.SimpleNamespace(
        Popen=FakePopen, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE,
        SubprocessError=subprocess.SubprocessError, run=subprocess.run))
    monkeypatch.setattr(plans, "time", types.SimpleNamespace(time=s.time, sleep=s.sleep))

    builds: dict[str, int] = {}             # job -> number of its last build
    polls: dict[str, int] = {}              # job -> polls since its last trigger

    def jenkins(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        s.jenkins.append((req.method, path, req.headers.get("authorization", "")))
        if path == "/crumbIssuer/api/json":
            return httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "c1"})
        parts = path.split("/")
        if len(parts) > 2 and parts[1] == "job" and parts[2] not in CODE_REPOS:
            return httpx.Response(404)              # e.g. a multibranch /job/<repo>/job/main
        if path.endswith("/lastBuild/api/json"):
            job = parts[2]
            if job not in builds:
                return httpx.Response(404)          # a fresh install: no build yet
            polls[job] = polls.get(job, 0) + 1
            if polls[job] == 1:
                return httpx.Response(200, json={"building": True, "result": None, "number": builds[job]})
            return httpx.Response(200, json={"building": False, "result": "SUCCESS", "number": builds[job]})
        if path.endswith("/build"):
            assert req.headers.get("Jenkins-Crumb") == "c1"
            builds[parts[2]] = builds.get(parts[2], 0) + 1
            polls[parts[2]] = 0
            return httpx.Response(201)
        if path == f"/job/{parts[2] if len(parts) > 2 else ''}/api/json":
            return httpx.Response(200, json={"name": parts[2]})
        return httpx.Response(404)

    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(jenkins)
        return REAL_CLIENT(*a, **kw)
    monkeypatch.setattr(plans, "httpx", types.SimpleNamespace(Client=client, HTTPError=httpx.HTTPError))

    s.plans, s.runner, s.kube, s.github = plans, runner, kube, github  # type: ignore[attr-defined]
    return s


def default_answer(action: dict[str, Any]) -> dict[str, str]:
    names = [f["name"] for f in action.get("fields", [])]
    if "client_id" in names:
        return {"client_id": OAUTH_ID + " ", "client_secret": " " + OAUTH_SECRET}
    return {}


def run_plan(sim: Sim, a: dict[str, Any], kind: str = "install",
             answer: Callable[[dict[str, Any]], dict[str, str]] = default_answer, timeout: float = 180):
    """Start the plan through runner.start, like app.start_run, and press
    Continue on every pause."""
    plans, runner = sim.plans, sim.runner      # type: ignore[attr-defined]
    plans.ensure_generated(a)
    steps = plans.destroy_plan(a) if kind == "destroy" else plans.install_plan(a)
    work = sim.tmp / "work"
    work.mkdir(exist_ok=True)
    run = runner.start(kind, steps, a, plans.secret_values(a), str(work))
    answered: set[int] = set()
    end = time.monotonic() + timeout
    while run.status not in ("done", "failed"):
        assert time.monotonic() < end, f"run stuck: {run.public()}"
        with run.cond:
            run.cond.wait(0.05)
        act = run.action
        # wait_for_user publishes status "waiting" before it resets reply/resume;
        # answer only once resume is cleared, and again if an answer got lost.
        if run.status == "waiting" and act is not None and not run.resume.is_set():
            time.sleep(0.02)
            if run.status != "waiting" or run.action is not act or run.resume.is_set():
                continue
            if id(act) not in answered:
                answered.add(id(act))
                sim.prompts.append(dict(act))
            run.reply = answer(act)
            run.resume.set()
    return run


def log_text(run) -> str:
    return "\n".join(line["text"] for line in run.lines) + "\n" + \
        "\n".join(s.detail for s in run.steps)


def assert_no_secrets(sim: Sim, run, a: dict[str, Any], extra: tuple[str, ...] = ()) -> None:
    plans = sim.plans                           # type: ignore[attr-defined]
    # the smee channel URL sits in answers.generated, so secret_values() lists it, but the run
    # that creates it logs it in clear (see qa/simulated-runs.md); only credentials are checked here
    g = a.get("generated", {})
    values = (set(plans.secret_values(a)) | set(extra)) - {g.get("smee_url"), g.get("webhook_url")}
    values = {v for v in values if v and len(v) >= 4}
    text = log_text(run)
    leaked = [v for v in values if v in text]
    assert not leaked, f"secrets in the run log: {leaked}"
    # and never on a command line (visible in `ps` on the host)
    argv = "\n".join(sim.cmds())
    on_cmdline = [v for v in values if v in argv]
    assert not on_cmdline, f"secrets on a command line: {on_cmdline}"


def assert_done(run) -> None:
    failed = [(s.id, s.detail) for s in run.steps if s.status == "failed"]
    assert run.status == "done", f"run {run.status}: {failed}\n" + log_text(run)[-3000:]
    assert all(s.status == "done" for s in run.steps), [(s.id, s.status) for s in run.steps]


def jenkins_values(sim: Sim) -> list[dict[str, Any]]:
    out = []
    for c in sim.calls:
        if "helm" == c.args[0] and "jenkins/jenkins" in c.args:
            v = yaml.safe_load(c.input)
            scripts = v["controller"]["JCasC"]["configScripts"]
            env = yaml.safe_load(scripts["ci-registry"])["jenkins"]["globalNodeProperties"][0]["envVars"]["env"]
            out.append({"values": v, "env": {e["key"]: e["value"] for e in env}, "jobs": scripts["ci-jobs"]})
    return out


def check_jenkins(sim: Sim, env: str, inst: str, registry_kind: str, registry: str, owner: str = "sim-owner",
                  aws_region: str = "") -> list[dict[str, Any]]:
    """One pipelineJob per code repository, reading its derived Jenkinsfile from env/<env>."""
    jv = jenkins_values(sim)
    assert jv
    want = {"REGISTRY_KIND": registry_kind, "REGISTRY": registry, "CODE_OWNER": owner,
            "INSTALLER_REPO": inst, "DEPLOY_BRANCH": f"env/{env}", "DEPLOY_ENV": env}
    if aws_region:
        want["AWS_REGION"] = aws_region
    for v in jv:
        assert v["env"] == want
        jobs = v["jobs"]
        assert "multibranch" not in jobs.lower()
        for repo in CODE_REPOS:
            assert f"pipelineJob('{repo}')" in jobs
            assert f"scriptPath('environments/{env}/ci/{repo}.Jenkinsfile')" in jobs
        assert jobs.count(f"url('https://github.com/{inst}.git')") == 3
        assert jobs.count(f"branch('*/env/{env}')") == 3
        assert "-deployment" not in jobs
    # the first build: /job/<repo>/build (no /job/main), crumb, basic auth
    posts = [p for m, p, _ in sim.jenkins if m == "POST"]
    assert posts == [f"/job/{j}/build" for j in CODE_REPOS]
    assert not any("/job/main" in p for _, p, _ in sim.jenkins)
    assert all(auth.startswith("Basic ") for _, _, auth in sim.jenkins)
    return jv


def check_rendered(sim: Sim) -> dict[str, list[dict[str, Any]]]:
    assert sorted(sim.rendered) == sorted(APPS)
    out = {}
    for app in APPS:
        out[app] = docs(sim.rendered[app])
        assert out[app], f"{app}: kustomize rendered nothing"
        assert sim.overlay_files[app]["kustomization.yaml"].count("  - base\n") == 1
    return out


def tree_digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.relative_to(root).parts}


def check_installer_branch(sim: Sim, env: str, inst: str, url: str) -> None:
    """The six project repositories are only read; everything goes to env/<env>
    of the installer repository."""
    branch = f"env/{env}"
    # clones: the three deployment repos (shallow, read-only), then the installer repo
    deploy = [c for c in sim.clones if "-deployment.git" in c[-2]]
    assert [c[-2] for c in deploy] == [f"https://x-access-token@github.com/sim-owner/{r}.git" for r in DEPLOY_REPOS]
    assert all("--depth" in c for c in deploy)
    assert sim.clones[-1][-2] == f"https://x-access-token@github.com/{inst}.git"
    for c in sim.calls:
        if c.args[0] == "git" and ("clone" in c.args or "push" in c.args or "ls-remote" in c.args):
            assert c.env.get("GIT_TOKEN") == TOKEN and c.env.get("GIT_ASKPASS"), c.args
    # pushes: only from the installer clone, only to env/<env>
    pushes = [c for c in sim.calls if c.args[0] == "git" and "push" in c.args]
    assert pushes
    for c in pushes:
        assert Path(c.args[2]) == sim.installer_dir and c.args[-2:] == ["origin", f"HEAD:{branch}"], c.args
    assert not any("-deployment" in a for c in sim.calls if c.args[0] == "git" and c.args[1] == "-C" for a in c.args)
    # the deployment clones are exactly what was read
    repos = Path(sim.installer_dir).parent
    for r in DEPLOY_REPOS:
        assert tree_digest(repos / r) == tree_digest(REPOS_SRC / r), f"{r} was modified"
    # "GitHub": main untouched, the install's branch holds environments/<env> only
    refs = sim.origin_git("for-each-ref", "--format=%(refname)").split()
    assert sorted(refs) == sorted(["refs/heads/main", f"refs/heads/{branch}"])
    assert sim.origin_git("ls-tree", "-r", "--name-only", "main").split() == ["README.md"]
    files = sim.origin_git("ls-tree", "-r", "--name-only", branch).split()
    assert all(f == "README.md" or f.startswith(f"environments/{env}/") for f in files), files
    assert f"installer: environments/{env} for {url}" in sim.origin_git("log", "--format=%s", branch).splitlines()
    for app, repo in zip(APPS, DEPLOY_REPOS):
        p = f"environments/{env}/{app}/"
        assert p + "kustomization.yaml" in files and p + "app-patch.yaml" in files
        assert p + "base/kustomization.yaml" in files
        base = [f[len(p + "base/"):] for f in files if f.startswith(p + "base/")]
        assert not any(x.split("/")[0] in (".github", "overlays", "platform", "job-test", "tailscale-operator")
                       or x.endswith(".md") for x in base), base
    # the derived Jenkinsfiles and the original pod templates
    for repo in CODE_REPOS:
        assert (TOKEN, "sim-owner", repo, "Jenkinsfile", "main") in sim.file_reads
        assert (TOKEN, "sim-owner", repo, "ci/pod.yaml", "main") in sim.file_reads
        jf = sim.origin_git("show", f"{branch}:environments/{env}/ci/{repo}.Jenkinsfile")
        assert jf.startswith(f"// Derived by ai-scanner-installer from {repo}/Jenkinsfile (main) for the '{env}'")
        assert f"yamlFile 'environments/{env}/ci/{repo}.pod.yaml'" in jf
        assert "set_env_image" in jf and "HEAD:${DEPLOY_BRANCH}" in jf and 'DEPLOY_REPO   = "${env.INSTALLER_REPO}"' in jf
        assert f"https://github.com/${{env.CODE_OWNER}}/{repo}.git" in jf
        assert "kustomization.yaml" not in jf.split("set_env_image()")[1].split("commit_and_push()")[0]
        pod = sim.origin_git("show", f"{branch}:environments/{env}/ci/{repo}.pod.yaml")
        assert pod == (REPOS_SRC / repo / "ci" / "pod.yaml").read_text(encoding="utf-8")
    # what Argo CD will see: a fresh checkout of the branch renders the same
    n = len(list(sim.tmp.glob("verify-*")))
    fresh = sim.tmp / f"verify-{n}"
    sim.git_out("clone", "--quiet", "--branch", branch, str(sim.origin), str(fresh))
    for app in APPS:
        p = REAL_POPEN([KUBECTL, "kustomize", str(fresh / "environments" / env / app)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0, out
        assert docs(out) == docs(sim.rendered[app]), f"{app}: the pushed branch renders differently"


def check_argocd(sim: Sim, env: str, inst: str, login: str) -> None:
    repo_url = f"https://github.com/{inst}.git"
    c = sim.calls[sim.index(secret("repo-ai-scanner-installer"))]
    assert json.loads(c.input)["metadata"]["labels"] == {"argocd.argoproj.io/secret-type": "repository"}
    assert secret_data(sim, "repo-ai-scanner-installer") == {"type": "git", "url": repo_url,
                                                            "username": login, "password": TOKEN}
    repo_secrets = [json.loads(c.input)["metadata"]["name"] for c in sim.calls
                    if "apply" in c.args and c.input and '"argocd.argoproj.io/secret-type": "repository"' in c.input]
    assert repo_secrets == ["repo-ai-scanner-installer"]
    apps = [yaml.safe_load(c.input) for c in sim.calls if applies("kind: Application")(c)]
    assert [x["metadata"]["name"] for x in apps] == [f"ai-scan-{a}" for a in APPS]
    for x, app in zip(apps, APPS):
        assert x["spec"]["source"] == {"repoURL": repo_url, "targetRevision": f"env/{env}",
                                       "path": f"environments/{env}/{app}"}
        assert x["spec"]["destination"]["namespace"] == "ai-scan-repos"


def check_default_ai(sim: Sim, run, want: dict[str, Any], start: int = 0) -> Call:
    """Saved once Argo CD reports healthy, through the gateway's settings code;
    the script and any key go in on stdin."""
    i = sim.index(is_ai_exec, start)
    c = sim.calls[i]
    assert i > sim.index(has("get", "applications"), start) and i > sim.index(has("exec", "/health"), start)
    assert c.step == "ai" and c.quiet
    assert c.args[-6:] == ["exec", "-i", "deploy/gateway", "--", "python", "-"] and "ai-scan-repos" in c.args
    assert json.loads(sim.ai_db.read_text()) == want
    return c


def check_no_default_ai_env(sim: Sim, r: dict[str, list[dict[str, Any]]]) -> None:
    genv = env_of(deployment_container(r["gateway"], "gateway"))
    assert not any(k.startswith("GATEWAY_DEFAULT_AI") for k in genv), genv
    assert "default-ai-key" not in secret_data(sim, "gateway-secrets")


def deployment_container(objs, name: str) -> dict[str, Any]:
    d = find(objs, "Deployment", name)
    assert d, f"no Deployment {name}"
    return next(c for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == name)


def force_rmtree(p: Path) -> None:
    """A git clone on Windows has read-only object files."""
    for f in p.rglob("*"):
        if f.is_file():
            f.chmod(stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(p)


# --- a) local minikube, model on the same machine ------------------------------------

def _minikube_kubeconfig(sim: Sim, monkeypatch) -> None:
    kube = sim.kube                             # type: ignore[attr-defined]
    mk = sim.tmp / "minikube"
    (mk / "profiles" / "minikube").mkdir(parents=True)
    (mk / "ca.crt").write_bytes(b"CA")
    (mk / "profiles" / "minikube" / "client.crt").write_bytes(b"CRT")
    (mk / "profiles" / "minikube" / "client.key").write_bytes(b"KEY")
    cfg = {"apiVersion": "v1", "kind": "Config", "current-context": "minikube",
           "clusters": [{"name": "minikube", "cluster": {
               "certificate-authority": r"C:\Users\sim\.minikube\ca.crt",
               "server": "https://127.0.0.1:50123"}}],
           "contexts": [{"name": "minikube", "context": {"cluster": "minikube", "user": "minikube"}}],
           "users": [{"name": "minikube", "user": {
               "client-certificate": r"C:\Users\sim\.minikube\profiles\minikube\client.crt",
               "client-key": r"C:\Users\sim\.minikube\profiles\minikube\client.key"}}]}
    hk = sim.tmp / "hostkube" / "config"
    hk.parent.mkdir()
    hk.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setattr(kube, "HOST_KUBE", hk)
    monkeypatch.setattr(kube, "HOST_MINIKUBE", mk)
    monkeypatch.setattr(kube, "_resolves", lambda name: False)


def local_answers() -> dict[str, Any]:
    return {"target": {"kind": "local", "context": "minikube"},
            "repos": {"owner": "sim-owner", "token": TOKEN, "login": "sim-user"},
            "ai": {"mode": "same_machine", "endpoint": "http://host.minikube.internal:1234/v1",
                   "model": "qwen2.5-coder-7b"},
            "admins": ["sim-user", "other-admin"]}


MINIKUBE = {"kind": "minikube", "node_ip": "192.168.49.2", "nodes": 1, "version": "v1.33.1",
            "runtime": "docker://26.1.1", "arch": "amd64"}
LOCAL_AI = {"preset": "local", "endpoint": "http://host.minikube.internal:1234/v1", "model": "qwen2.5-coder-7b",
            "api_key": None, "clear_key": False, "who": "installer"}


def test_a_local_minikube_same_machine(sim, monkeypatch):
    _minikube_kubeconfig(sim, monkeypatch)
    sim.flavour = MINIKUBE
    a = local_answers()
    run = run_plan(sim, a)
    assert_done(run)

    # the self-contained kubeconfig points at the host
    kc = yaml.safe_load((sim.tmp / "work" / "kubeconfig").read_text(encoding="utf-8"))
    cl = kc["clusters"][0]["cluster"]
    assert cl["server"] == "https://host.docker.internal:50123" and cl["tls-server-name"] == "127.0.0.1"

    reg = "10.96.100.100:5000"
    sim.assert_order(
        has("git", "clone", "ai-scanner-backend-deployment"),
        has("git", "clone", "ai-scanner-installer"),
        has("git", "ls-remote", "env/local"), has("git", "checkout", "-b", "env/local"),
        applies("clusterIP: 10.96.100.100"),
        has("rollout", "status", "deploy/registry"),
        has("helm", "ingress-nginx/ingress-nginx"),
        has("kubectl", "kustomize", "environments", "backend"),
        has("git", "add", "environments/local"), has("git", "commit"),
        has("git", "push", "origin", "HEAD:env/local"),
        has("get", "secret", "postgres-credentials"),
        secret("postgres-credentials"), secret("gateway-secrets"),
        secret("jenkins-admin"), secret("jenkins-github"), has("helm", "jenkins/jenkins"),
        secret("smee-channel"), applies("smee-client"),
        has("helm", "argo/argo-cd"), has("argocd-initial-admin-secret"), secret("repo-ai-scanner-installer"),
        applies("kind: Application"), secret("grafana-admin"), has("helm", "kube-prometheus-stack"),
        has("get", "applications"), has("exec", "deploy/gateway"), is_ai_exec)
    assert not any("terraform" in c.args[0] for c in sim.calls)
    # registries.yaml / ssh is a K3s-only thing
    assert not any(c.args[0] == "ssh" for c in sim.calls)
    assert "New branch env/local in DevOpsCourse22/ai-scanner-installer for this install." in log_text(run)

    # pauses: minikube tunnel, then the OAuth App
    assert [p["step"] for p in sim.prompts] == ["address", "oauth"]
    assert sim.prompts[0]["command"] == "minikube tunnel"
    assert sim.prompts[1]["info"]["Authorization callback URL"] == "http://localhost/auth/github/callback"
    assert a["oauth"] == {"client_id": OAUTH_ID, "client_secret": OAUTH_SECRET, "for_url": "http://localhost"}

    jv = check_jenkins(sim, "local", DEFAULT_INSTALLER, "local", reg)
    assert len(jv) == 1
    assert jv[0]["values"]["controller"]["serviceType"] == "ClusterIP"

    r = check_rendered(sim)
    be = r["backend"]
    api = deployment_container(be, "review-api")
    assert api["image"] == f"{reg}/review-api:bootstrap"
    assert env_of(api)["LMSTUDIO_BASE_URL"] == "http://host.minikube.internal:1234/v1"
    assert env_of(api)["SCANNER_WORKER_IMAGE"] == f"{reg}/scanner-worker:bootstrap"
    assert find(be, "Service", "lmstudio") is None, "no tailnet: the egress Service is dropped"
    assert find(be, "Ingress", "ai-scan-repos")["spec"]["ingressClassName"] == "nginx"
    sc = find(be, "StorageClass", "reports-noexec")
    assert sc["provisioner"] == "k8s.io/minikube-hostpath" and "mountOptions" not in sc
    assert find(be, "Namespace", "ai-scan-repos")

    gw = r["gateway"]
    genv = env_of(deployment_container(gw, "gateway"))
    assert genv["GATEWAY_FRONTEND_URL"] == "http://localhost"
    assert genv["GATEWAY_COOKIE_SECURE"] == "false"
    check_no_default_ai_env(sim, r)
    assert find(gw, "ConfigMap", "gateway-admins")["data"]["admins"].split() == ["sim-user", "other-admin"]
    fe = r["frontend"]
    assert deployment_container(fe, "frontend")["image"] == f"{reg}/review-frontend:bootstrap"

    check_installer_branch(sim, "local", DEFAULT_INSTALLER, "http://localhost")
    check_argocd(sim, "local", DEFAULT_INSTALLER, "sim-user")
    check_default_ai(sim, run, LOCAL_AI)
    assert "Default AI model saved: local qwen2.5-coder-7b" in log_text(run)

    gs = secret_data(sim, "gateway-secrets")
    assert gs["github-oauth-client-secret"] == OAUTH_SECRET
    assert secret_data(sim, "smee-channel") == {"url": SMEE}
    assert sim.webhooks == [(TOKEN, "sim-owner", r, SMEE) for r in CODE_REPOS]
    assert a["generated"]["smee_url"] == SMEE

    # Jenkins: port-forward, crumb, three builds polled until SUCCESS
    assert sim.portforwards and "svc/jenkins" in sim.portforwards[0]

    assert run.result["app_url"] == "http://localhost" and run.result["registry"] == reg
    # one admin password for Jenkins, Argo CD (bcrypt hash in its values) and Grafana
    pw = a["generated"]["jenkins_password"]
    assert run.result["passwords"] == {"Jenkins, Argo CD and Grafana": pw}
    argo = next(c for c in sim.calls if "argo/argo-cd" in c.args)
    assert pw not in (argo.input or "") and "argocdServerAdminPassword: $2" in (argo.input or "")
    assert_no_secrets(sim, run, a, (OAUTH_SECRET,))


def test_a2_local_kubeconfig_embeds_minikube_certs(sim, monkeypatch):
    _minikube_kubeconfig(sim, monkeypatch)
    out = sim.tmp / "kc"
    sim.kube.local_kubeconfig("minikube", out)              # type: ignore[attr-defined]
    kc = yaml.safe_load(out.read_text(encoding="utf-8"))
    cl, us = kc["clusters"][0]["cluster"], kc["users"][0]["user"]
    assert "certificate-authority" not in cl and "client-key" not in us
    assert base64.b64decode(cl["certificate-authority-data"]) == b"CA"
    assert base64.b64decode(us["client-certificate-data"]) == b"CRT"
    assert base64.b64decode(us["client-key-data"]) == b"KEY"


def test_a3_upgrade_keeps_jenkins_tags_and_admin_ai_choice(sim, monkeypatch):
    """A second install on the same target: the branch exists, Jenkins has pushed a
    new image tag to it meanwhile, and an admin's AI choice is already saved."""
    _minikube_kubeconfig(sim, monkeypatch)
    sim.flavour = MINIKUBE
    a = local_answers()
    assert_done(run_plan(sim, a))
    head1 = sim.origin_git("rev-parse", "env/local").strip()

    # Jenkins (backend pipeline) commits a new review-api tag to env/local
    jk = sim.tmp / "jenkins-clone"
    sim.git_out("clone", "--quiet", "--branch", "env/local", str(sim.origin), str(jk))
    patch = jk / "environments" / "local" / "backend" / "app-patch.yaml"
    text = patch.read_text(encoding="utf-8")
    assert "/review-api:bootstrap" in text
    patch.write_text(text.replace("/review-api:bootstrap", "/review-api:abc1234"), encoding="utf-8")
    sim.git_out("-C", str(jk), "commit", "--quiet", "-am", "ci(deploy): review-api -> abc1234")
    sim.git_out("-C", str(jk), "push", "--quiet", "origin", "HEAD:env/local")
    head2 = sim.origin_git("rev-parse", "env/local").strip()

    force_rmtree(sim.tmp / "work" / "repos")
    start, prompts = len(sim.calls), len(sim.prompts)
    sim.rendered.clear()
    sim.clones.clear()
    run = run_plan(sim, a)
    assert_done(run)
    later = sim.calls[start:]
    assert any(c.args[-3:] == ["checkout", "--quiet", "env/local"] for c in later)
    assert not any("-b" in c.args and "checkout" in c.args for c in later)
    assert any(c.args[-5:] == ["pull", "--quiet", "--rebase", "origin", "env/local"] for c in later)
    # the tag Jenkins wrote is kept, nothing else changed: no new commit
    be = docs(sim.rendered["backend"])
    assert deployment_container(be, "review-api")["image"] == "10.96.100.100:5000/review-api:abc1234"
    assert env_of(deployment_container(be, "review-api"))["SCANNER_WORKER_IMAGE"].endswith("/scanner-worker:bootstrap")
    assert "environments/local unchanged." in log_text(run)
    assert sim.origin_git("rev-parse", "env/local").strip() == head2 != head1
    check_installer_branch(sim, "local", DEFAULT_INSTALLER, "http://localhost")
    # OAuth App saved for this address: only the tunnel pause
    assert [p["step"] for p in sim.prompts[prompts:]] == ["address"]
    # the AI choice saved by the first run stays
    check_default_ai(sim, run, LOCAL_AI, start)
    assert "Default AI model kept: an admin already chose local" in log_text(run)
    assert_no_secrets(sim, run, a, (ARGO_PW, OAUTH_SECRET))


def test_a4_existing_cluster_secrets_are_kept(sim, monkeypatch):
    """The answers were lost but the database was not: the cluster's Postgres
    password and settings key are read back and reused, never logged."""
    _minikube_kubeconfig(sim, monkeypatch)
    sim.flavour = MINIKUBE
    pg, sk = "pg-live-SIM-pass/+=", "settings-live-SIM-key-0123456789"
    sim.live_secrets = {("postgres-credentials", "POSTGRES_PASSWORD"): pg, ("gateway-secrets", "settings-key"): sk}
    a = local_answers()
    a["oauth"] = {"client_id": OAUTH_ID, "client_secret": OAUTH_SECRET, "for_url": "http://localhost"}
    run = run_plan(sim, a)
    assert_done(run)
    sim.assert_order(has("get", "secret", "postgres-credentials", "jsonpath={.data.POSTGRES_PASSWORD}"),
                     has("get", "secret", "gateway-secrets", "jsonpath={.data.settings-key}"),
                     secret("postgres-credentials"), secret("gateway-secrets"))
    assert a["generated"]["postgres_password"] == pg and a["generated"]["settings_key"] == sk
    assert secret_data(sim, "postgres-credentials")["POSTGRES_PASSWORD"] == pg
    gs = secret_data(sim, "gateway-secrets")
    assert gs["settings-key"] == sk
    assert gs["database-url"] == f"postgresql+psycopg://ai_scan_repos:{urllib.parse.quote(pg, safe='')}" \
                                 "@postgres:5432/ai_scan_repos"
    text = log_text(run)
    assert "Keeping the cluster's existing postgres-credentials value (POSTGRES_PASSWORD)." in text
    assert "Keeping the cluster's existing gateway-secrets value (settings-key)." in text
    assert_no_secrets(sim, run, a, (pg, sk, ARGO_PW, OAUTH_SECRET))


def test_a5_default_ai_script_matches_gateway_settings_api():
    """The script s_default_ai pipes into the gateway calls the gateway's real API."""
    src = REPOS_SRC / "ai-scanner-gateway" / "src" / "review_gateway"
    tree = ast.parse((src / "site_settings.py").read_text(encoding="utf-8"))
    fns = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    assert "ai_public" in fns
    assert [x.arg for x in fns["save_ai"].args.kwonlyargs] == \
        ["preset", "endpoint", "model", "api_key", "clear_key", "who"]
    db = ast.parse((src / "db.py").read_text(encoding="utf-8"))
    assert any(isinstance(f, ast.FunctionDef) and f.name == "get_session" for f in db.body)


# --- b) SSH to a K3s server, sudo password, model over the tailnet ------------------

K3S = {"kind": "k3s", "node_ip": "10.0.0.5", "nodes": 1, "version": "v1.31.4+k3s1",
       "runtime": "containerd://1.7.23-k3s2", "arch": "amd64"}
CUSTOM_INSTALLER = "sim-owner/ai-scanner-installer"


def ssh_answers(port: int = 1234) -> dict[str, Any]:
    return {"target": {"kind": "ssh", "ssh": {"host": SSH_HOST, "user": "ubuntu", "port": 2222,
                                               "private_key": PRIVATE_KEY, "sudo_password": SUDO}},
            "repos": {"owner": "sim-owner", "token": TOKEN, "installer_repo": CUSTOM_INSTALLER},
            "ai": {"mode": "tailnet", "tailnet_ip": "100.93.244.39", "tailnet_port": port,
                   "model": "qwen", "ts_client_id": TS_ID, "ts_client_secret": TS_SECRET},
            "admins": ["sim-user"],
            "oauth": {"client_id": OAUTH_ID, "client_secret": OAUTH_SECRET, "for_url": f"http://{SSH_HOST}"}}


def _ssh_setup(sim: Sim, monkeypatch) -> None:
    kube = sim.kube                             # type: ignore[attr-defined]
    k3s_yaml = yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "current-context": "default",
                               "clusters": [{"name": "default", "cluster": {
                                   "certificate-authority-data": "Q0E=", "server": "https://127.0.0.1:6443"}}],
                               "contexts": [{"name": "default", "context": {"cluster": "default", "user": "default"}}],
                               "users": [{"name": "default", "user": {"client-key-data": "S0VZ"}}]})

    def fake_run(args, input=None, capture_output=False, text=False, timeout=None):
        sim.ssh_runs.append({"args": list(args), "input": input})
        return subprocess.CompletedProcess(args, 0, k3s_yaml, "")
    monkeypatch.setattr(kube, "subprocess", types.SimpleNamespace(run=fake_run))
    sim.flavour = K3S


def _run_remote_script(remote: str, root: Path) -> None:
    """Run the `sh -c` script inside `sudo ... sh -c '<script>'` locally, with
    /etc/rancher/k3s moved under `root` and systemctl a no-op."""
    parts = shlex.split(remote)
    assert parts[:2] == ["sudo", "-S"] and parts[-3] == "sh" and parts[-2] == "-c", parts
    script = parts[-1].replace("/etc/rancher/k3s", "./etc/rancher/k3s") \
                      .replace("systemctl restart k3s", "true")
    root.mkdir(parents=True, exist_ok=True)
    sh = shutil.which("sh") or shutil.which("bash")
    if sh is None:
        pytest.skip("no sh to run the remote script with")
    p = REAL_POPEN([sh, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(root))
    out, _ = p.communicate(timeout=30)
    assert p.returncode == 0, out


def test_b_ssh_k3s_sudo_tailnet(sim, monkeypatch):
    _ssh_setup(sim, monkeypatch)
    a = ssh_answers()
    run = run_plan(sim, a)
    assert_done(run)

    # the kubeconfig was read with sudo, the password on stdin only
    assert len(sim.ssh_runs) == 1
    r0 = sim.ssh_runs[0]
    assert r0["input"] == SUDO + "\n" and SUDO not in " ".join(r0["args"])
    assert r0["args"][-2] == f"ubuntu@{SSH_HOST}" and "2222" in r0["args"]
    kc = yaml.safe_load((sim.tmp / "work" / "kubeconfig").read_text(encoding="utf-8"))
    assert kc["clusters"][0]["cluster"]["server"] == f"https://{SSH_HOST}:6443"
    key = (sim.tmp / "work" / "ssh" / "id").read_text(encoding="utf-8")
    assert "\r" not in key and key.endswith("-----\n")

    reg = "10.0.0.5:30500"
    # registries.yaml written over ssh with sudo, then K3s comes back
    i_write = sim.index(lambda c: c.args[0] == "ssh" and "registries.yaml" in c.args[-1]
                        and "systemctl restart k3s" in c.args[-1])
    write = sim.calls[i_write]
    assert write.input == SUDO + "\n" and SUDO not in " ".join(write.args)
    assert write.args[-1].startswith("sudo -S -p '' sh -c ")
    _run_remote_script(write.args[-1], sim.tmp / "remote")
    reg_file = sim.tmp / "remote/etc/rancher/k3s/registries.yaml"
    assert yaml.safe_load(reg_file.read_text()) == {"mirrors": {reg: {"endpoint": [f"http://{reg}"]}}}
    sim.assert_order(
        has("git", "clone", "sim-owner/ai-scanner-installer"),
        applies("nodePort: 30500"), has("rollout", "status", "deploy/registry"),
        lambda c: c.args[0] == "ssh" and "cat /etc/rancher/k3s/registries.yaml" in c.args[-1],
        lambda c: c is write, has("get", "nodes"),
        secret("operator-oauth"), has("helm", "tailscale/tailscale-operator"),
        has("kubectl", "kustomize", "environments", "backend"), has("git", "push", "HEAD:env/local"),
        secret("gateway-secrets"), has("helm", "jenkins/jenkins"), secret("smee-channel"),
        has("helm", "argo/argo-cd"), has("get", "applications"), is_ai_exec)
    assert not any("ingress-nginx/ingress-nginx" in c.args for c in sim.calls), "K3s keeps Traefik"
    helm_ts = sim.calls[sim.index(has("helm", "tailscale/tailscale-operator"))]
    values = next(x for x in helm_ts.args if x.endswith("values.yaml"))
    assert Path(values).parts[-3:] == ("ai-scanner-backend-deployment", "tailscale-operator", "values.yaml")
    assert secret_data(sim, "operator-oauth") == {"client_id": TS_ID, "client_secret": TS_SECRET}

    # OAuth App saved for this address: no pause at all
    assert sim.prompts == []

    r = check_rendered(sim)
    be = r["backend"]
    api = deployment_container(be, "review-api")
    assert env_of(api)["LMSTUDIO_BASE_URL"] == "http://lmstudio:1234/v1"
    svc = find(be, "Service", "lmstudio")
    assert svc["metadata"]["annotations"]["tailscale.com/tailnet-ip"] == "100.93.244.39"
    assert find(be, "Ingress", "ai-scan-repos")["spec"]["ingressClassName"] == "traefik"
    sc = find(be, "StorageClass", "reports-noexec")
    assert sc["provisioner"] == "rancher.io/local-path" and "noexec" in sc["mountOptions"]
    genv = env_of(deployment_container(r["gateway"], "gateway"))
    assert genv["GATEWAY_FRONTEND_URL"] == f"http://{SSH_HOST}"
    assert genv["GATEWAY_COOKIE_SECURE"] == "false"
    check_no_default_ai_env(sim, r)

    check_jenkins(sim, "local", CUSTOM_INSTALLER, "local", reg)
    check_installer_branch(sim, "local", CUSTOM_INSTALLER, f"http://{SSH_HOST}")
    check_argocd(sim, "local", CUSTOM_INSTALLER, "x-access-token")
    check_default_ai(sim, run, {"preset": "local", "endpoint": "http://lmstudio:1234/v1", "model": "qwen",
                                "api_key": None, "clear_key": False, "who": "installer"})
    assert run.result["app_url"] == f"http://{SSH_HOST}"
    assert_no_secrets(sim, run, a, (ARGO_PW,))
    text = log_text(run)
    for line in PRIVATE_KEY.splitlines():
        if len(line) > 20 and "-----" not in line:
            assert line not in text


def test_b2_tailnet_custom_port_replaces_the_port(sim, monkeypatch):
    _ssh_setup(sim, monkeypatch)
    a = ssh_answers(port=11434)
    run = run_plan(sim, a)
    assert_done(run)
    svc = find(docs(sim.rendered["backend"]), "Service", "lmstudio")
    assert [p["port"] for p in svc["spec"]["ports"]] == [11434]
    assert json.loads(sim.ai_db.read_text())["endpoint"] == "http://lmstudio:11434/v1"


# --- c) EKS with a domain, cloud AI provider ---------------------------------------

def eks_answers() -> dict[str, Any]:
    return {"target": {"kind": "eks", "eks": {"region": "us-east-1", "name": "ai-scan-repos",
                                               "aws_access_key_id": AWS_KEY_ID,
                                               "aws_secret_access_key": AWS_SECRET,
                                               "domain": DOMAIN, "node_type": "t3.large", "node_count": 2}},
            "repos": {"owner": "sim-owner", "token": TOKEN, "login": "sim-user"},
            "ai": {"mode": "cloud", "preset": "openai", "model": "gpt-4.1-mini", "api_key": API_KEY},
            "admins": ["sim-user"]}


EKS = {"kind": "eks", "node_ip": "10.1.2.3", "nodes": 2, "version": "v1.33.5-eks-113cf36",
       "runtime": "containerd://1.7.27", "arch": "amd64"}


def test_c_eks_domain_cloud(sim):
    plans = sim.plans                           # type: ignore[attr-defined]
    sim.flavour = EKS
    a = eks_answers()
    run = run_plan(sim, a)
    assert_done(run)

    tfvars = json.loads((plans.DATA / "terraform" / "installer.auto.tfvars.json").read_text())
    assert tfvars == {"name": "ai-scan-repos", "region": "us-east-1", "node_type": "t3.large",
                      "node_count": 2, "domain": DOMAIN, "admin_arns": []}
    assert (plans.DATA / "terraform" / "main.tf").exists()
    sim.assert_order(
        has("aws", "sts", "get-caller-identity"),
        has("terraform", "init"), has("terraform", "apply"), has("terraform", "output"),
        has("aws", "eks", "update-kubeconfig", "ai-scan-repos"),
        has("aws", "acm", "wait", "certificate-validated", CERT),
        applies("ebs.csi.aws.com"), has("annotate", "storageclass", "gp2"),
        has("git", "clone"), has("git", "checkout", "-b", "env/eks"),
        has("helm", "ingress-nginx/ingress-nginx"),
        has("get", "svc", "ingress-nginx-controller"),
        has("kubectl", "kustomize", "environments", "backend"), has("git", "push", "HEAD:env/eks"),
        secret("gateway-secrets"),
        has("annotate", "serviceaccount", "default"),
        has("helm", "jenkins/jenkins"), has("get", "svc", "jenkins"), has("helm", "jenkins/jenkins"),
        has("helm", "argo/argo-cd"), has("argocd-initial-admin-secret"), has("helm", "kube-prometheus-stack"),
        has("get", "applications"), is_ai_exec)
    # AWS credentials go in the environment, never on a command line
    for c in sim.calls:
        if c.args[0] in ("aws", "terraform"):
            assert c.env.get("AWS_SECRET_ACCESS_KEY") == AWS_SECRET and c.env["AWS_REGION"] == "us-east-1"
    assert sim.calls[sim.index(has("terraform", "apply"))].cwd == str(plans.DATA / "terraform")
    assert not any(c.args[0] == "ssh" for c in sim.calls)
    assert not any(applies("kind: Namespace\nmetadata:\n  name: registry")(c) for c in sim.calls), "no in-cluster registry on EKS"
    assert not any(secret("smee-channel")(c) for c in sim.calls) and sim.smee_calls == 0

    # pauses: ACM validation record, CNAME to the load balancer, OAuth App
    assert [p["step"] for p in sim.prompts] == ["terraform", "address", "oauth"]
    assert sim.prompts[0]["info"] == {"Type": "CNAME", "Name": f"_abc.{DOMAIN}.", "Value": "_xyz.acm-validations.aws."}
    assert sim.prompts[1]["info"] == {"Type": "CNAME", "Name": DOMAIN, "Value": LB}
    assert sim.prompts[2]["info"]["Homepage URL"] == f"https://{DOMAIN}"
    assert sim.prompts[2]["info"]["Authorization callback URL"] == f"https://{DOMAIN}/auth/github/callback"

    ing = yaml.safe_load(sim.calls[sim.index(has("helm", "ingress-nginx/ingress-nginx"))].input)
    ann = ing["controller"]["service"]["annotations"]
    assert ann["service.beta.kubernetes.io/aws-load-balancer-ssl-cert"] == CERT
    assert ann["service.beta.kubernetes.io/aws-load-balancer-type"] == "nlb"

    sa = sim.calls[sim.index(has("annotate", "serviceaccount", "default"))]
    assert f"eks.amazonaws.com/role-arn={ROLE}" in sa.args and "jenkins" in sa.args

    jv = check_jenkins(sim, "eks", DEFAULT_INSTALLER, "ecr", ECR, aws_region="us-east-1")
    assert len(jv) == 2
    for v in jv:
        assert v["values"]["controller"]["serviceType"] == "LoadBalancer"
    assert jv[1]["values"]["controller"]["jenkinsUrl"] == f"http://{JENKINS_LB}:8080/"

    r = check_rendered(sim)
    be = r["backend"]
    assert deployment_container(be, "review-api")["image"] == f"{ECR}/review-api:bootstrap"
    sc = find(be, "StorageClass", "reports-noexec")
    assert sc["provisioner"] == "efs.csi.aws.com" and sc["parameters"]["fileSystemId"] == EFS
    assert "mountOptions" not in sc
    pvc = find(be, "PersistentVolumeClaim", "ai-scan-repos-reports")
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    ingress = find(be, "Ingress", "ai-scan-repos")
    assert ingress["spec"]["ingressClassName"] == "nginx" and ingress["spec"]["rules"][0]["host"] == DOMAIN
    assert find(be, "Service", "lmstudio") is None

    gw = r["gateway"]
    genv = env_of(deployment_container(gw, "gateway"))
    assert deployment_container(gw, "gateway")["image"] == f"{ECR}/review-gateway:bootstrap"
    assert genv["GATEWAY_FRONTEND_URL"] == f"https://{DOMAIN}"
    assert genv["GITHUB_OAUTH_REDIRECT_URI"] == f"https://{DOMAIN}/auth/github/callback"
    assert genv["GATEWAY_COOKIE_SECURE"] == "true"
    check_no_default_ai_env(sim, r)
    assert deployment_container(r["frontend"], "frontend")["image"] == f"{ECR}/review-frontend:bootstrap"

    check_installer_branch(sim, "eks", DEFAULT_INSTALLER, f"https://{DOMAIN}")
    check_argocd(sim, "eks", DEFAULT_INSTALLER, "sim-user")

    # the default AI key: only ever on the exec's stdin
    ai = check_default_ai(sim, run, {"preset": "openai", "endpoint": "", "model": "gpt-4.1-mini",
                                     "api_key": API_KEY, "clear_key": False, "who": "installer"})
    assert API_KEY in ai.input
    assert not any(API_KEY in (c.input or "") for c in sim.calls if c is not ai)
    assert not any(API_KEY in t for t in sim.rendered.values())
    code, _ = sim.real_git(["--git-dir", str(sim.origin), "grep", "-q", "-F", API_KEY, "env/eks"])
    assert code == 1, "the AI key was committed to the installer repository"
    assert "Default AI model saved: openai gpt-4.1-mini" in log_text(run)

    gs = secret_data(sim, "gateway-secrets")
    assert gs["github-oauth-client-id"] == OAUTH_ID
    hook = f"http://{JENKINS_LB}:8080/github-webhook/"
    assert [w[3] for w in sim.webhooks] == [hook] * 3
    assert sim.portforwards and sim.portforwards[0][:2] == ["kubectl", "--kubeconfig"]

    assert run.result["app_url"] == f"https://{DOMAIN}"
    assert run.result["jenkins_url"] == f"http://{JENKINS_LB}:8080/"
    assert run.result["registry"] == ECR
    assert_no_secrets(sim, run, a, (ARGO_PW, OAUTH_SECRET, AWS_SECRET, API_KEY))


# --- d) destroy -----------------------------------------------------------------------

def test_d_destroy_eks(sim):
    plans = sim.plans                           # type: ignore[attr-defined]
    sim.flavour = EKS
    a = eks_answers()
    a["oauth"] = {"client_id": OAUTH_ID, "client_secret": OAUTH_SECRET}
    work = plans.DATA / "terraform"
    work.mkdir(parents=True)
    (work / "terraform.tfstate").write_text('{"resources": [{"type": "aws_vpc"}]}', encoding="utf-8")
    run = run_plan(sim, a, "destroy")
    assert_done(run)
    assert [s.id for s in run.steps] == ["connect", "cluster", "terraform"]
    sim.assert_order(
        has("aws", "eks", "update-kubeconfig", "ai-scan-repos"),
        has("delete", "application", "ai-scan-backend"), has("delete", "namespace", "ai-scan-repos"),
        has("helm", "uninstall", "argocd"), has("helm", "uninstall", "jenkins"),
        has("helm", "uninstall", "ingress-nginx"), has("delete", "namespace", "ingress-nginx"),
        has("delete", "storageclass", "reports-noexec"),
        has("terraform", "init"), has("terraform", "destroy"))
    assert sim.calls[sim.index(has("terraform", "destroy"))].env["AWS_SECRET_ACCESS_KEY"] == AWS_SECRET
    assert not any(c.args[0] == "git" for c in sim.calls), "destroy leaves the repositories alone"
    assert sim.prompts == []
    assert_no_secrets(sim, run, a)


def test_d_destroy_eks_removes_webhooks(sim):
    """Install on EKS saves the hook ids and URL (Jenkins's load balancer); destroy removes them."""
    sim.flavour = EKS
    a = eks_answers()
    install = run_plan(sim, a)
    assert_done(install)
    hook = f"http://{JENKINS_LB}:8080/github-webhook/"
    assert a["generated"]["webhook_url"] == hook and len(a["generated"]["webhook_ids"]) == 3
    head = sim.origin_git("rev-parse", "env/eks")
    n = len(sim.calls)
    run = run_plan(sim, a, "destroy")
    assert_done(run)
    assert [h[:3] for h in sim.deleted_hooks] == [(TOKEN, "sim-owner", r) for r in CODE_REPOS]
    assert all(h[3] == a["generated"]["webhook_ids"][h[2]] and hook in h[3:] for h in sim.deleted_hooks)
    assert not any(c.args[0] == "git" for c in sim.calls[n:])
    assert sim.origin_git("rev-parse", "env/eks") == head, "destroy keeps the env branch"
    assert_no_secrets(sim, run, a, (API_KEY, OAUTH_SECRET))


def test_d_destroy_local(sim, monkeypatch):
    _minikube_kubeconfig(sim, monkeypatch)
    sim.flavour = MINIKUBE
    a = local_answers()
    a["generated"] = {"smee_url": SMEE, "webhook_url": SMEE,
                      "webhook_ids": {"ai-scanner-backend": 11, "ai-scanner-gateway": 12, "ai-scanner-frontend": 13}}
    run = run_plan(sim, a, "destroy")
    assert_done(run)
    assert [s.id for s in run.steps] == ["connect", "cluster"]
    sim.assert_order(
        has("delete", "application", "ai-scan-backend"), has("delete", "application", "ai-scan-frontend"),
        has("delete", "namespace", "ai-scan-repos"),
        has("helm", "uninstall", "argocd"), has("helm", "uninstall", "jenkins"),
        has("helm", "uninstall", "tailscale-operator"), has("helm", "uninstall", "ingress-nginx"),
        has("delete", "namespace", "registry"),
        has("delete", "storageclass", "reports-noexec"))
    assert not any(c.args[0] in ("terraform", "aws", "git") for c in sim.calls)
    assert [h[:3] for h in sim.deleted_hooks] == [(TOKEN, "sim-owner", r) for r in CODE_REPOS]
    assert [h[3] for h in sim.deleted_hooks] == [11, 12, 13] and all(SMEE in h[3:] for h in sim.deleted_hooks)
    assert_no_secrets(sim, run, a)


# --- runner: the wizard's Continue -------------------------------------------------------

def test_runner_continue_right_after_pause_is_not_lost():
    from installer import runner

    class Eager(runner.Run):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            # a client answers the moment the run reports "waiting" (what /api/run/continue checks)
            if name == "status" and value == "waiting":
                super().__setattr__("reply", {"client_id": "x"})
                self.resume.set()

    run = Eager(kind="install", steps=[runner.Step("s", "S", lambda c: None)])
    ctx = runner.Ctx(run, {}, ".")
    ctx.step_id = "s"
    got: dict[str, str] = {}
    t = threading.Thread(target=lambda: got.update(ctx.wait_for_user("m", fields=[{"name": "client_id"}])),
                         daemon=True)
    t.start()
    t.join(2)
    alive = t.is_alive()
    run.resume.set()                 # release the thread either way
    assert not alive and got == {"client_id": "x"}

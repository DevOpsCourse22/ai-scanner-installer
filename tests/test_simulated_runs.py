"""Simulated end-to-end runs of the install and destroy plans.

Every plan goes through installer.runner (its thread, its log, its redactor,
its wait_for_user pauses) with the outside world faked:

  Ctx.sh            records every command; answers kubectl/helm/git/aws/
                    terraform/ssh with canned output. `kubectl kustomize` is
                    the one command that really runs, on overlays the plan
                    wrote into copies of the real deployment repositories.
  kube.flavour      returns the cluster kind for the scenario
  kube.subprocess   (ssh scenario) the `ssh ... cat k3s.yaml` call
  github.*          webhooks and the smee channel
  httpx             a fake Jenkins (crumb, jobs, builds) via MockTransport
  subprocess.Popen  the kubectl port-forward of PortForward
  time.sleep        a fake clock, so timeouts and polls cost nothing

Nothing here talks to AWS, GitHub or a cluster.
"""

from __future__ import annotations

import base64
import importlib
import json
import shlex
import shutil
import subprocess
import threading
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import yaml

REPOS_SRC = Path(r"C:\Projects\Final-Project-Repos")
REAL_POPEN = subprocess.Popen
REAL_CLIENT = httpx.Client

KUBECTL = shutil.which("kubectl")
pytestmark = pytest.mark.skipif(
    KUBECTL is None or not all((REPOS_SRC / f"ai-scanner-{r}-deployment").is_dir()
                               for r in ("backend", "gateway", "frontend")),
    reason="needs kubectl on PATH and the three deployment repositories next to this one")

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
    calls: list[Call] = field(default_factory=list)
    rendered: dict[str, str] = field(default_factory=dict)        # repo -> kustomize output
    overlay_files: dict[str, dict[str, str]] = field(default_factory=dict)
    webhooks: list[tuple] = field(default_factory=list)
    deleted_hooks: list[tuple] = field(default_factory=list)
    smee_calls: int = 0
    portforwards: list[list[str]] = field(default_factory=list)
    jenkins: list[tuple[str, str, str]] = field(default_factory=list)  # (method, path, auth header)
    ssh_runs: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    clock: float = 1_000_000.0
    extra: Callable[[list[str], "Call"], tuple[int, str] | None] | None = None

    # --- Ctx.sh ---
    def respond(self, args: list[str], call: Call) -> tuple[int, str]:
        if self.extra:
            got = self.extra(args, call)
            if got is not None:
                return got
        if args[:2] == ["kubectl", "kustomize"]:
            d = Path(args[2])
            p = REAL_POPEN([KUBECTL, "kustomize", str(d)], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
            out, _ = p.communicate(timeout=120)
            repo = d.parent.parent.name
            self.rendered[repo] = out
            self.overlay_files[repo] = {f.name: f.read_text(encoding="utf-8") for f in d.iterdir()}
            return p.returncode, out
        if args[0] == "git":
            if args[1] == "clone":
                url, dest = args[-2], Path(args[-1])
                assert TOKEN not in url
                name = url.rsplit("/", 1)[1].removesuffix(".git")
                shutil.copytree(REPOS_SRC / name, dest, ignore=shutil.ignore_patterns(".git"))
                return 0, ""
            if "status" in args:
                return 0, "?? overlays/\n"
            return 0, ""
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
                return 0, '{"status":"ok"}\n'
            if "argocd-initial-admin-secret" in rest:
                return 0, base64.b64encode(ARGO_PW.encode()).decode()
            if rest[2:4] == ["get", "serviceaccount"] or rest[:2] == ["get", "serviceaccount"]:
                return 0, "NAME      SECRETS   AGE\ndefault   0         1s\n"
            if rest[:2] == ["get", "nodes"]:
                return 0, "NAME    STATUS   ROLES    AGE   VERSION\nnode1   Ready    master   1d    v1\n"
            if "svc" in rest and "get" in rest:
                return 0, JENKINS_LB if "jenkins" in rest else LB
            return 0, ""
        return 0, ""

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

    # tolerant of both webhook APIs (ensure_webhook(..., secret) / (..., known_id) -> id;
    # delete_webhooks(token, owner, repo, url) / delete_webhook(token, owner, repo, hook_id, url))
    def ensure(token, owner, repo, url, *rest, **kw):
        s.webhooks.append((token, owner, repo, url))
        return 1000 + len(s.webhooks)

    def delete(token, owner, repo, *rest, **kw):
        s.deleted_hooks.append((token, owner, repo, *rest, *kw.values()))
        return "removed"
    monkeypatch.setattr(github, "ensure_webhook", ensure)
    monkeypatch.setattr(github, "delete_webhooks", delete, raising=False)
    monkeypatch.setattr(github, "delete_webhook", delete, raising=False)
    monkeypatch.setattr(github, "file_text", lambda *a, **kw:
                        'DEPLOY_REPO = "${env.DEPLOY_OWNER ?: \'DevOpsCourse22\'}/x"', raising=False)

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

    polls: dict[str, int] = {}
    triggered: set[str] = set()

    def jenkins(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        s.jenkins.append((req.method, path, req.headers.get("authorization", "")))
        if path == "/crumbIssuer/api/json":
            return httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "c1"})
        if path.endswith("/lastBuild/api/json"):
            job = path.split("/")[2]
            if job not in triggered:
                return httpx.Response(404)          # a fresh install: no build yet
            polls[job] = polls.get(job, 0) + 1
            if polls[job] == 1:
                return httpx.Response(200, json={"building": True, "result": None, "number": 1})
            return httpx.Response(200, json={"building": False, "result": "SUCCESS", "number": 1})
        if path.endswith("/build"):
            assert req.headers.get("Jenkins-Crumb") == "c1"
            triggered.add(path.split("/")[2])
            return httpx.Response(201)
        if path.startswith("/job/") and path.endswith("/api/json"):
            return httpx.Response(200, json={"name": path.split("/")[2]})
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
             answer: Callable[[dict[str, Any]], dict[str, str]] = default_answer, timeout: float = 120):
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
            env = v["controller"]["JCasC"]["configScripts"]["ci-registry"]
            env = yaml.safe_load(env)["jenkins"]["globalNodeProperties"][0]["envVars"]["env"]
            out.append({"values": v, "env": {e["key"]: e["value"] for e in env}})
    return out


def check_repos_rendered(sim: Sim) -> dict[str, list[dict[str, Any]]]:
    repos = ["ai-scanner-backend-deployment", "ai-scanner-gateway-deployment", "ai-scanner-frontend-deployment"]
    assert sorted(sim.rendered) == sorted(repos)
    out = {}
    for r in repos:
        out[r] = docs(sim.rendered[r])
        assert out[r], f"{r}: kustomize rendered nothing"
    return out


def deployment_container(objs, name: str) -> dict[str, Any]:
    d = find(objs, "Deployment", name)
    assert d, f"no Deployment {name}"
    return next(c for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == name)


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


def test_a_local_minikube_same_machine(sim, monkeypatch):
    _minikube_kubeconfig(sim, monkeypatch)
    sim.flavour = MINIKUBE
    a = local_answers()
    run = run_plan(sim, a)
    assert_done(run)

    # the self-contained kubeconfig points at the host (cert embedding: see the xfail test below)
    kc = yaml.safe_load((sim.tmp / "work" / "kubeconfig").read_text(encoding="utf-8"))
    cl = kc["clusters"][0]["cluster"]
    assert cl["server"] == "https://host.docker.internal:50123" and cl["tls-server-name"] == "127.0.0.1"

    reg = "10.96.100.100:5000"
    sim.assert_order(
        has("git", "clone", "ai-scanner-backend-deployment"),
        applies("clusterIP: 10.96.100.100"),
        has("rollout", "status", "deploy/registry"),
        has("helm", "ingress-nginx/ingress-nginx"),
        has("kubectl", "kustomize", "ai-scanner-backend-deployment"),
        has("git", "push", "origin", "HEAD:main"),
        secret("postgres-credentials"), secret("gateway-secrets"),
        secret("jenkins-admin"), secret("jenkins-github"), has("helm", "jenkins/jenkins"),
        secret("smee-channel"), applies("smee-client"),
        has("helm", "argo/argo-cd"), secret("repo-ai-scanner-backend-deployment"),
        applies("kind: Application"),
        has("get", "applications"), has("exec", "deploy/gateway"),
        has("argocd-initial-admin-secret"))
    assert not any("terraform" in c.args[0] for c in sim.calls)
    # registries.yaml / ssh is a K3s-only thing
    assert not any(c.args[0] == "ssh" for c in sim.calls)

    # pauses: minikube tunnel, then the OAuth App
    assert [p["step"] for p in sim.prompts] == ["address", "oauth"]
    assert sim.prompts[0]["command"] == "minikube tunnel"
    assert sim.prompts[1]["info"]["Authorization callback URL"] == "http://localhost/auth/github/callback"
    assert a["oauth"] == {"client_id": OAUTH_ID, "client_secret": OAUTH_SECRET, "for_url": "http://localhost"}

    jv = jenkins_values(sim)
    assert len(jv) == 1
    assert jv[0]["env"] == {"REGISTRY_KIND": "local", "REGISTRY": reg, "DEPLOY_OWNER": "sim-owner",
                            "DEPLOY_ENV": "local"}
    assert jv[0]["values"]["controller"]["serviceType"] == "ClusterIP"

    r = check_repos_rendered(sim)
    be = r["ai-scanner-backend-deployment"]
    api = deployment_container(be, "review-api")
    assert api["image"] == f"{reg}/review-api:bootstrap"
    assert env_of(api)["LMSTUDIO_BASE_URL"] == "http://host.minikube.internal:1234/v1"
    assert env_of(api)["SCANNER_WORKER_IMAGE"] == f"{reg}/scanner-worker:bootstrap"
    assert find(be, "Service", "lmstudio") is None, "no tailnet: the egress Service is dropped"
    assert find(be, "Ingress", "ai-scan-repos")["spec"]["ingressClassName"] == "nginx"
    sc = find(be, "StorageClass", "reports-noexec")
    assert sc["provisioner"] == "k8s.io/minikube-hostpath" and "mountOptions" not in sc
    assert find(be, "Namespace", "ai-scan-repos")

    gw = r["ai-scanner-gateway-deployment"]
    genv = env_of(deployment_container(gw, "gateway"))
    assert genv["GATEWAY_FRONTEND_URL"] == "http://localhost"
    assert genv["GATEWAY_COOKIE_SECURE"] == "false"
    assert genv["GATEWAY_DEFAULT_AI_PRESET"] == "local"
    assert genv["GATEWAY_DEFAULT_AI_ENDPOINT"] == "http://host.minikube.internal:1234/v1"
    assert "GATEWAY_DEFAULT_AI_KEY" not in genv
    assert find(gw, "ConfigMap", "gateway-admins")["data"]["admins"].split() == ["sim-user", "other-admin"]
    fe = r["ai-scanner-frontend-deployment"]
    assert deployment_container(fe, "frontend")["image"] == f"{reg}/review-frontend:bootstrap"

    gs = secret_data(sim, "gateway-secrets")
    assert gs["github-oauth-client-secret"] == OAUTH_SECRET and "default-ai-key" not in gs
    assert secret_data(sim, "smee-channel") == {"url": SMEE}
    assert sim.webhooks == [(TOKEN, "sim-owner", r, SMEE) for r in
                            ("ai-scanner-backend", "ai-scanner-gateway", "ai-scanner-frontend")]
    assert a["generated"]["smee_url"] == SMEE

    # Jenkins: port-forward, crumb, three scans, builds polled until SUCCESS
    assert sim.portforwards and "svc/jenkins" in sim.portforwards[0]
    posts = [p for m, p, _ in sim.jenkins if m == "POST"]
    assert posts == [f"/job/{j}/build" for j in ("ai-scanner-backend", "ai-scanner-gateway", "ai-scanner-frontend")]
    assert all(auth.startswith("Basic ") for _, _, auth in sim.jenkins)

    assert run.result["app_url"] == "http://localhost" and run.result["registry"] == reg
    assert a["generated"]["argocd_password"] == ARGO_PW
    assert_no_secrets(sim, run, a, (ARGO_PW, OAUTH_SECRET))


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


# --- b) SSH to a K3s server, sudo password, model over the tailnet ------------------

K3S = {"kind": "k3s", "node_ip": "10.0.0.5", "nodes": 1, "version": "v1.31.4+k3s1",
       "runtime": "containerd://1.7.23-k3s2", "arch": "amd64"}


def ssh_answers(port: int = 1234) -> dict[str, Any]:
    return {"target": {"kind": "ssh", "ssh": {"host": SSH_HOST, "user": "ubuntu", "port": 2222,
                                               "private_key": PRIVATE_KEY, "sudo_password": SUDO}},
            "repos": {"owner": "sim-owner", "token": TOKEN},
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
        applies("nodePort: 30500"), has("rollout", "status", "deploy/registry"),
        lambda c: c.args[0] == "ssh" and "cat /etc/rancher/k3s/registries.yaml" in c.args[-1],
        lambda c: c is write, has("get", "nodes"),
        secret("operator-oauth"), has("helm", "tailscale/tailscale-operator"),
        has("kubectl", "kustomize", "ai-scanner-backend-deployment"),
        secret("gateway-secrets"), has("helm", "jenkins/jenkins"), secret("smee-channel"),
        has("helm", "argo/argo-cd"), has("get", "applications"))
    assert not any("ingress-nginx/ingress-nginx" in c.args for c in sim.calls), "K3s keeps Traefik"
    helm_ts = sim.calls[sim.index(has("helm", "tailscale/tailscale-operator"))]
    assert any(x.endswith("values.yaml") for x in helm_ts.args), "the repo's operator values are used"
    assert secret_data(sim, "operator-oauth") == {"client_id": TS_ID, "client_secret": TS_SECRET}

    # OAuth App saved for this address: no pause at all
    assert sim.prompts == []

    r = check_repos_rendered(sim)
    be = r["ai-scanner-backend-deployment"]
    api = deployment_container(be, "review-api")
    assert env_of(api)["LMSTUDIO_BASE_URL"] == "http://lmstudio:1234/v1"
    svc = find(be, "Service", "lmstudio")
    assert svc["metadata"]["annotations"]["tailscale.com/tailnet-ip"] == "100.93.244.39"
    assert find(be, "Ingress", "ai-scan-repos")["spec"]["ingressClassName"] == "traefik"
    sc = find(be, "StorageClass", "reports-noexec")
    assert sc["provisioner"] == "rancher.io/local-path" and "noexec" in sc["mountOptions"]
    genv = env_of(deployment_container(r["ai-scanner-gateway-deployment"], "gateway"))
    assert genv["GATEWAY_FRONTEND_URL"] == f"http://{SSH_HOST}"
    assert genv["GATEWAY_COOKIE_SECURE"] == "false"
    assert genv["GATEWAY_DEFAULT_AI_ENDPOINT"] == "http://lmstudio:1234/v1"

    jv = jenkins_values(sim)
    assert jv[0]["env"]["REGISTRY_KIND"] == "local" and jv[0]["env"]["REGISTRY"] == reg
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
    svc = find(docs(sim.rendered["ai-scanner-backend-deployment"]), "Service", "lmstudio")
    assert [p["port"] for p in svc["spec"]["ports"]] == [11434]


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
                      "node_count": 2, "domain": DOMAIN}
    assert (plans.DATA / "terraform" / "main.tf").exists()
    sim.assert_order(
        has("aws", "sts", "get-caller-identity"),
        has("terraform", "init"), has("terraform", "apply"), has("terraform", "output"),
        has("aws", "eks", "update-kubeconfig", "ai-scan-repos"),
        has("aws", "acm", "wait", "certificate-validated", CERT),
        applies("ebs.csi.aws.com"), has("annotate", "storageclass", "gp2"),
        has("git", "clone"),
        has("helm", "ingress-nginx/ingress-nginx"),
        has("get", "svc", "ingress-nginx-controller"),
        has("kubectl", "kustomize", "ai-scanner-backend-deployment"),
        secret("gateway-secrets"),
        has("annotate", "serviceaccount", "default"),
        has("helm", "jenkins/jenkins"), has("get", "svc", "jenkins"), has("helm", "jenkins/jenkins"),
        has("helm", "argo/argo-cd"), has("get", "applications"), has("argocd-initial-admin-secret"))
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

    jv = jenkins_values(sim)
    assert len(jv) == 2
    for v in jv:
        assert v["env"] == {"REGISTRY_KIND": "ecr", "REGISTRY": ECR, "DEPLOY_OWNER": "sim-owner",
                            "DEPLOY_ENV": "eks", "AWS_REGION": "us-east-1"}
        assert v["values"]["controller"]["serviceType"] == "LoadBalancer"
    assert jv[1]["values"]["controller"]["jenkinsUrl"] == f"http://{JENKINS_LB}:8080/"

    r = check_repos_rendered(sim)
    be = r["ai-scanner-backend-deployment"]
    assert deployment_container(be, "review-api")["image"] == f"{ECR}/review-api:bootstrap"
    sc = find(be, "StorageClass", "reports-noexec")
    assert sc["provisioner"] == "efs.csi.aws.com" and sc["parameters"]["fileSystemId"] == EFS
    assert "mountOptions" not in sc
    pvc = find(be, "PersistentVolumeClaim", "ai-scan-repos-reports")
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    assert find(be, "Ingress", "ai-scan-repos")["spec"]["ingressClassName"] == "nginx"
    assert find(be, "Service", "lmstudio") is None

    gw = r["ai-scanner-gateway-deployment"]
    genv = env_of(deployment_container(gw, "gateway"))
    assert deployment_container(gw, "gateway")["image"] == f"{ECR}/review-gateway:bootstrap"
    assert genv["GATEWAY_FRONTEND_URL"] == f"https://{DOMAIN}"
    assert genv["GITHUB_OAUTH_REDIRECT_URI"] == f"https://{DOMAIN}/auth/github/callback"
    assert genv["GATEWAY_COOKIE_SECURE"] == "true"
    assert genv["GATEWAY_DEFAULT_AI_PRESET"] == "openai"
    assert genv["GATEWAY_DEFAULT_AI_MODEL"] == "gpt-4.1-mini"
    assert genv["GATEWAY_DEFAULT_AI_KEY"]["secretKeyRef"]["key"] == "default-ai-key"
    assert deployment_container(r["ai-scanner-frontend-deployment"], "frontend")["image"] == \
        f"{ECR}/review-frontend:bootstrap"
    push = sim.calls[sim.index(has("git", "commit"))]
    assert f"installer: overlays/eks for https://{DOMAIN}" in push.args

    gs = secret_data(sim, "gateway-secrets")
    assert gs["default-ai-key"] == API_KEY and gs["github-oauth-client-id"] == OAUTH_ID
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
    (work / "terraform.tfstate").write_text("{}", encoding="utf-8")
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
    assert sim.prompts == []
    assert_no_secrets(sim, run, a)


def test_d_destroy_eks_removes_webhooks(sim):
    """Install on EKS saves the hook ids and URL (Jenkins's load balancer); destroy removes them."""
    plans = sim.plans                           # type: ignore[attr-defined]
    sim.flavour = EKS
    a = eks_answers()
    install = run_plan(sim, a)
    assert_done(install)
    hook = f"http://{JENKINS_LB}:8080/github-webhook/"
    assert a["generated"]["webhook_url"] == hook and len(a["generated"]["webhook_ids"]) == 3
    run = run_plan(sim, a, "destroy")
    assert_done(run)
    assert [h[:3] for h in sim.deleted_hooks] == [(TOKEN, "sim-owner", r) for r in
                                                 ("ai-scanner-backend", "ai-scanner-gateway", "ai-scanner-frontend")]
    assert all(h[3] == a["generated"]["webhook_ids"][h[2]] and hook in h[3:] for h in sim.deleted_hooks)


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
    assert not any(c.args[0] in ("terraform", "aws") for c in sim.calls)
    assert [h[:3] for h in sim.deleted_hooks] == [(TOKEN, "sim-owner", r) for r in
                                                 ("ai-scanner-backend", "ai-scanner-gateway", "ai-scanner-frontend")]
    assert all(SMEE in h[3:] or any(isinstance(x, int) for x in h[3:]) for h in sim.deleted_hooks)
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

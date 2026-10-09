"""Simulated full runs (second, independent suite): install_plan / destroy_plan
driven through the real runner, with every outside effect faked.

Faked: Ctx.sh (a fake cluster/AWS/git/ssh answering by command pattern),
kube.flavour, the SSH kubeconfig read (kube.subprocess.run), github.*,
httpx.Client (Jenkins over the port-forward), PortForward's Popen and
plans.time.sleep. Real: the runner thread and its redaction, every plan step,
overlays.py / platform.py, the kubeconfig rewrite and `kubectl kustomize`
on a copy of the three deployment repositories (working tree, including
untracked kustomization.yaml files). Nothing touches AWS, GitHub or a cluster.
"""

from __future__ import annotations

import base64
import json
import shlex
import shutil
import subprocess
import threading
import time
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from installer import github, kube, plans, runner
from installer.runner import StepFailed

REPOS_HOME = Path(r"C:\Projects\Final-Project-Repos")
DEPLOY = ["ai-scanner-backend-deployment", "ai-scanner-gateway-deployment", "ai-scanner-frontend-deployment"]
TF_SRC = Path(__file__).resolve().parent.parent / "terraform" / "eks"
REAL_RUN = subprocess.run

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None or not all(
    (REPOS_HOME / r).is_dir() for r in DEPLOY), reason="needs kubectl and the deployment repos")

ING_HOST = "k8s-ingress-abc123.elb.us-east-1.amazonaws.com"
JEN_HOST = "k8s-jenkins-def456.elb.us-east-1.amazonaws.com"
ECR = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
ARGO_PW = "argoInitialPw-77xyz"
TOKEN = "ghp_SIMTOKEN0123456789abcdef"
OAUTH_SECRET = "oauthSecret-5f2e9c0d1a"
SMEE = "https://smee.io/simChannelAbc123"
OAUTH_REPLY = {"client_id": "Iv1.simclient", "client_secret": OAUTH_SECRET}


# --- the repos snapshot ----------------------------------------------------------

@pytest.fixture(scope="session")
def repo_snapshot(tmp_path_factory) -> Path:
    """`git ls-files -co --exclude-standard` (tracked + untracked) of each deployment
    repo, copied as in the working tree. Same as `git ls-files | tar`, in Python."""
    root = tmp_path_factory.mktemp("snapshot")
    for r in DEPLOY:
        src = REPOS_HOME / r
        names = REAL_RUN(["git", "-C", str(src), "ls-files", "-co", "--exclude-standard", "-z"],
                         capture_output=True, text=True, check=True).stdout.split("\0")
        for n in filter(None, names):
            f = src / n
            if f.is_file():                      # an index entry deleted in the tree is skipped
                (root / r / n).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, root / r / n)
    assert (root / "ai-scanner-backend-deployment" / "storage" / "kustomization.yaml").is_file()
    assert (root / "ai-scanner-gateway-deployment" / "app" / "kustomization.yaml").is_file()
    return root


# --- the fake world ----------------------------------------------------------------

class World:
    """Answers every command a step runs, records it, and runs `kubectl kustomize` for real."""

    def __init__(self, snapshot: Path, tf_out: dict[str, Any] | None = None, cluster_gone: bool = False):
        self.snapshot = snapshot
        self.calls: list[dict[str, Any]] = []
        self.kustomize: dict[str, str] = {}
        self.tf_out = tf_out or {}
        self.cluster_gone = cluster_gone
        self.webhooks: list[tuple] = []
        self.deleted_hooks: list[tuple] = []
        self.jenkins_posts: list[str] = []
        self.popen: list[list[str]] = []
        self.terminated = 0

    def answer(self, args: list[str], input: str | None, env: dict, cwd: str | None) -> tuple[int, str]:
        s = " ".join(args)
        if args[:2] == ["kubectl", "kustomize"]:
            p = REAL_RUN(args, capture_output=True, text=True)
            self.kustomize[str(Path(args[2]))] = p.stdout
            return p.returncode, p.stdout + p.stderr
        if args[0] == "git":
            if "clone" in args:
                name = args[-2].rsplit("/", 1)[1].removesuffix(".git")
                shutil.copytree(self.snapshot / name, args[-1])
                return 0, ""
            if "status" in args:
                return 0, " M overlays/x/app-patch.yaml\n"
            return 0, ""
        if args[0] == "ssh":
            return 0, ""
        if args[0] == "aws":
            if "sts" in args:
                return 0, "arn:aws:iam::123456789012:user/sim\n"
            if "update-kubeconfig" in args:
                if self.cluster_gone:
                    return 254, "An error occurred (ResourceNotFoundException) when calling DescribeCluster\n"
                Path(args[args.index("--kubeconfig") + 1]).write_text("apiVersion: v1\n", encoding="utf-8")
                return 0, "Updated context in kubeconfig\n"
            return 0, ""
        if args[0] == "terraform":
            if "output" in args:
                return 0, json.dumps({k: {"value": v} for k, v in self.tf_out.items()})
            return 0, f"terraform {args[1]}: ok\n"
        if args[0] == "kubectl":
            if "get" in args and "svc" in args:
                return 0, ING_HOST if "ingress-nginx-controller" in args else JEN_HOST
            if "get" in args and "storageclass" in args:
                return 0, json.dumps({"items": [{"metadata": {"name": "standard", "annotations": {
                    "storageclass.kubernetes.io/is-default-class": "true"}},
                    "provisioner": "k8s.io/minikube-hostpath"}]})
            if "get" in args and "applications" in args:
                return 0, json.dumps({"items": [{"metadata": {"name": n}, "status": {
                    "sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}
                    for n in ("ai-scan-backend", "ai-scan-gateway", "ai-scan-frontend")]})
            if "argocd-initial-admin-secret" in args:
                return 0, base64.b64encode(ARGO_PW.encode()).decode()
            if "exec" in args:
                return 0, '{"status": "ok"}\n'
            if "get" in args and "serviceaccount" in args:
                return 0, "serviceaccount/default"
            if "get" in args and "nodes" in args:
                return 0, "node1   Ready   control-plane\n"
            if "apply" in args:
                return 0, "configured\n"
            return 0, ""
        if args[0] == "helm":
            return 0, ""
        raise AssertionError(f"unexpected command: {s}")

    def joined(self) -> list[str]:
        return [" ".join(c["args"]) for c in self.calls]

    def find(self, needle: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if needle in " ".join(c["args"])]

    def assert_order(self, *needles: str) -> None:
        cmds, pos = self.joined(), -1
        for n in needles:
            nxt = next((i for i in range(pos + 1, len(cmds)) if n in cmds[i]), None)
            assert nxt is not None, f"{n!r} not found after command #{pos}: {cmds[pos] if pos >= 0 else ''}"
            pos = nxt


class FakeResp:
    def __init__(self, code: int, body: Any = None):
        self.status_code, self._body = code, body

    def json(self) -> Any:
        return self._body


def fake_jenkins(world: World, builds: dict[str, int]):
    """A Jenkins whose main branch builds once per POST (scan or build), and
    succeeds. `builds` (job -> last build number) outlives one run, as Jenkins does."""
    class Client:
        def __init__(self, base_url: str = "", auth: Any = None, timeout: Any = None, **kw: Any):
            assert base_url == "http://127.0.0.1:18080"
            assert auth[0] == "admin"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path: str) -> FakeResp:
            if path == "/crumbIssuer/api/json":
                return FakeResp(200, {"crumbRequestField": "Jenkins-Crumb", "crumb": "c1"})
            if path.endswith("/lastBuild/api/json"):
                n = builds.get(path.split("/")[2], 0)
                return FakeResp(200, {"building": False, "result": "SUCCESS", "number": n}) if n else FakeResp(404)
            return FakeResp(200, {})

        def post(self, path: str, headers: dict | None = None) -> FakeResp:
            assert headers == {"Jenkins-Crumb": "c1"}
            world.jenkins_posts.append(path)
            job = path.split("/")[2]
            builds[job] = builds.get(job, 0) + 1
            return FakeResp(201)
    return Client


@pytest.fixture()
def sim(tmp_path, monkeypatch, repo_snapshot):
    """Returns make(world, flavour) -> workdir; installs every fake."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("INSTALLER_DATA", str(data))
    monkeypatch.setattr(plans, "DATA", data)
    monkeypatch.setattr(plans, "TF_DIR", TF_SRC)
    clock = {"skew": 0.0}            # a fake clock: sleep() advances it, so polls and timeouts cost nothing

    def _sleep(sec):
        clock["skew"] += sec
    monkeypatch.setattr(plans, "time", types.SimpleNamespace(time=lambda: time.time() + clock["skew"],
                                                             sleep=_sleep))
    holder: dict[str, World] = {}
    jenkins_builds: dict[str, int] = {}

    def fake_sh(self, args, *, input=None, check=True, env=None, quiet=False, cwd=None, timeout=None):
        w = holder["w"]
        w.calls.append({"args": list(args), "input": input, "env": dict(env or {}), "cwd": cwd,
                        "quiet": quiet, "step": self.step_id})
        if not quiet:
            self.log("$ " + " ".join(args), "cmd")
        code, out = w.answer(list(args), input, env or {}, cwd)
        if not quiet:
            for line in out.splitlines():
                self.log(line, "out")
        if check and code != 0:
            tail = self.run.redact(out.strip().splitlines()[-1] if out.strip() else "")
            raise StepFailed(f"`{' '.join(args[:3])}` failed (exit {code}). {tail}")
        return out

    class FakePopen:
        def __init__(self, args, **kw):
            holder["w"].popen.append(list(args))
            holder["w"].calls.append({"args": list(args), "input": None, "env": {}, "cwd": None,
                                      "quiet": True, "step": "popen"})

        def terminate(self):
            holder["w"].terminated += 1

    monkeypatch.setattr(runner.Ctx, "sh", fake_sh)
    monkeypatch.setattr(plans, "subprocess", types.SimpleNamespace(
        Popen=FakePopen, DEVNULL=subprocess.DEVNULL, SubprocessError=subprocess.SubprocessError,
        run=subprocess.run))
    monkeypatch.setattr(github, "new_smee_channel", lambda: SMEE)
    def ensure_webhook(token, owner, repo, url, known_id=None):
        w = holder["w"]
        w.webhooks.append((token, owner, repo, url, known_id))
        return known_id or 100 + github.CODE_REPOS.index(repo)

    def delete_webhook(token, owner, repo, hook_id, url=""):
        holder["w"].deleted_hooks.append((owner, repo, hook_id, url))
        return "removed" if hook_id else "none found"
    monkeypatch.setattr(github, "ensure_webhook", ensure_webhook)
    monkeypatch.setattr(github, "delete_webhook", delete_webhook)
    monkeypatch.setattr(github, "file_text", lambda token, owner, repo, path, ref="main":
                        "env.DEPLOY_OWNER ?: 'DevOpsCourse22'" if path == "Jenkinsfile" else "")

    def make(world: World, flavour: dict[str, Any]) -> Path:
        holder["w"] = world
        monkeypatch.setattr(kube, "flavour", lambda kubectl, env=None: dict(flavour))
        monkeypatch.setattr(httpx, "Client", fake_jenkins(world, jenkins_builds))
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        return work
    return make


def drive(kind: str, a: dict[str, Any], work: Path,
          answers: list[tuple[str, dict[str, str]]]) -> tuple[runner.Run, list[dict]]:
    """Start the run, answer each wait_for_user pause in order (matched by a
    word in its message or command), wait for the end."""
    plans.ensure_generated(a)
    steps = plans.destroy_plan(a) if kind == "destroy" else plans.install_plan(a)
    run = runner.start(kind, steps, a, plans.secret_values(a), str(work))
    seen: list[dict] = []
    try:
        _follow(run, answers, seen)
    finally:
        if run.status in ("running", "waiting"):   # never leave a run behind for the next test
            runner._current = None
            run.cancelled = True
            run.resume.set()
    if run.status == "failed":
        msgs = "\n".join(line["text"] for line in run.lines[-30:])
        pytest.fail(f"run failed:\n{msgs}")
    assert not answers, f"pauses never came: {answers}"
    return run, seen


def _follow(run: runner.Run, answers: list, seen: list) -> None:
    last = None
    end = time.time() + 30
    while run.status not in ("done", "failed") and time.time() < end:
        act = run.action
        if run.status == "waiting" and act is not None and act is not last:
            time.sleep(0.05)              # see test_runner_continue_right_after_pause_is_not_lost
            last = act
            seen.append(act)
            assert answers, f"unexpected pause: {act['message']}"
            word, reply = answers.pop(0)
            assert word in act["message"] + " " + act["command"], (word, act["message"])
            run.reply = dict(reply)
            run.resume.set()
        time.sleep(0.01)
    if run.status not in ("done", "failed"):
        step = next((x.id for x in run.steps if x.status in ("running", "waiting")), "?")
        tail = "\n".join(line["text"] for line in run.lines[-15:])
        raise AssertionError(f"run did not finish (step {step}, status {run.status}):\n{tail}")


def assert_no_secret_in_log(run: runner.Run, a: dict[str, Any], world: World, extra: list[str] = ()) -> None:
    urls = {a.get("generated", {}).get("smee_url"), a.get("generated", {}).get("webhook_url")}
    secrets = [s for s in plans.secret_values(a) + list(extra) if len(s) >= 4 and s not in urls]
    for line in run.lines:
        for s in secrets:
            assert s not in line["text"], f"secret leaked in log line: {line['text'][:120]}"
    for c in world.calls:                      # and never on a command line
        for s in secrets:
            assert s not in " ".join(c["args"]), f"secret on a command line: {c['args'][:4]}"


def rendered(world: World, work: Path, repo: str, env: str) -> list[dict]:
    key = str(work / "repos" / repo / "overlays" / env)
    assert key in world.kustomize, f"kubectl kustomize never ran on {key}"
    return [d for d in yaml.safe_load_all(world.kustomize[key]) if d]


def obj(docs: list[dict], kind: str, name: str) -> dict:
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def env_of(dep: dict) -> dict[str, Any]:
    c = dep["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in c.get("env", [])}


def jenkins_env(call: dict) -> tuple[dict[str, str], dict]:
    v = yaml.safe_load(call["input"])
    casc = yaml.safe_load(v["controller"]["JCasC"]["configScripts"]["ci-registry"])
    return {e["key"]: e["value"] for e in casc["jenkins"]["globalNodeProperties"][0]["envVars"]["env"]}, v


# --- a) local minikube, AI on the same machine ----------------------------------------

def _minikube_kubeconfig(tmp: Path, monkeypatch) -> None:
    mk = tmp / "minikube"
    (mk / "profiles" / "minikube").mkdir(parents=True)
    (mk / "ca.crt").write_bytes(b"CA-CERT")
    (mk / "profiles" / "minikube" / "client.crt").write_bytes(b"CLIENT-CERT")
    (mk / "profiles" / "minikube" / "client.key").write_bytes(b"CLIENT-KEY")
    cfg = {"apiVersion": "v1", "kind": "Config", "current-context": "minikube",
           "clusters": [{"name": "minikube", "cluster": {
               "server": "https://127.0.0.1:52345", "certificate-authority": r"C:\Users\sim\.minikube\ca.crt"}}],
           "users": [{"name": "minikube", "user": {
               "client-certificate": r"C:\Users\sim\.minikube\profiles\minikube\client.crt",
               "client-key": r"C:\Users\sim\.minikube\profiles\minikube\client.key"}}],
           "contexts": [{"name": "minikube", "context": {"cluster": "minikube", "user": "minikube"}}]}
    kc = tmp / "kube" / "config"
    kc.parent.mkdir()
    kc.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setattr(kube, "HOST_KUBE", kc)
    monkeypatch.setattr(kube, "HOST_MINIKUBE", mk)
    monkeypatch.setattr(kube, "_resolves", lambda name: False)


@pytest.mark.xfail(strict=True, reason="BUG kube.py:69: _embed stores the base64 file content under the "
                   "path key ('certificate-authority', 'client-certificate', 'client-key') because "
                   "key.replace('-file', '-data') never matches; kubectl then reads base64 as a file path")
def test_a_kubeconfig_embeds_certificates(tmp_path, monkeypatch):
    _minikube_kubeconfig(tmp_path, monkeypatch)
    kube.local_kubeconfig("minikube", tmp_path / "out")
    kc = yaml.safe_load((tmp_path / "out").read_text(encoding="utf-8"))
    cl, us = kc["clusters"][0]["cluster"], kc["users"][0]["user"]
    assert "certificate-authority" not in cl and "client-certificate" not in us and "client-key" not in us
    assert base64.b64decode(cl["certificate-authority-data"]) == b"CA-CERT"
    assert base64.b64decode(us["client-certificate-data"]) == b"CLIENT-CERT"
    assert base64.b64decode(us["client-key-data"]) == b"CLIENT-KEY"


MINIKUBE = {"kind": "minikube", "node_ip": "192.168.49.2", "nodes": 1, "version": "v1.33.1",
            "runtime": "docker://27.2.0", "arch": "amd64"}


def local_answers() -> dict[str, Any]:
    return {"target": {"kind": "local", "context": "minikube"},
            "repos": {"owner": "fork", "token": TOKEN},
            "ai": {"mode": "same_machine", "endpoint": "http://host.minikube.internal:1234/v1",
                   "model": "qwen2.5-coder"},
            "admins": ["alice", "bob"]}


def test_a_local_minikube_same_machine(sim, tmp_path, monkeypatch, repo_snapshot):
    _minikube_kubeconfig(tmp_path, monkeypatch)
    world = World(repo_snapshot)
    work = sim(world, MINIKUBE)
    a = local_answers()
    run, pauses = drive("install", a, work, [("minikube tunnel", {}), ("OAuth App", OAUTH_REPLY)])

    assert run.status == "done"
    assert pauses[0]["command"] == "minikube tunnel"
    assert pauses[1]["info"]["Authorization callback URL"] == "http://localhost/auth/github/callback"
    # kubeconfig rewritten for the container (certificate embedding: test_a_kubeconfig_embeds_certificates)
    kc = yaml.safe_load((work / "kubeconfig").read_text(encoding="utf-8"))
    cl = kc["clusters"][0]["cluster"]
    assert cl["server"] == "https://host.docker.internal:52345" and cl["tls-server-name"] == "127.0.0.1"
    assert all(c["args"][1:3] == ["--kubeconfig", str(work / "kubeconfig")]
               for c in world.calls if c["args"][0] in ("kubectl", "helm") and c["args"][1] != "kustomize")

    world.assert_order(
        "git clone --quiet --depth 20 https://x-access-token@github.com/fork/ai-scanner-backend-deployment.git",
        "ai-scanner-frontend-deployment.git",
        "apply -f -",                                         # registry
        "-n registry rollout status deploy/registry",
        "repo add ingress-nginx", "upgrade --install ingress-nginx",
        "get storageclass -o json",
        "kubectl kustomize", "add overlays/local", "status --porcelain",
        "commit --quiet -m installer: overlays/local for http://localhost", "push --quiet origin HEAD:main",
        "-n ai-scan-repos rollout restart deploy/gateway",
        "repo add jenkins", "upgrade --install jenkins jenkins/jenkins",
        "apply -f -",                                         # smee client
        "upgrade --install argocd argo/argo-cd",
        "port-forward svc/jenkins 18080:8080",
        "annotate application ai-scan-backend argocd.argoproj.io/refresh=hard",
        "get applications -o json", "exec deploy/gateway",
        "argocd-initial-admin-secret")
    assert not world.find("tailscale") and not world.find("terraform") and not world.find("ssh")
    reg_apply = next(c for c in world.find("apply -f -") if c["input"] and "name: registry" in c["input"])
    assert "clusterIP: 10.96.100.100" in reg_apply["input"]

    be = rendered(world, work, "ai-scanner-backend-deployment", "local")
    api = obj(be, "Deployment", "review-api")
    assert env_of(api)["LMSTUDIO_BASE_URL"] == "http://host.minikube.internal:1234/v1"
    assert api["spec"]["template"]["spec"]["containers"][0]["image"] == "10.96.100.100:5000/review-api:bootstrap"
    assert not [d for d in be if d["kind"] == "Service" and d["metadata"]["name"] == "lmstudio"]
    sc = obj(be, "StorageClass", "reports-noexec")
    assert sc["provisioner"] == "k8s.io/minikube-hostpath" and "mountOptions" not in sc
    assert obj(be, "Ingress", "ai-scan-repos")["spec"]["ingressClassName"] == "nginx"
    gw = env_of(obj(rendered(world, work, "ai-scanner-gateway-deployment", "local"), "Deployment", "gateway"))
    assert gw["GATEWAY_FRONTEND_URL"] == "http://localhost" and gw["GATEWAY_COOKIE_SECURE"] == "false"
    assert gw["GATEWAY_DEFAULT_AI_PRESET"] == "local" and "GATEWAY_DEFAULT_AI_KEY" not in gw
    fe = obj(rendered(world, work, "ai-scanner-frontend-deployment", "local"), "Deployment", "frontend")
    assert fe["spec"]["template"]["spec"]["containers"][0]["image"] == "10.96.100.100:5000/review-frontend:bootstrap"

    jenv, _ = jenkins_env(world.find("upgrade --install jenkins")[-1])
    assert jenv == {"REGISTRY_KIND": "local", "REGISTRY": "10.96.100.100:5000",
                    "DEPLOY_OWNER": "fork", "DEPLOY_ENV": "local"}
    assert [x[2:4] for x in world.webhooks] == [(r, SMEE) for r in github.CODE_REPOS]
    assert a["generated"]["webhook_ids"] == {r: 100 + i for i, r in enumerate(github.CODE_REPOS)}
    assert a["generated"]["smee_url"] == SMEE
    assert sorted(world.jenkins_posts) == sorted(f"/job/{j}/build?delay=0" for j in github.CODE_REPOS)
    assert world.terminated == 1
    assert run.result["app_url"] == "http://localhost" and run.result["registry"] == "10.96.100.100:5000"
    assert a["generated"]["argocd_password"] == ARGO_PW
    assert_no_secret_in_log(run, a, world, [OAUTH_SECRET, ARGO_PW])
    push = world.find("push --quiet origin HEAD:main")[0]     # the token only through askpass
    assert push["env"]["GIT_TOKEN"] == TOKEN and "GIT_ASKPASS" in push["env"]


# --- b) SSH K3s with a sudo password, AI over the tailnet ------------------------------

SUDO_PW = "sudoPa55-wordQ!"
PRIVKEY = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
           "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
           "QyNTUxOQAAACBzaW11bGF0ZWRrZXlzaW11bGF0ZWRrZXlzaW11bGF0ZWQAAAAA\n"
           "-----END OPENSSH PRIVATE KEY-----\n")
TS_SECRET = "tskey-client-kSIMSECRET-abcdef0123456789"
K3S = {"kind": "k3s", "node_ip": "10.0.0.5", "nodes": 1, "version": "v1.31.4+k3s1",
       "runtime": "containerd://1.7.23-k3s2", "arch": "amd64"}


def test_b_ssh_k3s_sudo_tailnet(sim, tmp_path, monkeypatch, repo_snapshot):
    world = World(repo_snapshot)
    work = sim(world, K3S)
    ssh_calls = []

    def fake_run(args, input=None, capture_output=False, text=False, timeout=None):
        ssh_calls.append((args, input))
        k3s_yaml = yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "clusters": [
            {"name": "default", "cluster": {"server": "https://127.0.0.1:6443", "certificate-authority-data": "Q0E="}}],
            "users": [{"name": "default", "user": {"token": "x"}}],
            "contexts": [{"name": "default", "context": {"cluster": "default", "user": "default"}}],
            "current-context": "default"})
        return subprocess.CompletedProcess(args, 0, k3s_yaml, "")
    monkeypatch.setattr(kube, "subprocess", types.SimpleNamespace(run=fake_run))

    a = {"target": {"kind": "ssh", "ssh": {"host": "10.0.0.5", "user": "ubuntu", "port": 22,
                                           "private_key": PRIVKEY, "sudo_password": SUDO_PW}},
         "repos": {"owner": "fork", "token": TOKEN},
         "ai": {"mode": "tailnet", "tailnet_ip": "100.93.244.39", "tailnet_port": 1234, "model": "qwen",
                "ts_client_id": "kSIMID", "ts_client_secret": TS_SECRET},
         "admins": ["alice"]}
    run, pauses = drive("install", a, work, [("OAuth App", OAUTH_REPLY)])
    assert run.status == "done" and len(pauses) == 1
    assert pauses[0]["info"]["Homepage URL"] == "http://10.0.0.5"

    # the kubeconfig came over ssh with sudo -S and the password on stdin
    (args, stdin), = ssh_calls
    assert args[0] == "ssh" and "ubuntu@10.0.0.5" in args and stdin == SUDO_PW + "\n"
    assert args[-1].startswith("sudo -S -p '' sh -c ")
    kc = yaml.safe_load((work / "kubeconfig").read_text(encoding="utf-8"))
    assert kc["clusters"][0]["cluster"]["server"] == "https://10.0.0.5:6443"
    assert (work / "ssh" / "id").read_text(encoding="utf-8") == PRIVKEY

    # registries.yaml written over ssh as root, with the sudo password on stdin only
    reg = [c for c in world.calls if c["args"][0] == "ssh" and "registries.yaml" in c["args"][-1]]
    assert len(reg) == 2                                 # read, then write
    write = reg[1]
    assert write["args"][-1].startswith("sudo -S -p '' sh -c ")
    assert "> /etc/rancher/k3s/registries.yaml" in write["args"][-1]
    assert "systemctl restart k3s" in write["args"][-1]
    assert write["input"] == SUDO_PW + "\n" and reg[0]["input"] == SUDO_PW + "\n"
    assert "-i" in write["args"] and str(work / "ssh" / "id") in write["args"]
    # the remote shell gets valid YAML: unquote the command as sh would and parse the file content
    inner = shlex.split(write["args"][-1])[-1]
    content = shlex.split(inner.split("printf")[1].split(">")[0])[1]
    assert yaml.safe_load(content) == {"mirrors": {"10.0.0.5:30500": {"endpoint": ["http://10.0.0.5:30500"]}}}

    world.assert_order("git clone", "apply -f -", "-n registry rollout status deploy/registry",
                       "cat /etc/rancher/k3s/registries.yaml", "systemctl restart k3s", "get nodes",
                       "repo add tailscale", "upgrade --install tailscale-operator",
                       "kubectl kustomize", "commit --quiet -m installer: overlays/local for http://10.0.0.5",
                       "upgrade --install jenkins", "upgrade --install argocd", "port-forward",
                       "get applications")
    assert not world.find("ingress-nginx")               # K3s's Traefik is used
    ts = world.find("upgrade --install tailscale-operator")[0]["args"]
    assert "-f" in ts and ts[ts.index("-f") + 1].endswith(str(Path("tailscale-operator") / "values.yaml"))
    ts_secret = [c for c in world.calls if c["input"] and '"operator-oauth"' in c["input"]][0]
    assert ts_secret["quiet"]

    be = rendered(world, work, "ai-scanner-backend-deployment", "local")
    lm = obj(be, "Service", "lmstudio")
    assert lm["metadata"]["annotations"]["tailscale.com/tailnet-ip"] == "100.93.244.39"
    assert env_of(obj(be, "Deployment", "review-api"))["LMSTUDIO_BASE_URL"] == "http://lmstudio:1234/v1"
    sc = obj(be, "StorageClass", "reports-noexec")
    assert sc["provisioner"] == "rancher.io/local-path" and "noexec" in sc["mountOptions"]
    assert obj(be, "Ingress", "ai-scan-repos")["spec"]["ingressClassName"] == "traefik"
    gw = env_of(obj(rendered(world, work, "ai-scanner-gateway-deployment", "local"), "Deployment", "gateway"))
    assert gw["GATEWAY_DEFAULT_AI_ENDPOINT"] == "http://lmstudio:1234/v1" and gw["GATEWAY_COOKIE_SECURE"] == "false"
    jenv, _ = jenkins_env(world.find("upgrade --install jenkins")[-1])
    assert jenv["REGISTRY_KIND"] == "local" and jenv["REGISTRY"] == "10.0.0.5:30500"
    assert_no_secret_in_log(run, a, world, [OAUTH_SECRET, ARGO_PW, SUDO_PW, TS_SECRET])
    assert all(SUDO_PW not in (c["input"] or "") for c in world.calls if c["args"][0] != "ssh")


# --- c) EKS with a domain, cloud AI -------------------------------------------------------

AWS_SECRET = "awsSIMsecretKey/0123456789abcdefGHIJ"
AI_KEY = "sk-sim-cloudkey-0123456789"
TF_OUT = {"cluster_name": "ai-scan-repos", "region": "us-east-1", "ecr_registry": ECR,
          "efs_id": "fs-0abc1234def567890",
          "jenkins_role_arn": "arn:aws:iam::123456789012:role/ai-scan-repos-jenkins",
          "acm_certificate_arn": "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222",
          "acm_validation": [{"name": "_abc.scan.example.com.", "type": "CNAME",
                              "value": "_def.acm-validations.aws."}]}
EKS = {"kind": "eks", "node_ip": "10.0.1.10", "nodes": 2, "version": "v1.31.2-eks-7f9249a",
       "runtime": "containerd://1.7.22", "arch": "amd64"}
EKS_PAUSES = [("DNS record", {}), ("Point your domain", {}), ("OAuth App", OAUTH_REPLY)]


def eks_answers() -> dict[str, Any]:
    return {"target": {"kind": "eks", "eks": {"region": "us-east-1", "name": "ai-scan-repos",
                                              "aws_access_key_id": "AKIASIMULATED000",
                                              "aws_secret_access_key": AWS_SECRET,
                                              "domain": "scan.example.com", "node_type": "t3.large",
                                              "node_count": 2}},
            "repos": {"owner": "fork", "token": TOKEN},
            "ai": {"mode": "cloud", "preset": "openai", "model": "gpt-4o-mini", "api_key": AI_KEY},
            "admins": ["alice"]}


def test_c_eks_domain_cloud(sim, tmp_path, repo_snapshot):
    world = World(repo_snapshot, tf_out=TF_OUT)
    work = sim(world, EKS)
    a = eks_answers()
    run, pauses = drive("install", a, work, list(EKS_PAUSES))
    assert run.status == "done"
    assert pauses[0]["info"] == {"Type": "CNAME", "Name": "_abc.scan.example.com.",
                                 "Value": "_def.acm-validations.aws."}
    assert pauses[1]["info"] == {"Type": "CNAME", "Name": "scan.example.com", "Value": ING_HOST}
    assert pauses[2]["info"]["Authorization callback URL"] == "https://scan.example.com/auth/github/callback"
    assert a["oauth"]["for_url"] == "https://scan.example.com"

    data = Path(plans.DATA)
    assert json.loads((data / "terraform" / "installer.auto.tfvars.json").read_text())["domain"] == "scan.example.com"
    assert (data / "terraform" / "main.tf").is_file()
    tf = [c for c in world.calls if c["args"][0] in ("terraform", "aws")]
    assert all(c["env"].get("AWS_SECRET_ACCESS_KEY") == AWS_SECRET for c in tf)
    assert all(c["cwd"] == str(data / "terraform") for c in tf if c["args"][0] == "terraform")

    world.assert_order(
        "aws sts get-caller-identity", "terraform init", "terraform apply -auto-approve", "terraform output -json",
        "aws eks update-kubeconfig --name ai-scan-repos --region us-east-1",
        "aws acm wait certificate-validated --certificate-arn " + TF_OUT["acm_certificate_arn"],
        "apply -f -", "annotate storageclass gp2",
        "git clone",
        "upgrade --install ingress-nginx",
        "get svc ingress-nginx-controller",
        "kubectl kustomize", "commit --quiet -m installer: overlays/eks for https://scan.example.com",
        "push --quiet origin HEAD:main",
        "-n jenkins get serviceaccount default",
        "annotate serviceaccount default --overwrite eks.amazonaws.com/role-arn=" + TF_OUT["jenkins_role_arn"],
        "upgrade --install jenkins", "get svc jenkins", "upgrade --install jenkins",
        "upgrade --install argocd", "port-forward", "get applications", "argocd-initial-admin-secret")
    assert not world.find("rollout status deploy/registry") and not world.find("tailscale-operator")
    assert not world.find("smee")
    sc = [c for c in world.find("apply -f -") if c["input"] and "kind: StorageClass" in c["input"]][0]
    assert "name: gp3" in sc["input"]
    ing = yaml.safe_load(world.find("upgrade --install ingress-nginx")[0]["input"])
    ann = ing["controller"]["service"]["annotations"]
    assert ann["service.beta.kubernetes.io/aws-load-balancer-ssl-cert"] == TF_OUT["acm_certificate_arn"]

    first, second = world.find("upgrade --install jenkins")
    jenv, v = jenkins_env(second)
    assert jenv["REGISTRY_KIND"] == "ecr" and jenv["REGISTRY"] == ECR
    assert jenv["AWS_REGION"] == "us-east-1" and jenv["DEPLOY_ENV"] == "eks"
    assert v["controller"]["jenkinsUrl"] == f"http://{JEN_HOST}:8080/"
    assert v["controller"]["serviceType"] == "LoadBalancer"
    assert yaml.safe_load(first["input"])["controller"]["jenkinsUrl"] == "http://jenkins.jenkins.svc.cluster.local:8080/"
    assert [x[2:4] for x in world.webhooks] == [(r, f"http://{JEN_HOST}:8080/github-webhook/")
                                                for r in github.CODE_REPOS]

    be = rendered(world, work, "ai-scanner-backend-deployment", "eks")
    sc = obj(be, "StorageClass", "reports-noexec")
    assert sc["provisioner"] == "efs.csi.aws.com" and sc["parameters"]["fileSystemId"] == TF_OUT["efs_id"]
    assert "mountOptions" not in sc
    assert obj(be, "PersistentVolumeClaim", "ai-scan-repos-reports")["spec"]["accessModes"] == ["ReadWriteMany"]
    api = obj(be, "Deployment", "review-api")["spec"]["template"]["spec"]["containers"][0]
    assert api["image"] == f"{ECR}/review-api:bootstrap"
    gdocs = rendered(world, work, "ai-scanner-gateway-deployment", "eks")
    gdep = obj(gdocs, "Deployment", "gateway")
    gw = env_of(gdep)
    assert gdep["spec"]["template"]["spec"]["containers"][0]["image"] == f"{ECR}/review-gateway:bootstrap"
    assert gw["GATEWAY_FRONTEND_URL"] == "https://scan.example.com"
    assert gw["GITHUB_OAUTH_REDIRECT_URI"] == "https://scan.example.com/auth/github/callback"
    assert gw["GATEWAY_COOKIE_SECURE"] == "true"
    assert gw["GATEWAY_DEFAULT_AI_PRESET"] == "openai" and gw["GATEWAY_DEFAULT_AI_MODEL"] == "gpt-4o-mini"
    assert gw["GATEWAY_DEFAULT_AI_KEY"]["secretKeyRef"]["key"] == "default-ai-key"
    assert "alice" in obj(gdocs, "ConfigMap", "gateway-admins")["data"]["admins"]
    patch = (work / "repos" / "ai-scanner-gateway-deployment" / "overlays" / "eks" / "app-patch.yaml").read_text()
    assert AI_KEY not in patch
    gs = [c for c in world.calls if c["input"] and '"gateway-secrets"' in c["input"]][0]
    assert gs["quiet"] and base64.b64encode(AI_KEY.encode()).decode() in gs["input"]
    fe = obj(rendered(world, work, "ai-scanner-frontend-deployment", "eks"), "Deployment", "frontend")
    assert fe["spec"]["template"]["spec"]["containers"][0]["image"] == f"{ECR}/review-frontend:bootstrap"

    assert run.result["app_url"] == "https://scan.example.com" and run.result["registry"] == ECR
    assert run.result["jenkins_url"] == f"http://{JEN_HOST}:8080/"
    assert_no_secret_in_log(run, a, world, [OAUTH_SECRET, ARGO_PW, AWS_SECRET, AI_KEY, TOKEN])


def test_c_eks_waits_for_certificate_before_ingress(sim, tmp_path, repo_snapshot):
    world = World(repo_snapshot, tf_out=TF_OUT)
    work = sim(world, EKS)
    drive("install", eks_answers(), work, list(EKS_PAUSES))
    world.assert_order("DNS-RECORD-PAUSE", "acm wait certificate-validated", "upgrade --install ingress-nginx")


# --- d) destroy -------------------------------------------------------------------------------

def test_d_destroy_eks(sim, tmp_path, repo_snapshot):
    world = World(repo_snapshot)
    work = sim(world, EKS)
    (Path(plans.DATA) / "terraform").mkdir()
    (Path(plans.DATA) / "terraform" / "terraform.tfstate").write_text("{}")
    a = eks_answers()
    run, pauses = drive("destroy", a, work, [])
    assert run.status == "done" and not pauses
    assert [s.status for s in run.steps] == ["done", "done", "done"]
    world.assert_order(
        "aws eks update-kubeconfig --name ai-scan-repos",
        "-n argocd delete application ai-scan-backend", "ai-scan-frontend",
        "delete namespace ai-scan-repos",
        "uninstall argocd -n argocd", "uninstall jenkins", "uninstall tailscale-operator",
        "uninstall ingress-nginx -n ingress-nginx",
        "delete namespace jenkins", "delete namespace ingress-nginx",
        "delete storageclass reports-noexec",
        "terraform init", "terraform destroy -auto-approve")
    tfd = world.find("terraform destroy")[0]
    assert tfd["cwd"] == str(Path(plans.DATA) / "terraform") and tfd["env"]["AWS_SECRET_ACCESS_KEY"] == AWS_SECRET
    assert_no_secret_in_log(run, a, world, [AWS_SECRET])


def test_d_destroy_eks_removes_webhooks(sim, tmp_path, repo_snapshot):
    """Install, then destroy with the answers the install saved: the 3 Jenkins hooks go.
    (Was a bug in the first review: only smee hooks were removed; fixed with webhook_ids.)"""
    world = World(repo_snapshot, tf_out=TF_OUT)
    work = sim(world, EKS)
    a = eks_answers()
    drive("install", a, work, list(EKS_PAUSES))
    world2 = World(repo_snapshot)
    sim(world2, EKS)
    drive("destroy", a, work, [])
    url = f"http://{JEN_HOST}:8080/github-webhook/"
    assert world2.deleted_hooks == [("fork", r, 100 + i, url) for i, r in enumerate(github.CODE_REPOS)]


def test_d_destroy_eks_no_state(sim, tmp_path, repo_snapshot):
    world = World(repo_snapshot)
    work = sim(world, EKS)
    run, _ = drive("destroy", eks_answers(), work, [])
    assert run.status == "done" and not world.find("terraform")
    assert any("No Terraform state" in line["text"] for line in run.lines)


def test_d_destroy_local(sim, tmp_path, monkeypatch, repo_snapshot):
    _minikube_kubeconfig(tmp_path, monkeypatch)
    world = World(repo_snapshot)
    work = sim(world, MINIKUBE)
    a = local_answers()
    plans.ensure_generated(a)
    # what a local install leaves in the answers
    a["generated"].update(smee_url=SMEE, webhook_url=SMEE,
                          webhook_ids={r: 100 + i for i, r in enumerate(github.CODE_REPOS)})
    run, pauses = drive("destroy", a, work, [])
    assert run.status == "done" and not pauses
    world.assert_order("-n argocd delete application ai-scan-backend", "delete namespace ai-scan-repos",
                       "uninstall argocd", "uninstall jenkins", "delete namespace registry",
                       "delete storageclass reports-noexec")
    assert not world.find("terraform") and not world.find("aws ")
    assert world.deleted_hooks == [("fork", r, 100 + i, SMEE) for i, r in enumerate(github.CODE_REPOS)]
    assert_no_secret_in_log(run, a, world)


# --- extra scenarios found while simulating ------------------------------------------------

def test_upgrade_local_keeps_tags_and_saved_oauth(sim, tmp_path, monkeypatch, repo_snapshot):
    """A second run on the same workdir: repos are pulled, the image tag Jenkins wrote
    is kept, and the saved OAuth App (same address) is reused without a pause."""
    _minikube_kubeconfig(tmp_path, monkeypatch)
    world = World(repo_snapshot)
    work = sim(world, MINIKUBE)
    a = local_answers()
    drive("install", a, work, [("minikube tunnel", {}), ("OAuth App", OAUTH_REPLY)])
    patch = work / "repos" / "ai-scanner-backend-deployment" / "overlays" / "local" / "app-patch.yaml"
    patch.write_text(patch.read_text(encoding="utf-8").replace("review-api:bootstrap", "review-api:abc1234"),
                     encoding="utf-8")
    world2 = World(repo_snapshot)
    sim(world2, MINIKUBE)
    run, pauses = drive("upgrade", a, work, [("minikube tunnel", {})])
    assert run.status == "done" and len(pauses) == 1
    assert not world2.find("git clone") and len(world2.find("pull --quiet --rebase")) >= 3
    api = obj(rendered(world2, work, "ai-scanner-backend-deployment", "local"), "Deployment", "review-api")
    assert api["spec"]["template"]["spec"]["containers"][0]["image"] == "10.96.100.100:5000/review-api:abc1234"
    assert [x[3:] for x in world2.webhooks] == [(SMEE, 100 + i) for i in range(3)]   # same channel, same hooks
    assert_no_secret_in_log(run, a, world2, [OAUTH_SECRET, ARGO_PW])


class _EagerRun(runner.Run):
    """Delivers the wizard's Continue the instant the pause becomes visible
    (run.action set), as POST /api/run/continue may do from another thread."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "action" and value is not None and getattr(self, "status", "") == "waiting":
            super().__setattr__("reply", {"client_id": "x"})
            self.resume.set()


@pytest.mark.xfail(strict=True, reason="BUG runner.py:131-138: wait_for_user publishes status 'waiting' and "
                   "the action BEFORE it resets run.reply and clears run.resume, so a Continue that lands in "
                   "that window is wiped and the run waits forever")
def test_runner_continue_right_after_pause_is_not_lost():
    run = _EagerRun(kind="install", steps=[runner.Step("s", "S", lambda c: None)])
    ctx = runner.Ctx(run, {}, ".")
    ctx.step_id = "s"
    got: dict[str, str] = {}
    t = threading.Thread(target=lambda: got.update(ctx.wait_for_user("m", fields=[{"name": "client_id"}])),
                         daemon=True)
    t.start()
    t.join(2)
    alive = t.is_alive()
    run.resume.set()               # release the thread either way
    assert not alive and got == {"client_id": "x"}

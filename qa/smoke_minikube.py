"""Live smoke test on minikube, without GitHub: runs the installer's own
steps for connect, registry, ingress and app secrets, writes the overlays
into a copy of the deployment repos, applies them with kubectl (instead of
Argo CD) using images loaded with `minikube image load`, then checks the app.

Run inside the installer image (see qa/README or the docker run line in the
thread): python -I /opt/installer/qa/smoke_minikube.py
"""

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/installer")
from installer import plans, runner  # noqa: E402

TAG = "smoke"
a = {"target": {"kind": "local", "context": "minikube"},
     "repos": {"owner": "DevOpsCourse22", "token": "unused"},
     "ai": {"mode": "same_machine", "endpoint": "http://host.minikube.internal:1234/v1", "model": ""},
     "admins": ["Elad-Eytan"], "public_url": "http://localhost",
     "oauth": {"client_id": "smoke-client", "client_secret": "smoke-secret-not-real", "for_url": "http://localhost"}}
plans.ensure_generated(a)

repos = Path("/tmp/repos")
shutil.rmtree(repos, ignore_errors=True)
shutil.copytree("/repos", repos, ignore=shutil.ignore_patterns(".git", "node_modules"))


def s_fake_clone(ctx):
    ctx.state["repos_dir"] = str(repos)
    ctx.state["installer_dir"] = "/tmp/installer"
    shutil.rmtree("/tmp/installer", ignore_errors=True)
    Path("/tmp/installer").mkdir()
    ctx.state["git_env"] = {}
    # the code repos' Jenkinsfile and ci/pod.yaml, from the mounted working trees
    plans.github.file_text = lambda token, owner, repo, path, ref="main": (
        Path("/code") / repo / path).read_text(encoding="utf-8")


def s_write_and_apply(ctx):
    orig = ctx.sh

    def sh(args, **kw):        # overlays: write files, skip git (no GitHub in this smoke test)
        if args[0] == "git":
            return ""
        return orig(args, **kw)
    ctx.sh = sh
    plans.s_overlays(ctx)
    ctx.sh = orig
    for app in ("backend", "gateway", "frontend"):
        d = Path("/tmp/installer/environments/local") / app
        f = d / "app-patch.yaml"
        f.write_text(f.read_text().replace(":bootstrap", f":{TAG}"))
        ctx.sh(plans.k(ctx) + ["apply", "-k", str(d)])
    for f in Path("/tmp/installer/environments/local/ci").iterdir():
        ctx.log(f"derived: {f.name} ({len(f.read_text())} bytes)")


def s_wait(ctx):
    for kind, name in (("statefulset", "postgres"), ("deploy", "review-api"), ("deploy", "gateway"),
                       ("deploy", "frontend")):
        ctx.sh(plans.k(ctx) + ["-n", plans.NS, "rollout", "status", f"{kind}/{name}", "--timeout=600s"])
    ctx.sh(plans.k(ctx) + ["-n", plans.NS, "get", "pods,pvc,ingress", "-o", "wide"])
    out = ctx.sh(plans.k(ctx) + ["-n", plans.NS, "exec", "deploy/gateway", "--", "python", "-c",
                                 "import urllib.request as u;print(u.urlopen('http://127.0.0.1:8080/health',timeout=10).read().decode())"])
    ctx.log("gateway health: " + out.strip())
    out = ctx.sh(plans.k(ctx) + ["-n", plans.NS, "exec", "deploy/review-api", "--", "python", "-c",
                                 "import socket;print(socket.gethostbyname('host.minikube.internal'))"], check=False)
    ctx.log("host.minikube.internal from a pod: " + out.strip())
    out = ctx.sh(plans.k(ctx) + ["-n", plans.NS, "exec", "deploy/review-api", "--", "python", "-c",
                                 "import json,urllib.request as u;print(json.load(u.urlopen('http://127.0.0.1:8000/api/v1/health?probe=1',timeout=30)).get('probe'))"],
                 check=False)
    ctx.log("review-api model probe: " + out.strip())
    out = ctx.sh(plans.k(ctx) + ["-n", plans.NS, "exec", "deploy/gateway", "--", "python", "-c",
                                 "import urllib.request as u;print(u.urlopen('http://frontend:8080/',timeout=10).read()[:60])"])
    ctx.log("frontend: " + out.strip())
    # the registry the pipelines push to answers inside the cluster
    out = ctx.sh(plans.k(ctx) + ["-n", plans.NS, "exec", "deploy/gateway", "--", "python", "-c",
                                 "import urllib.request as u;print(u.urlopen('http://10.96.100.100:5000/v2/',timeout=10).status)"])
    ctx.log("registry /v2/: " + out.strip())


steps = [runner.Step("connect", "Connect", plans.s_connect),
         runner.Step("clone", "Fake clone", s_fake_clone),
         runner.Step("registry", "Registry", plans.s_registry),
         runner.Step("ingress", "Ingress", plans.s_ingress),
         runner.Step("addr", "Address", lambda c: c.state.update(public_url="http://localhost")),
         runner.Step("secrets", "Secrets", plans.s_app_secrets),
         runner.Step("apply", "Overlays + apply", s_write_and_apply),
         runner.Step("database", "DB password", plans.s_db_password),
         runner.Step("check", "Checks", s_wait),
         runner.Step("ai", "Default AI model", plans.s_default_ai)]
run = runner.start("install", steps, a, plans.secret_values(a), "/tmp")
seen = 0
while run.status in ("running", "waiting"):
    for line in run.lines[seen:]:
        print(f"[{line['step']}] {line['text']}", flush=True)
    seen = len(run.lines)
    if run.status == "waiting":
        print("WAITING:", json.dumps(run.action), flush=True)
        break
    time.sleep(1)
for line in run.lines[seen:]:
    print(f"[{line['step']}] {line['text']}", flush=True)
print("RESULT:", run.status, [(s.id, s.status, s.detail) for s in run.steps], flush=True)

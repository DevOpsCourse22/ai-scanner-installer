"""The steps of an install, upgrade or destroy, for a local/server cluster
or for EKS. Each step is a small function of the run context (Ctx).

Answers (what the wizard collects, saved encrypted in the vault):

    target:  {kind: local|ssh|eks, context, ssh: {host, user, port, private_key, sudo_password},
              eks: {region, name, aws_profile, aws_access_key_id, aws_secret_access_key,
                    domain, node_type, node_count}}
    repos:   {owner, token}
    ai:      {mode: same_machine|tailnet|cloud, endpoint, model, preset, api_key,
              tailnet_ip, tailnet_port, ts_client_id, ts_client_secret}
    public_url: optional override of the app's address
    admins:  [GitHub logins]
    oauth:   {client_id, client_secret}        (asked during the run, once the address is known)
    generated: {postgres_password, settings_key, webhook_secret, jenkins_password, smee_url}
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import httpx

from . import github, kube, overlays, pipelines, platform
from .runner import Ctx, NeedsUser, Step, StepFailed

NS = "ai-scan-repos"
TF_DIR = Path(os.environ.get("INSTALLER_TERRAFORM", "/opt/installer/terraform/eks"))
DATA = Path(os.environ.get("INSTALLER_DATA", "/data"))


# --- helpers -------------------------------------------------------------------

def secret_values(a: dict[str, Any]) -> list[str]:
    """Every value the log must never show."""
    t, ai, gen = a.get("target", {}), a.get("ai", {}), a.get("generated", {})
    vals = [a.get("repos", {}).get("token", ""), ai.get("api_key", ""), ai.get("ts_client_secret", ""),
            a.get("oauth", {}).get("client_secret", ""),
            t.get("ssh", {}).get("sudo_password", ""), t.get("eks", {}).get("aws_secret_access_key", "")]
    vals += [v for v in gen.values() if isinstance(v, str)]
    key = t.get("ssh", {}).get("private_key", "")
    vals += [line for line in key.splitlines() if len(line) > 20]
    return [v for v in vals if v]


def ensure_generated(a: dict[str, Any]) -> None:
    """Generated once and kept in the answers, so an upgrade keeps the same
    database password and the same key the saved AI key is encrypted with."""
    g = a.setdefault("generated", {})
    g.setdefault("postgres_password", secrets.token_urlsafe(24).replace("-", "a").replace("_", "b"))
    g.setdefault("settings_key", secrets.token_hex(32))
    g.setdefault("webhook_secret", secrets.token_hex(32))
    g.setdefault("jenkins_password", secrets.token_urlsafe(18))
    g.setdefault("argocd_password", "")


def k(ctx: Ctx) -> list[str]:
    return ["kubectl", "--kubeconfig", ctx.state["kubeconfig"]]


def helm(ctx: Ctx) -> list[str]:
    return ["helm", "--kubeconfig", ctx.state["kubeconfig"]]


def apply_text(ctx: Ctx, text: str) -> None:
    ctx.sh(k(ctx) + ["apply", "-f", "-"], input=text)


def secret_apply(ctx: Ctx, ns: str, name: str, data: dict[str, str], labels: dict[str, str] | None = None) -> None:
    """Create or update a Secret from values passed on stdin (never on a command line)."""
    import base64
    obj = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": ns,
           "labels": labels or {}}, "type": "Opaque",
           "data": {kk: base64.b64encode(v.encode()).decode() for kk, v in data.items()}}
    ctx.log(f"Secret {ns}/{name}: {', '.join(data)}")
    # server-side apply: client-side apply would copy the values into an annotation
    ctx.sh(k(ctx) + ["apply", "--server-side", "--force-conflicts", "--field-manager=ai-scanner-installer",
                     "-f", "-"], input=json.dumps(obj), quiet=True)


def ensure_ns(ctx: Ctx, ns: str, labels: dict[str, str] | None = None) -> None:
    obj = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": labels or {}}}
    ctx.sh(k(ctx) + ["apply", "-f", "-"], input=json.dumps(obj))


def ssh_run(ctx: Ctx, command: str, root: bool = False, check: bool = True) -> str:
    ssh = ctx.a["target"]["ssh"]
    stdin = None
    if root:
        command, stdin = kube.sudo_cmd(ssh, command)
    return ctx.sh(kube.ssh_base(ssh, Path(ctx.state["ssh_key"])) + [command], input=stdin, check=check)


def aws_env(a: dict[str, Any]) -> dict[str, str]:
    e = a["target"]["eks"]
    env = {"AWS_REGION": e["region"], "AWS_DEFAULT_REGION": e["region"]}
    if e.get("aws_access_key_id"):
        env.update(AWS_ACCESS_KEY_ID=e["aws_access_key_id"], AWS_SECRET_ACCESS_KEY=e["aws_secret_access_key"])
    elif e.get("aws_profile"):
        env["AWS_PROFILE"] = e["aws_profile"]
    return env


def wait_until(ctx: Ctx, what: str, check: Callable[[], Any], timeout: int, every: int = 10) -> Any:
    end = time.time() + timeout
    while time.time() < end:
        try:
            got = check()
            if got:
                return got
        except (subprocess.SubprocessError, StepFailed, httpx.HTTPError, KeyError, ValueError):
            pass
        time.sleep(every)
    raise StepFailed(f"Timed out waiting for {what}.")


def env_name(a: dict[str, Any]) -> str:
    return "eks" if a["target"]["kind"] == "eks" else "local"


# --- steps: connect ------------------------------------------------------------

def s_connect(ctx: Ctx) -> None:
    t = ctx.a["target"]
    kc = Path(ctx.workdir) / "kubeconfig"
    if t["kind"] == "local":
        server = kube.local_kubeconfig(t["context"], kc)
    elif t["kind"] == "ssh":
        ctx.state["ssh_key"] = str(kube.write_ssh_key(t["ssh"], Path(ctx.workdir)))
        server = kube.ssh_kubeconfig(t["ssh"], Path(ctx.state["ssh_key"]), kc)
    else:
        server = "(EKS)"
    ctx.state["kubeconfig"] = str(kc)
    ctx.log(f"Cluster API: {server}")
    info = kube.flavour(k(ctx), ctx.env)
    ctx.state["flavour"] = info
    ctx.log(f"Cluster: {info['kind']}, {info['nodes']} node(s), Kubernetes {info['version']}, "
            f"node IP {info['node_ip']}, runtime {info['runtime']}")
    check_capacity(ctx, info)


MIN_MEMORY_GIB = 7.5     # app + Jenkins + Argo CD + one build Pod at a time


def check_capacity(ctx: Ctx, info: dict[str, Any]) -> None:
    """Jenkins builds need room: below ~8 GB the API server itself stops answering."""
    gib = info.get("memory_gib") or 0
    if not gib or gib >= MIN_MEMORY_GIB:
        return
    cmd = ("minikube start -p ai-scan --cpus 6 --memory 10g --container-runtime=docker"
           if info["kind"] == "minikube" else "")
    ctx.wait_for_user(
        f"This cluster has {gib:.1f} GB of memory; the app with Jenkins and Argo CD needs at least 8 GB, "
        "or the builds stall. For minikube, create a second, larger cluster (your current one stays as it is), "
        "restart the installer and choose the context \"ai-scan\". Continue only to try anyway.", command=cmd)


def s_terraform(ctx: Ctx) -> None:
    e = ctx.a["target"]["eks"]
    work = DATA / "terraform"
    work.mkdir(parents=True, exist_ok=True)
    for f in TF_DIR.glob("*.tf"):          # always this image's version; the state stays
        shutil.copy(f, work / f.name)
    tfvars = {"name": e.get("name") or "ai-scan-repos", "region": e["region"],
              "node_type": e.get("node_type") or "t3.large", "node_count": int(e.get("node_count") or 2),
              "domain": e.get("domain") or ""}
    (work / "installer.auto.tfvars.json").write_text(json.dumps(tfvars), encoding="utf-8")
    env = aws_env(ctx.a)
    ctx.sh(["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"], env=env)
    ctx.sh(["terraform", "init", "-input=false", "-no-color"], cwd=str(work), env=env)
    ctx.log("Creating the VPC, EKS, EFS and ECR. This usually takes 15 to 20 minutes.")
    ctx.sh(["terraform", "apply", "-auto-approve", "-input=false", "-no-color"], cwd=str(work), env=env)
    out = json.loads(ctx.sh(["terraform", "output", "-json"], cwd=str(work), env=env, quiet=True))
    ctx.state["tf"] = {kk: v["value"] for kk, v in out.items()}
    kc = Path(ctx.workdir) / "kubeconfig"
    ctx.sh(["aws", "eks", "update-kubeconfig", "--name", ctx.state["tf"]["cluster_name"],
            "--region", e["region"], "--kubeconfig", str(kc)], env=env)
    ctx.env.update(env)            # kubectl's exec plugin (aws eks get-token) needs the credentials
    ctx.state["kubeconfig"] = str(kc)
    ctx.state["flavour"] = kube.flavour(k(ctx), ctx.env)
    if tfvars["domain"]:
        recs = ctx.state["tf"].get("acm_validation") or []
        if recs:
            r = recs[0]
            ctx.wait_for_user(
                "Add this DNS record at your domain's DNS host so AWS can issue the HTTPS certificate. "
                "Continue once it is saved; the certificate is issued a few minutes later.",
                info={"Type": r["type"], "Name": r["name"], "Value": r["value"]})
        if ctx.state["tf"].get("acm_certificate_arn"):
            ctx.log("Waiting for AWS to issue the certificate (needs the DNS record above).")
            ctx.sh(["aws", "acm", "wait", "certificate-validated", "--certificate-arn",
                    ctx.state["tf"]["acm_certificate_arn"], "--region", e["region"]], env=env, timeout=45 * 60)


def s_storage_eks(ctx: Ctx) -> None:
    apply_text(ctx, platform.gp3_storageclass())
    # gp2 is no longer the default once gp3 is
    ctx.sh(k(ctx) + ["annotate", "storageclass", "gp2", "storageclass.kubernetes.io/is-default-class-",
                     "--overwrite"], check=False)


# --- steps: registry and ingress -----------------------------------------------

def registry_addr(ctx: Ctx) -> str:
    f = ctx.state["flavour"]
    if ctx.a["target"]["kind"] == "eks":
        return ctx.state["tf"]["ecr_registry"]
    if f["kind"] == "minikube":
        return f"{platform.REGISTRY_CLUSTER_IP}:5000"
    return f"{f['node_ip']}:30500"


def s_registry(ctx: Ctx) -> None:
    f = ctx.state["flavour"]
    addr = registry_addr(ctx)
    ctx.state["registry"] = addr
    if ctx.a["target"]["kind"] == "eks":
        ctx.log(f"Images go to ECR: {addr}")
        return
    apply_text(ctx, platform.registry("clusterip" if f["kind"] == "minikube" else "nodeport"))
    ctx.sh(k(ctx) + ["-n", "registry", "rollout", "status", "deploy/registry", "--timeout=300s"])
    if f["kind"] == "minikube":
        if not f.get("runtime", "").startswith("docker"):
            ctx.wait_for_user(
                f"This minikube uses {f.get('runtime') or 'another runtime'}, which will not pull from the "
                "in-cluster registry over plain HTTP. Recreate it with the Docker runtime, then continue.",
                command="minikube delete && minikube start --container-runtime=docker --cpus 4 --memory 8g")
            ctx.state["flavour"] = f = kube.flavour(k(ctx), ctx.env)
            if not f.get("runtime", "").startswith("docker"):
                raise StepFailed("minikube still does not use the Docker runtime.")
            apply_text(ctx, platform.registry("clusterip"))
            ctx.sh(k(ctx) + ["-n", "registry", "rollout", "status", "deploy/registry", "--timeout=300s"])
        ctx.log(f"minikube trusts {addr} as a plain-HTTP registry (its service range is insecure by default).")
        return
    conf = platform.k3s_registries(addr)
    if f["kind"] == "k3s" and ctx.a["target"]["kind"] == "ssh":
        current = ssh_run(ctx, "cat /etc/rancher/k3s/registries.yaml 2>/dev/null || true", root=True, check=False)
        if addr in current:
            ctx.log("K3s already pulls from the registry over HTTP.")
            return
        ssh_run(ctx, f"mkdir -p /etc/rancher/k3s && printf '%s' '{conf}' > /etc/rancher/k3s/registries.yaml"
                     " && systemctl restart k3s", root=True)
        wait_until(ctx, "K3s to come back", lambda: ctx.sh(k(ctx) + ["get", "nodes"], quiet=True), 180, 5)
        return
    cmd = (f"sudo mkdir -p /etc/rancher/k3s && printf '%s' '{conf}' | sudo tee /etc/rancher/k3s/registries.yaml"
           " && sudo systemctl restart k3s") if f["kind"] == "k3s" else \
          f"# Configure every node's container runtime to allow the plain-HTTP registry {addr}"
    ctx.wait_for_user(f"The cluster's nodes must be allowed to pull from {addr} over plain HTTP. "
                      "Run this on the server, then continue.", command=cmd)


def s_ingress(ctx: Ctx) -> None:
    f = ctx.state["flavour"]
    if f["kind"] == "k3s":
        ctx.state["ingress_class"] = "traefik"
        ctx.log("Using K3s's built-in Traefik.")
        return
    eks = ctx.a["target"]["kind"] == "eks"
    if not eks and ctx.sh(k(ctx) + ["get", "ingressclass", "nginx", "-o", "name"], check=False, quiet=True).strip()             == "ingressclass.networking.k8s.io/nginx":
        # e.g. `minikube addons enable ingress`: installing a second controller would clash with it
        ctx.state["ingress_class"] = "nginx"
        ctx.log("Using the cluster's existing nginx ingress controller.")
        return
    cert = (ctx.state.get("tf") or {}).get("acm_certificate_arn", "")
    ctx.sh(helm(ctx) + ["repo", "add", "ingress-nginx", "https://kubernetes.github.io/ingress-nginx", "--force-update"])
    ctx.sh(helm(ctx) + ["upgrade", "--install", "ingress-nginx", "ingress-nginx/ingress-nginx",
                        "-n", "ingress-nginx", "--create-namespace", "--version", platform.INGRESS_NGINX_CHART,
                        "-f", "-", "--timeout", "10m"],
           input=platform.ingress_nginx_values(eks, cert))
    # helm --wait would also wait for the LoadBalancer's address, which minikube only
    # hands out while `minikube tunnel` runs (asked for in the next step)
    ctx.sh(k(ctx) + ["-n", "ingress-nginx", "rollout", "status", "deploy/ingress-nginx-controller", "--timeout=600s"])
    ctx.state["ingress_class"] = "nginx"


def lb_address(ctx: Ctx, ns: str, svc: str) -> str:
    out = ctx.sh(k(ctx) + ["-n", ns, "get", "svc", svc, "-o",
                           "jsonpath={.status.loadBalancer.ingress[0].hostname}{.status.loadBalancer.ingress[0].ip}"],
                 quiet=True).strip()
    return out


def s_address(ctx: Ctx) -> None:
    a, f = ctx.a, ctx.state["flavour"]
    if a.get("public_url"):
        url = a["public_url"].rstrip("/")
    elif a["target"]["kind"] == "eks":
        if a["target"]["eks"].get("domain"):
            url = "https://" + a["target"]["eks"]["domain"]
        else:
            host = wait_until(ctx, "the load balancer's address",
                              lambda: lb_address(ctx, "ingress-nginx", "ingress-nginx-controller"), 600)
            url = "http://" + host
    elif f["kind"] == "minikube":
        url = "http://localhost"
        ctx.wait_for_user("minikube needs a tunnel so your browser can reach the app on http://localhost. "
                          "Run this in a terminal on your computer and leave it open (it may ask for your "
                          "password), then continue.",
                          command="minikube tunnel" + ("" if a["target"].get("context") in (None, "minikube")
                                                       else f" -p {a['target']['context']}"))
    elif f["kind"] == "k3s":
        url = "http://" + (a["target"]["ssh"]["host"] if a["target"]["kind"] == "ssh" else f["node_ip"])
    else:
        host = lb_address(ctx, "ingress-nginx", "ingress-nginx-controller")
        url = "http://" + (host if host else f["node_ip"])
        if host in ("localhost", "127.0.0.1"):
            url = "http://localhost"
    ctx.state["public_url"] = url
    ctx.log(f"The app's address: {url}")
    if a["target"]["kind"] == "eks" and a["target"]["eks"].get("domain"):
        host = wait_until(ctx, "the load balancer's address",
                          lambda: lb_address(ctx, "ingress-nginx", "ingress-nginx-controller"), 600)
        ctx.wait_for_user(f"Point your domain at the load balancer with this DNS record, then continue.",
                          info={"Type": "CNAME", "Name": a["target"]["eks"]["domain"], "Value": host})


def s_oauth(ctx: Ctx) -> None:
    url = ctx.state["public_url"]
    o = ctx.a.setdefault("oauth", {})
    if o.get("client_id") and o.get("client_secret") and o.get("for_url") == url:
        ctx.log("Using the saved GitHub OAuth App.")
        return
    got = ctx.wait_for_user(
        "Create a GitHub OAuth App (GitHub, Settings, Developer settings, OAuth Apps, New OAuth App) "
        "with these two URLs, then generate a client secret and paste both values here.",
        command="https://github.com/settings/applications/new",
        info={"Application name": "AI Scan Repos", "Homepage URL": url,
              "Authorization callback URL": f"{url}/auth/github/callback"},
        fields=[{"name": "client_id", "label": "Client ID", "secret": False},
                {"name": "client_secret", "label": "Client secret", "secret": True}])
    if not got.get("client_id") or not got.get("client_secret"):
        raise StepFailed("The OAuth App's client ID and secret are both needed.")
    o.update(client_id=got["client_id"].strip(), client_secret=got["client_secret"].strip(), for_url=url)
    ctx.state["answers_changed"] = True


# --- steps: model --------------------------------------------------------------

def s_tailscale(ctx: Ctx) -> None:
    ai = ctx.a["ai"]
    if ai["mode"] != "tailnet":
        ctx.log("No tailnet model chosen; skipped.")
        return
    ensure_ns(ctx, "tailscale")
    secret_apply(ctx, "tailscale", "operator-oauth",
                 {"client_id": ai["ts_client_id"], "client_secret": ai["ts_client_secret"]})
    ctx.sh(helm(ctx) + ["repo", "add", "tailscale", "https://pkgs.tailscale.com/helmcharts", "--force-update"])
    values = Path(ctx.state["repos_dir"]) / "ai-scanner-backend-deployment" / "tailscale-operator" / "values.yaml"
    args = ["-f", str(values)] if values.exists() else []
    ctx.sh(helm(ctx) + ["upgrade", "--install", "tailscale-operator", "tailscale/tailscale-operator",
                        "-n", "tailscale", "--version", platform.TAILSCALE_CHART, *args, "--wait", "--timeout", "10m"])


# --- steps: repos and overlays ---------------------------------------------------

def installer_repo(a: dict[str, Any]) -> str:
    """owner/name of THIS repository, where each install keeps its manifests."""
    return a["repos"].get("installer_repo") or github.default_installer_repo()


def env_branch(a: dict[str, Any]) -> str:
    return f"env/{env_name(a)}"


def s_clone(ctx: Ctx) -> None:
    """Read the three deployment repositories (never written to), and check out
    this install's branch of the installer repository (created from main)."""
    owner, token = ctx.a["repos"]["owner"], ctx.a["repos"]["token"]
    root = Path(ctx.workdir) / "repos"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    env = github.askpass(Path(ctx.workdir), token)
    ctx.state["git_env"] = env
    for r in github.DEPLOY_REPOS:
        ctx.sh(["git", "clone", "--quiet", "--depth", "1", github.clone_url(owner, r), str(root / r)], env=env)
    inst, branch = installer_repo(ctx.a), env_branch(ctx.a)
    d = root / "installer"
    ctx.sh(["git", "clone", "--quiet", github.clone_url(*inst.split("/", 1)), str(d)], env=env)
    has = ctx.sh(["git", "-C", str(d), "ls-remote", "--heads", "origin", branch], env=env, quiet=True).strip()
    if has:
        ctx.sh(["git", "-C", str(d), "checkout", "--quiet", branch])
    else:
        ctx.sh(["git", "-C", str(d), "checkout", "--quiet", "-b", branch])
        ctx.log(f"New branch {branch} in {inst} for this install.")
    ctx.state["repos_dir"] = str(root)
    ctx.state["installer_dir"] = str(d)


def _storage(ctx: Ctx) -> dict[str, Any]:
    f = ctx.state["flavour"]
    if ctx.a["target"]["kind"] == "eks":
        return {"provisioner": "efs.csi.aws.com", "rwx": True, "noexec": False,
                "parameters": {"provisioningMode": "efs-ap", "fileSystemId": ctx.state["tf"]["efs_id"],
                               "directoryPerms": "770", "uid": "10001", "gid": "10001",
                               "basePath": "/reports"}}
    if f["kind"] == "k3s":
        return {"provisioner": "rancher.io/local-path", "noexec": True}
    # any other cluster: copy its default StorageClass
    scs = json.loads(ctx.sh(k(ctx) + ["get", "storageclass", "-o", "json"], quiet=True))["items"]
    default = next((s for s in scs if s["metadata"].get("annotations", {}).get(
        "storageclass.kubernetes.io/is-default-class") == "true"), scs[0] if scs else None)
    if default is None:
        raise StepFailed("The cluster has no StorageClass, so volumes cannot be created.")
    return {"provisioner": default["provisioner"], "parameters": default.get("parameters"), "noexec": False,
            # e.g. minikube's hostpath provisioner cannot handle WaitForFirstConsumer
            "binding": default.get("volumeBindingMode", "Immediate")}


def _model_url(ctx: Ctx) -> str:
    ai = ctx.a["ai"]
    return ai.get("endpoint", "") if ai["mode"] == "same_machine" else ""


# what is copied from each deployment repository into base/
BASE_SKIP = shutil.ignore_patterns(".git", ".github", "overlays", "platform", "job-test", "tailscale-operator",
                                   "*.md", ".gitignore", ".gitattributes")
APP_OF = {"ai-scanner-backend-deployment": "backend", "ai-scanner-gateway-deployment": "gateway",
          "ai-scanner-frontend-deployment": "frontend"}


def s_overlays(ctx: Ctx) -> None:
    """Write environments/<env>/<app>/ (base copy + overlay) and the derived
    Jenkinsfiles to this install's branch of the installer repository."""
    a, env = ctx.a, env_name(ctx.a)
    root, inst = Path(ctx.state["repos_dir"]), Path(ctx.state["installer_dir"])
    branch = env_branch(a)
    if ctx.sh(["git", "-C", str(inst), "ls-remote", "--heads", "origin", branch], env=ctx.state["git_env"],
              quiet=True).strip():    # Jenkins may have pushed new tags meanwhile
        ctx.sh(["git", "-C", str(inst), "pull", "--quiet", "--rebase", "origin", branch], env=ctx.state["git_env"])
    out = inst / "environments" / env
    reg, ai = ctx.state["registry"], a["ai"]

    def keep_tag(app: str, image: str) -> str:
        """An upgrade keeps the tag Jenkins already wrote."""
        p = out / app / "app-patch.yaml"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if f"/{image}:" in line:
                    return line.rsplit(":", 1)[1].strip()
        return "bootstrap"

    files = {
        "backend": overlays.backend(dict(
            registry=reg, tag=keep_tag("backend", "review-api"), worker_tag=keep_tag("backend", "scanner-worker"),
            ai_mode=ai["mode"], model_url=_model_url(ctx), tailnet_ip=ai.get("tailnet_ip", ""),
            tailnet_port=ai.get("tailnet_port") or 1234, ingress_class=ctx.state["ingress_class"],
            public_url=ctx.state["public_url"], storage=_storage(ctx))),
        "gateway": overlays.gateway(dict(
            registry=reg, tag=keep_tag("gateway", "review-gateway"),
            public_url=ctx.state["public_url"], admins=a["admins"])),
        "frontend": overlays.frontend(dict(registry=reg, tag=keep_tag("frontend", "review-frontend"))),
    }
    for repo, app in APP_OF.items():
        d = out / app
        shutil.rmtree(d / "base", ignore_errors=True)
        shutil.copytree(root / repo, d / "base", ignore=BASE_SKIP)
        for name, text in files[app].items():
            (d / name).write_text(text, encoding="utf-8")
        ctx.sh(["kubectl", "kustomize", str(d)], quiet=True)    # fails here, not in Argo CD, if broken
    ci = out / "ci"
    ci.mkdir(parents=True, exist_ok=True)
    owner, token = a["repos"]["owner"], a["repos"]["token"]
    for repo in github.CODE_REPOS:
        jf = github.file_text(token, owner, repo, "Jenkinsfile")
        pod = github.file_text(token, owner, repo, "ci/pod.yaml")
        if not jf or not pod:
            raise StepFailed(f"Could not read Jenkinsfile and ci/pod.yaml from {owner}/{repo} (main).")
        try:
            (ci / f"{repo}.Jenkinsfile").write_text(pipelines.derive(repo, jf, env), encoding="utf-8")
        except pipelines.PipelineError as exc:
            raise StepFailed(f"{repo}/Jenkinsfile changed shape: {exc}. Update installer/pipelines.py.") from exc
        (ci / f"{repo}.pod.yaml").write_text(pod, encoding="utf-8")
    sh_git = ["git", "-C", str(inst)]
    ctx.sh(sh_git + ["add", f"environments/{env}"])
    if ctx.sh(sh_git + ["status", "--porcelain"], quiet=True).strip():
        ctx.sh(sh_git + ["-c", "user.name=ai-scanner-installer", "-c", "user.email=installer@ai-scan-repos.local",
                         "commit", "--quiet", "-m", f"installer: environments/{env} for {ctx.state['public_url']}"])
    else:
        ctx.log(f"environments/{env} unchanged.")
    ctx.sh(sh_git + ["push", "--quiet", "origin", f"HEAD:{branch}"], env=ctx.state["git_env"])


# --- steps: app secrets ----------------------------------------------------------

def s_app_secrets(ctx: Ctx) -> None:
    a, g = ctx.a, ctx.a["generated"]
    ensure_ns(ctx, NS, {"pod-security.kubernetes.io/enforce": "restricted",
                        "pod-security.kubernetes.io/enforce-version": "latest"})
    # A database (and saved, encrypted AI keys) may outlive the answers file: keep
    # the values the cluster already uses, or Postgres refuses the new password.
    import base64
    for secret, key, name in (("postgres-credentials", "POSTGRES_PASSWORD", "postgres_password"),
                              ("gateway-secrets", "settings-key", "settings_key")):
        live = ctx.sh(k(ctx) + ["-n", NS, "get", "secret", secret, "-o", f"jsonpath={{.data.{key}}}"],
                      check=False, quiet=True).strip()
        if live and not live.startswith("Error"):
            value = base64.b64decode(live).decode()
            if value != g[name]:
                g[name] = value
                ctx.run.secrets.append(value)
                ctx.state["answers_changed"] = True
                ctx.log(f"Keeping the cluster's existing {secret} value ({key}).")
    secret_apply(ctx, NS, "postgres-credentials", {
        "POSTGRES_USER": "ai_scan_repos", "POSTGRES_PASSWORD": g["postgres_password"],
        "POSTGRES_DB": "ai_scan_repos"})
    data = {
        "database-url": "postgresql+psycopg://ai_scan_repos:"
                        f"{urllib.parse.quote(g['postgres_password'], safe='')}@postgres:5432/ai_scan_repos",
        "github-oauth-client-id": a["oauth"]["client_id"],
        "github-oauth-client-secret": a["oauth"]["client_secret"],
        "github-webhook-secret": g["webhook_secret"],
        "settings-key": g["settings_key"],
    }
    secret_apply(ctx, NS, "gateway-secrets", data)
    # a running gateway reads Secrets only at start
    ctx.sh(k(ctx) + ["-n", NS, "rollout", "restart", "deploy/gateway"], check=False, quiet=True)


# --- steps: Jenkins, smee, webhooks ------------------------------------------------

def s_jenkins(ctx: Ctx) -> None:
    a, eks = ctx.a, ctx.a["target"]["kind"] == "eks"
    ensure_ns(ctx, "jenkins")
    secret_apply(ctx, "jenkins", "jenkins-admin", {"jenkins-admin-user": "admin",
                                                    "jenkins-admin-password": a["generated"]["jenkins_password"]})
    login = a["repos"].get("login") or "x-access-token"
    secret_apply(ctx, "jenkins", "jenkins-github", {"username": login, "token": a["repos"]["token"]})
    if eks:
        wait_until(ctx, "the jenkins default ServiceAccount",
                   lambda: ctx.sh(k(ctx) + ["-n", "jenkins", "get", "serviceaccount", "default"], quiet=True), 60, 2)
        ctx.sh(k(ctx) + ["-n", "jenkins", "annotate", "serviceaccount", "default", "--overwrite",
                         f"eks.amazonaws.com/role-arn={ctx.state['tf']['jenkins_role_arn']}"])
    ctx.sh(helm(ctx) + ["repo", "add", "jenkins", "https://charts.jenkins.io", "--force-update"])

    def install(url: str) -> None:
        values = platform.jenkins_values(dict(
            registry_kind="ecr" if eks else "local", registry=ctx.state["registry"],
            owner=a["repos"]["owner"], env=env_name(a), jenkins_url=url,
            installer_repo=installer_repo(a), branch=env_branch(a),
            aws_region=a["target"]["eks"]["region"] if eks else "",
            service_type="LoadBalancer" if eks else "ClusterIP"))
        ctx.sh(helm(ctx) + ["upgrade", "--install", "jenkins", "jenkins/jenkins", "-n", "jenkins",
                            "--version", platform.JENKINS_CHART, "-f", "-", "--wait", "--timeout", "15m"],
               input=values)

    install("http://jenkins.jenkins.svc.cluster.local:8080/")
    if eks:
        host = wait_until(ctx, "Jenkins's load balancer", lambda: lb_address(ctx, "jenkins", "jenkins"), 600)
        ctx.state["jenkins_url"] = f"http://{host}:8080/"
        install(ctx.state["jenkins_url"])     # so build links and the webhook use the public address


def s_webhooks(ctx: Ctx) -> None:
    a = ctx.a
    if a["target"]["kind"] == "eks":
        target = ctx.state["jenkins_url"] + "github-webhook/"
    else:
        g = a["generated"]
        if not g.get("smee_url"):
            g["smee_url"] = github.new_smee_channel()
            ctx.state["answers_changed"] = True
        ctx.run.secrets.append(g["smee_url"])     # anyone with the URL could read the webhook payloads
        target = g["smee_url"]
        secret_apply(ctx, "jenkins", "smee-channel", {"url": target})
        apply_text(ctx, platform.smee_client())
    ids = a["generated"].setdefault("webhook_ids", {})
    for repo in github.CODE_REPOS:
        hook = github.ensure_webhook(a["repos"]["token"], a["repos"]["owner"], repo, target, ids.get(repo))
        ids[repo] = hook
        ctx.log(f"Webhook on {a['repos']['owner']}/{repo}: #{hook} -> {target}")
    a["generated"]["webhook_url"] = target
    ctx.state["answers_changed"] = True
    ctx.state["webhook_url"] = target


# --- steps: Argo CD ---------------------------------------------------------------

def s_argocd(ctx: Ctx) -> None:
    a = ctx.a
    ensure_ns(ctx, "argocd")
    ctx.sh(helm(ctx) + ["repo", "add", "argo", "https://argoproj.github.io/argo-helm", "--force-update"])
    ctx.sh(helm(ctx) + ["upgrade", "--install", "argocd", "argo/argo-cd", "-n", "argocd",
                        "--version", platform.ARGOCD_CHART, "-f", "-", "--wait", "--timeout", "10m"],
           input=platform.argocd_values())
    inst = installer_repo(a)
    secret_apply(ctx, "argocd", "repo-ai-scanner-installer", {
        "type": "git", "url": f"https://github.com/{inst}.git",
        "username": a["repos"].get("login") or "x-access-token", "password": a["repos"]["token"]},
        labels={"argocd.argoproj.io/secret-type": "repository"})
    for app in ("backend", "gateway", "frontend"):
        apply_text(ctx, platform.argo_app(f"ai-scan-{app}", inst, env_branch(a), f"environments/{env_name(a)}/{app}",
                                          lmstudio=app == "backend"))


# --- steps: first build and health -------------------------------------------------

class PortForward:
    """kubectl port-forward that can be restarted: on a busy cluster it drops."""

    def __init__(self, ctx: Ctx, ns: str, svc: str, port: int, local: int):
        self.args = k(ctx) + ["-n", ns, "port-forward", f"svc/{svc}", f"{local}:{port}"]
        self.env = ctx.env
        self.url = f"http://127.0.0.1:{local}"
        self.p: subprocess.Popen | None = None
        self.restart()

    def restart(self) -> None:
        if self.p:
            self.p.terminate()
        self.p = subprocess.Popen(self.args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env)
        time.sleep(3)

    def __enter__(self) -> "PortForward":
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.p:
            self.p.terminate()


def s_first_build(ctx: Ctx) -> None:
    """Build the three images ONE AT A TIME (each build Pod runs tests, BuildKit
    and Trivy; three at once starve a small cluster), and keep waiting through
    dropped connections: a busy API server is not a failed build."""
    pw = ctx.a["generated"]["jenkins_password"]
    with PortForward(ctx, "jenkins", "jenkins", 8080, 18080) as pf:
        j = httpx.Client(base_url=pf.url, auth=("admin", pw), timeout=30)

        def call(method: str, path: str, **kw: Any) -> httpx.Response:
            for attempt in range(6):
                try:
                    return j.request(method, path, **kw)
                except httpx.HTTPError:
                    ctx.log("Jenkins did not answer; reconnecting.", "warn")
                    time.sleep(10 * (attempt + 1))
                    pf.restart()
            raise StepFailed("Lost the connection to Jenkins. The cluster may be short of memory: "
                             "check `kubectl top nodes` and give it at least 8 GB.")

        crumb = wait_until(ctx, "Jenkins to answer", lambda: call("GET", "/crumbIssuer/api/json").json(), 600, 5)
        hdr = {crumb["crumbRequestField"]: crumb["crumb"]}
        for job in github.CODE_REPOS:
            wait_until(ctx, f"the Jenkins job {job}", lambda: call("GET", f"/job/{job}/api/json").status_code == 200, 300, 5)
            last = call("GET", f"/job/{job}/lastBuild/api/json")
            before = last.json().get("number", 0) if last.status_code == 200 else 0
            r = call("POST", f"/job/{job}/build?delay=0", headers=hdr)
            if r.status_code >= 400:
                raise StepFailed(f"Jenkins refused to start {job} (HTTP {r.status_code}).")
            ctx.log(f"Jenkins: building {job} (test, build, scan, push). This takes a while.")
            end = time.time() + 60 * 60
            while time.time() < end:
                time.sleep(20)
                r = call("GET", f"/job/{job}/lastBuild/api/json")
                if r.status_code != 200:
                    continue
                b = r.json()
                if b.get("number", 0) <= before or b.get("building") or not b.get("result"):
                    continue
                if b["result"] != "SUCCESS":
                    raise StepFailed(f"The {job} pipeline finished with {b['result']}. "
                                     f"Open Jenkins to see why (build #{b['number']}).")
                ctx.log(f"{job}: build #{b['number']} succeeded.")
                break
            else:
                raise StepFailed(f"The {job} pipeline did not finish in 60 minutes.")
        j.close()


def s_db_password(ctx: Ctx) -> None:
    """Postgres sets its password only when its volume is first created. A volume
    that survived an earlier install (minikube's hostpath keeps the folder after
    the claim is deleted) still has the old one, and the gateway cannot log in.
    Set the password the Secret holds, through the trusted local socket inside the
    Pod; the SQL goes in on stdin, never on a command line."""
    # Argo CD creates it from git a little after the Applications exist
    wait_until(ctx, "Argo CD to create the database",
               lambda: ctx.sh(k(ctx) + ["-n", NS, "get", "statefulset", "postgres", "-o", "name"], quiet=True) is not None,
               600, 10)   # ctx.sh raises while it does not exist
    ctx.sh(k(ctx) + ["-n", NS, "rollout", "status", "statefulset/postgres", "--timeout=900s"])
    pw = ctx.a["generated"]["postgres_password"].replace("'", "''")
    ctx.sh(k(ctx) + ["-n", NS, "exec", "-i", "postgres-0", "--", "psql", "-q", "-U", "ai_scan_repos",
                     "-d", "ai_scan_repos", "-v", "ON_ERROR_STOP=1", "-f", "-"],
           input=f"ALTER USER ai_scan_repos PASSWORD '{pw}';\n", quiet=True)
    ctx.log("Database password matches the gateway's Secret.")
    ctx.sh(k(ctx) + ["-n", NS, "rollout", "restart", "deploy/gateway"], check=False, quiet=True)


def s_healthy(ctx: Ctx) -> None:
    def synced() -> bool:
        apps = json.loads(ctx.sh(k(ctx) + ["-n", "argocd", "get", "applications", "-o", "json"], quiet=True))["items"]
        states = {x["metadata"]["name"]: (x.get("status", {}).get("sync", {}).get("status"),
                                          x.get("status", {}).get("health", {}).get("status")) for x in apps}
        ctx.log("Argo CD: " + ", ".join(f"{n} {s}/{h}" for n, (s, h) in sorted(states.items())))
        return all(s == "Synced" and h == "Healthy" for s, h in states.values()) and len(states) == 3
    # refresh now instead of waiting for Argo CD's 3-minute poll
    for app in ("ai-scan-backend", "ai-scan-gateway", "ai-scan-frontend"):
        ctx.sh(k(ctx) + ["-n", "argocd", "annotate", "application", app, "argocd.argoproj.io/refresh=hard",
                         "--overwrite"], quiet=True, check=False)
    wait_until(ctx, "Argo CD to deploy the app", synced, 20 * 60, 20)
    out = ctx.sh(k(ctx) + ["-n", NS, "exec", "deploy/gateway", "--", "python", "-c",
                           "import urllib.request as u;print(u.urlopen('http://127.0.0.1:8080/health',timeout=10).read().decode())"])
    ctx.log("Gateway health: " + out.strip())


DEFAULT_AI = r"""
import json, sys
from review_gateway import site_settings
from review_gateway.db import get_session
v = json.loads(sys.stdin.read())
with get_session() as db:
    if site_settings.ai_public(db).get("configured"):
        print("kept: an admin already chose", site_settings.ai_public(db).get("preset"))
    else:
        site_settings.save_ai(db, preset=v["preset"], endpoint=v["endpoint"], model=v["model"],
                              api_key=v["api_key"] or None, clear_key=False, who="installer")
        print("saved:", v["preset"], v["model"] or "(default model)")
"""


def s_default_ai(ctx: Ctx) -> None:
    """Save the chosen default AI service through the running gateway's own
    settings code (same encryption as Settings), unless an admin already chose one.
    The script and the key go in on stdin, never on a command line."""
    ai = ctx.a["ai"]
    preset = {"same_machine": "local", "tailnet": "local", "cloud": ai.get("preset", "")}[ai["mode"]]
    endpoint = {"same_machine": ai.get("endpoint", ""),
                "tailnet": f"http://lmstudio:{ai.get('tailnet_port') or 1234}/v1", "cloud": ""}[ai["mode"]]
    payload = json.dumps({"preset": preset, "endpoint": endpoint, "model": ai.get("model", ""),
                          "api_key": ai.get("api_key", "") if ai["mode"] == "cloud" else ""})
    script = DEFAULT_AI.replace("sys.stdin.read()", repr(payload))
    out = ctx.sh(k(ctx) + ["-n", NS, "exec", "-i", "deploy/gateway", "--", "python", "-"], input=script, quiet=True)
    ctx.log("Default AI model " + out.strip().splitlines()[-1])


def s_result(ctx: Ctx) -> None:
    g = ctx.a["generated"]
    argo_pw = ctx.sh(k(ctx) + ["-n", "argocd", "get", "secret", "argocd-initial-admin-secret", "-o",
                               "jsonpath={.data.password}"], quiet=True, check=False).strip()
    import base64
    if argo_pw:
        g["argocd_password"] = base64.b64decode(argo_pw).decode()
        ctx.run.secrets.append(g["argocd_password"])
        ctx.state["answers_changed"] = True
    ctx.state["result"] = {
        "app_url": ctx.state["public_url"],
        "jenkins_url": ctx.state.get("jenkins_url", ""),
        "webhook": ctx.state.get("webhook_url", ""),
        "registry": ctx.state["registry"],
        "commands": {
            "Jenkins": "kubectl -n jenkins port-forward svc/jenkins 8080:8080",
            "Argo CD": "kubectl -n argocd port-forward svc/argocd-server 8081:80",
        },
        # shown on the wizard's last screen (localhost only); also in the encrypted answers
        "passwords": {"Jenkins": g["jenkins_password"], **({"Argo CD": g["argocd_password"]} if g.get("argocd_password") else {})},
    }


# --- destroy -----------------------------------------------------------------------

def s_destroy_cluster_side(ctx: Ctx) -> None:
    a = ctx.a
    for app in ("ai-scan-backend", "ai-scan-gateway", "ai-scan-frontend"):
        ctx.sh(k(ctx) + ["-n", "argocd", "delete", "application", app, "--ignore-not-found", "--wait=false"], check=False)
    # the app first, while the Tailscale operator still runs to remove its finalizer on lmstudio
    ctx.sh(k(ctx) + ["delete", "namespace", NS, "--ignore-not-found", "--timeout=10m"], check=False)
    for rel, ns in (("argocd", "argocd"), ("jenkins", "jenkins"), ("tailscale-operator", "tailscale"),
                    ("ingress-nginx", "ingress-nginx")):
        ctx.sh(helm(ctx) + ["uninstall", rel, "-n", ns, "--wait"], check=False)
    for ns in ("jenkins", "argocd", "registry", "tailscale", "ingress-nginx"):
        ctx.sh(k(ctx) + ["delete", "namespace", ns, "--ignore-not-found", "--timeout=10m"], check=False)
    ctx.sh(k(ctx) + ["delete", "storageclass", "reports-noexec", "--ignore-not-found"], check=False)
    g = a.get("generated", {})
    for repo in github.CODE_REPOS:
        hook = (g.get("webhook_ids") or {}).get(repo)
        how = github.delete_webhook(a["repos"]["token"], a["repos"]["owner"], repo, hook, g.get("webhook_url", ""))
        ctx.log(f"Webhook on {repo}: {how}")


def s_destroy_connect_eks(ctx: Ctx) -> None:
    e = ctx.a["target"]["eks"]
    env = aws_env(ctx.a)
    ctx.env.update(env)
    kc = Path(ctx.workdir) / "kubeconfig"
    r = ctx.sh(["aws", "eks", "update-kubeconfig", "--name", e.get("name") or "ai-scan-repos",
                "--region", e["region"], "--kubeconfig", str(kc)], env=env, check=False)
    ctx.state["kubeconfig"] = str(kc)
    ctx.state["cluster_gone"] = "ResourceNotFoundException" in r


def s_terraform_destroy(ctx: Ctx) -> None:
    work = DATA / "terraform"
    if not (work / "terraform.tfstate").exists():
        ctx.log("No Terraform state on this volume; nothing to destroy in AWS.", "warn")
        return
    env = aws_env(ctx.a)
    ctx.sh(["terraform", "init", "-input=false", "-no-color"], cwd=str(work), env=env)
    ctx.sh(["terraform", "destroy", "-auto-approve", "-input=false", "-no-color"], cwd=str(work), env=env)


# --- plans ---------------------------------------------------------------------------

def install_plan(a: dict[str, Any]) -> list[Step]:
    eks = a["target"]["kind"] == "eks"
    steps = [Step("terraform", "Create AWS resources with Terraform", s_terraform),
             Step("storage", "Make gp3 the default storage", s_storage_eks)] if eks else \
            [Step("connect", "Connect to the cluster", s_connect)]
    steps += [
        Step("clone", "Read the deployment repositories", s_clone),
        Step("registry", "Set up the image registry", s_registry),
        Step("ingress", "Set up the ingress controller", s_ingress),
        Step("address", "Find the app's address", s_address),
        Step("oauth", "Connect GitHub login", s_oauth),
        Step("tailscale", "Connect the tailnet model", s_tailscale),
        Step("overlays", "Write this environment's overlays", s_overlays),
        Step("secrets", "Create the app's secrets", s_app_secrets),
        Step("jenkins", "Install Jenkins and its pipelines", s_jenkins),
        Step("webhooks", "Connect GitHub webhooks", s_webhooks),
        Step("argocd", "Install Argo CD and the app", s_argocd),
        Step("database", "Match the database password", s_db_password),
        Step("build", "Build the images (first pipeline run)", s_first_build),
        Step("healthy", "Wait for the app to be healthy", s_healthy),
        Step("ai", "Save the default AI model", s_default_ai),
        Step("result", "Collect addresses and logins", s_result),
    ]
    return steps


def destroy_plan(a: dict[str, Any]) -> list[Step]:
    if a["target"]["kind"] == "eks":
        return [Step("connect", "Connect to EKS", s_destroy_connect_eks),
                Step("cluster", "Remove the app, Jenkins, Argo CD and load balancers", s_destroy_cluster_side),
                Step("terraform", "Delete the AWS resources (terraform destroy)", s_terraform_destroy)]
    return [Step("connect", "Connect to the cluster", s_connect),
            Step("cluster", "Remove the app, Jenkins, Argo CD and the registry", s_destroy_cluster_side)]


def detect_existing(a: dict[str, Any], workdir: str) -> dict[str, Any]:
    """Is the app already installed on this target? Used to offer Upgrade or Destroy."""
    if a.get("target", {}).get("kind") == "eks":
        return {"installed": (DATA / "terraform" / "terraform.tfstate").exists()}
    try:
        from .runner import Run
        ctx = Ctx(Run(kind="detect", steps=[]), a, workdir)
        s_connect(ctx)
        out = subprocess.run(k(ctx) + ["get", "namespace", NS, "-o", "name"], capture_output=True, text=True)
        return {"installed": out.returncode == 0, "flavour": ctx.state["flavour"]}
    except Exception as exc:  # noqa: BLE001
        return {"installed": False, "error": cluster_error(a, str(exc))}


def cluster_error(a: dict[str, Any], raw: str) -> str:
    """kubectl's wall of retries, as one sentence the user can act on."""
    name = a.get("target", {}).get("context") or a.get("target", {}).get("ssh", {}).get("host") or "the cluster"
    if "connection refused" in raw or "No connection could be made" in raw:
        hint = f" Start it (minikube: minikube start -p {name}) or choose another cluster." \
            if a.get("target", {}).get("kind") == "local" else ""
        return f"{name} is not running: nothing answers at its address.{hint}"
    if "TLS handshake timeout" in raw or "i/o timeout" in raw or "context deadline exceeded" in raw:
        return f"{name} answers too slowly. It may be out of memory; give it at least 8 GB."
    if "Unauthorized" in raw or "forbidden" in raw.lower():
        return f"{name} refused the kubeconfig's credentials."
    last = [ln for ln in raw.strip().splitlines() if ln.strip()]
    return (last[-1] if last else raw)[:300]

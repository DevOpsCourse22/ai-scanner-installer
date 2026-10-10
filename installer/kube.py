"""Getting a working kubeconfig inside the installer container, and finding
out what kind of cluster it points at.

Three ways in:
  local  the user's ~/.kube/config, mounted read-only at /host/kube/config.
         Addresses that mean "this computer" (127.0.0.1, localhost,
         kubernetes.docker.internal) are rewritten to host.docker.internal,
         keeping the original name for the TLS check. Certificate FILE paths
         (Windows or Unix, e.g. minikube's) are read from the mounted
         ~/.minikube and embedded, so the config is self-contained.
  ssh    a server reached over SSH: its K3s or kubeadm admin config is read
         with sudo, and its 127.0.0.1 address replaced by the SSH host.
  eks    written by `aws eks update-kubeconfig` after Terraform.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

HOST_KUBE = Path(os.environ.get("HOST_KUBECONFIG", "/host/kube/config"))
HOST_MINIKUBE = Path(os.environ.get("HOST_MINIKUBE", "/host/minikube"))
LOCAL_NAMES = {"127.0.0.1", "localhost", "kubernetes.docker.internal", "0.0.0.0"}
HOST_ALIAS = os.environ.get("HOST_ALIAS", "host.docker.internal")


def _kubeconfigs() -> list[tuple[Path, dict[str, Any]]]:
    """Every kubeconfig in the mounted ~/.kube: `config` first, then any other
    file that holds contexts (KUBECONFIG often points at one of those, e.g.
    ~/.kube/k3s-exercise/Kube.yml, and minikube writes its contexts there)."""
    root = HOST_KUBE.parent
    files = [HOST_KUBE] if HOST_KUBE.is_file() else []
    if root.is_dir():
        for f in sorted(root.rglob("*")):
            if f.is_file() and f != HOST_KUBE and "cache" not in f.parts and f.stat().st_size < 1_000_000 \
                    and (f.suffix in (".yml", ".yaml", ".conf", "") or f.name == "config"):
                files.append(f)
    out = []
    for f in files:
        try:
            cfg = yaml.safe_load(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if isinstance(cfg, dict) and cfg.get("contexts"):
            out.append((f, cfg))
    return out


def contexts() -> list[dict[str, str]]:
    """The contexts in the mounted kubeconfigs, for the wizard's picker."""
    out, seen = [], set()
    for f, cfg in _kubeconfigs():
        clusters = {c["name"]: c.get("cluster", {}) for c in cfg.get("clusters", [])}
        for c in cfg.get("contexts", []):
            if c["name"] in seen:
                continue
            seen.add(c["name"])
            cl = clusters.get(c.get("context", {}).get("cluster", ""), {})
            out.append({"name": c["name"], "server": cl.get("server", ""),
                        "current": c["name"] == cfg.get("current-context"),
                        "file": str(f.relative_to(HOST_KUBE.parent))})
    return out


def _host_file(p: str) -> Path | None:
    """Map a certificate path from the host (C:\\Users\\x\\.minikube\\ca.crt,
    /home/x/.minikube/ca.crt) to the mounted copy."""
    norm = p.replace("\\", "/")
    m = re.search(r"/\.minikube/(.*)$", norm)
    if m:
        return HOST_MINIKUBE / m.group(1)
    m = re.search(r"/\.kube/(.*)$", norm)
    if m:
        return HOST_KUBE.parent / m.group(1)
    return Path(norm) if Path(norm).is_file() else None


def _embed(obj: dict[str, Any], key: str) -> None:
    if key in obj:
        f = _host_file(obj.pop(key))
        if f is None or not f.is_file():
            raise FileNotFoundError(
                f"The kubeconfig names {key} that is not mounted. Start the installer with "
                "the ~/.minikube folder mounted (see the README's docker run line).")
        obj[key + "-data"] = base64.b64encode(f.read_bytes()).decode("ascii")


def local_kubeconfig(context: str, out: Path) -> str:
    """Write a self-contained kubeconfig for one context. Returns the server URL."""
    cfg = next((c for _, c in _kubeconfigs() if any(x["name"] == context for x in c["contexts"])), None)
    if cfg is None:
        raise RuntimeError(f"No kubeconfig in the mounted ~/.kube has the context {context!r}.")
    ctx = next(c for c in cfg["contexts"] if c["name"] == context)
    cl = next(c for c in cfg["clusters"] if c["name"] == ctx["context"]["cluster"])
    us = next(u for u in cfg["users"] if u["name"] == ctx["context"]["user"])
    cluster, user = dict(cl["cluster"]), dict(us["user"])
    _embed(cluster, "certificate-authority")
    _embed(user, "client-certificate")
    _embed(user, "client-key")
    url = urlparse(cluster["server"])
    if url.hostname in LOCAL_NAMES and _resolves(context):
        # a minikube profile, with the installer started on its Docker network
        # (--network <profile>): talk to the node directly, port 8443 inside that network
        cluster["tls-server-name"] = "localhost"
        cluster["server"] = f"https://{context}:8443"
    elif url.hostname in LOCAL_NAMES:
        if url.hostname != "0.0.0.0":
            cluster.setdefault("tls-server-name", url.hostname)
        cluster["server"] = f"{url.scheme}://{HOST_ALIAS}:{url.port or 443}"
    new = {"apiVersion": "v1", "kind": "Config", "current-context": context,
           "clusters": [{"name": cl["name"], "cluster": cluster}],
           "users": [{"name": us["name"], "user": user}],
           "contexts": [{"name": context, "context": ctx["context"]}]}
    _write(out, new)
    return cluster["server"]


def _resolves(name: str) -> bool:
    import socket
    try:
        socket.gethostbyname(name)
        return True
    except OSError:
        return False


def _write(out: Path, cfg: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    os.chmod(out, 0o600)


def ssh_base(ssh: dict[str, Any], keyfile: Path) -> list[str]:
    return ["ssh", "-i", str(keyfile), "-p", str(ssh.get("port") or 22),
            "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
            "-o", f"UserKnownHostsFile={keyfile.parent / 'known_hosts'}",
            f"{ssh['user']}@{ssh['host']}"]


def write_ssh_key(ssh: dict[str, Any], workdir: Path) -> Path:
    key = workdir / "ssh" / "id"
    key.parent.mkdir(parents=True, exist_ok=True)
    text = ssh["private_key"].replace("\r\n", "\n").strip() + "\n"
    key.write_text(text, encoding="utf-8")
    os.chmod(key, 0o600)
    return key


def sudo_cmd(ssh: dict[str, Any], command: str) -> tuple[str, str | None]:
    """A remote command run as root, and the stdin it needs (the sudo password)."""
    if ssh.get("sudo_password"):
        return f"sudo -S -p '' sh -c {shlex.quote(command)}", ssh["sudo_password"] + "\n"
    return f"sudo -n sh -c {shlex.quote(command)}", None


def ssh_kubeconfig(ssh: dict[str, Any], keyfile: Path, out: Path) -> str:
    """Read the server's admin kubeconfig over SSH. Returns the server URL."""
    cmd, stdin = sudo_cmd(ssh, "cat /etc/rancher/k3s/k3s.yaml 2>/dev/null || cat /etc/kubernetes/admin.conf")
    text = subprocess.run(ssh_base(ssh, keyfile) + [cmd], input=stdin, capture_output=True,
                          text=True, timeout=60)
    if text.returncode != 0 or "clusters" not in text.stdout:
        raise RuntimeError("Could not read the cluster's kubeconfig over SSH: "
                           + (text.stderr.strip().splitlines() or ["no output"])[-1])
    cfg = yaml.safe_load(text.stdout)
    for c in cfg["clusters"]:
        url = urlparse(c["cluster"]["server"])
        if url.hostname in LOCAL_NAMES:
            c["cluster"]["server"] = f"{url.scheme}://{ssh['host']}:{url.port or 6443}"
            c["cluster"].setdefault("tls-server-name", "127.0.0.1" if url.hostname != "localhost" else "localhost")
    _write(out, cfg)
    return cfg["clusters"][0]["cluster"]["server"]


def flavour(kubectl: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    """k3s | minikube | eks | docker-desktop | other, plus the first node's IP.
    env: on EKS kubectl's exec plugin (aws eks get-token) needs the AWS credentials."""
    out = subprocess.run(kubectl + ["get", "nodes", "-o", "json"], capture_output=True,
                         text=True, timeout=60, env=env)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "kubectl get nodes failed")
    import json
    nodes = json.loads(out.stdout)["items"]
    n0 = nodes[0]
    labels = n0["metadata"].get("labels", {})
    version = n0["status"]["nodeInfo"]["kubeletVersion"]
    runtime = n0["status"]["nodeInfo"]["containerRuntimeVersion"]
    ip = next((a["address"] for a in n0["status"]["addresses"] if a["type"] == "InternalIP"), "")
    if "+k3s" in version:
        kind = "k3s"
    elif "minikube.k8s.io/name" in labels:
        kind = "minikube"
    elif "eks.amazonaws.com/nodegroup" in labels or "-eks-" in version:
        kind = "eks"
    elif n0["metadata"]["name"] == "docker-desktop":
        kind = "docker-desktop"
    else:
        kind = "other"
    return {"kind": kind, "node_ip": ip, "nodes": len(nodes), "version": version,
            "runtime": runtime, "arch": labels.get("kubernetes.io/arch", ""),
            "memory_gib": sum(_gib(n["status"].get("allocatable", {}).get("memory", "0")) for n in nodes)}


def _gib(q: str) -> float:
    """A Kubernetes memory quantity (e.g. 16007352Ki, 8Gi) in GiB."""
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 1e3, "M": 1e6, "G": 1e9}
    for u in sorted(units, key=len, reverse=True):
        if q.endswith(u):
            return float(q[: -len(u)]) * units[u] / 2**30
    return float(q or 0) / 2**30

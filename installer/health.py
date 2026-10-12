"""Is the remembered install actually up?

The answers file remembers the last finished deploy, and the Terraform state
remembers what was created on AWS. Both can outlive the real thing (a cluster
deleted by hand, minikube recreated, an AWS account cleaned up), and then the
installer would keep showing an install that is gone. This looks at the
cluster itself and says one of:

    up           every app Deployment ready, the app answers on its address
    degraded     the cluster has the app, but some parts are not ready
    gone         the cluster answers, but the app's namespace does not exist
    unreachable  the cluster itself does not answer (stopped, deleted, no access)
    none         nothing is remembered and nothing was found

For anything but "up" the wizard suggests Remove (cleans what is left,
including AWS) or Forget (only clears the installer's own record).
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from urllib.parse import urlparse

import httpx

NS = "ai-scan-repos"
APP = ["postgres", "review-api", "gateway", "frontend"]


def _kubectl(kc: str, env: dict[str, str], *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "--kubeconfig", kc, *args], capture_output=True, text=True,
                          env=env, timeout=timeout)


def _probe(url: str) -> tuple[bool, str]:
    """GET <app>/health from inside the installer container."""
    if not url:
        return False, "no address remembered"
    u = urlparse(url)
    host = "host.docker.internal" if u.hostname in ("localhost", "127.0.0.1") else u.hostname
    target = f"{u.scheme}://{host}{':' + str(u.port) if u.port else ''}/health"
    try:
        r = httpx.get(target, timeout=8, verify=False, headers={"Host": u.netloc})
        return r.status_code == 200, f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        return False, type(exc).__name__


def check(a: dict[str, Any], workdir: str) -> dict[str, Any]:
    from . import plans, services
    last = a.get("last_result") or {}
    eks_state = a.get("target", {}).get("kind") == "eks" and plans.tf_has_resources()
    remembered = bool(last) or eks_state
    out: dict[str, Any] = {"remembered": remembered, "parts": [], "app_url": last.get("app_url", "")}
    if not a.get("target", {}).get("kind"):
        return {**out, "status": "none" if not remembered else "unreachable",
                "message": "No cluster is chosen in the answers."}
    try:
        kc, env = services.connect(a, workdir)
    except Exception as exc:  # noqa: BLE001
        return {**out, "status": "unreachable", "message": plans.cluster_error(a, str(exc))}
    try:
        ns = _kubectl(kc, env, "get", "namespace", NS, "-o", "name")
    except subprocess.TimeoutExpired:
        return {**out, "status": "unreachable", "message": "The cluster answers too slowly."}
    if ns.returncode != 0:
        if "NotFound" in ns.stderr or "not found" in ns.stderr:
            return {**out, "status": "gone" if remembered else "none",
                    "message": "The cluster is there, but the app is not installed on it."}
        return {**out, "status": "unreachable", "message": plans.cluster_error(a, ns.stderr)}
    parts = []
    objs = json.loads(_kubectl(kc, env, "-n", NS, "get", "deploy,statefulset", "-o", "json").stdout or "{}")
    by_name = {o["metadata"]["name"]: o for o in objs.get("items", [])}
    for name in APP:
        o = by_name.get(name)
        want = (o or {}).get("spec", {}).get("replicas", 1)
        ready = (o or {}).get("status", {}).get("readyReplicas", 0) or 0
        parts.append({"name": name, "ready": bool(o) and ready >= want and want > 0,
                      "detail": "missing" if not o else f"{ready}/{want} ready"})
    ok_http, why = _probe(last.get("app_url", ""))
    parts.append({"name": "app address", "ready": ok_http, "detail": why})
    out["parts"] = parts
    ready = [p for p in parts if p["ready"]]
    if len(ready) == len(parts):
        status, msg = "up", "Everything is running."
    elif all(p["ready"] for p in parts[:len(APP)]):
        # the pods are fine; only the browser-facing address did not answer (tunnel, DNS, LB)
        status, msg = "degraded", "The app runs, but its address does not answer from here (tunnel, DNS or load balancer)."
    elif ready:
        status, msg = "degraded", "Some parts are not ready: " + ", ".join(p["name"] for p in parts if not p["ready"]) + "."
    else:
        status, msg = "gone", "Nothing of the app is running."
    return {**out, "status": status, "message": msg}

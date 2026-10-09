"""The per-environment Kustomize overlays the installer writes into the three
deployment repositories, at overlays/<env>/ (env = "local" or "eks").

Each overlay builds on base/, a copy of that repository, so the
repository stays exactly what K3S-SRV deploys today. What an overlay changes:

  - the image of each Deployment, set by a patch (not by `images:`): the
    root already renames the image to its own registry, so an `images:`
    entry here could not match it. Jenkins edits these `image:` lines when
    DEPLOY_ENV is set (see the Jenkinsfiles), with the same sed it already
    uses for SCANNER_WORKER_IMAGE.
  - the public address, admins and default AI model (gateway),
  - where the model is reached (review-api, the lmstudio Service),
  - the Ingress class and the reports StorageClass for this cluster.

The image tag starts as "bootstrap"; the first Jenkins run replaces it.
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.parse import urlparse

import yaml

HEAD = "# Written by ai-scanner-installer for this environment. Jenkins edits the image lines.\n"


def _kustomization(resources: list[str], files: list[str], extra: str = "") -> str:
    res = "".join(f"  - {r}\n" for r in resources)
    patches = "".join(f"  - path: {f}\n" for f in files)
    return (HEAD + "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
            "resources:\n" + res + "patches:\n" + patches + extra)


# Each overlay builds on base/, a copy of the deployment repository it
# replaces (written next to it by the installer), so the shared *-deployment
# repositories are never changed.
BASE = ["base"]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def backend(o: dict[str, Any]) -> dict[str, str]:
    """o: registry, tag, worker_tag, ai_mode, model_url, tailnet_ip, tailnet_port,
    ingress_class, storage (dict: provisioner, parameters, rwx, size)."""
    files = {}
    model_url = {"tailnet": f"http://lmstudio:{o.get('tailnet_port') or 1234}/v1",
                 "same_machine": o.get("model_url") or "",
                 "cloud": "http://lmstudio:1234/v1"}[o["ai_mode"]]
    files["app-patch.yaml"] = f"""{HEAD}apiVersion: apps/v1
kind: Deployment
metadata:
  name: review-api
  namespace: ai-scan-repos
spec:
  template:
    spec:
      containers:
        - name: review-api
          image: {o['registry']}/review-api:{o['tag']}
          env:
            - name: SCANNER_WORKER_IMAGE
              value: {o['registry']}/scanner-worker:{o['worker_tag']}
            - name: LMSTUDIO_BASE_URL
              value: {model_url}
"""
    tailnet = o["ai_mode"] == "tailnet"
    if tailnet:
        # JSON 6902: replace the port list outright (a merge would keep 1234 next to a new port)
        port = int(o.get("tailnet_port") or 1234)
        files["lmstudio-patch.yaml"] = HEAD + yaml.safe_dump([
            {"op": "replace", "path": "/metadata/annotations/tailscale.com~1tailnet-ip", "value": o["tailnet_ip"]},
            {"op": "replace", "path": "/spec/ports", "value": [
                {"name": "http", "port": port, "protocol": "TCP", "targetPort": port}]},
        ], sort_keys=False)
    else:
        # No tailnet: the Tailscale egress Service would never resolve. Drop it.
        files["lmstudio-patch.yaml"] = f"""{HEAD}$patch: delete
apiVersion: v1
kind: Service
metadata:
  name: lmstudio
  namespace: ai-scan-repos
"""
    # JSON 6902: the class, and the app's host name when it has one. A host makes the
    # rules specific, so another app's catch-all Ingress on the same controller
    # (seen on a shared minikube) does not clash with ours. An IP cannot be a host.
    ing = [{"op": "replace", "path": "/spec/ingressClassName", "value": o["ingress_class"]}]
    host = urlparse(o.get("public_url") or "").hostname or ""
    if host and not _is_ip(host):
        ing.append({"op": "add", "path": "/spec/rules/0/host", "value": host})
    files["ingress-patch.yaml"] = HEAD + yaml.safe_dump(ing, sort_keys=False)
    st = o["storage"]
    # JSON 6902 ops: a StorageClass has no list keys for a merge patch to use
    ops = [{"op": "replace", "path": "/provisioner", "value": st["provisioner"]}]
    if st.get("parameters"):
        ops.append({"op": "add", "path": "/parameters", "value": st["parameters"]})
    if not st.get("noexec"):
        ops.append({"op": "remove", "path": "/mountOptions"})
    files["storage-patch.yaml"] = HEAD + yaml.safe_dump(ops, sort_keys=False)
    files["pvc-patch.yaml"] = f"""{HEAD}apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ai-scan-repos-reports
  namespace: ai-scan-repos
spec:
  accessModes:
    - {'ReadWriteMany' if st.get('rwx') else 'ReadWriteOnce'}
"""
    files["kustomization.yaml"] = _kustomization(
        BASE, ["app-patch.yaml", "pvc-patch.yaml"]
        + ([] if tailnet else ["lmstudio-patch.yaml"]),
        "  - path: storage-patch.yaml\n    target:\n      kind: StorageClass\n"
        "      name: reports-noexec\n"
        "  - path: ingress-patch.yaml\n    target:\n      kind: Ingress\n      name: ai-scan-repos\n"
        + ("  - path: lmstudio-patch.yaml\n    target:\n      kind: Service\n      name: lmstudio\n"
           if tailnet else ""))
    return files


def gateway(o: dict[str, Any]) -> dict[str, str]:
    """o: registry, tag, public_url, admins (list)."""
    url = o["public_url"].rstrip("/")
    admins = "".join(f"    {a}\n" for a in o["admins"])
    return {
        "app-patch.yaml": f"""{HEAD}apiVersion: apps/v1
kind: Deployment
metadata:
  name: gateway
  namespace: ai-scan-repos
spec:
  template:
    spec:
      containers:
        - name: gateway
          image: {o['registry']}/review-gateway:{o['tag']}
          env:
            - name: GATEWAY_FRONTEND_URL
              value: {url}
            - name: GATEWAY_CORS_ORIGINS
              value: {url}
            - name: GITHUB_OAUTH_REDIRECT_URI
              value: {url}/auth/github/callback
            - name: GATEWAY_COOKIE_SECURE
              value: "{'true' if url.startswith('https://') else 'false'}"
""",
        "admins-patch.yaml": f"""{HEAD}apiVersion: v1
kind: ConfigMap
metadata:
  name: gateway-admins
  namespace: ai-scan-repos
data:
  admins: |
{admins}""",
        "kustomization.yaml": _kustomization(BASE, ["app-patch.yaml", "admins-patch.yaml"]),
    }


def frontend(o: dict[str, Any]) -> dict[str, str]:
    return {
        "app-patch.yaml": f"""{HEAD}apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
  namespace: ai-scan-repos
spec:
  template:
    spec:
      containers:
        - name: frontend
          image: {o['registry']}/review-frontend:{o['tag']}
""",
        "kustomization.yaml": _kustomization(BASE, ["app-patch.yaml"]),
    }

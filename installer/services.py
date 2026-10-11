"""Jenkins, Argo CD and Grafana, opened from the installer.

None of the three is exposed publicly. The installer keeps a kubectl
port-forward to each one running inside its container, on a fixed port the
documented `docker run` publishes on 127.0.0.1 only:

    Jenkins   http://localhost:8081
    Argo CD   http://localhost:8082
    Grafana   http://localhost:8083

The same works for every target (minikube, a server over SSH, EKS), because
it only needs the kubeconfig the installer already builds. All three log in
as `admin` with ONE password, generated per install and kept in the
encrypted answers.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

SERVICES = [
    # name, namespace, Service, its port, local port
    ("Jenkins", "jenkins", "jenkins", 8080, 8081),
    ("Argo CD", "argocd", "argocd-server", 80, 8082),
    ("Grafana", "monitoring", "monitoring-grafana", 80, 8083),
]
HOST_PORTS_NOTE = "-p 127.0.0.1:8081-8083:8081-8083"

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "error": "", "kubeconfig": "", "procs": {}, "env": {}}


def status() -> dict[str, Any]:
    out = []
    for name, ns, svc, port, local in SERVICES:
        p = _state["procs"].get(name)
        out.append({"name": name, "url": f"http://localhost:{local}",
                    "up": bool(p and p.poll() is None)})
    return {"running": _state["running"], "error": _state["error"], "services": out,
            "publish": HOST_PORTS_NOTE}


def _keep(name: str, ns: str, svc: str, port: int, local: int) -> None:
    """One port-forward, restarted whenever it dies (pod restart, idle drop)."""
    while _state["running"]:
        p = subprocess.Popen(["kubectl", "--kubeconfig", _state["kubeconfig"], "-n", ns, "port-forward",
                              "--address", "0.0.0.0", f"svc/{svc}", f"{local}:{port}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_state["env"])
        _state["procs"][name] = p
        while _state["running"] and p.poll() is None:
            time.sleep(2)
        if p.poll() is None:
            p.terminate()
        time.sleep(3)


def start(kubeconfig: str, env: dict[str, str]) -> dict[str, Any]:
    with _lock:
        stop()
        _state.update(running=True, error="", kubeconfig=kubeconfig, env=env)
        for svc in SERVICES:
            threading.Thread(target=_keep, args=svc, daemon=True).start()
    time.sleep(3)
    return status()


def stop() -> None:
    _state["running"] = False
    for p in list(_state["procs"].values()):
        if p.poll() is None:
            p.terminate()
    _state["procs"].clear()


def connect(a: dict[str, Any], workdir: str) -> tuple[str, dict[str, str]]:
    """A kubeconfig for the install's cluster (built the same way a run does)."""
    from . import plans
    from .runner import Ctx, Run
    ctx = Ctx(Run(kind="services", steps=[]), a, workdir)
    if a["target"]["kind"] == "eks":
        plans.s_destroy_connect_eks(ctx)          # aws eks update-kubeconfig with the install's credentials
        if ctx.state.get("cluster_gone"):
            raise RuntimeError("The EKS cluster does not exist (any more).")
    else:
        plans.s_connect(ctx)
    return ctx.state["kubeconfig"], dict(ctx.env)


def kubeconfig_path(workdir: str) -> str:
    p = Path(workdir) / "kubeconfig"
    return str(p) if p.is_file() else ""


def env_for(a: dict[str, Any]) -> dict[str, str]:
    from . import plans
    env = dict(os.environ)
    if a.get("target", {}).get("kind") == "eks":
        env.update(plans.aws_env(a))
    return env

"""The installer's HTTP API and the wizard page.

Run inside the installer image; published only on 127.0.0.1 by the
documented `docker run -p 127.0.0.1:8088:8088`, so nothing here is reachable
from the network. Secrets typed in the wizard stay in this process's memory
and in the encrypted answers file; GET /api/answers never returns them.
"""

from __future__ import annotations

import asyncio
import configparser
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import github, kube, plans, runner, vault

app = FastAPI(title="ai-scanner-installer")
UI = Path(os.environ.get("INSTALLER_UI", Path(__file__).resolve().parent.parent / "ui" / "dist"))
WORKDIR = tempfile.mkdtemp(prefix="installer-")

# Paths to secret values inside the answers (dotted). Masked on the way out,
# kept when the wizard sends them back empty.
SECRET_PATHS = ["repos.token", "ai.api_key", "ai.ts_client_secret", "oauth.client_secret",
                "target.ssh.private_key", "target.ssh.sudo_password", "target.eks.aws_secret_access_key"]

_session: dict[str, Any] = {"answers": None, "passphrase": None}


def _get(d: dict[str, Any], path: str) -> Any:
    for p in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(p)  # type: ignore[assignment]
    return d


def _set(d: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def masked(a: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(a)
    out.pop("generated", None)
    for p in SECRET_PATHS:
        if _get(out, p):
            _set(out, p, "")
            _set(out, p + "_saved", True)
    return out


def _answers() -> dict[str, Any]:
    if _session["answers"] is None:
        raise HTTPException(409, "Unlock or start the answers first.")
    return _session["answers"]


def _save() -> None:
    vault.save(_session["answers"], _session["passphrase"])


# --- state ---------------------------------------------------------------------

@app.get("/api/status")
def status() -> dict[str, Any]:
    run = runner.current()
    return {"vault_exists": vault.exists(), "unlocked": _session["answers"] is not None,
            "contexts": kube.contexts(), "aws_profiles": aws_profiles(),
            "minikube_mounted": kube.HOST_MINIKUBE.is_dir(),
            "default_owner": github.default_owner(),
            "run": run.public() if run else None}


def aws_profiles() -> list[str]:
    names = []
    for f, prefix in ((Path("/root/.aws/config"), "profile "), (Path("/root/.aws/credentials"), "")):
        if f.is_file():
            cp = configparser.ConfigParser()
            cp.read(f)
            names += [s.removeprefix(prefix) for s in cp.sections()]
    return sorted(set(names))


class Unlock(BaseModel):
    passphrase: str
    new: bool = False


@app.post("/api/unlock")
def unlock(body: Unlock) -> dict[str, Any]:
    if len(body.passphrase) < 8:
        raise HTTPException(400, "Use a passphrase of at least 8 characters.")
    if body.new or not vault.exists():
        keep: dict[str, Any] = {}
        if vault.exists():
            # Start over keeps what a live install still needs: the generated
            # passwords and the OAuth App (only if the passphrase opens the file).
            try:
                old = vault.load(body.passphrase)
                keep = {k: old[k] for k in ("generated", "oauth") if k in old}
            except vault.VaultLocked:
                keep = {}
        _session.update(answers={"repos": {"owner": github.default_owner()}, "admins": [], **keep},
                        passphrase=body.passphrase)
        _save()
    else:
        try:
            _session.update(answers=vault.load(body.passphrase), passphrase=body.passphrase)
        except vault.VaultLocked as exc:
            raise HTTPException(403, str(exc)) from exc
    return {"answers": masked(_session["answers"])}


@app.get("/api/answers")
def get_answers() -> dict[str, Any]:
    return {"answers": masked(_answers())}


class Answers(BaseModel):
    answers: dict[str, Any]


@app.put("/api/answers")
def put_answers(body: Answers) -> dict[str, Any]:
    cur = _answers()
    run = runner.current()
    if run and run.status in ("running", "waiting"):
        raise HTTPException(409, "A deploy is running; change answers when it ends.")
    new = copy.deepcopy(body.answers)
    new.pop("generated", None)
    for p in SECRET_PATHS:
        _set(new, p + "_saved", None)
        if not _get(new, p) and _get(cur, p):
            _set(new, p, _get(cur, p))
    _strip_none(new)
    new["generated"] = cur.get("generated", {})
    if cur.get("oauth") and "oauth" not in body.answers:
        new["oauth"] = cur["oauth"]
    _session["answers"] = new
    _save()
    return {"answers": masked(new)}


def _strip_none(d: dict[str, Any]) -> None:
    for kk in list(d):
        if d[kk] is None:
            del d[kk]
        elif isinstance(d[kk], dict):
            _strip_none(d[kk])


# --- checks ----------------------------------------------------------------------

class GithubCheck(BaseModel):
    owner: str
    token: str = ""


@app.post("/api/check/github")
def check_github(body: GithubCheck) -> dict[str, Any]:
    token = body.token or _get(_answers(), "repos.token") or ""
    if not token:
        raise HTTPException(400, "Paste a GitHub token first.")
    try:
        res = github.check(token, body.owner, _get(_answers(), "repos.installer_repo") or "")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"GitHub could not be reached: {exc}") from exc
    if res.get("login"):
        _answers().setdefault("repos", {})["login"] = res["login"]
        _save()
    return res


@app.get("/api/check/answers")
def check_answers() -> dict[str, Any]:
    return {"missing": validate(_answers())}


@app.post("/api/check/target")
def check_target() -> dict[str, Any]:
    missing = validate(_answers())
    if missing:
        return {"installed": False, "error": "Fill in " + ", ".join(missing) + " first."}
    return plans.detect_existing(_answers(), WORKDIR)


class ModelCheck(BaseModel):
    endpoint: str


@app.post("/api/check/model")
def check_model(body: ModelCheck) -> dict[str, Any]:
    """Best effort, from this container: the cluster may see a different network."""
    url = body.endpoint.rstrip("/") + "/models"
    url = url.replace("host.minikube.internal", "host.docker.internal")
    try:
        r = httpx.get(url, timeout=8)
        ids = [m.get("id") for m in r.json().get("data", [])]
        return {"ok": r.status_code == 200, "models": ids}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


# --- runs ------------------------------------------------------------------------

class RunBody(BaseModel):
    kind: str            # install | upgrade | destroy
    confirm: str = ""


@app.post("/api/run")
def start_run(body: RunBody) -> dict[str, Any]:
    a = _answers()
    missing = validate(a)
    if body.kind in ("install", "upgrade") and missing:
        raise HTTPException(400, "Missing: " + "; ".join(missing))
    if body.kind == "destroy" and body.confirm != "destroy":
        raise HTTPException(400, 'Type "destroy" to confirm.')
    plans.ensure_generated(a)
    _save()
    steps = plans.destroy_plan(a) if body.kind == "destroy" else plans.install_plan(a)
    try:
        run = runner.start(body.kind, steps, a, plans.secret_values(a), WORKDIR)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    _watch_and_save(run)
    return run.public()


def _watch_and_save(run: runner.Run) -> None:
    """Values a run learns (OAuth App, smee channel, Argo CD password) go back
    into the encrypted answers when it ends or pauses."""
    import threading

    def loop() -> None:
        last = None
        while True:
            with run.cond:
                run.cond.wait(timeout=5)
            state = (run.status, len(run.lines))
            if state != last:
                last = state
                try:
                    _save()
                except Exception:  # noqa: BLE001
                    pass
            if run.status in ("done", "failed"):
                _save()
                return
    threading.Thread(target=loop, daemon=True).start()


def validate(a: dict[str, Any]) -> list[str]:
    miss = []
    t = a.get("target", {})
    if t.get("kind") == "local" and not t.get("context"):
        miss.append("which cluster (kube context)")
    if t.get("kind") == "ssh" and not all(_get(t, f"ssh.{x}") for x in ("host", "user", "private_key")):
        miss.append("SSH host, user and key")
    if t.get("kind") == "eks":
        e = t.get("eks", {})
        if not e.get("region"):
            miss.append("AWS region")
        if not (e.get("aws_profile") or (e.get("aws_access_key_id") and e.get("aws_secret_access_key"))):
            miss.append("AWS credentials")
    if t.get("kind") not in ("local", "ssh", "eks"):
        miss.append("where to install")
    if not _get(a, "repos.owner"):
        miss.append("GitHub owner")
    if not _get(a, "repos.token"):
        miss.append("GitHub token")
    for path, label in (("target.ssh.port", "SSH port"), ("ai.tailnet_port", "model port")):
        v = _get(a, path)
        if v is not None and not (isinstance(v, int) and 1 <= v <= 65535):
            miss.append(f"a valid {label} (1-65535)")
    n = _get(a, "target.eks.node_count")
    if t.get("kind") == "eks" and n is not None and not (isinstance(n, int) and 1 <= n <= 10):
        miss.append("1 to 10 nodes")
    ai = a.get("ai", {})
    if ai.get("mode") == "same_machine" and not ai.get("endpoint"):
        miss.append("the model server's address")
    if ai.get("mode") == "tailnet" and not all(ai.get(x) for x in ("tailnet_ip", "ts_client_id", "ts_client_secret")):
        miss.append("the tailnet model's IP and Tailscale OAuth client")
    if ai.get("mode") == "cloud" and not (ai.get("preset") and ai.get("api_key")):
        miss.append("the AI provider and its key")
    if ai.get("mode") not in ("same_machine", "tailnet", "cloud"):
        miss.append("the default AI model")
    if not a.get("admins"):
        miss.append("at least one admin")
    return miss


@app.get("/api/run")
def get_run() -> dict[str, Any]:
    run = runner.current()
    return run.public() if run else {}


@app.get("/api/run/events")
async def events(start: int = 0) -> StreamingResponse:
    run = runner.current()
    if run is None:
        raise HTTPException(404, "No run yet.")

    async def gen():
        i = start
        last_state = ""
        while True:
            lines = run.lines[i:]
            for line in lines:
                yield f"event: line\ndata: {json.dumps(line)}\n\n"
            i += len(lines)
            state = json.dumps(run.public())
            if state != last_state:
                last_state = state
                yield f"event: state\ndata: {state}\n\n"
            if run.status in ("done", "failed") and i >= len(run.lines):
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/run/cancel")
def run_cancel() -> dict[str, Any]:
    """Stops after the current step (or at once if it waits for the user)."""
    run = runner.current()
    if run is None or run.status not in ("running", "waiting"):
        raise HTTPException(409, "Nothing is running.")
    run.cancelled = True
    run.resume.set()
    run.log("Cancel requested: stopping after the current step.", "", "warn")
    return {"ok": True}


class Continue(BaseModel):
    values: dict[str, str] = {}


@app.post("/api/run/continue")
def run_continue(body: Continue) -> dict[str, Any]:
    run = runner.current()
    if run is None or run.status != "waiting":
        raise HTTPException(409, "Nothing is waiting.")
    run.reply = body.values
    run.resume.set()
    return {"ok": True}


# --- the wizard page ----------------------------------------------------------------

if (UI / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=UI / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str) -> FileResponse:
    f = UI / path
    if path and f.is_file() and UI in f.resolve().parents:
        return FileResponse(f)
    index = UI / "index.html"
    if not index.is_file():
        raise HTTPException(404, "The wizard page is not built (ui/dist).")
    return FileResponse(index)

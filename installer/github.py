"""GitHub: checking the token, webhooks, and the smee.io relay channel.

The token never appears in a URL or a command line: API calls send it in a
header, and git reads it through an askpass helper.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import httpx

API = "https://api.github.com"
CODE_REPOS = ["ai-scanner-backend", "ai-scanner-gateway", "ai-scanner-frontend"]
DEPLOY_REPOS = [r + "-deployment" for r in CODE_REPOS]


def _client(token: str) -> httpx.Client:
    return httpx.Client(base_url=API, timeout=20, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ai-scan-installer"})


def check(token: str, owner: str, installer_repo: str = "") -> dict[str, Any]:
    """Who the token belongs to, and what it may do: read the 6 project repos,
    add webhooks to the 3 code repos, push to the installer repo."""
    installer_repo = installer_repo or default_installer_repo()
    with _client(token) as gh:
        me = gh.get("/user")
        if me.status_code == 401:
            return {"ok": False, "error": "GitHub rejected this token."}
        repos = []
        for full in [f"{owner}/{n}" for n in CODE_REPOS + DEPLOY_REPOS] + [installer_repo]:
            name = full.split("/", 1)[1] if full != installer_repo else full
            r = gh.get(f"/repos/{full}")
            perms = r.json().get("permissions", {}) if r.status_code == 200 else {}
            repos.append({"name": name, "found": r.status_code == 200,
                          "private": r.json().get("private") if r.status_code == 200 else None,
                          "push": bool(perms.get("push")), "admin": bool(perms.get("admin"))})
    problems = [f"{x['name']}: not found (or the token cannot see it)" for x in repos if not x["found"]]
    problems += [f"{x['name']}: the token cannot push (each install writes its own branch here)"
                 for x in repos if x["found"] and x["name"] == installer_repo and not x["push"]]
    problems += [f"{x['name']}: the token cannot add webhooks (needs admin on the repo)" for x in repos
                 if x["found"] and x["name"] in CODE_REPOS and not x["admin"]]
    return {"ok": not problems, "login": me.json().get("login"), "repos": repos, "problems": problems}


def ensure_webhook(token: str, owner: str, repo: str, url: str, known_id: int | None = None) -> int:
    """Create the push webhook to `url`, or update THIS install's own hook
    (same URL, or the id saved from an earlier run). Hooks of other installs,
    such as K3S-SRV's smee channel, are never touched. Returns the hook id."""
    cfg = {"url": url, "content_type": "json", "insecure_ssl": "0"}
    body = {"config": cfg, "events": ["push", "pull_request"], "active": True}
    with _client(token) as gh:
        hooks = gh.get(f"/repos/{owner}/{repo}/hooks").json()
        for h in hooks if isinstance(hooks, list) else []:
            if h.get("config", {}).get("url") == url or (known_id and h.get("id") == known_id):
                r = gh.patch(f"/repos/{owner}/{repo}/hooks/{h['id']}", json=body)
                r.raise_for_status()
                return int(h["id"])
        r = gh.post(f"/repos/{owner}/{repo}/hooks", json={"name": "web", **body})
        r.raise_for_status()
        return int(r.json()["id"])


def delete_webhook(token: str, owner: str, repo: str, hook_id: int | None, url: str = "") -> str:
    """Remove this install's hook (by id, else by exact URL). Never raises."""
    try:
        with _client(token) as gh:
            if not hook_id and url:
                hooks = gh.get(f"/repos/{owner}/{repo}/hooks").json()
                hook_id = next((h["id"] for h in hooks if isinstance(hooks, list)
                                and h.get("config", {}).get("url") == url), None)
            if not hook_id:
                return "none found"
            r = gh.delete(f"/repos/{owner}/{repo}/hooks/{hook_id}")
            return "removed" if r.status_code in (204, 404) else f"GitHub answered {r.status_code}"
    except httpx.HTTPError as exc:
        return f"not removed ({exc})"


def file_text(token: str, owner: str, repo: str, path: str, ref: str = "main") -> str:
    """A file's text on a branch ("" if missing)."""
    with _client(token) as gh:
        r = gh.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref},
                   headers={"Accept": "application/vnd.github.raw+json"})
        return r.text if r.status_code == 200 else ""


def new_smee_channel() -> str:
    """smee.io answers /new with a redirect to a fresh channel URL."""
    r = httpx.head("https://smee.io/new", follow_redirects=False, timeout=20)
    loc = r.headers.get("location", "")
    if not loc.startswith("https://smee.io/"):
        raise RuntimeError("smee.io did not hand out a channel")
    return loc


def askpass(workdir: Path, token: str) -> dict[str, str]:
    """Env for git so it reads the token from a helper, never from the URL."""
    helper = workdir / "askpass.sh"
    helper.write_text('#!/bin/sh\nprintf "%s" "$GIT_TOKEN"\n', encoding="utf-8")
    helper.chmod(stat.S_IRWXU)
    return {"GIT_ASKPASS": str(helper), "GIT_TOKEN": token, "GIT_TERMINAL_PROMPT": "0"}


def clone_url(owner: str, repo: str) -> str:
    return f"https://x-access-token@github.com/{owner}/{repo}.git"


def default_installer_repo() -> str:
    return os.environ.get("INSTALLER_REPO", "DevOpsCourse22/ai-scanner-installer")


def default_owner() -> str:
    return os.environ.get("DEFAULT_REPO_OWNER", "DevOpsCourse22")

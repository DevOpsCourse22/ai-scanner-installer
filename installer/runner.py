"""Runs a plan (a list of steps) in a background thread and records every log
line, so the browser can follow it over Server-Sent Events.

Every line passes through the redactor first: any secret the user typed or
the installer generated is replaced by "••••" before it is stored or sent.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


class StepFailed(Exception):
    """A step's command failed; the message says what to do about it."""


class NeedsUser(Exception):
    """The run cannot go on until the user does something by hand
    (e.g. run `minikube tunnel`). The message is shown as an action card."""

    def __init__(self, message: str, command: str = ""):
        super().__init__(message)
        self.command = command


@dataclass
class Step:
    id: str
    title: str
    fn: Callable[["Ctx"], None]
    status: str = "pending"          # pending | running | done | failed | skipped | waiting
    started: float | None = None
    ended: float | None = None
    detail: str = ""


@dataclass
class Run:
    kind: str                         # install | upgrade | destroy
    steps: list[Step]
    secrets: list[str] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"           # running | done | failed | waiting
    action: dict[str, str] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    cond: threading.Condition = field(default_factory=threading.Condition)
    resume: threading.Event = field(default_factory=threading.Event)
    reply: dict[str, str] = field(default_factory=dict)
    cancelled: bool = False

    def redact(self, text: str) -> str:
        for s in self.secrets:
            if s and len(s) >= 4:
                text = text.replace(s, "••••")
        return text

    def log(self, text: str, step: str = "", level: str = "info") -> None:
        with self.cond:
            for line in self.redact(text).splitlines() or [""]:
                self.lines.append({"i": len(self.lines), "t": time.time(),
                                   "step": step, "level": level, "text": line})
            self.cond.notify_all()

    def touch(self) -> None:
        with self.cond:
            self.cond.notify_all()

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "status": self.status, "action": self.action,
                "result": self.result, "lines": len(self.lines),
                "steps": [{"id": s.id, "title": s.title, "status": s.status,
                           "detail": s.detail, "started": s.started, "ended": s.ended}
                          for s in self.steps]}


class Ctx:
    """What a step gets: the answers, the run (for logging), and helpers."""

    def __init__(self, run: Run, answers: dict[str, Any], workdir: str):
        self.run = run
        self.a = answers
        self.workdir = workdir
        self.env: dict[str, str] = dict(os.environ)
        self.step_id = ""
        self.state: dict[str, Any] = {}      # values one step hands to a later one

    def log(self, text: str, level: str = "info") -> None:
        self.run.log(text, self.step_id, level)

    def sh(self, args: list[str], *, input: str | None = None, check: bool = True,
           env: dict[str, str] | None = None, quiet: bool = False, cwd: str | None = None,
           timeout: int | None = None) -> str:
        """Run a command, stream its output into the log, return stdout+stderr."""
        if not quiet:
            self.log("$ " + " ".join(args), "cmd")
        proc = subprocess.Popen(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env={**self.env, **(env or {})}, cwd=cwd or self.workdir, bufsize=1)
        if input is not None:
            assert proc.stdin
            proc.stdin.write(input)
            proc.stdin.close()
        out: list[str] = []
        assert proc.stdout
        deadline = time.time() + timeout if timeout else None
        for line in proc.stdout:
            out.append(line)
            if not quiet:
                self.log(line.rstrip("\n"), "out")
            if deadline and time.time() > deadline:
                proc.kill()
                raise StepFailed(f"{args[0]} took longer than {timeout} s")
        code = proc.wait()
        text = "".join(out)
        if check and code != 0:
            tail = self.run.redact(text.strip().splitlines()[-1] if text.strip() else "")
            raise StepFailed(f"`{' '.join(args[:3])}` failed (exit {code}). {tail}")
        return text

    def wait_for_user(self, message: str, command: str = "", fields: list[dict[str, Any]] | None = None,
                      info: dict[str, str] | None = None) -> dict[str, str]:
        """Pause the run until the user presses Continue in the wizard.
        fields: inputs to show ({name, label, secret}); their values are returned.
        info: values to show with a copy button (e.g. the OAuth URLs)."""
        step = next(s for s in self.run.steps if s.id == self.step_id)
        # reset before publishing "waiting", so a fast Continue is never lost
        self.run.reply = {}
        self.run.resume.clear()
        step.status = "waiting"
        self.run.action = {"message": message, "command": command, "step": self.step_id,
                           "fields": fields or [], "info": info or {}}
        self.run.status = "waiting"
        self.run.touch()
        self.run.resume.wait()
        if self.run.cancelled:
            raise StepFailed("Cancelled. Finished steps stay done; run again to continue.")
        values = dict(self.run.reply)
        for f in fields or []:
            if f.get("secret") and values.get(f["name"]):
                self.run.secrets.append(values[f["name"]])
        step.status = "running"
        self.run.status = "running"
        self.run.action = None
        self.run.touch()
        return values


_current: Run | None = None
_lock = threading.Lock()


def current() -> Run | None:
    return _current


def start(kind: str, steps: list[Step], answers: dict[str, Any], secrets: list[str],
          workdir: str) -> Run:
    global _current
    with _lock:
        if _current and _current.status in ("running", "waiting"):
            raise RuntimeError("Another run is still going.")
        run = Run(kind=kind, steps=steps, secrets=sorted(set(secrets), key=len, reverse=True))
        _current = run
    threading.Thread(target=_execute, args=(run, answers, workdir), daemon=True).start()
    return run


def _execute(run: Run, answers: dict[str, Any], workdir: str) -> None:
    ctx = Ctx(run, answers, workdir)
    for step in run.steps:
        if run.cancelled:
            step.status = "skipped"
            continue
        ctx.step_id = step.id
        step.status, step.started = "running", time.time()
        run.touch()
        try:
            step.fn(ctx)
            step.status = "done" if step.status == "running" else step.status
        except StepFailed as exc:
            step.status, step.detail = "failed", run.redact(str(exc))
            run.log(step.detail, step.id, "error")
            run.status = "failed"
        except Exception as exc:  # noqa: BLE001 - shown to the user, never swallowed
            step.status, step.detail = "failed", run.redact(f"{type(exc).__name__}: {exc}")
            run.log(run.redact(traceback.format_exc()), step.id, "error")
            run.status = "failed"
        step.ended = time.time()
        run.touch()
        if run.status == "failed":
            break
    else:
        if run.cancelled:
            run.status = "failed"
        else:
            run.status = "done"
            run.result = ctx.state.get("result", {})
    run.touch()

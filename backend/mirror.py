"""Mirror AgentForge activity into Multica, for the admin view.

AgentForge is the source of truth: accounts, teams and runs live here, and
/api/admin/* works with or without Multica. This module pushes a copy into a
Multica workspace so an administrator can also watch every user's teams and
runs from Multica's own UI.

What maps to what
-----------------
  AgentForge team   -> one Multica issue describing the team and its agents
  AgentForge run    -> one Multica issue carrying the request, the trace and
                       the result

Deliberately NOT mapped: AgentForge sub-agents are not created as Multica
agents. A Multica agent is an executable collaborator bound to a runtime, so
creating one would invite Multica's daemon to execute it — a second orchestrator
driving the same work. Issues are a tracking surface; that is what we want.

Only flags verified against `multica issue create --help` on this machine are
used. Descriptions go through --description-file rather than --description,
because Multica's own help recommends it on Windows where stdin piping mangles
non-ASCII bytes — which matters for Arabic — and because a long trace would
otherwise hit the command-line length limit.

Disabled by default (MULTICA_MIRROR=1 to enable). While off, every hook is a
no-op and nothing leaves the machine. Note the configured server is Multica
Cloud, so enabling sends team names, prompts and agent output off-machine.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .envs import int_env

ENABLED = os.getenv("MULTICA_MIRROR", "0") == "1"
BIN = os.getenv("MULTICA_CLI_BIN", "multica")
PROFILE = os.getenv("MULTICA_PROFILE", "").strip()
WORKSPACE = os.getenv("MULTICA_WORKSPACE_ID", "").strip()
PROJECT = os.getenv("MULTICA_PROJECT_ID", "").strip()
TIMEOUT = int_env("MULTICA_TIMEOUT", 90)
MAX_BODY = int_env("MULTICA_MAX_BODY_CHARS", 8000)
# Where the description files are staged. Default: the system temp directory,
# NOT the project folder. Staging inside the project puts user prompts and agent
# output into a synced folder (OneDrive), where deletion is unreliable and the
# files linger. The system temp dir is unsynced and swept by the OS.
# Set MULTICA_OUTBOX_IN_CWD=1 to stage in ./.multica-outbox instead, which avoids
# needing --allow-external-file.
OUTBOX_IN_CWD = os.getenv("MULTICA_OUTBOX_IN_CWD", "0") == "1"

_state: Dict[str, Any] = {"sent": 0, "failed": 0, "last_error": "", "last_issue": ""}
_lock = threading.Lock()


def available() -> bool:
    return ENABLED and bool(shutil.which(BIN) or os.path.isfile(BIN))


def status() -> Dict[str, Any]:
    with _lock:
        s = dict(_state)
    s.update({
        "enabled": ENABLED,
        "cli_found": bool(shutil.which(BIN) or os.path.isfile(BIN)),
        "profile": PROFILE or "(default)",
        "workspace": WORKSPACE or "(CLI default)",
        "project": PROJECT or None,
        "note": ("Mirroring is off; the admin view here is complete without it."
                 if not ENABLED else
                 "Mirroring sends team names, prompts and agent output to the "
                 "configured Multica server."),
    })
    return s


def _base_argv() -> List[str]:
    argv = [BIN]
    if PROFILE:
        argv += ["--profile", PROFILE]
    if WORKSPACE:
        argv += ["--workspace-id", WORKSPACE]
    return argv


def _create_issue(title: str, body: str, status_value: str = "") -> str:
    """Create one issue; returns its id, or "" on failure.

    The body is written to a UTF-8 file inside the working directory and passed
    with --description-file: Multica's help recommends this on Windows for
    non-ASCII content, and it avoids the argv length limit on long traces.
    """
    if OUTBOX_IN_CWD:
        tmp_dir = Path.cwd() / ".multica-outbox"
    else:
        tmp_dir = Path(tempfile.gettempdir()) / "agentforge-multica"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _record_failure(f"cannot create {tmp_dir}: {e}")
        return ""
    _prune(tmp_dir)

    fd, path = tempfile.mkstemp(suffix=".md", dir=str(tmp_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body[:MAX_BODY])
        argv = _base_argv() + [
            "issue", "create",
            "--title", title[:200],
            "--description-file",
            os.path.relpath(path, Path.cwd()) if OUTBOX_IN_CWD else path,
            "--output", "json",
        ]
        if not OUTBOX_IN_CWD:
            # The file is outside the working directory by design (see OUTBOX
            # note above). It is freshly written per call with a unique name,
            # so the stale-file reuse this flag guards against cannot occur.
            argv.append("--allow-external-file")
        if PROJECT:
            argv += ["--project", PROJECT]
        if status_value:
            argv += ["--status", status_value]
        try:
            p = subprocess.run(argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=TIMEOUT, cwd=str(Path.cwd()))
        except Exception as e:
            _record_failure(f"{type(e).__name__}: {e}")
            return ""
        if p.returncode != 0:
            _record_failure((p.stderr or p.stdout or "").strip()[:300])
            return ""
        issue = _issue_id(p.stdout)
        with _lock:
            _state["sent"] += 1
            _state["last_error"] = ""
            _state["last_issue"] = issue
        return issue
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _prune(tmp_dir: Path, max_age_s: int = 600) -> None:
    """Drop stale staging files.

    Each file is normally deleted right after its command runs, but a delete can
    fail: a OneDrive or antivirus handle on Windows, or a sandboxed filesystem.
    These files hold user prompts and agent output, so they must not pile up.
    """
    now = time.time()
    try:
        for f in tmp_dir.glob("*.md"):
            try:
                if now - f.stat().st_mtime > max_age_s:
                    f.unlink()
            except OSError:
                pass      # cannot delete here; nothing more we can do
    except OSError:
        pass


def _record_failure(msg: str) -> None:
    with _lock:
        _state["failed"] += 1
        _state["last_error"] = msg[:300]


def _issue_id(stdout: str) -> str:
    if not stdout:
        return ""
    try:
        d = json.loads(stdout)
        if isinstance(d, dict):
            for k in ("identifier", "id", "issue_id", "key", "number"):
                if d.get(k):
                    return str(d[k])
            inner = d.get("issue") or d.get("data")
            if isinstance(inner, dict):
                for k in ("identifier", "id", "key"):
                    if inner.get(k):
                        return str(inner[k])
    except json.JSONDecodeError:
        pass
    m = re.search(r"\b([A-Z]{2,}-\d+)\b", stdout)
    return m.group(1) if m else ""


def _fire(fn) -> None:
    """Mirroring must never slow down or break a user's run."""
    threading.Thread(target=fn, daemon=True).start()


# ----------------------------------------------------------------- hooks
def team_created(owner: str, spec: Dict[str, Any], team_dir: str) -> None:
    if not available():
        return

    def work():
        roster = "\n".join(
            f"- **{a.get('name')}** — {a.get('role')}\n"
            f"  - toolsets: {', '.join(a.get('toolsets') or []) or 'none'}\n"
            + (f"  - skills: {', '.join(a['skills'])}\n" if a.get("skills") else "")
            + f"  - reasoning: {a.get('reasoning')}"
            for a in spec.get("sub_agents") or []
        )
        main = spec.get("main_agent") or {}
        body = (
            f"Team created in AgentForge by **{owner}**.\n\n"
            f"{spec.get('summary', '')}\n\n"
            f"**Goal:** {main.get('goal', '')}\n\n"
            f"**Supervisor:** {main.get('name')} (reasoning: {main.get('reasoning')})\n\n"
            f"**Agents ({len(spec.get('sub_agents') or [])}):**\n{roster}\n\n"
            f"**On disk:** `{team_dir}`\n\n"
            "_Tracking only — these agents execute in Hermes, not in Multica._"
        )
        _create_issue(f"[team] {spec.get('team_name')} · {owner}", body)

    _fire(work)


def run_finished(owner: str, team: str, run_id: str, prompt: str,
                 answer: str, trace: List[Dict[str, Any]]) -> None:
    if not available():
        return

    def work():
        interesting = [t for t in trace if t.get("kind") in
                       ("delegate", "tool_grant", "tool_call", "agent_result",
                        "error", "final")]
        lines = []
        for t in interesting[:60]:
            detail = (t.get("detail") or "").replace("\n", " ")[:200]
            lines.append(f"- `{t.get('kind')}` **{t.get('agent') or '—'}** — {detail}")
        failed = any(t.get("kind") == "error" for t in trace)
        body = (
            f"Run by **{owner}** on team **{team}**.\n\n"
            f"**Request:**\n> {prompt[:800]}\n\n"
            f"**Steps ({len(interesting)}):**\n" + "\n".join(lines) + "\n\n"
            f"**Result:**\n{(answer or '_no final answer_')[:2000]}\n\n"
            f"_AgentForge run `{run_id}`_"
        )
        verdict = "failed" if failed or not answer else "ok"
        _create_issue(f"[run] {team} · {owner} · {verdict}", body)

    _fire(work)

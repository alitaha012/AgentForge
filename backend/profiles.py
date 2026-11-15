"""Per-user Hermes profiles: one isolated work profile per account.

A Hermes profile is a separate instance with its own config, memory, sessions
and skills. Giving each account its own profile is what keeps users genuinely
separate: without it, two accounts' agents would share one memory store, one
session history and one user model, so what one user's agents learned would
surface in another's.

Profile ids are derived from the username and are stable:

    ali        -> af-ali-3f1c2a
    Ali.Taha   -> af-ali-taha-9b40e1

The six-character suffix is a hash of the exact username, so two usernames that
clean up to the same slug ("a.b" and "a-b") never collide on one profile.

Verified against this machine's Hermes source (hermes_cli/subcommands/profile.py,
hermes_constants.PROFILE_ID_RE):
  hermes profile list
  hermes profile create <name> [--clone-from SRC] [--no-alias] [--description T]
  hermes profile delete <name> -y
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
from typing import Dict, List, Optional, Set

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# off      - everyone shares whatever profile Hermes is already using
# per_user - one profile per account (the default, and the isolating one)
MODE = os.getenv("HERMES_PROFILE_MODE", "per_user").lower()
PREFIX = os.getenv("HERMES_PROFILE_PREFIX", "af").strip().lower() or "af"
CLONE_FROM = os.getenv("HERMES_PROFILE_CLONE_FROM", "default").strip()
# A separate sub-profile for worker runs, so machine-written delegation text
# never lands in the user's own memory or user model.
WORKER_SUBPROFILE = os.getenv("HERMES_WORKER_SUBPROFILE", "1") == "1"

# --- cheap tier -------------------------------------------------------------
# A small local model costs nothing and keeps data on the machine, but it is
# weaker at following the delegation protocol and using tools. So agents carry
# a tier and only the mechanical ones run cheap.
#
# Hermes takes base_url from a profile's config, not from a flag, so the cheap
# model needs its OWN profile. Setting HERMES_CHEAP_MODEL turns the tier on;
# the profile is created and configured automatically on first use.
CHEAP_MODEL = os.getenv("HERMES_CHEAP_MODEL", "").strip()
CHEAP_PROVIDER = os.getenv("HERMES_CHEAP_PROVIDER", "ollama").strip()
CHEAP_BASE_URL = os.getenv("HERMES_CHEAP_BASE_URL", "http://127.0.0.1:11434/v1").strip()


def cheap_available() -> bool:
    return MODE == "per_user" and bool(CHEAP_MODEL)

_lock = threading.Lock()
_known: Optional[Set[str]] = None          # profiles that exist on this machine
_provisioned: Set[str] = set()             # profiles we have ensured this process
_failures: Dict[str, str] = {}             # profile -> last error, for reporting


def _bin() -> str:
    return os.getenv("HERMES_CLI_BIN", "hermes")


def profile_id(username: str, *, worker: bool = False, cheap: bool = False) -> str:
    """Deterministic, collision-free profile id for an account.

    Three per user at most:
      af-<slug>-<hash>      the interview, on the capable model
      af-<slug>-<hash>-w    sub-agents on the capable model
      af-<slug>-<hash>-wc   sub-agents on the cheap local model
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (username or "").lower()).strip("-")
    digest = hashlib.sha256((username or "").encode("utf-8")).hexdigest()[:6]
    suffix = ("-wc" if cheap else "-w") if worker else ""
    # Budget: prefix + "-" + slug + "-" + digest + suffix <= 64 chars
    room = 64 - len(PREFIX) - 1 - 1 - len(digest) - len(suffix)
    slug = (slug[:room] or "user").strip("-") or "user"
    pid = f"{PREFIX}-{slug}-{digest}{suffix}"
    if not PROFILE_ID_RE.match(pid):           # belt and braces
        pid = f"{PREFIX}-user-{digest}{suffix}"
    return pid


def _run(args: List[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run([_bin()] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def list_profiles(refresh: bool = False) -> Set[str]:
    global _known
    with _lock:
        if _known is not None and not refresh:
            return set(_known)
    names: Set[str] = set()
    if shutil.which(_bin()):
        try:
            p = _run(["profile", "list"], timeout=60)
            # Output is a table; take every token that looks like a profile id.
            for line in (p.stdout or "").splitlines():
                for tok in re.findall(r"[a-z0-9][a-z0-9_-]{2,63}", line):
                    if tok.startswith(PREFIX + "-") or tok == "default":
                        names.add(tok)
        except Exception:
            pass
    with _lock:
        _known = names
    return set(names)


def _configure_cheap(pid: str) -> bool:
    """Point a freshly created profile at the local model."""
    settings = [("model.provider", CHEAP_PROVIDER),
                ("model.base_url", CHEAP_BASE_URL),
                ("model.default", CHEAP_MODEL),
                # the local model has no Honcho credentials of its own, and a
                # worker profile should not be feeding anyone's user model
                ("memory.provider", "")]
    for key, value in settings:
        try:
            p = _run(["-p", pid, "config", "set", key, value], timeout=60)
        except Exception as e:
            with _lock:
                _failures[pid] = f"config set {key}: {e}"
            return False
        if p.returncode != 0:
            with _lock:
                _failures[pid] = (f"config set {key} failed: "
                                  f"{(p.stderr or p.stdout or '').strip()[:200]}")
            return False
    return True


def ensure(username: str, *, worker: bool = False, cheap: bool = False) -> Optional[str]:
    """Return the profile id for this user, creating the profile if needed.

    Returns None when profiles are switched off, or when creation failed — the
    caller then runs on the default profile rather than failing the request.
    """
    if MODE != "per_user" or not username:
        return None
    if cheap and not cheap_available():
        cheap = False                       # tier not configured; use the normal one
    pid = profile_id(username, worker=worker, cheap=cheap)

    with _lock:
        if pid in _provisioned:
            return pid
        if pid in _failures:
            return None

    if pid in list_profiles():
        with _lock:
            _provisioned.add(pid)
        return pid

    args = ["profile", "create", pid, "--no-alias",
            "--description",
            (f"AgentForge cheap-tier workers for '{username}' on "
             f"{CHEAP_PROVIDER}/{CHEAP_MODEL}. Mechanical steps only."
             if cheap else
             f"AgentForge worker runs for user '{username}'. Isolated: runs sub-agents, "
             "holds no personal memory." if worker else
             f"AgentForge workspace for user '{username}'. Holds that account's agents, "
             "memory and sessions.")]
    if CLONE_FROM:
        # Carries config, skills and provider auth across; Hermes strips the
        # source's messaging-bot credentials, so two profiles never fight over
        # one bot token.
        args += ["--clone-from", CLONE_FROM]
    try:
        p = _run(args)
    except Exception as e:
        with _lock:
            _failures[pid] = str(e)
        return None

    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()[:300]
        # Someone else created it between our check and now: that is success.
        if "exists" in err.lower():
            with _lock:
                _provisioned.add(pid)
            return pid
        with _lock:
            _failures[pid] = err
        return None

    if cheap and not _configure_cheap(pid):
        return None                         # fall back to the capable profile
    with _lock:
        _provisioned.add(pid)
        if _known is not None:
            _known.add(pid)
    return pid


def for_role(username: str, role: str, tier: str = "") -> Optional[str]:
    """Which profile a given call should run on."""
    if role == "worker" and WORKER_SUBPROFILE:
        if tier == "cheap" and cheap_available():
            got = ensure(username, worker=True, cheap=True)
            if got:
                return got                  # else fall through to the capable one
        return ensure(username, worker=True) or ensure(username)
    return ensure(username)


def status(username: str = "") -> Dict[str, object]:
    out: Dict[str, object] = {
        "mode": MODE,
        "prefix": PREFIX,
        "clone_from": CLONE_FROM or None,
        "worker_subprofile": WORKER_SUBPROFILE,
        "failures": dict(_failures),
    }
    if username and MODE == "per_user":
        out["profile"] = profile_id(username)
        out["worker_profile"] = (profile_id(username, worker=True)
                                 if WORKER_SUBPROFILE else out["profile"])
        out["provisioned"] = out["profile"] in _provisioned
    out["cheap_tier"] = (
        {"enabled": True, "model": CHEAP_MODEL, "provider": CHEAP_PROVIDER,
         "base_url": CHEAP_BASE_URL,
         "profile": profile_id(username, worker=True, cheap=True) if username else None}
        if cheap_available() else {"enabled": False})
    return out

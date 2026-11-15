"""User accounts: sign-up, login, sessions.

Passwords are never stored, and never written to any file. Each one is put
through scrypt with its own random salt; only the derived hash is kept, so the
stored value cannot be turned back into the password by anyone — including the
operator of this machine.

The one real secret on disk is the server key in .secrets/server.key (created on
first run, permissions 0600). It signs nothing by itself: session tokens are
random, and only their hashes are stored, so a stolen database cannot be used to
mint a session.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional, Tuple

from . import store
from .envs import int_env

SECRETS_DIR = Path(os.getenv(
    "AGENTFORGE_SECRETS_DIR",
    str(Path(__file__).resolve().parent.parent / ".secrets"),
))
SESSION_TTL_DAYS = int_env("AGENTFORGE_SESSION_TTL_DAYS", 30)
USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,31}$")

# scrypt parameters. n=2**15 is ~100ms per hash on a laptop: slow enough to make
# guessing expensive, fast enough for an interactive login.
KEY_BYTES = 32
_N, _R, _P, _DKLEN = 2 ** 15, 8, 1, 64
# OpenSSL caps scrypt memory at 32 MiB by default, which these parameters exceed
# (128 * N * r = 32 MiB plus overhead), so raise it explicitly.
_MAXMEM = 128 * 1024 * 1024


def server_key() -> bytes:
    """Read (or create) the machine-local server key."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(SECRETS_DIR, 0o700)
    except OSError:
        pass  # Windows ignores POSIX modes
    path = SECRETS_DIR / "server.key"
    # uvicorn --reload runs two processes, so this can be entered concurrently.
    # Two hazards, not one:
    #   1. both generate a key and the second overwrites the first, silently
    #      invalidating every session signed with it;
    #   2. the loser opens the file the winner has only just created and reads
    #      it before the bytes are written, getting an empty key.
    # O_EXCL settles (1): exactly one writer wins. For (2) the loser waits for
    # the file to reach full length rather than trusting its mere existence.
    for _ in range(100):                       # ~5s, then give up and raise
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            data = b""
            try:
                data = path.read_bytes()
            except OSError:
                pass
            if len(data) >= KEY_BYTES:
                return data
            time.sleep(0.05)                   # winner is mid-write
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(KEY_BYTES))
            fh.flush()
            os.fsync(fh.fileno())              # readers must see the full key
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path.read_bytes()
    raise RuntimeError(f"server key at {path} did not become readable")


# --------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                        dklen=_DKLEN, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p),
                            dklen=len(hash_hex) // 2, maxmem=_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def password_problem(password: str) -> Optional[str]:
    if len(password) < 10:
        return "Password must be at least 10 characters."
    if password.lower() in {"password12", "1234567890", "qwertyuiop"}:
        return "That password is too common."
    return None


def username_problem(username: str) -> Optional[str]:
    if not USERNAME_RE.match(username or ""):
        return ("Username must be 3-32 characters: letters, digits, dot, "
                "underscore or hyphen, starting with a letter or digit.")
    return None


# --------------------------------------------------------------- accounts
def sign_up(username: str, password: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns (user_id, error)."""
    username = (username or "").strip()
    for problem in (username_problem(username), password_problem(password or "")):
        if problem:
            return None, problem
    if store.find_user(username):
        return None, "That username is taken."
    uid = store.create_user(username, hash_password(password))
    return uid, None


def log_in(username: str, password: str, *, agent: str = "",
           ip: str = "") -> Tuple[Optional[str], Optional[str]]:
    """Returns (session_token, error). Deliberately vague on failure: saying
    which half was wrong tells an attacker which usernames exist."""
    user = store.find_user((username or "").strip())
    if not user:
        # Spend comparable time so a missing user is not faster than a wrong
        # password — otherwise response time leaks which usernames exist.
        hash_password(password or "x")
        return None, "Wrong username or password."
    if not verify_password(password or "", user["pwd"]):
        return None, "Wrong username or password."
    token = secrets.token_urlsafe(32)
    store.create_auth_session(token_hash(token), user["id"],
                              time.time() + SESSION_TTL_DAYS * 86400,
                              agent=agent, ip=ip)
    store.touch_user(user["id"])
    return token, None


def token_hash(token: str) -> str:
    """Store only a keyed hash of a session token, so the database alone is
    not enough to impersonate anyone."""
    return hmac.new(server_key(), token.encode("utf-8"), hashlib.sha256).hexdigest()


def user_for_token(token: str) -> Optional[dict]:
    if not token:
        return None
    row = store.get_auth_session(token_hash(token))
    if not row:
        return None
    store.touch_auth_session(token_hash(token))
    if row["expires"] < time.time():
        store.delete_auth_session(row["token_hash"])
        return None
    return store.get_user(row["user_id"])


def log_out(token: str) -> None:
    if token:
        store.delete_auth_session(token_hash(token))


def change_password(user_id: str, current: str, new: str) -> Optional[str]:
    user = store.get_user(user_id)
    if not user or not verify_password(current or "", user["pwd"]):
        return "Current password is wrong."
    problem = password_problem(new or "")
    if problem:
        return problem
    store.set_user_password(user_id, hash_password(new))
    store.delete_user_auth_sessions(user_id)  # force re-login everywhere
    return None

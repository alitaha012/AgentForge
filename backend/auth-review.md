# Code Review: auth.py

## Summary

This is a well-written authentication module with solid security practices: scrypt password hashing, timing-attack mitigations, and keyed session-token storage. A few minor issues are noted below.

---

## Issues

### 1. Race condition in server_key() (Moderate)

```python
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
```

Two processes starting simultaneously could both see the file as missing, then both write their own key. The second write overwrites the first, silently invalidating any sessions created with the original key. Use exclusive file creation (e.g., `os.open` with `O_CREAT | O_EXCL`) or a file lock to avoid this.

---

### 2. No validation on SESSION_TTL_DAYS (Minor)

```python
SESSION_TTL_DAYS = int(os.getenv("AGENTFORGE_SESSION_TTL_DAYS", "30"))
```

If the environment variable is set to a non-integer value, the application crashes on import with a ValueError. Consider wrapping this in a try/except that falls back to the default.

---

### 3. Type inconsistency in SECRETS_DIR default (Style)

```python
SECRETS_DIR = Path(os.getenv(
    "AGENTFORGE_SECRETS_DIR",
    Path(__file__).resolve().parent.parent / ".secrets",
))
```

`os.getenv`'s second argument is typed as `str | None`, but a `Path` object is passed here. It works because `Path()` accepts both strings and Path objects, but passing a string (via `str()`) would be more explicit and match the function's contract.

---

## Positive Notes

- scrypt parameters (N=2^15, r=8, p=1) balance security and usability well.
- `hmac.compare_digest` prevents timing attacks on password verification.
- Dummy `hash_password` call on unknown usernames prevents username-enumeration via response time.
- Session tokens are never stored directly; only their keyed hashes are kept.
- Clear docstrings and well-organized code sections.

---

No other issues found.

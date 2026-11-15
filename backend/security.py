"""Response headers, and a Content-Security-Policy that is worth having.

The page is one self-contained file with its script and style inline, so the
usual advice — "use a nonce" — does not fit a static file, and the usual
shortcut — 'unsafe-inline' — gives up most of what CSP is for. Instead the
inline blocks are hashed and the hashes are named in the policy: the browser
runs exactly the script that shipped and nothing else, including anything an
injection manages to write into the DOM.

The hashes are recomputed when the file changes on disk, because during
development it changes often and a stale hash means a blank page.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

_cache: Dict[str, Tuple[float, List[str], List[str]]] = {}

_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)


def _sha(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def inline_hashes(path: Path) -> Tuple[List[str], List[str]]:
    """(script hashes, style hashes) for one HTML file, cached by mtime."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return [], []
    hit = _cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1], hit[2]

    html = path.read_text(encoding="utf-8", errors="replace")
    scripts = [_sha(m.group(1)) for m in _SCRIPT.finditer(html)]
    styles = [_sha(m.group(1)) for m in _STYLE.finditer(html)]
    _cache[str(path)] = (stamp, scripts, styles)
    return scripts, styles


def policy(index_path: Path) -> str:
    scripts, _styles = inline_hashes(index_path)
    # data: is allowed for images because the agent figures are inline SVG
    # and the browser treats some of them as data URLs; blob: is needed for
    # the voice recording before it is uploaded.
    return "; ".join([
        "default-src 'self'",
        # Script is hash-locked: exactly the block that shipped runs, and
        # nothing an injection writes into the DOM does. The markup carries
        # no inline handlers, so this costs nothing.
        "script-src 'self' " + " ".join(scripts),
        # Style cannot be: the markup uses style="..." attributes, which a
        # hash does not cover, and a directive carrying a hash makes
        # browsers ignore 'unsafe-inline' entirely. So style takes the
        # permissive form on its own. Injected CSS can reskin the page; it
        # cannot run code, which is the line that matters.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


HEADERS = {
    # Stops a response being re-interpreted as a different type, which is how
    # an uploaded file becomes an executed script.
    "X-Content-Type-Options": "nosniff",
    # Nothing here should ever be framed.
    "X-Frame-Options": "DENY",
    # Never leak a workspace path or a run id to another site.
    "Referrer-Policy": "no-referrer",
    # The page asks for the microphone itself; nothing else needs anything.
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(self)",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def apply(response, index_path: Path) -> None:
    for k, v in HEADERS.items():
        response.headers.setdefault(k, v)
    if os.getenv("AGENTFORGE_CSP", "1") == "1":
        response.headers.setdefault("Content-Security-Policy",
                                    policy(index_path))

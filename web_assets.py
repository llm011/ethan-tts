"""Static page rendering with content-hashed asset URLs.

`no-cache` only makes the browser *ask*; some browsers still reuse a cached
copy in memory. Appending `?v=<content hash>` changes the URL whenever the
file changes, which is the only thing that reliably invalidates it.
"""
from __future__ import annotations

import hashlib
import re

from config import STATIC_DIR

_ASSET_RE = re.compile(r'(href|src)="(/static/[^"?]+)"')


def asset_versioned(html: str) -> str:
    """Rewrite /static/* references to carry a short content hash."""

    def add_version(match: re.Match) -> str:
        attr, path = match.group(1), match.group(2)
        target = STATIC_DIR / path.removeprefix("/static/")
        try:
            digest = hashlib.md5(target.read_bytes()).hexdigest()[:10]
        except OSError:
            return match.group(0)
        return f'{attr}="{path}?v={digest}"'

    return _ASSET_RE.sub(add_version, html)


def render_page(name: str) -> str:
    """Read a page from static/ and version its asset references."""
    return asset_versioned((STATIC_DIR / name).read_text(encoding="utf-8"))

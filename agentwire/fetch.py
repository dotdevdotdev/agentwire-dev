"""`agentwire fetch <url>` — fetch a page via Jina Reader, return markdown."""

from __future__ import annotations

import sys
import urllib.error
import urllib.request


JINA_BASE = "https://r.jina.ai/"
DEFAULT_LIMIT = 8000
# Cloudflare blocks requests with the default urllib UA; pose as a browser.
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


class FetchError(Exception):
    pass


def fetch_url(url: str, limit: int = DEFAULT_LIMIT) -> str:
    req = urllib.request.Request(
        JINA_BASE + url,
        headers={"Accept": "text/plain", "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        raise FetchError(f"HTTP {e.code}: {msg[:300]}") from e
    except urllib.error.URLError as e:
        raise FetchError(f"Network error: {e.reason}") from e

    if limit and len(content) > limit:
        content = content[:limit] + f"\n\n[truncated at {limit} chars]"
    return content


def cmd_fetch(args) -> int:
    url = args.url.strip()
    if not url:
        print("Error: url is required. Usage: agentwire fetch <url>", file=sys.stderr)
        return 2
    try:
        content = fetch_url(url, limit=args.limit)
    except FetchError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(content)
    return 0

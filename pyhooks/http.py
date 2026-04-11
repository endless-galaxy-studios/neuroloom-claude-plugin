"""
Minimal HTTP client for neuroloom hook modules.

Uses only the Python standard library (``urllib.request``) — no third-party
packages are required or imported.  This keeps the hook startup time fast and
avoids dependency installation problems in the venv.

Design constraints
------------------
- Never raises.  Any network-level failure (DNS, TCP, TLS, timeout) returns
  ``None`` so that hook processes never crash and never block Claude Code.
- Returns ``(status_code, body_bytes)`` for *any* HTTP response, including
  4xx and 5xx.  The caller decides what to do with error responses.
- The API key appears **only** in the ``Authorization`` header.  It is never
  interpolated into URLs, log messages, or exception strings.
"""

import urllib.error
import urllib.request
from urllib.request import Request

_USER_AGENT = "neuroloom-plugin/0.1.0"


def post_json(
    url: str,
    headers: dict[str, str],
    payload: bytes,
    timeout: float,
) -> tuple[int, bytes] | None:
    """
    POST *payload* (raw bytes, expected to be UTF-8 JSON) to *url*.

    Parameters
    ----------
    url:
        Full URL including scheme, host, path, and optional query string.
    headers:
        Extra request headers.  The ``Authorization`` header carrying the API
        key should be included here.  ``Content-Type`` and ``User-Agent`` are
        added automatically and will override any caller-supplied values.
    payload:
        Request body as bytes.
    timeout:
        Socket-level timeout in seconds applied to both the connect and read
        phases.

    Returns
    -------
    ``(status_code, body_bytes)``
        On any completed HTTP exchange, regardless of status code.
    ``None``
        On network-level failure (connection refused, DNS failure, timeout,
        TLS error, etc.).  HTTP-level errors (4xx, 5xx) are *not* ``None``.
    """
    merged_headers: dict[str, str] = {
        **headers,
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }

    req = Request(url, data=payload, headers=merged_headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        # HTTPError is raised for 4xx/5xx responses; it carries a status code
        # and a readable body, so we surface it as a normal (status, body) pair
        # rather than a failure.
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, body
    except Exception:
        # Network-level failures (URLError, timeout, TLS, etc.).
        # The API key must not appear in any log output — do not format exc.
        return None

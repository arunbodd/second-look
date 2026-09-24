"""Keep the local app local.

Three layers, so the app is safe on a laptop on café or office Wi-Fi:

1. **Loopback only.** ``serve.py`` binds 127.0.0.1 and refuses any other address unless it is
   started with ``--allow-network`` (the app has no sign-in, so anyone who can reach the port
   could read the cases and spend the API credits).
2. **Host allow-list.** Requests whose ``Host`` header is not an allowed name are rejected. This
   stops DNS rebinding, where a web page on the internet points its own domain at 127.0.0.1 and
   reads the app from the victim's browser.
3. **Same-origin writes.** A POST, PUT, PATCH, or DELETE that a browser sends from another origin
   (a page on any other site, or another local app) is rejected, so a web page cannot trigger an
   AI review, upload data, or record a decision behind the investigator's back. Command-line
   clients send no ``Origin`` header and are unaffected.

``ALLOWED_HOSTS`` (comma-separated) extends the allow-list for a deployment behind a proxy.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

DEFAULT_HOSTS = ("localhost", "127.0.0.1", "::1")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
    return set(DEFAULT_HOSTS) | extra


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _hostname(netloc: str) -> str:
    try:
        return (urlsplit("//" + netloc).hostname or "").lower()
    except ValueError:
        return ""


def check(method: str, host_header: str | None, origin: str | None, sec_fetch_site: str | None,
          hosts: set[str] | None = None) -> str | None:
    """Return a reason to reject the request, or None to let it through."""
    hosts = hosts if hosts is not None else allowed_hosts()
    if "*" not in hosts:
        name = _hostname(host_header or "")
        if not name or name not in hosts:
            return "Host not allowed"
    if method.upper() in SAFE_METHODS:
        return None
    if sec_fetch_site and sec_fetch_site.lower() == "cross-site":
        return "Cross-site request blocked"
    if origin:
        if origin == "null":
            return "Cross-origin request blocked"
        o = urlsplit(origin)
        if (o.netloc or "").lower() != (host_header or "").lower():
            return "Cross-origin request blocked"
    return None

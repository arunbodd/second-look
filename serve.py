"""Start the app on http://127.0.0.1:8000 and https://127.0.0.1:8443 in one process.

The https listener uses a self-signed certificate generated on first run into data/certs/
(needs the `openssl` command, present on macOS and Linux). Browsers show a one-time warning
for a self-signed certificate; accept it to continue. Ctrl+C stops both listeners.

    python serve.py                 # both listeners
    python serve.py --http-only     # plain http on 8000
    python serve.py --port 8000 --https-port 8443

Security: the app has no sign-in, so it listens on this computer only (127.0.0.1) and cannot be
reached from the local network or public Wi-Fi. Other addresses need --allow-network; see
app/netguard.py for the host and origin checks that also stop malicious web pages.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
CERT_DIR = ROOT / "data" / "certs"


def ensure_cert(host: str) -> tuple[Path, Path] | None:
    cert, key = CERT_DIR / "localhost.crt", CERT_DIR / "localhost.key"
    if cert.exists() and key.exists():
        return cert, key
    if not shutil.which("openssl"):
        return None
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    san = f"subjectAltName=DNS:localhost,IP:127.0.0.1" + ("" if host in ("127.0.0.1", "localhost") else f",IP:{host}")
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "825", "-nodes",
           "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost", "-addext", san]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        key.chmod(0o600)  # the private key is readable by this user only
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Could not create a certificate ({exc}); https disabled.", file=sys.stderr)
        return None
    return cert, key


def free_port(host: str, start: int, avoid: set[int] = frozenset(), tries: int = 50) -> int:
    """The first port from ``start`` that nothing is listening on (another app may own 8000)."""
    for port in range(start, start + tries):
        if port in avoid:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port between {start} and {start + tries - 1}.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000, help="http port (default 8000)")
    ap.add_argument("--https-port", type=int, default=8443, help="https port (default 8443)")
    ap.add_argument("--http-only", action="store_true", help="skip the https listener")
    ap.add_argument("--open", action="store_true", help="open the app in the default browser once it is up")
    ap.add_argument("--allow-network", action="store_true",
                    help="allow a non-loopback --host (exposes the app, which has no sign-in, to that network)")
    args = ap.parse_args()

    from app.netguard import is_loopback

    if not is_loopback(args.host):
        if not args.allow_network:
            raise SystemExit(f"Refusing to listen on {args.host}: the app has no sign-in, so it only listens on this "
                             "computer (127.0.0.1). Add --allow-network if you really mean to expose it to that network.")
        print(f"WARNING: listening on {args.host}. Anyone who can reach this address can read the cases and use "
              "the API keys in .env.", file=sys.stderr)
        import os
        os.environ["ALLOWED_HOSTS"] = ",".join(filter(None, [os.environ.get("ALLOWED_HOSTS", ""), args.host]))

    port = free_port(args.host, args.port)
    if port != args.port:
        print(f"Port {args.port} is already used by another program; using {port} instead.")
    https_port = free_port(args.host, args.https_port, avoid={port})
    if not args.http_only and https_port != args.https_port:
        print(f"Port {args.https_port} is already used by another program; using {https_port} for https instead.")
    args.port, args.https_port = port, https_port

    # One service instance shared by both listeners, so the queue, cache, and event log are the same.
    from app.main import create_app
    from app.service import TriageService
    from app.settings import load_settings

    app = create_app(TriageService(load_settings()))
    servers = [uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="info", server_header=False))]
    cert = None if args.http_only else ensure_cert(args.host)
    if cert:
        servers.append(uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.https_port, log_level="info",
                                                     ssl_certfile=str(cert[0]), ssl_keyfile=str(cert[1]), server_header=False)))
    url = f"http://{args.host}:{args.port}"
    print(f"Second Look: {url}" + (f"  and  https://{args.host}:{args.https_port} (self-signed certificate)" if cert else ""))
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "server_url.txt").write_text(url + "\n")
    if args.open:
        threading.Timer(2.0, webbrowser.open, [url]).start()
    if len(servers) == 1:
        servers[0].run()
        return
    # uvicorn only installs signal handlers in the main thread; the https listener runs in a
    # daemon thread and is asked to stop when the main one exits on Ctrl+C.
    extra = servers[1:]
    for s in extra:
        threading.Thread(target=s.run, daemon=True, name="https").start()
    servers[0].run()
    for s in extra:
        s.should_exit = True


if __name__ == "__main__":
    main()

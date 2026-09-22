"""Get a refresh token for a live-test mailbox. The OWNER runs this on their own machine.

    python scripts/engagement_live_token.py google
    python scripts/engagement_live_token.py microsoft

It reads the app credentials from the environment (the same variables CI uses), opens the consent
screen in a browser, receives the redirect on http://localhost:8765/callback, and prints the refresh
token ONCE so it can be pasted into the GitHub secret. Nothing is written to disk.

The redirect URI http://localhost:8765/callback must be registered on both apps (see
docs/engagement/live-tests.md). Sign in as the TEST mailbox, not your own.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

REDIRECT = "http://localhost:8765/callback"


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"set {name} first")
    return value


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("google", "microsoft"):
        sys.exit("usage: python scripts/engagement_live_token.py google|microsoft")
    provider = sys.argv[1]

    from nexus.engagement.config import GOOGLE_SCOPES, MICROSOFT_SCOPES, OAuthApp
    from nexus.engagement.mailboxes import oauth
    from nexus.network.oauth import make_pkce

    if provider == "google":
        app = OAuthApp("google", _env("NEXUS_LIVE_GOOGLE_CLIENT_ID"),
                       _env("NEXUS_LIVE_GOOGLE_CLIENT_SECRET"), "", REDIRECT, GOOGLE_SCOPES, ())
    else:
        app = OAuthApp("microsoft", _env("NEXUS_LIVE_MICROSOFT_CLIENT_ID"),
                       _env("NEXUS_LIVE_MICROSOFT_CLIENT_SECRET"),
                       os.environ.get("NEXUS_LIVE_MICROSOFT_TENANT", "common"), REDIRECT,
                       MICROSOFT_SCOPES, ())

    verifier, challenge = make_pkce()
    state = os.urandom(12).hex()
    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib name)
            query = parse_qs(urlparse(self.path).query)
            received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. Return to the terminal.")

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 8765), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    url = oauth.authorize_url(app, state=state, challenge=challenge)
    print("Opening the consent screen. Sign in as the TEST mailbox.\n", url, "\n")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    if received.get("state") != state or "code" not in received:
        sys.exit(f"no authorization code received: {received.get('error', 'timeout')}")
    data = asyncio.run(oauth.exchange_code(app, code=received["code"], verifier=verifier))
    missing = oauth.missing_scopes(app, data.get("scope", ""))
    if missing:
        sys.exit(f"the grant is missing scopes: {missing}")
    print("Refresh token (paste into the GitHub secret, then clear this terminal):\n")
    print(data["refresh_token"])


if __name__ == "__main__":
    main()

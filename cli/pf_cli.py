#!/usr/bin/env python3
"""CLI for pf-server authentication and WebSocket connection."""

import asyncio
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

import httpx
import websockets

SERVER_URL = "http://localhost:8000"
TOKEN_PATH = Path.home() / ".config" / "pf" / "token"
CALLBACK_PORT = 9876


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback from GitHub."""

    code: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            query = parse_qs(parsed.query)
            OAuthCallbackHandler.code = query.get("code", [None])[0]

            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h1>Login successful!</h1><p>You can close this window.</p>"
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logging


def wait_for_callback() -> str:
    """Start local server and wait for OAuth callback."""
    server = HTTPServer(("localhost", CALLBACK_PORT), OAuthCallbackHandler)

    def serve():
        server.handle_request()

    thread = Thread(target=serve)
    thread.start()
    thread.join(timeout=120)  # 2 minute timeout

    if OAuthCallbackHandler.code is None:
        raise TimeoutError("OAuth callback not received")

    return OAuthCallbackHandler.code


def login():
    """Perform OAuth login flow."""
    # 1. Get OAuth URL from server
    resp = httpx.get(f"{SERVER_URL}/auth/login")
    resp.raise_for_status()
    oauth_url = resp.json()["url"]

    print(f"Opening browser for login...")

    # 2. Open browser
    webbrowser.open(oauth_url)

    # 3. Wait for callback
    print("Waiting for authentication...")
    code = wait_for_callback()

    # 4. Exchange code for JWT
    resp = httpx.post(f"{SERVER_URL}/auth/token", json={"code": code})
    if resp.status_code == 403:
        print("Error: You are not in the beta whitelist.")
        return
    resp.raise_for_status()
    token = resp.json()["token"]

    # 5. Save token
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token)
    TOKEN_PATH.chmod(0o600)

    print("Login successful! Token saved.")


def get_token() -> str:
    """Load saved token."""
    if not TOKEN_PATH.exists():
        raise FileNotFoundError("Not logged in. Run 'login' first.")
    return TOKEN_PATH.read_text().strip()


async def connect():
    """Connect to server via WebSocket."""
    token = get_token()
    uri = f"ws://localhost:8000/connect?token={token}"

    print("Connecting to server...")
    async with websockets.connect(uri) as ws:
        print("Connected! Type messages to send, Ctrl+C to exit.")

        async def receive():
            async for msg in ws:
                print(f"< {msg}")

        async def send():
            loop = asyncio.get_event_loop()
            while True:
                msg = await loop.run_in_executor(None, input)
                await ws.send(msg)

        await asyncio.gather(receive(), send())


def main():
    import sys

    if len(sys.argv) < 2:
        print("Usage: pf_cli.py <login|connect>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "login":
        login()
    elif cmd == "connect":
        asyncio.run(connect())
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()

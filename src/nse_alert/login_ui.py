from __future__ import annotations

import html
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from nse_alert.envfile import upsert_env

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
CALLBACK_PATH = "/callback"


def redirect_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def kite_login_url(api_key: str) -> str:
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"


def exchange_request_token(
    *,
    api_key: str,
    api_secret: str,
    request_token: str,
) -> str:
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    session = kite.generate_session(request_token, api_secret=api_secret)
    token = session.get("access_token")
    if not token:
        raise RuntimeError(f"Kite session response missing access_token: {session}")
    return str(token)


def _page(title: str, body: str) -> bytes:
    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #0f1419;
      --card: #1a222c;
      --text: #e8eef5;
      --muted: #9aabbc;
      --accent: #2f8f6b;
      --accent-2: #3d7ea6;
      --border: #2a3542;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background:
        radial-gradient(1000px 500px at 10% -10%, #1d3a33 0%, transparent 55%),
        radial-gradient(900px 500px at 100% 0%, #1a2f44 0%, transparent 50%),
        var(--bg);
      color: var(--text);
      display: grid; place-items: center; padding: 2rem 1rem;
    }}
    main {{
      width: min(560px, 100%); background: color-mix(in srgb, var(--card) 92%, black);
      border: 1px solid var(--border); border-radius: 18px; padding: 1.6rem 1.5rem 1.4rem;
      box-shadow: 0 20px 50px rgba(0,0,0,.35);
    }}
    h1 {{ margin: 0 0 .35rem; font-size: 1.45rem; letter-spacing: -.02em; }}
    p {{ margin: .4rem 0 1rem; color: var(--muted); line-height: 1.45; }}
    .btn {{
      display: inline-block; text-decoration: none; border: 0; cursor: pointer;
      background: linear-gradient(135deg, var(--accent), #246b52);
      color: white; font-weight: 600; padding: .75rem 1rem; border-radius: 10px;
      width: 100%; text-align: center; font-size: 1rem;
    }}
    .btn.secondary {{ background: linear-gradient(135deg, var(--accent-2), #2d5f80); margin-top: .65rem; }}
    label {{ display:block; font-size:.85rem; color: var(--muted); margin: 1rem 0 .35rem; }}
    input[type=text] {{
      width: 100%; padding: .75rem .8rem; border-radius: 10px; border: 1px solid var(--border);
      background: #10161d; color: var(--text); font-size: .95rem;
    }}
    .ok {{ color: #7ddea8; }}
    .err {{ color: #ff8f8f; }}
    code {{ color: #c9e4ff; }}
    hr {{ border: 0; border-top: 1px solid var(--border); margin: 1.25rem 0; }}
    .hint {{ font-size: .85rem; }}
    .token-box {{
      display: flex; gap: .5rem; align-items: stretch; margin: .75rem 0 1rem;
    }}
    .token-box input {{
      flex: 1; padding: .75rem .8rem; border-radius: 10px; border: 1px solid var(--border);
      background: #10161d; color: var(--text); font-size: .85rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .token-box button {{
      flex: 0 0 auto; border: 0; cursor: pointer; border-radius: 10px; padding: 0 1rem;
      background: linear-gradient(135deg, var(--accent-2), #2d5f80); color: white; font-weight: 600;
    }}
    .token-box button.copied {{ background: linear-gradient(135deg, var(--accent), #246b52); }}
    .copy-status {{ min-height: 1.2em; font-size: .85rem; color: var(--muted); margin: 0 0 .5rem; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
    {body}
  </main>
  <script>
    function copyAccessToken(btn) {{
      const input = document.getElementById("access-token");
      if (!input) return;
      const value = input.value;
      const done = () => {{
        btn.textContent = "Copied";
        btn.classList.add("copied");
        const status = document.getElementById("copy-status");
        if (status) status.textContent = "Token copied to clipboard.";
        setTimeout(() => {{
          btn.textContent = "Copy";
          btn.classList.remove("copied");
        }}, 1600);
      }};
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(value).then(done).catch(() => {{
          input.select();
          document.execCommand("copy");
          done();
        }});
      }} else {{
        input.select();
        document.execCommand("copy");
        done();
      }}
    }}
  </script>
</body>
</html>"""
    return doc.encode("utf-8")


def _success_body(token: str) -> str:
    safe = html.escape(token)
    return f"""
            <p class="ok">Access token saved to <code>.env</code> and <code>FEED_MODE=kite</code> was set.</p>
            <label for="access-token">Access token</label>
            <div class="token-box">
              <input id="access-token" type="text" readonly value="{safe}" />
              <button type="button" onclick="copyAccessToken(this)">Copy</button>
            </div>
            <p id="copy-status" class="copy-status"></p>
            <p class="hint">Use Copy if you need to paste it onto a cloud VM with <code>nse-alert set-token</code>.</p>
            <p>You can close this tab and run:</p>
            <p><code>uv run nse-alert watch</code></p>
            """


def run_login_ui(
    *,
    api_key: str,
    api_secret: str,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> str:
    """Serve a tiny local UI to obtain/save today's Kite access token.

    Returns the saved access token.
    """
    result: dict[str, Any] = {"token": "", "error": "", "done": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
            logger.debug("login-ui: " + fmt, *args)

        def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == CALLBACK_PATH:
                self._handle_callback(parsed.query)
                return
            if parsed.path in {"/", "/index.html"}:
                login = html.escape(kite_login_url(api_key))
                body = f"""
                <p>Get today's Kite access token in one click, or paste one you already have.</p>
                <a class="btn" href="{login}">1. Login with Kite</a>
                <p class="hint">After Zerodha login you will return here automatically.</p>
                <hr />
                <form method="POST" action="/save-token">
                  <label for="token">Or paste access token</label>
                  <input id="token" name="token" type="text" placeholder="paste KITE_ACCESS_TOKEN" autocomplete="off" required />
                  <button class="btn secondary" type="submit">Save token to .env</button>
                </form>
                <p class="hint">Kite app Redirect URL must be:<br/><code>{html.escape(redirect_url(port))}</code></p>
                """
                self._send(200, _page("NSE Alert · Kite login", body))
                return
            self._send(404, _page("Not found", "<p class='err'>Unknown page.</p>"))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            form = parse_qs(raw)
            if parsed.path == "/save-token":
                token = (form.get("token") or [""])[0].strip()
                if not token:
                    self._send(400, _page("Missing token", "<p class='err'>Paste a token first.</p>"))
                    return
                self._persist(token)
                self._send(200, _page("Token saved", _success_body(token)))
                result["done"].set()
                return
            self._send(404, _page("Not found", "<p class='err'>Unknown form.</p>"))

        def _handle_callback(self, query: str) -> None:
            params = parse_qs(query)
            status = (params.get("status") or [""])[0]
            request_token = (params.get("request_token") or [""])[0]
            if status and status != "success":
                msg = html.escape(status)
                self._send(400, _page("Login failed", f"<p class='err'>Kite returned status: {msg}</p>"))
                result["error"] = status
                result["done"].set()
                return
            if not request_token:
                self._send(
                    400,
                    _page(
                        "Missing request_token",
                        "<p class='err'>No request_token in the redirect URL. Try Login with Kite again.</p>",
                    ),
                )
                return
            try:
                token = exchange_request_token(
                    api_key=api_key,
                    api_secret=api_secret,
                    request_token=request_token,
                )
            except Exception as exc:  # noqa: BLE001 — show in UI
                logger.exception("Token exchange failed")
                self._send(
                    500,
                    _page("Exchange failed", f"<p class='err'>{html.escape(str(exc))}</p>"),
                )
                result["error"] = str(exc)
                result["done"].set()
                return
            self._persist(token)
            self._send(200, _page("Connected", _success_body(token)))
            result["done"].set()

        def _persist(self, token: str) -> None:
            upsert_env(
                {
                    "KITE_ACCESS_TOKEN": token,
                    "FEED_MODE": "kite",
                }
            )
            result["token"] = token
            logger.info("Saved KITE_ACCESS_TOKEN to .env and set FEED_MODE=kite")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="kite-login-ui", daemon=True)
    thread.start()
    home = f"http://127.0.0.1:{port}/"
    logger.info("Login UI listening on %s", home)
    if open_browser:
        webbrowser.open(home)

    # Wait until the user finishes login/paste (or Ctrl+C).
    try:
        while not result["done"].wait(timeout=0.5):
            pass
    finally:
        server.shutdown()
        server.server_close()

    if result["error"] and not result["token"]:
        raise RuntimeError(result["error"])
    if not result["token"]:
        raise RuntimeError("Login UI closed without saving a token")
    return str(result["token"])

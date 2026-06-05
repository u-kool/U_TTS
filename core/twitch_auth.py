import http.server
import socket
import subprocess
import threading
import time
import urllib.parse
import webbrowser

import requests


DEFAULT_SCOPES = [
    "chat:read", "chat:edit",
    "channel:read:subscriptions", "channel:read:redemptions",
    "bits:read", "channel:read:hype_train",
    "channel:read:goals",
    "moderator:read:followers",
    "channel:manage:redemptions",
]

OAUTH_PORT = 3000
REDIRECT_URI = f"http://localhost:{OAUTH_PORT}/redirect/"


def _kill_process_on_port(port: int):
    """Kill any process listening on the given port (Windows)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and f":{port}" in parts[1] and "LISTENING" in parts[3]:
                pid = parts[4]
                print(f"Killing process {pid} on port {port}...")
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                return True
    except Exception:
        pass
    return False


def _is_port_open(port: int) -> bool:
    """Check if a port is available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


class TwitchAuth:
    def __init__(self, client_id: str, client_secret: str,
                 redirect_uri: str = REDIRECT_URI,
                 oauth_port: int = OAUTH_PORT):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.oauth_port = oauth_port

    def get_auth_url(self):
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(DEFAULT_SCOPES),
            "force_verify": "true",
        }
        return "https://id.twitch.tv/oauth2/authorize?" + urllib.parse.urlencode(params)

    def exchange_code_for_token(self, code):
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }
        r = requests.post("https://id.twitch.tv/oauth2/token", data=data, timeout=15)
        r.raise_for_status()
        token_data = r.json()
        refresh = token_data.get("refresh_token")
        if not refresh:
            import logging
            logging.getLogger(__name__).warning("Twitch returned no refresh_token in exchange_code_for_token")
        return token_data["access_token"], refresh

    def refresh_access_token(self, refresh_token):
        import logging
        logger = logging.getLogger(__name__)
        if not refresh_token:
            logger.warning("refresh_token is empty")
            return None, None
        try:
            r = requests.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": " ".join(DEFAULT_SCOPES),
                },
                timeout=15,
            )
            if r.status_code != 200:
                logger.warning(f"Twitch refresh failed: HTTP {r.status_code}, body={r.text[:200]}")
                return None, None
            token_data = r.json()
            new_refresh = token_data.get("refresh_token", refresh_token)
            if not new_refresh:
                logger.warning("Twitch returned no refresh_token in response")
            return token_data["access_token"], new_refresh
        except requests.RequestException as e:
            logger.warning(f"Twitch refresh request exception: {e}")
            return None, None

    def get_user_from_token(self, access_token):
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {access_token}",
        }
        r = requests.get("https://api.twitch.tv/helix/users", headers=headers, timeout=10)
        r.raise_for_status()
        users = r.json()["data"]
        if not users:
            raise Exception("Не удалось получить данные пользователя")
        return users[0]["id"], users[0]["login"]

    def perform_full_oauth(self):
        # Try to free port 3000 if it's still in use from a previous run
        if not _is_port_open(self.oauth_port):
            print(f"Port {self.oauth_port} is in use. Attempting to free it...")
            _kill_process_on_port(self.oauth_port)
            time.sleep(1)

        handler = self._make_handler()
        try:
            server = http.server.HTTPServer(("localhost", self.oauth_port), handler)
        except OSError as e:
            print(f"Failed to bind OAuth server on port {self.oauth_port}: {e}")
            print("Make sure no other process is using this port and try again.")
            return None, None, None, None

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        webbrowser.open(self.get_auth_url())
        if not handler.event.wait(timeout=120):
            server.shutdown()
            return None, None, None, None
        server.shutdown()

        if handler.error:
            return None, None, None, None

        try:
            access_token, refresh_token = self.exchange_code_for_token(handler.code)
            user_id, login = self.get_user_from_token(access_token)
            return access_token, user_id, login, refresh_token
        except Exception:
            return None, None, None, None

    def _make_handler(self):
        class OAuthHandler(http.server.BaseHTTPRequestHandler):
            code = None
            error = None
            event = threading.Event()

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if "code" in query:
                    OAuthHandler.code = query["code"][0]
                    self.send_response(200)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        "<html><body><h2>✅ Успешная авторизация!</h2>"
                        "<p>Можно закрыть окно.</p></body></html>".encode()
                    )
                    OAuthHandler.event.set()
                elif "error" in query:
                    OAuthHandler.error = query["error"][0]
                    self.send_response(400)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        f"<html><body><h2>❌ Ошибка: {OAuthHandler.error}</h2>"
                        f"</body></html>".encode()
                    )
                    OAuthHandler.event.set()
                else:
                    self.send_response(400)
                    self.end_headers()

            def log_message(self, format, *args):
                pass

        return OAuthHandler

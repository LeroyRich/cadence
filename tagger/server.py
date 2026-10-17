"""Small authenticated HTTP service for the DSM package."""

import argparse
import base64
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import secrets
import socket
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .artwork import fetch_artwork_url
from .formats import inspect_artwork
from .library import Library, LibraryError
from .metadata import MetadataClient
from .scheduler import DailyScanScheduler


WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SECURITY_HEADERS = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                    "script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SESSION_LIFETIME = 12 * 3600
REMEMBER_LIFETIME = 30 * 24 * 3600
SESSION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
MAX_PASSWORD_BYTES = 256
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_LOGIN_BYTES = 8192


class TooManyRequests(LibraryError):
    pass


class Application:
    def __init__(self, state_dir, allowed_volume=None, resume_jobs=True, secure_cookies=False):
        self.library = Library(state_dir, allowed_volume, resume_jobs=resume_jobs)
        self.metadata = MetadataClient(self.library)
        self.scheduler = DailyScanScheduler(self.library)
        self.secure_cookies = secure_cookies
        self.sessions = {}
        self.login_failures = []
        self.password_checks = threading.BoundedSemaphore(1)
        self.lock = threading.Lock()

    def set_password(self, password):
        self._validate_password(password)
        salt = os.urandom(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        with self.lock:
            self.library.set_password_and_revoke_sessions(salt.hex() + ":" + digest.hex())
            self.sessions.clear()
            self.login_failures.clear()

    @staticmethod
    def _validate_password(password):
        if not isinstance(password, str) or len(password) < 12 or len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError("Password must be 12 to 256 bytes long")

    def _check_password(self, password):
        if not isinstance(password, str) or len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError("Invalid password")
        stored = self.library.get_setting("password")
        if not stored:
            raise LibraryError("Administrator password is not configured")
        salt_hex, expected_hex = stored.split(":", 1)
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return stored if hmac.compare_digest(candidate, bytes.fromhex(expected_hex)) else None

    def login(self, password, address=None, remember_me=False):
        if not isinstance(remember_me, bool):
            raise LibraryError("Remember me must be true or false")
        now = time.monotonic()
        with self.lock:
            self.login_failures = [stamp for stamp in self.login_failures if now - stamp < 60]
            if len(self.login_failures) >= 20:
                raise TooManyRequests("Too many login attempts. Try again shortly")
        if not self.password_checks.acquire(blocking=False):
            raise TooManyRequests("Login is busy. Try again shortly")
        try:
            password_fingerprint = self._check_password(password)
            if not password_fingerprint:
                with self.lock:
                    self.login_failures.append(time.monotonic())
                raise LibraryError("Incorrect password")
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            with self.lock:
                if remember_me:
                    created = time.time()
                    self.library.remember_session(self._token_hash(token), csrf,
                                                  created, created + REMEMBER_LIFETIME)
                else:
                    self.sessions[token] = (time.time() + SESSION_LIFETIME, csrf, password_fingerprint)
                self.login_failures.clear()
            return token, csrf
        finally:
            self.password_checks.release()

    def change_password(self, current, replacement):
        self._validate_password(replacement)
        if not self.password_checks.acquire(blocking=False):
            raise TooManyRequests("Password change is busy. Try again shortly")
        try:
            if not self._check_password(current):
                raise PermissionError("Current password is incorrect")
            self.set_password(replacement)
        finally:
            self.password_checks.release()

    @staticmethod
    def _token_hash(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def session(self, token):
        if not isinstance(token, str) or not SESSION_TOKEN_PATTERN.fullmatch(token):
            return None
        now = time.time()
        with self.lock:
            value = self.sessions.get(token)
            if value and value[0] > now and value[2] == self.library.get_setting("password"):
                return value[1]
            self.sessions.pop(token, None)
            return self.library.remembered_session(self._token_hash(token), now)

    def logout(self, token):
        if not isinstance(token, str) or not SESSION_TOKEN_PATTERN.fullmatch(token):
            return
        with self.lock:
            self.sessions.pop(token, None)
            self.library.forget_session(self._token_hash(token))


class Handler(BaseHTTPRequestHandler):
    server_version = "NASMusicTagger/0.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, format_string, *args):
        logging.getLogger("cadence").info("%s %s", self.address_string(), format_string % args)

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", SECURITY_HEADERS)

    def _cookie(self, value, expired=False, remembered=False):
        name = "__Host-cadence_session" if self.app.secure_cookies else "tagger_session"
        if expired:
            attributes = "; Max-Age=0"
        elif remembered:
            attributes = "; Max-Age={}".format(REMEMBER_LIFETIME)
        else:
            attributes = ""
        attributes += "; Secure" if self.app.secure_cookies else ""
        return "{}={}; HttpOnly; SameSite=Strict; Path=/{}".format(name, value, attributes)

    def _send_json(self, value, status=200, cookie=None):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
            if self.app.secure_cookies:
                self.send_header("Set-Cookie", "tagger_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, mime):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_image(self, data, mime, etag):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("ETag", etag)
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_audio(self, source, mime):
        size = source.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or not any(match.groups()):
                return self._invalid_audio_range(size)
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                suffix = int(last)
                start = max(0, size - suffix)
            if start >= size or end < start or (not first and not suffix):
                return self._invalid_audio_range(size)
        length = max(0, end - start + 1)
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        if range_header:
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        self.end_headers()
        try:
            with source.open("rb") as audio:
                audio.seek(start)
                remaining = length
                while remaining:
                    chunk = audio.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _invalid_audio_range(self, size):
        self.send_response(416)
        self.send_header("Content-Range", "bytes */{}".format(size))
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _body(self):
        size = int(self.headers.get("Content-Length", "0"))
        limit = MAX_LOGIN_BYTES if self.path.split("?", 1)[0] in ("/api/login", "/api/password") else MAX_REQUEST_BYTES
        if size < 0 or size > limit:
            raise LibraryError("Request is too large")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise LibraryError("Use application/json")
        data = self.rfile.read(size)
        if len(data) != size:
            raise LibraryError("Incomplete request")
        body = json.loads(data or b"{}")
        if not isinstance(body, dict):
            raise LibraryError("Expected a JSON object")
        return body

    def _session(self, modify=False):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            name = "__Host-cadence_session" if self.app.secure_cookies else "tagger_session"
            token = cookie[name].value
        except (KeyError, ValueError):
            raise PermissionError("Sign in required")
        csrf = self.app.session(token)
        if not csrf:
            raise PermissionError("Session expired")
        if modify and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf):
            raise PermissionError("Invalid request token")
        return csrf

    def _handle(self, method):
        parsed = urlsplit(self.path)
        path = parsed.path
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            if method == "GET" and path in ("/", "/app.css", "/library.css", "/app.js", "/genres.js"):
                name = "index.html" if path == "/" else path[1:]
                mime = {"index.html": "text/html; charset=utf-8", "app.css": "text/css; charset=utf-8",
                        "library.css": "text/css; charset=utf-8",
                        "app.js": "application/javascript; charset=utf-8",
                        "genres.js": "application/javascript; charset=utf-8"}[name]
                return self._send_file(WEB_DIR / name, mime)
            if path == "/api/session" and method == "GET":
                try:
                    csrf = self._session()
                except PermissionError:
                    csrf = None
                return self._send_json({"authenticated": bool(csrf), "csrf": csrf,
                                        "configured": bool(self.app.library.get_setting("password"))})
            if path == "/api/login" and method == "POST":
                body = self._body()
                remember_me = body.get("remember_me", False)
                token, csrf = self.app.login(body.get("password", ""), remember_me=remember_me)
                return self._send_json({"csrf": csrf}, cookie=self._cookie(token, remembered=remember_me))
            self._session(modify=method != "GET")
            library = self.app.library
            if method == "GET":
                if path == "/api/genres":
                    return self._send_json(library.genres(query.get("q", "")))
                if path == "/api/genres/list":
                    return self._send_json(library.genre_choices(query.get("root", "")))
                if path == "/api/roots":
                    return self._send_json({"roots": library.roots()})
                if path == "/api/browse":
                    return self._send_json(library.browse(query.get("path")))
                if path in ("/api/empty-folders", "/api/empty-folders/paths"):
                    return self._send_json(library.empty_folders(query.get("q", ""), query.get("root", ""),
                        query.get("offset", 0), query.get("limit", 100), query.get("sort", "path"),
                        query.get("direction", "asc"), paths_only=path.endswith("/paths")))
                if path == "/api/tracks":
                    return self._send_json(library.tracks(query.get("q", ""), query.get("root", ""),
                                                          query.get("offset", "0"), query.get("limit", "100"),
                                                          query.get("sort", "path"), query.get("direction", "asc"),
                                                          query.get("filter", "all"), query.get("genre", "")))
                if path == "/api/tracks/paths":
                    return self._send_json(library.track_paths(query.get("q", ""), query.get("root", ""),
                                                                query.get("sort", "path"), query.get("direction", "asc"),
                                                                query.get("filter", "all"), query.get("genre", "")))
                if path == "/api/queue":
                    return self._send_json({"items": library.queue_items()})
                if path == "/api/queue/art":
                    return self._send_json(library.queue_artwork(query["path"]))
                if path == "/api/track":
                    return self._send_json(library.track(query["path"]))
                if path == "/api/audio":
                    track = library.track(query["path"])
                    source = Path(track["path"])
                    if source.is_symlink():
                        raise LibraryError("Audio file is no longer a regular indexed file")
                    mime = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
                            ".m4b": "audio/mp4", ".ogg": "audio/ogg", ".oga": "audio/ogg",
                            ".opus": "audio/ogg", ".wav": "audio/wav", ".aif": "audio/aiff",
                            ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma",
                            ".asf": "audio/x-ms-asf"}.get(source.suffix.lower(), "application/octet-stream")
                    return self._send_audio(source, mime)
                if path == "/api/art/image":
                    track = library.track(query["path"])
                    etag = '"{}-{}"'.format(track["size"], track["mtime_ns"])
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.send_header("Cache-Control", "private, max-age=300")
                        self.send_header("ETag", etag)
                        self.end_headers()
                        return
                    present, artwork = inspect_artwork(track["path"])
                    if present != track["artwork"]:
                        library.set_artwork_presence(track["path"], present)
                    if not artwork:
                        return self._send_json({"error": "Embedded artwork cannot be previewed" if present else "No artwork"}, 404)
                    data, mime = artwork
                    if mime not in ("image/jpeg", "image/png") or len(data) > 10 * 1024 * 1024:
                        raise LibraryError("Artwork is unsupported or too large")
                    return self._send_image(data, mime, etag)
                if path == "/api/art":
                    source = query["path"]
                    track = library.track(source)
                    present, artwork = inspect_artwork(source)
                    if present != track["artwork"]:
                        library.set_artwork_presence(source, present)
                    if not artwork:
                        return self._send_json({"error": "Embedded artwork cannot be previewed" if present else "No artwork"}, 404)
                    data, mime = artwork
                    if len(data) > 10 * 1024 * 1024:
                        raise LibraryError("Artwork too large")
                    return self._send_json({"data": base64.b64encode(data).decode("ascii"), "mime": mime})
                if path == "/api/jobs":
                    return self._send_json({"jobs": library.jobs()})
                if path == "/api/jobs/detail":
                    return self._send_json(library.job_details(query["id"], query.get("offset", 0),
                                                               query.get("limit", 50)))
                if path == "/api/settings":
                    return self._send_json({"contact_email": library.get_setting("contact_email"),
                                            "confirm_commit": library.get_setting("confirm_commit", "true") != "false",
                                            "auto_id3_upgrade": library.get_setting("auto_id3_upgrade", "true") != "false",
                                            "daily_scan_enabled": library.get_setting("daily_scan_enabled", "false") == "true",
                                            "acoustid_configured": bool(library.get_setting("acoustid_key"))})
            if method == "POST":
                body = self._body()
                if path == "/api/art/common":
                    return self._send_json(library.common_artwork(body.get("paths"), body.get("expected")))
                if path == "/api/tracks/common":
                    return self._send_json(library.common_tags(body.get("paths")))
                if path == "/api/logout":
                    cookie = SimpleCookie()
                    cookie.load(self.headers.get("Cookie", ""))
                    name = "__Host-cadence_session" if self.app.secure_cookies else "tagger_session"
                    self.app.logout(cookie[name].value)
                    return self._send_json({"ok": True}, cookie=self._cookie("", expired=True))
                if path == "/api/password":
                    self.app.change_password(body.get("current_password"), body.get("new_password"))
                    return self._send_json({"ok": True}, cookie=self._cookie("", expired=True))
                if path == "/api/roots":
                    return self._send_json(library.add_root_and_scan(body["path"]))
                if path == "/api/roots/remove":
                    library.remove_root(body["path"])
                    return self._send_json({"ok": True})
                if path == "/api/scan":
                    job_id = library.start_scan()
                    return self._send_json({"job": job_id, "state": library.job(job_id)["state"]})
                if path == "/api/cancel":
                    library.cancel_job(body["id"])
                    return self._send_json({"ok": True})
                if path == "/api/jobs/clear":
                    library.clear_job(body["id"])
                    return self._send_json({"ok": True})
                if path == "/api/jobs/clear-completed":
                    return self._send_json({"cleared": library.clear_completed_jobs()})
                if path == "/api/preview":
                    return self._send_json(library.preview(body["paths"], body.get("edits", {}),
                                                           body.get("mode", "none"), body.get("artwork"),
                                                           body.get("rename", "none"),
                                                           body.get("normalize_title", False),
                                                           body.get("remove_title_text", ""), body.get("import_tags", "none"),
                                                           genre_options=body.get("genre_options")))
                if path == "/api/empty-folders/refresh":
                    job_id = library.start_folder_scan(body.get("root", ""))
                    return self._send_json({"job": job_id, "state": library.job(job_id)["state"]})
                if path == "/api/queue/add":
                    return self._send_json(library.queue_add(body["paths"], body.get("edits", {}),
                                                             body.get("mode"), body.get("artwork"), body.get("rename"),
                                                             body.get("revisions"), body.get("normalize_title", False),
                                                             body.get("remove_title_text", ""), body.get("import_tags", "none"),
                                                             genre_options=body.get("genre_options"), delete=body.get("delete"),
                                                             item_kind=body.get("item_kind", "track")))
                if path == "/api/queue/remove":
                    return self._send_json(library.queue_remove(body["path"]))
                if path == "/api/queue/clear":
                    return self._send_json(library.queue_clear())
                if path == "/api/queue/preview":
                    return self._send_json(library.preview_queue(body.get("pending")))
                if path == "/api/commit":
                    job_id = library.start_commit(body["id"])
                    return self._send_json({"job": job_id, "state": library.job(job_id)["state"],
                                            "queue": library.queue_items()})
                if path == "/api/settings":
                    if "confirm_commit" in body and not isinstance(body["confirm_commit"], bool):
                        raise LibraryError("Invalid commit confirmation option")
                    if "auto_id3_upgrade" in body and not isinstance(body["auto_id3_upgrade"], bool):
                        raise LibraryError("Invalid ID3 upgrade setting")
                    if "daily_scan_enabled" in body and not isinstance(body["daily_scan_enabled"], bool):
                        raise LibraryError("Invalid daily scan setting")
                    email = body.get("contact_email", "").strip()
                    if email and ("@" not in email or len(email) > 255):
                        raise LibraryError("Enter a valid contact email")
                    library.set_setting("contact_email", email)
                    if "acoustid_key" in body:
                        key = body["acoustid_key"].strip()
                        if len(key) > 200:
                            raise LibraryError("AcoustID key is too long")
                        library.set_setting("acoustid_key", key)
                    if "confirm_commit" in body:
                        library.set_setting("confirm_commit", "true" if body["confirm_commit"] else "false")
                    if "auto_id3_upgrade" in body:
                        library.set_setting("auto_id3_upgrade", "true" if body["auto_id3_upgrade"] else "false")
                    if "daily_scan_enabled" in body:
                        library.set_setting("daily_scan_enabled", "true" if body["daily_scan_enabled"] else "false")
                        scheduler = getattr(self.app, "scheduler", None)
                        if scheduler is not None:
                            scheduler.notify()
                    return self._send_json({"ok": True})
                if path == "/api/match":
                    return self._send_json({"candidates": self.app.metadata.search(body["path"], bool(body.get("fingerprint")))})
                if path == "/api/cover":
                    data, mime = self.app.metadata.cover(body["release_id"])
                    return self._send_json({"data": base64.b64encode(data).decode("ascii"), "mime": mime})
                if path == "/api/art/url":
                    data, mime = fetch_artwork_url(body.get("url"))
                    return self._send_json({"data": base64.b64encode(data).decode("ascii"), "mime": mime})
            return self._send_json({"error": "Not found"}, 404)
        except PermissionError as error:
            return self._send_json({"error": str(error)}, 403)
        except TooManyRequests as error:
            return self._send_json({"error": str(error)}, 429)
        except (LibraryError, ValueError, KeyError, TypeError, FileNotFoundError, json.JSONDecodeError) as error:
            return self._send_json({"error": str(error)}, 400)
        except socket.timeout:
            return self._send_json({"error": "Request timed out"}, 408)
        except Exception as error:
            self.log_error("Request failed: %s", error)
            return self._send_json({"error": "Server error"}, 500)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=os.environ.get("TAGGER_STATE_DIR", "./state"))
    parser.add_argument("--allowed-volume", default=os.environ.get("TAGGER_VOLUME"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--set-password", action="store_true")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--https-proxy", action="store_true")
    parser.add_argument("--insecure-local-http", action="store_true")
    args = parser.parse_args()
    if args.insecure_local_http and (args.https_proxy or args.host not in ("127.0.0.1", "::1", "localhost")):
        parser.error("Insecure HTTP is allowed only on loopback without the HTTPS proxy")
    os.umask(0o077)
    app = Application(args.state_dir, args.allowed_volume,
                      resume_jobs=not (args.set_password or args.password_stdin),
                      secure_cookies=not args.insecure_local_http)
    if args.set_password or args.password_stdin:
        if args.password_stdin:
            password = sys.stdin.read().rstrip("\n")
        else:
            import getpass
            password = getpass.getpass("Administrator password: ")
        app.set_password(password)
        return
    if not app.library.get_setting("password"):
        parser.error("Set an administrator password with --set-password before starting")
    if args.https_proxy:
        logger = logging.getLogger("cadence")
        logger.setLevel(logging.INFO)
        log_path = app.library.state_dir / "server.log"
        if log_path.is_symlink():
            parser.error("Server log cannot be a symbolic link")
        if log_path.exists() and log_path.stat().st_uid == os.geteuid():
            log_path.chmod(0o600)
        handler = RotatingFileHandler(str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        if sys.version_info < (3, 10):
            logger.warning("Python %s.%s is past upstream support; use a supported interpreter when available",
                           sys.version_info.major, sys.version_info.minor)
    server = BoundedHTTPServer((args.host, args.port), Handler)
    server.app = app
    message = "NAS Music Tagger listening on {}:{}".format(args.host, args.port)
    if args.https_proxy:
        logger.info(message)
    else:
        print(message, flush=True)
    app.scheduler.start()
    try:
        server.serve_forever()
    finally:
        app.scheduler.stop()


if __name__ == "__main__":
    main()

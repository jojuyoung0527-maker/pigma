"""Loopback-only SourceFlow 0.6 download queue and file UI."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hmac
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from downloader import VERSION, DownloadError, extract_urls, validate_cookies, MAX_COOKIE_BYTES, MAX_FILE_BYTES, MIN_FREE

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
PORT = int(os.getenv("PORT", "8080"))
CLOUD = os.getenv("SOURCEFLOW_CLOUD", "0") == "1"
HOST = "0.0.0.0" if CLOUD else "127.0.0.1"
CSRF = secrets.token_urlsafe(32)
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=2)
CANCEL = {}
ACTIVE = {"queued", "resolving", "downloading", "merging", "verifying", "cancelling"}
TERMINAL = {"completed", "failed", "cancelled"}


def db():
    connection = sqlite3.connect(DATA / "downloads.sqlite3", timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def init():
    if CLOUD:
        import static_ffmpeg
        static_ffmpeg.add_paths(weak=True)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "downloads").mkdir(exist_ok=True)
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS downloads(id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS batches(request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, ids TEXT NOT NULL)")
        for row in c.execute("SELECT id,data FROM downloads").fetchall():
            item = json.loads(row["data"])
            if item.get("status") in ACTIVE:
                item.update(status="failed", error={"code": "INTERRUPTED", "message": "프로그램이 종료돼 작업이 중단됐습니다. 다시 시도할 수 있습니다."})
                c.execute("UPDATE downloads SET data=? WHERE id=?", (json.dumps(item, ensure_ascii=False), item["id"]))
    # Credentials for an interrupted download are never retained on restart.
    for pattern in ("*/session.cookies.txt", "*/worker.json"):
        for p in (DATA / "downloads").glob(pattern):
            p.unlink(missing_ok=True)


def get(jid):
    with db() as c:
        row = c.execute("SELECT data FROM downloads WHERE id=?", (jid,)).fetchone()
    if not row:
        raise DownloadError("NOT_FOUND", "작업을 찾지 못했습니다.")
    return json.loads(row["data"])


def update(jid, **fields):
    with LOCK:
        item = get(jid)
        item.update(fields, updated=time.time())
        with db() as c:
            c.execute("UPDATE downloads SET data=? WHERE id=?", (json.dumps(item, ensure_ascii=False), jid))
    return item


def file_path(item):
    raw = item.get("result", {}).get("path")
    if not isinstance(raw, str):
        return None
    p = Path(raw).resolve()
    if not p.is_relative_to((DATA / "downloads" / item["id"]).resolve()) or not p.is_file():
        return None
    return p


def public(item):
    result = {k: v for k, v in item.items() if k not in {"cookie", "result"}}
    result["result"] = {k: v for k, v in item.get("result", {}).items() if k != "path"}
    result["fileReady"] = item["status"] == "completed" and file_path(item) is not None
    result["fileUrl"] = f"/v1/downloads/{item['id']}/file" if result["fileReady"] else None
    return result


def list_items():
    with db() as c:
        rows = c.execute("SELECT data FROM downloads ORDER BY created DESC LIMIT 300").fetchall()
    return [json.loads(r["data"]) for r in rows]


def create(payload):
    if not isinstance(payload, dict) or set(payload) - {"text", "request_id", "cookie_text"}:
        raise DownloadError("INVALID_REQUEST", "다운로드 요청 형식이 올바르지 않습니다.")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
        raise DownloadError("INVALID_REQUEST", "요청 식별자가 필요합니다.")
    urls = extract_urls(payload.get("text"))
    cookie = payload.get("cookie_text")
    if CLOUD and cookie is not None:
        raise DownloadError("LOGIN_DISABLED", "공개 베타에서는 로그인 쿠키 업로드를 지원하지 않습니다. 공개적으로 볼 수 있는 영상 링크를 사용해 주세요.")
    if cookie is not None:
        validate_cookies(cookie)
    # Never store cookies or their hashes in the database/idempotency record.
    fingerprint = json.dumps(urls, ensure_ascii=False)
    with LOCK:
        with db() as c:
            old = c.execute("SELECT payload,ids FROM batches WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["payload"] != fingerprint:
                    raise DownloadError("CONFLICT", "다른 요청에 사용된 식별자입니다.")
                return [public(get(jid)) for jid in json.loads(old["ids"])]
        current = list_items()
        if len([x for x in current if x["status"] in ACTIVE]) + len(urls) > 20:
            raise DownloadError("QUEUE_FULL", "최대 20개까지 대기할 수 있습니다. 진행 중인 작업이 끝난 뒤 추가해 주세요.")
        if len(current) + len(urls) > 300:
            raise DownloadError("HISTORY_FULL", "작업 기록이 300개입니다. 필요 없는 기록을 삭제한 뒤 추가해 주세요.")
        if shutil.disk_usage(DATA).free < MIN_FREE:
            raise DownloadError("DISK_FULL", "저장 공간이 부족합니다.")
        items = []
        try:
            for url in urls:
                jid = secrets.token_hex(16)
                folder = DATA / "downloads" / jid
                folder.mkdir()
                items.append({"id": jid, "url": url, "title": "영상 정보 확인 대기", "status": "queued", "created": time.time(), "percent": None})
                if cookie is not None:
                    cp = folder / "session.cookies.txt"
                    with cp.open("x", encoding="utf-8") as f:
                        os.chmod(cp, 0o600)
                        f.write(cookie)
            with db() as c:
                c.executemany("INSERT INTO downloads VALUES(?,?,?)", [(x["id"], json.dumps(x, ensure_ascii=False), x["created"]) for x in items])
                c.execute("INSERT INTO batches VALUES(?,?,?)", (request_id, fingerprint, json.dumps([x["id"] for x in items])))
        except Exception:
            for x in items:
                shutil.rmtree(DATA / "downloads" / x["id"], ignore_errors=True)
            raise
        for item in items:
            CANCEL[item["id"]] = threading.Event()
            POOL.submit(run_job, item["id"])
    return [public(x) for x in items]


def stop_tree(process):
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=10, creationflags=0x08000000)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def worker_command(path):
    if os.getenv("SOURCEFLOW_DESKTOP") == "1":
        return [sys.executable, "-B", "-X", "utf8", str(ROOT / "desktop_launch.py"), "--download-worker", "--job", str(path)]
    return [sys.executable, "-B", str(ROOT / "downloader.py"), "--job", str(path)]


def run_job(jid):
    folder = DATA / "downloads" / jid
    cookie = folder / "session.cookies.txt"
    descriptor = folder / "worker.json"
    process = None
    cancelled = CANCEL[jid]
    try:
        if cancelled.is_set():
            update(jid, status="cancelled")
            return
        job = update(jid, status="resolving", error=None)
        descriptor.write_text(json.dumps({"url": job["url"]}, ensure_ascii=False), encoding="utf-8")
        os.chmod(descriptor, 0o600)
        kwargs = {"creationflags": 0x08000000 | subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
        process = subprocess.Popen(worker_command(descriptor), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   encoding="utf-8", errors="replace", cwd=str(ROOT), **kwargs)
        events = queue.Queue(maxsize=256)
        def reader():
            try:
                while line := process.stdout.readline(65537):
                    if len(line) > 65536:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        events.put(event)
            finally:
                events.put(None)
        threading.Thread(target=reader, daemon=True).start()
        started = time.monotonic()
        final = None
        error = None
        while True:
            if cancelled.is_set():
                stop_tree(process)
                update(jid, status="cancelled")
                return
            if time.monotonic() - started > 6 * 3600:
                raise DownloadError("TIMEOUT", "6시간 다운로드 제한을 넘었습니다. 다시 시도해 주세요.")
            if shutil.disk_usage(DATA).free < MIN_FREE:
                raise DownloadError("DISK_FULL", "저장 공간이 부족해 작업을 중단했습니다.")
            try:
                event = events.get(timeout=.5)
            except queue.Empty:
                if process.poll() is not None and not process.stdout:
                    break
                continue
            if event is None:
                break
            kind = event.pop("event", "")
            if kind == "metadata":
                update(jid, **event)
            elif kind == "progress":
                stage = event.pop("stage", "downloading")
                if stage in ACTIVE:
                    update(jid, status=stage, **event)
            elif kind == "complete":
                final = event.get("result")
            elif kind == "error":
                error = {"code": event.get("code"), "message": event.get("message")}
        process.wait(timeout=15)
        if cancelled.is_set():
            update(jid, status="cancelled")
        elif process.returncode == 0 and isinstance(final, dict) and file_path({"id": jid, "result": final}):
            update(jid, status="completed", result=final, title=final.get("title", "영상"), percent=100, error=None)
        else:
            update(jid, status="failed", error=error or {"code": "WORKER_FAILED", "message": "다운로드가 정상적으로 완료되지 않았습니다. 다시 시도해 주세요."})
    except Exception as exc:
        update(jid, status="failed", error={"code": exc.code, "message": str(exc)} if isinstance(exc, DownloadError) else {"code": "DOWNLOAD_FAILED", "message": "다운로드 중 오류가 발생했습니다. 다시 시도해 주세요."})
    finally:
        if process is not None:
            stop_tree(process)
            if process.stdout is not None:
                process.stdout.close()
        cookie.unlink(missing_ok=True)
        descriptor.unlink(missing_ok=True)
        # Incomplete streams are never offered as finished files.
        if get(jid)["status"] != "completed":
            for path in folder.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
        CANCEL.pop(jid, None)


def retry(jid, payload):
    if not isinstance(payload, dict) or set(payload) - {"cookie_text"}:
        raise DownloadError("INVALID_REQUEST", "다시 시도 요청이 올바르지 않습니다.")
    cookie = payload.get("cookie_text")
    if CLOUD and cookie is not None:
        raise DownloadError("LOGIN_DISABLED", "공개 베타에서는 로그인 쿠키 업로드를 지원하지 않습니다. 공개적으로 볼 수 있는 영상 링크를 사용해 주세요.")
    if cookie is not None:
        validate_cookies(cookie)
    with LOCK:
        item = get(jid)
        if item["status"] not in {"failed", "cancelled"} or jid in CANCEL:
            raise DownloadError("BUSY", "진행 중이거나 완료된 작업은 다시 시작할 수 없습니다.")
        if sum(x["status"] in ACTIVE for x in list_items()) >= 20:
            raise DownloadError("QUEUE_FULL", "다운로드 대기열이 가득 찼습니다.")
        if cookie is not None:
            path = DATA / "downloads" / jid / "session.cookies.txt"
            path.write_text(cookie, encoding="utf-8")
            os.chmod(path, 0o600)
        update(jid, status="queued", error=None, percent=None, received=0, total=None)
        CANCEL[jid] = threading.Event()
        POOL.submit(run_job, jid)
    return public(get(jid))


class Handler(BaseHTTPRequestHandler):
    server_version = "SourceFlow/0.6"
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass

    def authenticate(self, write=False):
        if CLOUD:
            # Public page navigation may legitimately arrive from ChatGPT,
            # search engines, bookmarks, email, etc. Cross-site navigation is
            # therefore allowed for read-only GET requests. State-changing API
            # requests remain same-origin and CSRF protected.
            if write:
                host = self.headers.get("Host", "")
                origin = self.headers.get("Origin")
                if origin and origin not in {f"https://{host}", f"http://{host}"}:
                    raise DownloadError("FORBIDDEN", "다른 사이트의 요청은 허용하지 않습니다.")
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    raise DownloadError("FORBIDDEN", "다른 사이트의 요청은 허용하지 않습니다.")
        else:
            origins = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
            if self.client_address[0] != "127.0.0.1" or self.headers.get("Host") not in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}:
                raise DownloadError("FORBIDDEN", "이 PC에서만 사용할 수 있습니다.")
            if self.headers.get("Origin") and self.headers["Origin"] not in origins:
                raise DownloadError("FORBIDDEN", "다른 사이트의 요청은 허용하지 않습니다.")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise DownloadError("FORBIDDEN", "다른 사이트의 요청은 허용하지 않습니다.")
        if write and not hmac.compare_digest(self.headers.get("X-SourceFlow-Token", ""), CSRF):
            raise DownloadError("FORBIDDEN", "화면을 새로 고친 뒤 다시 시도해 주세요.")

    def headers_for(self, status, mime, length):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")

    def send_json(self, data, status=200):
        content = json.dumps(data, ensure_ascii=False).encode()
        self.headers_for(status, "application/json; charset=utf-8", len(content))
        self.end_headers()
        self.wfile.write(content)

    def fail(self, exc):
        code = exc.code if isinstance(exc, DownloadError) else "SERVICE_ERROR"
        message = str(exc) if isinstance(exc, DownloadError) else "요청을 처리하지 못했습니다. 다시 시도해 주세요."
        if code == "AUTH_REQUIRED":
            content = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", 'Basic realm="SourceFlow Test"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_json({"error": {"code": code, "message": message}}, {"NOT_FOUND": 404, "FORBIDDEN": 403, "BUSY": 409, "CONFLICT": 409, "QUEUE_FULL": 429}.get(code, 400))

    def body(self):
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise DownloadError("INVALID_REQUEST", "JSON 요청만 허용합니다.")
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= MAX_COOKIE_BYTES * 2:
            raise DownloadError("INVALID_REQUEST", "요청 크기가 올바르지 않습니다.")
        content = self.rfile.read(length)
        if len(content) != length:
            raise DownloadError("INVALID_REQUEST", "요청이 완성되지 않았습니다.")
        try:
            return json.loads(content)
        except ValueError:
            raise DownloadError("INVALID_REQUEST", "요청 형식이 올바르지 않습니다.") from None

    def do_GET(self):
        try:
            self.authenticate()
            path = urllib.parse.urlsplit(self.path).path
            static = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
            if path in static:
                p = ROOT / "web" / static[path]
                content = p.read_bytes()
                self.headers_for(200, mimetypes.guess_type(str(p))[0] + "; charset=utf-8", len(content))
                self.end_headers()
                self.wfile.write(content)
                return
            if path in {"/health", "/v1/health"}:
                from yt_dlp.version import __version__
                return self.send_json({"ok": True, "provider": "cloud-downloader" if CLOUD else "local-downloader", "version": VERSION, "engineVersion": __version__, "maxFileBytes": MAX_FILE_BYTES})
            if path == "/v1/bootstrap":
                return self.send_json({"token": CSRF, "version": VERSION})
            if path == "/v1/downloads":
                return self.send_json({"downloads": [public(x) for x in list_items()], "freeBytes": shutil.disk_usage(DATA).free})
            m = re.fullmatch(r"/v1/downloads/([a-f0-9]{32})/file", path)
            if m:
                item = get(m[1])
                p = file_path(item)
                if item["status"] != "completed" or p is None:
                    raise DownloadError("NOT_FOUND", "완료된 영상 파일이 없습니다.")
                filename = urllib.parse.quote(item["result"]["filename"], safe="")
                self.headers_for(200, "application/octet-stream", p.stat().st_size)
                self.send_header("Content-Disposition", "attachment; filename=SourceFlow-video." + p.suffix[1:] + "; filename*=UTF-8''" + filename)
                self.end_headers()
                with p.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1024 * 1024)
                return
            raise DownloadError("NOT_FOUND", "페이지를 찾지 못했습니다.")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.fail(exc)

    def do_POST(self):
        try:
            self.authenticate(write=True)
            payload = self.body()
            path = urllib.parse.urlsplit(self.path).path
            if path == "/v1/downloads":
                return self.send_json({"downloads": create(payload)}, 202)
            m = re.fullmatch(r"/v1/downloads/([a-f0-9]{32})/(cancel|retry)", path)
            if not m:
                raise DownloadError("NOT_FOUND", "요청을 찾지 못했습니다.")
            if m[2] == "retry":
                return self.send_json(retry(m[1], payload), 202)
            with LOCK:
                item = get(m[1])
                if item["status"] in ACTIVE and m[1] in CANCEL:
                    CANCEL[m[1]].set()
                    update(m[1], status="cancelling")
            self.send_json(public(get(m[1])))
        except Exception as exc:
            self.fail(exc)

    def do_DELETE(self):
        try:
            self.authenticate(write=True)
            m = re.fullmatch(r"/v1/downloads/([a-f0-9]{32})", urllib.parse.urlsplit(self.path).path)
            if not m:
                raise DownloadError("NOT_FOUND", "작업을 찾지 못했습니다.")
            with LOCK:
                item = get(m[1])
                if item["status"] in ACTIVE or m[1] in CANCEL:
                    raise DownloadError("BUSY", "작업이 끝나거나 취소된 뒤 삭제해 주세요.")
                shutil.rmtree(DATA / "downloads" / m[1], ignore_errors=False)
                with db() as c:
                    c.execute("DELETE FROM downloads WHERE id=?", (m[1],))
                    c.execute("DELETE FROM batches WHERE ids LIKE ?", ("%" + m[1] + "%",))
            self.send_json({"deleted": True})
        except Exception as exc:
            self.fail(exc)


def main():
    init()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        for event in list(CANCEL.values()):
            event.set()
        POOL.shutdown(wait=True)
        server.server_close()


if __name__ == "__main__":
    main()

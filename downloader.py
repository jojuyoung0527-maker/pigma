"""SourceFlow's local video downloader. No model, search or paid API.

Only dedicated yt-dlp extractors and explicitly recognised share redirects are
accepted. A Generic extractor is never used to fetch an arbitrary input URL.
"""
from pathlib import Path
import argparse
import html
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.6.0"
MAX_FILE_BYTES = int(os.getenv("SOURCEFLOW_MAX_FILE_BYTES", str(20 * 1024**3)))
MIN_FREE = 512 * 1024**2
MAX_COOKIE_BYTES = 1024 * 1024
SHARE_HOSTS = {
    "xhslink.com": {"xhslink.com", "xiaohongshu.com"},
    "b23.tv": {"b23.tv", "bilibili.com"},
    "v.douyin.com": {"douyin.com", "iesdouyin.com"},
    "vm.tiktok.com": {"tiktok.com"}, "vt.tiktok.com": {"tiktok.com"},
    "t.co": {"t.co", "twitter.com", "x.com"},
}


class DownloadError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def public_url(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 8192 or re.search(r"[\x00-\x20\x7f\\]", raw):
        raise DownloadError("INVALID_URL", "올바른 영상 링크를 넣어 주세요.")
    try:
        u = urllib.parse.urlsplit(raw)
        host = (u.hostname or "").lower().rstrip(".")
        if u.scheme not in {"http", "https"} or not host or u.username or u.password or u.port:
            raise ValueError()
        if "." not in host or host.endswith((".local", ".localhost", ".internal", ".test", ".invalid")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:  # Product accepts platform names, not raw IPs.
            raise ValueError()
        host = host.encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError()
        return urllib.parse.urlunsplit((u.scheme, host, u.path or "/", u.query, ""))
    except (ValueError, UnicodeError):
        raise DownloadError("INVALID_URL", "공개 플랫폼의 영상 주소를 넣어 주세요. 내부 주소는 지원하지 않습니다.") from None


def extract_urls(text):
    """Accept Chinese app share text as well as one URL per line."""
    if not isinstance(text, str) or len(text) > 32000:
        raise DownloadError("INVALID_URL", "한 번에 최대 20개의 링크를 넣어 주세요.")
    urls = []
    for match in re.findall(r"https?://[^\s<>\"'，。；！？【】《》]+", html.unescape(text)):
        url = public_url(match.rstrip(")]}.,;!"))
        if url not in urls:
            urls.append(url)
    if not urls or len(urls) > 20:
        raise DownloadError("INVALID_URL", "영상 링크를 1~20개 넣어 주세요. 앱의 공유 문구도 붙여 넣을 수 있습니다.")
    return urls


def belongs(host, roots):
    return any(host == r or host.endswith("." + r) for r in roots)


def check_dns(url):
    host = urllib.parse.urlsplit(public_url(url)).hostname
    # Configured HTTPS proxies resolve upstream DNS themselves. They are never
    # supplied by the web UI or by a job. Still apply syntactic host validation.
    if urllib.request.getproxies().get("https"):
        return
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise DownloadError("NETWORK_ERROR", "플랫폼에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.") from None
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise DownloadError("INVALID_URL", "내부 네트워크 주소는 열 수 없습니다.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_share(url, opener=None):
    url = public_url(url)
    host = urllib.parse.urlsplit(url).hostname
    allowed = SHARE_HOSTS.get(host)
    if not allowed:
        return canonical_url(url)
    opener = opener or urllib.request.build_opener(NoRedirect())
    deadline = time.monotonic() + 35
    for _ in range(6):
        u = urllib.parse.urlsplit(url)
        if not belongs(u.hostname, allowed):
            raise DownloadError("UNSAFE_REDIRECT", "공유 링크가 다른 사이트로 이동했습니다. 영상의 전체 주소를 넣어 주세요.")
        check_dns(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadError("TIMEOUT", "공유 링크를 여는 시간이 초과됐습니다.")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=min(15, remaining)) as response:
                return canonical_url(public_url(response.url))
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise DownloadError("SHARE_UNAVAILABLE", "공유 링크를 열지 못했습니다. 앱에서 링크를 다시 복사하거나 전체 영상 주소를 넣어 주세요.") from None
            url = public_url(urllib.parse.urljoin(url, exc.headers.get("Location", "")))
    raise DownloadError("SHARE_UNAVAILABLE", "공유 링크 이동이 너무 많습니다. 전체 영상 주소를 넣어 주세요.")


def canonical_url(url):
    u = urllib.parse.urlsplit(url)
    host = u.hostname
    if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        # xsec_token is required by some shared notes; never strip this query.
        path = re.sub(r"^/user/profile/[^/]+/([a-f0-9]+)$", r"/explore/\1", u.path)
        return urllib.parse.urlunsplit(("https", "www.xiaohongshu.com", path, u.query, ""))
    if host in {"iesdouyin.com", "www.iesdouyin.com"}:
        m = re.search(r"/share/video/(\d+)", u.path)
        if m:
            return "https://www.douyin.com/video/" + m[1]
    return url


def extractor_name(url):
    from yt_dlp.globals import plugin_dirs
    plugin_dirs.value = []
    from yt_dlp.extractor import gen_extractor_classes
    for cls in gen_extractor_classes():
        if cls.ie_key() not in {"Generic", "UnsupportedURL", "KnownDRM", "KnownPiracy", "KnownLiability"} and cls.suitable(url):
            return cls.ie_key()
    raise DownloadError("UNSUPPORTED_PLATFORM", "이 링크를 처리할 전용 다운로드 기능이 아직 없습니다. 지원 플랫폼의 개별 영상 링크를 사용해 주세요.")


def validate_cookies(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_COOKIE_BYTES or "\x00" in text:
        raise DownloadError("INVALID_COOKIES", "cookies.txt는 1MB 이하의 Netscape 형식 파일이어야 합니다.")
    if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise DownloadError("INVALID_COOKIES", "Netscape 형식의 cookies.txt 파일을 선택해 주세요.")
    for line in text.splitlines():
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        if len(line.split("\t")) != 7:
            raise DownloadError("INVALID_COOKIES", "cookies.txt 형식을 읽지 못했습니다.")
    return text


def safe_name(title, ext):
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(title or "SourceFlow-video"))
    title = title.strip(" .")[:100] or "SourceFlow-video"
    if title.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}:
        title = "SourceFlow-" + title
    ext = ext.lower() if re.fullmatch(r"[a-zA-Z0-9]{1,8}", ext or "") else "mkv"
    return title + "." + ext


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def error_detail(text):
    t = str(text).lower()
    if "drm" in t:
        return {"code": "DRM", "message": "암호화 보호된 영상은 저장할 수 없습니다."}
    if any(x in t for x in ["cookie", "login", "log in", "sign in", "登录", "age-restricted", "members-only"]):
        return {"code": "LOGIN_REQUIRED", "message": "플랫폼이 로그인 정보를 요구합니다. 로그인한 본인 계정의 cookies.txt를 선택한 뒤 다시 시도해 주세요."}
    if any(x in t for x in ["captcha", "bot", "429", "403", "forbidden", "412", "risk control"]):
        return {"code": "ACCESS_LIMITED", "message": "플랫폼이 현재 접근을 제한했습니다. 브라우저에서 영상이 열리는지 확인하고 잠시 후 재시도해 주세요."}
    if any(x in t for x in ["404", "not available", "unavailable", "removed", "not found"]):
        return {"code": "UNAVAILABLE", "message": "영상이 삭제됐거나 현재 지역·계정에서 열 수 없습니다."}
    if any(x in t for x in ["unsupported url", "no suitable extractor"]):
        return {"code": "UNSUPPORTED_PLATFORM", "message": "아직 지원하지 않는 링크입니다. 플랫폼의 개별 영상 주소인지 확인해 주세요."}
    if "no video formats" in t or "only images" in t:
        return {"code": "NO_VIDEO", "message": "저장 가능한 영상이 없습니다. 사진 게시물 또는 로그인·접근 제한일 수 있습니다."}
    if "requested format" in t:
        return {"code": "FORMAT_UNAVAILABLE", "message": "요청한 영상 형식을 제공하지 않습니다. 링크와 로그인 상태를 확인해 주세요."}
    if any(x in t for x in ["timed out", "timeout", "unable to download webpage", "resolve"]):
        return {"code": "NETWORK_ERROR", "message": "플랫폼 응답을 받지 못했습니다. 네트워크를 확인하고 다시 시도해 주세요."}
    return {"code": "DOWNLOAD_FAILED", "message": "영상 다운로드에 실패했습니다. 공유 링크를 새로 복사하거나 로그인 정보를 추가해 다시 시도해 주세요."}


def probe_video(path):
    options = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                            "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate", "-of", "json", str(path)],
                           capture_output=True, timeout=40, **options)
        if p.returncode:
            raise ValueError()
        data = json.loads(p.stdout)
        video = next(s for s in data["streams"] if s.get("codec_type") == "video")
        if not video.get("width") or not video.get("height"):
            raise ValueError()
        return {"width": video["width"], "height": video["height"], "codec": video.get("codec_name"),
                "duration": number(float(data.get("format", {}).get("duration", 0))),
                "hasAudio": any(s.get("codec_type") == "audio" for s in data["streams"]), "bytes": path.stat().st_size}
    except (OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired):
        raise DownloadError("INVALID_MEDIA", "완성된 영상 파일을 확인하지 못했습니다. 오류 페이지나 불완전한 파일은 저장 완료로 표시하지 않습니다.") from None


def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False, allow_nan=False), flush=True)


def download_one(url, folder, cookie_file=None, callback=emit):
    from yt_dlp.globals import plugin_dirs
    plugin_dirs.value = []
    from yt_dlp import YoutubeDL
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    callback("progress", stage="resolving", message="플랫폼과 공유 링크 확인 중")
    url = resolve_share(url)
    key = extractor_name(url)
    check_dns(url)
    if shutil.disk_usage(folder).free < MIN_FREE:
        raise DownloadError("DISK_FULL", "저장 공간이 부족합니다. 최소 512MB의 여유 공간을 확보해 주세요.")
    latest = [0.0]
    expected_audio = [False]

    class Logger:
        def debug(self, message):
            pass
        def warning(self, message):
            # Error diagnostics can contain cookies and signed CDN URLs.
            pass
        def error(self, message):
            pass

    def hook(d):
        status = d.get("status")
        info = d.get("info_dict") or {}
        if status == "downloading" and time.monotonic() - latest[0] < .4:
            return
        latest[0] = time.monotonic()
        total = number(d.get("total_bytes") or d.get("total_bytes_estimate"))
        received = number(d.get("downloaded_bytes")) or 0
        if received > MAX_FILE_BYTES or (total and total > MAX_FILE_BYTES):
            raise DownloadError("FILE_LIMIT", "영상이 현재 파일당 20GB 한도를 넘습니다. 화질을 낮춰 저장하지 않았습니다.")
        if shutil.disk_usage(folder).free < MIN_FREE:
            raise DownloadError("DISK_FULL", "저장 공간이 부족해 다운로드를 중단했습니다.")
        callback("progress", stage="downloading" if status == "downloading" else "merging",
                 title=str(info.get("title") or "영상")[:300], platform=key,
                 received=received, total=total, speed=number(d.get("speed")), eta=number(d.get("eta")),
                 percent=min(99, received * 100 / total) if total else None)

    def post_hook(d):
        callback("progress", stage="merging", message="최고 화질 영상과 오디오 결합 중")

    def match_filter(info, *, incomplete=False):
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            return "현재 진행 중인 라이브 방송은 지원하지 않습니다. 종료된 영상 링크를 사용해 주세요."
        return None

    opts = {"format": "bv*+ba/b", "outtmpl": str(folder / "video.%(ext)s"),
            "merge_output_format": "mkv", "noplaylist": True, "playlistend": 1,
            "socket_timeout": 20, "retries": 2, "fragment_retries": 2, "extractor_retries": 1,
            "skip_unavailable_fragments": False, "continuedl": True,
            "quiet": True, "no_warnings": True, "noprogress": True, "logger": Logger(),
            "progress_hooks": [hook], "postprocessor_hooks": [post_hook],
            "match_filter": match_filter, "max_filesize": MAX_FILE_BYTES,
            "cachedir": False, "js_runtimes": {"node": {}}, "remote_components": set(),
            "compat_opts": {"no-certifi"}, "windowsfilenames": True,
            "hls_prefer_native": True, "overwrites": True,
            "extract_flat": False, "cookiefile": str(cookie_file) if cookie_file else None}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise DownloadError("NO_VIDEO", "영상 정보를 받지 못했습니다.")
        # A single social post can wrap one video. Never silently download the
        # first item of a channel/playlist and call the whole request complete.
        if info.get("_type") in {"playlist", "multi_video"}:
            raise DownloadError("COLLECTION", "채널·재생목록·여러 영상 묶음 대신 개별 영상 링크를 넣어 주세요.")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise DownloadError("LIVE", "진행 중인 라이브는 지원하지 않습니다. 종료된 영상 링크를 사용해 주세요.")
        callback("metadata", title=str(info.get("title") or "영상")[:300], platform=key,
                 uploader=str(info.get("uploader") or info.get("channel") or "")[:200],
                 duration=number(info.get("duration")), width=number(info.get("width")), height=number(info.get("height")))
        selected = info.get("requested_formats") or [info]
        expected_audio[0] = any(x.get("acodec") not in {None, "none"} for x in selected)
        known_size = sum(number(x.get("filesize") or x.get("filesize_approx")) or 0 for x in selected)
        if known_size > MAX_FILE_BYTES:
            raise DownloadError("FILE_LIMIT", "최고 화질 파일이 20GB 한도를 넘습니다. 낮은 화질로 바꾸지 않았습니다.")
        if known_size and shutil.disk_usage(folder).free < 2 * known_size + MIN_FREE:
            raise DownloadError("DISK_FULL", "최고 화질 영상과 오디오를 결합할 저장 공간이 부족합니다.")
        ydl.process_ie_result(info, download=True)
    callback("progress", stage="verifying", message="완성된 영상 파일 확인 중")
    files = [p for p in folder.glob("video.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".ts", ".m4v", ".3gp", ".ogv"}]
    if len(files) != 1:
        raise DownloadError("INCOMPLETE", "완성된 영상 파일이 없습니다. 다운로드를 다시 시도해 주세요.")
    path = files[0]
    verified = probe_video(path)
    if expected_audio[0] and not verified["hasAudio"]:
        raise DownloadError("MISSING_AUDIO", "제공된 오디오를 영상에 결합하지 못했습니다.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DownloadError("FILE_LIMIT", "완성된 파일이 20GB 한도를 넘습니다.")
    return {"path": str(path), "filename": safe_name(info.get("title"), path.suffix[1:]),
            "title": str(info.get("title") or "영상")[:300], "platform": key,
            "format": path.suffix[1:].upper(), **verified,
            "qualityNote": "현재 접근 가능한 형식 중 최고 가용 화질. 원본보다 확대하거나 재인코딩하지 않습니다."}


def worker_main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    job_path = Path(args.job).resolve()
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    cookie = job_path.with_name("session.cookies.txt")
    try:
        result = download_one(payload["url"], job_path.parent, cookie if cookie.exists() else None)
        emit("complete", result=result)
        return 0
    except DownloadError as exc:
        emit("error", code=exc.code, message=str(exc))
    except Exception as exc:
        emit("error", **error_detail(str(exc)))
    finally:
        cookie.unlink(missing_ok=True)
        job_path.unlink(missing_ok=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(worker_main())

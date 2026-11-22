"""The HTTP server, tested over real sockets on a free port.

The engine (yt-dlp and ffmpeg) is replaced by fast fakes, so nothing here needs
the network or ffmpeg. "Video" files are a few bytes that start with VIDEO.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from clipstudio import __main__ as launcher
from clipstudio import __version__, engine
from clipstudio import server as web
from clipstudio.config import Settings
from clipstudio.server import (
    MARKER,
    App,
    fit_clip,
    open_server,
    parse_range,
    server_url,
    split_extension,
    split_host,
)

WAIT = 5.0
MAX_UPLOAD = 2 * 1024 * 1024
VIDEO_URL = "https://www.youtube.com/watch?v=abcDEF12345"
SECRET = "TOP-SECRET-CONTENT"
WORDS = [{"t": n * 0.5, "d": 0.4, "w": f"word{n}"} for n in range(240)]
MOMENTS = [
    {
        "start": 40.0,
        "end": 70.0,
        "peak": 45,
        "score": 100,
        "reasons": ["Viewers replayed this part", "Loud reaction"],
        "text": "word80 word81 word82",
        "title": "Word80 word81 word82",
    },
    {"start": 5.0, "end": 35.0, "peak": 9, "score": 61, "reasons": ["Excited or fast speech"], "text": "", "title": ""},
]


# ------------------------------------------------------------- fake engine


class FakeEngine:
    """Stands in for clipstudio.engine. Records every call; can block or fail on request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.entered: dict[str, threading.Event] = {}
        self.block: dict[str, threading.Event] = {}
        self.fail: dict[str, Any] = {}
        self.tools = {"ytdlp": "2026.10.26", "ffmpeg": "7.1", "deno": "2.3.1", "captions_filter": True}
        self.words = list(WORDS)
        self.channel = "Test Channel"
        self.duration = 120.0
        self.moments = [dict(m) for m in MOMENTS]
        self.reported_url = VIDEO_URL
        self.sound_shares: list[float] = []
        self._lock = threading.Lock()

    def install(self, monkeypatch) -> None:
        for name in (
            "tool_status",
            "inspect_url",
            "inspect_file",
            "download_audio",
            "download_preview",
            "audio_levels",
            "motion_levels",
            "find_moments",
            "fetch_section",
            "build_ass",
            "render_short",
            "probe_duration",
        ):
            monkeypatch.setattr(engine, name, getattr(self, name))

    def named(self, name: str) -> list[tuple[tuple, dict]]:
        with self._lock:
            return [(args, kwargs) for called, args, kwargs in self.calls if called == name]

    def hold(self, name: str) -> threading.Event:
        """Makes the named function wait. Returns the event that is set when it has been entered."""
        self.block[name] = threading.Event()
        self.entered[name] = threading.Event()
        return self.entered[name]

    def let_go(self, name: str | None = None) -> None:
        for held in [name] if name else list(self.block):
            self.block[held].set()

    def _enter(self, name: str, *args, **kwargs) -> int:
        with self._lock:
            self.calls.append((name, args, kwargs))
            number = sum(1 for called, _, _ in self.calls if called == name)
        if name in self.entered:
            self.entered[name].set()
        if name in self.block:
            assert self.block[name].wait(WAIT), f"the test never released {name}"
        failure = self.fail.get(name)
        if callable(failure):
            failure = failure(number)
        if failure is not None:
            raise failure
        return number

    # -- the engine API

    def tool_status(self) -> dict:
        self._enter("tool_status")
        return dict(self.tools)

    def inspect_url(self, url: str) -> dict:
        self._enter("inspect_url", url)
        return {
            "kind": "url",
            "url": self.reported_url,
            "id": "abcDEF12345",
            "title": "A long stream",
            "channel": self.channel,
            "duration": self.duration,
            "was_live": True,
            "thumbnail": "https://i.ytimg.com/vi/abcDEF12345/hq.jpg",
            "view_count": 1234,
            "heatmap": [{"start_time": 40.0, "end_time": 50.0, "value": 1.0}],
            "words": list(self.words),
            "caption_label": "en (automatic)",
        }

    def inspect_file(self, path: Path) -> dict:
        self._enter("inspect_file", path)
        assert Path(path).is_file()
        return {
            "kind": "file",
            "path": str(path),
            "id": Path(path).stem,
            "title": Path(path).stem,
            "channel": "",
            "duration": self.duration,
            "was_live": False,
            "thumbnail": "",
            "view_count": None,
            "heatmap": [],
            "words": [],
            "caption_label": "",
        }

    def download_audio(self, url: str, work_dir: Path, progress=None) -> Path:
        self._enter("download_audio", url, work_dir)
        assert Path(work_dir).is_dir()
        if progress is not None:
            for share in self.sound_shares:
                progress(share)
        target = Path(work_dir) / "audio.m4a"
        target.write_bytes(b"AUDIO")
        return target

    def download_preview(self, url: str, work_dir: Path, progress=None) -> Path:
        self._enter("download_preview", url, work_dir)
        assert Path(work_dir).is_dir()
        if progress is not None:
            for share in self.sound_shares:
                progress(share)
        target = Path(work_dir) / "preview.mp4"
        target.write_bytes(b"PREVIEW")
        return target

    def audio_levels(self, source) -> list[float]:
        self._enter("audio_levels", source)
        assert Path(source).is_file()
        return [-30.0] * int(self.duration)

    def motion_levels(self, source) -> list[float]:
        self._enter("motion_levels", source)
        assert Path(source).is_file()
        return [4.0] * int(self.duration)

    def find_moments(self, duration, heatmap, levels, words, length=30, count=5, motion=None, focus="talk") -> dict:
        self._enter(
            *("find_moments", duration, heatmap, levels, words),
            **{"length": length, "count": count, "motion": motion, "focus": focus},
        )
        return {
            "moments": [dict(m) for m in self.moments[:count]],
            "signals": {
                "replays": bool(heatmap),
                "audio": bool(levels),
                "speech": bool(words),
                "motion": bool(motion),
            },
            "note": ""
            if self.moments
            else "There was nothing to measure: no replay data, no readable audio and no captions.",
        }

    def fetch_section(self, url: str, start: float, end: float, target: Path) -> Path:
        self._enter("fetch_section", url, start, end, target)
        assert Path(target).parent.is_dir()
        Path(target).write_bytes(b"VIDEO section")
        return Path(target)

    def build_ass(self, words, start, end, title="") -> str:
        self._enter("build_ass", words, start, end, title)
        return f"ASS words={len(words)} start={start} end={end} title={title}\n"

    def render_short(self, segment, seek, length, out, *, layout="blur", zoom=1.3, position="center", ass_file=None):
        ass_text = None
        if ass_file is not None:  # the captions file must be a bare name next to the output
            assert "/" not in ass_file and "\\" not in ass_file
            ass_text = (Path(out).parent / ass_file).read_text(encoding="utf-8")
        segment_existed = Path(segment).is_file()
        beside_segment = sorted(entry.name for entry in Path(segment).parent.iterdir())
        self._enter(
            "render_short",
            Path(segment),
            seek,
            length,
            Path(out),
            layout=layout,
            zoom=zoom,
            position=position,
            ass_file=ass_file,
            ass_text=ass_text,
            segment_existed=segment_existed,
            beside_segment=beside_segment,
        )
        Path(out).write_bytes(b"VIDEO rendered " + f"{layout} {zoom} {position}".encode())

    def probe_duration(self, path) -> float:
        self._enter("probe_duration", Path(path))
        with open(path, "rb") as handle:
            return 12.5 if handle.read(5) == b"VIDEO" else 0.0


# ---------------------------------------------------------------- fixtures


class Reply:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status, self.headers, self.body = status, headers, body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Studio:
    """A running server with its app, fake engine and captured log."""

    def __init__(self, app: App, server: web.ClipServer, fake: FakeEngine, logs: list[str], root: Path):
        self.app, self.server, self.fake, self.logs, self.root = app, server, fake, logs, root
        self.port = server.port
        self.origin = f"http://127.0.0.1:{self.port}"

    @property
    def log_text(self) -> str:
        return "\n".join(self.logs)

    def call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=WAIT)
        try:
            return self.send(connection, method, path, body=body, data=data, headers=headers)
        finally:
            connection.close()

    @staticmethod
    def send(connection, method, path, *, body=None, data=None, headers=None) -> Reply:
        headers = dict(headers or {})
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        return Reply(response.status, {key.lower(): value for key, value in response.getheaders()}, payload)

    def raw(self, request: bytes, half_close: bool = False) -> bytes:
        """Sends bytes exactly as given and returns everything the server answers."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=WAIT) as sock:
            sock.sendall(request)
            if half_close:
                sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def wait(self, job_id: str) -> dict:
        deadline = time.monotonic() + WAIT
        while True:
            reply = self.call("GET", f"/api/jobs/{job_id}")
            assert reply.status == 200, reply.text
            snapshot = reply.json()
            if snapshot["state"] in ("done", "error", "cancelled"):
                return snapshot
            assert time.monotonic() < deadline, f"job still {snapshot['state']}: {snapshot['step']}"
            time.sleep(0.01)

    def analyze(self, **changes) -> dict:
        """Runs an analyze job for the test link and returns its result."""
        body = {"url": VIDEO_URL, "clipLength": 30, "count": 5, "permission": True, **changes}
        reply = self.call("POST", "/api/analyze", data=body)
        assert reply.status == 202, reply.text
        done = self.wait(reply.json()["jobId"])
        assert done["state"] == "done", done
        return done["result"]

    def upload(self, name: str = "holiday.mp4", content: bytes = b"VIDEO" + b"\x00" * 2000) -> dict:
        reply = self.call("POST", "/api/uploads", body=content, headers={"X-Filename": name})
        assert reply.status == 201, reply.text
        return reply.json()

    def render(self, analysis_id: str, clips: list[dict] | None = None, **options) -> dict:
        """Runs a render job and returns the finished job."""
        clips = clips if clips is not None else [{"start": 40.0, "end": 70.0, "title": "First clip"}]
        body = {"analysisId": analysis_id, "clips": clips, "options": {**RENDER_OPTIONS, **options}}
        reply = self.call("POST", "/api/render", data=body)
        assert reply.status == 202, reply.text
        return self.wait(reply.json()["jobId"])

    def stored(self, folder: Path) -> list[str]:
        return sorted(entry.name for entry in folder.iterdir() if entry.name != MARKER)


RENDER_OPTIONS = {"layout": "blur", "zoom": 1.3, "position": "center", "captions": True, "showTitle": False}


def make_public(root: Path) -> Path:
    public = root / "public"
    (public / "sub").mkdir(parents=True)
    (public / "index.html").write_text("<!doctype html><title>Clip Studio</title>", encoding="utf-8")
    (public / "styles.css").write_text("body { margin: 0 }", encoding="utf-8")
    (public / "app.js").write_text("console.log('hello')", encoding="utf-8")
    (public / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (public / "sub" / "page.html").write_text("<p>sub</p>", encoding="utf-8")
    (public / ".hidden").write_text(SECRET, encoding="utf-8")
    (public / "data.bin").write_bytes(b"\x00\x01")
    return public


@pytest.fixture
def studio(tmp_path, monkeypatch):
    fake = FakeEngine()
    fake.install(monkeypatch)
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    settings = Settings(host="127.0.0.1", port=0, data_dir=tmp_path / "data", max_upload_bytes=MAX_UPLOAD)
    logs: list[str] = []
    app = App(settings, public_dir=make_public(tmp_path), log=logs.append)
    (settings.data_dir / "secret.txt").write_text(SECRET, encoding="utf-8")
    server = open_server(app)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    made = Studio(app, server, fake, logs, tmp_path)
    yield made
    fake.let_go()
    server.shutdown()
    server.server_close()
    app.close()
    thread.join(WAIT)


def assert_secure(reply: Reply) -> None:
    """The headers every response must carry."""
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert reply.headers["referrer-policy"] == "no-referrer"
    policy = reply.headers["content-security-policy"]
    for part in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "media-src 'self' blob:",
        "img-src 'self' data: https://i.ytimg.com https://*.ggpht.com",
        "frame-ancestors 'none'",
    ):
        assert part in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert "Python" not in reply.headers.get("server", "")


def assert_error(reply: Reply, status: int, code: str) -> dict:
    """An error is always the same JSON shape, with the safety headers and no program details."""
    assert reply.status == status, reply.text
    assert reply.headers["content-type"] == "application/json; charset=utf-8"
    assert_secure(reply)
    payload = reply.json()
    assert set(payload) == {"error"}
    error = payload["error"]
    assert set(error) <= {"status", "code", "message", "hint"} and {"status", "code", "message"} <= set(error)
    assert error["status"] == status
    assert error["code"] == code, error
    assert isinstance(error["message"], str) and error["message"].strip()
    for leak in ("Traceback", 'File "', ".py", "Error(", SECRET):
        assert leak not in reply.text, reply.text
    return error


# ------------------------------------------------------------------ status


def test_status_has_the_exact_shape(studio):
    reply = studio.call("GET", "/api/status")
    assert reply.status == 200
    assert reply.headers["content-type"] == "application/json; charset=utf-8"
    assert reply.headers["cache-control"] == "no-store"
    assert_secure(reply)
    assert reply.json() == {
        "version": "1.0.0",
        "tools": {"ytdlp": "2026.10.26", "ffmpeg": "7.1", "deno": "2.3.1", "captions_filter": True},
        "limits": {
            "minClipSeconds": 8,
            "maxClipSeconds": 90,
            "maxClips": 10,
            "maxUploadBytes": MAX_UPLOAD,
            "allowedHosts": ["youtube.com", "youtu.be"],
        },
    }
    assert __version__ == "1.0.0"


def test_status_reports_missing_tools_as_null_and_checks_them_only_once(studio):
    studio.fake.tools = {"ytdlp": None, "ffmpeg": None, "deno": None, "captions_filter": False}
    first = studio.call("GET", "/api/status").json()
    assert first["tools"] == {"ytdlp": None, "ffmpeg": None, "deno": None, "captions_filter": False}
    studio.call("GET", "/api/status")
    assert len(studio.fake.named("tool_status")) == 1


def test_head_status_has_headers_and_no_body(studio):
    reply = studio.call("HEAD", "/api/status")
    assert reply.status == 200 and reply.body == b""
    assert int(reply.headers["content-length"]) > 0
    assert_secure(reply)


# ------------------------------------------------------------ static files


@pytest.mark.parametrize(
    ("path", "content_type", "start"),
    [
        ("/", "text/html; charset=utf-8", b"<!doctype html>"),
        ("/index.html", "text/html; charset=utf-8", b"<!doctype html>"),
        ("/index.html?v=2", "text/html; charset=utf-8", b"<!doctype html>"),
        ("/styles.css", "text/css; charset=utf-8", b"body"),
        ("/app.js", "text/javascript; charset=utf-8", b"console"),
        ("/favicon.svg", "image/svg+xml; charset=utf-8", b"<svg"),
        ("/sub/page.html", "text/html; charset=utf-8", b"<p>sub"),
        ("/sub%2Fpage.html", "text/html; charset=utf-8", b"<p>sub"),
        ("/data.bin", "application/octet-stream", b"\x00\x01"),
    ],
)
def test_app_files_are_served_with_the_right_type_and_no_cache(studio, path, content_type, start):
    reply = studio.call("GET", path)
    assert reply.status == 200
    assert reply.headers["content-type"] == content_type
    assert reply.headers["cache-control"] == "no-cache"
    assert reply.body.startswith(start)
    assert int(reply.headers["content-length"]) == len(reply.body)
    assert_secure(reply)


def test_head_on_an_app_file_has_no_body(studio):
    reply = studio.call("HEAD", "/styles.css")
    assert reply.status == 200 and reply.body == b""
    assert reply.headers["content-length"] == str(len("body { margin: 0 }"))


@pytest.mark.parametrize(
    "path", ["/missing.html", "/sub", "/sub/", "/.hidden", "/index.html/", "/index.html/x", "/sub/missing"]
)
def test_missing_app_files_are_a_json_404(studio, path):
    assert_error(studio.call("GET", path), 404, "not_found")


TRAVERSALS = [
    "/../secret.txt",
    "/../../secret.txt",
    "/%2e%2e/secret.txt",
    "/%2E%2E/secret.txt",
    "/..%2fsecret.txt",
    "/..%2Fsecret.txt",
    "/%2e%2e%2fsecret.txt",
    "/sub/../../secret.txt",
    "/sub/%2e%2e/%2e%2e/secret.txt",
    "/sub/..%2f..%2fsecret.txt",
    "/sub%2f..%2f..%2fsecret.txt",
    "/..\\secret.txt",
    "/..%5csecret.txt",
    "/sub\\..\\..\\secret.txt",
    "/%5c..%5c..%5csecret.txt",
    "/....//secret.txt",
    "/.../secret.txt",
    "/%252e%252e/secret.txt",
    "/..;/secret.txt",
    "/./../secret.txt",
    "/%00",
    "/index.html%00.png",
    "/secret.txt",
    "/C:/Windows/win.ini",
    "/C:%5cWindows%5cwin.ini",
    "/file:///etc/passwd",
    "//etc/passwd",
    "/etc/passwd",
    "/%2fetc%2fpasswd",
    "/data/secret.txt",
    "/../data/secret.txt",
    "/../data/uploads/",
    "/../clipstudio/server.py",
    "/index.html::$DATA",
    "/output/../secret.txt",
    "/output/../../secret.txt",
    "/output/%2e%2e/secret.txt",
    "/output/..%2fsecret.txt",
    "/output/..%2f..%2fsecret.txt",
    "/output/demo-abc12345/../secret.txt",
    "/output/demo-abc12345/../../secret.txt",
    "/output/demo-abc12345/%2e%2e%2f%2e%2e%2fsecret.txt",
    "/output/demo-abc12345/..%5c..%5csecret.txt",
    "/output/demo-abc12345/clip-01.mp4/../../../secret.txt",
    "/output/demo-abc12345%2f..%2f..%2fsecret.txt",
    "/output/..%2fuploads/clip-01.mp4",
    "/output/../uploads/clip-01.mp4",
    "/output/%2e%2e/uploads/clip-01.mp4",
    "/output/.clipstudio",
    "/output//secret.txt",
    "/output//etc/passwd",
    "/output/C:/secret.txt",
    "/output/demo-abc12345/secret.txt",
    "/output/demo-abc12345/clip-01.mp4%00.txt",
    "/output/demo-abc12345/",
    "/output/demo-abc12345",
    "/output/",
    "/output",
]


@pytest.mark.parametrize("path", TRAVERSALS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_no_path_reaches_outside_the_served_folders(studio, path, method):
    out = studio.app.settings.output_dir / "demo-abc12345"
    out.mkdir(exist_ok=True)
    (out / "clip-01.mp4").write_bytes(b"VIDEO clip")
    (out / "secret.txt").write_text(SECRET, encoding="utf-8")
    (studio.app.settings.uploads_dir / "clip-01.mp4").write_text(SECRET, encoding="utf-8")
    reply = studio.call(method, path)
    assert reply.status == 404, f"{path} -> {reply.status}"
    assert SECRET.encode() not in reply.body
    assert_secure(reply)
    if method == "GET":
        assert_error(reply, 404, "not_found")


@pytest.mark.skipif(os.name == "nt", reason="Windows does not allow these characters in file names at all")
@pytest.mark.parametrize(
    "name", ["odd:name.txt", "back\\slash.txt", "star*.txt", "tab\tname.txt", "trailing.dot.", "space.txt "]
)
def test_names_that_mean_something_special_on_windows_are_never_served(studio, name):
    (studio.app.public_dir / name).write_text(SECRET, encoding="utf-8")
    for path in ("/" + name, "/" + "".join(f"%{byte:02x}" for byte in name.encode())):
        reply = studio.call("GET", path.replace("\t", "%09").replace(" ", "%20"))
        assert reply.status == 404 and SECRET.encode() not in reply.body


def test_a_link_inside_public_that_points_outside_is_not_followed(studio):
    link = studio.app.public_dir / "link.txt"
    try:
        link.symlink_to(studio.root / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this system does not allow making symbolic links")
    assert_error(studio.call("GET", "/link.txt"), 404, "not_found")


# ----------------------------------------------------- host and origin


@pytest.mark.parametrize("name", ["127.0.0.1", "localhost", "LOCALHOST", "[::1]"])
def test_local_host_names_with_the_right_port_are_accepted(studio, name):
    reply = studio.call("GET", "/api/status", headers={"Host": f"{name}:{studio.port}"})
    assert reply.status == 200


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "example.com:{port}",
        "evil.example:80",
        "127.0.0.1.evil.com:{port}",
        "localhost.evil.com:{port}",
        "evil-localhost:{port}",
        "127.0.0.1",  # no port means port 80, which is not this server
        "localhost",
        "127.0.0.1:1",
        "localhost:{port}0",
        "127.0.0.1:{port}:{port}",
        "127.0.0.1:{port}@evil.com",
        "evil.com@127.0.0.1:{port}",
        "[::1]",
        "[::1]:1",
        "[::1",
        "::1",
        "192.168.1.10:{port}",
        "0.0.0.0:{port}",
        "127.0.0.1:abc",
        "127.0.0.1:\u00b2",
        "",
        ":{port}",
    ],
)
@pytest.mark.parametrize(("method", "path"), [("GET", "/api/status"), ("GET", "/"), ("POST", "/api/analyze")])
def test_other_host_headers_are_refused(studio, host, method, path):
    headers = {"Host": host.format(port=studio.port).encode("latin-1").decode("latin-1")}
    data = {"url": VIDEO_URL, "clipLength": 30, "count": 5, "permission": True} if method == "POST" else None
    connection = http.client.HTTPConnection("127.0.0.1", studio.port, timeout=WAIT)
    try:
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", headers["Host"].encode("latin-1"))
        body = json.dumps(data).encode() if data else b""
        if method == "POST":
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        reply = Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
    finally:
        connection.close()
    error = assert_error(reply, 403, "forbidden_host")
    assert f"http://127.0.0.1:{studio.port}" in error["hint"]
    assert studio.fake.named("inspect_url") == [] and studio.app.jobs.count() == 0


def test_a_request_with_no_host_header_or_two_of_them_is_refused(studio):
    answer = studio.raw(b"GET /api/status HTTP/1.1\r\nConnection: close\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403 ") and b"forbidden_host" in answer
    two = f"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\nHost: evil.com\r\nConnection: close\r\n\r\n"
    answer = studio.raw(two.encode())
    assert answer.startswith(b"HTTP/1.1 403 ") and b"forbidden_host" in answer


def test_the_bound_address_is_accepted_as_a_host_name_when_it_is_not_a_wildcard(tmp_path, monkeypatch):
    FakeEngine().install(monkeypatch)
    app = App(
        Settings(host="127.0.0.2", port=0, data_dir=tmp_path / "data"),
        public_dir=make_public(tmp_path),
        log=lambda m: None,
    )
    try:
        server = open_server(app)
    except RuntimeError:
        app.close()
        pytest.skip("this system has no 127.0.0.2 address")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.2", server.port, timeout=WAIT)
        assert Studio.send(connection, "GET", "/api/status").status == 200
        connection.close()
        assert server_url(server) == f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()
        server.server_close()
        app.close()


ANALYZE = {"url": VIDEO_URL, "clipLength": 30, "count": 5, "permission": True}


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.com",
        "https://evil.com",
        "http://evil.com:{port}",
        "https://127.0.0.1:{port}",
        "http://127.0.0.1",
        "http://127.0.0.1:1",
        "http://localhost:{port}",  # another origin than the one the page was opened with
        "http://127.0.0.1:{port}.evil.com",
        "null",
        "",
        "file://",
    ],
)
@pytest.mark.parametrize(
    "path", ["/api/analyze", "/api/render", "/api/uploads", "/api/jobs/abcdefgh12345678/cancel", "/api/nothing"]
)
def test_posts_from_another_site_are_refused(studio, origin, path):
    headers = {"Origin": origin.format(port=studio.port), "X-Filename": "a.mp4"}
    reply = studio.call("POST", path, body=json.dumps(ANALYZE).encode(), headers=headers)
    assert_error(reply, 403, "forbidden_origin")
    assert studio.app.jobs.count() == 0 and studio.fake.named("inspect_url") == []
    assert studio.stored(studio.app.settings.uploads_dir) == []


def test_fetch_metadata_marked_cross_site_is_refused_even_without_an_origin(studio):
    reply = studio.call("POST", "/api/analyze", data=ANALYZE, headers={"Sec-Fetch-Site": "cross-site"})
    assert_error(reply, 403, "forbidden_origin")
    assert studio.app.jobs.count() == 0


@pytest.mark.parametrize("extra", [{}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}])
def test_posts_from_the_app_itself_are_accepted(studio, extra):
    for origin in (studio.origin, studio.origin + "/", studio.origin.upper().replace("HTTP", "http"), None):
        headers = dict(extra)
        if origin:
            headers["Origin"] = origin
        reply = studio.call("POST", "/api/analyze", data=ANALYZE, headers=headers)
        assert reply.status == 202, reply.text


def test_origin_must_match_the_name_the_page_was_opened_with(studio):
    headers = {"Host": f"localhost:{studio.port}", "Origin": f"http://localhost:{studio.port}"}
    assert studio.call("POST", "/api/analyze", data=ANALYZE, headers=headers).status == 202


def test_reading_is_allowed_with_a_foreign_origin_but_gets_no_cors_headers(studio):
    reply = studio.call("GET", "/api/status", headers={"Origin": "http://evil.com"})
    assert reply.status == 200
    assert not any(name.startswith("access-control-") for name in reply.headers)
    preflight = studio.call(
        "OPTIONS", "/api/analyze", headers={"Origin": "http://evil.com", "Access-Control-Request-Method": "POST"}
    )
    assert_error(preflight, 405, "method_not_allowed")
    assert not any(name.startswith("access-control-") for name in preflight.headers)


# ------------------------------------------------- methods and bad requests


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"])
@pytest.mark.parametrize("path", ["/", "/api/status", "/api/analyze", "/output/demo-abc12345/clip-01.mp4"])
def test_other_methods_are_refused(studio, method, path):
    reply = studio.call(method, path, body=b"{}" if method != "TRACE" else None)
    assert_error(reply, 405, "method_not_allowed")
    assert reply.headers["allow"] == "GET, HEAD, POST"


@pytest.mark.parametrize("path", ["/api/analyze", "/api/render", "/api/uploads", "/api/jobs/abcdefgh12345678/cancel"])
def test_get_on_a_post_route_is_405(studio, path):
    reply = studio.call("GET", path)
    assert_error(reply, 405, "method_not_allowed")
    assert reply.headers["allow"] == "POST"


@pytest.mark.parametrize(
    "path", ["/api/status", "/api/jobs/abcdefgh12345678", "/", "/index.html", "/output/a/clip-01.mp4"]
)
def test_post_on_a_get_route_is_405(studio, path):
    reply = studio.call("POST", path, data={})
    assert_error(reply, 405, "method_not_allowed")
    assert reply.headers["allow"] == "GET, HEAD"


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "path",
    [
        "/api",
        "/api/",
        "/api/nothing",
        "/api/jobs",
        "/api/jobs/",
        "/api/status/",
        "/api/jobs/short",
        "/api/jobs/abcdefgh12345678/other",
        "/api/../api/status",
    ],
)
def test_unknown_api_routes_are_a_json_404(studio, method, path):
    assert_error(studio.call(method, path, data={} if method == "POST" else None), 404, "not_found")


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"GARBAGE\r\n\r\n",
        b"GET /\r\n\r\n",  # HTTP/0.9, which has no status line and no headers
        b"POST /api/analyze\r\n\r\n",
        b"GET / HTTP/9.9\r\nHost: x\r\n\r\n",
        b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET / HTTP/1.1\r\nHost: x\r\n" + b"X-Long: " + b"a" * 70000 + b"\r\n\r\n",
        b"BREW /pot HTTP/1.1\r\nHost: x\r\n\r\n",
    ],
)
def test_requests_the_server_cannot_read_get_a_json_error_with_the_safety_headers(studio, request_bytes):
    answer = studio.raw(request_bytes)
    head, _, body = answer.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    assert status in (400, 414, 431, 501, 505)
    lowered = head.lower()
    assert b"x-content-type-options: nosniff" in lowered
    assert b"referrer-policy: no-referrer" in lowered
    assert b"content-security-policy: default-src 'self'" in lowered
    assert b"content-type: application/json" in lowered
    error = json.loads(body)["error"]
    assert error["status"] == status and error["message"]
    assert b"Traceback" not in answer and b"<html" not in answer.lower()


def test_an_unexpected_failure_is_a_plain_500_and_the_details_go_to_the_log(studio, monkeypatch):
    def broken(self):
        raise RuntimeError("secret detail in /home/user/private.py")

    monkeypatch.setattr(App, "status", broken)
    reply = studio.call("GET", "/api/status")
    error = assert_error(reply, 500, "server_error")
    assert "secret detail" not in reply.text and "RuntimeError" not in reply.text
    assert "clip-log.txt" in error["hint"]
    assert "Traceback (most recent call last)" in studio.log_text and "secret detail" in studio.log_text
    assert studio.call("GET", "/styles.css").status == 200  # the server carries on


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("GET", "/api/status", None),
        ("GET", "/", None),
        ("GET", "/missing", None),
        ("GET", "/api/jobs/abcdefgh12345678", None),
        ("POST", "/api/analyze", {"permission": False}),
        ("POST", "/api/analyze", ANALYZE),
        ("POST", "/api/render", {}),
        ("POST", "/api/jobs/abcdefgh12345678/cancel", None),
        ("DELETE", "/api/status", None),
        ("GET", "/output/x/clip-01.mp4", None),
    ],
)
def test_safety_headers_are_on_every_kind_of_response(studio, method, path, data):
    reply = studio.call(method, path, data=data)
    assert_secure(reply)
    assert "Traceback" not in reply.text


# ---------------------------------------------------------- request bodies


@pytest.mark.parametrize("path", ["/api/analyze", "/api/render"])
@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"{", b"[1, 2]", b'"text"', b"42", b"null", b"\xff\xfe", b"[" * 100000, b'{"a": 1} trailing'],
)
def test_a_body_that_is_not_a_json_object_is_refused(studio, path, body):
    assert_error(studio.call("POST", path, body=body), 400, "bad_json")
    assert studio.app.jobs.count() == 0


@pytest.mark.parametrize("path", ["/api/analyze", "/api/render", "/api/jobs/abcdefgh12345678/cancel"])
def test_a_json_body_that_is_too_large_is_refused(studio, path):
    body = json.dumps({**ANALYZE, "padding": "x" * web.MAX_JSON_BYTES}).encode()
    reply = studio.call("POST", path, body=body)
    assert_error(reply, 413, "body_too_large")
    assert reply.headers["connection"] == "close"
    assert studio.app.jobs.count() == 0


def test_a_body_just_under_the_limit_is_read(studio):
    padding = "x" * (web.MAX_JSON_BYTES - len(json.dumps({**ANALYZE, "padding": ""}).encode()))
    body = json.dumps({**ANALYZE, "padding": padding}).encode()
    assert len(body) == web.MAX_JSON_BYTES
    assert studio.call("POST", "/api/analyze", body=body).status == 202


@pytest.mark.parametrize("length", ["abc", "-5", "1e3", "5, 6", "\u00b2"])
def test_a_content_length_that_is_not_a_number_is_refused(studio, length):
    request = (
        f"POST /api/analyze HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\nContent-Length: ".encode()
        + length.encode("latin-1")
        + b"\r\n\r\n{}"
    )
    answer = studio.raw(request)
    assert answer.startswith(b"HTTP/1.1 400 ") and b"bad_content_length" in answer
    assert b"connection: close" in answer.lower()  # the server ends the connection itself


def test_a_json_post_with_no_content_length_is_an_empty_body(studio):
    request = f"POST /api/analyze HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\nConnection: close\r\n\r\n"
    answer = studio.raw(request.encode())
    assert answer.startswith(b"HTTP/1.1 400 ") and b"bad_json" in answer


def test_chunked_bodies_are_refused(studio):
    request = (
        f"POST /api/analyze HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\nTransfer-Encoding: chunked\r\n\r\n"
        "2\r\n{}\r\n0\r\n\r\n"
    )
    answer = studio.raw(request.encode())
    assert answer.startswith(b"HTTP/1.1 411 ") and b"length_required" in answer
    assert b"connection: close" in answer.lower()


def test_a_body_that_stops_early_is_refused(studio):
    request = f"POST /api/analyze HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\nContent-Length: 500\r\n\r\n{{"
    answer = studio.raw(request.encode(), half_close=True)
    assert answer.startswith(b"HTTP/1.1 400 ") and b"incomplete_request" in answer


def test_the_connection_stays_usable_after_errors(studio):
    connection = http.client.HTTPConnection("127.0.0.1", studio.port, timeout=WAIT)
    try:
        assert_error(Studio.send(connection, "POST", "/api/analyze", body=b"not json"), 400, "bad_json")
        sock = connection.sock
        assert_error(Studio.send(connection, "POST", "/api/nothing", data={"unread": "x" * 5000}), 404, "not_found")
        assert_error(Studio.send(connection, "POST", "/api/status", data={"unread": 1}), 405, "method_not_allowed")
        assert_error(Studio.send(connection, "GET", "/missing"), 404, "not_found")
        assert_error(
            Studio.send(connection, "POST", "/api/analyze", data={"permission": False}), 400, "permission_required"
        )
        assert Studio.send(connection, "GET", "/api/status").status == 200
        assert Studio.send(connection, "HEAD", "/styles.css").body == b""
        assert Studio.send(connection, "GET", "/styles.css").body == b"body { margin: 0 }"
        assert connection.sock is sock, "the server closed a connection it could have kept"
    finally:
        connection.close()


# ----------------------------------------------------------------- uploads


def test_upload_stores_the_file_under_a_generated_name(studio):
    content = b"VIDEO" + os.urandom(5000)
    reply = studio.call("POST", "/api/uploads", body=content, headers={"X-Filename": "My%20holiday%20%E0%A4%A8.MP4"})
    assert reply.status == 201
    assert_secure(reply)
    payload = reply.json()
    assert set(payload) == {"uploadId", "name", "bytes"}
    assert payload["name"] == "My holiday न.mp4"
    assert payload["bytes"] == len(content)
    upload_id = payload["uploadId"]
    assert isinstance(upload_id, str) and len(upload_id) >= 16
    assert studio.stored(studio.app.settings.uploads_dir) == [f"{upload_id}.mp4"]
    assert (studio.app.settings.uploads_dir / f"{upload_id}.mp4").read_bytes() == content


def test_upload_is_written_to_disk_in_pieces_not_held_in_memory(studio, monkeypatch):
    reads: list[int] = []
    original = web.Handler._upload

    def spying(self):
        real_read = self.rfile.read

        def read(size=-1):
            reads.append(size)
            return real_read(size)

        self.rfile = type(
            "Spy",
            (),
            {"read": staticmethod(read), "readline": self.rfile.readline, "close": self.rfile.close, "closed": False},
        )()
        return original(self)

    monkeypatch.setattr(web.Handler, "_upload", spying)
    monkeypatch.setattr(web, "UPLOAD_CHUNK", 64 * 1024)
    content = b"VIDEO" + os.urandom(1024 * 1024)
    payload = studio.upload("big.mkv", content)
    assert payload["bytes"] == len(content)
    assert len(reads) >= 16 and max(reads) <= 64 * 1024 and min(reads) > 0
    assert (studio.app.settings.uploads_dir / f"{payload['uploadId']}.mkv").read_bytes() == content


@pytest.mark.parametrize("extension", [".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".MOV", ".WebM"])
def test_each_allowed_extension_is_accepted(studio, extension):
    payload = studio.upload("clip" + extension)
    assert studio.stored(studio.app.settings.uploads_dir) == [payload["uploadId"] + extension.lower()]


@pytest.mark.parametrize(
    ("sent", "shown"),
    [
        ("..%2F..%2F..%2Fetc%2Fpasswd.mp4", "passwd.mp4"),
        ("..%5C..%5Cwindows%5Csystem.mp4", "system.mp4"),
        ("%2Fabsolute%2Fpath%2Fvideo.mp4", "video.mp4"),
        ("C%3A%5CUsers%5Cme%5Cvideo.mp4", "video.mp4"),
        ("..%2F..%2Fsecret.txt%00.mp4", "secret.txt.mp4"),
        ("name%0Awith%0Dbreaks.mp4", "name with breaks.mp4"),
        ("x.MP4%20", "x.mp4"),
        ("...mp4", "video.mp4"),
        ("%3Cscript%3E.mp4", "<script>.mp4"),
        ("a" * 400 + ".mp4", "a" * 150 + ".mp4"),
    ],
)
def test_the_name_sent_never_decides_where_the_file_goes(studio, sent, shown):
    payload = studio.upload(sent)
    assert payload["name"] == shown
    assert studio.stored(studio.app.settings.uploads_dir) == [payload["uploadId"] + ".mp4"]
    assert (studio.root / "secret.txt").read_text(encoding="utf-8") == SECRET
    everything = sorted(p.name for p in studio.root.rglob("*") if p.is_file() and "public" not in p.parts)
    assert everything == sorted([MARKER] * 3 + ["secret.txt", "secret.txt", payload["uploadId"] + ".mp4"])


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "run.exe",
        "video",
        "video.mp4.exe",
        "video.mp3",
        "video.mp4%2F",
        "video.mp4.",
        "page.html",
        ".mp4",
        "mp4",
    ],
)
def test_other_kinds_of_file_are_refused(studio, name):
    reply = studio.call("POST", "/api/uploads", body=b"VIDEO data", headers={"X-Filename": name})
    error = assert_error(reply, 415, "unsupported_type")
    assert ".mp4" in error["hint"] and ".avi" in error["hint"]
    assert studio.stored(studio.app.settings.uploads_dir) == []


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({}, "missing_filename"),
        ({"X-Filename": "  "}, "missing_filename"),
        ({"X-Filename": "%FF%FE.mp4"}, "invalid_filename"),
        ({"X-Filename": "a" * 3000 + ".mp4"}, "invalid_filename"),
    ],
)
def test_a_missing_or_unreadable_file_name_is_refused(studio, headers, code):
    reply = studio.call("POST", "/api/uploads", body=b"VIDEO data", headers=headers)
    assert_error(reply, 400, code)
    assert studio.stored(studio.app.settings.uploads_dir) == []


def test_a_file_over_the_size_limit_is_refused_and_nothing_is_kept(studio):
    content = b"VIDEO" + b"\x00" * MAX_UPLOAD
    reply = studio.call("POST", "/api/uploads", body=content, headers={"X-Filename": "big.mp4"})
    error = assert_error(reply, 413, "file_too_large")
    assert "2 MB" in error["message"]
    assert studio.stored(studio.app.settings.uploads_dir) == []
    assert studio.fake.named("probe_duration") == []


def test_a_file_exactly_at_the_size_limit_is_accepted(studio):
    payload = studio.upload("edge.mp4", b"VIDEO" + b"\x00" * (MAX_UPLOAD - 5))
    assert payload["bytes"] == MAX_UPLOAD


def test_a_declared_size_over_the_limit_is_refused_before_any_of_it_is_read(studio):
    request = (
        f"POST /api/uploads HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\nX-Filename: huge.mp4\r\n"
        f"Content-Length: {50 * 1024**3}\r\n\r\n"
    )
    answer = studio.raw(request.encode(), half_close=True)
    assert answer.startswith(b"HTTP/1.1 413 ") and b"file_too_large" in answer
    assert studio.stored(studio.app.settings.uploads_dir) == []


def test_an_upload_without_content_length_is_refused(studio):
    request = (
        f"POST /api/uploads HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\nX-Filename: a.mp4\r\nConnection: close\r\n\r\n"
        "VIDEO data"
    )
    answer = studio.raw(request.encode())
    assert answer.startswith(b"HTTP/1.1 411 ") and b"length_required" in answer
    assert b"x-content-type-options: nosniff" in answer.lower()
    chunked = (
        f"POST /api/uploads HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\nX-Filename: a.mp4\r\n"
        "Transfer-Encoding: chunked\r\n\r\n5\r\nVIDEO\r\n0\r\n\r\n"
    )
    answer = studio.raw(chunked.encode())
    assert answer.startswith(b"HTTP/1.1 411 ") and b"length_required" in answer
    assert studio.stored(studio.app.settings.uploads_dir) == []


def test_an_empty_upload_is_refused(studio):
    reply = studio.call("POST", "/api/uploads", body=b"", headers={"X-Filename": "empty.mp4"})
    assert_error(reply, 400, "empty_file")


def test_a_file_that_is_not_a_video_is_deleted_and_refused(studio):
    reply = studio.call("POST", "/api/uploads", body=b"just some text, renamed", headers={"X-Filename": "fake.mp4"})
    assert_error(reply, 422, "not_a_video")
    assert len(studio.fake.named("probe_duration")) == 1
    assert studio.stored(studio.app.settings.uploads_dir) == []
    # and it cannot be used afterwards
    reply = studio.call(
        "POST", "/api/analyze", data={"uploadId": "0" * 32, "clipLength": 30, "count": 5, "permission": True}
    )
    assert_error(reply, 404, "upload_not_found")


def test_an_upload_that_stops_early_is_deleted(studio):
    request = (
        f"POST /api/uploads HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\nX-Filename: cut.mp4\r\n"
        "Content-Length: 100000\r\n\r\nVIDEO only the start"
    )
    answer = studio.raw(request.encode(), half_close=True)
    assert answer.startswith(b"HTTP/1.1 400 ") and b"incomplete_upload" in answer
    assert studio.stored(studio.app.settings.uploads_dir) == []


def test_a_failing_video_check_deletes_the_file_and_hides_the_details(studio):
    studio.fake.fail["probe_duration"] = RuntimeError("No ffmpeg exe could be found at /secret/path")
    reply = studio.call("POST", "/api/uploads", body=b"VIDEO data", headers={"X-Filename": "a.mp4"})
    assert_error(reply, 503, "tool_missing")
    assert "/secret/path" not in reply.text
    assert "/secret/path" in studio.log_text and "Traceback" in studio.log_text
    assert studio.stored(studio.app.settings.uploads_dir) == []


def test_not_enough_disk_space_is_reported_before_the_file_is_read(studio, monkeypatch):
    monkeypatch.setattr(web.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 10})())
    reply = studio.call(
        "POST", "/api/uploads", body=b"VIDEO data that is longer than ten bytes", headers={"X-Filename": "a.mp4"}
    )
    assert_error(reply, 507, "not_enough_space")
    assert studio.stored(studio.app.settings.uploads_dir) == []


def test_only_the_newest_uploads_are_kept(studio, monkeypatch):
    monkeypatch.setattr(web, "MAX_UPLOADS", 3)
    ids = [studio.upload(f"clip{n}.mp4")["uploadId"] for n in range(5)]
    assert studio.stored(studio.app.settings.uploads_dir) == sorted(f"{uid}.mp4" for uid in ids[2:])
    assert studio.app.get_upload(ids[0]) is None and studio.app.get_upload(ids[4]) is not None


# ----------------------------------------------------------------- analyze


def test_analyze_a_link_gives_the_exact_result_shape(studio):
    reply = studio.call(
        "POST", "/api/analyze", data={"url": VIDEO_URL, "clipLength": 30, "count": 5, "permission": True}
    )
    assert reply.status == 202
    assert_secure(reply)
    assert set(reply.json()) == {"jobId"}
    job_id = reply.json()["jobId"]
    done = studio.wait(job_id)
    assert set(done) == {"id", "kind", "state", "step", "progress", "result"}
    assert (done["id"], done["kind"], done["state"], done["progress"]) == (job_id, "analyze", "done", 1.0)
    result = done["result"]
    assert result == {
        "analysisId": result["analysisId"],
        "video": {
            "title": "A long stream",
            "channel": "Test Channel",
            "duration": 120.0,
            "url": VIDEO_URL,
            "thumbnail": "https://i.ytimg.com/vi/abcDEF12345/hq.jpg",
            "wasLive": True,
        },
        "signals": {"replays": True, "audio": True, "speech": True, "motion": False},
        "focus": "talk",
        "captionLabel": "en (automatic)",
        "note": "",
        "moments": [
            {
                "id": "m1",
                "start": 40.0,
                "end": 70.0,
                "score": 100,
                "reasons": ["Viewers replayed this part", "Loud reaction"],
                "text": "word80 word81 word82",
                "title": "Word80 word81 word82",
            },
            {
                "id": "m2",
                "start": 5.0,
                "end": 35.0,
                "score": 61,
                "reasons": ["Excited or fast speech"],
                "text": "",
                "title": "",
            },
        ],
    }
    assert isinstance(result["analysisId"], str) and len(result["analysisId"]) >= 12
    assert all(type(m["score"]) is int and type(m["start"]) is float for m in result["moments"])
    assert "word5" not in json.dumps(result)  # the word list itself stays on the server


def test_analyze_a_link_calls_the_engine_in_order_and_tidies_up(studio):
    studio.analyze(clipLength=45, count=3)
    fake = studio.fake
    assert [name for name, _, _ in fake.calls if name != "tool_status"] == [
        "inspect_url",
        "download_audio",
        "audio_levels",
        "find_moments",
    ]
    assert fake.named("inspect_url")[0][0] == (VIDEO_URL,)
    url, work = fake.named("download_audio")[0][0]
    assert url == VIDEO_URL
    assert work.parent == studio.app.settings.work_dir and work.name.startswith("job-")
    assert fake.named("audio_levels")[0][0] == (work / "audio.m4a",)
    args, kwargs = fake.named("find_moments")[0]
    assert args[0] == 120.0 and args[1] == [{"start_time": 40.0, "end_time": 50.0, "value": 1.0}]
    assert len(args[2]) == 120 and args[3] == WORDS
    assert kwargs == {"length": 45, "count": 3, "motion": [], "focus": "talk"}
    assert studio.stored(studio.app.settings.work_dir) == []  # the audio track is gone again


def test_analyze_an_uploaded_file(studio):
    upload = studio.upload("My holiday.mp4")
    result = studio.analyze(url=None, uploadId=upload["uploadId"])
    assert result["video"] == {
        "title": "My holiday",
        "channel": "",
        "duration": 120.0,
        "url": None,
        "thumbnail": "",
        "wasLive": False,
    }
    assert result["signals"] == {"replays": False, "audio": True, "speech": False, "motion": False}
    assert result["captionLabel"] == ""
    stored = studio.app.settings.uploads_dir / f"{upload['uploadId']}.mp4"
    assert studio.fake.named("inspect_file")[0][0] == (stored,)
    assert studio.fake.named("audio_levels")[0][0] == (stored,)
    assert studio.fake.named("download_audio") == [] and studio.fake.named("inspect_url") == []
    assert stored.is_file()  # kept for rendering


@pytest.mark.parametrize(
    "link",
    [
        "https://www.youtube.com/watch?v=abcDEF12345",
        "https://youtube.com/watch?v=abcDEF12345",
        "https://youtu.be/abcDEF12345",
        "https://m.youtube.com/watch?v=abcDEF12345",
        "https://music.youtube.com/watch?v=abcDEF12345",
        "http://youtube.com/watch?v=abcDEF12345",
        "  youtu.be/abcDEF12345  ",
        "https://WWW.YOUTUBE.COM/live/abcDEF12345?feature=share#t=5",
    ],
)
def test_youtube_links_are_accepted_and_reach_the_engine_tidied(studio, link):
    studio.analyze(url=link)
    (sent,) = studio.fake.named("inspect_url")[0][0]
    assert sent.startswith(("https://", "http://")) and " " not in sent and "#" not in sent
    assert sent.split("/")[2] in ("www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com")


@pytest.mark.parametrize(
    ("link", "code"),
    [
        ("https://example.com/watch?v=abc", "host_not_allowed"),
        ("https://vimeo.com/12345", "host_not_allowed"),
        ("https://youtube.com.evil.com/watch", "host_not_allowed"),
        ("https://127.0.0.1:5180/api/status", "host_not_allowed"),
        ("http://localhost/", "host_not_allowed"),
        ("http://169.254.169.254/latest/meta-data/", "host_not_allowed"),
        ("file:///etc/passwd", "invalid_url"),
        ("file://youtube.com/etc/passwd", "invalid_url"),
        ("ftp://youtube.com/video.mp4", "invalid_url"),
        ("javascript:alert(1)", "invalid_url"),
        ("https://user:pass@youtube.com/watch?v=abc", "invalid_url"),
        ("https://user@youtu.be/abc", "invalid_url"),
        ("https://youtube.com@evil.com/", "invalid_url"),
        ("https://youtube.com:8080/watch", "invalid_url"),
        ("https://youtube.com/watch?v=a b", "invalid_url"),
        ("--exec=rm -rf", "invalid_url"),
        ("", "invalid_url"),
        ("https://youtube.com/watch?v=" + "a" * 5000, "url_too_long"),
        (42, "invalid_url"),
        (["https://youtu.be/abc"], "invalid_url"),
        ({"href": "https://youtu.be/abc"}, "invalid_url"),
        (True, "invalid_url"),
    ],
)
def test_links_that_break_the_rules_never_reach_the_engine(studio, link, code):
    reply = studio.call("POST", "/api/analyze", data={**ANALYZE, "url": link})
    assert_error(reply, 400, code)
    assert studio.fake.named("inspect_url") == [] and studio.app.jobs.count() == 0


@pytest.mark.parametrize("permission", [None, False, "true", "yes", 1, 0, [], {}, "MISSING"])
def test_permission_must_be_exactly_true(studio, permission):
    body: dict[str, Any] = {**ANALYZE, "permission": permission}
    if permission == "MISSING":
        del body["permission"]
    reply = studio.call("POST", "/api/analyze", data=body)
    error = assert_error(reply, 400, "permission_required")
    assert "permission" in error["message"]
    assert studio.fake.named("inspect_url") == [] and studio.app.jobs.count() == 0


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"url": None}, 400, "missing_source"),
        ({"url": None, "uploadId": None}, 400, "missing_source"),
        ({"uploadId": "0123456789abcdef0123456789abcdef"}, 400, "one_source"),
        ({"uploadId": ""}, 400, "one_source"),
        ({"url": None, "uploadId": 42}, 400, "invalid_upload_id"),
        ({"url": None, "uploadId": ""}, 400, "invalid_upload_id"),
        ({"url": None, "uploadId": "short"}, 400, "invalid_upload_id"),
        ({"url": None, "uploadId": "../../etc/passwd"}, 400, "invalid_upload_id"),
        ({"url": None, "uploadId": "a" * 65}, 400, "invalid_upload_id"),
        ({"url": None, "uploadId": ["0123456789abcdef"]}, 400, "invalid_upload_id"),
        ({"url": None, "uploadId": "0123456789abcdef0123456789abcdef"}, 404, "upload_not_found"),
        ({"clipLength": None}, 400, "invalid_clip_length"),
        ({"clipLength": 7}, 400, "invalid_clip_length"),
        ({"clipLength": 91}, 400, "invalid_clip_length"),
        ({"clipLength": 0}, 400, "invalid_clip_length"),
        ({"clipLength": -30}, 400, "invalid_clip_length"),
        ({"clipLength": "30"}, 400, "invalid_clip_length"),
        ({"clipLength": 30.5}, 400, "invalid_clip_length"),
        ({"clipLength": True}, 400, "invalid_clip_length"),
        ({"clipLength": [30]}, 400, "invalid_clip_length"),
        ({"clipLength": 1e400}, 400, "invalid_clip_length"),
        ({"count": None}, 400, "invalid_count"),
        ({"count": 0}, 400, "invalid_count"),
        ({"count": 11}, 400, "invalid_count"),
        ({"count": -1}, 400, "invalid_count"),
        ({"count": "5"}, 400, "invalid_count"),
        ({"count": 2.5}, 400, "invalid_count"),
        ({"count": False}, 400, "invalid_count"),
        ({"count": {}}, 400, "invalid_count"),
    ],
)
def test_analyze_checks_every_field(studio, changes, status, code):
    body = {key: value for key, value in {**ANALYZE, **changes}.items() if value is not None or key == "uploadId"}
    reply = studio.call("POST", "/api/analyze", data=body)
    assert_error(reply, status, code)
    assert studio.app.jobs.count() == 0 and studio.fake.named("inspect_url") == []


def test_analyze_with_fields_missing_altogether(studio):
    assert_error(studio.call("POST", "/api/analyze", data={"permission": True}), 400, "missing_source")
    assert_error(
        studio.call("POST", "/api/analyze", data={"url": VIDEO_URL, "permission": True}), 400, "invalid_clip_length"
    )
    assert_error(
        studio.call("POST", "/api/analyze", data={"url": VIDEO_URL, "permission": True, "clipLength": 30}),
        400,
        "invalid_count",
    )


@pytest.mark.parametrize(("clip_length", "count"), [(8, 1), (90, 10), (30.0, 5.0)])
def test_the_edges_of_the_allowed_ranges_are_accepted(studio, clip_length, count):
    studio.analyze(clipLength=clip_length, count=count)
    kwargs = studio.fake.named("find_moments")[0][1]
    assert kwargs == {"length": int(clip_length), "count": int(count), "motion": [], "focus": "talk"}
    assert type(kwargs["length"]) is int and type(kwargs["count"]) is int


@pytest.mark.parametrize(
    ("tools", "body", "missing"),
    [
        ({"ffmpeg": None}, ANALYZE, "ffmpeg"),
        ({"ytdlp": None}, ANALYZE, "yt-dlp"),
    ],
)
def test_analyze_says_which_tool_is_missing(studio, tools, body, missing):
    studio.fake.tools.update(tools)
    error = assert_error(studio.call("POST", "/api/analyze", data=body), 503, "tool_missing")
    assert missing in error["message"] and "start.bat" in error["hint"]
    assert studio.app.jobs.count() == 0


def test_a_file_can_be_analyzed_without_ytdlp(studio):
    studio.fake.tools["ytdlp"] = None
    upload = studio.upload()
    assert studio.analyze(url=None, uploadId=upload["uploadId"])["moments"]


def test_analysis_carries_on_without_sound_when_the_audio_fails(studio):
    studio.fake.fail["download_audio"] = RuntimeError("ERROR: unable to download audio: HTTP Error 403")
    result = studio.analyze()
    assert result["signals"] == {"replays": True, "audio": False, "speech": True, "motion": False}
    assert len(result["moments"]) == 2
    assert studio.fake.named("audio_levels") == []
    assert studio.fake.named("find_moments")[0][0][2] == []
    assert "carrying on without it" in studio.log_text and "Traceback" in studio.log_text


def test_action_focus_fetches_a_small_copy_of_the_video_and_measures_movement(studio):
    studio.fake.sound_shares = [0.5, 1.0]
    result = studio.analyze(focus="action")
    fake = studio.fake
    order = [name for name, _, _ in fake.calls if name != "tool_status"]
    assert order == ["inspect_url", "download_preview", "audio_levels", "motion_levels", "find_moments"]
    assert fake.named("download_audio") == [], "the sound alone is not fetched as well"
    preview = fake.named("audio_levels")[0][0][0]
    assert Path(preview).name == "preview.mp4" and fake.named("motion_levels")[0][0][0] == preview
    _, kwargs = fake.named("find_moments")[0]
    assert kwargs["focus"] == "action" and kwargs["motion"] == [4.0] * 120
    assert result["focus"] == "action" and result["signals"]["motion"] is True
    assert "small copy of the video: 100% downloaded" in studio.log_text
    assert list(studio.app.settings.work_dir.glob("job-*")) == [], "the copy is removed when the job ends"


def test_action_focus_on_an_uploaded_file_measures_the_file_itself(studio):
    upload = studio.upload()
    result = studio.analyze(url=None, uploadId=upload["uploadId"], focus="action")
    stored = studio.app.settings.uploads_dir / f"{upload['uploadId']}.mp4"
    assert studio.fake.named("download_preview") == [] and studio.fake.named("download_audio") == []
    assert Path(studio.fake.named("motion_levels")[0][0][0]) == stored
    assert result["signals"]["motion"] is True


def test_talk_focus_never_measures_movement(studio):
    studio.analyze()
    assert studio.fake.named("motion_levels") == [] and studio.fake.named("download_preview") == []


def test_action_focus_carries_on_when_the_picture_cannot_be_measured(studio):
    studio.fake.fail["motion_levels"] = RuntimeError("ffmpeg could not read the picture")
    result = studio.analyze(focus="action")
    assert result["signals"] == {"replays": True, "audio": True, "speech": True, "motion": False}
    assert "the picture could not be measured, carrying on without it" in studio.log_text


def test_action_focus_carries_on_when_the_copy_cannot_be_fetched(studio):
    studio.fake.fail["download_preview"] = RuntimeError("ERROR: unable to download video data: HTTP Error 403")
    result = studio.analyze(focus="action")
    assert result["signals"]["audio"] is False and result["signals"]["motion"] is False
    assert studio.fake.named("motion_levels") == [], "there is no file to measure"


@pytest.mark.parametrize("focus", ["", "games", "ACTION", 1, None, ["action"]])
def test_an_unknown_focus_is_refused(studio, focus):
    reply = studio.call("POST", "/api/analyze", data={**ANALYZE, "focus": focus})
    assert_error(reply, 400, "invalid_focus")
    assert studio.fake.named("inspect_url") == []


def test_analysis_carries_on_when_measuring_loudness_fails(studio):
    studio.fake.fail["audio_levels"] = RuntimeError("ffmpeg could not read the audio")
    upload = studio.upload()
    result = studio.analyze(url=None, uploadId=upload["uploadId"])
    assert result["signals"] == {"replays": False, "audio": False, "speech": False, "motion": False}


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            ValueError("This stream is still live. Wait until it has ended, then paste the link again."),
            "This stream is still live. Wait until it has ended, then paste the link again.",
        ),
        (
            RuntimeError("ERROR: [youtube] abc: Private video. Sign in"),
            "This video is private, so it cannot be opened.",
        ),
        (RuntimeError("ERROR: [youtube] abc: Video unavailable"), "YouTube says this video is unavailable."),
        (KeyError("formats"), "Something went wrong on this computer while doing that."),
    ],
)
def test_a_failing_analysis_reports_one_plain_sentence(studio, failure, message):
    studio.fake.fail["inspect_url"] = failure
    reply = studio.call("POST", "/api/analyze", data=ANALYZE)
    failed = studio.wait(reply.json()["jobId"])
    assert set(failed) == {"id", "kind", "state", "step", "progress", "error"}
    assert failed["state"] == "error"
    assert failed["error"] == {"message": message, "hint": "The details are in clip-log.txt in the app folder."}
    assert "Traceback" not in json.dumps(failed) and 'File "' not in json.dumps(failed)
    assert "Traceback (most recent call last)" in studio.log_text
    assert studio.stored(studio.app.settings.work_dir) == []


def test_a_video_with_no_readable_length_is_an_error(studio):
    studio.fake.duration = 0.0
    failed = studio.wait(studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"])
    assert failed["state"] == "error"
    assert failed["error"] == {"message": "The length of this video could not be read.", "hint": "Try another video."}


def test_moments_are_brought_inside_the_video_and_the_clip_length_limits(studio):
    studio.fake.moments = [
        {
            "start": 10.0,
            "end": 15.0,
            "peak": 11,
            "score": 250,
            "reasons": ["Loud reaction"],
            "text": "t",
            "title": "T" * 200,
        },
        {"start": 10.0, "end": 105.0, "peak": 20, "score": 0, "reasons": [], "text": "", "title": ""},
        {
            "start": 116.0,
            "end": 130.0,
            "peak": 118,
            "score": 40.6,
            "reasons": ["x"],
            "text": "",
            "title": "line\nbreak",
        },
        {"start": "soon", "end": 20.0, "peak": 1, "score": 5, "reasons": [], "text": "", "title": ""},
    ]
    moments = studio.analyze()["moments"]
    assert [(m["id"], m["start"], m["end"], m["score"]) for m in moments] == [
        ("m1", 10.0, 18.0, 100),
        ("m2", 10.0, 100.0, 1),
        ("m3", 112.0, 120.0, 41),
    ]
    assert len(moments[0]["title"]) == 80 and moments[2]["title"] == "line break"
    for moment in moments:  # every suggestion can be sent straight back to /api/render
        done = studio.render(
            studio.analyze()["analysisId"], [{"start": moment["start"], "end": moment["end"], "title": ""}]
        )
        assert done["state"] == "done", done


def test_no_moments_is_a_result_with_a_note_not_an_error(studio):
    studio.fake.moments = []
    result = studio.analyze()
    assert result["moments"] == []
    assert result["note"].startswith("There was nothing to measure")


def test_the_address_reported_by_the_site_is_used_only_if_it_passes_the_link_rules(studio):
    studio.fake.reported_url = "https://evil.example/redirected"
    result = studio.analyze(url="https://youtu.be/abcDEF12345")
    assert result["video"]["url"] == "https://youtu.be/abcDEF12345"
    assert studio.fake.named("download_audio")[0][0][0] == "https://youtu.be/abcDEF12345"


def test_a_thumbnail_that_is_not_https_is_dropped(studio, monkeypatch):
    original = studio.fake.inspect_url
    monkeypatch.setattr(engine, "inspect_url", lambda url: {**original(url), "thumbnail": "javascript:alert(1)"})
    assert studio.analyze()["video"]["thumbnail"] == ""


# -------------------------------------------------------------------- jobs


@pytest.mark.parametrize("job_id", ["abcdefgh12345678", "A" * 64, "--------", "________"])
def test_an_unknown_job_is_a_404(studio, job_id):
    assert_error(studio.call("GET", f"/api/jobs/{job_id}"), 404, "job_not_found")
    assert_error(studio.call("POST", f"/api/jobs/{job_id}/cancel"), 404, "job_not_found")


def test_a_running_job_shows_its_step_and_progress(studio):
    entered = studio.fake.hold("download_audio")
    job_id = studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"]
    assert entered.wait(WAIT)
    snapshot = studio.call("GET", f"/api/jobs/{job_id}").json()
    assert snapshot == {
        "id": job_id,
        "kind": "analyze",
        "state": "running",
        "step": "Getting the sound of the video",
        "progress": 0.2,
    }
    studio.fake.let_go()
    assert studio.wait(job_id)["state"] == "done"


def test_the_sound_download_shows_how_far_it_is_and_logs_every_tenth(studio):
    studio.fake.sound_shares = [0.0, 0.05, 0.42, 0.42, 0.97, 1.0, 7.0]
    entered = studio.fake.hold("audio_levels")
    job_id = studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"]
    assert entered.wait(WAIT)
    studio.fake.let_go()
    assert studio.wait(job_id)["state"] == "done"
    lines = [line.split("]", 1)[1].strip() for line in studio.log_text.splitlines() if "sound of the video:" in line]
    assert lines == [f"sound of the video: {percent}% downloaded" for percent in (40, 90, 100)]


def test_the_sound_download_updates_the_job_at_most_once_a_second(studio):
    seen = []

    def work(job):
        report = studio.app._sound_progress(job, 0.2, 0.55)
        for share, pause in ((0.1, 0), (0.5, 0), (0.9, 1.05), (1.0, 0)):
            time.sleep(pause)
            report(share)
            seen.append((job.step, round(job.progress, 3)))

    job = studio.app.jobs.submit("analyze", work)
    assert studio.wait(job.id)["state"] == "done"
    assert seen == [
        ("Getting the sound of the video: 10%", 0.235),
        ("Getting the sound of the video: 10%", 0.235),
        ("Getting the sound of the video: 90%", 0.515),
        ("Getting the sound of the video: 90%", 0.515),
    ]


def test_cancel_stops_the_sound_download_while_it_runs(studio):
    studio.fake.sound_shares = [0.3]
    entered = studio.fake.hold("download_audio")
    job_id = studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"]
    assert entered.wait(WAIT)
    assert studio.call("POST", f"/api/jobs/{job_id}/cancel").status == 200
    studio.fake.let_go()
    assert studio.wait(job_id)["state"] == "cancelled"
    assert studio.fake.named("audio_levels") == [], "the download must stop at its next progress report"


def test_analyze_jobs_queue_and_run_in_the_order_they_were_sent(studio):
    entered = studio.fake.hold("inspect_url")
    links = [f"https://youtu.be/video{n:06d}" for n in range(4)]
    jobs = []
    for number, link in enumerate(links):
        jobs.append(studio.call("POST", "/api/analyze", data={**ANALYZE, "url": link}).json()["jobId"])
        if number == 0:
            assert entered.wait(WAIT)
    states = [studio.call("GET", f"/api/jobs/{job_id}").json() for job_id in jobs]
    assert [s["state"] for s in states] == ["running", "queued", "queued", "queued"]
    assert states[1]["step"] == "Waiting to start" and states[1]["progress"] == 0.0
    assert len(studio.fake.named("inspect_url")) == 1  # only one analysis runs at a time
    studio.fake.let_go()
    for job_id in jobs:
        assert studio.wait(job_id)["state"] == "done"
    assert [args[0] for args, _ in studio.fake.named("inspect_url")] == links


def test_a_render_runs_while_an_analysis_is_running(studio):
    analysis = studio.analyze()
    entered = studio.fake.hold("inspect_url")
    analyze_job = studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"]
    assert entered.wait(WAIT)
    assert studio.render(analysis["analysisId"])["state"] == "done"
    assert studio.call("GET", f"/api/jobs/{analyze_job}").json()["state"] == "running"
    studio.fake.let_go()
    assert studio.wait(analyze_job)["state"] == "done"


def test_cancel_a_waiting_job(studio):
    entered = studio.fake.hold("inspect_url")
    first = studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"]
    assert entered.wait(WAIT)
    second = studio.call("POST", "/api/analyze", data={**ANALYZE, "url": "https://youtu.be/second00000"}).json()[
        "jobId"
    ]
    reply = studio.call("POST", f"/api/jobs/{second}/cancel")
    assert reply.status == 200 and reply.json() == {"id": second, "state": "cancelled"}
    assert_secure(reply)
    studio.fake.let_go()
    assert studio.wait(first)["state"] == "done"
    cancelled = studio.wait(second)
    assert set(cancelled) == {"id", "kind", "state", "step", "progress"} and cancelled["state"] == "cancelled"
    assert len(studio.fake.named("inspect_url")) == 1  # the cancelled job never started


def test_cancel_a_running_analysis_stops_it_before_the_next_step(studio):
    entered = studio.fake.hold("download_audio")
    job_id = studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"]
    assert entered.wait(WAIT)
    reply = studio.call("POST", f"/api/jobs/{job_id}/cancel", data={})
    assert reply.json() == {"id": job_id, "state": "running"}
    assert studio.call("GET", f"/api/jobs/{job_id}").json()["step"] == "Stopping after the current step"
    studio.fake.let_go()
    final = studio.wait(job_id)
    assert final["state"] == "cancelled" and "result" not in final and "error" not in final
    assert studio.fake.named("audio_levels") == [] and studio.fake.named("find_moments") == []
    assert studio.stored(studio.app.settings.work_dir) == []
    # cancelling again, or after it has finished, changes nothing
    assert studio.call("POST", f"/api/jobs/{job_id}/cancel").json() == {"id": job_id, "state": "cancelled"}


def test_cancelling_a_finished_job_leaves_its_result(studio):
    job_id = studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"]
    studio.wait(job_id)
    assert studio.call("POST", f"/api/jobs/{job_id}/cancel").json() == {"id": job_id, "state": "done"}
    assert "result" in studio.call("GET", f"/api/jobs/{job_id}").json()


def test_too_many_waiting_jobs_are_refused(studio):
    entered = studio.fake.hold("inspect_url")
    assert studio.call("POST", "/api/analyze", data=ANALYZE).status == 202
    assert entered.wait(WAIT)
    for _ in range(10):
        assert studio.call("POST", "/api/analyze", data=ANALYZE).status == 202
    error = assert_error(studio.call("POST", "/api/analyze", data=ANALYZE), 429, "too_many_jobs")
    assert "Wait for one to finish" in error["hint"]


def test_finished_jobs_are_forgotten_past_the_cap(studio):
    first = studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"]
    studio.wait(first)
    last = first
    for _ in range(50):
        last = studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"]
        studio.wait(last)
    assert studio.app.jobs.count() == 50
    assert_error(studio.call("GET", f"/api/jobs/{first}"), 404, "job_not_found")
    assert studio.call("GET", f"/api/jobs/{last}").json()["state"] == "done"


# ------------------------------------------------------------------ render


def test_render_from_a_link_gives_the_exact_result_shape(studio):
    analysis = studio.analyze()
    clips = [
        {"start": 40.0, "end": 70.0, "title": "First clip"},
        {"start": 5.5, "end": 20.25, "title": ""},
    ]
    reply = studio.call(
        "POST", "/api/render", data={"analysisId": analysis["analysisId"], "clips": clips, "options": RENDER_OPTIONS}
    )
    assert reply.status == 202 and set(reply.json()) == {"jobId"}
    assert_secure(reply)
    job_id = reply.json()["jobId"]
    done = studio.wait(job_id)
    assert set(done) == {"id", "kind", "state", "step", "progress", "result"}
    assert (done["kind"], done["state"], done["progress"]) == ("render", "done", 1.0)
    folder = studio.app.settings.output_dir / f"abcDEF12345-{job_id[:8]}"
    size = len(b"VIDEO rendered blur 1.3 center")
    assert done["result"] == {
        "folder": str(folder),
        "clips": [
            {
                "index": 1,
                "ok": True,
                "name": "clip-01.mp4",
                "url": f"/output/{folder.name}/clip-01.mp4",
                "seconds": 12.5,
                "bytes": size,
                "start": 40.0,
                "end": 70.0,
                "caption": "First clip\nClip from Test Channel on YouTube",
            },
            {
                "index": 2,
                "ok": True,
                "name": "clip-02.mp4",
                "url": f"/output/{folder.name}/clip-02.mp4",
                "seconds": 12.5,
                "bytes": size,
                "start": 5.5,
                "end": 20.25,
                "caption": "A long stream\nClip from Test Channel on YouTube",
            },
        ],
    }
    # the folder holds the clips and their suggested post text, and nothing else
    assert sorted(p.name for p in folder.iterdir()) == ["clip-01.mp4", "clip-01.txt", "clip-02.mp4", "clip-02.txt"]
    assert (folder / "clip-01.txt").read_text(encoding="utf-8") == "First clip\nClip from Test Channel on YouTube\n"
    # and each one can be fetched at the address given
    for clip in done["result"]["clips"]:
        fetched = studio.call("GET", clip["url"])
        assert fetched.status == 200 and fetched.body == b"VIDEO rendered blur 1.3 center"
        assert fetched.headers["content-type"] == "video/mp4"
    text = studio.call("GET", f"/output/{folder.name}/clip-02.txt")
    assert text.headers["content-type"] == "text/plain; charset=utf-8"
    assert text.body.decode("utf-8") == "A long stream\nClip from Test Channel on YouTube\n"


def test_render_from_a_link_downloads_each_part_then_renders_from_its_start(studio):
    analysis = studio.analyze()
    clips = [{"start": 40.0, "end": 70.0, "title": "One"}, {"start": 80.0, "end": 100.0, "title": "Two"}]
    done = studio.render(analysis["analysisId"], clips, layout="fill", position="left", zoom=1.75)
    fake = studio.fake
    order = [name for name, _, _ in fake.calls if name in ("fetch_section", "build_ass", "render_short")]
    assert order == ["fetch_section", "build_ass", "render_short"] * 2
    sections = fake.named("fetch_section")
    work = sections[0][0][3].parent
    assert work.parent == studio.app.settings.work_dir
    assert [(a[0], a[1], a[2], a[3].name) for a, _ in sections] == [
        (VIDEO_URL, 40.0, 70.0, "section-01.mp4"),
        (VIDEO_URL, 80.0, 100.0, "section-02.mp4"),
    ]
    out_dir = Path(done["result"]["folder"])
    for number, (args, kwargs) in enumerate(fake.named("render_short"), 1):
        segment, seek, length, out = args
        assert segment == work / f"section-{number:02d}.mp4" and kwargs["segment_existed"]
        assert seek == 0.0  # the downloaded part already starts at the right place
        assert length == (30.0 if number == 1 else 20.0)
        assert out == out_dir / f"clip-{number:02d}.mp4"
        assert (kwargs["layout"], kwargs["position"], kwargs["zoom"]) == ("fill", "left", 1.75)
        assert kwargs["ass_file"] == f"clip-{number:02d}.ass"
        # each downloaded part is removed as soon as its clip is made, so they never pile up
        assert kwargs["beside_segment"] == [f"section-{number:02d}.mp4"]
    assert not work.exists()
    assert studio.stored(studio.app.settings.work_dir) == []
    assert not list(out_dir.glob("*.ass"))


def test_render_from_a_file_cuts_straight_from_the_file(studio):
    upload = studio.upload("My holiday.mp4")
    analysis = studio.analyze(url=None, uploadId=upload["uploadId"])
    done = studio.render(analysis["analysisId"], [{"start": 12.5, "end": 42.5, "title": "From a file"}])
    stored = studio.app.settings.uploads_dir / f"{upload['uploadId']}.mp4"
    assert studio.fake.named("fetch_section") == []
    (args, kwargs) = studio.fake.named("render_short")[0]
    assert args[0] == stored and args[1] == 12.5 and args[2] == 30.0
    assert kwargs["ass_file"] is None  # an uploaded file has no words to caption
    clip = done["result"]["clips"][0]
    assert clip["ok"] and clip["caption"] == "From a file"  # no channel, so no credit line
    folder = Path(done["result"]["folder"])
    assert folder.name.startswith("My-holiday-") and folder.parent == studio.app.settings.output_dir
    assert stored.is_file()  # the upload is never deleted by a render


def test_captions_are_written_next_to_the_output_and_passed_by_name(studio):
    analysis = studio.analyze()
    studio.render(analysis["analysisId"], [{"start": 40.0, "end": 50.0, "title": "Shown title"}], showTitle=True)
    words, start, end, title = studio.fake.named("build_ass")[0][0]
    assert (start, end, title) == (40.0, 50.0, "Shown title")
    assert words == [w for w in WORDS if 40.0 <= w["t"] < 50.0]
    kwargs = studio.fake.named("render_short")[0][1]
    assert kwargs["ass_file"] == "clip-01.ass"
    assert kwargs["ass_text"] == "ASS words=20 start=40.0 end=50.0 title=Shown title\n"


def test_the_title_is_left_out_of_the_captions_unless_asked_for(studio):
    analysis = studio.analyze()
    studio.render(analysis["analysisId"], [{"start": 40.0, "end": 50.0, "title": "Hidden title"}], showTitle=False)
    assert studio.fake.named("build_ass")[0][0][3] == ""


@pytest.mark.parametrize(
    ("setup", "options", "expect_file", "expect_words", "expect_title"),
    [
        ("normal", {"captions": True, "showTitle": True}, True, 20, "T"),
        ("normal", {"captions": True, "showTitle": False}, True, 20, ""),
        ("normal", {"captions": False, "showTitle": False}, False, 0, ""),
        ("normal", {"captions": False, "showTitle": True}, True, 0, "T"),  # the title alone
        ("no_filter", {"captions": True, "showTitle": True}, False, 0, ""),
        ("no_words", {"captions": True, "showTitle": False}, False, 0, ""),
        ("no_words", {"captions": True, "showTitle": True}, True, 0, "T"),
    ],
)
def test_when_a_captions_file_is_made(studio, setup, options, expect_file, expect_words, expect_title):
    if setup == "no_filter":
        studio.fake.tools["captions_filter"] = False
    if setup == "no_words":
        studio.fake.words = []
    analysis = studio.analyze()
    done = studio.render(analysis["analysisId"], [{"start": 40.0, "end": 50.0, "title": "T"}], **options)
    assert done["result"]["clips"][0]["ok"]
    kwargs = studio.fake.named("render_short")[0][1]
    if expect_file:
        words, _, _, title = studio.fake.named("build_ass")[0][0]
        assert kwargs["ass_file"] == "clip-01.ass" and len(words) == expect_words and title == expect_title
    else:
        assert kwargs["ass_file"] is None and studio.fake.named("build_ass") == []


def test_one_failing_clip_does_not_stop_the_others(studio):
    analysis = studio.analyze()
    studio.fake.fail["render_short"] = lambda number: (
        RuntimeError("ffmpeg could not make the clip: Invalid data found") if number == 2 else None
    )
    clips = [{"start": 10.0 * n, "end": 10.0 * n + 10, "title": f"Clip {n}"} for n in range(1, 4)]
    done = studio.render(analysis["analysisId"], clips)
    assert done["state"] == "done"
    results = done["result"]["clips"]
    assert [clip["ok"] for clip in results] == [True, False, True]
    assert results[1] == {
        "index": 2,
        "ok": False,
        "error": "ffmpeg could not make the clip: Invalid data found",
        "name": "clip-02.mp4",
        "url": "",
        "seconds": 0.0,
        "bytes": 0,
        "start": 20.0,
        "end": 30.0,
        "caption": "Clip 2\nClip from Test Channel on YouTube",
    }
    assert "error" not in results[0] and "error" not in results[2]
    assert results[2]["url"].endswith("/clip-03.mp4")
    folder = Path(done["result"]["folder"])
    assert sorted(p.name for p in folder.iterdir()) == ["clip-01.mp4", "clip-01.txt", "clip-03.mp4", "clip-03.txt"]
    assert "Traceback" not in json.dumps(done) and "Traceback (most recent call last)" in studio.log_text
    assert studio.stored(studio.app.settings.work_dir) == []


def test_a_failing_download_of_one_part_is_reported_for_that_clip_only(studio):
    analysis = studio.analyze()
    studio.fake.fail["fetch_section"] = lambda number: (
        RuntimeError("HTTP Error 429: Too Many Requests") if number == 1 else None
    )
    clips = [{"start": 10.0, "end": 20.0, "title": "A"}, {"start": 30.0, "end": 40.0, "title": "B"}]
    results = studio.render(analysis["analysisId"], clips)["result"]["clips"]
    assert [clip["ok"] for clip in results] == [False, True]
    assert results[0]["error"].startswith("YouTube is limiting requests")
    assert len(studio.fake.named("render_short")) == 1


def test_every_clip_failing_is_still_a_finished_job_with_the_reasons(studio):
    analysis = studio.analyze()
    studio.fake.fail["render_short"] = KeyError("stream")
    results = studio.render(analysis["analysisId"])["result"]["clips"]
    assert [clip["ok"] for clip in results] == [False]
    assert results[0]["error"] == "'stream'" or results[0]["error"]
    assert "Traceback" not in results[0]["error"]


def test_render_progress_moves_clip_by_clip(studio):
    analysis = studio.analyze()
    entered = studio.fake.hold("render_short")
    clips = [{"start": 10.0, "end": 20.0, "title": "A"}, {"start": 30.0, "end": 40.0, "title": "B"}]
    job_id = studio.call(
        "POST", "/api/render", data={"analysisId": analysis["analysisId"], "clips": clips, "options": RENDER_OPTIONS}
    ).json()["jobId"]
    assert entered.wait(WAIT)
    first = studio.call("GET", f"/api/jobs/{job_id}").json()
    assert first["state"] == "running" and first["step"] == "Clip 1 of 2: making the vertical video"
    assert first["progress"] == 0.25
    studio.fake.let_go()
    assert studio.wait(job_id)["progress"] == 1.0


def test_cancelling_a_render_stops_before_the_next_clip_and_removes_its_folder(studio):
    analysis = studio.analyze()
    entered = studio.fake.hold("render_short")
    clips = [{"start": 10.0 * n, "end": 10.0 * n + 10, "title": ""} for n in range(1, 5)]
    job_id = studio.call(
        "POST", "/api/render", data={"analysisId": analysis["analysisId"], "clips": clips, "options": RENDER_OPTIONS}
    ).json()["jobId"]
    assert entered.wait(WAIT)
    assert studio.call("POST", f"/api/jobs/{job_id}/cancel").json() == {"id": job_id, "state": "running"}
    studio.fake.let_go()
    final = studio.wait(job_id)
    assert final["state"] == "cancelled" and "result" not in final
    assert len(studio.fake.named("render_short")) == 1
    assert studio.stored(studio.app.settings.output_dir) == []
    assert studio.stored(studio.app.settings.work_dir) == []


def test_only_one_render_runs_at_a_time(studio):
    analysis = studio.analyze()
    entered = studio.fake.hold("render_short")
    body = {
        "analysisId": analysis["analysisId"],
        "clips": [{"start": 10.0, "end": 20.0, "title": ""}],
        "options": RENDER_OPTIONS,
    }
    first = studio.call("POST", "/api/render", data=body).json()["jobId"]
    second = studio.call("POST", "/api/render", data=body).json()["jobId"]
    assert entered.wait(WAIT)
    assert studio.call("GET", f"/api/jobs/{second}").json()["state"] == "queued"
    assert len(studio.fake.named("render_short")) == 1
    studio.fake.let_go()
    first_done, second_done = studio.wait(first), studio.wait(second)
    assert first_done["result"]["folder"] != second_done["result"]["folder"]


def render_body(analysis_id: Any, **changes) -> dict:
    body: dict[str, Any] = {
        "analysisId": analysis_id,
        "clips": [{"start": 40.0, "end": 70.0, "title": "First clip"}],
        "options": dict(RENDER_OPTIONS),
    }
    for key, value in changes.items():
        if key in ("clips", "options", "analysisId"):
            body[key] = value
        elif key in RENDER_OPTIONS:
            body["options"][key] = value
        else:
            body["clips"][0][key] = value
    return body


LONG_TITLE = "T" * 81


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"analysisId": None}, 400, "invalid_analysis_id"),
        ({"analysisId": 42}, 400, "invalid_analysis_id"),
        ({"analysisId": ""}, 400, "invalid_analysis_id"),
        ({"analysisId": "../../x"}, 400, "invalid_analysis_id"),
        ({"analysisId": ["abcdefgh12345678"]}, 400, "invalid_analysis_id"),
        ({"analysisId": "abcdefgh12345678"}, 404, "analysis_not_found"),
        ({"clips": None}, 400, "invalid_clips"),
        ({"clips": "all"}, 400, "invalid_clips"),
        ({"clips": {"start": 40, "end": 70}}, 400, "invalid_clips"),
        ({"clips": []}, 400, "no_clips"),
        ({"clips": [{"start": 10.0, "end": 20.0, "title": ""}] * 11}, 400, "too_many_clips"),
        ({"clips": ["40-70"]}, 400, "invalid_clip"),
        ({"clips": [None]}, 400, "invalid_clip"),
        ({"clips": [[40, 70]]}, 400, "invalid_clip"),
        (
            {"clips": [{"start": 10.0, "end": 20.0, "title": ""}, {"start": 30.0, "end": 31.0, "title": ""}]},
            400,
            "clip_too_short",
        ),
        ({"start": None}, 400, "invalid_clip_times"),
        ({"start": "40"}, 400, "invalid_clip_times"),
        ({"start": True}, 400, "invalid_clip_times"),
        ({"start": [40]}, 400, "invalid_clip_times"),
        ({"end": None}, 400, "invalid_clip_times"),
        ({"end": "70"}, 400, "invalid_clip_times"),
        ({"start": float("nan")}, 400, "invalid_clip_times"),
        ({"end": float("inf")}, 400, "invalid_clip_times"),
        ({"start": -1.0, "end": 20.0}, 400, "invalid_clip_times"),
        ({"start": 70.0, "end": 40.0}, 400, "invalid_clip_times"),
        ({"start": 50.0, "end": 50.0}, 400, "invalid_clip_times"),
        ({"start": 100.0, "end": 121.0}, 400, "clip_outside_video"),
        ({"start": 500.0, "end": 530.0}, 400, "clip_outside_video"),
        ({"start": 40.0, "end": 47.9}, 400, "clip_too_short"),
        ({"start": 40.0, "end": 40.001}, 400, "clip_too_short"),
        ({"start": 10.0, "end": 100.5}, 400, "clip_too_long"),
        ({"start": 0.0, "end": 120.0}, 400, "clip_too_long"),
        ({"title": 42}, 400, "invalid_title"),
        ({"title": ["a"]}, 400, "invalid_title"),
        ({"title": {"text": "a"}}, 400, "invalid_title"),
        ({"title": LONG_TITLE}, 400, "title_too_long"),
        ({"options": "blur"}, 400, "invalid_options"),
        ({"options": ["blur"]}, 400, "invalid_options"),
        ({"layout": "crop"}, 400, "invalid_layout"),
        ({"layout": None}, 400, "invalid_layout"),
        ({"layout": "blur,scale=1"}, 400, "invalid_layout"),
        ({"layout": ["blur"]}, 400, "invalid_layout"),
        ({"position": "top"}, 400, "invalid_position"),
        ({"position": 1}, 400, "invalid_position"),
        ({"position": "iw-ow;movie=/etc/passwd"}, 400, "invalid_position"),
        ({"zoom": "big"}, 400, "invalid_zoom"),
        ({"zoom": None}, 400, "invalid_zoom"),
        ({"zoom": True}, 400, "invalid_zoom"),
        ({"zoom": float("nan")}, 400, "invalid_zoom"),
        ({"zoom": [1.3]}, 400, "invalid_zoom"),
        ({"captions": "yes"}, 400, "invalid_captions"),
        ({"captions": 1}, 400, "invalid_captions"),
        ({"captions": None}, 400, "invalid_captions"),
        ({"showTitle": "no"}, 400, "invalid_show_title"),
        ({"showTitle": 0}, 400, "invalid_show_title"),
        ({"showTitle": None}, 400, "invalid_show_title"),
    ],
)
def test_render_checks_every_field(studio, changes, status, code):
    analysis_id = studio.analyze()["analysisId"]
    reply = studio.call("POST", "/api/render", data=render_body(analysis_id, **changes))
    error = assert_error(reply, status, code)
    if code.startswith("clip_") or code in ("invalid_clip", "invalid_clip_times", "invalid_title", "title_too_long"):
        assert "lip " in error["message"]  # says which clip
    assert [job.kind for job in studio.app.jobs.active()] == []
    assert studio.fake.named("render_short") == [] and studio.fake.named("fetch_section") == []
    assert studio.stored(studio.app.settings.output_dir) == []


def test_the_error_names_the_clip_that_is_wrong(studio):
    analysis_id = studio.analyze()["analysisId"]
    clips = [
        {"start": 10.0, "end": 20.0, "title": ""},
        {"start": 30.0, "end": 40.0, "title": ""},
        {"start": 50.0, "end": 51.0, "title": ""},
    ]
    error = assert_error(
        studio.call("POST", "/api/render", data=render_body(analysis_id, clips=clips)), 400, "clip_too_short"
    )
    assert error["message"] == "Clip 3 is shorter than 8 seconds."


@pytest.mark.parametrize(
    ("sent", "used"), [(1.0, 1.0), (2.0, 2.0), (1.5, 1.5), (5, 2.0), (0.2, 1.0), (-3, 1.0), (1e308, 2.0), (2, 2.0)]
)
def test_zoom_is_clamped_between_one_and_two(studio, sent, used):
    analysis_id = studio.analyze()["analysisId"]
    assert studio.render(analysis_id, zoom=sent)["state"] == "done"
    assert studio.fake.named("render_short")[0][1]["zoom"] == used


@pytest.mark.parametrize(
    ("start", "end"), [(0.0, 8.0), (0, 8), (30.0, 120.0), (112.0, 120.0), (10.5, 100.5), (100.0, 120.04)]
)
def test_clips_at_the_edges_of_the_limits_are_accepted(studio, start, end):
    analysis_id = studio.analyze()["analysisId"]
    done = studio.render(analysis_id, [{"start": start, "end": end, "title": ""}])
    clip = done["result"]["clips"][0]
    assert clip["ok"] and clip["start"] == float(start) and clip["end"] == min(float(end), 120.0)


def test_title_limits_and_cleaning(studio):
    analysis_id = studio.analyze()["analysisId"]
    clips = [
        {"start": 10.0, "end": 20.0, "title": "T" * 80},
        {"start": 10.0, "end": 20.0, "title": "  two\nlines\tand {braces} \\N  "},
        {"start": 10.0, "end": 20.0, "title": "नमस्ते दोस्तों"},
        {"start": 10.0, "end": 20.0},
        {"start": 10.0, "end": 20.0, "title": None},
        {"start": 10.0, "end": 20.0, "title": "; rm -rf / && $(reboot) `id` | ../../x"},
    ]
    done = studio.render(analysis_id, clips, showTitle=True)
    assert done["state"] == "done"
    titles = [args[3] for args, _ in studio.fake.named("build_ass")]
    assert titles == [
        "T" * 80,
        "two lines and {braces} \\N",
        "नमस्ते दोस्तों",
        "",
        "",
        "; rm -rf / && $(reboot) `id` | ../../x",
    ]
    # whatever the title says, the files are always named clip-NN
    folder = Path(done["result"]["folder"])
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        f"clip-{n:02d}.{ext}" for n in range(1, 7) for ext in ("mp4", "txt")
    )
    for args, kwargs in studio.fake.named("render_short"):
        assert kwargs["ass_file"] in (None, f"{args[3].stem}.ass")
    assert done["result"]["clips"][3]["caption"].startswith("A long stream")


def test_missing_options_use_the_defaults(studio):
    analysis_id = studio.analyze()["analysisId"]
    reply = studio.call(
        "POST", "/api/render", data={"analysisId": analysis_id, "clips": [{"start": 40.0, "end": 70.0, "title": "x"}]}
    )
    assert reply.status == 202
    assert studio.wait(reply.json()["jobId"])["state"] == "done"
    kwargs = studio.fake.named("render_short")[0][1]
    assert (kwargs["layout"], kwargs["zoom"], kwargs["position"], kwargs["ass_file"]) == (
        "blur",
        1.3,
        "center",
        "clip-01.ass",
    )
    assert studio.fake.named("build_ass")[0][0][3] == ""


def test_ten_clips_are_allowed(studio):
    analysis_id = studio.analyze()["analysisId"]
    clips = [{"start": float(n), "end": float(n) + 10, "title": f"Clip {n}"} for n in range(10)]
    done = studio.render(analysis_id, clips)
    assert [clip["name"] for clip in done["result"]["clips"]] == [f"clip-{n:02d}.mp4" for n in range(1, 11)]
    assert all(clip["ok"] for clip in done["result"]["clips"])


def test_render_needs_the_tools(studio):
    analysis_id = studio.analyze()["analysisId"]
    studio.fake.tools["ffmpeg"] = None
    studio.app.tools(refresh=True)
    assert_error(studio.call("POST", "/api/render", data=render_body(analysis_id)), 503, "tool_missing")


def test_render_after_the_uploaded_file_has_gone(studio):
    upload = studio.upload()
    analysis_id = studio.analyze(url=None, uploadId=upload["uploadId"])["analysisId"]
    (studio.app.settings.uploads_dir / f"{upload['uploadId']}.mp4").unlink()
    error = assert_error(studio.call("POST", "/api/render", data=render_body(analysis_id)), 404, "source_missing")
    assert error["hint"] == "Choose the file again."


def test_only_the_newest_analyses_are_remembered(studio):
    first = studio.app.add_analysis({"kind": "url", "url": VIDEO_URL, "id": "a", "duration": 120.0, "words": []})
    others = [
        studio.app.add_analysis({"kind": "url", "url": VIDEO_URL, "id": "a", "duration": 120.0, "words": []})
        for _ in range(20)
    ]
    assert_error(studio.call("POST", "/api/render", data=render_body(first)), 404, "analysis_not_found")
    assert studio.call("POST", "/api/render", data=render_body(others[0])).status == 202
    assert studio.call("POST", "/api/render", data=render_body(others[-1])).status == 202


def test_the_output_folder_name_is_made_only_of_safe_characters(studio, monkeypatch):
    original = studio.fake.inspect_url
    monkeypatch.setattr(
        engine, "inspect_url", lambda url: {**original(url), "id": "../../etc/passwd; rm -rf ~ \\\\x C:"}
    )
    done = studio.render(studio.analyze()["analysisId"])
    folder = Path(done["result"]["folder"])
    assert folder.parent == studio.app.settings.output_dir
    assert web._OUTPUT_DIR.fullmatch(folder.name) and folder.name.startswith("etc-passwd-rm-rf-x-C-")
    assert studio.call("GET", done["result"]["clips"][0]["url"]).status == 200


# ------------------------------------------------------- ranges on outputs


@pytest.fixture
def clip(studio):
    """A 1000-byte "clip" in the output folder. Returns its address and its bytes."""
    folder = studio.app.settings.output_dir / "demo-abc12345"
    folder.mkdir()
    content = bytes(n % 251 for n in range(1000))
    (folder / "clip-01.mp4").write_bytes(content)
    (folder / "clip-01.txt").write_text("Post text\n", encoding="utf-8")
    (folder / "clip-02.mp4").write_bytes(b"")
    (folder / "clip-01.ass").write_text(SECRET, encoding="utf-8")
    (folder / "notes.md").write_text(SECRET, encoding="utf-8")
    return "/output/demo-abc12345/clip-01.mp4", content


def test_a_whole_clip_is_served_and_says_it_takes_ranges(studio, clip):
    path, content = clip
    reply = studio.call("GET", path)
    assert reply.status == 200 and reply.body == content
    assert reply.headers["content-type"] == "video/mp4"
    assert reply.headers["accept-ranges"] == "bytes"
    assert reply.headers["content-length"] == "1000"
    assert "content-range" not in reply.headers
    assert_secure(reply)


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [
        ("bytes=0-99", 0, 99),
        ("bytes=0-0", 0, 0),
        ("bytes=500-599", 500, 599),
        ("bytes=250-250", 250, 250),
        ("bytes=999-999", 999, 999),
        ("bytes=900-", 900, 999),
        ("bytes=0-", 0, 999),
        ("bytes=999-", 999, 999),
        ("bytes=-100", 900, 999),
        ("bytes=-1", 999, 999),
        ("bytes=-1000", 0, 999),
        ("bytes=-5000", 0, 999),  # a suffix longer than the file is the whole file
        ("bytes=990-2000", 990, 999),  # an end past the file is cut at the last byte
        ("bytes=0-999", 0, 999),
        ("bytes=0-99999999999", 0, 999),
        ("BYTES=10-19", 10, 19),
        (" bytes = 10 - 19 ", 10, 19),
    ],
)
def test_a_single_range_is_served_as_206(studio, clip, header, first, last):
    path, content = clip
    reply = studio.call("GET", path, headers={"Range": header})
    assert reply.status == 206
    assert reply.body == content[first : last + 1]
    assert reply.headers["content-range"] == f"bytes {first}-{last}/1000"
    assert reply.headers["content-length"] == str(last - first + 1)
    assert reply.headers["accept-ranges"] == "bytes"
    assert reply.headers["content-type"] == "video/mp4"
    assert_secure(reply)


@pytest.mark.parametrize(
    "header", ["bytes=1000-", "bytes=1000-1100", "bytes=5000-6000", "bytes=-0", "bytes=99999999999-"]
)
def test_a_range_outside_the_file_is_416(studio, clip, header):
    path, _ = clip
    reply = studio.call("GET", path, headers={"Range": header})
    assert_error(reply, 416, "range_not_satisfiable")
    assert reply.headers["content-range"] == "bytes */1000"
    assert reply.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize(
    "header",
    [
        "bytes=0-9,20-29",
        "bytes=abc",
        "bytes=",
        "bytes=-",
        "items=0-9",
        "0-9",
        "bytes=500-100",
        "bytes=1-2-3",
        "bytes=--5",
        "bytes=0x10-0x20",
        "bytes=" + "9" * 40 + "-",
    ],
)
def test_ranges_the_server_does_not_handle_get_the_whole_file(studio, clip, header):
    path, content = clip
    reply = studio.call("GET", path, headers={"Range": header})
    assert reply.status == 200 and reply.body == content
    assert "content-range" not in reply.headers


def test_if_range_gets_the_whole_file_because_no_validator_can_match(studio, clip):
    path, content = clip
    reply = studio.call("GET", path, headers={"Range": "bytes=0-9", "If-Range": '"some-etag"'})
    assert reply.status == 200 and reply.body == content


def test_head_on_a_clip_gives_the_size_and_no_body(studio, clip):
    path, _ = clip
    reply = studio.call("HEAD", path)
    assert reply.status == 200 and reply.body == b""
    assert reply.headers["content-length"] == "1000"
    assert reply.headers["accept-ranges"] == "bytes"
    assert reply.headers["content-type"] == "video/mp4"
    assert_secure(reply)


def test_head_with_a_range_gives_the_206_headers_and_no_body(studio, clip):
    path, _ = clip
    reply = studio.call("HEAD", path, headers={"Range": "bytes=100-199"})
    assert reply.status == 206 and reply.body == b""
    assert reply.headers["content-range"] == "bytes 100-199/1000"
    assert reply.headers["content-length"] == "100"
    unsatisfiable = studio.call("HEAD", path, headers={"Range": "bytes=2000-"})
    assert unsatisfiable.status == 416 and unsatisfiable.body == b""
    assert unsatisfiable.headers["content-range"] == "bytes */1000"


def test_several_ranges_on_one_connection_like_a_video_player_seeking(studio, clip):
    path, content = clip
    connection = http.client.HTTPConnection("127.0.0.1", studio.port, timeout=WAIT)
    try:
        for first, last in [(0, 1), (0, 999), (800, 999), (300, 450), (999, 999)]:
            reply = Studio.send(connection, "GET", path, headers={"Range": f"bytes={first}-{last}"})
            assert reply.status == 206 and reply.body == content[first : last + 1]
        assert Studio.send(connection, "GET", path, headers={"Range": "bytes=1000-"}).status == 416
        assert Studio.send(connection, "GET", path).body == content
    finally:
        connection.close()


def test_an_empty_clip_and_the_text_file(studio, clip):
    empty = studio.call("GET", "/output/demo-abc12345/clip-02.mp4")
    assert empty.status == 200 and empty.body == b"" and empty.headers["content-length"] == "0"
    assert studio.call("GET", "/output/demo-abc12345/clip-02.mp4", headers={"Range": "bytes=0-"}).status == 416
    text = studio.call("GET", "/output/demo-abc12345/clip-01.txt", headers={"Range": "bytes=0-3"})
    assert text.status == 206 and text.body == b"Post" and text.headers["content-type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize(
    "path",
    [
        "/output/demo-abc12345/clip-01.ass",
        "/output/demo-abc12345/notes.md",
        "/output/demo-abc12345/clip-03.mp4",
        "/output/demo-abc12345/clip-1.mp4",
        "/output/demo-abc12345/CLIP-01.MP4",
        "/output/demo-abc12345/clip-01.mp4.",
        "/output/demo-abc12345/clip-01.mp4%20",
        "/output/other/clip-01.mp4",
        "/output/clip-01.mp4",
        "/output/demo-abc12345/sub/clip-01.mp4",
    ],
)
def test_only_clips_and_their_text_can_be_fetched_from_output(studio, clip, path):
    reply = studio.call("GET", path)
    assert_error(reply, 404, "not_found")


def test_a_player_that_hangs_up_mid_file_does_not_upset_the_server(studio, clip, monkeypatch):
    folder = studio.app.settings.output_dir / "demo-abc12345"
    (folder / "clip-03.mp4").write_bytes(os.urandom(8 * 1024 * 1024))
    request = f"GET /output/demo-abc12345/clip-03.mp4 HTTP/1.1\r\nHost: 127.0.0.1:{studio.port}\r\n\r\n"
    with socket.create_connection(("127.0.0.1", studio.port), timeout=WAIT) as sock:
        sock.sendall(request.encode())
        assert sock.recv(1024).startswith(b"HTTP/1.1 200 ")
        # linger on, zero seconds: closing now resets the connection, like a player that seeks away
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH" if os.name == "nt" else "ii", 1, 0))
    time.sleep(0.1)
    assert studio.call("GET", "/api/status").status == 200
    assert "Traceback" not in studio.log_text


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-99", 1000, (0, 99)),
        ("bytes=-100", 1000, (900, 999)),
        ("bytes=900-", 1000, (900, 999)),
        ("bytes=990-5000", 1000, (990, 999)),
        ("bytes=1000-", 1000, "unsatisfiable"),
        ("bytes=-0", 1000, "unsatisfiable"),
        ("bytes=0-", 0, "unsatisfiable"),
        ("bytes=-5", 0, "unsatisfiable"),
        ("bytes=5-1", 1000, None),
        ("bytes=0-1,5-6", 1000, None),
        ("bytes=", 1000, None),
        ("bytes=-", 1000, None),
        ("chunks=0-1", 1000, None),
        ("bytes=\u0661-\u0662", 1000, None),
    ],
)
def test_parse_range(header, size, expected):
    assert parse_range(header, size) == expected


# ----------------------------------------------------------------- cleanup


def age(path: Path, hours: float) -> None:
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


def test_old_work_uploads_and_output_are_removed_and_new_ones_kept(studio):
    settings = studio.app.settings
    old, new = [], []
    for folder in (settings.work_dir, settings.uploads_dir, settings.output_dir):
        for label, hours, keep in (("old", 73, old), ("new", 71, new)):
            directory = folder / f"{label}-folder"
            directory.mkdir()
            (directory / "clip-01.mp4").write_bytes(b"VIDEO")
            single = folder / f"{label}-file.mp4"
            single.write_bytes(b"VIDEO")
            age(directory, hours)
            age(single, hours)
            keep += [directory, single]
    removed = studio.app.cleanup()
    assert sorted(removed) == sorted(old)
    assert not any(path.exists() for path in old)
    assert all(path.exists() for path in new)
    for folder in (settings.work_dir, settings.uploads_dir, settings.output_dir):
        assert (folder / MARKER).is_file()
    assert (settings.data_dir / "secret.txt").is_file()  # only the three folders are touched
    assert "removed 6 old item(s)" in studio.log_text


def test_the_keep_time_comes_from_the_settings(tmp_path, monkeypatch):
    FakeEngine().install(monkeypatch)
    app = App(
        Settings(port=0, data_dir=tmp_path / "data", keep_hours=2), public_dir=make_public(tmp_path), log=lambda m: None
    )
    try:
        three, one = app.settings.output_dir / "three-hours", app.settings.output_dir / "one-hour"
        three.mkdir()
        one.mkdir()
        age(three, 3)
        age(one, 1)
        assert app.cleanup() == [three]
        assert app.cleanup(now=time.time() + 2 * 3600) == [one]
    finally:
        app.close()


def test_cleanup_runs_when_the_app_starts(tmp_path, monkeypatch):
    FakeEngine().install(monkeypatch)
    settings = Settings(port=0, data_dir=tmp_path / "data")
    App(settings, public_dir=make_public(tmp_path), log=lambda m: None).close()  # makes the folders
    old, new = settings.output_dir / "old-clips", settings.output_dir / "new-clips"
    stale_upload = settings.uploads_dir / "0123456789abcdef.mp4"
    for path in (old, new):
        path.mkdir()
    stale_upload.write_bytes(b"VIDEO")
    age(old, 100)
    age(stale_upload, 100)
    logs: list[str] = []
    App(settings, public_dir=tmp_path / "public", log=logs.append).close()
    assert not old.exists() and not stale_upload.exists() and new.exists()
    assert any("removed 2 old item(s)" in line for line in logs)


def test_cleanup_runs_after_each_job(studio):
    old = studio.app.settings.output_dir / "old-clips"
    old.mkdir()
    age(old, 100)
    studio.analyze()
    deadline = time.monotonic() + WAIT
    while old.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not old.exists()


def test_cleanup_also_runs_after_a_job_that_failed(studio):
    old = studio.app.settings.work_dir / "job-left-behind"
    old.mkdir()
    age(old, 100)
    studio.fake.fail["inspect_url"] = RuntimeError("boom")
    studio.wait(studio.call("POST", "/api/analyze", data=ANALYZE).json()["jobId"])
    deadline = time.monotonic() + WAIT
    while old.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not old.exists()


def test_files_a_job_is_using_are_not_removed(studio):
    upload = studio.upload()
    stored = studio.app.settings.uploads_dir / f"{upload['uploadId']}.mp4"
    age(stored, 100)
    with studio.app.in_use(stored):
        with studio.app.in_use(stored):
            assert studio.app.cleanup() == []
        assert studio.app.cleanup() == []  # still held by the outer user
        assert stored.is_file()
    assert studio.app.cleanup() == [stored]


def test_using_an_upload_keeps_it_from_looking_old(studio):
    upload = studio.upload()
    stored = studio.app.settings.uploads_dir / f"{upload['uploadId']}.mp4"
    age(stored, 100)
    # the tidy-up that follows every job runs just after the job reports that it is done: wait for it, so that
    # it cannot meet the file at the moment this test makes it look old again
    tidied = threading.Event()
    tidy = studio.app._cleanup_quietly
    studio.app._cleanup_quietly = lambda: (tidy(), tidied.set())
    analysis_id = studio.analyze(url=None, uploadId=upload["uploadId"])["analysisId"]
    assert tidied.wait(WAIT)
    studio.app._cleanup_quietly = tidy
    assert stored.is_file() and time.time() - stored.stat().st_mtime < 60
    age(stored, 100)
    assert studio.render(analysis_id)["result"]["clips"][0]["ok"]
    assert stored.is_file() and time.time() - stored.stat().st_mtime < 60


def test_an_upload_removed_by_cleanup_can_no_longer_be_used(studio):
    upload = studio.upload()
    age(studio.app.settings.uploads_dir / f"{upload['uploadId']}.mp4", 100)
    assert len(studio.app.cleanup()) == 1
    assert studio.app.get_upload(upload["uploadId"]) is None
    reply = studio.call(
        "POST", "/api/analyze", data={"uploadId": upload["uploadId"], "clipLength": 30, "count": 5, "permission": True}
    )
    assert_error(reply, 404, "upload_not_found")


def test_a_folder_the_app_did_not_make_is_never_cleaned(tmp_path, monkeypatch):
    FakeEngine().install(monkeypatch)
    data = tmp_path / "data"
    (data / "output").mkdir(parents=True)
    precious = data / "output" / "wedding-video.mp4"
    precious.write_bytes(b"VIDEO")
    age(precious, 10000)
    logs: list[str] = []
    app = App(Settings(port=0, data_dir=data), public_dir=make_public(tmp_path), log=logs.append)
    try:
        assert not (data / "output" / MARKER).exists()
        assert (data / "work" / MARKER).is_file() and (data / "uploads" / MARKER).is_file()
        assert app.cleanup() == [] and precious.is_file()
        assert any("will not be removed" in line for line in logs)
    finally:
        app.close()


def test_cleanup_removes_a_link_but_not_what_it_points_at(studio):
    target = studio.root / "keep-me"
    target.mkdir()
    (target / "file.txt").write_text("keep", encoding="utf-8")
    link = studio.app.settings.output_dir / "old-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this system does not allow making symbolic links")
    # the clock is moved instead of the link's own time, which Windows cannot set without following the link
    assert studio.app.cleanup() == []
    assert studio.app.cleanup(now=time.time() + 100 * 3600) == [link]
    assert not link.is_symlink() and (target / "file.txt").read_text(encoding="utf-8") == "keep"


# ------------------------------------------------------- server and helpers


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("holiday.mp4", ("holiday", ".mp4")),
        ("Holiday.Final.MOV", ("Holiday.Final", ".mov")),
        ("...mp4", ("..", ".mp4")),
        ("a..mkv", ("a.", ".mkv")),
        (".mp4", (".mp4", "")),
        ("mp4", ("mp4", "")),
        ("name.", ("name.", "")),
        ("", ("", "")),
    ],
)
def test_split_extension_gives_the_same_answer_on_every_python_version(name, expected):
    assert split_extension(name) == expected


def test_the_next_free_port_is_used_when_the_first_is_taken(studio):
    taken = studio.port
    logs: list[str] = []
    studio.app.log = logs.append
    second = open_server(studio.app, "127.0.0.1", taken)
    try:
        assert taken < second.port < taken + 20
        assert any(f"Port {taken} is not available" in line for line in logs)
        assert server_url(second) == f"http://127.0.0.1:{second.port}"
    finally:
        second.server_close()


def test_no_free_port_is_a_clear_error(studio):
    with pytest.raises(RuntimeError, match="No free port found"):
        open_server(studio.app, "127.0.0.1", studio.port, attempts=1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("localhost:5180", ("localhost", 5180)),
        ("LOCALHOST:5180", ("localhost", 5180)),
        ("127.0.0.1", ("127.0.0.1", None)),
        ("[::1]:5180", ("[::1]", 5180)),
        ("[::1]", ("[::1]", None)),
        ("[::1", None),
        ("[::1]x", None),
        ("a:b", None),
        ("a:1:2", None),
        ("a:", None),
        (":80", None),
        ("a:123456", None),
        ("", None),
    ],
)
def test_split_host(value, expected):
    assert split_host(value) == expected


@pytest.mark.parametrize(
    ("start", "end", "duration", "expected"),
    [
        (40.0, 70.0, 120.0, (40.0, 70.0)),
        (10.0, 15.0, 120.0, (10.0, 18.0)),  # too short: made longer
        (116.0, 119.0, 120.0, (112.0, 120.0)),  # too short at the very end: moved back
        (10.0, 150.0, 300.0, (10.0, 100.0)),  # too long: cut
        (100.0, 130.0, 120.0, (100.0, 120.0)),  # past the end: cut
        (-5.0, 20.0, 120.0, (0.0, 20.0)),
        (0.0, 5.0, 6.0, None),  # the video itself is shorter than the shortest clip
        (10.004, 40.006, 120.0, (10.01, 40.0)),
    ],
)
def test_fit_clip(start, end, duration, expected):
    assert fit_clip(start, end, duration) == expected
    if expected:
        assert 8 <= round(expected[1] - expected[0], 2) <= 90 and expected[1] <= duration


def test_the_log_keeps_a_copy_in_a_file_and_survives_odd_consoles(tmp_path, capsys, monkeypatch):
    log = web.AppLog(tmp_path / "clip-log.txt")
    log.reset()
    log("first line")
    log("नमस्ते")
    lines = (tmp_path / "clip-log.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 1)[1] for line in lines] == ["first line", "नमस्ते"]
    assert "first line" in capsys.readouterr().out
    log.reset()
    assert (tmp_path / "clip-log.txt").read_text(encoding="utf-8") == ""

    class AsciiConsole:
        encoding = "ascii"

        def __init__(self):
            self.written = []

        def write(self, text):
            text.encode("ascii")
            self.written.append(text)

        def flush(self):
            pass

    console = AsciiConsole()
    monkeypatch.setattr(web.sys, "stdout", console)
    log("नमस्ते again")
    assert "again" in "".join(console.written)
    monkeypatch.setattr(web.sys, "stdout", None)
    log("no console at all")
    unwritable = web.AppLog(tmp_path / "missing-folder" / "clip-log.txt", echo=False)
    unwritable.reset()
    unwritable("still fine")


# ---------------------------------------------------------------- starting


@pytest.fixture
def launch(tmp_path, monkeypatch):
    """Runs the launcher's main() with the engine faked, the log in a temp folder and no real browser."""
    fake = FakeEngine()
    fake.install(monkeypatch)
    monkeypatch.setattr(engine, "LOG", engine.LOG)  # put back afterwards
    monkeypatch.setattr(web, "LOG_FILE", tmp_path / "clip-log.txt")
    monkeypatch.setenv("CLIPSTUDIO_DATA_DIR", str(tmp_path / "data"))
    for name in (
        "CLIPSTUDIO_PORT",
        "CLIPSTUDIO_HOST",
        "CLIPSTUDIO_ALLOWED_HOSTS",
        "CLIPSTUDIO_MAX_UPLOAD_MB",
        "CLIPSTUDIO_KEEP_HOURS",
    ):
        monkeypatch.delenv(name, raising=False)
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    served: list[web.ClipServer] = []
    ports: list[int] = []

    def serve_forever(self, poll_interval=0.5):
        served.append(self)
        ports.append(self.port)
        time.sleep(0.8 if "open" in launch_mode else 0)
        raise KeyboardInterrupt

    launch_mode: set[str] = set()
    monkeypatch.setattr(web.ClipServer, "serve_forever", serve_forever)

    class Launch:
        log_file = tmp_path / "clip-log.txt"

        def __init__(self):
            self.fake, self.opened, self.served, self.mode, self.ports = fake, opened, served, launch_mode, ports

        @property
        def log(self) -> str:
            return self.log_file.read_text(encoding="utf-8")

    return Launch()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_start_logs_the_address_and_tools_and_stops_cleanly_on_ctrl_c(launch, capsys):
    port = free_port()
    assert launcher.main(["--port", str(port), "--no-open"]) == 0
    out = capsys.readouterr().out
    assert f"Dost Clip Studio is running at http://127.0.0.1:{port}" in out
    assert "Video tools found: yt-dlp 2026.10.26, ffmpeg 7.1, Deno 2.3.1" in out
    assert "Warning" not in out and "Traceback" not in out
    assert out.rstrip().endswith("Stopped.")
    assert "is running at" in launch.log and "Stopped." in launch.log
    assert launch.opened == []
    assert launch.served[0].app.settings.host == "127.0.0.1"
    assert engine.LOG.__class__ is web.AppLog  # the engine now logs to the same place


def test_start_opens_the_browser_unless_told_not_to(launch):
    launch.mode.add("open")
    port = free_port()
    assert launcher.main(["--port", str(port)]) == 0
    assert launch.opened == [f"http://127.0.0.1:{port}"]


def test_start_warns_in_plain_words_about_each_missing_tool(launch, capsys):
    launch.fake.tools = {"ytdlp": None, "ffmpeg": None, "deno": None, "captions_filter": False}
    assert launcher.main(["--port", str(free_port()), "--no-open"]) == 0
    out = capsys.readouterr().out
    assert "Video tools found: none" in out
    for tool in ("yt-dlp is missing", "ffmpeg is missing", "Deno is missing"):
        assert f"Warning: {tool}" in out
    assert out.count("start.bat") == 3


def test_start_warns_when_ffmpeg_cannot_draw_captions(launch, capsys):
    launch.fake.tools["captions_filter"] = False
    launcher.main(["--port", str(free_port()), "--no-open"])
    assert "Warning: this ffmpeg cannot draw captions" in capsys.readouterr().out


def test_start_uses_the_environment_and_the_next_port_when_one_is_taken(launch, monkeypatch, capsys):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        monkeypatch.setenv("CLIPSTUDIO_PORT", str(taken))
        assert launcher.main(["--no-open"]) == 0
    out = capsys.readouterr().out
    assert f"Port {taken} is not available" in out
    assert taken < launch.ports[0] < taken + 20


@pytest.mark.parametrize(
    ("arguments", "environment", "expected"),
    [
        (["--port", "70000"], {}, "--port must be between 1 and 65535"),
        (["--port", "abc"], {}, "--port must be a whole number"),
        (["--host", "example.com"], {}, "--host must be localhost or an IP address"),
        ([], {"CLIPSTUDIO_PORT": "http"}, "CLIPSTUDIO_PORT must be a whole number"),
        ([], {"CLIPSTUDIO_KEEP_HOURS": "0"}, "CLIPSTUDIO_KEEP_HOURS must be between"),
        ([], {"CLIPSTUDIO_ALLOWED_HOSTS": "https://youtube.com"}, "CLIPSTUDIO_ALLOWED_HOSTS"),
    ],
)
def test_a_bad_setting_stops_the_start_with_a_plain_message_and_a_non_zero_code(
    launch, monkeypatch, capsys, arguments, environment, expected
):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    code = launcher.main([*arguments, "--no-open"])
    assert code != 0
    out = capsys.readouterr().out
    assert "Dost Clip Studio could not start: " in out and expected in out
    assert "Traceback (most recent call last)" in out
    assert expected in launch.log and "Traceback (most recent call last)" in launch.log
    assert launch.served == []


def test_any_other_start_failure_is_logged_with_its_traceback_and_a_non_zero_code(launch, monkeypatch, capsys):
    def no_ports(app, *args, **kwargs):
        raise RuntimeError("No free port found from 5180 to 5199")

    monkeypatch.setattr(web, "open_server", no_ports)
    assert launcher.main(["--no-open"]) == 1
    out = capsys.readouterr().out
    assert "Dost Clip Studio could not start:" in out and "No free port found" in out
    assert "Traceback (most recent call last)" in out and "Traceback (most recent call last)" in launch.log


def test_listening_beyond_this_computer_gives_a_warning(launch, capsys):
    assert launcher.main(["--host", "0.0.0.0", "--port", str(free_port()), "--no-open"]) == 0
    out = capsys.readouterr().out
    assert "Warning: listening on 0.0.0.0" in out
    assert "is running at http://127.0.0.1:" in out


def test_an_old_python_is_told_to_upgrade(monkeypatch, capsys):
    monkeypatch.setattr(launcher.sys, "version_info", (3, 9, 0))
    assert launcher.main(["--no-open"]) == 1
    assert "needs Python 3.11 or newer" in capsys.readouterr().out

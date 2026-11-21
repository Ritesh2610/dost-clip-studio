"""Shared fixtures for the engine tests.

All media is generated here with the bundled ffmpeg: nothing is downloaded and no
third-party footage is used. The main clip carries a burnt-in, machine-readable
timecode (rows of white boxes that spell the current time in binary), so a test can
read the exact source time of any frame without OCR and without a font.

Layout of the 1280x720 main clip:

* left third tinted red, right third tinted blue (to tell crop positions apart)
* a white 8 px line along the top and the bottom edge (to measure the picture height)
* 8 boxes at y=330..390 for the whole seconds, most significant bit on the left
* 4 boxes at y=420..480 for the tenths of a second
* a 440 Hz tone that is quiet except for two loud bursts (see BURSTS)
"""

from __future__ import annotations

import http.server
import os
import random
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HTTP_PORT = 5414
MAIN_SECONDS = 90
MAIN_FPS = 10
BURSTS = ((40, 46), (70, 74))  # seconds where the tone is loud
BIT_X0, BIT_STEP, BIT_W = 460, 45, 40  # the eight "seconds" boxes
SECONDS_ROW_Y, TENTHS_ROW_Y, ROW_H = 330, 420, 60
HIGHLIGHT_RGB = (0xFF, 0xC4, 0x2E)  # the colour build_ass gives the spoken word

# ffmpeg and urllib both honour proxy variables; the tests only ever talk to this computer
for _name in ("NO_PROXY", "no_proxy"):
    _parts = [p for p in os.environ.get(_name, "").split(",") if p]
    os.environ[_name] = ",".join(dict.fromkeys([*_parts, "127.0.0.1", "localhost"]))


def _timecode_filter() -> str:
    boxes = [
        "drawbox=x=0:y=0:w=426:h=720:color=0xb02020:t=fill",
        "drawbox=x=854:y=0:w=426:h=720:color=0x2030b0:t=fill",
        "drawbox=x=0:y=0:w=1280:h=8:color=white:t=fill",
        "drawbox=x=0:y=712:w=1280:h=8:color=white:t=fill",
    ]
    for bit in range(8):
        x = BIT_X0 + BIT_STEP * (7 - bit)
        boxes.append(
            f"drawbox=x={x}:y={SECONDS_ROW_Y}:w={BIT_W}:h={ROW_H}:color=white:t=fill:"
            f"enable='gte(mod(floor(t+0.001),{2 ** (bit + 1)}),{2**bit})'"
        )
    for bit in range(4):
        x = BIT_X0 + BIT_STEP * (7 - bit)
        boxes.append(
            f"drawbox=x={x}:y={TENTHS_ROW_Y}:w={BIT_W}:h={ROW_H}:color=white:t=fill:"
            f"enable='gte(mod(mod(floor(t*10+0.5),10),{2 ** (bit + 1)}),{2**bit})'"
        )
    return ",".join(boxes)


class MediaKit:
    """Generated test media plus helpers to look inside video files."""

    def __init__(self, ffmpeg: str, root: Path) -> None:
        self.ffmpeg = ffmpeg
        self.root = root
        self.served = root / "served"
        self.served.mkdir()
        self._cache: dict[str, Path] = {}
        self.main = self._make_main()

    # ----------------------------------------------------------- generation

    def run(self, *args: str, binary: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *args]
        process = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL)
        if check and process.returncode != 0:
            raise RuntimeError("test media command failed: " + process.stderr.decode("utf-8", "replace")[-600:])
        return process

    def _make_main(self) -> Path:
        out = self.served / "sample.mp4"
        loud = "+".join(f"between(t,{a},{b})" for a, b in BURSTS)
        graph = f"[0:v]{_timecode_filter()}[v];[1:a]volume='if({loud},1,0.02)':eval=frame[a]"
        self.run(
            *("-f", "lavfi", "-i", f"color=c=0x1c2733:s=1280x720:r={MAIN_FPS}:d={MAIN_SECONDS}"),
            *("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={MAIN_SECONDS}"),
            *("-filter_complex", graph, "-map", "[v]", "-map", "[a]"),
            *("-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-g", str(MAIN_FPS * 2), "-pix_fmt", "yuv420p"),
            *("-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", "-y", str(out)),
        )
        return out

    def source(self, kind: str) -> Path:
        """A small 3 second clip of the given kind, generated once and cached.

        The picture kinds show a white box in the middle that is a quarter of the picture wide
        and a quarter high, on a red, grey-blue and blue background (left, middle and right third).
        """
        if kind in self._cache:
            return self._cache[kind]
        out = self.root / f"src-{kind}.mp4"
        video = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p"]
        audio = ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=44100:duration=3"]
        aac = ["-c:a", "aac", "-b:a", "48k"]
        boxes = (
            "drawbox=x=0:y=0:w=iw/3:h=ih:color=0xb02020:t=fill,drawbox=x=iw*2/3:y=0:w=iw/3:h=ih:color=0x2030b0:t=fill,"
            "drawbox=x=iw*3/8:y=ih*3/8:w=iw/4:h=ih/4:color=white:t=fill"
        )

        def picture(size: str, rate: int = 10, chain: str = boxes) -> list[str]:
            return ["-f", "lavfi", "-i", f"color=c=0x204060:s={size}:r={rate}:d=3,{chain}"]

        sizes = {
            "portrait": "360x640",
            "narrow": "200x640",
            "square": "480x480",
            "four_three": "640x480",
            "tiny": "160x120",
        }
        if kind in sizes:
            self.run(*picture(sizes[kind]), *audio, *video, *aac, "-y", str(out))
        elif kind == "odd":  # odd width and height need a pixel format without chroma subsampling
            self.run(
                *picture("853x481", chain="format=yuv444p," + boxes),
                *audio,
                *video[:-1],
                "yuv444p",
                *aac,
                "-y",
                str(out),
            )
        elif kind == "silent":  # a video stream only
            self.run(*picture("640x360"), *video, "-y", str(out))
        elif kind == "fps120":
            self.run(*picture("640x360", rate=120), *audio, *video, *aac, "-y", str(out))
        elif kind == "two_audio":
            maps = ["-map", "0:v", "-map", "1:a", "-map", "2:a"]
            self.run(*picture("640x360"), *audio, *audio, *maps, *video, *aac, "-y", str(out))
        elif kind == "rotated":  # landscape pixels flagged "turn by 90 degrees", as phones record portrait video
            self.run("-display_rotation", "90", "-i", str(self.source("four_three")), "-c", "copy", "-y", str(out))
        elif kind == "anamorphic":  # 360x360 stored pixels that are shown as 640x360: each pixel is wider than tall
            self.run(*picture("360x360", chain=boxes + ",setsar=16/9"), *video, "-y", str(out))
        elif kind == "audio_only":
            out = out.with_suffix(".m4a")
            self.run("-f", "lavfi", "-i", "sine=frequency=330:sample_rate=44100:duration=12", *aac, "-y", str(out))
        elif kind == "corrupt":
            out.write_bytes(random.Random(7).randbytes(60_000))
        elif kind == "empty":
            out.write_bytes(b"")
        elif kind == "text":
            out.write_text("this is not a video\n" * 50, encoding="utf-8")
        else:
            raise KeyError(kind)
        self._cache[kind] = out
        return out

    # ------------------------------------------------------------ inspection

    def info(self, path: Path) -> str:
        """What `ffmpeg -i` prints about a file."""
        process = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, stdin=subprocess.DEVNULL
        )
        return process.stderr.decode("utf-8", "replace")

    def frame(self, path: Path, at: float = 0.0, size: tuple[int, int] | None = None) -> tuple[int, int, bytes]:
        """One decoded frame as (width, height, rgb24 bytes). `at` counts decoded output time, so it is exact."""
        info = self.info(path)
        if size is None:
            match = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", info)
            assert match, f"no video stream in {path.name}"
            size = (int(match.group(1)), int(match.group(2)))
        scale = f"scale={size[0]}:{size[1]}"
        process = self.run(
            *("-i", str(path), "-ss", f"{at:.3f}", "-frames:v", "1", "-vf", scale),
            *("-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"),
        )
        data = process.stdout
        assert len(data) == size[0] * size[1] * 3, f"no frame at {at}s in {path.name}"
        return size[0], size[1], data

    @staticmethod
    def pixel(frame: tuple[int, int, bytes], x: float, y: float) -> tuple[int, int, int]:
        width, height, data = frame
        xi, yi = min(width - 1, max(0, int(x))), min(height - 1, max(0, int(y)))
        i = (yi * width + xi) * 3
        return data[i], data[i + 1], data[i + 2]

    def timecode(self, frame: tuple[int, int, bytes], box: tuple[float, float, float, float] | None = None) -> float:
        """Reads the burnt-in time. `box` is where the 1280x720 picture sits in the frame: (x, y, width, height)."""
        width, height, _ = frame
        bx, by, bw, bh = box or (0.0, 0.0, float(width), float(height))

        def lit(x: float, y: float) -> bool:
            r, g, b = self.pixel(frame, bx + x * bw / 1280, by + y * bh / 720)
            return min(r, g, b) > 150

        def row(y: int, bits: int) -> int:
            return sum(2**bit for bit in range(bits) if lit(BIT_X0 + BIT_STEP * (7 - bit) + BIT_W / 2, y + ROW_H / 2))

        return row(SECONDS_ROW_Y, 8) + row(TENTHS_ROW_Y, 4) / 10

    @staticmethod
    def count_colour(frame: tuple[int, int, bytes], rgb: tuple[int, int, int], tolerance: int = 40) -> int:
        _, _, data = frame
        r0, g0, b0 = rgb
        return sum(
            1
            for i in range(0, len(data), 3)
            if abs(data[i] - r0) <= tolerance
            and abs(data[i + 1] - g0) <= tolerance
            and abs(data[i + 2] - b0) <= tolerance
        )

    @staticmethod
    def atoms(path: Path) -> list[str]:
        """Top-level MP4 box names in file order ('moov' before 'mdat' means the file is ready for streaming)."""
        names = []
        with open(path, "rb") as handle:
            while True:
                head = handle.read(8)
                if len(head) < 8:
                    break
                size, name = int.from_bytes(head[:4], "big"), head[4:8].decode("latin-1")
                names.append(name)
                if size == 1:
                    size = int.from_bytes(handle.read(8), "big") - 8
                if size < 8:
                    break
                handle.seek(size - 8, os.SEEK_CUR)
        return names


@pytest.fixture(scope="session")
def ffmpeg_path() -> str:
    """The bundled ffmpeg, or a skip with the reason when it is not installed."""
    imageio_ffmpeg = pytest.importorskip(
        "imageio_ffmpeg", reason="imageio-ffmpeg is not installed, so tests that need ffmpeg are skipped"
    )
    try:
        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as error:  # the package is present but has no binary for this platform
        pytest.skip(f"imageio-ffmpeg found no ffmpeg program ({error}), so tests that need ffmpeg are skipped")
        raise


@pytest.fixture(scope="session")
def media(ffmpeg_path: str, tmp_path_factory: pytest.TempPathFactory) -> MediaKit:
    return MediaKit(ffmpeg_path, tmp_path_factory.mktemp("media"))


@pytest.fixture(scope="session")
def ytdlp() -> object:
    """yt-dlp, or a skip with the reason when it is not installed."""
    return pytest.importorskip("yt_dlp", reason="yt-dlp is not installed, so download tests are skipped")


class RangeServer:
    """A file server with byte-range support on 127.0.0.1, standing in for the video site."""

    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.requests: list[str] = []
        kit = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def do_HEAD(self) -> None:
                self.do_GET()

            def do_GET(self) -> None:
                kit.requests.append(self.path)
                name = self.path.split("?")[0].lstrip("/")
                cut_short = name.startswith("broken/")  # promise the whole file, then hang up early
                if cut_short:
                    name = name.split("/", 1)[1]
                target = kit.root / name
                if "/" in name or not target.is_file():
                    self.send_error(404)
                    return
                size = target.stat().st_size
                start, end = 0, size - 1
                header = self.headers.get("Range")
                if header and header.startswith("bytes="):
                    first, _, last = header[6:].partition("-")
                    start = int(first or 0)
                    end = min(size - 1, int(last)) if last else size - 1
                    if start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                kind = "application/json" if name.endswith((".json", ".json3")) else "video/mp4"
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                if self.command == "HEAD":
                    return
                left = end - start + 1
                if cut_short:
                    left = min(left, 20_000)
                    self.close_connection = True
                with open(target, "rb") as handle:
                    handle.seek(start)
                    try:
                        while left > 0:
                            chunk = handle.read(min(65536, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        class QuietServer(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: object, client_address: object) -> None:
                pass  # clients hang up early all the time: ffmpeg seeks by dropping the connection

        self.httpd = QuietServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="session")
def http_server(media: MediaKit) -> Iterator[RangeServer]:
    """Serves the generated media from 127.0.0.1:5414 for the yt-dlp tests."""
    try:
        server = RangeServer(media.served, HTTP_PORT)
    except OSError as error:
        pytest.skip(f"port {HTTP_PORT} on 127.0.0.1 is not free ({error}), so download tests are skipped")
        raise
    yield server
    server.close()


@pytest.fixture
def engine_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collects what the engine logs instead of printing it."""
    import clipstudio.engine as engine

    lines: list[str] = []
    monkeypatch.setattr(engine, "LOG", lines.append)
    return lines


@pytest.fixture
def local_network_only(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Makes any connection from this process to an address other than this computer fail at once.

    Returns the list of refused addresses, which stays empty in a correct test.
    """
    refused: list[object] = []
    real_connect = socket.socket.connect

    def connect(sock: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in ("127.0.0.1", "::1", "localhost"):
            refused.append(address)
            raise OSError(f"test tried to reach {address!r}: only 127.0.0.1 is allowed")
        real_connect(sock, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    return refused

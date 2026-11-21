"""The local web server for Clip Studio. Standard library only.

It serves the app's pages from public/, takes uploads, starts the background
jobs that find moments and render clips, and serves the finished clips with
byte-range support so the browser can seek inside them.

It is written to be safe on a personal computer:
  * it listens on 127.0.0.1 unless told otherwise, and answers only when the
    Host header names this computer and this port (stops DNS rebinding);
  * requests that change something are refused when another website sent them;
  * every value in every request is checked before it is used;
  * file paths are built from generated names and are always resolved inside
    their own folder;
  * errors reach the browser as one plain sentence. Tracebacks go to the log.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from . import __version__, engine
from .config import (
    MAX_CLIP_SECONDS,
    MAX_CLIPS,
    MIN_CLIP_SECONDS,
    PROJECT_ROOT,
    Settings,
    is_loopback_host,
    validate_video_url,
)
from .jobs import LOG_HINT, Job, JobCancelled, JobFailed, JobManager, JobQueueFull

LOG_FILE = PROJECT_ROOT / "clip-log.txt"
PUBLIC_DIR = PROJECT_ROOT / "public"

MAX_JSON_BYTES = 256 * 1024
MAX_TITLE_CHARS = 80
MAX_ANALYSES = 20
MAX_UPLOADS = 20
UPLOAD_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
CHUNK = 256 * 1024
UPLOAD_CHUNK = 1024 * 1024
DRAIN_SECONDS = 30.0
PORT_ATTEMPTS = 20
MARKER = ".clipstudio"  # a folder is cleaned up only when the app itself put this file in it
TIME_SLACK = 0.05  # seconds a clip may run past the measured end of the video (rounding)

DEFAULT_OPTIONS: dict[str, Any] = {
    "layout": "blur",
    "zoom": 1.3,
    "position": "center",
    "captions": True,
    "showTitle": False,
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; media-src 'self' blob:; "
        "img-src 'self' data: https://i.ytimg.com https://*.ggpht.com; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".mp4": "video/mp4",
}

_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_JOB_ROUTE = re.compile(r"/api/jobs/([A-Za-z0-9_-]{8,64})")
_CANCEL_ROUTE = re.compile(r"/api/jobs/([A-Za-z0-9_-]{8,64})/cancel")
_OUTPUT_DIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_OUTPUT_FILE = re.compile(r"clip-[0-9]{2}\.(mp4|txt)")
_RANGE = re.compile(r"\s*bytes\s*=\s*([0-9]{0,19})\s*-\s*([0-9]{0,19})\s*", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")
_STATIC_SEGMENT_BAD = re.compile(r'[\x00-\x1f\x7f\\:*?"<>|]')

UNSATISFIABLE = "unsatisfiable"
TOOL_HINT = "Close the app and run start.bat (start.sh on Mac or Linux) again so it can be installed."


class AppError(Exception):
    """An error that is safe and useful to show in the browser."""

    def __init__(
        self, status: int, code: str, message: str, hint: str | None = None, headers: dict[str, str] | None = None
    ):
        super().__init__(message)
        self.status, self.code, self.message, self.hint = status, code, message, hint
        self.headers = headers or {}


# --------------------------------------------------------------------- log


class AppLog:
    """Prints a line and keeps a copy, with the time, in clip-log.txt."""

    def __init__(self, path: Path | None = LOG_FILE, *, echo: bool = True, verbose: bool = False):
        self.path, self.echo, self.verbose = path, echo, verbose
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Starts a fresh log file: one run per log."""
        if self.path is None:
            return
        try:
            self.path.write_text("", encoding="utf-8")
        except OSError:
            self.path = None  # the folder is read-only: keep printing, stop trying to write

    def __call__(self, message: str) -> None:
        text = str(message)
        with self._lock:
            if self.echo:
                self._print(text)
            if self.path is not None:
                try:
                    with open(self.path, "a", encoding="utf-8") as handle:
                        handle.write(f"{time.strftime('%H:%M:%S')} {text}\n")
                except OSError:
                    pass  # a log that cannot be written must never stop the app

    @staticmethod
    def _print(text: str) -> None:
        stream = sys.stdout
        if stream is None:  # started without a console
            return
        try:
            print(text, file=stream, flush=True)
        except UnicodeEncodeError:  # a console that cannot show, say, Hindi letters
            encoding = getattr(stream, "encoding", None) or "ascii"
            print(text.encode(encoding, "replace").decode(encoding), file=stream, flush=True)
        except (OSError, ValueError):
            pass  # the console went away


# ------------------------------------------------------------- validation


def safe_name(text: object, fallback: str = "video", limit: int = 40) -> str:
    """Reduces any text to letters, digits, hyphens and underscores, for use in a folder name."""
    cleaned = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9_-]+", "-", str(text))).strip("-_")[:limit].strip("-_")
    return cleaned or fallback


def clean_text(value: str) -> str:
    """One line of text: control characters and line breaks become spaces."""
    return " ".join(_CONTROL.sub(" ", value).split())


def _number(value: object) -> float | None:
    """A finite number from JSON, or None. True and False are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _whole_number(value: object) -> int | None:
    number = _number(value)
    if number is None or number != int(number):
        return None
    return int(number)


def fit_clip(start: float, end: float, duration: float) -> tuple[float, float] | None:
    """Brings a suggested moment inside the video and inside the allowed clip length."""
    start = max(0.0, min(float(start), duration))
    end = max(start, min(float(end), duration))
    if end - start > MAX_CLIP_SECONDS:
        end = start + MAX_CLIP_SECONDS
    if end - start < MIN_CLIP_SECONDS:
        end = min(duration, start + MIN_CLIP_SECONDS)
        start = max(0.0, end - MIN_CLIP_SECONDS)
    if end - start < MIN_CLIP_SECONDS - 1e-6:
        return None  # the whole video is shorter than the shortest clip
    start, end = math.ceil(start * 100 - 1e-6) / 100, math.floor(end * 100 + 1e-6) / 100
    if end - start < MIN_CLIP_SECONDS:  # rounding to hundredths took a sliver off
        start = max(0.0, round(end - MIN_CLIP_SECONDS, 2))
    return start, end


def parse_range(header: str, size: int) -> tuple[int, int] | str | None:
    """Reads a single-range `Range` header.

    Returns (first, last) byte positions, UNSATISFIABLE when the range lies outside
    the file, or None when the header should be ignored (several ranges, another
    unit or bad syntax), in which case the whole file is sent.
    """
    match = _RANGE.fullmatch(header)
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:  # "bytes=-500": the last 500 bytes
        suffix = int(last)
        if suffix == 0 or size == 0:
            return UNSATISFIABLE
        return max(0, size - suffix), size - 1
    start = int(first)
    if last and int(last) < start:
        return None
    if start >= size:
        return UNSATISFIABLE
    return start, min(int(last), size - 1) if last else size - 1


def split_host(value: str) -> tuple[str, int | None] | None:
    """Splits a Host header into name and port. Returns None when it is not a plain host[:port]."""
    host = value.strip().lower()
    if host.startswith("["):  # an IPv6 address in brackets, which has colons of its own
        close = host.find("]")
        if close < 0:
            return None
        name, rest = host[: close + 1], host[close + 1 :]
        if rest and not rest.startswith(":"):
            return None
        port_text = rest[1:] if rest else None
    else:
        name, colon, after = host.partition(":")
        port_text = after if colon else None
    if not name or (
        port_text is not None and not (port_text.isascii() and port_text.isdigit() and len(port_text) <= 5)
    ):
        return None
    return name, (int(port_text) if port_text is not None else None)


def format_bytes(count: int) -> str:
    if count >= 1024**3:
        return f"{count / 1024**3:.1f} GB".replace(".0 GB", " GB")
    if count >= 1024**2:
        return f"{count / 1024**2:.0f} MB"
    return f"{max(1, count // 1024)} KB"


# --------------------------------------------------------------------- app


@dataclass
class Upload:
    id: str
    path: Path
    name: str
    bytes: int
    created: float

    @property
    def title(self) -> str:
        return Path(self.name).stem or "Uploaded video"


@dataclass(frozen=True)
class AnalyzeRequest:
    url: str | None
    upload: Upload | None
    clip_length: int
    count: int


@dataclass(frozen=True)
class RenderRequest:
    analysis_id: str
    source: dict[str, Any]
    clips: tuple[dict[str, Any], ...]
    options: dict[str, Any]


class App:
    """Everything the server knows: settings, jobs, uploads and finished analyses."""

    def __init__(
        self,
        settings: Settings,
        *,
        public_dir: Path | None = None,
        log: Callable[[str], None] | None = None,
        verbose: bool = False,
    ):
        self.settings = settings
        self.public_dir = Path(public_dir or PUBLIC_DIR).resolve()
        self.log: Callable[[str], None] = log or print
        self.verbose = verbose
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._analyses: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._uploads: OrderedDict[str, Upload] = OrderedDict()
        self._in_use: dict[Path, int] = {}
        self._tools: dict[str, Any] | None = None
        self._prepare_folders()
        self.jobs = JobManager(log=self.log, on_finish=lambda job: self._cleanup_quietly())
        self._cleanup_quietly()

    def close(self) -> None:
        self.jobs.shutdown()

    # -- folders and cleanup

    def _folders(self) -> tuple[Path, Path, Path]:
        return self.settings.work_dir, self.settings.uploads_dir, self.settings.output_dir

    def _prepare_folders(self) -> None:
        for folder in self._folders():
            existed = folder.is_dir()
            folder.mkdir(parents=True, exist_ok=True)
            marker = folder / MARKER
            if marker.is_file():
                continue
            if existed and any(folder.iterdir()):
                # Not a folder this app made. Use it, but never delete anything in it automatically.
                self.log(f"[cleanup] {folder} already had files in it, so old files there will not be removed for you.")
                continue
            marker.write_text("Clip Studio made this folder and removes old files from it.\n", encoding="utf-8")

    @contextlib.contextmanager
    def in_use(self, *paths: Path | None) -> Iterator[None]:
        """Keeps cleanup away from files and folders a job is working with."""
        held = [Path(p) for p in paths if p is not None]
        with self._lock:
            for path in held:
                self._in_use[path] = self._in_use.get(path, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                for path in held:
                    if self._in_use.get(path, 0) <= 1:
                        self._in_use.pop(path, None)
                    else:
                        self._in_use[path] -= 1

    def cleanup(self, now: float | None = None) -> list[Path]:
        """Removes uploads, work folders and clip folders older than the keep time. Returns what it removed."""
        now = time.time() if now is None else now
        removed: list[Path] = []
        with self._cleanup_lock:
            for folder in self._folders():
                if not (folder / MARKER).is_file():
                    continue
                try:
                    entries = list(os.scandir(folder))
                except OSError:
                    continue
                for entry in entries:
                    path = Path(entry.path)
                    if entry.name == MARKER:
                        continue
                    with self._lock:
                        busy = path in self._in_use
                    if busy:
                        continue
                    try:
                        if now - entry.stat(follow_symlinks=False).st_mtime < self.settings.keep_seconds:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            shutil.rmtree(path)
                        else:
                            path.unlink()  # for a link this removes the link, never what it points at
                    except OSError as error:
                        if self.verbose:
                            self.log(f"[cleanup] could not remove {path}: {error}")
                        continue
                    removed.append(path)
            with self._lock:
                for upload_id in [uid for uid, upload in self._uploads.items() if not upload.path.is_file()]:
                    del self._uploads[upload_id]
        if removed:
            self.log(f"[cleanup] removed {len(removed)} old item(s) from {self.settings.data_dir}")
        return removed

    def _cleanup_quietly(self) -> None:
        try:
            self.cleanup()
        except Exception:
            self.log(f"[cleanup] failed\n{traceback.format_exc().rstrip()}")

    # -- tools and status

    def tools(self, refresh: bool = False) -> dict[str, Any]:
        """What is installed. Checked once, because asking ffmpeg and Deno takes a moment."""
        with self._lock:
            cached = self._tools
        if cached is None or refresh:
            try:
                found = dict(engine.tool_status())
            except Exception:
                self.log(f"[tools] the check failed\n{traceback.format_exc().rstrip()}")
                found = {}
            cached = {
                "ytdlp": found.get("ytdlp") or None,
                "ffmpeg": found.get("ffmpeg") or None,
                "deno": found.get("deno") or None,
                "captions_filter": bool(found.get("captions_filter")),
            }
            with self._lock:
                self._tools = cached
        return dict(cached)

    def status(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "tools": self.tools(),
            "limits": {
                "minClipSeconds": MIN_CLIP_SECONDS,
                "maxClipSeconds": MAX_CLIP_SECONDS,
                "maxClips": MAX_CLIPS,
                "maxUploadBytes": self.settings.max_upload_bytes,
                "allowedHosts": list(self.settings.allowed_hosts),
            },
        }

    def _require_tools(self, *, links: bool) -> None:
        tools = self.tools()
        if not tools["ffmpeg"]:
            raise AppError(503, "tool_missing", "ffmpeg is not installed, so videos cannot be read or cut.", TOOL_HINT)
        if links and not tools["ytdlp"]:
            raise AppError(503, "tool_missing", "yt-dlp is not installed, so video links cannot be opened.", TOOL_HINT)

    # -- uploads

    def upload_name(self, header: str | None) -> tuple[str, str]:
        """Checks the X-Filename header. Returns a tidy display name and the allowed extension."""
        if not header or not header.strip():
            raise AppError(
                400, "missing_filename", "The upload did not include the file's name.", "Choose the file again."
            )
        if len(header) > 2000:
            raise AppError(400, "invalid_filename", "The file's name is too long.", "Rename the file and try again.")
        try:
            decoded = unquote(header.strip(), encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            raise AppError(
                400, "invalid_filename", "The file's name could not be read.", "Rename the file and try again."
            ) from None
        name = clean_text(decoded.replace("\\", "/").rsplit("/", 1)[-1])
        extension = Path(name).suffix.lower()
        if extension not in UPLOAD_EXTENSIONS:
            raise AppError(
                415,
                "unsupported_type",
                "That kind of file cannot be used.",
                "Choose a video file that ends in "
                + ", ".join(UPLOAD_EXTENSIONS[:-1])
                + f" or {UPLOAD_EXTENSIONS[-1]}.",
            )
        stem = Path(name).stem.strip(" .")[:150] or "video"
        return stem + extension, extension

    def add_upload(self, upload: Upload) -> None:
        stale: list[Upload] = []
        with self._lock:
            self._uploads[upload.id] = upload
            for upload_id, old in list(self._uploads.items()):
                if len(self._uploads) <= MAX_UPLOADS:
                    break
                if old.path in self._in_use or upload_id == upload.id:
                    continue
                stale.append(self._uploads.pop(upload_id))
        for old in stale:
            with contextlib.suppress(OSError):
                old.path.unlink()

    def get_upload(self, upload_id: str) -> Upload | None:
        with self._lock:
            return self._uploads.get(upload_id)

    # -- analyses

    def add_analysis(self, source: dict[str, Any]) -> str:
        analysis_id = secrets.token_urlsafe(12)
        with self._lock:
            self._analyses[analysis_id] = {"source": source, "created": time.time()}
            while len(self._analyses) > MAX_ANALYSES:
                self._analyses.popitem(last=False)
        return analysis_id

    def get_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._analyses.get(analysis_id)

    # -- analyze: request

    def parse_analyze(self, body: dict[str, Any]) -> AnalyzeRequest:
        if body.get("permission") is not True:
            raise AppError(
                400,
                "permission_required",
                "Confirm that you made this video or have permission from its owner to clip it.",
                "Tick the permission box, then try again.",
            )
        url, upload_id = body.get("url"), body.get("uploadId")
        if url is None and upload_id is None:
            raise AppError(400, "missing_source", "Paste a video link or choose a video file first.")
        if url is not None and upload_id is not None:
            raise AppError(400, "one_source", "Use either a video link or a video file, not both at once.")

        clip_length = _whole_number(body.get("clipLength"))
        if clip_length is None or not MIN_CLIP_SECONDS <= clip_length <= MAX_CLIP_SECONDS:
            raise AppError(
                400,
                "invalid_clip_length",
                f"The clip length must be a whole number of seconds from {MIN_CLIP_SECONDS} to {MAX_CLIP_SECONDS}.",
            )
        count = _whole_number(body.get("count"))
        if count is None or not 1 <= count <= MAX_CLIPS:
            raise AppError(400, "invalid_count", f"The number of moments must be a whole number from 1 to {MAX_CLIPS}.")

        upload: Upload | None = None
        clean_url: str | None = None
        if url is not None:
            try:
                clean_url = validate_video_url(url, self.settings)
            except ValueError as error:
                raise AppError(400, getattr(error, "code", "invalid_url"), str(error)) from None
        else:
            if not isinstance(upload_id, str) or not _ID.fullmatch(upload_id):
                raise AppError(400, "invalid_upload_id", "That file reference is not valid.", "Choose the file again.")
            upload = self.get_upload(upload_id)
            if upload is None or not upload.path.is_file():
                raise AppError(
                    404, "upload_not_found", "That video file is no longer on hand.", "Choose the file again."
                )
        self._require_tools(links=clean_url is not None)
        return AnalyzeRequest(clean_url, upload, clip_length, count)

    def start_analyze(self, request: AnalyzeRequest) -> Job:
        return self._submit("analyze", lambda job: self._run_analyze(job, request))

    def _submit(self, kind: str, func: Callable[[Job], Any]) -> Job:
        try:
            return self.jobs.submit(kind, func)
        except JobQueueFull:
            raise AppError(
                429, "too_many_jobs", "Too many jobs are already waiting.", "Wait for one to finish, then try again."
            ) from None

    # -- analyze: the job

    def _run_analyze(self, job: Job, request: AnalyzeRequest) -> dict[str, Any]:
        work = self.settings.work_dir / f"job-{job.id}"
        upload = request.upload
        with self.in_use(work, upload.path if upload else None):
            try:
                work.mkdir(parents=True, exist_ok=True)
                job.update("Reading the video details", 0.05)
                if request.url is not None:
                    source = dict(engine.inspect_url(request.url))
                    source["kind"] = "url"
                    source["url"] = self._trusted_url(source.get("url"), request.url)
                else:
                    assert upload is not None  # noqa: S101 - parse_analyze guarantees one of the two
                    if not upload.path.is_file():
                        raise JobFailed("That video file is no longer on hand.", "Choose the file again.")
                    with contextlib.suppress(OSError):
                        os.utime(upload.path)  # recently used: keep it clear of cleanup
                    source = dict(engine.inspect_file(upload.path))
                    source.update(kind="file", path=str(upload.path), title=upload.title)
                    source["id"] = safe_name(upload.title)
                duration = _number(source.get("duration")) or 0.0
                if duration <= 0:
                    raise JobFailed("The length of this video could not be read.", "Try another video.")
                source["duration"] = duration
                words = [w for w in source.get("words") or [] if isinstance(w, dict)]
                source["words"] = words
                heatmap = list(source.get("heatmap") or [])
                job.check()

                levels: list[float] = []
                try:
                    if request.url is not None:
                        job.update("Getting the sound of the video", 0.2)
                        audio: Path | str = engine.download_audio(source["url"], work)
                    else:
                        audio = Path(source["path"])
                    job.update("Listening for loud moments", 0.55)
                    levels = list(engine.audio_levels(audio))
                except JobCancelled:
                    raise
                except Exception as error:  # sound is one signal of three: carry on without it
                    self.log(
                        "[analyze] the sound could not be measured, carrying on without it: "
                        f"{self._friendly(error)}\n{traceback.format_exc().rstrip()}"
                    )
                    levels = []

                job.update("Choosing the strongest moments", 0.85)
                found = engine.find_moments(
                    duration, heatmap, levels, words, length=request.clip_length, count=request.count
                )
                job.check()
                signals = found.get("signals") or {}
                analysis_id = self.add_analysis(source)
                return {
                    "analysisId": analysis_id,
                    "video": {
                        "title": clean_text(str(source.get("title") or "")) or "Untitled video",
                        "channel": clean_text(str(source.get("channel") or "")),
                        "duration": duration,
                        "url": source["url"] if source["kind"] == "url" else None,
                        "thumbnail": self._thumbnail(source.get("thumbnail")),
                        "wasLive": bool(source.get("was_live")),
                    },
                    "signals": {name: bool(signals.get(name)) for name in ("replays", "audio", "speech")},
                    "captionLabel": str(source.get("caption_label") or ""),
                    "note": str(found.get("note") or ""),
                    "moments": self._public_moments(found.get("moments") or [], duration),
                }
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def _trusted_url(self, reported: object, fallback: str) -> str:
        """The address the video site reports for the video, if it also passes the link rules."""
        try:
            return validate_video_url(reported, self.settings)
        except ValueError:
            return fallback

    @staticmethod
    def _thumbnail(value: object) -> str:
        return value if isinstance(value, str) and value.startswith("https://") and len(value) <= 2048 else ""

    @staticmethod
    def _public_moments(moments: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
        public: list[dict[str, Any]] = []
        for moment in moments:
            start, end = _number(moment.get("start")), _number(moment.get("end"))
            if start is None or end is None:
                continue
            fitted = fit_clip(start, end, duration)
            if fitted is None:
                continue
            score = _number(moment.get("score")) or 1
            public.append(
                {
                    "id": f"m{len(public) + 1}",
                    "start": fitted[0],
                    "end": fitted[1],
                    "score": max(1, min(100, int(round(score)))),
                    "reasons": [str(reason) for reason in moment.get("reasons") or []],
                    "text": str(moment.get("text") or ""),
                    "title": clean_text(str(moment.get("title") or ""))[:MAX_TITLE_CHARS],
                }
            )
        return public

    def _friendly(self, error: BaseException) -> str:
        try:
            text = " ".join(str(engine.friendly_error(error) or "").split())[:400]  # type: ignore[arg-type]
        except Exception:
            text = ""
        if not text or "Traceback (most recent call last)" in text:
            return "Something went wrong on this computer while doing that."
        return text

    # -- render: request

    def parse_render(self, body: dict[str, Any]) -> RenderRequest:
        analysis_id = body.get("analysisId")
        if not isinstance(analysis_id, str) or not _ID.fullmatch(analysis_id):
            raise AppError(
                400, "invalid_analysis_id", "The request did not say which video to cut.", "Find moments again."
            )
        analysis = self.get_analysis(analysis_id)
        if analysis is None:
            raise AppError(
                404, "analysis_not_found", "The moments for this video are no longer on hand.", "Find moments again."
            )
        source = analysis["source"]
        duration = float(source["duration"])

        raw_clips = body.get("clips")
        if not isinstance(raw_clips, list):
            raise AppError(400, "invalid_clips", "The request did not include a list of clips to make.")
        if not raw_clips:
            raise AppError(400, "no_clips", "Choose at least one moment to make into a clip.")
        if len(raw_clips) > MAX_CLIPS:
            raise AppError(400, "too_many_clips", f"You can make up to {MAX_CLIPS} clips at a time.")
        clips = tuple(self._parse_clip(number, raw, duration) for number, raw in enumerate(raw_clips, 1))
        options = self._parse_options(body.get("options"))

        if source["kind"] == "file" and not Path(source["path"]).is_file():
            raise AppError(
                404,
                "source_missing",
                "The video file for these moments is no longer on hand.",
                "Choose the file again.",
            )
        self._require_tools(links=source["kind"] == "url")
        return RenderRequest(analysis_id, source, clips, options)

    @staticmethod
    def _parse_clip(number: int, raw: object, duration: float) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AppError(400, "invalid_clip", f"Clip {number} could not be read.")
        start, end = _number(raw.get("start")), _number(raw.get("end"))
        if start is None or end is None:
            raise AppError(400, "invalid_clip_times", f"Clip {number} needs a start time and an end time, in seconds.")
        if start < 0:
            raise AppError(400, "invalid_clip_times", f"Clip {number} starts before the beginning of the video.")
        if start >= end:
            raise AppError(400, "invalid_clip_times", f"Clip {number} must end after it starts.")
        if end > duration + TIME_SLACK:
            raise AppError(
                400,
                "clip_outside_video",
                f"Clip {number} ends after the end of the video.",
                "Move its end time earlier.",
            )
        end = min(end, duration)
        length = end - start
        if length < MIN_CLIP_SECONDS - 0.01:
            raise AppError(
                400,
                "clip_too_short",
                f"Clip {number} is shorter than {MIN_CLIP_SECONDS} seconds.",
                "Move its start or end time to make it longer.",
            )
        if length > MAX_CLIP_SECONDS + 0.01:
            raise AppError(
                400,
                "clip_too_long",
                f"Clip {number} is longer than {MAX_CLIP_SECONDS} seconds.",
                "Move its start or end time to make it shorter.",
            )
        title = raw.get("title", "")
        if title is None:
            title = ""
        if not isinstance(title, str):
            raise AppError(400, "invalid_title", f"The title of clip {number} must be text.")
        title = clean_text(title)
        if len(title) > MAX_TITLE_CHARS:
            raise AppError(
                400,
                "title_too_long",
                f"The title of clip {number} is longer than {MAX_TITLE_CHARS} characters.",
                "Shorten the title.",
            )
        return {"start": round(start, 3), "end": round(end, 3), "title": title}

    @staticmethod
    def _parse_options(raw: object) -> dict[str, Any]:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise AppError(400, "invalid_options", "The clip settings could not be read.")
        options = {**DEFAULT_OPTIONS, **{key: raw[key] for key in DEFAULT_OPTIONS if key in raw}}
        if options["layout"] not in ("blur", "fill"):
            raise AppError(400, "invalid_layout", "The layout must be blur or fill.")
        if options["position"] not in ("left", "center", "right"):
            raise AppError(400, "invalid_position", "The position must be left, center or right.")
        zoom = _number(options["zoom"])
        if zoom is None:
            raise AppError(400, "invalid_zoom", "The zoom must be a number from 1.0 to 2.0.")
        options["zoom"] = max(1.0, min(2.0, zoom))
        if not isinstance(options["captions"], bool):
            raise AppError(400, "invalid_captions", "The captions setting must be on or off (true or false).")
        if not isinstance(options["showTitle"], bool):
            raise AppError(400, "invalid_show_title", "The title setting must be on or off (true or false).")
        return options

    def start_render(self, request: RenderRequest) -> Job:
        return self._submit("render", lambda job: self._run_render(job, request))

    # -- render: the job

    def _run_render(self, job: Job, request: RenderRequest) -> dict[str, Any]:
        source, options = request.source, request.options
        from_link = source["kind"] == "url"
        work = self.settings.work_dir / f"job-{job.id}"
        folder_name = f"{safe_name(source.get('id'))}-{job.id[:8]}"
        out_dir = self.settings.output_dir / folder_name
        source_file = None if from_link else Path(source["path"])
        tools = self.tools()
        words = (source.get("words") or []) if options["captions"] else []
        if options["captions"] and source.get("words") and not tools["captions_filter"]:
            self.log("[render] this ffmpeg has no captions filter, so the clips are made without captions")
        total = len(request.clips)
        clips: list[dict[str, Any]] = []
        with self.in_use(work, out_dir, source_file):
            try:
                work.mkdir(parents=True, exist_ok=True)
                out_dir.mkdir(parents=True, exist_ok=False)
                if source_file is not None:
                    with contextlib.suppress(OSError):
                        os.utime(source_file)
                for index, clip in enumerate(request.clips, 1):
                    job.update(f"Making clip {index} of {total}", (index - 1) / total)
                    clips.append(
                        self._render_clip(job, index, total, clip, source, options, words, tools, work, out_dir)
                    )
                job.check()
            except JobCancelled:
                shutil.rmtree(out_dir, ignore_errors=True)  # nobody is waiting for a cancelled job's clips
                raise
            finally:
                shutil.rmtree(work, ignore_errors=True)
        made = sum(1 for clip in clips if clip["ok"])
        self.log(f"[render] {made} of {total} clip(s) saved in {out_dir}")
        return {"folder": str(out_dir), "clips": clips}

    def _render_clip(
        self,
        job: Job,
        index: int,
        total: int,
        clip: dict[str, Any],
        source: dict[str, Any],
        options: dict[str, Any],
        words: list[dict[str, Any]],
        tools: dict[str, Any],
        work: Path,
        out_dir: Path,
    ) -> dict[str, Any]:
        start, end, title = float(clip["start"]), float(clip["end"]), str(clip["title"])
        stem = f"clip-{index:02d}"
        out = out_dir / f"{stem}.mp4"
        ass_path = out_dir / f"{stem}.ass"
        caption = self._post_caption(title, source)
        result: dict[str, Any] = {
            "index": index,
            "ok": False,
            "name": out.name,
            "url": "",
            "seconds": 0.0,
            "bytes": 0,
            "start": start,
            "end": end,
            "caption": caption,
        }
        section: Path | None = None
        try:
            if source["kind"] == "url":
                job.update(f"Clip {index} of {total}: getting that part of the video")
                section = Path(engine.fetch_section(source["url"], start, end, work / f"section-{index:02d}.mp4"))
                segment, seek = section, 0.0
                job.update(f"Clip {index} of {total}: making the vertical video", (index - 0.5) / total)
            else:
                segment, seek = Path(source["path"]), start
                job.update(f"Clip {index} of {total}: making the vertical video")

            ass_name: str | None = None
            overlay_title = title if options["showTitle"] else ""
            spoken = [w for w in words if start <= _word_time(w) < end]
            if tools["captions_filter"] and (spoken or overlay_title):
                ass_path.write_text(engine.build_ass(spoken, start, end, overlay_title), encoding="utf-8")
                ass_name = ass_path.name

            engine.render_short(
                segment,
                seek,
                end - start,
                out,
                layout=options["layout"],
                zoom=options["zoom"],
                position=options["position"],
                ass_file=ass_name,
            )
            size = out.stat().st_size
            if size <= 0:
                raise RuntimeError("ffmpeg could not make the clip: the file it wrote is empty")
            try:
                seconds = float(engine.probe_duration(out))
            except Exception:
                seconds = 0.0
            if not math.isfinite(seconds) or seconds <= 0:
                seconds = end - start
            (out_dir / f"{stem}.txt").write_text(caption + "\n", encoding="utf-8")
            result.update(ok=True, url=f"/output/{out_dir.name}/{out.name}", seconds=round(seconds, 2), bytes=size)
            self.log(f"[render] clip {index} of {total}: done, {format_bytes(size)}")
        except JobCancelled:
            with contextlib.suppress(OSError):
                out.unlink()
            raise
        except Exception as error:  # one clip that fails must not stop the others
            self.log(f"[render] clip {index} of {total} failed\n{traceback.format_exc().rstrip()}")
            result["error"] = self._friendly(error)
            with contextlib.suppress(OSError):
                out.unlink()
        finally:
            with contextlib.suppress(OSError):
                ass_path.unlink()
            if section is not None:
                with contextlib.suppress(OSError):
                    section.unlink()
        return result

    @staticmethod
    def _post_caption(title: str, source: dict[str, Any]) -> str:
        """Suggested text to post with the clip: the title, then a credit to the channel."""
        first = title or clean_text(str(source.get("title") or "")) or "Untitled clip"
        channel = clean_text(str(source.get("channel") or ""))
        return f"{first}\nClip from {channel} on YouTube" if channel else first


def _word_time(word: dict[str, Any]) -> float:
    return _number(word.get("t")) or 0.0


# ------------------------------------------------------------------ server


class Handler(BaseHTTPRequestHandler):
    server_version = f"ClipStudio/{__version__}"
    sys_version = ""  # no need to tell the world which Python this is
    protocol_version = "HTTP/1.1"
    timeout = 120  # seconds a silent connection is kept

    _responded = False
    _unread = 0

    @property
    def app(self) -> App:
        return cast("ClipServer", self.server).app

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class names it
        if self.app.verbose:
            self.app.log(f"[request] {self.address_string()} {format % args}")

    # -- responses

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self._unread > 0 or self.close_connection:
            self.send_header("Connection", "close")  # part of the request was not read: do not reuse the line
        self.end_headers()
        self._responded = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store", **(extra or {})})

    def _error(self, error: AppError) -> None:
        body: dict[str, Any] = {"status": error.status, "code": error.code, "message": error.message}
        if error.hint:
            body["hint"] = error.hint
        self._json(error.status, {"error": body}, error.headers)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Used by the base class for requests it cannot read. Same JSON shape and headers as every other error."""
        texts = {
            400: ("bad_request", "The request could not be read."),
            408: ("timeout", "The request took too long to arrive."),
            414: ("address_too_long", "The address of the request is too long."),
            431: ("headers_too_large", "The request headers are too large."),
            501: ("method_not_allowed", "This kind of request is not supported."),
            505: ("bad_request", "This version of HTTP is not supported."),
        }
        label, text = texts.get(code, ("bad_request", "The request could not be handled."))
        self.close_connection = True
        try:
            self._error(AppError(code, label, text))
        except OSError:
            pass  # the other side has gone

    # -- request checks

    def _check_host(self) -> None:
        """Answers only when the browser asked for this computer by a local name (stops DNS rebinding)."""
        port = cast("ClipServer", self.server).port
        allowed = {"localhost", "127.0.0.1", "[::1]"}
        bound = self.app.settings.host
        if bound not in ("0.0.0.0", "::", "localhost"):  # noqa: S104 - compared, not bound
            allowed.add(f"[{bound}]" if ":" in bound else bound)
        values = self.headers.get_all("Host") or []
        parsed = split_host(values[0]) if len(values) == 1 else None
        if parsed is None or parsed[0] not in allowed or (80 if parsed[1] is None else parsed[1]) != port:
            raise AppError(
                403,
                "forbidden_host",
                "This address is not allowed to use the app.",
                f"Open the app at http://127.0.0.1:{port} on the computer it runs on.",
            )

    def _check_origin(self) -> None:
        """Stops other websites from driving this local server."""
        origin = self.headers.get("Origin")
        host = (self.headers.get("Host") or "").strip().lower()
        same = origin is None or origin.strip().lower().rstrip("/") == f"http://{host}"
        if same and (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            same = False
        if not same:
            raise AppError(403, "forbidden_origin", "This request came from another site and was blocked.")

    def _content_length(self) -> int | None:
        values = self.headers.get_all("Content-Length") or []
        if not values:
            return None
        texts = {value.strip() for value in values}
        text = texts.pop()
        if texts or not (text.isascii() and text.isdigit()) or len(text) > 18:
            self.close_connection = True  # where this request ends is unknown, so nothing more can be read
            raise AppError(400, "bad_content_length", "The request did not say clearly how large it is.")
        return int(text)

    def _read_json(self, required: bool = True) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise AppError(411, "length_required", "The request did not say how large it is.")
        length = self._content_length() or 0
        if length > MAX_JSON_BYTES:
            raise AppError(413, "body_too_large", "The request is too large.")
        raw = self.rfile.read(length) if length else b""
        self._unread = 0
        if len(raw) < length:
            self.close_connection = True
            raise AppError(400, "incomplete_request", "The request stopped before all of it arrived.")
        if not required:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            raise AppError(
                400, "bad_json", "The request could not be read.", "Reload the page and try again."
            ) from None
        if not isinstance(data, dict):
            raise AppError(400, "bad_json", "The request could not be read.", "Reload the page and try again.")
        return data

    def _drain(self) -> None:
        """Reads and drops the rest of a refused request, so the browser gets the answer instead of a broken line."""
        deadline = time.monotonic() + DRAIN_SECONDS
        try:
            self.connection.settimeout(2.0)
            while self._unread > 0 and time.monotonic() < deadline:
                chunk = self.rfile.read(min(self._unread, CHUNK))
                if not chunk:
                    break
                self._unread -= len(chunk)
        except OSError:
            pass  # the other side stopped sending
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(self.timeout)

    # -- routing

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")  # same answers; _send leaves the body out

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _other_method(self) -> None:
        self._dispatch("OTHER")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _other_method

    def _dispatch(self, method: str) -> None:
        self._responded = False
        self._unread = 0
        path = "?"
        try:
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True  # a body with no stated length cannot be skipped safely
            self._unread = self._content_length() or 0
            self._check_host()
            try:
                path = urlsplit(self.path).path or "/"
            except ValueError:
                raise AppError(400, "bad_request", "The request could not be read.") from None
            if method == "POST":
                self._check_origin()
                self._post(path)
            elif method == "GET":
                self._get(path)
            else:
                raise AppError(
                    405,
                    "method_not_allowed",
                    "This kind of request is not supported.",
                    headers={"Allow": "GET, HEAD, POST"},
                )
        except AppError as error:
            if error.status >= 500 or self.app.verbose:
                self.app.log(f"[error] {self.command} {path}: {error.code}: {error.message}")
            if 0 < self._unread <= MAX_JSON_BYTES and not self.close_connection:
                self._drain()  # a small unread body: skip it so the connection can be used again
            self._answer_error(error)
        except (ConnectionError, TimeoutError):
            self.close_connection = True  # the browser closed the connection: nothing to answer
            self._unread = 0
        except Exception:
            self.app.log(f"[error] {self.command} {path}: unexpected failure\n{traceback.format_exc().rstrip()}")
            self._answer_error(AppError(500, "server_error", "Something went wrong on this computer.", LOG_HINT))
        finally:
            if self._unread > 0:
                self.close_connection = True
                self._drain()

    def _answer_error(self, error: AppError) -> None:
        if self._responded:  # too late to change the answer: end the connection instead
            self.close_connection = True
            return
        try:
            self._error(error)
        except OSError:
            self.close_connection = True

    def _get(self, path: str) -> None:
        if path == "/api/status":
            return self._json(200, self.app.status())
        match = _JOB_ROUTE.fullmatch(path)
        if match:
            snapshot = self.app.jobs.snapshot(match.group(1))
            if snapshot is None:
                raise AppError(404, "job_not_found", "That job is no longer on hand.", "Start it again.")
            return self._json(200, snapshot)
        if path in ("/api/uploads", "/api/analyze", "/api/render") or _CANCEL_ROUTE.fullmatch(path):
            raise AppError(
                405, "method_not_allowed", "This address only takes POST requests.", headers={"Allow": "POST"}
            )
        if path == "/api" or path.startswith("/api/"):
            raise AppError(404, "not_found", "There is nothing at this address.")
        if path == "/output" or path.startswith("/output/"):
            return self._serve_output(path)
        return self._serve_static(path)

    def _post(self, path: str) -> None:
        if path == "/api/uploads":
            return self._upload()
        if path == "/api/analyze":
            request = self.app.parse_analyze(self._read_json())
            job = self.app.start_analyze(request)
            return self._json(202, {"jobId": job.id})
        if path == "/api/render":
            render = self.app.parse_render(self._read_json())
            job = self.app.start_render(render)
            return self._json(202, {"jobId": job.id})
        match = _CANCEL_ROUTE.fullmatch(path)
        if match:
            self._read_json(required=False)
            state = self.app.jobs.cancel(match.group(1))
            if state is None:
                raise AppError(404, "job_not_found", "That job is no longer on hand.")
            return self._json(200, state)
        if path == "/api/status" or _JOB_ROUTE.fullmatch(path):
            raise AppError(
                405, "method_not_allowed", "This address only takes GET requests.", headers={"Allow": "GET, HEAD"}
            )
        if path == "/api" or path.startswith("/api/"):
            raise AppError(404, "not_found", "There is nothing at this address.")
        raise AppError(
            405, "method_not_allowed", "This address only takes GET requests.", headers={"Allow": "GET, HEAD"}
        )

    # -- uploads

    def _upload(self) -> None:
        app, settings = self.app, self.app.settings
        if self.headers.get("Transfer-Encoding"):
            raise AppError(
                411, "length_required", "The upload did not say how large the file is.", "Choose the file again."
            )
        length = self._content_length()
        if length is None:
            raise AppError(
                411, "length_required", "The upload did not say how large the file is.", "Choose the file again."
            )
        if length > settings.max_upload_bytes:
            raise AppError(
                413,
                "file_too_large",
                f"That file is larger than the limit of {format_bytes(settings.max_upload_bytes)}.",
                "Choose a smaller file.",
            )
        name, extension = app.upload_name(self.headers.get("X-Filename"))
        if length == 0:
            raise AppError(400, "empty_file", "That file is empty.", "Choose another file.")
        try:
            free = shutil.disk_usage(settings.uploads_dir).free
        except OSError:
            free = None
        if free is not None and free < length:
            raise AppError(
                507,
                "not_enough_space",
                "There is not enough free disk space for that file.",
                "Free some space and try again.",
            )

        upload_id = secrets.token_hex(16)
        final = settings.uploads_dir / f"{upload_id}{extension}"
        part = final.with_name(final.name + ".part")
        with app.in_use(part, final):
            try:
                remaining = length
                try:
                    with open(part, "xb") as target:
                        while remaining > 0:
                            chunk = self.rfile.read(min(remaining, UPLOAD_CHUNK))
                            if not chunk:
                                break
                            target.write(chunk)
                            remaining -= len(chunk)
                            self._unread = remaining
                    if remaining == 0:
                        os.replace(part, final)
                except (ConnectionError, TimeoutError):
                    raise
                except OSError as error:
                    app.log(f"[upload] could not save the file: {error!r}")
                    raise AppError(
                        507,
                        "save_failed",
                        "The file could not be saved on this computer.",
                        "Check the free disk space and try again.",
                    ) from None
                if remaining > 0:
                    self._unread = 0
                    self.close_connection = True
                    raise AppError(
                        400, "incomplete_upload", "The upload stopped before the whole file arrived.", "Try again."
                    )
                try:
                    duration = float(engine.probe_duration(final))
                except Exception:
                    app.log(f"[upload] the file could not be checked\n{traceback.format_exc().rstrip()}")
                    raise AppError(
                        503, "tool_missing", "The file could not be checked because ffmpeg did not run.", TOOL_HINT
                    ) from None
                if not math.isfinite(duration) or duration <= 0:
                    raise AppError(
                        422,
                        "not_a_video",
                        "That file could not be read as a video.",
                        "Choose a video file, or convert it to .mp4 and try again.",
                    )
            except BaseException:
                for leftover in (part, final):
                    with contextlib.suppress(OSError):
                        leftover.unlink()
                raise
            app.add_upload(Upload(upload_id, final, name, length, time.time()))
        app.log(f"[upload] {format_bytes(length)} saved as {final.name}")
        self._json(201, {"uploadId": upload_id, "name": name, "bytes": length})

    # -- files

    def _serve_static(self, path: str) -> None:
        """Files from public/. The path can only ever name a file inside that folder."""
        not_found = AppError(404, "not_found", "There is nothing at this address.")
        try:
            relative = unquote(path, errors="strict")
        except UnicodeDecodeError:
            raise not_found from None
        if not relative.startswith("/"):
            raise not_found
        segments = relative.split("/")[1:]
        if segments[-1] == "":  # "/" and "/folder/" mean the index page of that folder
            segments[-1] = "index.html"
        for segment in segments:
            if (
                not segment
                or segment.startswith(".")
                or segment.endswith((".", " "))
                or _STATIC_SEGMENT_BAD.search(segment)
            ):
                raise not_found
        root = self.app.public_dir
        try:
            target = root.joinpath(*segments).resolve()
        except (OSError, ValueError):
            raise not_found from None
        if root not in target.parents or not target.is_file():
            raise not_found
        content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send_file(target, content_type, cache="no-cache", ranges=False)

    def _serve_output(self, path: str) -> None:
        """Finished clips: /output/<folder>/clip-NN.mp4 (or .txt), and nothing else."""
        not_found = AppError(404, "not_found", "That clip is no longer on hand.", "Make the clips again.")
        try:
            parts = unquote(path, errors="strict").split("/")
        except UnicodeDecodeError:
            raise not_found from None
        if len(parts) != 4 or parts[0] != "" or parts[1] != "output":
            raise not_found
        folder, name = parts[2], parts[3]
        if not _OUTPUT_DIR.fullmatch(folder) or not _OUTPUT_FILE.fullmatch(name):
            raise not_found
        root = self.app.settings.output_dir.resolve()
        try:
            target = (root / folder / name).resolve()
        except (OSError, ValueError):
            raise not_found from None
        if root not in target.parents or not target.is_file():
            raise not_found
        self._send_file(target, CONTENT_TYPES[target.suffix.lower()], cache="no-cache", ranges=True)

    def _send_file(self, target: Path, content_type: str, *, cache: str, ranges: bool) -> None:
        try:
            handle = open(target, "rb")  # noqa: SIM115 - closed by the with block below
        except OSError:
            raise AppError(404, "not_found", "There is nothing at this address.") from None
        with handle:
            size = os.fstat(handle.fileno()).st_size
            start, end, status = 0, size - 1, 200
            headers = {**SECURITY_HEADERS, "Cache-Control": cache}
            if ranges:
                headers["Accept-Ranges"] = "bytes"
                wanted = self.headers.get("Range")
                # No validators are sent, so an If-Range can never match: the whole file goes out.
                if wanted and not self.headers.get("If-Range"):
                    parsed = parse_range(wanted, size)
                    if parsed == UNSATISFIABLE:
                        raise AppError(
                            416,
                            "range_not_satisfiable",
                            "That part of the file does not exist.",
                            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
                        )
                    if isinstance(parsed, tuple):
                        start, end = parsed
                        status = 206
                        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            remaining = max(0, end - start + 1)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(remaining))
            self.end_headers()
            self._responded = True
            if self.command == "HEAD":
                return
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(remaining, CHUNK))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
            if remaining > 0:  # the file shrank while it was being sent
                self.close_connection = True


class ClipServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, address reuse would let two programs share one port silently.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], app: App):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.app = app
        super().__init__(address, Handler)

    @property
    def port(self) -> int:
        """The port that was really bound (differs from the one asked for when that was 0)."""
        return int(self.socket.getsockname()[1])

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A request thread failed outside the handler's own error handling. Keep the traceback out of the console."""
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return  # the browser closed the connection, which is normal while seeking in a video
        self.app.log(f"[error] request from {client_address} failed\n{traceback.format_exc().rstrip()}")


def open_server(
    app: App, host: str | None = None, port: int | None = None, attempts: int = PORT_ATTEMPTS
) -> ClipServer:
    """Binds the first free port, starting from the requested one. Port 0 means any free port."""
    host = app.settings.host if host is None else host
    port = app.settings.port if port is None else port
    candidates = [0] if port == 0 else [p for p in range(port, port + max(1, attempts)) if p <= 65535]
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            return ClipServer((host, candidate), app)
        except OSError as error:
            last_error = error
            app.log(f"Port {candidate} is not available ({error}). Trying the next one.")
    raise RuntimeError(f"No free port found from {candidates[0]} to {candidates[-1]}: {last_error}")


def server_url(server: ClipServer) -> str:
    """The address to open in the browser."""
    host, port = str(server.socket.getsockname()[0]), server.port
    if host in ("0.0.0.0", "::", "localhost") or is_loopback_host(host):  # noqa: S104 - compared, not bound
        host = "127.0.0.1" if ":" not in host else "[::1]"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"

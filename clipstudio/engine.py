"""Clip Studio engine: find strong moments in a long video and cut vertical shorts.

Everything runs on this computer. Three free tools do the heavy work:
yt-dlp (reads YouTube), Deno (runs YouTube's player script for yt-dlp) and
ffmpeg (decodes, crops, burns captions, encodes).
"""

from __future__ import annotations

import bisect
import contextlib
import glob
import itertools
import json
import math
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

LOG: Callable[[str], None] = print  # the server replaces this with its logger

Word = dict[str, Any]  # one caption word: {"t": start in seconds, "d": seconds on screen, "w": text}

MIN_CLIP = 8  # seconds: the shortest clip find_moments returns
MAX_CLIP = 180  # seconds: the longest clip length find_moments accepts
_GAP = 5  # seconds kept free between two picked stretches of the video
_MAX_SECONDS = 2 * 86400  # a duration beyond two days is a bad number, not a video: work stops there


# ------------------------------------------------------------------- tools


def ffmpeg_exe() -> str:
    """Path of the ffmpeg program that ships with imageio-ffmpeg."""
    import imageio_ffmpeg

    return str(imageio_ffmpeg.get_ffmpeg_exe())


def deno_exe() -> str | None:
    """Path of the Deno program (from the PyPI package, else from PATH), or None when it is not installed."""
    try:
        import deno

        return str(deno.find_deno_bin())
    except Exception:
        return shutil.which("deno")


def _run(command: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Runs a tool and returns its output as text. Never raises for a non-zero exit code."""
    # stdin is closed so that ffmpeg never waits for, or swallows, keys typed in the terminal that started the app.
    # utf-8 is spelled out because ffmpeg prints file names in utf-8 whatever the Windows code page is.
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, cwd=cwd
    )


def _input_args(source: str | Path) -> list[str]:
    """The ffmpeg arguments that open one input."""
    text = str(source)
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]+://", text):
        return ["-i", text]
    # "file:" stops ffmpeg from reading a colon in a file name as the name of a protocol. For a local file ffmpeg
    # allows only local protocols by itself, so a playlist disguised as a video cannot make it open a web address.
    return ["-i", "file:" + text]


def _number(value: Any, default: float = 0.0) -> float:
    """A finite float made from anything; the default when that is not possible."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def tool_status() -> dict[str, Any]:
    """What is installed, for the status line in the app."""
    status: dict[str, Any] = {"ytdlp": None, "ffmpeg": None, "deno": None, "captions_filter": False}
    # each tool is looked up on its own: one that is missing or broken must not hide the others
    with contextlib.suppress(Exception):
        import yt_dlp

        status["ytdlp"] = str(yt_dlp.version.__version__)
    with contextlib.suppress(Exception):
        exe = ffmpeg_exe()
        first = _run([exe, "-hide_banner", "-version"]).stdout.split("\n")[0]
        status["ffmpeg"] = first.replace("ffmpeg version ", "").split(" ")[0] or None
        status["captions_filter"] = " ass " in _run([exe, "-hide_banner", "-filters"]).stdout
    with contextlib.suppress(Exception):
        deno = deno_exe()
        if deno:
            first = _run([deno, "--version"]).stdout.split("\n")[0]
            status["deno"] = first.replace("deno ", "").split(" ")[0] or None
    return status


class _YdlLogger:
    """Sends yt-dlp's messages to LOG."""

    def debug(self, message: str) -> None:
        if not message.startswith("[debug]"):
            LOG(f"[yt-dlp] {message}")

    def info(self, message: str) -> None:
        LOG(f"[yt-dlp] {message}")

    def warning(self, message: str) -> None:
        LOG(f"[yt-dlp] warning: {message}")

    def error(self, message: str) -> None:
        LOG(f"[yt-dlp] error: {message}")


def ydl_options(**extra: Any) -> dict[str, Any]:
    """The yt-dlp settings shared by every call, plus `extra`."""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "noprogress": True,
        "logger": _YdlLogger(),
        "ffmpeg_location": ffmpeg_exe(),
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
    }
    deno = deno_exe()
    if deno:
        options["js_runtimes"] = {"deno": {"path": deno}}
    options.update(extra)
    return options


@contextlib.contextmanager
def open_ydl(options: dict[str, Any]) -> Iterator[Any]:
    """A yt-dlp session that finds the bundled ffmpeg everywhere, also when no ffmpeg is on PATH.

    The `ffmpeg_location` setting reaches yt-dlp's post-processors, but its downloaders decide whether
    ffmpeg exists through a context variable that only the yt-dlp command line sets. Without it, cutting
    one part out of a video fails with "ffmpeg is not installed" on a computer that has no ffmpeg of its own.
    """
    import yt_dlp
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    with contextlib.ExitStack() as stack:
        variable = getattr(FFmpegPostProcessor, "_ffmpeg_location", None)
        if variable is not None:
            stack.callback(variable.reset, variable.set(options.get("ffmpeg_location")))
        yield stack.enter_context(yt_dlp.YoutubeDL(options))


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# a run of two or more path parts, optionally after a Windows drive letter: /home/me/x.mp4, C:\Users\me\x.mp4
_LOCAL_PATH = re.compile(r"(?:\bfile:|(?<![\w/\\~.:-]))(?:[A-Za-z]:)?(?:[\\/]+[^\\/\s:*?\"'<>|]+){2,}")


def _hide_paths(text: str) -> str:
    """Replaces folder paths with the bare file name, so a message never shows where things live on this computer."""
    home = str(Path.home())
    for prefix in {home, home.replace("\\", "/")}:
        if len(prefix) > 1:
            text = text.replace(prefix, "~")
    return _LOCAL_PATH.sub(lambda match: re.split(r"[\\/]", match.group(0))[-1], text)


def friendly_error(error: Exception) -> str:
    """Turns a yt-dlp or ffmpeg failure into one plain sentence with a next step."""
    text = _ANSI.sub("", str(error))
    text = re.sub(r"^ERROR:\s*", "", text.strip()).strip()
    low = text.lower()
    # the age check comes first: YouTube words it "Sign in to confirm your age", which also looks like the bot check
    if (
        "confirm your age" in low
        or "age-restricted" in low
        or "age restricted" in low
        or "inappropriate for some" in low
    ):
        return "This video is age-restricted and needs a signed-in account, which this app does not use."
    if "sign in to confirm" in low or "not a bot" in low:
        return (
            "YouTube asked for a sign-in check and refused the request. "
            "Wait a while and try again, or try another video."
        )
    if "private video" in low:
        return "This video is private, so it cannot be opened."
    if "members-only" in low or "join this channel" in low:
        return "This video is for channel members only, so it cannot be opened."
    if "video unavailable" in low or "has been removed" in low:
        return "YouTube says this video is unavailable."
    if "javascript runtime" in low or "js runtime" in low:
        return "The Deno helper that yt-dlp needs for YouTube is missing. Run start.bat (Windows) or start.sh (macOS, Linux) again so it can be installed."
    if "unsupported url" in low or "is not a valid url" in low:
        return "That does not look like a video link. Paste the full address of a YouTube video or finished stream."
    if "http error 429" in low or "too many requests" in low:
        return "YouTube is limiting requests from this connection right now. Wait a few minutes and try again."
    if "getaddrinfo" in low or "timed out" in low or "connection" in low or "network" in low:
        return "The video site could not be reached. Check your internet connection and try again."
    if "no space left" in low or "not enough space" in low or "disk full" in low:
        return "The disk is full. Free some space and try again."
    if "giving up after" in low or "more expected" in low or "ffmpeg exited with code" in low:
        return "The download stopped before it finished. Check your internet connection and try again."
    # anything else: one line, no folder names, and short. A traceback is cut down to its last line.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.startswith("Traceback (most recent call last)") for line in lines):
        lines = lines[-1:]
    text = _hide_paths(" ".join(" ".join(lines).split()))
    return text[:300] or "Something went wrong while reading the video."


# ------------------------------------------------------------ video source


def _clean_heatmap(heatmap: Any) -> list[dict[str, float]]:
    """The usable "most replayed" markers: each needs a start, a later end and a value, all as numbers."""
    markers: list[dict[str, float]] = []
    if not isinstance(heatmap, (list, tuple)):
        return markers
    for marker in heatmap:
        if not isinstance(marker, dict):
            continue
        a, b = _number(marker.get("start_time"), math.nan), _number(marker.get("end_time"), math.nan)
        value = _number(marker.get("value"), math.nan)
        if math.isnan(a) or math.isnan(b) or math.isnan(value) or b <= a or b <= 0:
            continue
        markers.append({"start_time": max(0.0, a), "end_time": b, "value": max(0.0, value)})
    return markers


def inspect_url(url: str) -> dict[str, Any]:
    """Reads the video's details without downloading it."""
    with open_ydl(ydl_options(skip_download=True)) as ydl:
        info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise ValueError("That link did not lead to a video.")
        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries") or [] if isinstance(e, dict)]
            if not entries:
                raise ValueError("That link is a playlist with no videos. Paste the link of one video.")
            info = entries[0]
        if info.get("is_live") or info.get("live_status") == "is_live":
            raise ValueError(
                "This stream is still live. Wait until it has ended and YouTube has finished processing it, "
                "then paste the link again."
            )
        if info.get("live_status") == "is_upcoming":
            raise ValueError("This stream has not started yet.")
        captions, caption_label = fetch_captions(ydl, info)
    return {
        "kind": "url",
        "url": info.get("webpage_url") or url,
        "id": str(info.get("id") or "video"),
        "title": info.get("title") or "Untitled video",
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration": max(0.0, _number(info.get("duration"))),
        "was_live": bool(info.get("was_live")),
        "thumbnail": info.get("thumbnail") or "",
        "view_count": info.get("view_count"),
        "heatmap": _clean_heatmap(info.get("heatmap")),
        "words": captions,
        "caption_label": caption_label,
    }


def inspect_file(path: Path) -> dict[str, Any]:
    """Describes a video file on this computer in the same shape as inspect_url."""
    path = Path(path)
    duration = probe_duration(path)
    if duration <= 0:
        raise ValueError("That file could not be read as a video.")
    return {
        "kind": "file",
        "path": str(path),
        "id": re.sub(r"[^A-Za-z0-9_-]+", "-", path.stem)[:40] or "video",
        "title": path.stem,
        "channel": "",
        "duration": duration,
        "was_live": False,
        "thumbnail": "",
        "view_count": None,
        "heatmap": [],
        "words": [],
        "caption_label": "",
    }


def probe_duration(path: Path | str) -> float:
    """Length of a media file in seconds, read from ffmpeg's own report (no ffprobe needed). 0.0 if unreadable."""
    out = _run([ffmpeg_exe(), "-hide_banner", *_input_args(path)]).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


# ---------------------------------------------------------------- captions

PREFERRED_LANGS = ("hi", "en", "en-US", "en-GB", "en-IN", "hi-Latn")

# characters that would break a line of a text file or flip the reading direction of what follows
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")


def _plain(value: Any) -> str:
    """Caption text as one clean line: control characters become spaces, unpaired surrogates are replaced."""
    if not isinstance(value, str):
        return ""
    # an unpaired surrogate (from a broken \\u escape in JSON) cannot be written to a utf-8 file
    return _CONTROL.sub(" ", value.encode("utf-8", "replace").decode("utf-8")).strip()


def fetch_captions(ydl: Any, info: dict[str, Any]) -> tuple[list[Word], str]:
    """Returns timed words from the video's captions (uploaded ones first, then automatic)."""
    original = str(info.get("language") or "").split("-")[0]
    order = [original] if original else []
    order += [lang for lang in PREFERRED_LANGS if lang not in order]
    for source, label in (("subtitles", "uploaded"), ("automatic_captions", "automatic")):
        tracks = info.get(source)
        if not isinstance(tracks, dict):
            continue
        # automatic captions list every translation; "<lang>-orig" is the spoken language
        if source == "automatic_captions":
            candidates = [f"{lang}-orig" for lang in order] + order
        else:
            candidates = order + sorted(str(key) for key in tracks)
        for lang in dict.fromkeys(candidates):
            formats = tracks.get(lang)
            if not isinstance(formats, list):
                continue
            usable = (f for f in formats if isinstance(f, dict) and f.get("ext") == "json3" and f.get("url"))
            entry = next(usable, None)
            if not entry:
                continue
            try:
                raw = ydl.urlopen(entry["url"]).read().decode("utf-8", "replace")
                words = parse_json3(json.loads(raw))
            except Exception as error:  # captions are optional: carry on without them
                LOG(f"[captions] {lang} ({label}) could not be fetched: {friendly_error(error)}")
                continue
            if words:
                return words, f"{lang.replace('-orig', '')} ({label})"
    return [], ""


def parse_json3(data: Any) -> list[Word]:
    """YouTube's json3 caption format -> [{'t': seconds, 'd': seconds, 'w': word}]. Skips what it cannot use."""
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    lines: list[tuple[float, float, list[dict[str, Any]]]] = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("segs"), list):
            continue
        segs = [seg for seg in event["segs"] if isinstance(seg, dict)]
        if segs:
            start = max(0.0, _number(event.get("tStartMs")) / 1000)
            lines.append((start, max(0.0, _number(event.get("dDurationMs")) / 1000), segs))

    words: list[Word] = []
    for n, (start, duration, segs) in enumerate(lines):
        if any("tOffsetMs" in seg for seg in segs):  # automatic captions: each segment is one word with its own offset
            for seg in segs:
                text = _plain(seg.get("utf8"))
                if not text:
                    continue
                offset = max(0.0, _number(seg.get("tOffsetMs")) / 1000)
                if duration > 0:  # an offset past the end of its own line is a broken stamp
                    offset = min(offset, duration)
                words.append({"t": start + offset, "d": 0.0, "w": text})
        else:  # uploaded captions: spread the words evenly over the line
            parts = " ".join(_plain(seg.get("utf8")) for seg in segs).split()
            if not parts:
                continue
            if duration <= 0:  # no length given: assume a normal speaking pace, up to where the next line starts
                gap = next((later - start for later, _, _ in lines[n + 1 : n + 4] if later > start), 0.0)
                duration = min(0.4 * len(parts), gap) if gap > 0 else 0.4 * len(parts)
            step = duration / len(parts)
            for i, part in enumerate(parts):
                words.append({"t": start + i * step, "d": step, "w": part})
    words.sort(key=lambda w: w["t"])
    for i, word in enumerate(words):  # a word lasts until the next one, within reason
        gap = (words[i + 1]["t"] - word["t"]) if i + 1 < len(words) else 0.6
        word["d"] = max(0.12, min(gap, 1.2)) if word["d"] <= 0 else max(0.05, min(word["d"], 1.5))
    return words


def _clean_words(words: Any) -> list[Word]:
    """The usable caption words in time order. Entries without a sensible time or text are dropped."""
    clean: list[Word] = []
    if not isinstance(words, (list, tuple)):
        return clean
    for word in words:
        if not isinstance(word, dict):
            continue
        text = word.get("w")
        t = _number(word.get("t"), -1.0)
        if not isinstance(text, str) or not text.strip() or not 0 <= t <= _MAX_SECONDS:
            continue
        clean.append({"t": t, "d": min(max(_number(word.get("d")), 0.0), 10.0) or 0.4, "w": text})
    clean.sort(key=lambda w: w["t"])
    return clean


# ------------------------------------------------------------ audio energy


def audio_levels(source: str | Path) -> list[float]:
    """Loudness (RMS, in dB) of each second of the audio, measured by ffmpeg.

    Returns an empty list when the file has no audio track. Raises RuntimeError when the file cannot be read.
    """
    command = [
        *(ffmpeg_exe(), "-hide_banner", "-nostats", "-loglevel", "error", *_input_args(source), "-vn"),
        # resample inside the filter chain, so that 8000 samples really is one second
        "-af",
        "aresample=8000,aformat=channel_layouts=mono,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
        "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-",
        *("-f", "null", "-"),
    ]
    process = _run(command)
    levels = []
    for line in process.stdout.splitlines():
        if line.startswith("lavfi.astats.Overall.RMS_level="):
            # "-inf" is what silence measures as; anything unreadable is treated the same way
            levels.append(max(-70.0, _number(line.split("=", 1)[1].strip(), -70.0)))
    if not levels and process.returncode != 0:
        low = process.stderr.lower()
        if "does not contain any stream" in low or "matches no streams" in low:
            return []  # a video without sound: the caller carries on without the audio signal
        reason = (process.stderr.strip().splitlines() or ["unknown error"])[-1][:300]
        raise RuntimeError("ffmpeg could not read the audio: " + reason)
    return levels


_KEY_FRAME_GAP = 6.0  # seconds: with key frames further apart than this, one frame a second is compared instead


def _picture_changes(source: str | Path, key_frames_only: bool) -> tuple[list[tuple[float, float]], str]:
    """(time, change) for each compared frame, where change is the mean difference from the frame before (0..255)."""
    chain = "scale=96:54,format=gray,tblend=all_mode=difference,signalstats,"
    chain += "metadata=mode=print:key=lavfi.signalstats.YAVG:file=-"
    command = [
        *(ffmpeg_exe(), "-hide_banner", "-nostats", "-loglevel", "error"),
        *(("-skip_frame", "nokey") if key_frames_only else ()),
        *(*_input_args(source), "-an", "-vf", chain if key_frames_only else "fps=1," + chain),
        *("-fps_mode", "passthrough", "-f", "null", "-"),
    ]
    process = _run(command)
    samples: list[tuple[float, float]] = []
    at: float | None = None
    for line in process.stdout.splitlines():
        if line.startswith("frame:"):
            found = re.search(r"pts_time:(-?[0-9.]+)", line)
            at = _number(found.group(1), -1.0) if found else None
        elif line.startswith("lavfi.signalstats.YAVG=") and at is not None and at >= 0:
            samples.append((at, max(0.0, min(255.0, _number(line.split("=", 1)[1].strip())))))
            at = None
    return samples, "" if samples or process.returncode == 0 else process.stderr


def motion_levels(source: str | Path) -> list[float]:
    """How much the picture changes during each second of the video (0 is a still picture, 255 the most possible).

    Only key frames are compared, which takes seconds even for a stream of many hours. When the key frames lie
    too far apart for that, one frame a second is compared instead, which is slower. One changed frame on its
    own, such as a cut to another scene, is not counted as movement.

    Returns an empty list when the file has no picture. Raises RuntimeError when the file cannot be read.
    """
    samples, problem = _picture_changes(source, key_frames_only=True)
    sparse = not problem and (len(samples) < 3 or samples[-1][0] / len(samples) > _KEY_FRAME_GAP)
    if sparse or "skip_frame" in problem:  # ffmpeg refuses the key-frame setting for a file with sound only
        samples, problem = _picture_changes(source, key_frames_only=False)
    if problem:
        low = problem.lower()
        if "does not contain any stream" in low or "matches no streams" in low:
            return []  # sound only: the caller carries on without the movement signal
        reason = (problem.strip().splitlines() or ["unknown error"])[-1][:300]
        raise RuntimeError("ffmpeg could not read the picture: " + reason)
    samples.sort()
    values = [value for _, value in samples]
    levels: list[float] = []
    for i, (at, _) in enumerate(samples):
        # the middle one of three neighbours: a single jump, which is what a cut looks like, disappears
        near = sorted(values[max(0, i - 1) : i + 2])
        value = near[len(near) // 2]
        last = min(int(_MAX_SECONDS), math.ceil(at))
        levels.extend([value] * (last - len(levels)))  # this frame speaks for the seconds since the one before
    return levels


def _remove_leftovers(folder: Path, stem: str, before: set[str]) -> None:
    """After a failed download, deletes the files it left behind (`stem`.* that were not there before)."""
    try:
        names = [p for p in folder.iterdir() if p.name.startswith(stem + ".")]
    except OSError:
        return
    for path in names:
        if path.name not in before or path.suffix.lower() in (".part", ".ytdl"):
            with contextlib.suppress(OSError):
                path.unlink()


def _listing(folder: Path) -> set[str]:
    """Names of the files in a folder (empty when the folder cannot be read)."""
    try:
        return {p.name for p in folder.iterdir()}
    except OSError:
        return set()


def _downloaded_file(ydl: Any, info: Any, folder: Path, stem: str) -> Path:
    """The file yt-dlp wrote for `stem`, whatever extension it ended up with."""
    path = Path(ydl.prepare_filename(info))
    if not path.exists():  # the merged file may have another extension
        partial = (".part", ".ytdl", ".ass", ".json", ".temp")
        matches = [p for p in sorted(folder.glob(glob.escape(stem) + ".*")) if p.suffix.lower() not in partial]
        if not matches:
            raise RuntimeError("The clip could not be downloaded.")
        path = matches[0]
    return path


# A long stream arrives as thousands of small pieces. Fetched one at a time they take an hour or more.
AUDIO_PIECES_AT_ONCE = 8


def _download_share(status: Any) -> float | None:
    """How much of a download has arrived, from 0 to 1, read from a yt-dlp progress report."""
    if not isinstance(status, dict):
        return None
    if status.get("status") == "finished":
        return 1.0
    pieces, piece = _number(status.get("fragment_count")), _number(status.get("fragment_index"))
    if pieces > 0:
        return max(0.0, min(1.0, piece / pieces))
    total = _number(status.get("total_bytes")) or _number(status.get("total_bytes_estimate"))
    if total > 0:
        return max(0.0, min(1.0, _number(status.get("downloaded_bytes")) / total))
    return None


def _download_whole(url: str, work: Path, stem: str, wanted: str, progress: Callable[[float], None] | None) -> Path:
    """Downloads the whole video in the format `wanted` (a yt-dlp format choice) as the file `stem` in `work`."""
    work = Path(work)
    before = _listing(work)
    # "%" starts a field in yt-dlp's name template, so one that belongs to the folder name is doubled
    template = str(work / stem).replace("%", "%%") + ".%(ext)s"

    def report(status: Any) -> None:
        share = _download_share(status)
        if progress is not None and share is not None:
            progress(share)

    options = ydl_options(
        format=wanted,
        outtmpl=template,
        overwrites=True,
        concurrent_fragment_downloads=AUDIO_PIECES_AT_ONCE,
        progress_hooks=[report],
    )
    try:
        with open_ydl(options) as ydl:
            info = ydl.extract_info(url, download=True)
            return _downloaded_file(ydl, info, work, stem)
    except BaseException:
        _remove_leftovers(work, stem, before)
        raise


def download_audio(url: str, work: Path, progress: Callable[[float], None] | None = None) -> Path:
    """Fetches the smallest audio track, which is enough to measure loudness.

    `progress` is called with the share that has arrived, from 0 to 1, many times a second and possibly from
    several threads. An exception it raises stops the download, which is how a job is cancelled.
    """
    return _download_whole(url, work, "audio", "worstaudio/bestaudio/worst", progress)


def download_preview(url: str, work: Path, progress: Callable[[float], None] | None = None) -> Path:
    """Fetches the smallest copy of the video that has both picture and sound, to measure movement and loudness.

    For a stream of many hours this is some hundreds of megabytes. `progress` works as in download_audio.
    """
    both = "[vcodec!=none][acodec!=none]"
    wanted = f"worst{both}[height<=240]/worstvideo[height<=240]+worstaudio/worst{both}/worstvideo+worstaudio/worst"
    return _download_whole(url, work, "preview", wanted, progress)


# ----------------------------------------------------------------- scoring

EXCITED = {
    *("laughter", "laughs", "laughing", "applause", "cheering", "screams", "screaming", "gasps"),
    *("wow", "omg", "insane", "crazy", "unbelievable", "no way", "what", "wait", "bro", "dude", "oh", "damn", "holy"),
    *("lets go", "let's go", "clutch", "gg", "yes", "noo", "nooo", "run", "dead", "killed", "won", "win"),
    *("arre", "arey", "bhai", "kya", "pagal", "gazab", "kamaal", "mast"),
    *("अरे", "भाई", "क्या", "पागल", "गज़ब", "कमाल", "वाह", "हँसी", "तालियाँ"),
}
QUIET_MARKERS = {"music", "संगीत"}
_PAIR_STARTS = {phrase.split()[0] for phrase in EXCITED if " " in phrase}
# \w alone would drop Devanagari vowel signs (they are marks, not letters), so the whole block is listed
_NOT_WORD = re.compile(r"[^\wऀ-ॿ']+")
_NOT_LATIN = re.compile(r"[^a-z]+")
_SENTENCE_END = (".", "?", "!", "।")


def _smooth(values: list[float], radius: int) -> list[float]:
    """Moving average over `radius` neighbours on each side."""
    if radius <= 0 or not values:
        return list(values)
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    n = len(values)
    out = []
    for i in range(n):
        a, b = max(0, i - radius), min(n, i + radius + 1)
        out.append((prefix[b] - prefix[a]) / (b - a))
    return out


def heat_signal(heatmap: list[dict[str, Any]], seconds: int) -> list[float] | None:
    """YouTube's "most replayed" graph as one value (0..1) per second, or None when there is no graph."""
    markers = _clean_heatmap(heatmap)
    if not markers or seconds <= 0:
        return None
    signal = [0.0] * seconds
    covered = False
    for marker in markers:
        a, b = int(min(marker["start_time"], seconds)), math.ceil(min(marker["end_time"], seconds))
        covered = covered or a < b
        value = marker["value"]
        for t in range(a, b):
            if value > signal[t]:
                signal[t] = value
    if not covered:  # every marker lies outside the video
        return None
    top = max(signal)
    if top > 1.0:  # the graph is meant to be 0..1; bring any other scale back to that
        signal = [v / top for v in signal]
    # Every video's graph starts high because everyone watches the opening. Turn that down.
    intro = max(20, int(seconds * 0.03))
    for t in range(min(intro, seconds)):
        signal[t] *= 0.4
    return signal


def energy_signal(levels: list[float], seconds: int) -> list[float] | None:
    """How much each second stands out in loudness (0..1), or None when there is too little audio to judge."""
    if seconds <= 0 or not isinstance(levels, (list, tuple)) or len(levels) < 10:
        return None
    measured = [max(-70.0, min(20.0, _number(x, -70.0))) for x in levels[:seconds]]
    median = statistics.median(measured)
    # seconds the measurement does not cover count as ordinary, not as loud or quiet
    measured += [median] * (seconds - len(measured))
    spread = statistics.median(abs(x - median) for x in measured) * 1.4826 or 3.0
    prefix = [0.0]
    for level in measured:
        prefix.append(prefix[-1] + level)
    signal = []
    for t, level in enumerate(measured):
        loud = max(0.0, min(1.0, (level - median) / (3 * spread)))
        a = max(0, t - 20)
        before = (prefix[t] - prefix[a]) / (t - a) if t > a else level
        burst = max(0.0, min(1.0, (level - before) / 12))  # a sudden jump of 12 dB counts fully
        signal.append(0.6 * loud + 0.4 * burst)
    # rounded so that rounding noise in the sums above cannot pass for a (tiny) peak in an even recording
    return [round(v, 9) for v in _smooth(signal, 1)]


def motion_signal(levels: list[float], seconds: int) -> list[float] | None:
    """How much each second stands out in movement on screen (0..1), or None with too little picture to judge."""
    if seconds <= 0 or not isinstance(levels, (list, tuple)) or len(levels) < 10:
        return None
    measured = [max(0.0, min(255.0, _number(x))) for x in levels[:seconds]]
    median = statistics.median(measured)
    measured += [median] * (seconds - len(measured))  # seconds without a measurement count as ordinary
    spread = statistics.median(abs(x - median) for x in measured) * 1.4826 or 1.0
    signal = [max(0.0, min(1.0, (level - median) / (3 * spread))) for level in measured]
    return [round(v, 9) for v in _smooth(signal, 2)]


FOCUS_WEIGHTS = {
    # what people say and how the audience reacted
    "talk": {"replays": 0.5, "audio": 0.3, "speech": 0.2},
    # what happens on screen: for game streams, where the talking is not the point
    "action": {"motion": 0.45, "audio": 0.3, "replays": 0.25},
}


def speech_signal(words: list[Word], seconds: int) -> list[float] | None:
    """Excited words and fast talking per second (0..1), or None when there are too few captions to judge."""
    return _speech(_clean_words(words), seconds)


def _speech(words: list[Word], seconds: int) -> list[float] | None:
    """speech_signal for words that are already cleaned and in time order."""
    if len(words) < 20 or seconds <= 0:
        return None
    hits, rate = [0.0] * seconds, [0.0] * seconds
    lowered = [word["w"].lower() for word in words]
    for i, word in enumerate(words):
        t = int(word["t"])
        if not 0 <= t < seconds:
            continue
        raw = lowered[i]
        token = _NOT_WORD.sub("", raw)
        if token in QUIET_MARKERS:
            hits[t] -= 0.5
        elif "[" in raw or "(" in raw:
            hits[t] += 1.0 if token in EXCITED else 0.0
        elif token in EXCITED:
            hits[t] += 0.5
        elif (
            token in _PAIR_STARTS and i + 1 < len(words) and f"{token} {_NOT_LATIN.sub('', lowered[i + 1])}" in EXCITED
        ):
            hits[t] += 0.5
        if "!" in raw:
            hits[t] += 0.4
        rate[t] += 1
    rate = _smooth(rate, 2)
    top = sorted(rate)[int(len(rate) * 0.95)] or 1.0
    hits = _smooth([max(0.0, min(1.0, h)) for h in hits], 2)
    return [min(1.0, 0.7 * min(1.0, h * 2.5) + 0.3 * min(1.0, r / top)) for h, r in zip(hits, rate, strict=True)]


def _window_scores(combined: list[float], length: int) -> list[float]:
    """Score of every `length`-second window: mostly its average, partly its best second."""
    prefix = [0.0]
    for v in combined:
        prefix.append(prefix[-1] + v)
    scores: list[float] = []
    best: deque[int] = deque()  # positions of candidates for "highest value in the window", best first
    for t, value in enumerate(combined):
        while best and combined[best[-1]] <= value:
            best.pop()
        best.append(t)
        s = t - length + 1
        if s < 0:
            continue
        if best[0] < s:
            best.popleft()
        # rounded so that windows which are equal on paper are equal here too, and the earlier one wins the tie
        scores.append(round(0.6 * (prefix[t + 1] - prefix[s]) / length + 0.4 * combined[best[0]], 9))
    return scores


def find_moments(
    duration: float,
    heatmap: list[dict[str, Any]],
    levels: list[float],
    words: list[Word],
    length: int = 30,
    count: int = 5,
    *,
    motion: list[float] | None = None,
    focus: str = "talk",
) -> dict[str, Any]:
    """Ranks the strongest stretches of the video.

    `focus` chooses what counts: "talk" looks at replays, loudness and what is said; "action" looks at
    movement on screen (`motion`, from motion_levels), loudness and replays, and ignores what is said.

    Returns {'moments': [...], 'signals': {...}, 'note': str}. Every moment lies inside the video, is between
    MIN_CLIP seconds and `length` seconds long, and no two moments overlap. Scores are 1..100, the best is 100.
    """
    duration = min(max(0.0, _number(duration)), float(_MAX_SECONDS))
    seconds = int(duration)
    length = max(MIN_CLIP, min(int(_number(length, 30.0)), MAX_CLIP))
    count = max(0, int(_number(count, 5.0)))
    words = _clean_words(words)
    signals = {
        "replays": heat_signal(heatmap, seconds),
        "audio": energy_signal(levels, seconds),
        "speech": _speech(words, seconds),
        "motion": motion_signal(motion or [], seconds),
    }
    action = focus == "action"
    weights = FOCUS_WEIGHTS["action" if action else "talk"]
    used = {name: s for name, s in signals.items() if s and name in weights}
    summary = {name: bool(s) for name, s in signals.items()}
    if seconds < length:
        return {"moments": [], "signals": summary, "note": "The video is shorter than the clip length you chose."}
    if not used:
        note = "There was nothing to measure: no replay data, no readable audio and no captions."
        if action:
            note = "There was nothing to measure: no picture that could be read, no readable audio and no replay data."
        return {"moments": [], "signals": summary, "note": note}

    total = sum(weights[name] for name in used)
    combined = [sum(weights[name] * used[name][t] for name in used) / total for t in range(seconds)]
    combined = _smooth(combined, 1)
    window = _window_scores(combined, length)
    order = sorted(range(len(window)), key=window.__getitem__, reverse=True)
    best = window[order[0]]
    flat = best <= 1e-9  # nothing rises above the rest anywhere: no ranking is possible
    note = ""
    if flat:
        fits = min(count, (seconds + _GAP) // (length + _GAP))
        spare = seconds - length
        order = [spare // 2] if fits <= 1 else [i * spare // (fits - 1) for i in range(fits)]
        note = "No part of this video stood out from the rest, so the clips are spread evenly across it."

    pauses = _pauses(words)
    word_times = [w["t"] for w in words]
    averages = {name: sum(sig) / len(sig) for name, sig in used.items()}
    typical = statistics.median(window)
    # Clear action: at least half of the clip moves far more than is usual for this video. A lower bar would
    # let in a clip that only touches the edge of an action scene.
    clear_motion = max(0.5, 1.6 * averages["motion"]) if "motion" in used else 0.0
    motion_sums = [0.0, *itertools.accumulate(used.get("motion") or [])]

    def busy(a: int, b: int) -> bool:
        return (motion_sums[b] - motion_sums[a]) / (b - a) >= clear_motion

    def pick(clear_only: bool) -> list[dict[str, Any]]:
        """The best windows that do not overlap. With `clear_only`, windows without clear action are passed over."""
        taken: list[tuple[float, float]] = []  # accepted clips, in time order
        blocked = bytearray(len(window))  # window starts that would come too close to an accepted clip
        moments: list[dict[str, Any]] = []
        for s in order:
            if len(moments) >= count:
                break
            if blocked[s] and not flat:
                continue
            if clear_only and not busy(s, s + length):
                continue
            # the free room around this window: it ends where the neighbouring accepted clips begin
            at = bisect.bisect_left(taken, (float(s), 0.0))
            low = taken[at - 1][1] + 1.0 if at else 0.0
            high = taken[at][0] - 1.0 if at < len(taken) else duration
            peak = s if flat else max(range(s, s + length), key=combined.__getitem__)
            first = s
            if peak - s > 0.4 * length:  # pull the strongest second towards the front of the clip
                first = max(s, min(int(peak - 0.4 * length), seconds - length, int(high - length)))
            # Cut at pauses in speech. This and the shift above move the clip after it was chosen, so the result is
            # held inside the free room: without that, two clips picked 5 seconds apart could end up overlapping.
            start, end = _snap(float(first), float(first + length), pauses, duration, float(length), (peak, peak + 1.0))
            start = round(max(start, low), 2)
            end = _floor2(min(end, high, duration, start + length))
            if end - start < MIN_CLIP:
                end = _floor2(min(high, duration, start + MIN_CLIP))
                start = round(max(low, min(start, end - MIN_CLIP)), 2)
            if not (0 <= start < end <= duration and end - start >= MIN_CLIP - 0.011):
                continue  # cannot happen for a window that fits; kept so a wrong clip can never be returned
            bisect.insort(taken, (start, end))
            a, b = max(0, math.floor(start - _GAP - length) + 1), min(len(window), math.ceil(end + _GAP))
            blocked[a:b] = b"\x01" * max(0, b - a)

            inside = [
                w["w"] for w in words[bisect.bisect_left(word_times, start) : bisect.bisect_left(word_times, end)]
            ]
            text = _plain(clean_caption_text(" ".join(inside[:400])))
            reasons = []
            a, b = int(start), max(int(start) + 1, int(end))

            def mean(name: str, a: int = a, b: int = b) -> float:
                return sum(used[name][a:b]) / (b - a)

            if "motion" in used and (clear_only or mean("motion") >= max(0.2, 1.6 * averages["motion"])):
                reasons.append("Fast action on screen")
            if "replays" in used and mean("replays") >= max(0.35, 1.5 * averages["replays"]):
                reasons.append("Viewers replayed this part")
            if "audio" in used and mean("audio") >= max(0.2, 1.6 * averages["audio"]):
                reasons.append("Loud sound" if action else "Loud reaction")
            if "speech" in used and mean("speech") >= max(0.2, 1.6 * averages["speech"]):
                reasons.append("Excited or fast speech")
            if not reasons and flat:
                reasons.append("Picked to cover the video evenly")
            elif not reasons and window[s] > typical:
                reasons.append("Stronger than the rest of the video")
            elif not reasons:
                reasons.append("Added to reach the number of clips you asked for")
            moments.append(
                {
                    "start": start,
                    "end": end,
                    "peak": peak,
                    "score": 100 if flat else max(1, min(100, round(100 * window[s] / best))),
                    "reasons": reasons,
                    "text": text[:400],
                    # what is said during game action is rarely about it, so no title is made up from it
                    "title": "" if action else suggest_title(text),
                }
            )
        return moments

    moments = pick(clear_only=action and "motion" in used and not flat)
    if action and "motion" in used and not flat:
        if not moments:
            moments = pick(clear_only=False)
            note = "No part of this video had clearly more action on screen than the rest, so these moments go by sound and replays."
        elif len(moments) < count:
            found = "1 moment" if len(moments) == 1 else f"{len(moments)} moments"
            note = f"Only {found} with clear action on screen could be found in this video."
    moments.sort(key=lambda m: -m["score"])
    return {"moments": moments, "signals": summary, "note": note}


def _floor2(value: float) -> float:
    """Rounds down to hundredths, so an end time can never be pushed past a limit by rounding."""
    return math.floor(value * 100 + 1e-6) / 100


def _pauses(words: list[Word]) -> list[float]:
    """Times where a new sentence or phrase starts: after a gap in speech or after closing punctuation."""
    if not words:
        return []
    pauses = [words[0]["t"]]
    for prev, word in itertools.pairwise(words):
        if word["t"] - (prev["t"] + prev["d"]) >= 0.45 or prev["w"].rstrip().endswith(_SENTENCE_END):
            pauses.append(word["t"])
    return pauses


def _nearest(pauses: list[float], target: float, low: float, high: float) -> float | None:
    """The pause closest to `target` among those between `low` and `high` (the earlier one on a tie)."""
    if low > high or not pauses:
        return None
    i = bisect.bisect_right(pauses, min(target, high)) - 1  # last pause at or before the target
    j = bisect.bisect_left(pauses, max(target, low))  # first pause at or after the target
    before = pauses[i] if i >= 0 and pauses[i] >= low else None
    after = pauses[j] if j < len(pauses) and pauses[j] <= high else None
    if before is None or after is None:
        return before if after is None else after
    return before if target - before <= after - target else after


def _snap(
    start: float,
    end: float,
    pauses: list[float],
    duration: float,
    max_length: float | None = None,
    keep: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """snap_to_speech on prepared pauses. `max_length` caps the clip; `keep` is a stretch that must stay inside."""
    low, high = start - 3.0, start + 1.5
    if keep:
        high = min(high, keep[0] + 0.15)
        if max_length is not None:
            low = max(low, keep[1] - max_length + 0.15)
    pause = _nearest(pauses, start, low, high)
    if pause is not None:
        start = max(0.0, pause - 0.15)
    limit = end + 3.0
    if max_length is not None:
        limit = min(limit, start + max_length + 0.1)
    target = min(end, limit)
    pause = _nearest(pauses, target, max(target - 1.5, keep[1] + 0.1) if keep else target - 1.5, limit)
    if pause is not None:
        end = pause - 0.1
    if max_length is not None:
        end = min(end, start + max_length)
    start = max(0.0, min(start, duration))
    end = min(duration, max(start + 5.0, end))
    if end - start < 5.0:  # too close to the end of the video: take the last five seconds instead
        start = max(0.0, end - 5.0)
    return start, end


def snap_to_speech(start: float, end: float, words: list[Word], duration: float) -> tuple[float, float]:
    """Moves the cut points to pauses in speech so a clip does not start or end mid-sentence.

    The start moves by at most about 3 seconds and the end by at most about 3 seconds. The result is never
    shorter than 5 seconds (unless the whole video is) and always lies inside the video.
    """
    duration = max(0.0, _number(duration))
    start = min(max(0.0, _number(start)), duration)
    end = max(start, min(_number(end, start), duration))
    return _snap(start, end, _pauses(_clean_words(words)), duration)


def trim_lead_in(levels: list[float], start: float, end: float, max_trim: float = 3.0) -> float:
    """Returns a start time moved forward past near-silence, so that a clip opens on sound instead of dead air.

    `levels` is what audio_levels() returns (dB per second of the source). The start moves by at most `max_trim`
    seconds, the clip always keeps at least 5 seconds, and nothing moves when the whole clip is near-silent.
    """
    start = max(0.0, _number(start))
    room = min(_number(max_trim), _number(end, start) - start - 5.0)
    if room <= 0 or not isinstance(levels, (list, tuple)):
        return start
    first = int(start)
    inside = [max(-70.0, _number(x, -70.0)) for x in levels[first : math.ceil(_number(end, start))]]
    if not inside:
        return start
    # "near-silence" is 30 dB under the loudest second of this clip, but never above -45 dB and never below -60 dB
    threshold = max(-60.0, min(-45.0, max(inside) - 30.0))
    for offset, level in enumerate(inside):
        if level > threshold:  # sound begins somewhere inside this second
            return min(max(start, float(first + offset)), start + room)
    return start  # near-silent throughout: there is no sound to open on


def clean_caption_text(text: str) -> str:
    """Caption text without sound descriptions such as [Music] or (laughs), speaker arrows and extra spaces."""
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|>>+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def suggest_title(text: str) -> str:
    """A short title made from the first words spoken in the clip."""
    words = text.split()
    if not words:
        return ""
    title = " ".join(words[:7])
    return title[:1].upper() + title[1:60]


# --------------------------------------------------------------- rendering

_HIGHLIGHT = "{\\c&H2EC4FF&}"  # the colour of the word being spoken (ASS colours are blue-green-red)
_NORMAL = "{\\c&HFFFFFF&}"
_MAX_WORD = 24  # characters: a longer "word" is cut, because a line can only break at a space
_MAX_TITLE = 80


def ass_time(seconds: float) -> str:
    """Seconds as an ASS timestamp (h:mm:ss.cc). Negative or unusable values become zero."""
    total = round(max(0.0, _number(seconds)) * 100)  # whole hundredths first, so 59.999 never prints as "60.00"
    whole, hundredths = divmod(total, 100)
    return f"{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}.{hundredths:02d}"


def ass_escape(text: str) -> str:
    """Makes text safe inside an ASS Dialogue line: it can start neither a style override nor a new line."""
    text = _plain(text)  # no line breaks or other control characters: each would end the Dialogue line early
    text = re.sub(r"\\[Nnh]", " ", text).replace("\\", "/")  # a backslash starts a line break or a hard space
    return text.replace("{", "(").replace("}", ")")  # braces open an override block such as {\fs200}


def _shorten(text: str, limit: int) -> str:
    """Cuts text to `limit` characters, ending in an ellipsis when something was cut off."""
    return text if len(text) <= limit else text[: limit - 1].rstrip("\u200d ") + "…"


def caption_font(text: str) -> str:
    """A font that is present on this system and can show the text (Devanagari needs its own)."""
    devanagari = bool(re.search(r"[ऀ-ॿ]", text))
    if sys.platform.startswith("win"):
        return "Nirmala UI" if devanagari else "Arial"
    if sys.platform == "darwin":
        return "Kohinoor Devanagari" if devanagari else "Helvetica Neue"
    return "Noto Sans Devanagari" if devanagari else "DejaVu Sans"


def build_ass(words: list[Word], start: float, end: float, title: str = "") -> str:
    """Captions as an ASS subtitle file: two or three words on screen, the spoken one highlighted.

    Times in the file count from `start`. Words and title are treated as plain text whatever they contain.
    """
    start, end = max(0.0, _number(start)), _number(end)
    length = max(0.0, end - start)
    inside: list[Word] = []
    for word in _clean_words(words):
        if start <= word["t"] < end:
            text = _shorten(ass_escape(clean_caption_text(word["w"])), _MAX_WORD)
            if text:
                inside.append({"t": word["t"], "d": word["d"], "w": text})
    title = _shorten(" ".join(ass_escape(title if isinstance(title, str) else "").split()), _MAX_TITLE)
    font = caption_font(" ".join(w["w"] for w in inside) + title)
    style_format = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding"
    )
    lines = [
        *("[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 0"),
        *("ScaledBorderAndShadow: yes", "", "[V4+ Styles]", style_format),
        f"Style: Words,{font},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,2,2,70,70,560,1",
        f"Style: Title,{font},60,&H00FFFFFF,&H00FFFFFF,&H00000000,&HC8000000,-1,0,0,0,100,100,0,0,3,14,0,8,70,70,170,1",
        *("", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"),
    ]
    if title and length >= 0.01:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(length)},Title,,0,0,0,,{title}")
    # group words: up to three, and never across a pause
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in inside:
        too_long = sum(len(w["w"]) for w in current) + len(word["w"]) > 22
        if current and (len(current) >= 3 or word["t"] - (current[-1]["t"] + current[-1]["d"]) > 0.5 or too_long):
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    for g, group in enumerate(groups):
        group_end = min(group[-1]["t"] + group[-1]["d"] + 0.15, groups[g + 1][0]["t"] if g + 1 < len(groups) else end)
        # one long word would run off both edges of the picture, so it is set in smaller type
        longest = max(len(w["w"]) for w in group)
        size = f"{{\\fs{max(54, 86 * 15 // longest)}}}" if longest > 15 else ""
        for i, word in enumerate(group):
            t0 = word["t"] - start
            t1 = min((group[i + 1]["t"] if i + 1 < len(group) else group_end) - start, length)
            if round(t1 * 100) <= round(t0 * 100):  # nothing left once the times are written in hundredths
                continue
            parts = [_HIGHLIGHT + w["w"] + _NORMAL if j == i else w["w"] for j, w in enumerate(group)]
            lines.append(f"Dialogue: 0,{ass_time(t0)},{ass_time(t1)},Words,,0,0,0,,{size}{' '.join(parts)}")
    return "\n".join(lines) + "\n"


def fetch_section(url: str, start: float, end: float, target: Path) -> Path:
    """Downloads just one part of the video, cut exactly at the requested times."""
    from yt_dlp.utils import download_range_func

    start, end = max(0.0, _number(start)), _number(end)
    if end <= start:
        raise ValueError("The clip must end after it starts.")
    target = Path(target)
    folder, stem = target.parent, target.with_suffix("").name
    before = _listing(folder)
    options = ydl_options(
        format="bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        merge_output_format="mp4",
        # "%" starts a field in yt-dlp's name template, so one that belongs to the folder name is doubled
        outtmpl=str(folder / stem).replace("%", "%%") + ".%(ext)s",
        overwrites=True,
        download_ranges=download_range_func(None, [(start, end)]),
        force_keyframes_at_cuts=True,
    )
    try:
        with open_ydl(options) as ydl:
            info = ydl.extract_info(url, download=True)
            path = _downloaded_file(ydl, info, folder, stem)
        if probe_duration(path) <= 0:  # asking for a part past the end "works" but gives a file with nothing in it
            raise RuntimeError("That part of the video could not be downloaded. It may lie past the end of the video.")
    except BaseException:
        _remove_leftovers(folder, stem, before)
        raise
    return path


_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def _filter_graph(layout: str, zoom: float, position: str, captions: str | None) -> str:
    """The ffmpeg filter graph that turns any picture into 1080x1920. `captions` must be a plain, safe file name."""
    # 30 fps first, so the heavy filters never work on frames that would be dropped anyway; then square pixels,
    # because every size below is counted in pixels and a stretched (anamorphic) source would come out distorted
    head = "[0:v]fps=30,scale=iw*sar:ih,setsar=1"
    if layout == "fill":
        x = {"left": "0", "right": "iw-ow"}.get(position, "(iw-ow)/2")
        # "increase" makes the picture cover the whole frame whatever its shape; the crop then takes the overhang
        video = f"{head},scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:{x}:(ih-oh)/2,setsar=1[v0]"
    else:
        # the zoom cuts the sides off, but never further than a 9:16 picture, so a portrait source is not cropped
        # to a sliver; "decrease" then fits the result inside the frame even when it is taller than 9:16
        video = (
            f"{head},split=2[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:4,"
            "eq=brightness=-0.08[bg];"
            f"[b]crop='min(iw,max(iw/{zoom:.3f},ih*9/16))':ih,"
            "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]"
        )
    return video + (f";[v0]ass={captions}[v]" if captions else ";[v0]null[v]")


def render_short(
    segment: Path,
    seek: float,
    length: float,
    out: Path,
    *,
    layout: str = "blur",
    zoom: float = 1.3,
    position: str = "center",
    ass_file: str | None = None,
) -> None:
    """Turns a video segment into a 1080x1920, 30 fps short. `ass_file` is a name inside out.parent.

    `layout` "blur" shows the whole picture (cut at the sides by `zoom`) over a blurred copy of itself; "fill"
    covers the frame and keeps the `position` side of the picture. Raises RuntimeError when ffmpeg fails or
    there is no picture at `seek`, and ValueError for unusable arguments.
    """
    out = Path(out)
    folder = out.parent
    seek, length = max(0.0, _number(seek)), _number(length)
    if length <= 0:
        raise ValueError("The clip length must be more than zero seconds.")
    zoom = max(1.0, min(_number(zoom, 1.3), 2.5))
    captions: str | None = None
    copy: Path | None = None
    if ass_file:
        captions = str(ass_file)
        if captions in (".", "..") or "/" in captions or "\\" in captions or Path(captions).name != captions:
            raise ValueError("The captions file must be a plain file name inside the output folder.")
        if not (folder / captions).is_file():
            raise RuntimeError("ffmpeg could not make the clip: the captions file is missing.")
        if not _SAFE_NAME.fullmatch(captions):
            # ffmpeg parses the filter text itself: a quote, comma, colon, bracket or semicolon in the name would be
            # read as filter syntax. A copy under a plain name needs no escaping at all.
            copy = folder / f"captions-{secrets.token_hex(6)}.ass"
            shutil.copyfile(folder / captions, copy)
            captions = copy.name
    command = [
        *(ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"),
        *("-ss", f"{seek:.3f}", "-t", f"{length:.3f}", *_input_args(os.path.abspath(segment))),
        *("-filter_complex", _filter_graph(layout, zoom, position, captions)),
        *("-map", "[v]", "-map", "0:a:0?"),  # one audio track, if there is any
        *("-af", "loudnorm=I=-14:TP=-1.5:LRA=11"),
        *("-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"),
        *("-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart"),
        "file:" + out.name,
    ]
    # cwd is the output folder so the captions file can be named without a drive letter (ffmpeg filters dislike "C:")
    try:
        process = _run(command, cwd=str(folder))
    finally:
        if copy is not None:
            with contextlib.suppress(OSError):
                copy.unlink(missing_ok=True)
    problem = ""
    if process.returncode != 0 or not out.exists():
        problem = (process.stderr.strip().splitlines() or ["unknown error"])[-1][:300]
    elif probe_duration(out) <= 0:  # ffmpeg reports success for a file with no frames in it
        problem = "there is no picture at that position in the source."
    if problem:
        with contextlib.suppress(OSError):  # a half-written file must not pass for a finished clip
            out.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg could not make the clip: " + problem)

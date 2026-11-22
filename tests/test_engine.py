"""Tests for clipstudio.engine.

Nothing here reaches the internet: yt-dlp is pointed at a small file server on 127.0.0.1 and every
video is generated with ffmpeg (see conftest.py). Tests that need ffmpeg or yt-dlp are skipped, with
the reason, when that tool is not installed.
"""

from __future__ import annotations

import copy
import math
import os
import random
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from clipstudio import engine

pytestmark = pytest.mark.usefixtures("local_network_only")

MOMENT_KEYS = {"start", "end", "peak", "score", "reasons", "text", "title"}
ON_WINDOWS = sys.platform.startswith("win")
# picture size of each small test clip (media.source) as it is shown on screen: (width, height)
SHAPES = {
    "portrait": (360, 640),
    "narrow": (200, 640),
    "square": (480, 480),
    "four_three": (640, 480),
    "tiny": (160, 120),
    "odd": (853, 481),
    "silent": (640, 360),
    "fps120": (640, 360),
    "two_audio": (640, 360),
    "rotated": (480, 640),  # stored as 640x480 with a rotation flag
    "anamorphic": (640, 360),  # stored as 360x360 with wide pixels
}


# ------------------------------------------------------------------ helpers


def burst_levels(seconds: int, bursts: list[tuple[int, int]], quiet: float = -52.0, loud: float = -21.0) -> list[float]:
    """Loudness per second: quiet everywhere except the given (from, to) stretches."""
    return [loud if any(a <= t < b for a, b in bursts) else quiet for t in range(seconds)]


def spoken(count: int, step: float = 0.4, start: float = 0.0, text: str = "word") -> list[dict[str, Any]]:
    """`count` evenly spaced caption words."""
    return [{"t": round(start + i * step, 3), "d": step, "w": text} for i in range(count)]


def random_case(seed: int) -> dict[str, Any]:
    """A reproducible, deliberately messy set of find_moments arguments."""
    rnd = random.Random(seed)
    duration = rnd.choice([8.0, 9.5, 12.0, 30.0, 31.0, 45.7, 60.0, 119.99, 300.0, 601.3, 1800.0])
    seconds = int(duration)
    heatmap: list[Any] = []
    if rnd.random() < 0.6:
        step = duration / 100
        heatmap = [{"start_time": i * step, "end_time": (i + 1) * step, "value": rnd.random() ** 3} for i in range(100)]
        if rnd.random() < 0.5:  # shuffle, overlap, overrun and damage it
            rnd.shuffle(heatmap)
            heatmap += [
                {"start_time": duration - 5, "end_time": duration + 500, "value": 0.9},
                {"start_time": 3, "end_time": 40, "value": rnd.random()},
                {"start_time": 7},
                {"start_time": None, "end_time": 9, "value": 1},
                {"start_time": 20, "end_time": 10, "value": 1},
                {"start_time": 1, "end_time": 2},
                {"start_time": float("nan"), "end_time": 5, "value": 1},
                "junk",
                None,
            ]
    levels: list[Any] = []
    if rnd.random() < 0.7:
        measured = rnd.choice([seconds, seconds + 1, max(0, seconds - 7), seconds // 2, seconds * 2, 5])
        levels = [-55 + 8 * rnd.random() + (30 if rnd.random() < 0.04 else 0) for _ in range(measured)]
        if levels and rnd.random() < 0.3:
            levels[rnd.randrange(len(levels))] = rnd.choice([float("nan"), float("-inf"), None, "loud"])
    words: list[Any] = []
    if rnd.random() < 0.7:
        t = rnd.random() * 3
        vocabulary = ["so", "then", "we", "go", "here.", "wow!", "what", "[Laughter]", "bro", "okay", "right?", "भाई"]
        while t < duration + 5:
            words.append({"t": round(t, 2), "d": 0.3, "w": rnd.choice(vocabulary)})
            t += rnd.choice([0.25, 0.3, 0.35, 0.4, 0.9, 2.5])
        if rnd.random() < 0.4:
            rnd.shuffle(words)
            words += [{"t": -4, "d": 1, "w": "early"}, {"t": 1e15, "d": 1, "w": "late"}, {"t": None, "w": "x"}, 7, None]
    return {
        "duration": duration,
        "heatmap": heatmap,
        "levels": levels,
        "words": words,
        "length": rnd.choice([8, 8, 10, 15, 20, 30, 45, 60, 90, 180]),
        "count": rnd.choice([1, 2, 3, 5, 10, 40]),
    }


def assert_valid_result(result: dict[str, Any], duration: float, length: int, count: int) -> None:
    """Everything find_moments promises about its return value."""
    assert set(result) == {"moments", "signals", "note"}
    assert set(result["signals"]) == {"replays", "audio", "speech", "motion"}
    assert all(isinstance(v, bool) for v in result["signals"].values())
    assert isinstance(result["note"], str)
    moments = result["moments"]
    assert len(moments) <= max(0, count)
    limit = max(engine.MIN_CLIP, min(length, engine.MAX_CLIP))
    for m in moments:
        assert set(m) == MOMENT_KEYS
        assert 0 <= m["start"] < m["end"] <= duration, m
        assert engine.MIN_CLIP - 1e-6 <= m["end"] - m["start"] <= limit + 1e-6, m
        assert isinstance(m["peak"], int) and m["start"] <= m["peak"] < m["end"], m
        assert type(m["score"]) is int and 1 <= m["score"] <= 100, m
        assert m["reasons"] and all(isinstance(r, str) and r for r in m["reasons"])
        assert isinstance(m["text"], str) and len(m["text"]) <= 400
        assert isinstance(m["title"], str) and len(m["title"]) <= 60
        m["text"].encode("utf-8")
    if moments:
        assert moments[0]["score"] == 100
        assert [m["score"] for m in moments] == sorted((m["score"] for m in moments), reverse=True)
    ordered = sorted(moments, key=lambda m: m["start"])
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert earlier["end"] <= later["start"], f"clips overlap: {earlier} and {later}"


def dialogues(ass: str) -> list[dict[str, Any]]:
    """The Dialogue lines of an ASS file, with times in seconds."""

    def seconds(stamp: str) -> float:
        h, m, s = stamp.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    events = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            fields = line[len("Dialogue:") :].strip().split(",", 9)
            assert len(fields) == 10, line
            assert re.fullmatch(r"\d+:[0-5]\d:[0-5]\d\.\d\d", fields[1]) and re.fullmatch(
                r"\d+:[0-5]\d:[0-5]\d\.\d\d", fields[2]
            )
            events.append(
                {"start": seconds(fields[1]), "end": seconds(fields[2]), "style": fields[3], "text": fields[9]}
            )
    return events


OWN_TAGS = re.compile(r"\{\\c&H(?:2EC4FF|FFFFFF)&\}|^\{\\fs\d{2}\}")


def visible_text(event_text: str) -> str:
    """The text of a Dialogue line without the colour and size tags build_ass itself adds."""
    return OWN_TAGS.sub("", event_text)


def free_port() -> int:
    """A port on 127.0.0.1 that nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def white_run(frame: tuple[int, int, bytes], x: int | None = None, y: int | None = None) -> int:
    """Number of pure white pixels along the column `x` or the row `y`.

    The limit is set so that white in the blurred background, which is darkened, does not count.
    """
    width, height, data = frame
    if x is not None:
        return sum(1 for row in range(height) if min(data[(row * width + x) * 3 : (row * width + x) * 3 + 3]) > 244)
    assert y is not None
    return sum(1 for col in range(width) if min(data[(y * width + col) * 3 : (y * width + col) * 3 + 3]) > 244)


def expected_box(shape: tuple[int, int], layout: str, zoom: float) -> tuple[float, float, float]:
    """On-screen size of the white box of a test clip (a quarter of the picture each way), and the scale used."""
    width, height = shape
    if layout == "fill":
        scale = max(1080 / width, 1920 / height)  # cover the frame
    else:
        kept = min(width, max(width / zoom, height * 9 / 16))  # the zoom never cuts below 9:16
        scale = min(1080 / kept, 1920 / height)  # fit inside the frame
    return width / 4 * scale, height / 4 * scale, scale


def stream_lines(info: str) -> list[str]:
    return [line.strip() for line in info.splitlines() if "Stream #" in line]


def assert_is_short(media: Any, path: Path, audio: bool, seconds: float | None = None) -> None:
    """The output format every rendered clip must have."""
    info = media.info(path)
    streams = stream_lines(info)
    video = [s for s in streams if "Video:" in s]
    sound = [s for s in streams if "Audio:" in s]
    assert len(video) == 1, streams
    assert "h264" in video[0] and "yuv420p" in video[0] and "1080x1920" in video[0] and "30 fps" in video[0], video[0]
    assert "SAR 1:1" in video[0] or "SAR" not in video[0], video[0]
    assert "rotation" not in info and "displaymatrix" not in info
    if audio:
        assert len(sound) == 1, streams
        assert "aac" in sound[0] and "48000 Hz" in sound[0], sound[0]
    else:
        assert not sound, streams
    names = media.atoms(path)
    assert "moov" in names and "mdat" in names and names.index("moov") < names.index("mdat"), names  # +faststart
    if seconds is not None:
        assert abs(engine.probe_duration(path) - seconds) < 0.25


# ------------------------------------------------------------ tools, status


def test_tool_status_reports_what_is_installed(ffmpeg_path: str) -> None:
    status = engine.tool_status()
    assert set(status) == {"ytdlp", "ffmpeg", "deno", "captions_filter"}
    assert isinstance(status["ffmpeg"], str) and re.match(r"\w*\d", status["ffmpeg"])
    assert status["captions_filter"] is True
    assert status["ytdlp"] is None or isinstance(status["ytdlp"], str)
    assert status["deno"] is None or re.match(r"\d+\.\d+", status["deno"])


def test_tool_status_when_ytdlp_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yt_dlp", None)  # makes `import yt_dlp` fail
    monkeypatch.setattr(engine, "ffmpeg_exe", lambda: "/nowhere/ffmpeg")
    status = engine.tool_status()
    assert status["ytdlp"] is None


def test_tool_status_when_ffmpeg_package_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    with pytest.raises(ImportError):
        engine.ffmpeg_exe()
    status = engine.tool_status()
    assert status["ffmpeg"] is None and status["captions_filter"] is False


def test_tool_status_when_ffmpeg_program_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(engine, "ffmpeg_exe", lambda: str(tmp_path / "no-such-ffmpeg"))
    status = engine.tool_status()
    assert status["ffmpeg"] is None and status["captions_filter"] is False


def test_tool_status_when_deno_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "deno", None)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(engine, "ffmpeg_exe", lambda: "/nowhere/ffmpeg")
    assert engine.deno_exe() is None
    assert engine.tool_status()["deno"] is None


def test_tool_status_when_deno_cannot_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(engine, "deno_exe", lambda: str(tmp_path / "no-such-deno"))
    monkeypatch.setattr(engine, "ffmpeg_exe", lambda: "/nowhere/ffmpeg")
    assert engine.tool_status()["deno"] is None


def test_deno_exe_falls_back_to_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "deno", None)
    monkeypatch.setattr(shutil, "which", lambda name: f"/somewhere/{name}")
    assert engine.deno_exe() == "/somewhere/deno"


def test_deno_exe_when_the_package_has_no_program(monkeypatch: pytest.MonkeyPatch) -> None:
    def find_deno_bin() -> str:
        raise FileNotFoundError("no deno binary in this wheel")

    monkeypatch.setitem(sys.modules, "deno", types.SimpleNamespace(find_deno_bin=find_deno_bin))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert engine.deno_exe() is None


def test_ydl_options_only_names_deno_when_it_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "ffmpeg_exe", lambda: "/tools/ffmpeg")
    monkeypatch.setattr(engine, "deno_exe", lambda: None)
    options = engine.ydl_options(format="b")
    assert "js_runtimes" not in options
    assert options["format"] == "b" and options["ffmpeg_location"] == "/tools/ffmpeg" and options["noplaylist"] is True
    monkeypatch.setattr(engine, "deno_exe", lambda: "/tools/deno")
    assert engine.ydl_options()["js_runtimes"] == {"deno": {"path": "/tools/deno"}}


def test_ydl_logger_passes_messages_on_but_not_debug_noise(engine_log: list[str]) -> None:
    logger = engine._YdlLogger()
    logger.debug("[debug] internals")
    logger.debug("[download] 10%")
    logger.info("hello")
    logger.warning("careful")
    logger.error("broken")
    assert engine_log == [
        "[yt-dlp] [download] 10%",
        "[yt-dlp] hello",
        "[yt-dlp] warning: careful",
        "[yt-dlp] error: broken",
    ]


# ----------------------------------------------------------- friendly_error

SIGN_IN = "YouTube asked for a sign-in check"
REACH = "The video site could not be reached"
FRIENDLY_CASES = [
    ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser", SIGN_IN),
    ("[youtube] abc: Please prove you are not a bot", SIGN_IN),
    (
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
        "This video is private",
    ),
    ("[youtube] abc: Join this channel to get access to members-only content like this video", "channel members only"),
    (
        "This video is available to this channel's members on level: Gold. Join this channel to get access",
        "members only",
    ),
    ("ERROR: [youtube] abc: Video unavailable. This video is not available", "this video is unavailable"),
    ("[youtube] abc: This video has been removed by the uploader", "this video is unavailable"),
    (
        "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
        "age-restricted",
    ),
    ("This video is age-restricted and only available on YouTube", "age-restricted"),
    (
        "No supported JavaScript runtime could be found. YouTube extraction without a JS runtime has been deprecated",
        "Deno",
    ),
    ("ERROR: Unsupported URL: https://example.com/page", "does not look like a video link"),
    ("ERROR: [generic] 'hello' is not a valid URL", "does not look like a video link"),
    ("<urlopen error [Errno 11001] getaddrinfo failed>", REACH),
    ("The read operation timed out", REACH),
    ("[Errno 111] Connection refused", REACH),
    ("[WinError 10051] A socket operation was attempted to an unreachable network", REACH),
    ("ERROR: [youtube] abc: Unable to download webpage: HTTP Error 429: Too Many Requests", "limiting requests"),
    ("HTTP Error 429 (caused by connection pool)", "limiting requests"),
    ("[Errno 28] No space left on device", "disk is full"),
    ("ERROR: ffmpeg exited with code 183", "stopped before it finished"),
    (
        "[download] Got error: 20000 bytes read, 701171 more expected. Giving up after 5 retries",
        "stopped before it finished",
    ),
    ("Something odd happened", "Something odd happened"),
    ("", "Something went wrong while reading the video."),
    ("ERROR:   ", "Something went wrong while reading the video."),
]


@pytest.mark.parametrize(("message", "expected"), FRIENDLY_CASES)
def test_friendly_error_gives_one_plain_sentence(message: str, expected: str) -> None:
    text = engine.friendly_error(Exception(message))
    assert expected.lower() in text.lower()
    assert "\n" not in text and 0 < len(text) <= 300
    assert not text.startswith("ERROR")


def test_friendly_error_removes_colour_codes() -> None:
    coloured = "\x1b[0;31mERROR:\x1b[0m [youtube] abc: \x1b[1mPrivate video\x1b[0m"
    assert engine.friendly_error(Exception(coloured)) == "This video is private, so it cannot be opened."
    other = engine.friendly_error(Exception("\x1b[0;31mERROR:\x1b[0m \x1b[33;1mweird\x1b[0m failure\x1b[K"))
    assert other == "weird failure"


def test_friendly_error_does_not_mistake_common_words_for_the_age_check() -> None:
    # "age" hides inside "page", "message" and "image"
    text = engine.friendly_error(Exception("Could not parse the page: please confirm the image message format"))
    assert "age-restricted" not in text


def test_friendly_error_keeps_a_traceback_and_local_paths_out() -> None:
    home = str(Path.home())
    trace = (
        "Traceback (most recent call last):\n"
        f'  File "{home}/dost-clip-studio/clipstudio/engine.py", line 120, in fetch\n'
        "    explode()\n"
        "KeyError: 'formats'"
    )
    text = engine.friendly_error(Exception(trace))
    assert text == "KeyError: 'formats'"

    posix = engine.friendly_error(Exception(f"Error opening input file {home}/Videos/My Clips/match day.mp4."))
    assert home not in posix and "match day.mp4" in posix
    other = engine.friendly_error(Exception("could not write /var/tmp/clipstudio/work 7/seg.mp4: Invalid argument"))
    assert "/var/tmp" not in other and "clipstudio/work" not in other
    windows = engine.friendly_error(Exception(r"C:\Users\ritesh\AppData\Local\Temp\cs\seg.mp4: Invalid data found"))
    assert "Users" not in windows and "AppData" not in windows and "seg.mp4: Invalid data found" in windows
    link = engine.friendly_error(Exception("HTTP Error 404: Not Found for https://www.youtube.com/watch?v=abc/def"))
    assert "https://www.youtube.com/watch?v=abc/def" in link


def test_friendly_error_is_never_longer_than_300_characters() -> None:
    long = "x" * 250 + " " + "/home/someone/secret/folder//clip.mp4 " * 400 + "\n" + "more lines\n" * 50
    text = engine.friendly_error(Exception(long))
    assert len(text) <= 300 and "\n" not in text and "/home/someone" not in text
    assert len(engine.friendly_error(Exception("y" * 5000))) == 300


# -------------------------------------------------------------- parse_json3


def automatic_captions() -> dict[str, Any]:
    """Automatic captions as YouTube sends them: one event per line, one timed segment per word."""
    plain = [(t, ["so", " today", " we", " are", " playing"]) for t in range(0, 60, 4)]
    excited = [(64, ["no", " way", " bro", " [Laughter]", " what!"]), (66, ["oh", " my", " god", " insane!", " wow"])]
    later = [(t, ["and", " then", " we", " go", " here"]) for t in range(72, 118, 4)]
    events: list[dict[str, Any]] = [
        {
            "tStartMs": 1000 * t,
            "dDurationMs": 4000,
            "segs": [{"utf8": w, "tOffsetMs": 400 * i} for i, w in enumerate(ws)],
        }
        for t, ws in plain + excited + later
    ]
    events.append({"tStartMs": 5000, "aAppend": 1, "segs": [{"utf8": "\n"}]})
    return {"events": events}


def test_parse_json3_automatic_captions_give_one_timed_word_each() -> None:
    words = engine.parse_json3(automatic_captions())
    assert len(words) > 100
    assert words[1]["w"] == "today" and words[1]["t"] == pytest.approx(0.4)
    assert all(set(w) == {"t", "d", "w"} and w["d"] > 0 for w in words)
    assert [w["t"] for w in words] == sorted(w["t"] for w in words)


def test_parse_json3_uploaded_captions_are_spread_across_their_line() -> None:
    manual = engine.parse_json3(
        {"events": [{"tStartMs": 2000, "dDurationMs": 3000, "segs": [{"utf8": "अरे भाई\nक्या बात है"}]}]}
    )
    assert [w["w"] for w in manual] == ["अरे", "भाई", "क्या", "बात", "है"]
    assert manual[2]["t"] == pytest.approx(3.2) and manual[2]["d"] == pytest.approx(0.6)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "text",
        42,
        {},
        {"events": None},
        {"events": []},
        {"events": "abc"},
        {"events": {"segs": []}},
        {"events": [None, 3, "x", [], {}]},
        {"events": [{"tStartMs": 0, "dDurationMs": 1000}]},
        {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": None}]},
        {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": []}]},
        {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": "abc"}]},
        {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [None, 5, "x"]}]},
        {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{}]}]},
        {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": None}, {"utf8": 5}]}]},
        {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "\n"}, {"utf8": "  \n "}]}]},
        {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "\n", "tOffsetMs": 5}]}]},
    ],
)
def test_parse_json3_returns_nothing_for_unusable_input(data: Any) -> None:
    assert engine.parse_json3(data) == []


def test_parse_json3_keeps_what_is_usable_next_to_junk() -> None:
    data = {
        "events": [
            None,
            "junk",
            {"segs": "abc"},
            {"tStartMs": "oops", "dDurationMs": [], "segs": [{"utf8": "first"}]},  # bad times: starts at zero
            {"tStartMs": 4000, "dDurationMs": 2000, "segs": [{"utf8": "two words"}, None, {"utf8": 7}]},
            {"tStartMs": float("nan"), "dDurationMs": float("inf"), "segs": [{"utf8": "nan"}]},
            {
                "tStartMs": 9000,
                "dDurationMs": 1000,
                "segs": [{"utf8": "a", "tOffsetMs": None}, {"utf8": "b", "tOffsetMs": "x"}],
            },
            {"tStartMs": -5000, "dDurationMs": 1000, "segs": [{"utf8": "early"}]},
        ]
    }
    words = engine.parse_json3(data)
    texts = [w["w"] for w in words]
    assert sorted(texts) == sorted(["first", "two", "words", "nan", "a", "b", "early"])
    assert all(math.isfinite(w["t"]) and w["t"] >= 0 and math.isfinite(w["d"]) and w["d"] > 0 for w in words)
    assert next(w for w in words if w["w"] == "two")["t"] == pytest.approx(4.0)


def test_parse_json3_clamps_an_offset_that_lies_beyond_its_event() -> None:
    event = {
        "tStartMs": 10_000,
        "dDurationMs": 2000,
        "segs": [{"utf8": "in"}, {"utf8": " out", "tOffsetMs": 90_000_000}],
    }
    words = engine.parse_json3({"events": [event]})
    assert [w["w"] for w in words] == ["in", "out"]
    assert words[1]["t"] == pytest.approx(12.0)  # the end of its own line, not 25 hours later
    negative = {"tStartMs": 10_000, "dDurationMs": 2000, "segs": [{"utf8": "x", "tOffsetMs": -700}]}
    assert engine.parse_json3({"events": [negative]})[0]["t"] == pytest.approx(10.0)


def test_parse_json3_copes_with_zero_or_missing_durations() -> None:
    data = {
        "events": [
            {"tStartMs": 1000, "segs": [{"utf8": "no length given here"}]},  # 4 words, next line at 2 s
            {"tStartMs": 2000, "dDurationMs": 0, "segs": [{"utf8": "zero length"}]},
            {
                "tStartMs": 30_000,
                "dDurationMs": 0,
                "segs": [{"utf8": "w1", "tOffsetMs": 0}, {"utf8": "w2", "tOffsetMs": 500}],
            },
        ]
    }
    words = engine.parse_json3(data)
    assert [w["w"] for w in words] == ["no", "length", "given", "here", "zero", "length", "w1", "w2"]
    assert words[3]["t"] < 2.0, "words of a line without a length must end before the next line starts"
    assert words[4]["t"] == pytest.approx(2.0) and words[5]["t"] == pytest.approx(2.4)
    assert words[7]["t"] == pytest.approx(30.5)
    assert all(0.05 <= w["d"] <= 1.5 for w in words)


def test_parse_json3_makes_text_safe_to_store() -> None:
    data = {
        "events": [{"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "bad\ud83dpair tab\there\r\nnext\x00nul"}]}]
    }
    words = engine.parse_json3(data)
    for word in words:
        word["w"].encode("utf-8")  # an unpaired surrogate would raise here
        assert not re.search(r"[\x00-\x1f]", word["w"])
    assert [w["w"] for w in words] == ["bad?pair", "tab", "here", "next", "nul"]


# ------------------------------------------- captions and inspect (no network)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body


class FakeYdl:
    """Stands in for yt_dlp.YoutubeDL: returns canned video details and caption files."""

    info: dict[str, Any] = {}
    pages: dict[str, Any] = {}

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}
        self.opened: list[str] = []

    def __enter__(self) -> FakeYdl:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> Any:
        return self.info

    def urlopen(self, url: str) -> FakeResponse:
        self.opened.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return FakeResponse(page)


def json3_bytes(*texts: str) -> bytes:
    import json

    events = [{"tStartMs": 1000 * i, "dDurationMs": 900, "segs": [{"utf8": text}]} for i, text in enumerate(texts)]
    return json.dumps({"events": events}).encode("utf-8")


def test_fetch_captions_prefers_uploaded_captions_in_the_video_language() -> None:
    ydl = FakeYdl()
    ydl.pages = {"u-hi": json3_bytes("नमस्ते दोस्तों"), "u-en": json3_bytes("hello friends"), "a-hi": json3_bytes("auto")}
    info = {
        "language": "en-US",
        "subtitles": {
            "hi": [{"ext": "vtt", "url": "nope"}, {"ext": "json3", "url": "u-hi"}],
            "en": [{"ext": "json3", "url": "u-en"}],
            "live_chat": [{"ext": "json", "url": "chat"}],
        },
        "automatic_captions": {"hi-orig": [{"ext": "json3", "url": "a-hi"}]},
    }
    words, label = engine.fetch_captions(ydl, info)
    assert [w["w"] for w in words] == ["hello", "friends"] and label == "en (uploaded)"
    assert ydl.opened == ["u-en"]


def test_fetch_captions_uses_the_spoken_language_of_automatic_captions() -> None:
    ydl = FakeYdl()
    ydl.pages = {"a-hi-orig": json3_bytes("अरे भाई"), "a-en": json3_bytes("translated")}
    info = {
        "subtitles": {},
        "automatic_captions": {
            "en": [{"ext": "json3", "url": "a-en"}],
            "hi-orig": [{"ext": "json3", "url": "a-hi-orig"}],
        },
    }
    words, label = engine.fetch_captions(ydl, info)
    assert [w["w"] for w in words] == ["अरे", "भाई"] and label == "hi (automatic)"


def test_fetch_captions_carries_on_when_a_track_cannot_be_read(engine_log: list[str]) -> None:
    ydl = FakeYdl()
    ydl.pages = {
        "broken": OSError("Connection reset by peer"),
        "garbage": b"<html>not json</html>",
        "good": json3_bytes("ok then"),
    }
    info = {
        "subtitles": {
            "hi": [{"ext": "json3", "url": "broken"}],
            "en": [{"ext": "json3", "url": "garbage"}],
            "fr": [{"ext": "json3", "url": "good"}],
        }
    }
    words, label = engine.fetch_captions(ydl, info)
    assert [w["w"] for w in words] == ["ok", "then"] and label == "fr (uploaded)"
    assert len(engine_log) == 2 and all("could not be fetched" in line for line in engine_log)
    assert "could not be reached" in engine_log[0]


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"subtitles": None, "automatic_captions": None},
        {"subtitles": [], "automatic_captions": "x"},
        {"subtitles": {"en": None, "hi": "x", "fr": [None, 5, {"ext": "json3"}, {"ext": "vtt", "url": "u"}]}},
        {"language": 5, "subtitles": {7: [{"ext": "json3", "url": "empty"}]}},
    ],
)
def test_fetch_captions_returns_nothing_when_there_are_no_usable_tracks(info: dict[str, Any]) -> None:
    ydl = FakeYdl()
    ydl.pages = {"empty": json3_bytes()}
    assert engine.fetch_captions(ydl, info) == ([], "")


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch, ytdlp: Any) -> type[FakeYdl]:
    """Replaces yt_dlp.YoutubeDL with FakeYdl, so inspect_url can be tested without any network."""

    class Local(FakeYdl):
        pass

    monkeypatch.setattr(ytdlp, "YoutubeDL", Local)
    monkeypatch.setattr(engine, "ffmpeg_exe", lambda: "ffmpeg")
    return Local


def test_inspect_url_describes_a_finished_video(fake_ydl: type[FakeYdl]) -> None:
    fake_ydl.pages = {"caps": json3_bytes("hello there")}
    fake_ydl.info = {
        "id": 12345,
        "title": "A stream",
        "uploader": "Some Channel",
        "duration": 3605,
        "was_live": True,
        "live_status": "was_live",
        "webpage_url": "https://www.youtube.com/watch?v=abc",
        "thumbnail": "https://i.ytimg.com/vi/abc/hq.jpg",
        "view_count": 99,
        "subtitles": {"en": [{"ext": "json3", "url": "caps"}]},
        "heatmap": [
            {"start_time": 0, "end_time": 36.05, "value": 1.0},
            {"start_time": 50, "end_time": 50, "value": 1.0},  # empty marker
            {"start_time": 90, "end_time": 80, "value": 1.0},  # reversed marker
            {"start_time": None, "end_time": 5},
            "junk",
        ],
    }
    source = engine.inspect_url("https://youtu.be/abc")
    assert set(source) == {
        *("kind", "url", "id", "title", "channel", "duration", "was_live", "thumbnail", "view_count"),
        *("heatmap", "words", "caption_label"),
    }
    assert source["kind"] == "url" and source["url"] == "https://www.youtube.com/watch?v=abc"
    assert source["id"] == "12345" and source["title"] == "A stream" and source["channel"] == "Some Channel"
    assert source["duration"] == 3605.0 and isinstance(source["duration"], float)
    assert source["was_live"] is True and source["view_count"] == 99
    assert source["heatmap"] == [{"start_time": 0.0, "end_time": 36.05, "value": 1.0}]
    assert [w["w"] for w in source["words"]] == ["hello", "there"] and source["caption_label"] == "en (uploaded)"


def test_inspect_url_fills_gaps_with_defaults(fake_ydl: type[FakeYdl]) -> None:
    fake_ydl.info = {"duration": None, "heatmap": None}
    source = engine.inspect_url("https://youtu.be/abc")
    assert source["url"] == "https://youtu.be/abc" and source["id"] == "video" and source["title"] == "Untitled video"
    assert source["channel"] == "" and source["duration"] == 0.0 and source["was_live"] is False
    assert source["heatmap"] == [] and source["words"] == [] and source["caption_label"] == ""


@pytest.mark.parametrize(
    ("info", "message"),
    [
        ({"is_live": True}, "still live"),
        ({"live_status": "is_live"}, "still live"),
        ({"live_status": "is_upcoming"}, "not started yet"),
        ({"_type": "playlist", "entries": []}, "playlist with no videos"),
        ({"_type": "playlist", "entries": [None]}, "playlist with no videos"),
        (None, "did not lead to a video"),
    ],
)
def test_inspect_url_explains_what_cannot_be_clipped(fake_ydl: type[FakeYdl], info: Any, message: str) -> None:
    fake_ydl.info = info
    with pytest.raises(ValueError, match=message):
        engine.inspect_url("https://youtu.be/abc")


def test_inspect_url_takes_the_first_video_of_a_playlist(fake_ydl: type[FakeYdl]) -> None:
    fake_ydl.info = {
        "_type": "playlist",
        "entries": [None, {"id": "first", "title": "One", "duration": 61.5}, {"id": "second"}],
    }
    source = engine.inspect_url("https://youtu.be/abc")
    assert source["id"] == "first" and source["duration"] == 61.5


# ------------------------------------------------------------- find_moments


def test_audio_alone_finds_both_loud_bursts() -> None:
    levels = burst_levels(120, [(41, 46), (90, 94)])
    result = engine.find_moments(120, [], levels, [], length=20, count=3)
    assert_valid_result(result, 120, 20, 3)
    top = sorted((m["start"], m["end"]) for m in result["moments"][:2])
    assert top[0][0] <= 41 <= top[0][1] and top[1][0] <= 90 <= top[1][1]
    assert result["signals"] == {"replays": False, "audio": True, "speech": False, "motion": False}
    assert result["moments"][0]["reasons"] == ["Loud reaction"]
    for m in result["moments"][:2]:  # the burst sits in the first part of its clip, not at the very end
        assert m["start"] <= m["peak"] <= m["start"] + 0.45 * (m["end"] - m["start"])


def test_replay_peak_wins_over_the_always_high_intro() -> None:
    heat = [
        {"start_time": i * 1.2, "end_time": (i + 1) * 1.2, "value": 1.0 if i == 0 else (0.95 if 58 <= i <= 62 else 0.1)}
        for i in range(100)
    ]
    result = engine.find_moments(120, heat, [], [], length=15, count=2)
    assert_valid_result(result, 120, 15, 2)
    best = result["moments"][0]
    assert best["start"] <= 72 <= best["end"] and best["reasons"] == ["Viewers replayed this part"]
    assert result["signals"] == {"replays": True, "audio": False, "speech": False, "motion": False}


def test_excited_speech_is_found_from_captions_alone() -> None:
    words = engine.parse_json3(automatic_captions())
    result = engine.find_moments(120, [], [], words, length=15, count=1)
    assert_valid_result(result, 120, 15, 1)
    best = result["moments"][0]
    assert best["start"] <= 65 <= best["end"] and "Excited or fast speech" in best["reasons"]
    assert "[" not in best["text"] and "Laughter" not in best["text"]
    assert best["title"] and best["title"][0].isupper()
    assert result["signals"] == {"replays": False, "audio": False, "speech": True, "motion": False}


def test_nothing_to_measure_is_reported_not_raised() -> None:
    result = engine.find_moments(120, [], [], [], 20, 3)
    assert result["moments"] == [] and "nothing to measure" in result["note"]
    assert result["signals"] == {"replays": False, "audio": False, "speech": False, "motion": False}


def test_too_few_measurements_do_not_count_as_a_signal() -> None:
    result = engine.find_moments(120, [], [-20.0] * 9, spoken(19), 20, 3)
    assert result["moments"] == [] and result["signals"] == {
        "replays": False,
        "audio": False,
        "speech": False,
        "motion": False,
    }


@pytest.mark.parametrize(
    ("duration", "length", "expected"),
    [
        (10, 30, 0),  # shorter than the clip
        (12, 30, 0),
        (29.9, 30, 0),
        (10, 8, 1),
        (8, 8, 1),
        (8.004, 8, 1),
        (30, 30, 1),  # exactly the clip length: the whole video is the one possible clip
        (31, 30, 1),
        (31.9, 30, 1),
        (7.99, 8, 0),
        (0, 30, 0),
        (-5, 30, 0),
    ],
)
def test_short_videos(duration: float, length: int, expected: int) -> None:
    levels = burst_levels(max(10, int(duration) + 1), [(5, 7)])
    result = engine.find_moments(duration, [], levels, [], length, 5)
    assert_valid_result(result, max(0.0, duration), length, 5)
    assert len(result["moments"]) == expected
    if expected:
        assert result["note"] == ""
    else:
        assert "shorter than the clip length" in result["note"]
    if (duration, length) == (30, 30):
        assert (result["moments"][0]["start"], result["moments"][0]["end"]) == (0.0, 30.0)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf"), None, "abc", [], -1, 0])
def test_unusable_durations_do_not_raise(duration: Any) -> None:
    result = engine.find_moments(
        duration, [{"start_time": 0, "end_time": 5, "value": 1}], [-20.0] * 60, spoken(60), 30, 5
    )
    assert result["moments"] == [] and "shorter" in result["note"]


def test_absurdly_long_duration_is_capped_instead_of_exhausting_memory() -> None:
    started = time.perf_counter()
    result = engine.find_moments(1e15, [], burst_levels(600, [(300, 305)]), [], 30, 2)
    assert time.perf_counter() - started < 10
    assert result["moments"] and result["moments"][0]["start"] <= 300 <= result["moments"][0]["end"]


def six_hour_inputs() -> tuple[list[dict[str, Any]], list[float], list[dict[str, Any]]]:
    rnd = random.Random(1)
    heat = [{"start_time": i * 216.0, "end_time": (i + 1) * 216.0, "value": rnd.random()} for i in range(100)]
    levels = [-50 + 30 * rnd.random() for _ in range(21_600)]
    vocabulary = ["so", "wow", "what", "then", "go", "bro!", "[Laughter]", "okay.", "no", "way"]
    words = [{"t": i * 0.36, "d": 0.3, "w": rnd.choice(vocabulary)} for i in range(60_000)]
    return heat, levels, words


@pytest.mark.parametrize(("length", "count"), [(30, 5), (180, 5), (8, 10), (30, 100_000)])
def test_six_hour_video_is_ranked_in_well_under_a_second(length: int, count: int) -> None:
    heat, levels, words = six_hour_inputs()
    engine.find_moments(600, heat[:3], levels[:600], words[:1500], length, 5)  # warm-up
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        result = engine.find_moments(21_600, heat, levels, words, length, count)
        timings.append(time.perf_counter() - started)
    assert_valid_result(result, 21_600, length, count)
    assert len(result["moments"]) == count if count <= 10 else len(result["moments"]) > 100
    assert min(timings) < 1.0, f"find_moments took {min(timings):.2f}s for a six hour video"


def test_count_larger_than_what_fits() -> None:
    rnd = random.Random(3)
    levels = [-50 + 20 * rnd.random() for _ in range(100)]
    result = engine.find_moments(100, [], levels, [], length=30, count=50)
    assert_valid_result(result, 100, 30, 50)
    assert 1 <= len(result["moments"]) <= 3  # three 30 s clips with gaps is all a 100 s video holds
    assert engine.find_moments(100, [], levels, [], 30, 1_000_000)["moments"] == result["moments"]


@pytest.mark.parametrize(
    ("count", "expected"), [(0, 0), (-3, 0), (1, 1), (2.9, 2), (None, 2), (float("nan"), 2), ("many", 2)]
)
def test_zero_or_unusable_count(count: Any, expected: int) -> None:
    # a 100 second video holds two 30 second clips here; an unusable count falls back to the default of five
    result = engine.find_moments(100, [], burst_levels(100, [(20, 25), (70, 75)]), [], 30, count)
    assert len(result["moments"]) == expected


@pytest.mark.parametrize(
    ("length", "limit"), [(3, 8), (0, 8), (-10, 8), (500, 180), (10**9, 180), (None, 30), ("x", 30)]
)
def test_clip_length_is_held_between_8_and_180_seconds(length: Any, limit: int) -> None:
    result = engine.find_moments(900, [], burst_levels(900, [(400, 410)]), [], length, 3)
    assert result["moments"]
    for m in result["moments"]:
        assert 8 - 1e-6 <= m["end"] - m["start"] <= limit + 1e-6


@pytest.mark.parametrize(
    "inputs",
    [
        {"levels": [-20.0] * 300},  # constant loudness
        {"levels": [-70.0] * 300},  # digital silence
        {"heatmap": [{"start_time": 0, "end_time": 300, "value": 0.0}]},
        {
            "levels": [-33.3] * 300,
            "heatmap": [{"start_time": i * 3, "end_time": i * 3 + 3, "value": 0} for i in range(100)],
        },
    ],
)
def test_all_equal_signals_give_evenly_spread_clips(inputs: dict[str, Any]) -> None:
    result = engine.find_moments(300, inputs.get("heatmap", []), inputs.get("levels", []), [], 30, 5)
    assert_valid_result(result, 300, 30, 5)
    moments = sorted(result["moments"], key=lambda m: m["start"])
    assert len(moments) == 5 and all(m["score"] == 100 for m in moments)
    assert "spread evenly" in result["note"]
    assert all(m["reasons"] == ["Picked to cover the video evenly"] for m in moments)
    assert moments[0]["start"] < 10 and moments[-1]["end"] > 290
    gaps = [b["start"] - a["start"] for a, b in zip(moments, moments[1:], strict=False)]
    assert max(gaps) - min(gaps) <= 1.0


def test_equal_replay_values_still_rank_by_score_with_the_best_at_100() -> None:
    # equal everywhere except the opening, which is turned down: every later window ties
    heat = [{"start_time": i * 3.0, "end_time": (i + 1) * 3.0, "value": 0.5} for i in range(100)]
    result = engine.find_moments(300, heat, [], [], 30, 5)
    assert_valid_result(result, 300, 30, 5)
    assert len(result["moments"]) == 5 and result["moments"][0]["score"] == 100
    assert all(m["start"] >= 15 for m in result["moments"][:3])


def test_flat_video_with_fewer_slots_than_requested() -> None:
    result = engine.find_moments(70, [], [-30.0] * 70, [], 30, 5)
    assert_valid_result(result, 70, 30, 5)
    assert len(result["moments"]) == 2
    one = engine.find_moments(40, [], [-30.0] * 40, [], 30, 5)
    assert_valid_result(one, 40, 30, 5)
    assert len(one["moments"]) == 1 and one["moments"][0]["start"] == 5.0


def test_filler_clips_are_not_called_strong() -> None:
    result = engine.find_moments(300, [], burst_levels(300, [(100, 106)]), [], 30, 3)
    assert_valid_result(result, 300, 30, 3)
    best, *rest = result["moments"]
    assert best["start"] <= 100 <= best["end"] and best["reasons"] == ["Loud reaction"]
    assert rest and all(m["score"] == 1 for m in rest)
    assert all(m["reasons"] == ["Added to reach the number of clips you asked for"] for m in rest)


def test_damaged_heatmap_markers_are_skipped() -> None:
    good = [
        {"start_time": i * 3.0, "end_time": (i + 1) * 3.0, "value": 0.9 if 60 <= i < 64 else 0.1} for i in range(100)
    ]
    junk: list[Any] = [
        {"start_time": 5},
        {"end_time": 9, "value": 1},
        {"start_time": None, "end_time": 4, "value": 1},
        {"start_time": "a", "end_time": "b", "value": "c"},
        {"start_time": 250, "end_time": 240, "value": 1},  # reversed
        {"start_time": 30, "end_time": 30, "value": 1},  # empty
        {"start_time": float("nan"), "end_time": float("inf"), "value": 1},
        {"start_time": -50, "end_time": -10, "value": 1},  # before the video
        {"start_time": 5000, "end_time": 6000, "value": 1},  # after the video
        {"start_time": 100, "end_time": 103},  # no value
        {"start_time": 100, "end_time": 103, "value": None},
        {"start_time": 100, "end_time": 103, "value": -7},
        None,
        "marker",
        17,
        [1, 2, 3],
    ]
    clean = engine.find_moments(300, good, [], [], 30, 3)
    dirty = engine.find_moments(300, junk + good + junk, [], [], 30, 3)
    assert_valid_result(dirty, 300, 30, 3)
    assert dirty == clean
    assert clean["moments"][0]["start"] <= 185 <= clean["moments"][0]["end"]
    assert (
        engine.find_moments(300, [junk[-5]], [], [], 30, 3)["signals"]["replays"] is True
    )  # a negative value counts as 0
    for heatmap in (junk[:11] + junk[12:], None, "x", {"start_time": 0, "end_time": 5, "value": 1}, 5):
        assert engine.find_moments(300, heatmap, [], [], 30, 3)["signals"]["replays"] is False


def test_heatmap_markers_out_of_order_overlapping_and_past_the_end() -> None:
    markers = [
        {"start_time": i * 3.0, "end_time": (i + 1) * 3.0, "value": 0.9 if 60 <= i < 64 else 0.1} for i in range(100)
    ]
    expected = engine.find_moments(300, markers, [], [], 30, 3)
    shuffled = list(markers)
    random.Random(5).shuffle(shuffled)
    assert engine.find_moments(300, shuffled, [], [], 30, 3) == expected
    # weaker markers lying over stronger ones change nothing: the highest value wins
    overlapping = [
        *shuffled,
        {"start_time": 0, "end_time": 300, "value": 0.05},
        {"start_time": 170, "end_time": 200, "value": 0.1},
    ]
    assert engine.find_moments(300, overlapping, [], [], 30, 3) == expected
    overrun = [*markers[:-1], {"start_time": 297.0, "end_time": 9999.0, "value": 0.1}]
    assert engine.find_moments(300, overrun, [], [], 30, 3) == expected
    late_peak = [*markers, {"start_time": 290, "end_time": 4000, "value": 1.0}]
    result = engine.find_moments(300, late_peak, [], [], 30, 3)
    assert_valid_result(result, 300, 30, 3)
    assert result["moments"][0]["end"] <= 300


def test_heatmap_on_another_scale_is_read_the_same_way() -> None:
    unit = [
        {"start_time": i * 3.0, "end_time": (i + 1) * 3.0, "value": 0.9 if 60 <= i < 64 else 0.1} for i in range(100)
    ]
    percent = [dict(m, value=m["value"] * 100) for m in unit]
    as_percent, as_unit = (
        engine.find_moments(300, percent, [], [], 30, 3),
        engine.find_moments(300, unit, [], [], 30, 3),
    )
    assert_valid_result(as_percent, 300, 30, 3)
    assert as_percent["moments"][0]["start"] == pytest.approx(as_unit["moments"][0]["start"], abs=2)
    assert as_percent["moments"][0]["reasons"] == ["Viewers replayed this part"]


@pytest.mark.parametrize("measured", [10, 40, 199, 200, 201, 260, 5000])
def test_levels_shorter_or_longer_than_the_video(measured: int) -> None:
    levels = burst_levels(measured, [(25, 30)])
    result = engine.find_moments(200, [], levels, [], 20, 3)
    assert_valid_result(result, 200, 20, 3)
    assert result["signals"]["audio"] is True
    best = result["moments"][0]
    if measured >= 40:
        assert best["start"] <= 25 <= best["end"]


def test_levels_shorter_than_the_video_do_not_make_the_rest_look_loud() -> None:
    # the measurement stops during a loud second; the unmeasured rest must not inherit that loudness
    levels = [-50.0] * 59 + [-10.0]
    result = engine.find_moments(600, [], levels, [], 30, 1)
    assert result["moments"][0]["end"] <= 95


def test_levels_with_unusable_values() -> None:
    levels: list[Any] = burst_levels(200, [(100, 105)])
    for i, value in enumerate([float("nan"), float("inf"), float("-inf"), None, "loud", [], True]):
        levels[10 + i * 7] = value
    result = engine.find_moments(200, [], levels, [], 20, 2)
    assert_valid_result(result, 200, 20, 2)
    assert result["moments"][0]["start"] <= 100 <= result["moments"][0]["end"]
    assert engine.find_moments(200, [], "not a list", [], 20, 2)["signals"]["audio"] is False  # type: ignore[arg-type]


def test_words_out_of_order_give_the_same_result() -> None:
    words = engine.parse_json3(automatic_captions())
    expected = engine.find_moments(120, [], [], words, 15, 3)
    shuffled = copy.deepcopy(words)
    random.Random(9).shuffle(shuffled)
    assert engine.find_moments(120, [], [], shuffled, 15, 3) == expected


def test_words_with_impossible_times_or_shapes_are_ignored() -> None:
    words = engine.parse_json3(automatic_captions())
    expected = engine.find_moments(120, [], [], words, 15, 3)
    junk: list[Any] = [
        {"t": -30, "d": 0.3, "w": "wow!"},
        {"t": 1e18, "d": 0.3, "w": "wow!"},
        {"t": float("nan"), "d": 0.3, "w": "wow!"},
        {"t": float("inf"), "d": 0.3, "w": "wow!"},
        {"t": None, "d": 0.3, "w": "wow!"},
        {"t": "soon", "d": 0.3, "w": "wow!"},
        {"t": 5, "d": 0.3, "w": None},
        {"t": 5, "d": 0.3, "w": 42},
        {"t": 5, "d": 0.3, "w": "   "},
        {"d": 0.3, "w": "wow!"},
        {"t": 5},
        {},
        None,
        "word",
        3.5,
    ]
    result = engine.find_moments(120, [], [], junk + words + junk, 15, 3)
    assert_valid_result(result, 120, 15, 3)
    assert result == expected
    assert engine.find_moments(120, [], [], junk, 15, 3)["signals"]["speech"] is False
    assert engine.find_moments(120, [], [], "words", 15, 3)["moments"] == []  # type: ignore[arg-type]


def test_words_without_a_length_are_usable() -> None:
    words = [{"t": w["t"], "w": w["w"]} for w in engine.parse_json3(automatic_captions())]
    result = engine.find_moments(120, [], [], words, 15, 1)
    assert_valid_result(result, 120, 15, 1)
    assert result["moments"][0]["start"] <= 65 <= result["moments"][0]["end"]


@pytest.mark.parametrize("seed", range(120))
def test_every_result_keeps_its_promises(seed: int) -> None:
    case = random_case(seed)
    before = copy.deepcopy(case)
    result = engine.find_moments(**case)
    assert_valid_result(result, case["duration"], case["length"], case["count"])
    assert repr(case) == repr(before), "find_moments changed its arguments"  # repr: NaN is not equal to itself
    assert engine.find_moments(**case) == result, "find_moments is not deterministic"


def test_results_do_not_depend_on_set_ordering_between_runs() -> None:
    # a fresh interpreter has a different string hash seed, which changes the iteration order of sets
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]);"
        "from clipstudio import engine; import test_engine;"
        "print(json.dumps([engine.find_moments(**test_engine.random_case(s)) for s in (3, 11, 12, 29)], sort_keys=True))"
    )
    root = Path(__file__).resolve().parents[1]
    outputs = set()
    for seed in ("1", "2", "777"):
        run = subprocess.run(
            [sys.executable, "-c", script, str(root), str(root / "tests")],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert run.returncode == 0, run.stderr[-800:]
        outputs.add(run.stdout)
    assert len(outputs) == 1


def test_clips_do_not_overlap_after_snapping_to_speech() -> None:
    """Two clips picked five seconds apart can both snap to the one pause that lies between them.

    The first then ends 0.10 s before the pause and the second starts 0.15 s before it. Before the fix
    they overlapped by 0.05 s, because overlap was only checked before the cut points were moved.
    """
    heat = [{"start_time": i * 3.0, "end_time": (i + 1) * 3.0, "value": 0.5} for i in range(100)]
    plain = sorted(engine.find_moments(300, heat, [], [], 30, 5)["moments"], key=lambda m: m["start"])
    assert len(plain) == 5
    for first, second in zip(plain, plain[1:], strict=False):
        # one pause in every gap; fewer than 20 words, so the ranking itself is unchanged
        middle = (first["end"] + second["start"]) / 2
        words = [{"t": 1.0, "d": 0.3, "w": "start"}, {"t": round(middle, 2), "d": 0.3, "w": "pause"}]
        result = engine.find_moments(300, heat, [], words, 30, 5)
        assert_valid_result(result, 300, 30, 5)
    every_gap = [{"t": 1.0, "d": 0.3, "w": "start"}] + [
        {"t": round((a["end"] + b["start"]) / 2, 2), "d": 0.3, "w": "pause"}
        for a, b in zip(plain, plain[1:], strict=False)
    ]
    assert_valid_result(engine.find_moments(300, heat, [], every_gap, 30, 5), 300, 30, 5)


def test_clips_do_not_overlap_after_the_peak_is_pulled_forward() -> None:
    """A lone spike shortly before a strong stretch used to produce two overlapping clips.

    The strong stretch (200..230 s) is picked first. The best remaining window that holds the spike at
    190 s starts at 162 s, the spike is near its end, so the clip is moved to start at 178 s: that ran
    into the first clip (178..208 against 200..230) before the fix.
    """
    values = [0.0] * 300
    values[200:230] = [1.0] * 30
    values[190] = 0.9
    heat = [{"start_time": float(t), "end_time": t + 1.0, "value": v} for t, v in enumerate(values)]
    result = engine.find_moments(300, heat, [], [], 30, 2)
    assert_valid_result(result, 300, 30, 2)
    first, second = result["moments"]
    assert first["start"] <= 200 and first["end"] >= 229
    assert second["start"] <= 190 < second["end"] <= first["start"], "the spike is still inside the second clip"


@pytest.mark.parametrize("seed", range(40))
def test_no_overlap_with_dense_speech_and_many_clips(seed: int) -> None:
    rnd = random.Random(1000 + seed)
    duration = rnd.choice([240.0, 600.0, 1200.0])
    length = rnd.choice([8, 12, 20, 30, 60])
    levels = [-55 + 10 * rnd.random() + (25 if rnd.random() < 0.08 else 0) for _ in range(int(duration))]
    words, t = [], 0.0
    while t < duration:
        words.append({"t": round(t, 2), "d": 0.25, "w": rnd.choice(["yes", "no.", "maybe", "wow!", "and", "then?"])})
        t += rnd.choice([0.3, 0.3, 0.4, 0.8, 1.2])
    result = engine.find_moments(duration, [], levels, words, length, 60)
    assert_valid_result(result, duration, length, 60)
    assert len(result["moments"]) >= 3


def test_moment_text_and_title_come_from_the_words_inside_the_clip() -> None:
    words = spoken(30, 0.5, start=100.0, text="hello") + [{"t": 50.0, "d": 0.3, "w": "outside"}]
    words[0]["w"] = ">> bad\ud83dpair"
    words[1]["w"] = "[Applause]"
    result = engine.find_moments(300, [], burst_levels(300, [(100, 110)]), words, 30, 1)
    best = result["moments"][0]
    assert "outside" not in best["text"] and "Applause" not in best["text"] and ">>" not in best["text"]
    assert best["text"].startswith("bad?pair hello") and best["title"].startswith("Bad?pair hello")
    best["text"].encode("utf-8")
    long = engine.find_moments(
        300, [], burst_levels(300, [(100, 110)]), spoken(300, 0.1, 95.0, "supercalifragilistic"), 30, 1
    )
    assert len(long["moments"][0]["text"]) <= 400 and len(long["moments"][0]["title"]) <= 60


def test_signal_helpers_cope_with_empty_input() -> None:
    assert engine.heat_signal([], 100) is None
    assert engine.heat_signal([{"start_time": 0, "end_time": 5, "value": 1}], 0) is None
    assert engine.energy_signal([], 100) is None
    assert engine.energy_signal([-20.0] * 50, 0) is None
    assert engine.speech_signal([], 100) is None
    assert engine.speech_signal(spoken(50), 0) is None
    signal = engine.speech_signal(spoken(50), 30)
    assert signal is not None and len(signal) == 30 and all(0 <= v <= 1 for v in signal)
    energy = engine.energy_signal(burst_levels(60, [(30, 33)]), 60)
    assert energy is not None and len(energy) == 60 and all(0 <= v <= 1 for v in energy)
    assert max(range(60), key=energy.__getitem__) in (30, 31)
    heat = engine.heat_signal([{"start_time": 50, "end_time": 60, "value": 0.8}], 100)
    assert heat is not None and len(heat) == 100 and heat[55] == 0.8 and heat[49] == 0 and heat[60] == 0


def test_speech_signal_scores_excitement_and_ignores_music() -> None:
    calm = spoken(200, 0.5, text="okay")
    for i, text in enumerate(["no", "way", "[Laughter]", "wow!", "let's", "go!"]):
        calm[120 + i]["w"] = text  # around 60 s
    for i in range(40, 50):
        calm[i]["w"] = "[Music]"  # around 20 s
    signal = engine.speech_signal(calm, 100)
    assert signal is not None
    assert max(range(100), key=signal.__getitem__) in range(58, 65)
    assert signal[22] <= signal[80]


# ----------------------------------------------------------- snap_to_speech


def test_snap_without_words_keeps_the_times() -> None:
    assert engine.snap_to_speech(10.0, 40.0, [], 100.0) == (10.0, 40.0)
    assert engine.snap_to_speech(10.0, 40.0, None, 100.0) == (10.0, 40.0)  # type: ignore[arg-type]


def test_snap_moves_both_cuts_to_pauses() -> None:
    words = [
        {"t": 0.0, "d": 0.3, "w": "intro"},
        {"t": 8.6, "d": 0.3, "w": "So"},  # a pause before this word: 8.6 s
        {"t": 9.0, "d": 0.3, "w": "we"},
        {"t": 9.4, "d": 0.3, "w": "begin."},
        {"t": 9.8, "d": 0.3, "w": "Next"},  # sentence start: 9.8 s
        {"t": 30.0, "d": 0.3, "w": "ending"},
        {"t": 31.2, "d": 0.3, "w": "Later"},  # 31.2 s
    ]
    start, end = engine.snap_to_speech(10.0, 30.0, words, 100.0)
    assert start == pytest.approx(9.65) and end == pytest.approx(29.9)  # 0.15 s and 0.10 s before each pause
    start, end = engine.snap_to_speech(10.0, 32.0, words, 100.0)
    assert start == pytest.approx(9.65) and end == pytest.approx(31.1)


@pytest.mark.parametrize(
    ("start", "end", "duration", "expected"),
    [
        (58.0, 60.0, 60.0, (55.0, 60.0)),  # too close to the end: the last five seconds
        (10.0, 11.0, 60.0, (10.0, 15.0)),  # shorter than five seconds: lengthened
        (0.0, 3.0, 3.0, (0.0, 3.0)),  # the whole video is shorter than five seconds
        (-4.0, 10.0, 60.0, (0.0, 10.0)),
        (50.0, 500.0, 60.0, (50.0, 60.0)),
        (40.0, 20.0, 60.0, (40.0, 45.0)),  # reversed
        (float("nan"), float("nan"), 60.0, (0.0, 5.0)),
        (10.0, 20.0, float("nan"), (0.0, 0.0)),
    ],
)
def test_snap_edge_cases(start: float, end: float, duration: float, expected: tuple[float, float]) -> None:
    assert engine.snap_to_speech(start, end, [], duration) == pytest.approx(expected)


@pytest.mark.parametrize("seed", range(60))
def test_snap_never_gives_a_short_or_much_longer_clip(seed: int) -> None:
    rnd = random.Random(seed)
    duration = rnd.choice([5.0, 6.5, 20.0, 90.0, 400.0])
    words: list[Any] = []
    t = rnd.random()
    while t < duration + 10:
        words.append(
            {"t": round(t, 2), "d": rnd.choice([0.2, 0.3, 0.6]), "w": rnd.choice(["a", "b.", "c?", "d", "ओह।"])}
        )
        t += rnd.choice([0.3, 0.5, 1.0, 2.0, 6.0])
    rnd.shuffle(words)
    words += [{"t": -3, "d": 1, "w": "x"}, {"t": float("inf"), "w": "y"}, None, "z"]
    for _ in range(30):
        a = rnd.uniform(0, duration)
        b = min(duration, a + rnd.choice([1.0, 5.0, 8.0, 30.0, 180.0]))
        start, end = engine.snap_to_speech(a, b, words, duration)
        assert 0 <= start < end <= duration
        assert end - start >= 5.0 - 1e-9
        assert end - start <= max(b - a, 5.0) + 6.2, "snapping may add a few seconds, not more"
        if duration - a >= 5:
            assert a - 3.2 <= start <= a + 1.5
    assert engine.snap_to_speech(3.0, 12.0, words, duration) == engine.snap_to_speech(
        3.0,
        12.0,
        sorted((w for w in words if isinstance(w, dict) and 0 <= w["t"] < 1e9), key=lambda w: w["t"]),
        duration,
    )


# ------------------------------------------------------------- trim_lead_in


def quiet_then_sound(sound_from: int, total: int = 60, quiet: float = -62.0, loud: float = -20.0) -> list[float]:
    return [quiet if t < sound_from else loud for t in range(total)]


def test_trim_moves_the_start_to_where_the_sound_begins() -> None:
    assert engine.trim_lead_in(quiet_then_sound(12), 10.0, 40.0) == 12.0
    assert engine.trim_lead_in(quiet_then_sound(12), 10.4, 40.0) == 12.0
    assert isinstance(engine.trim_lead_in(quiet_then_sound(12), 10, 40), float)


def test_trim_never_moves_more_than_max_trim() -> None:
    assert engine.trim_lead_in(quiet_then_sound(20), 10.0, 40.0) == 13.0
    assert engine.trim_lead_in(quiet_then_sound(20), 10.0, 40.0, max_trim=1.0) == 11.0
    assert engine.trim_lead_in(quiet_then_sound(20), 10.0, 40.0, max_trim=7.5) == 17.5
    assert engine.trim_lead_in(quiet_then_sound(12), 10.0, 40.0, max_trim=0.5) == 10.5


def test_trim_always_leaves_five_seconds() -> None:
    assert engine.trim_lead_in(quiet_then_sound(14), 10.0, 16.5) == 11.5
    assert engine.trim_lead_in(quiet_then_sound(14), 10.0, 15.2) == pytest.approx(10.2)
    assert engine.trim_lead_in(quiet_then_sound(14), 10.0, 15.0) == 10.0
    assert engine.trim_lead_in(quiet_then_sound(11), 10.0, 12.0) == 10.0  # already too short: left alone


def test_trim_leaves_a_clip_that_opens_on_sound_alone() -> None:
    assert engine.trim_lead_in(quiet_then_sound(0), 10.0, 40.0) == 10.0
    assert engine.trim_lead_in(quiet_then_sound(10), 10.7, 40.0) == 10.7
    assert engine.trim_lead_in(quiet_then_sound(11), 11.7, 40.0) == 11.7


def test_trim_does_nothing_when_there_is_no_sound_to_open_on() -> None:
    assert engine.trim_lead_in([-70.0] * 60, 10.0, 40.0) == 10.0
    assert engine.trim_lead_in([-61.0] * 60, 10.0, 40.0) == 10.0
    assert engine.trim_lead_in(quiet_then_sound(45), 10.0, 40.0) == 10.0  # the sound starts after the clip


def test_trim_judges_silence_relative_to_the_clip() -> None:
    quiet_talker = [-65.0] * 12 + [-41.0] * 48  # soft speech over a very low floor
    assert engine.trim_lead_in(quiet_talker, 10.0, 40.0) == 12.0
    room_tone = [-44.0] * 12 + [-8.0] * 48  # -44 dB is audible: it is not silence however loud the rest is
    assert engine.trim_lead_in(room_tone, 10.0, 40.0) == 10.0
    hiss = [-52.0] * 12 + [-21.0] * 48  # more than 30 dB under the loud part: dead air
    assert engine.trim_lead_in(hiss, 10.0, 40.0) == 12.0


@pytest.mark.parametrize(
    ("levels", "start", "end", "max_trim", "expected"),
    [
        ([], 10.0, 40.0, 3.0, 10.0),
        (None, 10.0, 40.0, 3.0, 10.0),
        ("levels", 10.0, 40.0, 3.0, 10.0),
        ([-60.0] * 5, 10.0, 40.0, 3.0, 10.0),  # measurement ends before the clip starts
        (quiet_then_sound(12), 10.0, 40.0, 0.0, 10.0),
        (quiet_then_sound(12), 10.0, 40.0, -2.0, 10.0),
        (quiet_then_sound(12), 10.0, 40.0, float("nan"), 10.0),
        (quiet_then_sound(12), -3.0, 40.0, 3.0, 3.0),  # a negative start counts as zero
        (quiet_then_sound(2), -3.0, 40.0, 3.0, 2.0),
        (quiet_then_sound(12), float("nan"), 40.0, 3.0, 3.0),  # so does an unusable one
        (quiet_then_sound(12), 10.0, float("nan"), 3.0, 10.0),
        (quiet_then_sound(12), 10.0, 5.0, 3.0, 10.0),
        ([float("nan"), None, "x", float("-inf")] * 3 + [-20.0] * 30, 10.0, 40.0, 3.0, 12.0),
    ],
)
def test_trim_with_unusable_input(levels: Any, start: float, end: float, max_trim: float, expected: float) -> None:
    assert engine.trim_lead_in(levels, start, end, max_trim) == expected


@pytest.mark.parametrize("seed", range(50))
def test_trim_promises(seed: int) -> None:
    rnd = random.Random(seed)
    levels = [rnd.choice([-70.0, -63.0, -55.0, -47.0, -30.0, -12.0]) for _ in range(rnd.choice([0, 10, 60, 200]))]
    for _ in range(40):
        start = rnd.uniform(0, 150)
        end = start + rnd.uniform(0, 60)
        max_trim = rnd.choice([0.0, 0.5, 3.0, 10.0])
        result = engine.trim_lead_in(levels, start, end, max_trim)
        assert start <= result <= start + max_trim + 1e-9
        if end - start >= 5:
            assert end - result >= 5 - 1e-9
        else:
            assert result == start


# ---------------------------------------------------------------- build_ass

HOSTILE_TEXTS = [
    "{\\fs200}BIG",
    "{\\an5\\pos(540,960)\\p1}m 0 0 l 1080 0 1080 1920 0 1920",
    "{\\c&H0000FF&}red{\\r}",
    "a{b}c",
    "}{",
    "{",
    "\\",
    "back\\slash",
    "C:\\Users\\name",
    "line\\Nbreak",
    "soft\\nbreak\\hspace",
    "\\\\N",
    "comma, separated, text",
    "Wort" * 80,
    "😀🎉👨‍👩‍👧 emoji",
    "अरे भाई क्या बात है",
    "مرحبا بالعالم",
    "שלום עולם",
    "tab\there",
    "new\nline",
    "cr\rDialogue: 0,0:00:00.00,0:00:05.00,Words,,0,0,0,,INJECTED",
    "crlf\r\n[V4+ Styles]\r\nStyle: Words,Arial,300,&H000000FF",
    "sep\u2028Dialogue: 0,0:00:00.00,0:00:05.00,Words,,0,0,0,,INJECTED",
    "nul\x00byte \x1b[31mcolour",
    "lone\ud83dsurrogate",
    "\u202eright-to-left override",
    "  spaced   out  ",
    "[brackets] (parens) >> arrows",
    "Dialogue: 0,0:00:00.00,0:00:05.00,Words,,0,0,0,,{\\fs300}X",
]


@pytest.mark.parametrize("text", HOSTILE_TEXTS)
def test_build_ass_treats_any_text_as_plain_text(text: str) -> None:
    words = [
        {"t": 1.0, "d": 0.4, "w": "plain"},
        {"t": 1.4, "d": 0.4, "w": text},
        {"t": 5.0, "d": 0.4, "w": text + text},
    ]
    ass = engine.build_ass(words, 0, 10, title=text)
    ass.encode("utf-8")  # must be writable as a utf-8 file
    assert not re.search(r"[\x00-\x09\x0b-\x1f\x7f\u2028\u2029\u202e]", ass)
    lines = ass.split("\n")
    header = lines[: lines.index("[Events]") + 2]
    assert len(header) == 14 and header[:9] == engine.build_ass([], 0, 10).split("\n")[:9]
    assert all(re.fullmatch(r"Style: (Words|Title),[A-Za-z ]+,\d+(,[-&\w]+)+", line) for line in header[9:11]), header[
        9:11
    ]
    assert sum(1 for line in lines if line.startswith(("Style:", "[", "Format:"))) == 7
    body = [line for line in lines[lines.index("[Events]") + 2 :] if line]
    assert all(
        re.match(r"Dialogue: [01],\d:\d\d:\d\d\.\d\d,\d:\d\d:\d\d\.\d\d,(Words|Title),,0,0,0,,", line) for line in body
    )
    events = dialogues(ass)
    assert len(events) == len(body)
    assert sum(1 for e in events if e["style"] == "Title") == 1
    assert all(e["start"] >= 0 and e["end"] <= 10 and e["start"] < e["end"] for e in events)
    assert not any(e["start"] == 0 and e["end"] == 5 for e in events), "an injected line made it into the file"
    for event in events:
        shown = visible_text(event["text"]) if event["style"] == "Words" else event["text"]
        assert "{" not in shown and "}" not in shown and "\\" not in shown, event


def test_build_ass_keeps_readable_text_readable() -> None:
    ass = engine.build_ass(
        [{"t": 1.0, "d": 0.4, "w": "अरे"}, {"t": 1.4, "d": 0.4, "w": "{wow},"}], 0, 10, "Wait, for it: 100% {real}"
    )
    events = dialogues(ass)
    assert events[0] == {"start": 0.0, "end": 10.0, "style": "Title", "text": "Wait, for it: 100% (real)"}
    assert [visible_text(e["text"]) for e in events[1:]] == ["अरे (wow),", "अरे (wow),"]
    assert events[1]["text"] == "{\\c&H2EC4FF&}अरे{\\c&HFFFFFF&} (wow),"
    emoji = dialogues(engine.build_ass([{"t": 1.0, "d": 0.4, "w": "😀 مرحبا"}], 0, 10, "שלום 🎉"))
    assert emoji[0]["text"] == "שלום 🎉" and visible_text(emoji[1]["text"]) == "😀 مرحبا"
    assert engine.ass_escape("a\\Nb\\nc\\hd\\e{f}") == "a b c d/e(f)"


def test_build_ass_structure() -> None:
    ass = engine.build_ass(spoken(10, 0.5, start=20.0), 20, 30, "Title")
    assert ass.endswith("\n") and "\r" not in ass
    assert ass.startswith("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n")
    assert ass.count("[V4+ Styles]") == 1 and ass.count("[Events]") == 1
    assert len(re.findall(r"^Style: (Words|Title),", ass, flags=re.M)) == 2
    style_format = next(line for line in ass.splitlines() if line.startswith("Format: Name"))
    for style in (line for line in ass.splitlines() if line.startswith("Style:")):
        assert len(style.split(",")) == len(style_format.split(","))


@pytest.mark.parametrize("seed", range(30))
def test_build_ass_times_and_groups(seed: int) -> None:
    rnd = random.Random(seed)
    start = rnd.choice([0.0, 12.5, 3599.2])
    length = rnd.choice([5.0, 8.0, 30.0, 61.3])
    end = start + length
    vocabulary = [
        "a",
        "of",
        "the",
        "going",
        "यह",
        "wonderful",
        "internationalisation",
        "x" * 40,
        "no.",
        "[Music]",
        "(laughs)",
    ]
    words: list[Any] = []
    t = start - 5
    while t < end + 5:
        words.append({"t": round(t, 3), "d": rnd.choice([0.0, 0.2, 0.4, 3.0]), "w": rnd.choice(vocabulary)})
        t += rnd.choice([0.0, 0.01, 0.2, 0.35, 0.5, 1.5])
    rnd.shuffle(words)
    words += [{"t": -1, "d": 1, "w": "neg"}, {"t": 1e12, "d": 1, "w": "huge"}, {"t": None, "w": "none"}, None, 5]
    ass = engine.build_ass(words, start, end, "T")
    events = [e for e in dialogues(ass) if e["style"] == "Words"]
    assert events
    assert "neg" not in ass and "huge" not in ass and "none" not in ass and "Music" not in ass and "laughs" not in ass
    previous = 0.0
    for event in events:
        assert 0 <= event["start"] < event["end"] <= length + 0.006, event
        assert event["start"] >= previous - 1e-9, "events must come in time order"
        previous = event["start"]
        shown = visible_text(event["text"]).split(" ")
        assert 1 <= len(shown) <= 3
        assert all(len(word) <= 24 for word in shown)
        assert len(shown) == 1 or sum(len(word) for word in shown) <= 22
        assert event["text"].count("{\\c&H2EC4FF&}") == 1, "exactly one word is highlighted"
    # two words on screen at once never happens: each event ends before or when the next begins
    for a, b in zip(events, events[1:], strict=False):
        assert a["end"] <= b["start"] + 0.011


def test_build_ass_leaves_out_words_outside_the_clip() -> None:
    words = [
        {"t": 9.99, "d": 0.3, "w": "before"},
        {"t": 10.0, "d": 0.3, "w": "first"},
        {"t": 19.99, "d": 5.0, "w": "last"},
        {"t": 20.0, "d": 0.3, "w": "after"},
    ]
    ass = engine.build_ass(words, 10, 20)
    events = dialogues(ass)
    assert "before" not in ass and "after" not in ass
    assert [visible_text(e["text"]) for e in events] == ["first", "last"]
    assert events[0]["start"] == 0.0 and events[1]["end"] == 10.0  # the last word is cut off where the clip ends


def test_build_ass_with_only_a_title_or_nothing_at_all() -> None:
    only_title = engine.build_ass([], 5, 20, "Just a title")
    assert dialogues(only_title) == [{"start": 0.0, "end": 15.0, "style": "Title", "text": "Just a title"}]
    nothing = engine.build_ass([], 5, 20)
    assert dialogues(nothing) == [] and nothing.rstrip().endswith("MarginV, Effect, Text")
    assert dialogues(engine.build_ass([], 5, 20, "   \n\t ")) == []
    assert dialogues(engine.build_ass(None, 5, 20, None)) == []  # type: ignore[arg-type]
    for a, b in ((20, 5), (5, 5), (5, float("nan")), (5, float("inf"))):
        assert dialogues(engine.build_ass(spoken(50), a, b, "T")) == []


def test_build_ass_shortens_what_cannot_fit() -> None:
    long_word = "pneumonoultramicroscopicsilicovolcanoconiosis"
    ass = engine.build_ass(
        [{"t": 1.0, "d": 0.5, "w": long_word}, {"t": 3.0, "d": 0.5, "w": "eighteen-characters"}], 0, 10, "T" * 500
    )
    title, first, second = dialogues(ass)
    assert len(title["text"]) == 80 and title["text"].endswith("…")
    assert visible_text(first["text"]) == long_word[:23] + "…"
    size = re.match(r"\{\\fs(\d+)\}", first["text"])
    assert size and 54 <= int(size.group(1)) < 86, "a long single word is set in smaller type so it fits the width"
    assert re.match(r"\{\\fs\d+\}", second["text"])
    assert not dialogues(engine.build_ass([{"t": 1.0, "d": 0.5, "w": "short"}], 0, 10))[0]["text"].startswith("{\\fs")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0:00:00.00"),
        (0.004, "0:00:00.00"),
        (0.006, "0:00:00.01"),
        (59.994, "0:00:59.99"),
        (59.996, "0:01:00.00"),
        (3599.999, "1:00:00.00"),
        (3661.5, "1:01:01.50"),
        (36000, "10:00:00.00"),
        (-5, "0:00:00.00"),
        (float("nan"), "0:00:00.00"),
        (float("inf"), "0:00:00.00"),
    ],
)
def test_ass_time(seconds: float, expected: str) -> None:
    assert engine.ass_time(seconds) == expected


@pytest.mark.parametrize(
    ("platform", "latin", "devanagari"),
    [
        ("win32", "Arial", "Nirmala UI"),
        ("darwin", "Helvetica Neue", "Kohinoor Devanagari"),
        ("linux", "DejaVu Sans", "Noto Sans Devanagari"),
    ],
)
def test_caption_font_per_system(monkeypatch: pytest.MonkeyPatch, platform: str, latin: str, devanagari: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    assert engine.caption_font("hello") == latin
    assert engine.caption_font("hello भाई") == devanagari
    assert f"Style: Words,{devanagari}," in engine.build_ass([], 0, 5, "क्या बात है")
    assert "," not in latin and "," not in devanagari  # a comma would shift every field of the Style line


def test_caption_text_helpers() -> None:
    assert engine.clean_caption_text("  >> [Music] so (laughs)  this   is >>> it ") == "so this is it"
    assert engine.suggest_title("") == "" and engine.suggest_title("   ") == ""
    assert engine.suggest_title("one two three four five six seven eight nine") == "One two three four five six seven"
    assert len(engine.suggest_title("x" * 500)) == 60
    assert engine.suggest_title("अरे भाई") == "अरे भाई"


# ------------------------------------------------- probing and audio levels


def test_probe_duration_without_ffprobe(media: Any) -> None:
    assert engine.probe_duration(media.main) == pytest.approx(90, abs=0.5)
    assert engine.probe_duration(str(media.main)) == pytest.approx(90, abs=0.5)
    assert engine.probe_duration(media.source("audio_only")) == pytest.approx(12, abs=0.3)


@pytest.mark.parametrize("kind", ["corrupt", "empty", "text"])
def test_probe_duration_of_an_unreadable_file_is_zero(media: Any, kind: str) -> None:
    assert engine.probe_duration(media.source(kind)) == 0.0


def test_probe_duration_of_a_missing_file_is_zero(media: Any, tmp_path: Path) -> None:
    assert engine.probe_duration(tmp_path / "nothing here.mp4") == 0.0


@pytest.mark.skipif(ON_WINDOWS, reason="Windows does not allow a colon in a file name")
def test_a_colon_in_a_file_name_is_not_read_as_a_protocol(
    media: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copyfile(media.source("square"), tmp_path / "part2:the end.mp4")
    monkeypatch.chdir(tmp_path)
    assert engine.probe_duration("part2:the end.mp4") == pytest.approx(3, abs=0.2)
    assert len(engine.audio_levels("part2:the end.mp4")) in (3, 4)
    assert engine.inspect_file(Path("part2:the end.mp4"))["duration"] == pytest.approx(3, abs=0.2)
    engine.render_short(Path("part2:the end.mp4"), 0, 0.5, tmp_path / "out.mp4", layout="fill")
    assert engine.probe_duration(tmp_path / "out.mp4") == pytest.approx(0.5, abs=0.1)


def test_a_playlist_given_as_a_local_file_cannot_make_ffmpeg_go_online(
    media: Any, http_server: Any, tmp_path: Path
) -> None:
    # ffmpeg only allows local protocols when its input is a local file; this guards that default
    fake = tmp_path / "upload.m3u8"
    fake.write_text(
        f"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.0,\n{http_server.url('sample.mp4')}\n#EXT-X-ENDLIST\n"
    )
    seen = len(http_server.requests)
    assert engine.probe_duration(fake) == 0.0
    with pytest.raises(RuntimeError):
        engine.audio_levels(fake)
    with pytest.raises(RuntimeError):
        engine.render_short(fake, 0, 1, tmp_path / "out.mp4")
    assert http_server.requests[seen:] == [], "ffmpeg followed a link inside a local file"


def test_inspect_file(media: Any, tmp_path: Path) -> None:
    odd_name = tmp_path / "My Clip (final) ü #1.mp4"
    shutil.copyfile(media.source("square"), odd_name)
    source = engine.inspect_file(odd_name)
    assert set(source) == {
        *("kind", "path", "id", "title", "channel", "duration", "was_live", "thumbnail", "view_count"),
        *("heatmap", "words", "caption_label"),
    }
    assert source["kind"] == "file" and source["path"] == str(odd_name) and source["title"] == "My Clip (final) ü #1"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,40}", source["id"])
    assert source["duration"] == pytest.approx(3, abs=0.2) and source["was_live"] is False
    assert source["heatmap"] == [] and source["words"] == [] and source["caption_label"] == ""
    assert engine.inspect_file(media.source("silent"))["duration"] == pytest.approx(3, abs=0.2)
    shutil.copyfile(media.source("square"), tmp_path / "ü.mp4")
    assert engine.inspect_file(tmp_path / "ü.mp4")["id"] == "-"


@pytest.mark.parametrize("kind", ["corrupt", "empty", "text", "missing"])
def test_inspect_file_rejects_what_is_not_a_video(media: Any, tmp_path: Path, kind: str) -> None:
    path = tmp_path / "missing.mp4" if kind == "missing" else media.source(kind)
    with pytest.raises(ValueError, match="could not be read as a video"):
        engine.inspect_file(path)


def test_audio_levels_give_one_value_per_second(media: Any) -> None:
    levels = engine.audio_levels(media.main)
    assert 89 <= len(levels) <= 92
    assert all(isinstance(v, float) and -70 <= v <= 0 for v in levels)
    quiet = [v for t, v in enumerate(levels[:89]) if not any(a - 1 <= t <= b for a, b in ((40, 46), (70, 74)))]
    loud = [levels[t] for a, b in ((40, 46), (70, 74)) for t in range(a, b)]
    assert min(loud) > max(quiet) + 15, f"{min(loud):.1f} dB in the bursts against {max(quiet):.1f} dB outside"
    assert engine.audio_levels(str(media.main)) == levels


def test_audio_levels_drive_find_moments_and_trim(media: Any) -> None:
    levels = engine.audio_levels(media.main)
    result = engine.find_moments(90, [], levels, [], length=15, count=2)
    assert_valid_result(result, 90, 15, 2)
    spans = sorted((m["start"], m["end"]) for m in result["moments"])
    assert spans[0][0] <= 40 < spans[0][1] and spans[1][0] <= 70 < spans[1][1]
    assert all(m["reasons"] == ["Loud reaction"] for m in result["moments"])
    assert engine.trim_lead_in(levels, 38.0, 60.0) == 40.0  # the first burst starts at 40 s
    assert engine.trim_lead_in(levels, 41.5, 60.0) == 41.5  # already inside it
    assert engine.trim_lead_in(levels, 30.0, 60.0) == 33.0  # ten quiet seconds: only three are cut


def test_audio_levels_of_a_video_without_sound_is_empty(media: Any) -> None:
    assert engine.audio_levels(media.source("silent")) == []
    assert engine.audio_levels(media.source("anamorphic")) == []
    # and the caller can carry on with whatever other signals it has
    result = engine.find_moments(3, [], engine.audio_levels(media.source("silent")), [], 30, 5)
    assert result["moments"] == [] and result["signals"]["audio"] is False
    heat = [{"start_time": 100, "end_time": 130, "value": 1.0}, {"start_time": 0, "end_time": 300, "value": 0.1}]
    with_heat = engine.find_moments(300, heat, engine.audio_levels(media.source("silent")), [], 30, 1)
    assert with_heat["signals"] == {"replays": True, "audio": False, "speech": False, "motion": False}
    assert with_heat["moments"][0]["start"] <= 100 < with_heat["moments"][0]["end"]


def test_audio_levels_of_an_audio_only_file(media: Any) -> None:
    levels = engine.audio_levels(media.source("audio_only"))
    assert 12 <= len(levels) <= 13 and all(-30 < v < -15 for v in levels[:12])


def test_audio_levels_measures_the_first_track_only(media: Any) -> None:
    assert len(engine.audio_levels(media.source("two_audio"))) in (3, 4)


@pytest.mark.parametrize("kind", ["corrupt", "empty", "text", "missing"])
def test_audio_levels_of_an_unreadable_file_raises_a_short_error(media: Any, tmp_path: Path, kind: str) -> None:
    path = tmp_path / "missing.mp4" if kind == "missing" else media.source(kind)
    with pytest.raises(RuntimeError) as caught:
        engine.audio_levels(path)
    assert 0 < len(str(caught.value)) <= 300
    friendly = engine.friendly_error(caught.value)
    assert str(path.parent) not in friendly and "\n" not in friendly


# ------------------------------------------------------------- render_short

BLUR_13 = (-162.0, 565.0, 1404.0, 790.0)  # where the whole 1280x720 picture sits in a blur render at zoom 1.3
BLUR_10 = (0.0, 656.0, 1080.0, 608.0)  # the same at zoom 1.0
FILL_CENTER = (-1166.7, 0.0, 3413.3, 1920.0)  # the same in the fill layout, centred


def test_render_blur_with_captions_and_title(media: Any, tmp_path: Path) -> None:
    text = "अरे भाई ये क्या हो गया no way bro this is {insane} wow"
    words = [{"t": 40.1 + 0.4 * i, "d": 0.4, "w": w} for i, w in enumerate(text.split())]
    (tmp_path / "c1.ass").write_text(engine.build_ass(words, 40, 43, "Wait for it"), encoding="utf-8")
    out = tmp_path / "c1.mp4"
    result = engine.render_short(media.main, 40, 3, out, layout="blur", zoom=1.3, ass_file="c1.ass")
    assert result is None
    assert_is_short(media, out, audio=True, seconds=3.0)
    assert media.timecode(media.frame(out, 0.0), BLUR_13) == 40.0, "the clip must start exactly at the seek time"
    assert media.timecode(media.frame(out, 2.95), BLUR_13) == 42.9
    with_captions = media.frame(out, 1.0)
    assert media.count_colour(with_captions, (0xFF, 0xC4, 0x2E)) > 300, "the highlighted caption word is missing"
    levels = engine.audio_levels(out)
    assert len(levels) in (3, 4) and min(levels[:3]) > -30, "the loud burst at 40 s should be audible"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c1.ass", "c1.mp4"]


@pytest.mark.parametrize("position", ["left", "center", "right"])
def test_render_fill_positions(media: Any, tmp_path: Path, position: str) -> None:
    out = tmp_path / f"{position}.mp4"
    engine.render_short(media.main, 10, 1, out, layout="fill", position=position)
    assert_is_short(media, out, audio=True, seconds=1.0)
    frame = media.frame(out, 0.5)
    red, _, blue = media.pixel(frame, 540, 1200)
    if position == "left":
        assert red > blue + 60, "the left third of the source is red"
    elif position == "right":
        assert blue > red + 60, "the right third of the source is blue"
    else:
        assert abs(red - blue) < 40
        assert media.timecode(frame, FILL_CENTER) == 10.5
    assert min(media.pixel(frame, 540, 6)) > 200 and min(media.pixel(frame, 540, 1913)) > 200, (
        "top and bottom edge fill the frame"
    )


@pytest.mark.parametrize(
    ("zoom", "height"), [(1.0, 608), (0.2, 608), (-3, 608), (1.5, 911), (2.5, 1519), (9, 1519), (float("nan"), 790)]
)
def test_render_zoom_is_held_between_1_and_2_5(media: Any, tmp_path: Path, zoom: float, height: int) -> None:
    out = tmp_path / "zoom.mp4"
    engine.render_short(media.main, 5, 0.5, out, layout="blur", zoom=zoom)
    assert_is_short(media, out, audio=True)
    width, total, data = frame = media.frame(out, 0.2)
    # the sharp white lines along the top and bottom edge of the source mark where the picture sits
    rows = [y for y in range(total) if min(media.pixel(frame, 540, y)) > 215 and min(media.pixel(frame, 300, y)) > 215]
    assert rows and abs((rows[-1] - rows[0] + 1) - height) <= 8, (
        f"picture height {rows[-1] - rows[0] + 1}, expected {height}"
    )
    assert abs((rows[0] + rows[-1]) / 2 - 960) <= 6, "the picture is centred"


@pytest.mark.parametrize("position", ["left", "right", "center"])
def test_render_blur_accepts_every_position(media: Any, tmp_path: Path, position: str) -> None:
    out = tmp_path / "out.mp4"
    engine.render_short(
        media.source("four_three"), 0, 0.5, out, layout="blur", zoom=1.3, position=position, ass_file=""
    )
    assert_is_short(media, out, audio=True)
    wide, high, scale = expected_box(SHAPES["four_three"], "blur", 1.3)
    frame = media.frame(out, 0.2)  # the blur layout keeps the middle of the picture whatever the position
    assert white_run(frame, y=960) == pytest.approx(wide, abs=12) and white_run(frame, x=540) == pytest.approx(
        high, abs=12
    )


def test_filter_graph_text() -> None:
    graph = engine._filter_graph
    assert graph("fill", 1.3, "left", None).count("crop=1080:1920:0:") == 1
    assert "crop=1080:1920:iw-ow:" in graph("fill", 1.3, "right", None)
    for position in ("center", "middle", "", None):
        assert "crop=1080:1920:(iw-ow)/2:" in graph("fill", 1.3, position, None)  # type: ignore[arg-type]
    for layout in ("blur", "anything", "", None):
        assert "boxblur" in graph(layout, 1.3, "center", None)  # type: ignore[arg-type]
    assert "boxblur" not in graph("fill", 1.3, "center", None)
    assert "iw/2.500" in graph("blur", 2.5, "center", None)
    assert graph("blur", 1.3, "center", "c1.ass").endswith(";[v0]ass=c1.ass[v]")
    assert graph("blur", 1.3, "center", None).endswith(";[v0]null[v]")
    for layout in ("blur", "fill"):
        assert graph(layout, 1.3, "center", None).startswith("[0:v]fps=30,")


@pytest.mark.parametrize("layout", ["blur", "fill"])
@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_render_any_source_shape(media: Any, tmp_path: Path, kind: str, layout: str) -> None:
    out = tmp_path / "out.mp4"
    engine.render_short(media.source(kind), 0.5, 1, out, layout=layout, zoom=1.3)
    assert_is_short(media, out, audio=kind not in ("silent", "anamorphic"), seconds=1.0)
    # the white box in the middle of the source shows how the picture was scaled, and that it was not distorted
    frame = media.frame(out, 0.5)
    wide, high, scale = expected_box(SHAPES[kind], layout, 1.3)
    slack = max(10, 1.5 * scale)
    assert white_run(frame, y=960) == pytest.approx(wide, abs=slack), "box width: wrong scale, crop or pixel shape"
    assert white_run(frame, x=540) == pytest.approx(high, abs=slack), "box height: wrong scale or rotation"


def test_test_clips_have_the_shapes_they_claim(media: Any) -> None:
    assert "853x481" in media.info(media.source("odd"))
    assert "rotation of" in media.info(media.source("rotated")) and "640x480" in media.info(media.source("rotated"))
    assert "360x360 [SAR 16:9" in media.info(media.source("anamorphic"))
    assert "120 fps" in media.info(media.source("fps120"))
    assert len([line for line in stream_lines(media.info(media.source("two_audio"))) if "Audio:" in line]) == 2
    assert "Audio:" not in media.info(media.source("silent"))


@pytest.mark.parametrize(
    ("kind", "zoom"), [("portrait", 2.0), ("portrait", 1.0), ("narrow", 2.5), ("square", 2.5), ("rotated", 1.0)]
)
def test_render_zoom_never_crops_below_nine_by_sixteen(media: Any, tmp_path: Path, kind: str, zoom: float) -> None:
    """A portrait source keeps its full width whatever the zoom; a square one is cropped to 9:16 at most."""
    out = tmp_path / "out.mp4"
    engine.render_short(media.source(kind), 0, 0.5, out, layout="blur", zoom=zoom)
    frame = media.frame(out, 0.2)
    wide, high, scale = expected_box(SHAPES[kind], "blur", zoom)
    assert white_run(frame, y=960) == pytest.approx(wide, abs=max(10, 1.5 * scale))
    assert white_run(frame, x=540) == pytest.approx(high, abs=max(10, 1.5 * scale))
    if kind == "portrait":  # 9:16 already: the picture fills the frame, with its red and blue edges still in view
        assert wide == pytest.approx(270) and high == pytest.approx(480)
        left, right = media.pixel(frame, 60, 960), media.pixel(frame, 1020, 960)
        assert left[0] > left[2] + 60 and right[2] > right[0] + 60


def test_render_high_frame_rate_source_keeps_its_speed(media: Any, tmp_path: Path) -> None:
    out = tmp_path / "fast.mp4"
    engine.render_short(media.source("fps120"), 0, 2, out, layout="blur")
    assert_is_short(media, out, audio=True, seconds=2.0)


def test_render_segment_shorter_than_the_requested_length(media: Any, tmp_path: Path) -> None:
    out = tmp_path / "short.mp4"
    engine.render_short(media.source("square"), 1, 10, out)
    assert_is_short(media, out, audio=True)
    assert engine.probe_duration(out) == pytest.approx(2.0, abs=0.25)


def test_render_past_the_end_of_the_segment_is_an_error_not_an_empty_file(media: Any, tmp_path: Path) -> None:
    out = tmp_path / "nothing.mp4"
    with pytest.raises(RuntimeError, match="could not make the clip: there is no picture at that position"):
        engine.render_short(media.source("square"), 10, 5, out)
    assert not out.exists()


@pytest.mark.parametrize("kind", ["missing", "corrupt", "empty", "text", "audio_only"])
def test_render_unusable_input_raises_a_readable_error(media: Any, tmp_path: Path, kind: str) -> None:
    source = tmp_path / "missing.mp4" if kind == "missing" else media.source(kind)
    out = tmp_path / "out.mp4"
    out.write_bytes(b"left over from an earlier run")
    with pytest.raises(RuntimeError, match="ffmpeg could not make the clip: ") as caught:
        engine.render_short(source, 0, 5, out)
    assert "\n" not in str(caught.value) and len(str(caught.value)) < 340
    assert not out.exists(), "a failed render must not leave a file behind"


@pytest.mark.parametrize("length", [0, -1, float("nan"), float("inf"), None, "long"])
def test_render_rejects_an_unusable_length(tmp_path: Path, length: Any) -> None:
    with pytest.raises(ValueError, match="more than zero"):
        engine.render_short(tmp_path / "in.mp4", 0, length, tmp_path / "out.mp4")


def test_render_negative_seek_starts_at_zero(media: Any, tmp_path: Path) -> None:
    out = tmp_path / "out.mp4"
    engine.render_short(media.main, -20, 0.5, out, layout="fill")
    assert media.timecode(media.frame(out, 0.0), FILL_CENTER) == 0.0


def test_render_relative_paths(media: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the segment is named relative to the current folder, the output goes into another folder
    shutil.copyfile(media.source("square"), tmp_path / "in.mp4")
    (tmp_path / "shorts").mkdir()
    monkeypatch.chdir(tmp_path)
    engine.render_short(Path("in.mp4"), 0, 0.5, Path("shorts") / "out.mp4")
    assert_is_short(media, tmp_path / "shorts" / "out.mp4", audio=True)
    engine.render_short(Path("in.mp4"), 0, 0.5, Path("here.mp4"))
    assert (tmp_path / "here.mp4").stat().st_size > 1000


AWKWARD_FOLDERS = [
    "my clips",
    "it's here",
    "क्लिप éñ 日本 😀",
    "100% done (final) & more",
    "-starts-with-a-dash",
    pytest.param(
        "a:b,c[d];e'f=g\\h", marks=pytest.mark.skipif(ON_WINDOWS, reason="not a valid folder name on Windows")
    ),
    pytest.param("semi;colon, [brackets] = 'quotes'", id="filter-syntax-characters"),
]


@pytest.mark.parametrize("folder", AWKWARD_FOLDERS)
def test_render_into_awkwardly_named_folders(media: Any, tmp_path: Path, folder: str) -> None:
    work = tmp_path / folder
    work.mkdir()
    segment = work / "seg ment's ü.mp4"
    shutil.copyfile(media.source("four_three"), segment)
    words = [{"t": 0.1, "d": 0.9, "w": "caption"}]
    (work / "c1.ass").write_text(engine.build_ass(words, 0, 1, "Title"), encoding="utf-8")
    out = work / "short one's ü #1.mp4"
    engine.render_short(segment, 0, 1, out, layout="blur", ass_file="c1.ass")
    assert_is_short(media, out, audio=True, seconds=1.0)
    assert media.count_colour(media.frame(out, 0.5), (0xFF, 0xC4, 0x2E)) > 300, "captions were not burnt in"
    assert sorted(p.name for p in work.iterdir()) == sorted(["c1.ass", segment.name, out.name])


@pytest.mark.skipif(ON_WINDOWS, reason="Windows does not allow these characters in a file name")
@pytest.mark.parametrize("name", ["a:b.mp4", "-dash.mp4", "pipe|star*.mp4", "%03d.mp4"])
def test_render_output_names_that_look_like_options_or_protocols(media: Any, tmp_path: Path, name: str) -> None:
    out = tmp_path / name
    engine.render_short(media.source("tiny"), 0, 0.5, out, layout="fill")
    assert_is_short(media, out, audio=True)
    assert [p.name for p in tmp_path.iterdir()] == [name]


UNSAFE_CAPTION_NAMES = [
    "my captions.ass",
    "it's.ass",
    "caps,with;filter[syntax]=1.ass",
    "-dash.ass",
    ".hidden.ass",
    "क्लिप.ass",
    "100%.ass",
    pytest.param("a:b.ass", marks=pytest.mark.skipif(ON_WINDOWS, reason="not a valid file name on Windows")),
]


@pytest.mark.parametrize("name", UNSAFE_CAPTION_NAMES)
def test_render_never_puts_an_unsafe_caption_name_into_the_filter(
    media: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    content = engine.build_ass([{"t": 0.1, "d": 0.9, "w": "caption"}], 0, 1)
    (tmp_path / name).write_text(content, encoding="utf-8")
    commands: list[list[str]] = []
    real_run = engine._run

    def spy(command: list[str], cwd: str | None = None) -> Any:
        commands.append(command)
        return real_run(command, cwd)

    monkeypatch.setattr(engine, "_run", spy)
    out = tmp_path / "out.mp4"
    engine.render_short(media.source("four_three"), 0, 1, out, ass_file=name)
    graph = commands[0][commands[0].index("-filter_complex") + 1]
    used = re.search(r"\]ass=(.*)\[v\]$", graph)
    assert used and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", used.group(1)), graph
    assert media.count_colour(media.frame(out, 0.5), (0xFF, 0xC4, 0x2E)) > 300, "captions were not burnt in"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([name, "out.mp4"]), "the temporary copy must be removed"
    assert (tmp_path / name).read_text(encoding="utf-8") == content


def test_render_uses_a_safe_caption_name_as_it_is(media: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "clip_01-final.v2.ass").write_text(engine.build_ass([], 0, 1, "T"), encoding="utf-8")
    commands: list[list[str]] = []
    real_run = engine._run
    monkeypatch.setattr(engine, "_run", lambda command, cwd=None: commands.append(command) or real_run(command, cwd))
    engine.render_short(media.source("tiny"), 0, 0.5, tmp_path / "out.mp4", ass_file="clip_01-final.v2.ass")
    assert commands[0][commands[0].index("-filter_complex") + 1].endswith("ass=clip_01-final.v2.ass[v]")
    assert "shell" not in repr(commands) and all(isinstance(part, str) for part in commands[0])


@pytest.mark.parametrize("name", ["../c1.ass", "sub/c1.ass", "sub\\c1.ass", "/etc/passwd", "..", ".", "C:\\c1.ass"])
def test_render_refuses_a_caption_path_outside_the_output_folder(tmp_path: Path, name: str) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c1.ass").write_text("x", encoding="utf-8")
    (tmp_path / "out").mkdir()
    with pytest.raises(ValueError, match="plain file name"):
        engine.render_short(tmp_path / "in.mp4", 0, 1, tmp_path / "out" / "out.mp4", ass_file=name)


def test_render_with_a_missing_caption_file(media: Any, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="captions file is missing"):
        engine.render_short(media.source("tiny"), 0, 1, tmp_path / "out.mp4", ass_file="nope.ass")
    assert list(tmp_path.iterdir()) == []


def test_render_removes_the_caption_copy_when_ffmpeg_fails(media: Any, tmp_path: Path) -> None:
    (tmp_path / "my caps.ass").write_text(engine.build_ass([], 0, 1, "T"), encoding="utf-8")
    with pytest.raises(RuntimeError):
        engine.render_short(media.source("corrupt"), 0, 1, tmp_path / "out.mp4", ass_file="my caps.ass")
    assert [p.name for p in tmp_path.iterdir()] == ["my caps.ass"]


def test_render_with_hostile_caption_text(media: Any, tmp_path: Path) -> None:
    """Every hostile string at once, as words and as the title: the file must still render as plain captions."""
    words = [{"t": 0.05 + 0.05 * i, "d": 0.05, "w": text} for i, text in enumerate(HOSTILE_TEXTS)]
    hostile_ass = engine.build_ass(words, 0, 1.5, "{\\fs200\\an5}" + HOSTILE_TEXTS[1])
    (tmp_path / "hostile.ass").write_text(hostile_ass, encoding="utf-8")
    # the reference shares the header, so the font is the same, and spells the word with harmless brackets by hand
    header = hostile_ass.split("[Events]")[0]
    events = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    harmless = "Dialogue: 0,0:00:00.05,0:00:01.50,Words,,0,0,0,,{\\c&H2EC4FF&}(/fs200)BIG{\\c&HFFFFFF&}"
    tame_ass = f"{header}[Events]\n{events}\n{harmless}\n"
    (tmp_path / "tame.ass").write_text(tame_ass, encoding="utf-8")
    hostile, tame = tmp_path / "hostile.mp4", tmp_path / "tame.mp4"
    engine.render_short(media.source("four_three"), 0, 1.5, hostile, ass_file="hostile.ass")
    engine.render_short(media.source("four_three"), 0, 1.5, tame, ass_file="tame.ass")
    assert_is_short(media, hostile, audio=True, seconds=1.5)
    # the first word on screen is "{\fs200}BIG": it must look like the same text typed with harmless brackets.
    # It is shown from 0.05 s to 0.10 s, which at 30 frames a second is the single frame at 0.067 s.
    lit = media.count_colour(media.frame(hostile, 0.06), (0xFF, 0xC4, 0x2E))
    expected = media.count_colour(media.frame(tame, 0.06), (0xFF, 0xC4, 0x2E))
    assert expected > 300 and 0.85 * expected < lit < 1.15 * expected, f"{lit} highlighted pixels against {expected}"
    # a drawing command that took effect would paint the whole frame white
    for at in (0.2, 0.5, 1.0, 1.4):
        frame = media.frame(hostile, at, size=(108, 192))
        assert media.count_colour(frame, (255, 255, 255), tolerance=30) < 0.5 * 108 * 192


# ------------------------------------------- yt-dlp against the local server


def folder_names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def test_fetch_section_is_cut_at_the_asked_times(ytdlp: Any, http_server: Any, media: Any, tmp_path: Path) -> None:
    path = engine.fetch_section(http_server.url("sample.mp4"), 38.3, 48.3, tmp_path / "seg")
    assert path == tmp_path / "seg.mp4" and folder_names(tmp_path) == ["seg.mp4"]
    assert engine.probe_duration(path) == pytest.approx(10.0, abs=0.15)
    assert media.timecode(media.frame(path, 0.0)) == 38.3, "the first frame is the one at the asked start"
    assert media.timecode(media.frame(path, 9.9)) == 48.2, "the last frame is the one just before the asked end"
    levels = [round(v) for v in engine.audio_levels(path)]
    # the burst runs from 40 s to 46 s, which is 1.7 s to 7.7 s of this segment
    assert levels[0] < -40 and all(v > -25 for v in levels[2:7]) and levels[8] < -40 and levels[9] < -40, levels
    info = media.info(path)
    assert "1280x720" in info and "Audio:" in info


def test_fetch_section_needs_no_ffmpeg_on_path(
    ytdlp: Any, http_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A computer with no ffmpeg of its own (most Windows PCs) must still be able to cut a part out of a video."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    out = tmp_path / "out"
    out.mkdir()
    path = engine.fetch_section(http_server.url("sample.mp4"), 10, 13, out / "seg")
    assert path == out / "seg.mp4" and folder_names(out) == ["seg.mp4"]
    assert engine.probe_duration(path) == pytest.approx(3.0, abs=0.15)


def test_open_ydl_puts_back_the_ffmpeg_setting_it_changed() -> None:
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    before = FFmpegPostProcessor._ffmpeg_location.get()
    with engine.open_ydl(engine.ydl_options(skip_download=True)):
        assert FFmpegPostProcessor._ffmpeg_location.get() == engine.ffmpeg_exe()
    assert FFmpegPostProcessor._ffmpeg_location.get() == before
    with pytest.raises(RuntimeError, match="boom"), engine.open_ydl(engine.ydl_options(skip_download=True)):
        raise RuntimeError("boom")
    assert FFmpegPostProcessor._ffmpeg_location.get() == before


def test_fetch_section_from_the_very_start(ytdlp: Any, http_server: Any, media: Any, tmp_path: Path) -> None:
    path = engine.fetch_section(http_server.url("sample.mp4"), 0, 6, tmp_path / "first.mp4")
    assert path == tmp_path / "first.mp4"
    assert engine.probe_duration(path) == pytest.approx(6.0, abs=0.15)
    assert media.timecode(media.frame(path, 0.0)) == 0.0 and media.timecode(media.frame(path, 5.9)) == 5.9
    negative = engine.fetch_section(http_server.url("sample.mp4"), -5, 2, tmp_path / "neg")
    assert engine.probe_duration(negative) == pytest.approx(2.0, abs=0.15)


def test_fetch_section_that_runs_past_the_end(ytdlp: Any, http_server: Any, media: Any, tmp_path: Path) -> None:
    path = engine.fetch_section(http_server.url("sample.mp4"), 84, 96, tmp_path / "tail")
    assert engine.probe_duration(path) == pytest.approx(6.0, abs=0.2)
    assert media.timecode(media.frame(path, 0.0)) == 84.0 and media.timecode(media.frame(path, 5.9)) == 89.9


def test_fetch_section_entirely_past_the_end_fails_cleanly(ytdlp: Any, http_server: Any, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="past the end"):
        engine.fetch_section(http_server.url("sample.mp4"), 100, 110, tmp_path / "seg")
    assert folder_names(tmp_path) == []


@pytest.mark.parametrize(("start", "end"), [(10, 10), (20, 10), (5, float("nan")), (-5, 0), (3, float("-inf"))])
def test_fetch_section_rejects_an_empty_range_without_going_online(
    ytdlp: Any, tmp_path: Path, start: float, end: float
) -> None:
    with pytest.raises(ValueError, match="must end after it starts"):
        engine.fetch_section("http://127.0.0.1:1/never-asked.mp4", start, end, tmp_path / "seg")


def test_fetch_section_from_an_unreachable_address(ytdlp: Any, tmp_path: Path) -> None:
    (tmp_path / "seg.ass").write_text("captions made earlier", encoding="utf-8")
    (tmp_path / "segment-notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ytdlp.utils.DownloadError) as caught:
        engine.fetch_section(f"http://127.0.0.1:{free_port()}/sample.mp4", 0, 6, tmp_path / "seg")
    assert (
        engine.friendly_error(caught.value)
        == "The video site could not be reached. Check your internet connection and try again."
    )
    assert folder_names(tmp_path) == ["seg.ass", "segment-notes.txt"], "only files of the failed download are removed"


def test_fetch_section_of_a_missing_file(ytdlp: Any, http_server: Any, tmp_path: Path) -> None:
    with pytest.raises(ytdlp.utils.DownloadError) as caught:
        engine.fetch_section(http_server.url("nope.mp4"), 0, 6, tmp_path / "seg")
    assert "404" in engine.friendly_error(caught.value)
    assert folder_names(tmp_path) == []


def test_fetch_section_leaves_nothing_behind_when_the_download_breaks(
    ytdlp: Any, http_server: Any, tmp_path: Path
) -> None:
    (tmp_path / "seg.mp4.part").write_bytes(b"left by an earlier crash")
    with pytest.raises(ytdlp.utils.DownloadError) as caught:
        engine.fetch_section(http_server.url("broken/sample.mp4"), 30, 36, tmp_path / "seg")
    assert "stopped before it finished" in engine.friendly_error(caught.value)
    assert folder_names(tmp_path) == []


def test_fetch_section_into_a_folder_with_template_characters(ytdlp: Any, http_server: Any, tmp_path: Path) -> None:
    folder = tmp_path / "50%(title)s done [x] 100%"
    folder.mkdir()
    path = engine.fetch_section(http_server.url("sample.mp4"), 10, 12, folder / "seg [1]")
    assert path == folder / "seg [1].mp4" and path.is_file()
    assert folder_names(tmp_path) == [folder.name], "yt-dlp must not expand %(title)s into another folder"


def test_fetched_section_renders_to_a_short(ytdlp: Any, http_server: Any, media: Any, tmp_path: Path) -> None:
    segment = engine.fetch_section(http_server.url("sample.mp4"), 68.0, 73.0, tmp_path / "seg")
    out = tmp_path / "short.mp4"
    engine.render_short(segment, 2.0, 2.0, out, layout="blur", zoom=1.0)
    assert_is_short(media, out, audio=True, seconds=2.0)
    assert media.timecode(media.frame(out, 0.0), BLUR_10) == 70.0
    assert media.timecode(media.frame(out, 1.95), BLUR_10) == 71.9


def test_download_audio(ytdlp: Any, http_server: Any, tmp_path: Path) -> None:
    path = engine.download_audio(http_server.url("sample.mp4"), tmp_path)
    assert path.parent == tmp_path and path.stem == "audio" and path.is_file()
    assert folder_names(tmp_path) == [path.name]
    levels = engine.audio_levels(path)
    assert 89 <= len(levels) <= 92 and levels[42] > levels[20] + 15


def test_download_audio_into_a_folder_with_template_characters(ytdlp: Any, http_server: Any, tmp_path: Path) -> None:
    folder = tmp_path / "work 100%(id)s [a]"
    folder.mkdir()
    path = engine.download_audio(http_server.url("sample.mp4"), folder)
    assert path.parent == folder and path.is_file() and folder_names(tmp_path) == [folder.name]


def test_download_audio_leaves_nothing_behind_when_the_download_breaks(
    ytdlp: Any, http_server: Any, tmp_path: Path
) -> None:
    (tmp_path / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ytdlp.utils.DownloadError) as caught:
        engine.download_audio(http_server.url("broken/sample.mp4"), tmp_path)
    assert "stopped before it finished" in engine.friendly_error(caught.value)
    assert folder_names(tmp_path) == ["notes.txt"], "the .part file of the broken download must be removed"


def test_download_audio_reports_how_much_has_arrived(ytdlp: Any, http_server: Any, tmp_path: Path) -> None:
    shares: list[float] = []
    path = engine.download_audio(http_server.url("sample.mp4"), tmp_path, progress=shares.append)
    assert path.is_file() and shares, "the progress function was never called"
    assert all(0.0 <= share <= 1.0 for share in shares) and shares[-1] == 1.0
    assert shares == sorted(shares), "the share never goes backwards for a file that arrives in one piece"


def test_download_audio_stops_when_the_progress_function_raises(ytdlp: Any, http_server: Any, tmp_path: Path) -> None:
    class Stop(Exception):
        pass

    def stop(share: float) -> None:
        raise Stop

    (tmp_path / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(Stop):
        engine.download_audio(http_server.url("sample.mp4"), tmp_path, progress=stop)
    assert folder_names(tmp_path) == ["notes.txt"], "nothing of the stopped download may be left behind"


def test_download_audio_fetches_several_pieces_of_a_stream_at_once(
    ytdlp: Any, http_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    real = ytdlp.YoutubeDL

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, params: dict[str, Any]) -> None:
            seen.append(params)
            super().__init__(params)

    monkeypatch.setattr(ytdlp, "YoutubeDL", Spy)
    engine.download_audio(http_server.url("sample.mp4"), tmp_path)
    assert seen[0]["concurrent_fragment_downloads"] == engine.AUDIO_PIECES_AT_ONCE >= 4


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"status": "downloading", "fragment_index": 728, "fragment_count": 7282}, 728 / 7282),
        ({"status": "downloading", "fragment_index": 9, "fragment_count": 3}, 1.0),
        ({"status": "downloading", "downloaded_bytes": 250, "total_bytes": 1000}, 0.25),
        ({"status": "downloading", "downloaded_bytes": 250, "total_bytes_estimate": 500.0}, 0.5),
        ({"status": "downloading", "downloaded_bytes": 250, "total_bytes": None}, None),
        ({"status": "downloading", "downloaded_bytes": "x", "total_bytes": 100}, 0.0),
        ({"status": "downloading"}, None),
        ({"status": "finished", "downloaded_bytes": 1, "total_bytes": 9}, 1.0),
        ("not a report", None),
        (None, None),
    ],
)
def test_download_share(status: Any, expected: float | None) -> None:
    assert engine._download_share(status) == expected


def test_download_preview_fetches_picture_and_sound(ytdlp: Any, http_server: Any, media: Any, tmp_path: Path) -> None:
    shares: list[float] = []
    path = engine.download_preview(http_server.url("sample.mp4"), tmp_path, progress=shares.append)
    assert path == tmp_path / "preview.mp4" and folder_names(tmp_path) == ["preview.mp4"]
    info = media.info(path)
    assert "Video:" in info and "Audio:" in info and shares[-1] == 1.0
    assert len(engine.motion_levels(path)) >= 50 and len(engine.audio_levels(path)) >= 50


def calm_busy_calm(media: Any, folder: Path, key_frames_every: int = 20) -> Path:
    """30 seconds of video: a still grey picture, then 10 seconds of a moving test picture, then grey again."""
    out = folder / f"calm-busy-calm-{key_frames_every}.mp4"
    still = "color=c=gray:s=320x180:r=10:d=10"
    media.run(
        *("-f", "lavfi", "-i", still, "-f", "lavfi", "-i", "testsrc2=s=320x180:r=10:d=10", "-f", "lavfi", "-i", still),
        *("-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100:duration=30"),
        *("-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-g", str(key_frames_every), "-keyint_min", str(key_frames_every)),
        *("-sc_threshold", "0", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k", "-shortest", "-y", str(out)),
    )
    return out


@pytest.mark.parametrize("key_frames_every", [20, 300])  # every 2 seconds; and only one in the whole file
def test_motion_levels_tell_a_moving_picture_from_a_still_one(
    media: Any, tmp_path: Path, key_frames_every: int
) -> None:
    levels = engine.motion_levels(calm_busy_calm(media, tmp_path, key_frames_every))
    assert 27 <= len(levels) <= 30
    assert max(levels[:8]) < 1 and max(levels[22:]) < 1, "the still parts measure as still"
    assert min(levels[12:19]) > 3, "the moving part measures as moving"
    # the cut from still to moving, and back, is one changed frame: it must not count as more movement than the rest
    assert max(levels) < 2 * statistics.median(levels[12:19])


def test_motion_levels_of_a_file_without_a_picture(media: Any, tmp_path: Path) -> None:
    sound = tmp_path / "sound.m4a"
    media.run("-f", "lavfi", "-i", "sine=frequency=300:duration=3", "-c:a", "aac", "-y", str(sound))
    assert engine.motion_levels(sound) == []


def test_motion_levels_of_a_file_that_is_not_media(tmp_path: Path) -> None:
    (tmp_path / "notes.mp4").write_text("not a video", encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not read the picture"):
        engine.motion_levels(tmp_path / "notes.mp4")


def test_motion_signal() -> None:
    assert engine.motion_signal([], 100) is None and engine.motion_signal([5.0] * 9, 100) is None
    assert engine.motion_signal([5.0] * 50, 0) is None
    levels = [2.0] * 100 + [30.0] * 20 + [2.0] * 100
    signal = engine.motion_signal(levels, 220)
    assert signal is not None and len(signal) == 220
    assert all(0.0 <= v <= 1.0 for v in signal)
    assert max(signal[:95]) == 0 and max(signal[125:]) == 0 and min(signal[103:117]) == 1.0
    # values that are no numbers, or lie outside 0..255, do no harm, and missing seconds count as ordinary
    odd = engine.motion_signal([2.0, "x", None, -5, 1e9, float("nan")] * 5, 60)
    assert odd is not None and len(odd) == 60 and all(0.0 <= v <= 1.0 for v in odd)


def test_action_focus_follows_the_movement_and_ignores_what_is_said() -> None:
    seconds = 600
    motion = [2.0] * seconds
    motion[400:430] = [40.0] * 30
    words = [{"t": 100 + i * 0.2, "d": 0.2, "w": "wow"} for i in range(150)]  # excited talk from 100 s to 130 s
    levels = [-30.0] * seconds
    talk = engine.find_moments(seconds, [], levels, words, length=30, count=1)
    action = engine.find_moments(seconds, [], levels, words, length=30, count=1, motion=motion, focus="action")
    assert 95 <= talk["moments"][0]["start"] <= 130 and "Excited or fast speech" in talk["moments"][0]["reasons"]
    assert 385 <= action["moments"][0]["start"] <= 415 and action["moments"][0]["end"] >= 425
    assert action["moments"][0]["reasons"] == ["Fast action on screen"]
    assert talk["signals"] == {"replays": False, "audio": True, "speech": True, "motion": False}
    assert action["signals"] == {"replays": False, "audio": True, "speech": True, "motion": True}


def test_action_focus_names_a_loud_part_as_sound_not_as_a_reaction() -> None:
    levels = [-40.0] * 300
    levels[200:215] = [-8.0] * 15
    found = engine.find_moments(300, [], levels, [], length=20, count=1, motion=[3.0] * 300, focus="action")
    assert found["moments"][0]["reasons"] == ["Loud sound"]
    assert engine.find_moments(300, [], levels, [], length=20, count=1)["moments"][0]["reasons"] == ["Loud reaction"]


def test_action_focus_keeps_only_the_moments_with_clear_action() -> None:
    seconds = 1200
    motion = [2.0] * seconds
    motion[300:340] = [40.0] * 40
    motion[900:935] = [35.0] * 35
    levels = [-40.0] * seconds
    levels[600:630] = [-5.0] * 30  # the loudest part of the video, with a still picture
    found = engine.find_moments(seconds, [], levels, [], length=30, count=5, motion=motion, focus="action")
    starts = sorted(m["start"] for m in found["moments"])
    assert len(starts) == 2 and 285 <= starts[0] <= 315 and 885 <= starts[1] <= 910, starts
    assert all("Fast action on screen" in m["reasons"] for m in found["moments"])
    assert found["note"] == "Only 2 moments with clear action on screen could be found in this video."
    # the same video searched for talk and reactions picks the loud part first and fills up to the number asked for
    talk = engine.find_moments(seconds, [], levels, [], length=30, count=5)
    assert len(talk["moments"]) == 5 and 590 <= talk["moments"][0]["start"] <= 615


def test_action_focus_says_so_when_only_one_moment_has_clear_action() -> None:
    motion = [2.0] * 600
    motion[200:240] = [40.0] * 40
    found = engine.find_moments(600, [], [-30.0] * 600, [], length=30, count=3, motion=motion, focus="action")
    assert len(found["moments"]) == 1
    assert found["note"] == "Only 1 moment with clear action on screen could be found in this video."


def test_action_focus_gives_the_number_asked_for_when_there_is_enough_action() -> None:
    motion = [2.0] * 3000
    for start in range(200, 3000, 400):
        motion[start : start + 40] = [40.0] * 40
    found = engine.find_moments(3000, [], [-30.0] * 3000, [], length=30, count=4, motion=motion, focus="action")
    assert len(found["moments"]) == 4 and found["note"] == ""
    assert_valid_result(found, 3000, 30, 4)


def test_action_focus_falls_back_to_sound_when_no_part_has_clear_action() -> None:
    levels = [-40.0] * 600
    levels[300:320] = [-6.0] * 20
    found = engine.find_moments(600, [], levels, [], length=30, count=2, motion=[3.0] * 600, focus="action")
    assert len(found["moments"]) == 2 and 285 <= found["moments"][0]["start"] <= 305
    assert "No part of this video had clearly more action on screen" in found["note"]
    assert all("Fast action on screen" not in m["reasons"] for m in found["moments"])


def test_action_focus_makes_up_no_title_from_what_is_said() -> None:
    motion = [2.0] * 600
    motion[200:240] = [40.0] * 40
    words = [{"t": 195 + i * 0.5, "d": 0.4, "w": f"word{i}"} for i in range(120)]
    action = engine.find_moments(600, [], [-30.0] * 600, words, length=30, count=1, motion=motion, focus="action")
    assert action["moments"][0]["title"] == "" and action["moments"][0]["text"] != ""
    talk = engine.find_moments(600, [], [-30.0] * 600, words, length=30, count=1)
    assert talk["moments"][0]["title"] != ""


def test_action_focus_with_nothing_to_measure() -> None:
    found = engine.find_moments(300, [], [], [{"t": 5.0, "d": 0.3, "w": "wow"}] * 40, focus="action")
    assert found["moments"] == [] and "no picture that could be read" in found["note"]


def test_an_unknown_focus_means_talk() -> None:
    levels = [-40.0] * 300
    levels[200:215] = [-8.0] * 15
    assert engine.find_moments(300, [], levels, [], focus="games") == engine.find_moments(300, [], levels, [])


def test_download_audio_from_an_unreachable_address(ytdlp: Any, tmp_path: Path) -> None:
    with pytest.raises(ytdlp.utils.DownloadError) as caught:
        engine.download_audio(f"http://127.0.0.1:{free_port()}/sample.mp4", tmp_path)
    assert "could not be reached" in engine.friendly_error(caught.value)
    assert folder_names(tmp_path) == []


def test_inspect_url_through_yt_dlp(ytdlp: Any, http_server: Any) -> None:
    source = engine.inspect_url(http_server.url("sample.mp4"))
    assert source["kind"] == "url" and source["url"] == http_server.url("sample.mp4")
    assert source["id"] == "sample" and source["title"] == "sample" and source["was_live"] is False
    assert isinstance(source["duration"], float) and source["heatmap"] == [] and source["words"] == []


def test_inspect_url_unreachable_gives_a_plain_message(ytdlp: Any) -> None:
    with pytest.raises(ytdlp.utils.DownloadError) as caught:
        engine.inspect_url(f"http://127.0.0.1:{free_port()}/nothing.mp4")
    assert (
        engine.friendly_error(caught.value)
        == "The video site could not be reached. Check your internet connection and try again."
    )


def test_captions_are_fetched_through_yt_dlp(ytdlp: Any, http_server: Any, media: Any) -> None:
    (media.served / "captions.json3").write_bytes(json3_bytes("नमस्ते दोस्तों", "welcome back"))
    info = {"subtitles": {"hi": [{"ext": "json3", "url": http_server.url("captions.json3")}]}}
    with ytdlp.YoutubeDL(engine.ydl_options()) as ydl:
        words, label = engine.fetch_captions(ydl, info)
    assert [w["w"] for w in words] == ["नमस्ते", "दोस्तों", "welcome", "back"] and label == "hi (uploaded)"


def test_no_test_reached_beyond_this_computer(
    local_network_only: list[object], ytdlp: Any, http_server: Any, tmp_path: Path
) -> None:
    engine.download_audio(http_server.url("sample.mp4"), tmp_path)
    assert local_network_only == []
    with pytest.raises(OSError, match="only 127.0.0.1 is allowed"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    assert local_network_only == [("192.0.2.1", 80)]

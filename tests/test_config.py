"""Settings and the rules for video links."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from clipstudio import config
from clipstudio.config import Settings, UrlError, load_settings, validate_video_url

SETTINGS = Settings()


# ---------------------------------------------------------------- settings


def test_defaults_when_nothing_is_set():
    settings = load_settings({})
    assert settings.host == "127.0.0.1"
    assert settings.port == 5180
    assert settings.allowed_hosts == ("youtube.com", "youtu.be")
    assert settings.max_upload_bytes == 4096 * 1024 * 1024
    assert settings.keep_hours == 72
    assert settings.keep_seconds == 72 * 3600
    assert settings.data_dir == config.PROJECT_ROOT / "data"
    assert settings.work_dir == settings.data_dir / "work"
    assert settings.uploads_dir == settings.data_dir / "uploads"
    assert settings.output_dir == settings.data_dir / "output"


def test_blank_variables_use_the_defaults():
    blank = {name: "  " for name in ("CLIPSTUDIO_PORT", "CLIPSTUDIO_HOST", "CLIPSTUDIO_ALLOWED_HOSTS")}
    blank.update(CLIPSTUDIO_MAX_UPLOAD_MB="", CLIPSTUDIO_DATA_DIR="", CLIPSTUDIO_KEEP_HOURS="")
    assert load_settings(blank) == load_settings({})


def test_reads_the_real_environment_by_default(monkeypatch):
    monkeypatch.setenv("CLIPSTUDIO_PORT", "6001")
    assert load_settings().port == 6001


def test_every_variable_is_read(tmp_path):
    settings = load_settings(
        {
            "CLIPSTUDIO_PORT": " 6200 ",
            "CLIPSTUDIO_HOST": "LOCALHOST",
            "CLIPSTUDIO_ALLOWED_HOSTS": "Example.org, www.youtube.com ,youtu.be,example.org,",
            "CLIPSTUDIO_MAX_UPLOAD_MB": "10",
            "CLIPSTUDIO_DATA_DIR": str(tmp_path / "clips"),
            "CLIPSTUDIO_KEEP_HOURS": "1.5",
        }
    )
    assert settings.port == 6200
    assert settings.host == "localhost"
    assert settings.allowed_hosts == ("example.org", "youtube.com", "youtu.be")
    assert settings.max_upload_bytes == 10 * 1024 * 1024
    assert settings.data_dir == tmp_path / "clips"
    assert settings.keep_hours == 1.5


def test_relative_data_dir_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_settings({"CLIPSTUDIO_DATA_DIR": "my-clips"}).data_dir == tmp_path / "my-clips"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CLIPSTUDIO_PORT", "abc"),
        ("CLIPSTUDIO_PORT", "0"),
        ("CLIPSTUDIO_PORT", "-1"),
        ("CLIPSTUDIO_PORT", "65536"),
        ("CLIPSTUDIO_PORT", "80.5"),
        ("CLIPSTUDIO_HOST", "example.com"),
        ("CLIPSTUDIO_HOST", "127.0.0.1:80"),
        ("CLIPSTUDIO_HOST", "http://127.0.0.1"),
        ("CLIPSTUDIO_HOST", "999.1.1.1"),
        ("CLIPSTUDIO_ALLOWED_HOSTS", ","),
        ("CLIPSTUDIO_ALLOWED_HOSTS", "https://youtube.com"),
        ("CLIPSTUDIO_ALLOWED_HOSTS", "youtube.com/watch"),
        ("CLIPSTUDIO_ALLOWED_HOSTS", "you tube.com"),
        ("CLIPSTUDIO_ALLOWED_HOSTS", "*.youtube.com"),
        ("CLIPSTUDIO_ALLOWED_HOSTS", "localhost"),
        ("CLIPSTUDIO_ALLOWED_HOSTS", "user@youtube.com"),
        ("CLIPSTUDIO_ALLOWED_HOSTS", ",".join(f"site{n}.example" for n in range(30))),
        ("CLIPSTUDIO_MAX_UPLOAD_MB", "0"),
        ("CLIPSTUDIO_MAX_UPLOAD_MB", "-5"),
        ("CLIPSTUDIO_MAX_UPLOAD_MB", "lots"),
        ("CLIPSTUDIO_MAX_UPLOAD_MB", "1.5"),
        ("CLIPSTUDIO_MAX_UPLOAD_MB", "99999999999"),
        ("CLIPSTUDIO_KEEP_HOURS", "0"),
        ("CLIPSTUDIO_KEEP_HOURS", "-3"),
        ("CLIPSTUDIO_KEEP_HOURS", "nan"),
        ("CLIPSTUDIO_KEEP_HOURS", "inf"),
        ("CLIPSTUDIO_KEEP_HOURS", "forever"),
        ("CLIPSTUDIO_KEEP_HOURS", "100000"),
    ],
)
def test_bad_values_are_refused_with_the_variable_named(name, value):
    with pytest.raises(ValueError) as caught:
        load_settings({name: value})
    assert name in str(caught.value)


def test_data_dir_cannot_be_a_file_or_the_top_of_a_drive(tmp_path):
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x")
    with pytest.raises(ValueError, match="CLIPSTUDIO_DATA_DIR"):
        load_settings({"CLIPSTUDIO_DATA_DIR": str(a_file)})
    with pytest.raises(ValueError, match="CLIPSTUDIO_DATA_DIR"):
        load_settings({"CLIPSTUDIO_DATA_DIR": Path(tmp_path.anchor).as_posix()})


def test_settings_cannot_be_changed_after_creation():
    with pytest.raises(dataclasses.FrozenInstanceError):
        SETTINGS.port = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "example.com"},
        {"host": ""},
        {"port": -1},
        {"port": 70000},
        {"port": "5180"},
        {"port": True},
        {"allowed_hosts": ()},
        {"allowed_hosts": ("not a host",)},
        {"allowed_hosts": ("https://youtube.com",)},
        {"max_upload_bytes": 0},
        {"max_upload_bytes": 1.5},
        {"keep_hours": 0},
        {"keep_hours": float("nan")},
        {"keep_hours": "72"},
    ],
)
def test_settings_made_directly_are_checked_too(changes):
    with pytest.raises(ValueError):
        Settings(**changes)


def test_port_zero_means_any_free_port_and_lists_become_tuples(tmp_path):
    settings = Settings(port=0, allowed_hosts=["youtube.com"], data_dir=str(tmp_path))  # type: ignore[arg-type]
    assert settings.port == 0
    assert settings.allowed_hosts == ("youtube.com",)
    assert settings.data_dir == tmp_path


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("192.168.1.20", False),
        ("x", False),
    ],
)
def test_is_loopback_host(host, loopback):
    assert config.is_loopback_host(host) is loopback


def test_parse_host_takes_ipv6_in_brackets():
    assert config.parse_host("[::1]") == "::1"


# ------------------------------------------------------------- video links


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "https://youtu.be/dQw4w9WgXcQ?t=42"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "https://m.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "https://music.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/abcDEF12345?si=xyz", "https://www.youtube.com/live/abcDEF12345?si=xyz"),
        ("http://www.youtube.com/watch?v=abc", "http://www.youtube.com/watch?v=abc"),
        # tidied: spaces around it, capitals in the host, a trailing dot, the default port, a #fragment
        ("  https://WWW.YouTube.com/watch?v=abc  ", "https://www.youtube.com/watch?v=abc"),
        ("HTTPS://youtube.com./watch?v=abc", "https://youtube.com/watch?v=abc"),
        ("https://youtube.com:443/watch?v=abc", "https://youtube.com/watch?v=abc"),
        ("https://www.youtube.com/watch?v=abc&t=10s#comments", "https://www.youtube.com/watch?v=abc&t=10s"),
        ("https://youtube.com", "https://youtube.com/"),
        # pasted without the https:// part
        ("youtu.be/dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ"),
        ("www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
    ],
)
def test_good_links_pass_and_are_tidied(link, expected):
    assert validate_video_url(link, SETTINGS) == expected


def test_letters_outside_ascii_in_the_path_are_encoded():
    assert validate_video_url("https://youtube.com/results?search_query=नमस्ते", SETTINGS) == (
        "https://youtube.com/results?search_query=%E0%A4%A8%E0%A4%AE%E0%A4%B8%E0%A5%8D%E0%A4%A4%E0%A5%87"
    )


@pytest.mark.parametrize(
    ("link", "code"),
    [
        ("https://example.com/watch?v=abc", "host_not_allowed"),
        ("https://vimeo.com/123", "host_not_allowed"),
        ("https://evil.youtube.com/watch?v=abc", "host_not_allowed"),
        ("https://youtube.com.evil.com/watch?v=abc", "host_not_allowed"),
        ("https://notyoutube.com/watch?v=abc", "host_not_allowed"),
        ("https://evil.com/?https://youtube.com/watch", "host_not_allowed"),
        ("https://evil.com/#@youtube.com", "host_not_allowed"),
        ("https://127.0.0.1/watch", "host_not_allowed"),
        ("https://localhost/watch", "host_not_allowed"),
        ("https://[::1]/watch", "host_not_allowed"),
        ("https://y\u043eutube.com/watch?v=abc", "host_not_allowed"),  # the first "o" is a Cyrillic letter
        ("example.com/watch", "host_not_allowed"),
        ("file:///etc/passwd", "invalid_url"),
        ("file://youtube.com/etc/passwd", "invalid_url"),
        ("ftp://youtube.com/video.mp4", "invalid_url"),
        ("javascript:alert(1)", "invalid_url"),
        ("data:text/html,hello", "invalid_url"),
        ("https://user:secret@youtube.com/watch?v=abc", "invalid_url"),
        ("https://user@youtube.com/watch?v=abc", "invalid_url"),
        ("https://youtube.com@evil.com/watch", "invalid_url"),
        ("https://@youtube.com/watch", "invalid_url"),
        ("https://youtube.com:8443/watch?v=abc", "invalid_url"),
        ("https://youtube.com:notaport/watch", "invalid_url"),
        ("https://youtube.com\\@evil.com/watch", "invalid_url"),
        ("https://youtube.com/watch?v=a b", "invalid_url"),
        ("https://youtube.com/watch\n?v=abc", "invalid_url"),
        ("https://youtube.com/\x00", "invalid_url"),
        ("https://", "invalid_url"),
        ("https:///watch?v=abc", "invalid_url"),
        ("https://[youtube.com/watch", "invalid_url"),
        ("", "invalid_url"),
        ("   ", "invalid_url"),
        ("https://youtube.com/watch?v=" + "a" * 3000, "url_too_long"),
    ],
)
def test_bad_links_are_refused(link, code):
    with pytest.raises(UrlError) as caught:
        validate_video_url(link, SETTINGS)
    assert caught.value.code == code
    message = str(caught.value)
    assert message and message[0].isupper() and message.endswith(".")
    assert "Traceback" not in message


@pytest.mark.parametrize("link", [None, 42, ["https://youtube.com/watch"], {"url": "x"}, b"https://youtu.be/x"])
def test_links_that_are_not_text_are_refused(link):
    with pytest.raises(ValueError):
        validate_video_url(link, SETTINGS)


def test_url_error_is_a_value_error_with_a_plain_message():
    with pytest.raises(ValueError, match="Only links from youtube.com or youtu.be work here"):
        validate_video_url("https://example.com/x", SETTINGS)


def test_longest_allowed_link_still_passes():
    link = "https://youtube.com/watch?v=" + "a" * (config.MAX_URL_LENGTH - len("https://youtube.com/watch?v="))
    assert len(link) == config.MAX_URL_LENGTH
    assert validate_video_url(link, SETTINGS) == link


def test_allowed_hosts_can_be_changed():
    settings = Settings(allowed_hosts=("example.org",))
    assert validate_video_url("https://www.example.org/v/1", settings) == "https://www.example.org/v/1"
    assert validate_video_url("https://m.example.org/v/1", settings) == "https://m.example.org/v/1"
    with pytest.raises(UrlError, match="Only links from example.org work here"):
        validate_video_url("https://www.youtube.com/watch?v=abc", settings)
    with pytest.raises(UrlError):
        validate_video_url("https://cdn.example.org/v/1", settings)


def test_host_allowed_rules():
    hosts = ("youtube.com", "youtu.be")
    for good in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "YOUTUBE.COM."):
        assert config.host_allowed(good, hosts), good
    for bad in ("gaming.youtube.com", "youtube.com.evil.com", "evilyoutube.com", "be", "", "www.www.youtube.com"):
        assert not config.host_allowed(bad, hosts), bad

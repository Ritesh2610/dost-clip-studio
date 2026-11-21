"""Settings for Clip Studio, read from environment variables and checked before use.

Every setting has a safe default, so the app starts with no configuration at all.
A value that is set but wrong stops the app with a plain message instead of being
silently ignored.

    CLIPSTUDIO_PORT            port to listen on (default 5180)
    CLIPSTUDIO_HOST            address to listen on (default 127.0.0.1)
    CLIPSTUDIO_ALLOWED_HOSTS   comma-separated video sites (default youtube.com,youtu.be)
    CLIPSTUDIO_MAX_UPLOAD_MB   largest video file that can be added, in MB (default 4096)
    CLIPSTUDIO_DATA_DIR        folder for uploads, work files and finished clips (default <project>/data)
    CLIPSTUDIO_KEEP_HOURS      hours to keep uploads, work files and clips (default 72)
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PORT = 5180
DEFAULT_HOST = "127.0.0.1"
DEFAULT_ALLOWED_HOSTS = ("youtube.com", "youtu.be")
DEFAULT_MAX_UPLOAD_MB = 4096
DEFAULT_KEEP_HOURS = 72.0

# A link may use an allowed host itself or one of these well-known subdomains of it.
SUBDOMAIN_PREFIXES = ("www", "m", "music")
MAX_URL_LENGTH = 2048
MAX_ALLOWED_HOSTS = 20

MIN_CLIP_SECONDS = 8
MAX_CLIP_SECONDS = 90
MAX_CLIPS = 10

_HOST_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_LOOPBACK_NAMES = ("localhost",)


class UrlError(ValueError):
    """A video link that cannot be used. The message is written for the person using the app."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Settings:
    """Checked settings. Creating one with a bad value raises ValueError."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_MB * 1024 * 1024
    data_dir: Path = PROJECT_ROOT / "data"
    keep_hours: float = DEFAULT_KEEP_HOURS

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not _valid_bind_host(self.host):
            raise ValueError("The host must be localhost or an IP address, for example 127.0.0.1.")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 0 <= self.port <= 65535:
            raise ValueError("The port must be a whole number between 1 and 65535.")
        hosts = tuple(self.allowed_hosts)
        if not hosts or len(hosts) > MAX_ALLOWED_HOSTS:
            raise ValueError(f"The list of allowed video sites needs between 1 and {MAX_ALLOWED_HOSTS} names.")
        for name in hosts:
            if not isinstance(name, str) or not _HOST_NAME.match(name):
                raise ValueError(
                    f"'{name}' is not a site name. Use names like youtube.com, without https:// or a path."
                )
        object.__setattr__(self, "allowed_hosts", hosts)
        if (
            isinstance(self.max_upload_bytes, bool)
            or not isinstance(self.max_upload_bytes, int)
            or self.max_upload_bytes < 1
        ):
            raise ValueError("The upload limit must be a whole number greater than zero.")
        if isinstance(self.keep_hours, bool) or not isinstance(self.keep_hours, (int, float)):
            raise ValueError("The number of hours to keep files must be a number.")
        if not math.isfinite(self.keep_hours) or self.keep_hours <= 0:
            raise ValueError("The number of hours to keep files must be greater than zero.")
        object.__setattr__(self, "data_dir", Path(self.data_dir))

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def keep_seconds(self) -> float:
        return self.keep_hours * 3600


def _valid_bind_host(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_loopback_host(host: str) -> bool:
    """True when only programs on this computer can reach the address."""
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_port(text: str, name: str = "CLIPSTUDIO_PORT") -> int:
    try:
        port = int(text.strip())
    except ValueError:
        raise ValueError(f"{name} must be a whole number between 1 and 65535, not '{text}'.") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535, not {port}.")
    return port


def parse_host(text: str, name: str = "CLIPSTUDIO_HOST") -> str:
    host = text.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not _valid_bind_host(host):
        raise ValueError(f"{name} must be localhost or an IP address such as 127.0.0.1, not '{text}'.")
    return host


def parse_allowed_hosts(text: str) -> tuple[str, ...]:
    hosts: list[str] = []
    for part in text.split(","):
        name = part.strip().lower().rstrip(".")
        if not name:
            continue
        if not _HOST_NAME.match(name):
            raise ValueError(
                f"CLIPSTUDIO_ALLOWED_HOSTS has an entry that is not a site name: '{part.strip()}'. "
                "Use names like youtube.com, separated by commas, without https:// or a path."
            )
        for prefix in SUBDOMAIN_PREFIXES:  # www.youtube.com means the same as youtube.com here
            if name.startswith(prefix + ".") and _HOST_NAME.match(name[len(prefix) + 1 :]):
                name = name[len(prefix) + 1 :]
                break
        if name not in hosts:
            hosts.append(name)
    if not hosts:
        raise ValueError("CLIPSTUDIO_ALLOWED_HOSTS is empty. List at least one site, for example youtube.com.")
    if len(hosts) > MAX_ALLOWED_HOSTS:
        raise ValueError(f"CLIPSTUDIO_ALLOWED_HOSTS lists more than {MAX_ALLOWED_HOSTS} sites.")
    return tuple(hosts)


def _parse_upload_mb(text: str) -> int:
    try:
        megabytes = int(text.strip())
    except ValueError:
        raise ValueError(f"CLIPSTUDIO_MAX_UPLOAD_MB must be a whole number of megabytes, not '{text}'.") from None
    if not 1 <= megabytes <= 1024 * 1024:
        raise ValueError(f"CLIPSTUDIO_MAX_UPLOAD_MB must be between 1 and {1024 * 1024}, not {megabytes}.")
    return megabytes * 1024 * 1024


def _parse_keep_hours(text: str) -> float:
    try:
        hours = float(text.strip())
    except ValueError:
        raise ValueError(f"CLIPSTUDIO_KEEP_HOURS must be a number of hours, not '{text}'.") from None
    if not math.isfinite(hours) or not 1 <= hours <= 8760:
        raise ValueError(f"CLIPSTUDIO_KEEP_HOURS must be between 1 and 8760, not '{text}'.")
    return hours


def _parse_data_dir(text: str) -> Path:
    if "\x00" in text:
        raise ValueError("CLIPSTUDIO_DATA_DIR is not a usable folder path.")
    path = Path(text.strip()).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(path))
    if path.exists() and not path.is_dir():
        raise ValueError(f"CLIPSTUDIO_DATA_DIR points at a file, not a folder: {path}")
    if path == Path(path.anchor):
        raise ValueError("CLIPSTUDIO_DATA_DIR cannot be the top of a drive. Choose a folder made for this app.")
    return path


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Builds Settings from environment variables. Unset or blank variables use the defaults."""
    source = os.environ if env is None else env

    def value(name: str) -> str | None:
        raw = source.get(name)
        return raw if raw is not None and raw.strip() else None

    port, host = value("CLIPSTUDIO_PORT"), value("CLIPSTUDIO_HOST")
    hosts, upload_mb = value("CLIPSTUDIO_ALLOWED_HOSTS"), value("CLIPSTUDIO_MAX_UPLOAD_MB")
    data_dir, keep_hours = value("CLIPSTUDIO_DATA_DIR"), value("CLIPSTUDIO_KEEP_HOURS")
    return Settings(
        host=parse_host(host) if host else DEFAULT_HOST,
        port=parse_port(port) if port else DEFAULT_PORT,
        allowed_hosts=parse_allowed_hosts(hosts) if hosts else DEFAULT_ALLOWED_HOSTS,
        max_upload_bytes=_parse_upload_mb(upload_mb) if upload_mb else DEFAULT_MAX_UPLOAD_MB * 1024 * 1024,
        data_dir=_parse_data_dir(data_dir) if data_dir else PROJECT_ROOT / "data",
        keep_hours=_parse_keep_hours(keep_hours) if keep_hours else DEFAULT_KEEP_HOURS,
    )


def host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """True for an allowed site or its www, m or music subdomain. Other subdomains are refused."""
    host = host.lower().rstrip(".")
    for allowed in allowed_hosts:
        if host == allowed or any(host == f"{prefix}.{allowed}" for prefix in SUBDOMAIN_PREFIXES):
            return True
    return False


def _site_names(allowed_hosts: tuple[str, ...]) -> str:
    names = list(allowed_hosts)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def validate_video_url(url: object, settings: Settings) -> str:
    """Checks a pasted video link and returns it in a tidy form.

    Only http and https links to an allowed site pass. Links with a user name or
    password, a port number, odd characters or an absurd length are refused.
    Raises ValueError (UrlError) with a message that can be shown as it is.
    """
    if not isinstance(url, str):
        raise UrlError("invalid_url", "Paste the link of a video first.")
    text = url.strip()
    if not text:
        raise UrlError("invalid_url", "Paste the link of a video first.")
    if len(text) > MAX_URL_LENGTH:
        raise UrlError("url_too_long", "That link is too long to be a video link. Copy it again from the address bar.")
    if "\\" in text or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise UrlError(
            "invalid_url", "That link has spaces or characters a link cannot have. Copy it again from the address bar."
        )
    if "://" not in text and not _SCHEME.match(text):
        text = "https://" + text  # "youtu.be/abc" pasted without the https:// part
    try:
        parts = urlsplit(text)
        port = parts.port
        user, password, host = parts.username, parts.password, (parts.hostname or "")
    except ValueError:
        raise UrlError(
            "invalid_url", "That does not look like a video link. Copy it again from the address bar."
        ) from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlError("invalid_url", "Only web links work here. Paste a link that starts with https://.")
    if user is not None or password is not None or "@" in parts.netloc:
        raise UrlError(
            "invalid_url", "Links that carry a user name or password are not accepted. Paste the plain video link."
        )
    host = host.lower().rstrip(".")
    if not host:
        raise UrlError("invalid_url", "That does not look like a video link. Copy it again from the address bar.")
    names = _site_names(settings.allowed_hosts)
    if not host.isascii() or not host_allowed(host, settings.allowed_hosts):
        raise UrlError("host_not_allowed", f"Only links from {names} work here. Paste a link from {names}.")
    if port is not None and port != (443 if scheme == "https" else 80):
        raise UrlError("invalid_url", "Links with a port number are not accepted. Paste the plain video link.")
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/%:@!$&'()*+,;=-._~?")
    return urlunsplit((scheme, host, path, query, ""))

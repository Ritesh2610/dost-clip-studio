"""Starts Clip Studio: `python -m clipstudio`.

python -m clipstudio                 start the app and open it in the browser
python -m clipstudio --no-open       start without opening the browser
python -m clipstudio --port 5200     use another port (the next free one is used if it is taken)
"""

from __future__ import annotations

import argparse
import dataclasses
import platform
import sys
import threading
import traceback
import webbrowser
from collections.abc import Sequence

MIN_PYTHON = (3, 11)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m clipstudio",
        description="Dost Clip Studio: find the strongest moments in a video and cut vertical shorts.",
    )
    parser.add_argument(
        "--port", default=None, help="port to listen on (default 5180; the next free port is used if it is taken)"
    )
    parser.add_argument("--host", default=None, help="address to listen on (default 127.0.0.1, this computer only)")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    parser.add_argument("--verbose", action="store_true", help="also log every request")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        print(
            f"Clip Studio needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer. This is Python {platform.python_version()}. "
            "Install a newer one from https://www.python.org/downloads/ and run this again."
        )
        return 1

    from . import __version__, config, engine
    from . import server as web

    log = web.AppLog(web.LOG_FILE, verbose=args.verbose)
    log.reset()
    server = None
    app = None
    timer = None
    try:
        log(f"Starting Dost Clip Studio {__version__}. Python {platform.python_version()} on {platform.platform()}")
        log(f"Interpreter: {sys.executable}")

        settings = config.load_settings()
        if args.port is not None:
            settings = dataclasses.replace(settings, port=config.parse_port(str(args.port), "--port"))
        if args.host is not None:
            settings = dataclasses.replace(settings, host=config.parse_host(str(args.host), "--host"))
        if not config.is_loopback_host(settings.host):
            log(
                f"Warning: listening on {settings.host}, which other devices on your network may be able to reach. "
                "The app still answers only requests that name this computer. Use 127.0.0.1 unless you need otherwise."
            )

        engine.LOG = log  # type: ignore[assignment]
        app = web.App(settings, log=log, verbose=args.verbose)
        log("Checking the video tools...")
        report_tools(app.tools(), log)

        server = web.open_server(app)
        url = web.server_url(server)
        log(f"Dost Clip Studio is running at {url}")
        log(f"Finished clips are saved in {settings.output_dir}")
        log("Keep this window open while you use the app. Press Ctrl+C to stop.")
        if not args.no_open:
            timer = threading.Timer(0.6, open_browser, args=(url, log))
            timer.daemon = True
            timer.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log("Stopped.")
        return 0
    except KeyboardInterrupt:
        log("Stopped.")
        return 0
    except ValueError as error:  # a setting with a bad value: the message says which one
        log(traceback.format_exc().rstrip())
        log(f"Dost Clip Studio could not start: {error}")
        return 2
    except Exception:
        log("Dost Clip Studio could not start:\n" + traceback.format_exc().rstrip())
        return 1
    finally:
        if timer is not None:
            timer.cancel()
        if server is not None:
            server.server_close()
        if app is not None:
            app.close()


def report_tools(tools: dict, log) -> None:
    """Says which tools were found, with a plain warning for each one that is missing."""
    found = [
        f"{label} {tools[key]}"
        for key, label in (("ytdlp", "yt-dlp"), ("ffmpeg", "ffmpeg"), ("deno", "Deno"))
        if tools.get(key)
    ]
    log("Video tools found: " + (", ".join(found) if found else "none"))
    fix = "Close this window and run start.bat (start.sh on Mac or Linux) again so it can be installed."
    if not tools.get("ytdlp"):
        log(f"Warning: yt-dlp is missing, so video links cannot be opened. Video files still work. {fix}")
    if not tools.get("ffmpeg"):
        log(f"Warning: ffmpeg is missing, so videos cannot be read or cut. {fix}")
    if not tools.get("deno"):
        log(f"Warning: Deno is missing, so YouTube may refuse some links. {fix}")
    if tools.get("ffmpeg") and not tools.get("captions_filter"):
        log("Warning: this ffmpeg cannot draw captions, so clips will be made without captions.")


def open_browser(url: str, log) -> None:
    try:
        webbrowser.open(url)
    except Exception as error:  # no browser is not a reason to stop: the address is printed above
        log(f"The browser could not be opened ({error}). Open {url} yourself.")


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:  # show any failure instead of vanishing
        print("Dost Clip Studio could not start:\n" + traceback.format_exc(), flush=True)
        code = 1
    sys.exit(code)

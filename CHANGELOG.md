# Changelog

All notable changes to this project are recorded in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A choice of what to look for: "Talk and reactions", as before, or "Game action on screen", which goes by movement in the picture, loudness and replay data, and not by what is said.

### Changed

- The captions switch on the review screen now reads "Add captions to the video".

- Analysing a long stream is much faster. Its sound arrives as thousands of small pieces, and the app now fetches eight of them at a time.
- The app and the log show how far the sound download is, and Cancel stops it while it runs.

### Fixed

- Cutting a part out of a YouTube video no longer fails with "ffmpeg is not installed" on a computer that has no ffmpeg of its own. The ffmpeg that is installed with the app is now used for this step too.
- A request the server cannot read is always answered with a status line and the safety headers, on every supported Python version. Requests in the header-less HTTP/0.9 form are refused.
- The post text saved next to each clip has the same line endings on Windows as on other systems.
- A video file whose name starts with several dots is accepted on Python 3.14 as it is on older versions.

## [1.0.0] - 2026-11-21

First public release.

### Added

- Analyse a YouTube video or a finished YouTube stream from its link, or a video file from your computer (`.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v`, `.avi`).
- Find the strongest moments from up to three signals: YouTube replay data, audio loudness and captions. The weights are 0.5, 0.3 and 0.2, shared out again over the signals that are available for the video.
- Each suggested moment has a score from 1 to 100 and the reasons it was picked.
- The strongest second of a moment is placed in the first 40 percent of the clip, and cut points move to pauses in speech when captions are available.
- A review step where you change the start, end and title of each clip before rendering. Clips are 8 to 90 seconds long, and you can render up to 10 at a time.
- Render vertical 1080x1920 shorts at 30 frames per second (H.264 video, AAC audio) with a blurred background or a fill crop, adjustable zoom and a left, centre or right crop position.
- Burned-in captions for YouTube videos that have captions, uploaded or automatic, with the spoken word highlighted. Devanagari and Latin scripts are both drawn with a suitable system font.
- An optional title at the top of each clip.
- Only the parts of a YouTube video that you chose are downloaded for rendering. For analysis the app downloads the smallest audio track.
- Suggested post text for each clip, with a credit line that names the source channel.
- A required permission checkbox, which the server also checks on every analyse request.
- Background jobs with progress messages in plain words and a cancel action. One analysis and one render run at a time, and others wait in a queue.
- A local web server built on the Python standard library. It listens on 127.0.0.1 by default, checks the `Host` and `Origin` headers, accepts links only from an allow-list of hosts, limits upload and request sizes, keeps served files inside their folders and sends a Content-Security-Policy with every response.
- Settings through `CLIPSTUDIO_*` environment variables, and `--port`, `--host`, `--no-open` and `--verbose` options on the command line. If the port is taken, the app uses the next free one.
- Uploads, work files and finished clips are removed after 72 hours by default.
- A log file, `clip-log.txt`, for troubleshooting.
- Start scripts for Windows (`start.bat`) and for macOS and Linux (`start.sh`).
- Tests that run without the network, and continuous integration on Ubuntu and Windows with Python 3.11, 3.13 and 3.14.

[Unreleased]: https://github.com/Ritesh2610/dost-clip-studio/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Ritesh2610/dost-clip-studio/releases/tag/v1.0.0

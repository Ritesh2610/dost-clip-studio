# Dost Clip Studio

[![CI](https://github.com/Ritesh2610/dost-clip-studio/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Ritesh2610/dost-clip-studio/actions/workflows/ci.yml)

Dost Clip Studio finds the strongest moments in a long YouTube video, a finished stream or a video file, and turns the ones you choose into vertical 1080x1920 shorts with captions. It runs on your own computer, for videos you made or have permission to clip.

## What it does and does not do

It does:

- Find the strongest moments of a video from three signals: YouTube's replay data, audio loudness and captions.
- Show you each moment with a score and the reasons for it, so you can review it and adjust the start, the end and the title.
- Render vertical shorts with burned-in captions, a blurred background or a fill crop, and an optional title.
- Run locally. The work happens on your computer, and your video files stay on it.
- Suggest post text for each clip, with a credit line for the source channel.

It does not:

- Upload anything. The only site the app contacts is the video site, to read and download the video you asked for. The page in your browser also loads two typefaces from Google Fonts and the video's thumbnail from YouTube. There is no telemetry.
- Post for you. There is no posting, scheduling or bulk clipping. You download the clips and post them yourself.
- Make a clip popular. It finds moments that are more likely to hold attention. What happens after you post is not something a tool can decide.
- Check that you have permission. You confirm that yourself before each analysis. See [Use it responsibly](#use-it-responsibly).

## Requirements

- Windows 10 or 11, macOS, or Linux.
- Python 3.11 or newer. The tests run on Python 3.11, 3.13 and 3.14, on Ubuntu and Windows.
- A current browser, such as Chrome, Edge, Firefox or Safari.
- An internet connection for the first start, which installs the tools, and for YouTube links. Video files from your computer work offline after that.
- Free disk space for uploads and rendered clips.

You do not need to install ffmpeg, Deno or yt-dlp yourself. They are installed as Python packages on first start.

On Linux, captions use the fonts DejaVu Sans and, for Hindi, Noto Sans Devanagari. Install them if your system does not have them.

## Quick start

Get the code first: download the repository as a ZIP file from GitHub and unpack it, or clone it.

```
git clone https://github.com/Ritesh2610/dost-clip-studio.git
cd dost-clip-studio
```

### Windows

Double-click `start.bat`, or run it from a terminal in the project folder:

```
start.bat
```

### macOS and Linux

```
./start.sh
```

The start script installs what the app needs and starts it. The app then opens in your browser. If it does not, open the address shown in the terminal, which is normally <http://127.0.0.1:5180>. Keep the terminal window open while you use the app, and press Ctrl+C in it to stop.

### Manual start

If you prefer to run the steps yourself:

```
python -m venv .venv
```

Activate the environment with `.venv\Scripts\activate` on Windows or `source .venv/bin/activate` on macOS and Linux. Then:

```
python -m pip install -r requirements.txt
python -m clipstudio
```

Options for `python -m clipstudio`:

| Option | Meaning |
| --- | --- |
| `--no-open` | Start without opening the browser. |
| `--port 5200` | Listen on another port. |
| `--host 127.0.0.1` | Listen on another address. |
| `--verbose` | Also log every request. |

### Using the app

1. Paste the link of a YouTube video or finished stream, or choose a video file.
2. Choose the clip length and how many clips to look for.
3. Tick "I made this video, or I have permission from its owner to clip it." and start the search.
4. Review the moments. Change times and titles, leave out the ones you do not want and choose how the shorts should look.
5. Make the shorts, play them in the page and download the ones you want.

The finished files are also in `data/output/`, one folder for each render: `clip-01.mp4`, `clip-02.mp4` and so on, each with a `.txt` file that holds the suggested post text. The app removes them after 72 hours unless you change that, so copy the clips you want to keep.

## How the strongest moments are found

The app gives every second of the video a value between 0 and 1 from up to three signals.

| Signal | Weight | Where it comes from |
| --- | --- | --- |
| Replays | 0.5 | YouTube's "most replayed" graph, which shows the parts viewers went back to. The opening of a video is turned down, because every video's graph starts high. |
| Audio | 0.3 | Loudness measured by ffmpeg for each second. A second scores high when it is louder than the rest of the video, or much louder than the 20 seconds before it. |
| Speech | 0.2 | The video's captions. Reaction words, exclamations and fast speech raise the value. Captions that mark music lower it. |

Then:

1. **The weights are shared over the signals that exist.** Not every video has all three. If a video has no replay data, audio and speech are scaled up to 0.6 and 0.4. If only audio is available, it counts for everything. The app tells you which signals it used.
2. **Every possible clip is scored.** For each start time, the score is 60 percent the average value across the clip and 40 percent its highest second.
3. **The best clips are picked so that they do not overlap.**
4. **The strongest second is pulled forward.** If the peak falls later than the first 40 percent of the clip, the clip is moved so that the peak sits at about 40 percent. Viewers decide quickly, so the strongest part should come early.
5. **Cuts snap to pauses in speech.** When there are captions, the start and end move to the nearest pause or sentence end, within a few seconds, so a clip does not begin or end in the middle of a word.

The score shown for each moment runs from 1 to 100 and is relative to the strongest moment in the same video, which gets 100. It compares moments within one video. It is not a prediction of views.

If no part of the video stands out from the rest, the app spreads the clips evenly across it and tells you so.

## Configuration

The app works without any configuration. To change a setting, set an environment variable before you start the app. A value that is set but wrong stops the app with a message that says what to fix.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLIPSTUDIO_PORT` | `5180` | The port the app listens on. If it is taken, the app tries the next ports and prints the address it chose. |
| `CLIPSTUDIO_HOST` | `127.0.0.1` | The address the app listens on. `localhost` or an IP address. Leave it alone unless you understand the risk: with an address that other machines can reach, people on your network may be able to use the app. It has no login. |
| `CLIPSTUDIO_ALLOWED_HOSTS` | `youtube.com,youtu.be` | The video sites that links may point to, separated by commas. The `www`, `m` and `music` subdomains of each name are accepted too. |
| `CLIPSTUDIO_MAX_UPLOAD_MB` | `4096` | The largest video file you can add, in megabytes. The default is 4 GB. |
| `CLIPSTUDIO_DATA_DIR` | `data` in the project folder | The folder for uploads, work files and finished clips. The app removes old files only from folders it created itself. If you point it at folders that already hold files, nothing there is removed for you. |
| `CLIPSTUDIO_KEEP_HOURS` | `72` | How many hours uploads, work files and finished clips are kept before the app removes them. Between 1 and 8760. |

Example on Windows (Command Prompt):

```
set CLIPSTUDIO_PORT=5200
start.bat
```

Example on macOS and Linux:

```
CLIPSTUDIO_PORT=5200 ./start.sh
```

The `--port` and `--host` options on the command line take priority over the variables.

These limits are fixed: a clip is 8 to 90 seconds long, one render makes at most 10 clips, and a title is at most 80 characters.

## Troubleshooting

The app writes what it does, and the details of any failure, to `clip-log.txt` in the project folder. Look there first. The file starts empty each time the app starts, so copy what you need before you restart. If you report a bug, include the relevant lines and remove private links first.

**"YouTube asked for a sign-in check and refused the request."**
YouTube sometimes decides a connection looks automated. The app does not sign in to YouTube, so it cannot pass that check. Wait a while and try again, try another network, or try another video. Updating yt-dlp often helps, because YouTube changes frequently: run `python -m pip install --upgrade "yt-dlp[default]"` in the app's environment, or run the start script again.

**"The Deno helper that yt-dlp needs for YouTube is missing."**
yt-dlp needs Deno to run YouTube's player script. Run `start.bat` or `start.sh` again so it can be installed, or run `python -m pip install -r requirements.txt` in the app's environment. The app shows which tools it found on its first screen.

**The app says there is no replay data.**
YouTube publishes its "most replayed" graph only for videos with enough views, and not for every video. For others, including most small channels and new uploads, the app uses audio and captions. The moments are still ranked, with less to go on.

**The clips have no captions.**
Captions come from the video's own YouTube captions, uploaded or automatic. If YouTube has none for the video, or has not created automatic ones yet, there is nothing to burn in. This is common for very new uploads and for streams that ended recently. Video files from your computer never have captions in this version.

**"This stream is still live."**
Wait until the stream has ended and YouTube has finished processing it, then paste the link again.

**The video is private, members-only or age-restricted.**
These need a signed-in account, which the app does not use. They cannot be opened.

**The port is already in use.**
If port 5180 is taken by another program or another copy of the app, the app tries the next ports, up to 5199, and prints the address it chose in the terminal. Use that address. To choose the port yourself, set `CLIPSTUDIO_PORT`, as shown under [Configuration](#configuration).

**The page does not open.**
Check that the terminal window is still open and shows no error. Use the address printed in the terminal, with `http`, not `https`. The app answers only to `127.0.0.1` or `localhost` with its own port, so an address copied from another computer or another name for your machine is refused.

**An upload is refused.**
The file must be `.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v` or `.avi`, and no larger than the upload limit.

**Rendering is slow.**
ffmpeg encodes on the processor, so the time depends on your computer and on the length and number of clips. One render runs at a time, and a second one waits for the first to finish.

## Project layout

```
clipstudio/          the Python package
  __init__.py        version number
  __main__.py        starts the app: python -m clipstudio
  config.py          settings from environment variables, and the check for video links
  engine.py          yt-dlp and ffmpeg work: read, score, download, caption, render
  jobs.py            background jobs: queue, progress, cancel
  server.py          HTTP server, routes, uploads, static files
public/              the page: index.html, styles.css, app.js, kit.css, kit.js
tests/               pytest tests; tests/e2e/ holds the browser test
docs/                ARCHITECTURE.md, RESPONSIBLE_USE.md, RELEASING.md
.github/             CI workflow, Dependabot settings, issue and pull request templates
start.bat            start script for Windows
start.sh             start script for macOS and Linux
pyproject.toml       project metadata and tool settings
requirements.txt     runtime dependencies
requirements-dev.txt development tools
```

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the parts work together, the security measures and what is kept on disk.

## Development

Create and activate a virtual environment as shown under [Manual start](#manual-start), then install the runtime dependencies and the development tools:

```
python -m pip install -r requirements.txt -r requirements-dev.txt
```

| Task | Command |
| --- | --- |
| Run the tests | `python -m pytest` |
| Lint | `ruff check .` |
| Check formatting | `ruff format --check .` |
| Type check | `mypy clipstudio` |
| Check dependencies for known vulnerabilities | `pip-audit -r requirements.txt` |
| Run the UI test (needs Node.js and Playwright) | `node tests/e2e/ui.test.mjs` |

The tests do not use the network. Tests that need ffmpeg make their own short clips with it.

The UI test drives the page in Chromium against a stand-in server, and is not part of CI. Run it yourself when you change the page or the API. [CONTRIBUTING.md](CONTRIBUTING.md) explains how to set it up, along with the branch, commit and review rules.

Run the app from a copy of the repository. The page in `public/` is not part of the installed Python package, so `pip install` alone does not give you a working app.

Other documents:

- [CONTRIBUTING.md](CONTRIBUTING.md): how to propose a change.
- [SECURITY.md](SECURITY.md): how to report a vulnerability privately.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md): how we treat each other.
- [CHANGELOG.md](CHANGELOG.md): what changed in each version.
- [docs/RELEASING.md](docs/RELEASING.md): how releases are made, and the GitHub settings the project expects.

## Use it responsibly

Clip only videos you made, or videos whose owner has given you permission. The app asks you to confirm this before every analysis, and the server refuses to start an analysis without it.

Permission from the creator is not the whole story. The game or the music in a video can belong to someone else, and a platform can still limit or remove a clip under its own rules. Instagram and YouTube have both published rules about reposted and reused content.

Read [docs/RESPONSIBLE_USE.md](docs/RESPONSIBLE_USE.md) before you post clips of someone else's video. It is general information, not legal advice.

## Licence

Dost Clip Studio is released under the [MIT licence](LICENSE). Copyright (c) 2026 Ritesh Pandey.

### Third-party software

The repository contains only this project's own code. The tools below are not included in it. pip downloads them to your computer when you install the requirements, and each keeps its own licence.

| Tool | Used for | Licence |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Reading and downloading from YouTube | The Unlicense |
| [Deno](https://deno.com/) | Running YouTube's player script for yt-dlp | MIT |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | Provides an ffmpeg program | BSD 2-Clause for the package itself |
| [ffmpeg](https://ffmpeg.org/) | Measuring audio and rendering clips | GPL for the build that imageio-ffmpeg provides |

About ffmpeg: the build that imageio-ffmpeg provides is licensed under the GPL. This app does not include ffmpeg in the repository and does not link to it or copy its code. It only runs ffmpeg as a separate program on your computer. If you redistribute a bundle that contains that ffmpeg build, the GPL's terms apply to your distribution of it.

The `default` extra of yt-dlp installs some further packages, each under its own licence. Run `python -m pip list` in the app's environment to see them.

The page loads the typefaces Bricolage Grotesque and Instrument Sans from Google Fonts. Both are published under the SIL Open Font License.

No video, music, logo or brand asset from a third party is in this repository. Test media is generated with ffmpeg while the tests run.

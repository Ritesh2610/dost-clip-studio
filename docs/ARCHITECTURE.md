# Architecture

Dost Clip Studio is one Python process on the user's computer. It serves a small web page to the user's browser, runs long tasks on background threads and calls three tools to do the video work:

- **yt-dlp**, used as a Python library, reads video details, captions and replay data from YouTube and downloads the parts that are needed.
- **Deno** runs YouTube's player script for yt-dlp. It is a separate program that yt-dlp starts.
- **ffmpeg** measures loudness and renders the clips. It is a separate program that the app starts.

Nothing runs on a remote server that belongs to this project. There is no account, no database and no telemetry.

## Modules

| File | Responsibility |
| --- | --- |
| `clipstudio/__init__.py` | Holds the version number, `__version__`. |
| `clipstudio/__main__.py` | Entry point for `python -m clipstudio`. Reads the command line options, loads the settings, starts the log, reports which tools were found, starts the server and opens the browser. |
| `clipstudio/config.py` | Reads the `CLIPSTUDIO_*` environment variables, checks them and stops the app with a plain message when one is wrong. Also holds the check for pasted video links (`validate_video_url`) and the fixed limits for clips. |
| `clipstudio/jobs.py` | The job manager. One worker thread for analysis and one for rendering, a queue for each, progress and step text, cancelling, and a cap on how many jobs are remembered. Turns failures into a plain message and keeps the traceback for the log. |
| `clipstudio/server.py` | The HTTP server, built on the Python standard library. Routes, request checks (`Host`, `Origin`, sizes, JSON shape), uploads, static files from `public/`, rendered files from the output folder with HTTP Range support, response headers, logging, and cleaning up old folders. It defines what an analysis job and a render job do by calling the engine. |
| `clipstudio/engine.py` | All yt-dlp and ffmpeg work: reading a source, captions, loudness per second, scoring moments, downloading one section, building the captions file and rendering a short. It knows nothing about HTTP or jobs. |
| `public/` | The page: `index.html`, `styles.css`, `app.js`, plus the shared `kit.css` and `kit.js`. Plain HTML, CSS and JavaScript with no build step. It talks to the server only through the JSON API. |
| `tests/` | pytest tests for the engine, settings, jobs and server. `tests/e2e/` holds the browser test, which drives the page in Chromium against a stand-in server that answers the API with made-up data. |

The dependency direction is one way: `server` uses `jobs`, `config` and `engine`. `jobs` uses `engine` only to phrase error messages. `engine` and `config` use neither of the others.

Two interfaces are treated as contracts and change only with care:

- The public functions of `engine.py` (names, arguments and return shapes).
- The HTTP API between the page and the server.

## The HTTP API in brief

All bodies are JSON, except an upload, which is the raw bytes of the file. Errors always have the shape `{"error": {"status", "code", "message", "hint"}}`, where `hint` is optional.

| Request | Purpose |
| --- | --- |
| `GET /api/status` | Version, the tools that were found and the limits the page should apply. |
| `POST /api/uploads` | Stores a video file from the user's computer. Returns an `uploadId`. |
| `POST /api/analyze` | Starts an analysis job for a link or an `uploadId`. Returns a `jobId`. |
| `GET /api/jobs/{id}` | State, step text, progress and, when finished, the result or the error. |
| `POST /api/jobs/{id}/cancel` | Asks a job to stop. |
| `POST /api/render` | Starts a render job for the chosen clips of an analysis. Returns a `jobId`. |
| `GET /output/<dir>/<file>` | A rendered clip. Supports Range requests so the browser can seek. |
| Anything else | A file from `public/`. |

## How an analysis runs

```
Browser (public/app.js)        server.py                    jobs.py                 engine.py
        |                          |                           |                        |
        |  POST /api/analyze       |                           |                        |
        |  {url or uploadId,       |                           |                        |
        |   clipLength, count,     |                           |                        |
        |   permission: true}      |                           |                        |
        |------------------------->|                           |                        |
        |                          | check Host and Origin     |                        |
        |                          | check body size and JSON  |                        |
        |                          | permission must be true   |                        |
        |                          | check the link or upload  |                        |
        |                          |-------------------------->|                        |
        |                          |   submit("analyze", ...)  | job is queued          |
        |  202 {jobId}             |                           |                        |
        |<-------------------------|                           |                        |
        |                          |                           | analyze worker thread  |
        |                          |                           |----------------------->|
        |                          |                           |  inspect_url or        |
        |                          |                           |    inspect_file        |
        |                          |                           |  download_audio        |
        |                          |                           |    (links only)        |
        |                          |                           |  audio_levels          |
        |                          |                           |  find_moments          |
        |  GET /api/jobs/{id}      |                           |                        |
        |  (repeated)              |                           |                        |
        |------------------------->| snapshot of the job       |                        |
        |  state, step, progress   |                           |                        |
        |<-------------------------|                           |                        |
        |                          |                           |                        |
        |  ... until state "done": result with analysisId,     |                        |
        |      video details, signals and moments              |                        |
```

1. The server refuses the request before any work starts if a check fails. A missing or false `permission` gives a 400 error with the code `permission_required`.
2. For a link, `inspect_url` reads the title, channel, duration, replay data and captions without downloading the video. For a file, `inspect_file` reads its duration.
3. The app measures loudness for each second. For a link it first downloads the smallest audio track, which is enough for this. For a file it reads the audio from the file.
4. `find_moments` combines the signals and returns the ranked moments. The scoring is described in the [README](../README.md#how-the-strongest-moments-are-found).
5. If the audio cannot be fetched or measured, the analysis carries on with the other signals and the failure is logged.
6. The server keeps the source details and caption words in memory under an `analysisId`, so a later render request does not need to send them back and cannot change them. It remembers the 20 most recent analyses.

## How a render runs

```
Browser                        server.py                    jobs.py                 engine.py
        |                          |                           |                        |
        |  POST /api/render        |                           |                        |
        |  {analysisId,            |                           |                        |
        |   clips: [{start, end,   |                           |                        |
        |            title}],      |                           |                        |
        |   options}               |                           |                        |
        |------------------------->|                           |                        |
        |                          | check Host and Origin     |                        |
        |                          | each clip 8 to 90 s and   |                        |
        |                          |   inside the video        |                        |
        |                          | at most 10 clips          |                        |
        |                          | options from fixed lists  |                        |
        |                          |-------------------------->|                        |
        |  202 {jobId}             |   submit("render", ...)   | job is queued          |
        |<-------------------------|                           |                        |
        |                          |                           | render worker thread   |
        |                          |                           | for each clip:         |
        |                          |                           |----------------------->|
        |                          |                           |  fetch_section         |
        |                          |                           |    (links only)        |
        |                          |                           |  build_ass             |
        |                          |                           |    (captions, title)   |
        |                          |                           |  render_short          |
        |  GET /api/jobs/{id}      |                           |                        |
        |  (repeated)              |                           |                        |
        |------------------------->|                           |                        |
        |  ... until "done": folder and a list of clips,       |                        |
        |      each with ok, name, url, seconds and caption    |                        |
        |                          |                           |                        |
        |  GET /output/<dir>/<file>.mp4  (with Range)          |                        |
        |------------------------->| file from the output      |                        |
        |  video bytes             |   folder only             |                        |
        |<-------------------------|                           |                        |
```

1. For a link, `fetch_section` downloads only the seconds that the clip needs, at up to 1080p. For a file, the upload is used directly.
2. `build_ass` writes the captions file for the clip from the caption words, and the title if the user wants it shown. It escapes the text.
3. `render_short` runs ffmpeg once per clip: crop or blurred background to 1080x1920, captions burned in, loudness normalised, H.264 video and AAC audio at 30 frames per second.
4. A clip that fails does not stop the others. Its entry in the result has `ok: false` and a message.
5. The result includes suggested post text for each clip: the title, and a credit line that names the channel when the source has one. The same text is saved next to the clip as a `.txt` file.
6. Each render writes to its own folder, `data/output/<video id>-<job id>/`, with files named `clip-01.mp4`, `clip-02.mp4` and so on. If the job is cancelled, the folder is removed.

## Jobs

- There is one worker thread for each kind of job. One analysis and one render can run at the same time. Further jobs of the same kind wait in the order they arrived.
- At most 10 jobs of a kind can wait. A request beyond that is refused.
- The app remembers up to 50 jobs. When the list is full, the oldest finished job is forgotten.
- Cancelling is cooperative. A waiting job is cancelled at once. A running job stops at its next checkpoint. Checkpoints sit between steps and inside the sound download of an analysis, which reports its progress many times a second. A clip download or an ffmpeg run that has started is allowed to finish first.
- Jobs live in memory. Restarting the app forgets them.
- The job's work folder, `data/work/job-<id>/`, is removed when the job ends, whether it finished, failed or was cancelled.

## Threat model

The app is a web server that can download files and run programs, so the question is who could make it do that without the user meaning to.

### Who and what we defend against

1. **Other websites.** Any page open in the user's browser can try to send requests to `http://127.0.0.1`. A hostile page must not be able to start jobs, upload files or read results.
2. **Other machines.** Computers on the same network must not be able to reach the app at all.
3. **Untrusted input.** The app treats all of these as hostile until checked:
   - the pasted link;
   - uploaded files, both their names and their contents;
   - titles, whether typed by the user or read from the video site;
   - everything else that comes back from the video site: channel names, captions, thumbnails and the media itself.

### What is outside the model

- Someone who already controls the user's account on the computer. They can read the files directly.
- Other programs running as the same user. The API has no login, so they can call it.
- Faults inside yt-dlp, ffmpeg or Deno when they parse data from the network or from a file. The project keeps their minimum versions current and receives update pull requests from Dependabot, but it cannot rule out bugs in them.

### How each measure answers a threat

| Measure | What it stops |
| --- | --- |
| The server listens on `127.0.0.1` unless `CLIPSTUDIO_HOST` or `--host` says otherwise. | Other machines cannot connect. |
| Requests whose `Host` header is not `localhost`, `127.0.0.1` or `[::1]` with the server's port are refused with a 403 error. | DNS rebinding, where a hostile site points its own name at 127.0.0.1 so the browser treats the app as part of that site. |
| Every `POST` request is refused if its `Origin` header is present and is not the app's own origin, or if the browser marks it as cross-site with `Sec-Fetch-Site`. Only `GET`, `HEAD` and `POST` are accepted, and `GET` never changes anything. | Cross-site requests from another website that try to start an upload, an analysis, a render or a cancel. |
| The server sends no CORS headers, and sends `Cross-Origin-Resource-Policy: same-origin`, `X-Frame-Options: DENY` and `frame-ancestors 'none'`. | Another website reading the app's responses, embedding its clips or showing the app inside a frame to trick the user into clicking. |
| Links must be `http` or `https`, on an allowed host (`youtube.com`, its `www`, `m` and `music` subdomains, and `youtu.be` by default), at most 2,048 characters, with no user name or password, no unusual port and no spaces, backslashes or control characters. The address that the video site reports back for the video goes through the same check before it is used. | Using the app as a general downloader, or pointing yt-dlp at addresses inside the user's network. |
| `permission` must be exactly `true` on every analysis request, checked by the server. | Skipping the permission confirmation by calling the API without the page. |
| Every value in a request is checked for type and range before use: clip length, count, clip times, layout, position, zoom, the on and off options, and the identifiers, which must match a fixed pattern. A render refers to an analysis by its identifier, so the source cannot be swapped between the two steps. | Malformed or oversized values reaching yt-dlp or ffmpeg. |
| No `shell=True`. Every subprocess call passes its arguments as a list. | Command injection through a link, a title or a file name. |
| User text never becomes a file name, a command argument or an ffmpeg filter string. Work and output files get generated names (`section-01.mp4`, `clip-01.mp4`), and output folder names are reduced to letters, digits, hyphens and underscores. Titles lose their control characters, are limited to 80 characters and go only into the captions file, through `engine.build_ass`, which removes backslashes, replaces braces and flattens line breaks. | Injection into the ffmpeg command or filter graph, and into the captions format, which has its own control codes. |
| ffmpeg is given the captions file as a bare generated name and runs inside the output folder. | Path and quoting problems in the ffmpeg filter argument. |
| Upload names are used only for display. The file is stored under a random name and keeps only its extension, which must be one of `.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v` or `.avi`. | Writing files outside the uploads folder, overwriting files and storing files that would run when opened. |
| Static files are served only if every part of the path is an ordinary name and the resolved path is inside `public/`. Rendered files are served only if the path has the exact form `/output/<folder>/clip-NN.mp4` or `.txt` and resolves inside the output folder. | Path traversal, such as `/output/../../some-file`, and reading hidden files. |
| Limits: JSON bodies up to 256 KB, uploads up to 4 GB by default and only with a stated length and enough free disk space, clips of 8 to 90 seconds, 10 clips per render, one running job of each kind, 10 waiting, 50 jobs, 20 analyses and 20 uploads remembered, and old files removed after a set time. | Filling the disk or the memory, or tying up the computer, by accident or on purpose. |
| Stack traces go to the terminal and `clip-log.txt`. The browser gets a plain message and, when useful, a hint. | Leaking local paths and internals to a page or a screenshot. |
| Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and a Content-Security-Policy. The policy allows scripts only from the app itself, styles and fonts from the app and Google Fonts, media from the app and `blob:`, images from the app, `data:`, `i.ytimg.com` and `*.ggpht.com`, and connections only back to the app. | If text from a video title ever reached the page as markup, the browser would still refuse to run it as a script, load content from elsewhere or send data elsewhere. The referrer rule keeps the local address out of requests to other sites. |
| The page has no inline scripts, inline event handlers or inline `style` attributes. | Lets the Content-Security-Policy stay strict, with no `unsafe-inline`. |
| Thumbnail addresses from the video site are passed to the page only if they start with `https://`. | Odd address schemes reaching the page. |
| No telemetry. The server makes no network calls except the ones yt-dlp makes to the video site. | Nothing about the user's videos is sent to the project or to anyone else. |

Checks that support these measures during development:

- `ruff` runs the flake8-bandit rules in CI, including the rules against `shell=True` and `os.system`.
- The tests cover the request checks, the link checks and the path handling without using the network.
- `pip-audit` runs in CI against the runtime dependencies, as a job that reports but does not block.

### What the browser itself fetches

The server contacts only the video site. The page in the browser also loads two things from other servers, and the Content-Security-Policy allows exactly these:

- two typefaces from Google Fonts;
- the thumbnail of the video, from YouTube's image servers.

If the fonts cannot be loaded, the page uses system fonts instead. The app works without either.

### If you change the listening address

Setting `CLIPSTUDIO_HOST` to an address that other machines can reach removes the first line of defence. The app logs a warning when it starts this way. The `Host` check then also accepts the address you chose, and the `Origin` check still applies, but the app has no login and no encryption, and it is not designed for that use. Do it only on a network you trust, and prefer not to.

## Data kept on disk

Everything the app writes is inside its data folder, `data/` in the project folder by default (`CLIPSTUDIO_DATA_DIR` changes it), plus one log file.

| Location | Contents | Kept for |
| --- | --- | --- |
| `data/uploads/` | Video files you chose from your computer, under random names. | 72 hours after they were last used, by default. The app also keeps at most 20 uploads and deletes the oldest beyond that. |
| `data/work/job-<id>/` | Temporary files of one job: the audio track used for analysis, or the downloaded sections used for rendering. | Until the job ends. Anything left behind, for example after a crash, is removed after 72 hours. |
| `data/output/<folder>/` | The rendered clips of one render, `clip-NN.mp4`, each with a `clip-NN.txt` that holds the suggested post text. | 72 hours by default. Copy the clips you want to keep somewhere else. |
| `clip-log.txt` in the project folder | What the app did, messages from yt-dlp and ffmpeg, and stack traces of failures. It can contain video links, titles and folder paths. | Until the next start. The file is emptied each time the app starts. |

How the clean-up works:

- It runs when the app starts and each time a job finishes. Nothing is removed while the app is not running.
- It removes files and folders whose last change is older than `CLIPSTUDIO_KEEP_HOURS` (72 by default).
- It leaves alone anything a running job or an upload in progress is using.
- It only touches folders the app created itself, which it marks with a small file named `.clipstudio`. If `CLIPSTUDIO_DATA_DIR` points at folders that already held files, the app uses them but never deletes from them.

Not stored on disk: job state, analysis results and caption words, which live in memory until the app stops.

Nothing is stored anywhere else, and `data/` and `clip-log.txt` are in `.gitignore`.

## Known limits

- **One at a time.** One analysis and one render run at once. Others wait in a queue.
- **Finished streams only.** A live stream must have ended, and YouTube must have finished processing it, before the app can read it.
- **No sign-in.** The app does not use a YouTube account. Private, members-only and age-restricted videos cannot be opened.
- **Replay data is not always there.** YouTube publishes its "most replayed" graph only for videos with enough views. Without it the app relies on audio and captions.
- **Captions depend on YouTube.** Caption words come from the video's uploaded or automatic captions. If the video has none, the clips have no burned-in captions and the speech signal is not used. Video files from your computer have no captions and no replay data, so only loudness is measured for them.
- **No face or subject tracking.** The "fill" layout crops a fixed left, centre or right part of the picture. The "blur" layout keeps the whole picture over a blurred background.
- **Fixed output.** Clips are 1080x1920 at 30 frames per second, H.264 with AAC audio. Sources are fetched at up to 1080p.
- **Caption fonts come from the system.** The app asks for Nirmala UI or Arial on Windows, Kohinoor Devanagari or Helvetica Neue on macOS, and Noto Sans Devanagari or DejaVu Sans on Linux. If a font is missing, ffmpeg picks another one.
- **Speech scoring knows a limited word list.** The words that count as excitement are a short English and Hindi list in `engine.py`. Other languages still get the speaking-speed part of the signal.
- **Scores are relative.** A score of 100 means the strongest moment in this video, not a strong moment in general.
- **Memory only.** Restarting the app forgets jobs and analyses. Rendered files stay on disk until the clean-up removes them.

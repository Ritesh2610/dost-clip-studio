# Contributing

Thank you for helping with Dost Clip Studio. This page explains how to set up the project, how changes get in and what a change needs before it can be merged.

By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md). Security problems are reported privately: see [SECURITY.md](SECURITY.md).

## Before you start

- For a bug, open an issue with the bug report form, or comment on the existing one.
- For a new feature, open a feature request first and wait for the maintainer's answer before you write code. This saves you from building something that will not be merged.
- Two things are out of scope: posting or scheduling clips for the user, and anything that helps clip videos without the owner's permission. [docs/RESPONSIBLE_USE.md](docs/RESPONSIBLE_USE.md) explains why.

## Set up

You need Python 3.11 or newer and Git.

```
git clone https://github.com/Ritesh2610/dost-clip-studio.git
cd dost-clip-studio
python -m venv .venv
```

Activate the environment. On Windows:

```
.venv\Scripts\activate
```

On macOS and Linux:

```
source .venv/bin/activate
```

Install the app's dependencies and the development tools:

```
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Start the app from the project folder:

```
python -m clipstudio
```

You do not need to install ffmpeg or Deno yourself. The `imageio-ffmpeg` and `deno` packages bring their own copies.

## Run the checks

Run these from the project folder. CI runs the same commands, and all of them must pass.

| Check | Command |
| --- | --- |
| Lint | `ruff check .` |
| Formatting | `ruff format --check .` |
| Types | `mypy clipstudio` |
| Tests | `python -m pytest` |

To fix what can be fixed automatically, run `ruff check --fix .` and `ruff format .`.

To check the runtime dependencies for known vulnerabilities, run `pip-audit -r requirements.txt`. CI runs this as a separate job that does not block merging, so read its result yourself when you change a dependency.

If you like, install the pre-commit hooks so lint and formatting run before each commit:

```
python -m pip install pre-commit
pre-commit install
```

### About the tests

- Tests must not use the network and must not contact YouTube. In server and job tests, the engine functions that go online are replaced with stand-ins.
- Tests that need ffmpeg make their own short clips with it. They are skipped, with a reason, when ffmpeg is missing.
- Mark a test that takes more than a few seconds with `@pytest.mark.slow`. Run only the quick ones with `python -m pytest -m "not slow"`.
- Never add a real video, song, logo or other third-party media file to the repository, not even a small one for a test.

### The UI test

The browser test, `tests/e2e/ui.test.mjs`, drives the real page in Chromium with Playwright. It does not use the Python server. It starts a stand-in server, `tests/e2e/mock_server.mjs`, which serves `public/` and answers the HTTP API with made-up data, so it needs no network and no YouTube. A test fails on its own assertions and also on any Content-Security-Policy violation, page error or console error.

CI does not run it, so run it yourself whenever you change anything in `public/`. If you change the HTTP API, change the stand-in server to match in the same pull request.

You need Node.js and Playwright with its Chromium browser. From the project folder:

```
npm install --no-save playwright
npx playwright install chromium
```

Then, with your Python environment active (the stand-in server uses the ffmpeg from `imageio-ffmpeg` to make a three-second sample video):

```
node tests/e2e/ui.test.mjs
```

- To run only some tests, add part of a test name: `node tests/e2e/ui.test.mjs upload`.
- Ports 5412 and 5413 must be free.
- Screenshots go to a temporary folder that the test prints at the end. Set `CLIPSTUDIO_SHOTS` to choose the folder.

Say in your pull request that you ran it.

## Branches

- `main` is always releasable. Nobody pushes to it directly.
- Create a short-lived branch from `main` for each change, and name it after the kind of change: `fix/upload-range-header`, `feat/clip-position`, `docs/troubleshooting`.
- Keep the branch up to date by rebasing on `main` or merging `main` into it. Delete it after the merge.

## Commits

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<optional scope>): <what changed, in the imperative, lower case, no full stop>
```

Types used in this project: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`.

Examples:

```
feat(engine): score moments from caption speed
fix(server): reject requests whose Host header is not local
docs: explain what happens when a video has no replay data
test(jobs): cover cancelling a queued render
build: raise the minimum yt-dlp version
ci: run the tests on Python 3.13
```

Mark a breaking change with `!` and explain it in the body:

```
feat(api)!: return clip times in milliseconds

BREAKING CHANGE: `start` and `end` in the render result are now integers in milliseconds.
```

Do not mention tools or people that helped you write the change in commit messages. Describe the change.

## Pull requests

- One logical change per pull request. A bug fix and an unrelated tidy-up are two pull requests.
- Add or update tests for every change in behaviour. A bug fix comes with a test that fails without the fix.
- Update the docs and add a line to the "Unreleased" section of [CHANGELOG.md](CHANGELOG.md) when behaviour changes.
- Fill in the pull request template.
- CI must pass: lint, formatting, type check and the tests on Ubuntu and Windows.
- The code owner, [@Ritesh2610](https://github.com/Ritesh2610), reviews every pull request and merges it. Pull requests are squashed into one commit, so the pull request title must also follow Conventional Commits.

## Coding standards

### Python

- Python 3.11 is the oldest supported version. Do not use syntax or library features that are newer.
- `ruff format` decides the layout. Lines are up to 120 characters.
- New and changed functions have type hints. `mypy clipstudio` must report no errors.
- The server uses only the Python standard library. The runtime dependencies are `yt-dlp`, `deno` and `imageio-ffmpeg`, and nothing else. Open an issue before proposing a new one.
- The names, arguments and return shapes of the public functions in `clipstudio/engine.py` and the HTTP API are a contract between the parts of the app. Change them only together with everything that uses them, and note it in the changelog.

### Security rules for code

These are checked in review. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains the reasons.

- Never use `shell=True` or `os.system`. Pass subprocess arguments as a list.
- Text from the user or from the video site never becomes a file name, a command argument or an ffmpeg filter string without validation. Titles go into the captions file only, through `engine.build_ass`, which escapes them.
- Files that are served or written must resolve to a path inside their own folder.
- Never send a stack trace or a local path to the browser. Log it, and return a plain message.
- No telemetry and no network calls other than the ones yt-dlp makes to the video site.
- The page must work under the Content-Security-Policy: no inline scripts, no inline event handlers and no inline `style` attributes.

### Words in the app and the docs

- Plain words, sentence case and active voice. Buttons say what they do.
- An error message says what went wrong and what to do next.
- Never promise that a clip will become popular. Say "strongest moments" or "more likely to hold attention".
- No labels in capital letters, no emoji and no arrows in button text.
- Every input has a label, and the page must work at a width of 390 pixels without scrolling sideways.

## Licence

The project is under the [MIT licence](LICENSE). By sending a pull request you agree that your contribution is released under the same licence.

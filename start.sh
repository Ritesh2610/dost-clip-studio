#!/usr/bin/env sh
# Starts Dost Clip Studio on Mac or Linux. The first run installs the video tools
# (yt-dlp, Deno and ffmpeg) into a private folder (.venv) next to this file.
cd "$(dirname "$0")" || exit 1

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  echo "Python 3.11 or newer is needed. This computer has $("$PY" --version 2>&1)."
  echo "Install a newer one from https://www.python.org/downloads/ and run this again."
  exit 1
fi

if [ ! -x .venv/bin/python ] || [ ! -f .venv/.ready ]; then
  echo "Setting up the video tools. This happens once, needs internet and can take a few minutes."
  if ! "$PY" -m venv --clear .venv; then
    echo "The private folder for the video tools (.venv) could not be made."
    echo "On Debian or Ubuntu, run this first: sudo apt install python3-venv"
    exit 1
  fi
  .venv/bin/python -m pip install --quiet --upgrade pip
  if ! .venv/bin/python -m pip install --quiet -r requirements.txt; then
    echo "The video tools could not be installed. Check your internet connection and run this again."
    exit 1
  fi
  touch .venv/.ready
fi

# Ctrl+C stops the app, not this script, so the closing message below is still shown.
trap ':' INT
code=0
.venv/bin/python -X faulthandler -m clipstudio "$@" || code=$?
trap - INT

echo
echo "Dost Clip Studio has stopped (code $code)."
if [ "$code" -ne 0 ]; then
  echo "If you did not stop it yourself, the reason is in clip-log.txt in this folder."
fi
exit "$code"

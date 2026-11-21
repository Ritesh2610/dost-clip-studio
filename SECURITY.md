# Security policy

Dost Clip Studio is a local app. It starts a small web server on your own computer and runs yt-dlp and ffmpeg for you. A security problem here usually means one of two things: something outside your computer can make the app do work it should not, or something the app reads (a link, a file, a video title, a reply from the video site) can make it run or write something it should not.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x (latest release) | Yes |
| Anything older | No |

Fixes go into a new release. Older releases are not patched.

## How to report a vulnerability

Report it privately. Do not open a public issue, pull request or discussion for a security problem.

1. Go to the repository's [Security tab](https://github.com/Ritesh2610/dost-clip-studio/security).
2. Choose "Report a vulnerability". This opens a private security advisory that only you and the maintainer can see.

If you do not see that button, private reporting has not been switched on yet. In that case open a normal issue that says only "I have a security report, please enable private vulnerability reporting". Do not put any details in it.

## What to include

- The version of the app (shown in the app, and in `clipstudio/__init__.py`), your operating system and your Python version.
- What an attacker can do, and what they need first. For example: "a web page open in the same browser can start a render job".
- Steps to reproduce it. A short script or a `curl` command is ideal.
- Whether you changed any settings, such as the address the server listens on or the list of allowed hosts.
- Any log lines from `clip-log.txt` that help. Remove private links and personal details first.

Do not include other people's videos or private links in a report.

## What to expect

- This project has one maintainer who works on it in spare time. You will get an acknowledgement when the maintainer is able to read the report. There is no guaranteed response time.
- If the report is confirmed, the fix is developed in the private advisory, released as a new version and described in `CHANGELOG.md`.
- You will be credited in the advisory and the changelog if you would like to be. Say so in the report.
- There is no bug bounty and no payment for reports.
- Please give the maintainer a reasonable chance to release a fix before you publish details.

## What counts

Examples of problems this policy covers:

- Another website, or another machine on the network, can call the app's API while it runs with default settings.
- A crafted link, file name, video title or caption text leads to a command running, or to a file being read or written outside the app's own data folder.
- A request can read files outside `public/` or the output folder.
- The app sends data somewhere other than the video site that yt-dlp talks to.
- A stack trace or a file path from your computer is returned to the browser.

Problems that are not covered here:

- Bugs in yt-dlp, ffmpeg or Deno themselves. Report those to their own projects. Do tell this project if the app should require a newer version because of one.
- Attacks that need someone to already control your user account or your computer.
- Risks you take on by changing the defaults, such as making the server listen on all network interfaces. The app is not designed to be exposed to a network or the internet.

## How the app is protected

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) describes the threat model and the specific measures in the code: the checks on the `Host` and `Origin` headers, the allow-list of video hosts, how file names and paths are handled, the limits on sizes and jobs, and the response headers.

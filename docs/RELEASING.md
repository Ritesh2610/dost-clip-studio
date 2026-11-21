# Releasing

This page is for the maintainer. It covers how versions are numbered, the steps for a release and the GitHub settings the project expects.

## Versioning

The project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html): `MAJOR.MINOR.PATCH`.

| Change | Example | Version |
| --- | --- | --- |
| A fix that does not change how the app is used | A cut point was off by one second | PATCH: 1.0.0 to 1.0.1 |
| A new feature that keeps existing behaviour working | A new crop position | MINOR: 1.0.1 to 1.1.0 |
| A change that breaks something people rely on | A removed or renamed `CLIPSTUDIO_*` variable, a changed HTTP API response, a higher minimum Python version, a start script that works differently | MAJOR: 1.1.0 to 2.0.0 |

Raising the minimum version of yt-dlp, Deno or imageio-ffmpeg is a PATCH or MINOR change. YouTube changes often, and a newer yt-dlp is usually the fix.

The version number is written in two places, and they must agree:

- `version` in `pyproject.toml`
- `__version__` in `clipstudio/__init__.py`

The lint job in CI fails when they differ.

## Release checklist

1. Make sure `main` is green in CI and that there are no open security advisories waiting for this release.
2. Create a branch, for example `chore/release-1.0.1`.
3. Update the version in `pyproject.toml` and in `clipstudio/__init__.py`.
4. In `CHANGELOG.md`, rename "Unreleased" to the new version with today's date, add a new empty "Unreleased" section above it and update the links at the bottom of the file.
5. If the supported version changed, update the table in `SECURITY.md`.
6. Run every check on your own machine:

   ```
   ruff check .
   ruff format --check .
   mypy clipstudio
   python -m pytest
   pip-audit -r requirements.txt
   ```

7. Run the UI test with `node tests/e2e/ui.test.mjs`. CI does not run it. [CONTRIBUTING.md](../CONTRIBUTING.md#the-ui-test) explains the set-up.
8. Try the app by hand on Windows, because that is where most people use it: run `start.bat` from a fresh copy of the repository, analyse one YouTube video you own and one video file, render a clip with captions and play it.
9. Open a pull request titled `chore: release 1.0.1`, wait for CI and merge it.
10. Tag the merge commit on `main` and push the tag:

    ```
    git switch main
    git pull
    git tag -a v1.0.1 -m "Dost Clip Studio 1.0.1"
    git push origin v1.0.1
    ```

11. On GitHub, create a release from the tag. Paste the changelog section for this version as the description.
12. If the release fixes a security problem, publish the security advisory and credit the reporter if they asked for it.

There is nothing to upload: the project is not published to PyPI, and people run it from a copy of the repository. Releases do not include ffmpeg, Deno or yt-dlp. The start scripts install them with pip on the user's machine.

### If a release is broken

Do not move or delete a tag that has been pushed. Fix the problem on `main` and release the next PATCH version. Add a note to the broken release on GitHub that points to the fixed one.

## Recommended GitHub settings

Nothing in this section is enforced by the files in the repository. These are settings on github.com that the repository owner has to switch on by hand, and the project's process assumes they are on. Check them after creating the repository and again after any change of ownership.

### Protect `main`

Settings, then Branches (or Rules, then Rulesets), for the `main` branch:

- Require a pull request before merging.
- Require status checks to pass before merging, and select these checks from the CI workflow:
  - `lint`
  - `test (ubuntu-latest, Python 3.11)`
  - `test (ubuntu-latest, Python 3.13)`
  - `test (ubuntu-latest, Python 3.14)`
  - `test (windows-latest, Python 3.11)`
  - `test (windows-latest, Python 3.13)`
  - `test (windows-latest, Python 3.14)`
- Do not select `audit (not required)`. That job is allowed to fail so that a new advisory in a dependency does not block unrelated work.
- Require branches to be up to date before merging.
- Block force pushes, and do not allow the branch to be deleted.
- Apply the rules to administrators too.

A note for a project with one maintainer: GitHub does not let you approve your own pull request. If you also require an approving review, you will not be able to merge your own work. Either leave the number of required approvals at zero and rely on the required checks, or add a second person with write access who can review. Turn on "Require review from Code Owners" once there is a second reviewer.

### Pull requests

Settings, then General:

- Allow squash merging and switch off merge commits and rebase merging, so that each pull request becomes one commit with a Conventional Commits title.
- Automatically delete head branches.

### Security

Settings, then Advanced Security (called "Code security" in some accounts):

- Private vulnerability reporting: on. `SECURITY.md` sends reporters to the "Report a vulnerability" button, and that button only exists when this is on.
- Dependabot alerts: on.
- Dependabot security updates: on.
- Secret scanning: on, with push protection.

Dependabot version updates are configured by `.github/dependabot.yml` and start working once the file is on `main`.

### Actions

Settings, then Actions, then General:

- Workflow permissions: "Read repository contents and packages permissions". The CI workflow also asks for read-only access itself.
- Require approval for workflows from first-time contributors.

### Moderation

Settings, then Moderation options, then Reported content:

- Allow people to report content to the maintainers. `CODE_OF_CONDUCT.md` tells people to use it.

### Labels

The issue forms use the labels `bug` and `enhancement`, which GitHub creates by default. If they have been deleted, create them again or the forms will open issues without a label.

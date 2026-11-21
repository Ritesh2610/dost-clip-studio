# Tests

Python tests run with `python -m pytest` from the project root. They do not need the network.
Tests that need ffmpeg make their own short clips and are skipped when ffmpeg is missing.

- `test_config.py`: settings and link validation
- `test_jobs.py`: the background job queue
- `test_server.py`: every HTTP route, the limits and the protections
- `test_engine.py`: moment finding, captions and rendering
- `e2e/`: the browser test of the interface (see `e2e/README.md`)

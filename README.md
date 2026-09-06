# Y4M Converter

A PyQt6 desktop app for creating Chromium-compatible `.y4m` fake-camera video
from an existing video or a live Linux webcam.

> **Project status:** Beta. File conversion is covered by automated tests;
> webcam support currently requires testing on real V4L2 hardware.

Y4M is uncompressed. At 640×360, 30 FPS uses about 9.9 MiB/s; at 1280×720 it
uses about 39.6 MiB/s. The app estimates output size, checks available disk
space, and asks before creating large or unknown-size outputs. Conversions are
written to a private staging file and atomically published only after ffmpeg
succeeds and the Y4M output is validated. A failure or cancellation therefore
does not overwrite an existing destination.

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` available in `PATH`
- Linux and V4L2 devices such as `/dev/video0` for webcam capture
- Optional: `v4l2-ctl` for camera filtering and format probing

On Debian or Ubuntu, ffmpeg is available from the `ffmpeg` package and
`v4l2-ctl` from `v4l-utils`.

## Install and run

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
y4m-converter
```

For development from a checkout:

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -v
python -m build
```

Unexpected Python exceptions are logged to
`$XDG_STATE_HOME/y4m-converter/errors.log`, or
`~/.local/state/y4m-converter/errors.log` when `XDG_STATE_HOME` is unset.

## Use with Chromium

Chromium rewinds a Y4M fake-camera file at EOF, so the generated video loops:

```bash
chromium \
  --use-fake-device-for-media-stream \
  --use-file-for-fake-video-capture=/absolute/path/to/output.y4m
```

For a Hardened Chromium launcher that consumes the original environment
variables:

```bash
HARDENED_MEDIA_MODE=loop \
HARDENED_LOOP_VIDEO_FILE=/absolute/path/to/output.y4m \
/path/to/run_for_automation.sh
```

## Operational notes

- The output folder must already exist and have enough free space for a second
  copy when replacing an existing file; the old file is retained until commit.
- Cancelling first asks ffmpeg to stop gracefully, then forces termination after
  three seconds if needed.
- The application keeps at most 10,000 log blocks in memory during long runs.

## Contributing

Bug reports and focused pull requests are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the development and verification steps.

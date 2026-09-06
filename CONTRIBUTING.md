# Contributing

Thanks for helping improve Y4M Converter. Keep changes focused and include a
test when behavior changes.

## Development setup

The project requires Python 3.10 or newer and `ffmpeg` in `PATH`.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the same checks used by continuous integration before opening a pull
request:

```bash
python -m unittest discover -v
python -m build
```

The integration test uses Qt's offscreen platform and performs a short real
conversion with ffmpeg. Webcam behavior must also be checked manually on Linux
with a V4L2 capture device when it is affected by a change.

## Repository hygiene

Do not commit generated Y4M recordings, virtual environments, build output, or
package metadata. These are intentionally excluded by `.gitignore`.

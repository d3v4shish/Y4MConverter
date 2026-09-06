"""Core helpers for Y4M conversion.

This module deliberately has no Qt dependency so its filesystem and subprocess
behaviour can be tested without starting a graphical application.
"""

from __future__ import annotations

import math
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


Y4M_MAGIC = b"YUV4MPEG2 "


@dataclass(frozen=True)
class WebcamDevice:
    path: str
    label: str

    def display_name(self) -> str:
        return f"{self.path} — {self.label}" if self.label else self.path


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False


def _text(value: str | bytes | None) -> str:
    """Normalize subprocess output, including TimeoutExpired's byte output."""

    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def run_text_command(args: Sequence[str], timeout: float) -> CommandResult:
    """Run a short, non-interactive command without raising OS errors."""

    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            _text(completed.stdout),
            _text(completed.stderr),
            completed.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            _text(exc.stdout),
            _text(exc.stderr),
            None,
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult("", str(exc), None)


def parse_v4l2_devices(output: str) -> list[WebcamDevice]:
    """Parse the grouped output produced by ``v4l2-ctl --list-devices``."""

    devices: list[WebcamDevice] = []
    current_label = ""
    seen: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if not line.startswith((" ", "\t")):
            current_label = line.rstrip(":")
            continue
        path = line.strip()
        if re.fullmatch(r"/dev/video\d+", path) and path not in seen:
            seen.add(path)
            devices.append(WebcamDevice(path, current_label))
    return devices


def normalized_output_path(path: str) -> str:
    """Return an absolute, expanded Y4M destination path."""

    value = path.strip()
    if not value:
        return ""
    expanded = os.path.abspath(os.path.expanduser(value))
    if not expanded.lower().endswith(".y4m"):
        expanded += ".y4m"
    return expanded


def paths_refer_to_same_file(first: Path, second: Path) -> bool:
    """Compare paths safely, including a destination that does not exist yet."""

    try:
        return first.samefile(second)
    except (FileNotFoundError, OSError):
        return first.resolve(strict=False) == second.resolve(strict=False)


def validate_output_destination(destination: Path, input_path: Path | None = None) -> None:
    """Validate a destination before any converter process is started."""

    if input_path is not None and paths_refer_to_same_file(input_path, destination):
        raise ValueError("The input and output paths must be different.")
    if destination.exists() and not destination.is_file():
        raise ValueError("The output path exists but is not a regular file.")
    parent = destination.parent
    if not parent.exists():
        raise ValueError(f"The output folder does not exist: {parent}")
    if not parent.is_dir():
        raise ValueError(f"The output parent is not a folder: {parent}")


def create_staging_path(destination: Path) -> Path:
    """Create a private temporary file beside the final output.

    Keeping both files on the same filesystem makes the final replacement
    atomic. The existing destination remains untouched until conversion and
    validation have both succeeded.
    """

    validate_output_destination(destination)
    descriptor, name = tempfile.mkstemp(
        prefix=".y4m-converter-",
        suffix=".part",
        dir=destination.parent,
    )
    os.close(descriptor)
    return Path(name)


def discard_staging_path(staging_path: Path) -> None:
    """Remove a staging file created by :func:`create_staging_path`."""

    try:
        staging_path.unlink(missing_ok=True)
    except OSError:
        # Cleanup failure should not hide the original conversion error.
        pass


def validate_y4m_file(path: Path) -> None:
    """Reject empty, truncated, or obviously non-Y4M converter output."""

    try:
        file_stat = path.stat()
    except OSError as exc:
        raise ValueError(f"The converter did not create an output file: {exc}") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("The converter output is not a regular file.")
    if file_stat.st_size <= len(Y4M_MAGIC):
        raise ValueError("The converter output is empty or truncated.")
    try:
        with path.open("rb") as output_file:
            header = output_file.readline(4096)
            frame_header = output_file.readline(4096)
            frame_data_offset = output_file.tell()
    except OSError as exc:
        raise ValueError(f"The converter output cannot be read: {exc}") from exc
    if not header.startswith(Y4M_MAGIC) or not header.endswith(b"\n"):
        raise ValueError("The converter output does not contain a valid Y4M header.")
    fields = header.split()
    width_field = next((field[1:] for field in fields if field.startswith(b"W")), b"")
    height_field = next((field[1:] for field in fields if field.startswith(b"H")), b"")
    chroma_field = next((field[1:] for field in fields if field.startswith(b"C")), b"420")
    try:
        width = int(width_field)
        height = int(height_field)
    except ValueError as exc:
        raise ValueError("The converter output has invalid Y4M dimensions.") from exc
    if width <= 0 or height <= 0 or not chroma_field.startswith(b"420"):
        raise ValueError("The converter output has an unsupported Y4M frame format.")
    if not frame_header.startswith(b"FRAME") or not frame_header.endswith(b"\n"):
        raise ValueError("The converter output does not contain a complete video frame.")
    minimum_frame_size = width * height * 3 // 2
    if file_stat.st_size - frame_data_offset < minimum_frame_size:
        raise ValueError("The converter output contains a truncated video frame.")


def commit_staged_output(staging_path: Path, destination: Path) -> None:
    """Validate, flush, and atomically publish a completed conversion."""

    validate_y4m_file(staging_path)
    previous_mode: int | None = None
    try:
        previous_mode = stat.S_IMODE(destination.stat().st_mode)
    except OSError:
        pass

    try:
        with staging_path.open("rb") as output_file:
            os.fsync(output_file.fileno())
        if previous_mode is not None:
            staging_path.chmod(previous_mode)
        os.replace(staging_path, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not support syncing directories. The atomic
            # replacement itself has still completed successfully.
            pass
    except OSError as exc:
        raise ValueError(f"The completed output could not be saved: {exc}") from exc


def estimate_y4m_size(width: int, height: int, fps: int, duration: float) -> int:
    """Estimate bytes for 8-bit 4:2:0 frames plus small per-frame headers."""

    if min(width, height, fps) <= 0 or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Dimensions, frame rate, and duration must be positive.")
    frame_count = math.ceil(fps * duration)
    frame_bytes = width * height * 3 // 2
    return len(Y4M_MAGIC) + frame_count * (frame_bytes + len(b"FRAME\n")) + 256


def format_byte_size(size: int) -> str:
    """Format a non-negative byte count using binary units."""

    value = float(max(0, size))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def probe_media_duration(ffprobe: str, input_path: Path, timeout: float = 5) -> float | None:
    """Return a media duration in seconds, or ``None`` when it is unavailable."""

    result = run_text_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        return None
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    return duration if math.isfinite(duration) and duration > 0 else None

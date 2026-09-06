from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from y4m_converter.core import (
    CommandResult,
    commit_staged_output,
    create_staging_path,
    estimate_y4m_size,
    format_byte_size,
    normalized_output_path,
    parse_v4l2_devices,
    probe_media_duration,
    run_text_command,
    validate_output_destination,
    validate_y4m_file,
)


VALID_Y4M = b"YUV4MPEG2 W2 H2 F1:1 Ip A1:1 C420jpeg\nFRAME\n" + bytes(6)


class DeviceParsingTests(unittest.TestCase):
    def test_parses_grouped_devices_and_removes_duplicates(self) -> None:
        output = """USB Camera (usb-1):
\t/dev/video0
\t/dev/video1

Metadata device:
    /dev/media0
    /dev/video1
"""

        devices = parse_v4l2_devices(output)

        self.assertEqual([device.path for device in devices], ["/dev/video0", "/dev/video1"])
        self.assertEqual(devices[0].label, "USB Camera (usb-1)")

    @patch("y4m_converter.core.subprocess.run")
    def test_timeout_output_is_always_text(self, run: unittest.mock.Mock) -> None:
        run.side_effect = subprocess.TimeoutExpired(
            ["slow-command"], 1, output=b"partial", stderr=b"problem"
        )

        result = run_text_command(["slow-command"], timeout=1)

        self.assertTrue(result.timed_out)
        self.assertEqual(result.stdout, "partial")
        self.assertEqual(result.stderr, "problem")


class OutputPathTests(unittest.TestCase):
    def test_normalizes_suffix_expansion_and_relative_path(self) -> None:
        result = normalized_output_path(" clips/example ")

        self.assertTrue(os.path.isabs(result))
        self.assertTrue(result.endswith("/clips/example.y4m"))

    def test_rejects_input_as_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "same.y4m"
            source.write_bytes(VALID_Y4M)

            with self.assertRaisesRegex(ValueError, "must be different"):
                validate_output_destination(source, source)

    def test_atomic_commit_preserves_old_file_until_valid_output_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "camera.y4m"
            destination.write_bytes(b"old output")
            destination.chmod(0o640)
            staging = create_staging_path(destination)
            staging.write_bytes(VALID_Y4M)

            self.assertEqual(destination.read_bytes(), b"old output")
            commit_staged_output(staging, destination)

            self.assertEqual(destination.read_bytes(), VALID_Y4M)
            self.assertFalse(staging.exists())
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o640)

    def test_invalid_staged_output_does_not_replace_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "camera.y4m"
            destination.write_bytes(b"old output")
            staging = create_staging_path(destination)
            staging.write_bytes(b"this is definitely not a y4m file\n")

            with self.assertRaisesRegex(ValueError, "valid Y4M header"):
                commit_staged_output(staging, destination)

            self.assertEqual(destination.read_bytes(), b"old output")
            self.assertTrue(staging.exists())

    def test_rejects_truncated_y4m(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "short.y4m"
            output.write_bytes(b"YUV4MPEG2 ")

            with self.assertRaisesRegex(ValueError, "empty or truncated"):
                validate_y4m_file(output)

    def test_rejects_y4m_with_a_partial_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "partial.y4m"
            output.write_bytes(b"YUV4MPEG2 W640 H360 F30:1 Ip C420jpeg\nFRAME\nshort")

            with self.assertRaisesRegex(ValueError, "truncated video frame"):
                validate_y4m_file(output)


class SizeAndProbeTests(unittest.TestCase):
    def test_estimate_accounts_for_yuv420_frame_size(self) -> None:
        size = estimate_y4m_size(640, 360, 30, 10)

        self.assertGreater(size, 98_000_000)
        self.assertLess(size, 105_000_000)
        self.assertEqual(format_byte_size(1024**3), "1.0 GiB")

    @patch("y4m_converter.core.run_text_command")
    def test_probe_rejects_non_finite_duration(self, run: unittest.mock.Mock) -> None:
        run.return_value = CommandResult("nan\n", "", 0)

        self.assertIsNone(probe_media_duration("ffprobe", Path("input.mp4")))


if __name__ == "__main__":
    unittest.main()

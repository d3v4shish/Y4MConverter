from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from y4m_converter.core import validate_y4m_file
from y4m_converter.main import Y4MConverterWindow


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class ConversionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(["y4m-converter-tests"])

    def test_file_conversion_runs_and_publishes_valid_y4m(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            destination = root / "output.y4m"
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=32x24:rate=5:duration=0.4",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
                check=True,
                timeout=15,
            )

            with (
                patch("y4m_converter.main.available_webcams", return_value=[]),
                patch.object(QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok),
                patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok),
            ):
                window = Y4MConverterWindow()
                window.input_video.set_path(str(source))
                window.output_y4m_file.set_path(str(destination))
                window.file_width.setValue(32)
                window.file_height.setValue(24)
                window.file_fps.setValue(5)
                window.start_current_job()

                deadline = time.monotonic() + 15
                while window.process is not None and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)

                self.app.processEvents()
                self.assertIsNone(window.process, window.log.toPlainText())
                self.assertTrue(destination.exists(), window.log.toPlainText())
                validate_y4m_file(destination)
                self.assertFalse(list(root.glob(".y4m-converter-*.part")))
                window.close()


if __name__ == "__main__":
    unittest.main()

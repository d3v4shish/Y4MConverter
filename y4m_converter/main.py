from __future__ import annotations

import argparse
from functools import wraps
import os
import shlex
import shutil
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

from PyQt6.QtCore import QProcess, QTimer
from PyQt6.QtGui import QCloseEvent, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ._version import __version__
from .core import (
    WebcamDevice,
    commit_staged_output,
    create_staging_path,
    discard_staging_path,
    estimate_y4m_size,
    format_byte_size,
    normalized_output_path,
    parse_v4l2_devices,
    probe_media_duration,
    run_text_command,
    validate_output_destination,
)


DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 360
DEFAULT_FPS = 30
DEFAULT_DURATION_SECONDS = 10
LARGE_OUTPUT_WARNING_BYTES = 1024**3
F = TypeVar("F", bound=Callable[..., Any])


def application_state_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "").strip()
    if configured and Path(configured).is_absolute():
        return Path(configured) / "y4m-converter"
    return Path.home() / ".local" / "state" / "y4m-converter"


CRASH_LOG_PATH = application_state_dir() / "errors.log"


@dataclass
class RunningJob:
    output_path: Path
    staging_path: Path
    cancel_requested: bool = False


def available_webcams() -> list[WebcamDevice]:
    devices: list[WebcamDevice] = []
    v4l2_ctl = shutil.which("v4l2-ctl")
    if v4l2_ctl:
        output = run_text_command([v4l2_ctl, "--list-devices"], timeout=2).stdout
        devices.extend(parse_v4l2_devices(output))

    known_paths = {device.path for device in devices}
    for path in sorted(Path("/dev").glob("video*")):
        value = str(path)
        if value not in known_paths:
            devices.append(WebcamDevice(value, ""))

    if not v4l2_ctl:
        return devices

    capture_devices: list[WebcamDevice] = []
    for device in devices:
        if device_supports_capture(v4l2_ctl, device.path):
            capture_devices.append(device)
    return capture_devices


def device_supports_capture(v4l2_ctl: str, device_path: str) -> bool:
    result = run_text_command([v4l2_ctl, f"--device={device_path}", "--all"], timeout=2)
    if result.returncode != 0:
        return False
    text = f"{result.stdout}\n{result.stderr}".lower()
    return "video capture" in text


def write_crash_log(text: str) -> None:
    try:
        CRASH_LOG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with CRASH_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(text)
            if not text.endswith("\n"):
                log_file.write("\n")
        CRASH_LOG_PATH.chmod(0o600)
    except OSError:
        pass


def install_exception_hook() -> None:
    previous_hook = sys.excepthook

    def hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if exc_type is KeyboardInterrupt:
            previous_hook(exc_type, exc, tb)
            return
        formatted = "".join(traceback.format_exception(exc_type, exc, tb))
        write_crash_log(formatted)
        app = QApplication.instance()
        if app:
            QMessageBox.critical(
                None,
                "Y4M Converter error",
                f"An unexpected error occurred. Details were written to:\n"
                f"{CRASH_LOG_PATH}\n\n{exc}",
            )
        else:
            previous_hook(exc_type, exc, tb)

    sys.excepthook = hook


def guarded_slot(label: str) -> Callable[[F], F]:
    def decorator(function: F) -> F:
        @wraps(function)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return function(self, *args, **kwargs)
            except Exception:
                formatted = traceback.format_exc()
                write_crash_log(formatted)
                if hasattr(self, "append_log"):
                    self.append_log("")
                    self.append_log(f"{label} failed. Details: {CRASH_LOG_PATH}")
                    self.append_log(formatted.rstrip())
                QMessageBox.critical(
                    self if isinstance(self, QWidget) else None,
                    "Y4M Converter error",
                    f"{label} failed. Details were written to:\n{CRASH_LOG_PATH}",
                )
                return None

        return cast(F, wrapper)

    return decorator


class PathPicker(QWidget):
    def __init__(
        self,
        button_text: str,
        dialog_title: str,
        file_filter: str,
        save_dialog: bool = False,
    ) -> None:
        super().__init__()
        self.dialog_title = dialog_title
        self.file_filter = file_filter
        self.save_dialog = save_dialog

        self.edit = QLineEdit()
        self.button = QPushButton(button_text)
        self.button.clicked.connect(self.pick_path)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, path: str) -> None:
        self.edit.setText(path)

    def pick_path(self) -> None:
        try:
            if self.save_dialog:
                path, _ = QFileDialog.getSaveFileName(
                    self, self.dialog_title, self.path(), self.file_filter
                )
            else:
                path, _ = QFileDialog.getOpenFileName(
                    self, self.dialog_title, self.path(), self.file_filter
                )
            if path:
                self.set_path(path)
        except Exception:
            formatted = traceback.format_exc()
            write_crash_log(formatted)
            QMessageBox.critical(
                self,
                "File dialog failed",
                f"The file picker failed. Details were written to:\n{CRASH_LOG_PATH}",
            )


class Y4MConverterWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Y4M Converter")
        self.process: QProcess | None = None
        self.active_job: RunningJob | None = None
        self._closing = False
        self.ffmpeg_path = shutil.which("ffmpeg")
        self.ffprobe_path = shutil.which("ffprobe")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.document().setMaximumBlockCount(10_000)

        self.tabs = QTabWidget()
        self.file_tab = self.build_file_tab()
        self.webcam_tab = self.build_webcam_tab()
        self.tabs.addTab(self.file_tab, "Video file")
        self.tabs.addTab(self.webcam_tab, "Webcam")

        self.convert_button = QPushButton("Convert / Record")
        self.convert_button.clicked.connect(self.start_current_job)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_job)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        action_row.addWidget(self.convert_button)
        action_row.addWidget(self.cancel_button)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(self.tabs)
        layout.addLayout(action_row)
        layout.addWidget(QLabel("ffmpeg log"))
        layout.addWidget(self.log, 1)
        self.setCentralWidget(root)
        self.resize(900, 650)

        self.append_log("Ready.")
        if not self.ffmpeg_path:
            self.append_log("ERROR: ffmpeg was not found in PATH.")
            self.convert_button.setEnabled(False)
            self.convert_button.setToolTip("Install ffmpeg and restart the application.")

    def build_file_tab(self) -> QWidget:
        self.input_video = PathPicker(
            "Browse…",
            "Choose input video",
            "Video files (*.mp4 *.webm *.mkv *.mov *.avi *.m4v);;All files (*)",
        )
        self.output_y4m_file = PathPicker(
            "Save as…",
            "Choose output Y4M",
            "Y4M video (*.y4m);;All files (*)",
            save_dialog=True,
        )
        self.file_width = self.make_spinbox(2, 7680, DEFAULT_WIDTH, 2)
        self.file_height = self.make_spinbox(2, 4320, DEFAULT_HEIGHT, 2)
        self.file_fps = self.make_spinbox(1, 120, DEFAULT_FPS, 1)
        self.file_limit_duration = QCheckBox("Limit output duration")
        self.file_limit_duration.setChecked(False)
        self.file_duration = self.make_spinbox(1, 3600, DEFAULT_DURATION_SECONDS, 1)
        self.file_duration.setSuffix(" s")
        self.file_duration.setEnabled(False)
        self.file_limit_duration.toggled.connect(self.file_duration.setEnabled)

        form = QFormLayout()
        form.addRow("Input video", self.input_video)
        form.addRow("Output .y4m", self.output_y4m_file)
        form.addRow("Width", self.file_width)
        form.addRow("Height", self.file_height)
        form.addRow("FPS", self.file_fps)
        form.addRow("", self.file_limit_duration)
        form.addRow("Duration", self.file_duration)

        help_label = QLabel(
            "The output uses yuv420p YUV4MPEG2. Chromium rewinds the file at EOF, "
            "so the fake camera loops automatically."
        )
        help_label.setWordWrap(True)

        box = QGroupBox("Convert existing video")
        layout = QVBoxLayout(box)
        layout.addLayout(form)
        layout.addWidget(help_label)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.addWidget(box)
        root_layout.addStretch(1)
        return root

    def build_webcam_tab(self) -> QWidget:
        self.webcam_combo = QComboBox()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_webcams)
        self.probe_button = QPushButton("Probe")
        self.probe_button.clicked.connect(self.probe_webcam)
        self.refresh_webcams()

        webcam_row = QHBoxLayout()
        webcam_row.addWidget(self.webcam_combo, 1)
        webcam_row.addWidget(self.refresh_button)
        webcam_row.addWidget(self.probe_button)

        webcam_row_widget = QWidget()
        webcam_row_widget.setLayout(webcam_row)

        self.output_y4m_webcam = PathPicker(
            "Save as…",
            "Choose output Y4M",
            "Y4M video (*.y4m);;All files (*)",
            save_dialog=True,
        )
        self.webcam_width = self.make_spinbox(2, 7680, DEFAULT_WIDTH, 2)
        self.webcam_height = self.make_spinbox(2, 4320, DEFAULT_HEIGHT, 2)
        self.webcam_fps = self.make_spinbox(1, 120, DEFAULT_FPS, 1)
        self.webcam_duration = self.make_spinbox(1, 3600, DEFAULT_DURATION_SECONDS, 1)
        self.webcam_duration.setSuffix(" s")

        form = QFormLayout()
        form.addRow("Camera", webcam_row_widget)
        form.addRow("Output .y4m", self.output_y4m_webcam)
        form.addRow("Width", self.webcam_width)
        form.addRow("Height", self.webcam_height)
        form.addRow("FPS", self.webcam_fps)
        form.addRow("Record duration", self.webcam_duration)

        help_label = QLabel(
            "Webcam recording uses Linux V4L2 input. If the selected mode is not "
            "supported by your camera, choose a lower resolution or FPS."
        )
        help_label.setWordWrap(True)

        box = QGroupBox("Record webcam to Y4M")
        layout = QVBoxLayout(box)
        layout.addLayout(form)
        layout.addWidget(help_label)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.addWidget(box)
        root_layout.addStretch(1)
        return root

    @staticmethod
    def make_spinbox(minimum: int, maximum: int, value: int, step: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(minimum, maximum)
        box.setSingleStep(step)
        box.setValue(value)
        return box

    @guarded_slot("Refresh webcams")
    def refresh_webcams(self, *_unused: Any) -> None:
        current = self.webcam_combo.currentData() if hasattr(self, "webcam_combo") else None
        self.webcam_combo.clear()
        devices = available_webcams()
        for device in devices:
            self.webcam_combo.addItem(device.display_name(), device.path)
        if not devices:
            self.webcam_combo.addItem("No /dev/video* devices found", "")
            if hasattr(self, "probe_button"):
                self.probe_button.setEnabled(False)
        elif current:
            index = self.webcam_combo.findData(current)
            if index >= 0:
                self.webcam_combo.setCurrentIndex(index)
            if hasattr(self, "probe_button"):
                self.probe_button.setEnabled(True)
        elif hasattr(self, "probe_button"):
            self.probe_button.setEnabled(True)

    @guarded_slot("Probe webcam")
    def probe_webcam(self, *_unused: Any) -> None:
        device = self.webcam_combo.currentData()
        if not device:
            QMessageBox.warning(self, "Missing camera", "No usable webcam device was found.")
            return
        v4l2_ctl = shutil.which("v4l2-ctl")
        if not v4l2_ctl:
            QMessageBox.warning(self, "v4l2-ctl missing", "v4l2-ctl was not found in PATH.")
            return
        result = run_text_command(
            [v4l2_ctl, f"--device={device}", "--list-formats-ext"],
            timeout=5,
        )
        self.append_log("")
        self.append_log(f"Camera probe for {device}:")
        self.append_log((result.stdout or result.stderr or "No output.").rstrip())

    @guarded_slot("Start conversion")
    def start_current_job(self, *_unused: Any) -> None:
        if self.process is not None:
            QMessageBox.warning(self, "Already running", "A conversion is already running.")
            return

        self.ffmpeg_path = shutil.which("ffmpeg")
        if not self.ffmpeg_path:
            QMessageBox.critical(self, "ffmpeg missing", "ffmpeg was not found in PATH.")
            return

        is_file_job = self.tabs.currentWidget() is self.file_tab
        if is_file_job:
            command = self.build_file_command()
        else:
            command = self.build_webcam_command()
        if command is None:
            return

        output_path = Path(command[-1])
        input_path = Path(self.input_video.path()).expanduser() if is_file_job else None
        try:
            validate_output_destination(output_path, input_path)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid output", str(exc))
            return

        if output_path.exists():
            answer = QMessageBox.question(
                self,
                "Overwrite output?",
                f"{output_path} already exists. Replace it after conversion succeeds?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        estimated_size = self.estimated_output_size(is_file_job)
        if not self.confirm_output_size(output_path, estimated_size):
            return

        try:
            staging_path = create_staging_path(output_path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(
                self,
                "Output unavailable",
                f"A temporary output could not be created beside the destination:\n{exc}",
            )
            return

        command[-1] = str(staging_path)
        if is_file_job:
            self.output_y4m_file.set_path(str(output_path))
        else:
            self.output_y4m_webcam.set_path(str(output_path))

        self.log.clear()
        self.append_log("$ " + shlex.join(command))
        if estimated_size is not None:
            self.append_log(f"Estimated output size: {format_byte_size(estimated_size)}")
        self.append_log(f"Destination: {output_path}")
        self.append_log("")
        self.active_job = RunningJob(output_path, staging_path)
        self.process = QProcess(self)
        self.process.setProgram(command[0])
        self.process.setArguments(command[1:])
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_process_output)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.set_job_running(True)
        self.process.start()

    def estimated_output_size(self, is_file_job: bool) -> int | None:
        if is_file_job:
            duration: float | None
            if self.file_limit_duration.isChecked():
                duration = float(self.file_duration.value())
            elif self.ffprobe_path:
                duration = probe_media_duration(
                    self.ffprobe_path,
                    Path(self.input_video.path()).expanduser(),
                )
            else:
                duration = None
            if duration is None:
                return None
            return estimate_y4m_size(
                self.even_value(self.file_width.value()),
                self.even_value(self.file_height.value()),
                self.file_fps.value(),
                duration,
            )
        return estimate_y4m_size(
            self.even_value(self.webcam_width.value()),
            self.even_value(self.webcam_height.value()),
            self.webcam_fps.value(),
            float(self.webcam_duration.value()),
        )

    def confirm_output_size(self, output_path: Path, estimated_size: int | None) -> bool:
        try:
            free_space = shutil.disk_usage(output_path.parent).free
        except OSError:
            free_space = None

        if estimated_size is not None and free_space is not None and estimated_size > free_space:
            QMessageBox.critical(
                self,
                "Not enough disk space",
                f"The Y4M output is estimated at {format_byte_size(estimated_size)}, "
                f"but only {format_byte_size(free_space)} is available.\n\n"
                "Y4M video is uncompressed. Reduce the duration, resolution, or FPS.",
            )
            return False

        if estimated_size is None:
            message = (
                "The input duration could not be determined, so the output size is unknown. "
                "Y4M video is uncompressed and can use tens of megabytes per second. Continue?"
            )
        elif estimated_size >= LARGE_OUTPUT_WARNING_BYTES:
            available = (
                f" Available space: {format_byte_size(free_space)}."
                if free_space is not None
                else ""
            )
            message = (
                f"The uncompressed output is estimated at {format_byte_size(estimated_size)}."
                f"{available}\n\nContinue?"
            )
        else:
            return True

        answer = QMessageBox.question(
            self,
            "Large output warning",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def set_job_running(self, running: bool) -> None:
        self.tabs.setEnabled(not running)
        self.convert_button.setEnabled(not running and bool(self.ffmpeg_path))
        self.cancel_button.setEnabled(running)

    def build_file_command(self) -> list[str] | None:
        input_value = self.input_video.path()
        input_path = os.path.abspath(os.path.expanduser(input_value)) if input_value else ""
        output_path = normalized_output_path(self.output_y4m_file.path())
        if not input_path or not os.path.isfile(input_path):
            QMessageBox.warning(self, "Missing input", "Choose an existing input video file.")
            return None
        if not output_path:
            QMessageBox.warning(self, "Missing output", "Choose an output .y4m file.")
            return None

        width = self.even_value(self.file_width.value())
        height = self.even_value(self.file_height.value())
        fps = self.file_fps.value()
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={fps},setsar=1,format=yuv420p"
        )

        command = [
            self.ffmpeg_path or "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            input_path,
            "-map",
            "0:v:0",
            "-vf",
            video_filter,
            "-an",
        ]
        if self.file_limit_duration.isChecked():
            command.extend(["-t", str(self.file_duration.value())])
        command.extend(["-f", "yuv4mpegpipe", output_path])
        return command

    def build_webcam_command(self) -> list[str] | None:
        device = self.webcam_combo.currentData()
        output_path = normalized_output_path(self.output_y4m_webcam.path())
        if not device:
            QMessageBox.warning(self, "Missing camera", "Choose a webcam device.")
            return None
        if not os.path.exists(device):
            QMessageBox.warning(self, "Camera missing", f"{device} does not exist.")
            return None
        if not os.access(device, os.R_OK):
            QMessageBox.warning(
                self,
                "Camera not readable",
                f"{device} is not readable by this user. Check camera permissions.",
            )
            return None
        if not output_path:
            QMessageBox.warning(self, "Missing output", "Choose an output .y4m file.")
            return None

        width = self.even_value(self.webcam_width.value())
        height = self.even_value(self.webcam_height.value())
        fps = self.webcam_fps.value()
        duration = self.webcam_duration.value()
        return [
            self.ffmpeg_path or "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "v4l2",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            str(fps),
            "-i",
            device,
            "-t",
            str(duration),
            "-vf",
            "format=yuv420p",
            "-f",
            "yuv4mpegpipe",
            output_path,
        ]

    @staticmethod
    def even_value(value: int) -> int:
        return value if value % 2 == 0 else value + 1

    @guarded_slot("Read ffmpeg output")
    def read_process_output(self) -> None:
        if not self.process:
            return
        output = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        if output:
            cursor = self.log.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(output.replace("\r", "\n"))
            self.log.setTextCursor(cursor)
            self.log.ensureCursorVisible()

    @guarded_slot("ffmpeg process error")
    def process_error(self, error: QProcess.ProcessError) -> None:
        process_error = self.process.errorString() if self.process else ""
        self.append_log(f"Process error: {error.name}")
        if process_error:
            self.append_log(process_error)
        if error != QProcess.ProcessError.FailedToStart:
            return

        process = self.process
        job = self.active_job
        self.process = None
        self.active_job = None
        self.set_job_running(False)
        if job:
            discard_staging_path(job.staging_path)
        if process:
            process.deleteLater()
        if not self._closing:
            QMessageBox.critical(
                self,
                "ffmpeg did not start",
                f"ffmpeg could not be started:\n{process_error or 'Unknown error'}",
            )

    @guarded_slot("Finish conversion")
    def process_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self.read_process_output()
        process = self.process
        job = self.active_job
        self.process = None
        self.active_job = None
        self.set_job_running(False)
        if process:
            process.deleteLater()

        if job is None:
            return

        succeeded = exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0
        if succeeded and not job.cancel_requested:
            try:
                commit_staged_output(job.staging_path, job.output_path)
            except ValueError as exc:
                discard_staging_path(job.staging_path)
                self.append_log("")
                self.append_log(f"Output validation failed: {exc}")
                if not self._closing:
                    QMessageBox.warning(
                        self,
                        "Invalid converter output",
                        f"ffmpeg exited successfully, but its output was not published:\n{exc}",
                    )
                return

            self.append_log("")
            self.append_log("Done.")
            self.append_log("")
            self.append_log("Use with Chromium:")
            self.append_log(
                "chromium --use-fake-device-for-media-stream "
                f"--use-file-for-fake-video-capture={shlex.quote(str(job.output_path))}"
            )
            if not self._closing:
                QMessageBox.information(
                    self,
                    "Conversion complete",
                    f"Created:\n{job.output_path}",
                )
        else:
            discard_staging_path(job.staging_path)
            self.append_log("")
            if job.cancel_requested:
                self.append_log("Cancelled. The destination was not changed.")
            else:
                status = "crashed" if exit_status == QProcess.ExitStatus.CrashExit else "failed"
                self.append_log(f"ffmpeg {status} with exit code {exit_code}.")
                self.append_log("The destination was not changed.")
                if not self._closing:
                    QMessageBox.warning(
                        self,
                        "Conversion failed",
                        "ffmpeg could not record/convert with the selected settings. "
                        "The previous destination, if any, was preserved. Check the log, "
                        "probe the camera, or try a lower resolution/FPS.",
                    )

    @guarded_slot("Cancel conversion")
    def cancel_job(self, *_unused: Any) -> None:
        if not self.process or not self.active_job:
            return
        self.append_log("Cancelling...")
        self.active_job.cancel_requested = True
        self.cancel_button.setEnabled(False)
        process_to_stop = self.process
        process_to_stop.terminate()
        QTimer.singleShot(3000, lambda: self.force_kill_process(process_to_stop))

    def force_kill_process(self, expected_process: QProcess) -> None:
        if (
            self.process is expected_process
            and expected_process.state() != QProcess.ProcessState.NotRunning
        ):
            self.append_log("ffmpeg did not stop gracefully; forcing it to exit.")
            expected_process.kill()

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(text)
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.process is not None:
            answer = QMessageBox.question(
                self,
                "Conversion running",
                "A conversion is still running. Cancel it and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._closing = True
            if self.active_job:
                self.active_job.cancel_requested = True
            self.process.kill()
            self.process.waitForFinished(3000)
            if self.active_job:
                discard_staging_path(self.active_job.staging_path)
                self.active_job = None
        event.accept()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="y4m-converter",
        description=(
            "Open the PyQt6 Y4M converter GUI for creating Chromium-compatible "
            "fake-camera .y4m files."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"y4m-converter {__version__}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    parse_args(arguments)
    install_exception_hook()
    app = QApplication([sys.argv[0], *arguments])
    app.setApplicationName("Y4M Converter")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Y4M Converter")
    window = Y4MConverterWindow()
    window.show()
    return app.exec()

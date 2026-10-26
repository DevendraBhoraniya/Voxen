"""Polished, non-modal settings window for Voxen.

Sections are navigated via a sidebar. Supported settings are applied through
callbacks supplied by the main application so that the existing handlers
(device_changed, model_changed, mode_changed, cleanup_toggled, overlay_toggled)
remain the single source of truth.
"""

import logging
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal, Slot, QTimer
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QStackedWidget, QPushButton,
    QLabel, QComboBox, QCheckBox, QLineEdit, QFrame, QScrollArea,
    QFormLayout, QSizePolicy, QButtonGroup, QRadioButton,
)
from PySide6.QtGui import QFont

from . import theme as T
from .audio import list_audio_devices, AudioRecorder

logger = logging.getLogger("Voxen.Settings")


class _SidebarButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setFixedHeight(36)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                text-align: left;
                padding-left: {T.Space.MD}px;
                border: none;
                border-radius: {T.Radius.SM}px;
                background: transparent;
                color: {T.Color.TEXT_SECONDARY};
                font-size: {T.Font.SIZE_SM}px;
            }}
            QPushButton:hover {{
                background: {T.Color.ELEVATED};
                color: {T.Color.TEXT_PRIMARY};
            }}
            QPushButton:checked {{
                background: {T.Color.ELEVATED};
                color: {T.Color.ACCENT};
                font-weight: 600;
            }}
        """)


class _Section(QWidget):
    """Base class for a settings section page."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(T.Space.XL, T.Space.XL, T.Space.XL, T.Space.XL)
        self._layout.setSpacing(T.Space.LG)

        heading = QLabel(title)
        heading.setStyleSheet(
            f"color: {T.Color.TEXT_PRIMARY}; font-size: {T.Font.SIZE_XL}px; "
            f"font-weight: {T.Font.WEIGHT_SEMIBOLD};"
        )
        self._layout.addWidget(heading)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {T.Color.BORDER};")
        self._layout.addWidget(sep)

    def add_row(self, label: str, widget: QWidget, hint: str = ""):
        row = QHBoxLayout()
        row.setSpacing(T.Space.MD)
        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {T.Color.TEXT_PRIMARY}; font-size: {T.Font.SIZE_SM}px;")
        lbl.setMinimumWidth(160)
        row.addWidget(lbl)
        row.addWidget(widget, 1)
        if hint:
            hint_lbl = QLabel(hint)
            hint_lbl.setStyleSheet(f"color: {T.Color.TEXT_MUTED}; font-size: {T.Font.SIZE_XS}px;")
            hint_lbl.setWordWrap(True)
            self._layout.addWidget(hint_lbl)
        self._layout.addLayout(row)

    def add_widget(self, widget: QWidget):
        self._layout.addWidget(widget)

    def add_stretch(self):
        self._layout.addStretch(1)


class SettingsWindow(QWidget):
    """Non-modal settings window with sidebar navigation."""

    audio_level = Signal(float, object)  # forwarded from main app

    def __init__(
        self,
        config: dict,
        on_device_changed: Optional[Callable] = None,
        on_model_changed: Optional[Callable] = None,
        on_mode_changed: Optional[Callable] = None,
        on_cleanup_toggled: Optional[Callable] = None,
        on_overlay_toggled: Optional[Callable] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.config = config
        self._on_device_changed = on_device_changed
        self._on_model_changed = on_model_changed
        self._on_mode_changed = on_mode_changed
        self._on_cleanup_toggled = on_cleanup_toggled
        self._on_overlay_toggled = on_overlay_toggled

        self.setWindowTitle("Voxen Settings")
        self.setMinimumSize(720, 520)
        self.resize(780, 560)
        self.setStyleSheet(f"background: {T.Color.BG}; color: {T.Color.TEXT_PRIMARY};")

        self._test_recorder: Optional[AudioRecorder] = None
        self._test_timer: Optional[QTimer] = None

        self._build_ui()
        self._load_values()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Sidebar
        sidebar = QWidget()
        sidebar.setFixedWidth(180)
        sidebar.setStyleSheet(f"background: {T.Color.SURFACE};")
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(T.Space.SM, T.Space.LG, T.Space.SM, T.Space.LG)
        side_layout.setSpacing(T.Space.XS)

        title = QLabel("Settings")
        title.setStyleSheet(
            f"color: {T.Color.TEXT_PRIMARY}; font-size: {T.Font.SIZE_LG}px; "
            f"font-weight: {T.Font.WEIGHT_SEMIBOLD}; padding: {T.Space.SM}px;"
        )
        side_layout.addWidget(title)
        side_layout.addSpacing(T.Space.SM)

        self._sidebar_group = QButtonGroup(self)
        self._sidebar_group.setExclusive(True)

        sections = [
            ("General", self._build_general),
            ("Dictation", self._build_dictation),
            ("Audio", self._build_audio),
            ("Transcription", self._build_transcription),
            ("Text cleanup", self._build_cleanup),
            ("Privacy", self._build_privacy),
            ("About", self._build_about),
        ]

        self._stack = QStackedWidget()
        for name, builder in sections:
            btn = _SidebarButton(name)
            btn.clicked.connect(lambda checked, b=btn, i=len(self._stack): self._switch(i))
            self._sidebar_group.addButton(btn)
            side_layout.addWidget(btn)
            page = builder()
            self._stack.addWidget(page)

        side_layout.addStretch(1)

        # Select first
        first_btn = self._sidebar_group.buttons()[0]
        first_btn.setChecked(True)

        root.addWidget(sidebar)
        root.addWidget(self._stack, 1)

    def _switch(self, index: int):
        self._stack.setCurrentIndex(index)

    # ------------------------------------------------------------------ sections

    def _build_general(self) -> _Section:
        page = _Section("General")
        ui_cfg = self.config.setdefault("ui", {})

        self._overlay_check = QCheckBox("Show floating pill overlay")
        self._overlay_check.setChecked(ui_cfg.get("show_overlay", True))
        self._overlay_check.toggled.connect(self._on_overlay_toggled)
        page.add_row("Overlay", self._overlay_check, "Display the recording pill near the bottom of the screen.")

        pos_combo = QComboBox()
        pos_combo.addItems(["bottom_center", "bottom_left", "bottom_right"])
        pos_combo.setCurrentText(ui_cfg.get("pill_position", "bottom_center"))
        pos_combo.currentTextChanged.connect(lambda v: ui_cfg.__setitem__("pill_position", v))
        page.add_row("Pill position", pos_combo, "Where the floating pill appears.")

        page.add_stretch()
        return page

    def _build_dictation(self) -> _Section:
        page = _Section("Dictation")

        hotkey_cfg = self.config.setdefault("hotkey", {})
        combo = hotkey_cfg.get("combination", "ctrl+shift").upper()
        mode = hotkey_cfg.get("mode", "toggle").lower()

        hotkey_lbl = QLabel(combo)
        hotkey_lbl.setStyleSheet(f"color: {T.Color.ACCENT}; font-weight: 600;")
        page.add_row("Hotkey", hotkey_lbl, "Global shortcut to start/stop dictation. Edit in config.yaml.")

        self._mode_group = QButtonGroup(self)
        self._mode_toggle = QRadioButton("Toggle (press to start, press to stop)")
        self._mode_hold = QRadioButton("Hold to talk (hold keys while speaking)")
        self._mode_toggle.setChecked(mode == "toggle")
        self._mode_hold.setChecked(mode == "hold")
        self._mode_toggle.toggled.connect(lambda c: self._on_mode_changed("toggle") if c else None)
        self._mode_hold.toggled.connect(lambda c: self._on_mode_changed("hold") if c else None)
        self._mode_group.addButton(self._mode_toggle)
        self._mode_group.addButton(self._mode_hold)
        page.add_widget(self._mode_toggle)
        page.add_widget(self._mode_hold)

        hint = QLabel(
            "Toggle: press once to start recording, press again to stop and transcribe.\n"
            "Hold: keep the keys held while speaking; release to stop."
        )
        hint.setStyleSheet(f"color: {T.Color.TEXT_MUTED}; font-size: {T.Font.SIZE_XS}px;")
        hint.setWordWrap(True)
        page.add_widget(hint)

        page.add_stretch()
        return page

    def _build_audio(self) -> _Section:
        page = _Section("Audio")

        audio_cfg = self.config.setdefault("audio", {})
        current_idx = audio_cfg.get("device_index", None)

        self._mic_combo = QComboBox()
        self._mic_combo.addItem("System Default Microphone", None)
        for d in list_audio_devices(refresh=True):
            self._mic_combo.addItem(f"{d['name']} (#{d['index']})", d["index"])
        # select current
        for i in range(self._mic_combo.count()):
            if self._mic_combo.itemData(i) == current_idx:
                self._mic_combo.setCurrentIndex(i)
                break
        self._mic_combo.currentIndexChanged.connect(self._on_mic_changed)
        page.add_row("Microphone", self._mic_combo, "Select the input device. Changes apply immediately.")

        # Live level meter
        self._level_bar = _LevelBar()
        page.add_row("Level", self._level_bar, "Live input level (updates while recording).")

        # Test microphone
        self._test_btn = QPushButton("Test microphone (2 s)")
        self._test_btn.setCursor(Qt.PointingHandCursor)
        self._test_btn.setStyleSheet(self._btn_style())
        self._test_btn.clicked.connect(self._test_microphone)
        page.add_row("Test", self._test_btn, "Records 2 seconds and reports the captured RMS.")

        self._test_result = QLabel("")
        self._test_result.setStyleSheet(f"color: {T.Color.TEXT_SECONDARY}; font-size: {T.Font.SIZE_SM}px;")
        page.add_widget(self._test_result)

        page.add_stretch()
        return page

    def _build_transcription(self) -> _Section:
        page = _Section("Transcription")

        stt_cfg = self.config.setdefault("stt", {})
        engine = str(stt_cfg.get("engine", "faster-whisper")).lower().strip()
        model = stt_cfg.get("model") or stt_cfg.get("model_name", "base.en")

        engine_combo = QComboBox()
        engine_combo.addItems(["faster-whisper", "whistle", "moonshine"])
        engine_combo.setCurrentText(engine if engine in ("faster-whisper", "whistle", "moonshine") else "faster-whisper")
        engine_combo.currentTextChanged.connect(lambda v: stt_cfg.__setitem__("engine", v))
        page.add_row("Engine", engine_combo, "faster-whisper is recommended for accuracy on CPU.")

        model_combo = QComboBox()
        models = [
            "base.en", "small.en", "distil-small.en", "distil-large-v3",
            "large-v3-turbo", "tiny.en", "whistle.cact", "moonshine-base-en",
        ]
        model_combo.addItems(models)
        if model in models:
            model_combo.setCurrentText(model)
        model_combo.currentTextChanged.connect(lambda v: stt_cfg.__setitem__("model", v))
        page.add_row("Model", model_combo, "base.en is the CPU default. Switching reloads the model.")

        status = QLabel(f"Current: {engine} \u2192 {model}")
        status.setStyleSheet(f"color: {T.Color.TEXT_MUTED}; font-size: {T.Font.SIZE_XS}px;")
        page.add_widget(status)

        note = QLabel(
            "Model loading happens on the UI thread today and may briefly freeze "
            "the window. This will be improved in a future update."
        )
        note.setStyleSheet(f"color: {T.Color.TEXT_MUTED}; font-size: {T.Font.SIZE_XS}px;")
        note.setWordWrap(True)
        page.add_widget(note)

        page.add_stretch()
        return page

    def _build_cleanup(self) -> _Section:
        page = _Section("Text cleanup")

        clean_cfg = self.config.setdefault("cleanup", self.config.setdefault("llm", {}))
        enabled = clean_cfg.get("enabled", False)

        self._cleanup_check = QCheckBox("Enable Ollama text cleanup")
        self._cleanup_check.setChecked(enabled)
        self._cleanup_check.toggled.connect(self._on_cleanup_toggled)
        page.add_row("Ollama cleanup", self._cleanup_check,
                     "Refines punctuation and removes fillers using a local Ollama model.")

        endpoint = QLineEdit(clean_cfg.get("base_url", clean_cfg.get("endpoint", "http://localhost:11434")))
        endpoint.textChanged.connect(lambda v: clean_cfg.__setitem__("base_url", v))
        page.add_row("Endpoint", endpoint, "Ollama base URL (localhost only).")

        model = QLineEdit(clean_cfg.get("model", "llama3.2:3b"))
        model.textChanged.connect(lambda v: clean_cfg.__setitem__("model", v))
        page.add_row("Model", model, "Ollama model name.")

        privacy = QLabel(
            "Cleanup runs entirely on your machine against a local Ollama daemon. "
            "No audio or text is sent to the cloud."
        )
        privacy.setStyleSheet(f"color: {T.Color.TEXT_MUTED}; font-size: {T.Font.SIZE_XS}px;")
        privacy.setWordWrap(True)
        page.add_widget(privacy)

        page.add_stretch()
        return page

    def _build_privacy(self) -> _Section:
        page = _Section("Privacy")

        items = [
            ("Audio retention",
             "Each successful take is written to temp_last_capture.wav (current "
             "directory) and last.wav (%LOCALAPPDATA%\\Voxen). Only the most "
             "recent take is kept. There is no automatic deletion."),
            ("Logging",
             "A rotating log (3 x 2 MB) is written to %LOCALAPPDATA%\\Voxen\\logs. "
             "Raw transcripts are logged at INFO level."),
            ("Network",
             "No telemetry or cloud API. Network is used only to download models "
             "from Hugging Face on first run and to reach a local Ollama daemon "
             "when cleanup is enabled."),
            ("Clipboard",
             "Dictation text is placed on the clipboard briefly to paste, then "
             "the previous clipboard text is restored. Non-text clipboard content "
             "(images, files) may be lost — known limitation."),
        ]
        for title, body in items:
            t = QLabel(title)
            t.setStyleSheet(f"color: {T.Color.TEXT_PRIMARY}; font-weight: 600; font-size: {T.Font.SIZE_SM}px;")
            page.add_widget(t)
            b = QLabel(body)
            b.setStyleSheet(f"color: {T.Color.TEXT_SECONDARY}; font-size: {T.Font.SIZE_SM}px;")
            b.setWordWrap(True)
            page.add_widget(b)
            page.add_widget(QLabel(""))

        page.add_stretch()
        return page

    def _build_about(self) -> _Section:
        page = _Section("About")
        from . import __version__
        info = [
            ("Application", "Voxen"),
            ("Version", __version__),
            ("Processing", "100% local — no cloud, no telemetry"),
            ("STT engine", "faster-whisper (CTranslate2)"),
            ("UI", "PySide6 (Qt 6)"),
        ]
        for k, v in info:
            row = QHBoxLayout()
            kl = QLabel(k)
            kl.setStyleSheet(f"color: {T.Color.TEXT_MUTED}; font-size: {T.Font.SIZE_SM}px;")
            kl.setMinimumWidth(140)
            vl = QLabel(v)
            vl.setStyleSheet(f"color: {T.Color.TEXT_PRIMARY}; font-size: {T.Font.SIZE_SM}px;")
            row.addWidget(kl)
            row.addWidget(vl)
            page.addLayout(row)

        page.add_stretch()
        return page

    # ------------------------------------------------------------------ handlers

    def _on_overlay_toggled(self, checked: bool):
        ui_cfg = self.config.setdefault("ui", {})
        ui_cfg["show_overlay"] = checked
        if self._on_overlay_toggled:
            self._on_overlay_toggled(checked)

    def _on_mode_changed(self, mode: str):
        self.config.setdefault("hotkey", {})["mode"] = mode
        if self._on_mode_changed:
            self._on_mode_changed(mode)

    def _on_mic_changed(self, index: int):
        idx = self._mic_combo.itemData(index)
        self.config.setdefault("audio", {})["device_index"] = idx
        if self._on_device_changed:
            self._on_device_changed(idx)

    def _on_cleanup_toggled(self, checked: bool):
        clean_cfg = self.config.setdefault("cleanup", self.config.setdefault("llm", {}))
        clean_cfg["enabled"] = checked
        if self._on_cleanup_toggled:
            self._on_cleanup_toggled(checked)

    def _test_microphone(self):
        self._test_btn.setEnabled(False)
        self._test_result.setText("Recording 2 s...")
        self._test_recorder = AudioRecorder(
            sample_rate=16000, channels=1, audio_level_callback=self._on_test_level
        )
        self._test_recorder.start_stream()
        ok, msg = self._test_recorder.start_recording()
        if not ok:
            self._test_result.setText(f"Failed: {msg}")
            self._test_btn.setEnabled(True)
            return
        self._test_timer = QTimer(self)
        self._test_timer.setSingleShot(True)
        self._test_timer.timeout.connect(self._finish_mic_test)
        self._test_timer.start(2000)

    def _on_test_level(self, rms: float, sub_levels=None):
        self._level_bar.set_level(rms)

    def _finish_mic_test(self):
        audio, dur, status = self._test_recorder.stop_recording()
        self._test_recorder.close()
        self._test_recorder = None
        if status.get("success"):
            rms = status.get("raw_rms", 0.0)
            self._test_result.setText(f"Captured {dur:.1f}s, RMS {rms:.4f} on {status.get('device', '?')}")
        else:
            self._test_result.setText(f"Failed: {status.get('error', 'unknown')}")
        self._test_btn.setEnabled(True)

    def _load_values(self):
        pass

    def closeEvent(self, event):
        if self._test_recorder is not None:
            self._test_recorder.close()
            self._test_recorder = None
        super().closeEvent(event)

    def _btn_style(self) -> str:
        return (
            f"QPushButton {{ background: {T.Color.ACCENT}; color: white; "
            f"border: none; border-radius: {T.Radius.SM}px; "
            f"padding: {T.Space.SM}px {T.Space.LG}px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {T.Color.ACCENT_HOVER}; }}"
            f"QPushButton:disabled {{ background: {T.Color.ELEVATED}; color: {T.Color.TEXT_MUTED}; }}"
        )

    def _on_audio_level(self, rms: float, sub_levels=None):
        self._level_bar.set_level(rms)


class _LevelBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(12)
        self.setMinimumWidth(120)
        self._level = 0.0

    def set_level(self, level: float):
        self._level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        painter.setBrush(QColor(T.Color.ELEVATED))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, w, h, 4, 4)
        if self._level > 0:
            bar_w = int(w * self._level)
            painter.setBrush(QColor(T.Color.ACCENT))
            painter.drawRoundedRect(0, 0, bar_w, h, 4, 4)
        painter.end()

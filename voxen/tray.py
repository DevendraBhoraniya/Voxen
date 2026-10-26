"""
System tray icon and context menu module for Voxen.
Provides tray notifications, live audio device switching, hotkey mode toggles,
model selection, settings access, and persistent status reminders.
"""

import os
import sys
import logging
from typing import Callable, Dict, List, Optional
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QAction, QActionGroup

from .audio import list_audio_devices

logger = logging.getLogger("Voxen.Tray")


def create_tray_icon_pixmap(recording: bool = False) -> QPixmap:
    """Draw a waveform glyph: 5 bars of increasing then decreasing height."""
    size = 32
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    try:
        from .overlay import COLORS as _C
        color = QColor(_C["record"] if recording else _C["accent"])
    except Exception:
        color = QColor("#FF4D6D" if recording else "#6B5EFF")

    painter.setBrush(QBrush(color))
    painter.setPen(Qt.NoPen)

    heights = [6, 12, 20, 12, 6]
    bar_w = 3
    gap = 3
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    start_x = (size - total) // 2
    for i, h in enumerate(heights):
        x = start_x + i * (bar_w + gap)
        y = (size - h) // 2
        painter.drawRoundedRect(x, y, bar_w, h, 1.5, 1.5)

    painter.end()
    return pixmap


class SystemTrayManager(QObject):
    """Manages the Windows taskbar system tray icon and interactive menu."""

    toggle_recording_requested = Signal()
    mode_changed = Signal(str)            # "toggle" or "hold"
    device_changed = Signal(object)       # int or None
    model_changed = Signal(str)           # "tiny.en", "base.en"
    cleanup_toggled = Signal(bool)
    overlay_toggled = Signal(bool)
    quit_requested = Signal()

    def __init__(
        self,
        config: Dict,
        on_open_config: Optional[Callable[[], None]] = None
    ):
        super().__init__()
        self.config = config
        self.on_open_config = on_open_config

        self.tray_icon = QSystemTrayIcon()
        self.icon_idle = QIcon(create_tray_icon_pixmap(recording=False))
        self.icon_recording = QIcon(create_tray_icon_pixmap(recording=True))

        self.tray_icon.setIcon(self.icon_idle)
        self._update_tooltip(False)

        self.menu = QMenu()
        self._build_menu()
        self.tray_icon.setContextMenu(self.menu)

        # Left-click on tray icon toggles recording
        self.tray_icon.activated.connect(self._on_tray_activated)

    def show(self):
        self.tray_icon.show()

    def hide(self):
        self.tray_icon.hide()

    def _update_tooltip(self, recording: bool):
        status = "Recording..." if recording else "Ready"
        combo = self.config.get("hotkey", {}).get("combination", "ctrl+shift").upper()
        mode = self.config.get("hotkey", {}).get("mode", "toggle").title()
        self.tray_icon.setToolTip(f"Voxen: {status}\nHotkey: {combo} ({mode} mode)")

    def set_recording_state(self, is_recording: bool):
        """Update tray icon when recording state changes."""
        self.tray_icon.setIcon(self.icon_recording if is_recording else self.icon_idle)
        self._update_tooltip(is_recording)
        if hasattr(self, "act_header"):
            label = "Recording" if is_recording else "Ready"
            self.act_header.setText("Voxen  \u2022  " + label)

        # Update action text in menu
        combo = self.config.get("hotkey", {}).get("combination", "ctrl+shift").upper()
        if is_recording:
            self.action_toggle.setText(f"Stop dictation\t{combo}")
        else:
            self.action_toggle.setText(f"Start dictation\t{combo}")

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # Single click
            self.toggle_recording_requested.emit()

    def _build_menu(self):
        self.menu.clear()

        combo = self.config.get("hotkey", {}).get("combination", "ctrl+shift").upper()

        # 1. Status header (non-clickable)
        status = "Recording" if False else "Ready"
        self.act_header = QAction(f"Voxen  \u2022  {status}", self.menu)
        act_header = self.act_header
        act_header.setEnabled(False)
        self.menu.addAction(act_header)

        self.menu.addSeparator()

        # 2. Toggle action with hotkey suffix
        self.action_toggle = QAction(f"Start dictation\t{combo}", self.menu)
        self.action_toggle.triggered.connect(self.toggle_recording_requested.emit)
        self.menu.addAction(self.action_toggle)

        self.menu.addSeparator()

        # 3. Hotkey Mode Selection (Toggle vs Hold)
        mode_menu = self.menu.addMenu("Hotkey mode")
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)

        current_mode = self.config.get("hotkey", {}).get("mode", "toggle").lower()

        act_toggle = QAction("Toggle mode", mode_menu, checkable=True)
        act_toggle.setChecked(current_mode == "toggle")
        act_toggle.triggered.connect(lambda: self._on_mode_selected("toggle"))
        self.mode_group.addAction(act_toggle)
        mode_menu.addAction(act_toggle)

        act_hold = QAction("Hold to talk", mode_menu, checkable=True)
        act_hold.setChecked(current_mode == "hold")
        act_hold.triggered.connect(lambda: self._on_mode_selected("hold"))
        self.mode_group.addAction(act_hold)
        mode_menu.addAction(act_hold)

        # 4. Microphone Device Selector Submenu
        self.device_menu = self.menu.addMenu("Microphone")
        self.device_menu.aboutToShow.connect(self._refresh_audio_devices)
        self._refresh_audio_devices()

        # 5. Model Selector Submenu
        model_menu = self.menu.addMenu("Speech model")
        self.model_group = QActionGroup(self)
        self.model_group.setExclusive(True)

        stt_cfg = self.config.get("stt") or self.config.get("transcription", {})
        engine = str(stt_cfg.get("engine", "faster-whisper")).lower().strip()
        if engine in ("whistle", "cactus", "cactus-compute", "whistle.cact"):
            current_model_key = "whistle"
        elif engine in ("moonshine", "moonshine-base", "moonshine-medium"):
            current_model_key = "moonshine"
        else:
            current_model_key = stt_cfg.get("model") or stt_cfg.get("model_name", "base.en")

        available_models = [
            ("whistle", "whistle.cact (Cactus Compute - 16.9 MB CPU, ~0.6s) [Fastest]"),
            ("moonshine", "moonshine-base (Useful Sensors - ONNX CPU, ~0.59s)"),
            ("distil-small.en", "distil-small.en (Distilled Small - available, slower on this CPU)"),
            ("base.en", "base.en (Standard Whisper - ~1.5s CPU) [Recommended Default]"),
            ("small.en", "small.en (4.4% WER, ~1.5s GPU) [Recommended Accuracy]"),
            ("distil-large-v3", "distil-large-v3 (Distilled Large - ~3s GPU)"),
            ("large-v3-turbo", "large-v3-turbo (Flagship Mac Model - ~3-6s GPU)"),
            ("tiny.en", "tiny.en (Lightweight Whisper - ~0.4s GPU)"),
        ]

        for m_id, m_label in available_models:
            act_m = QAction(m_label, model_menu, checkable=True)
            act_m.setChecked(current_model_key == m_id)
            act_m.triggered.connect(lambda checked, m=m_id: self.model_changed.emit(m))
            self.model_group.addAction(act_m)
            model_menu.addAction(act_m)

        # 6. Cleanup Toggle
        cleanup_cfg = self.config.get("cleanup", {})
        self.act_cleanup = QAction("Ollama text cleanup", self.menu, checkable=True)
        self.act_cleanup.setChecked(cleanup_cfg.get("enabled", False))
        self.act_cleanup.toggled.connect(self.cleanup_toggled.emit)
        self.menu.addAction(self.act_cleanup)

        # 7. Overlay Toggle
        ui_cfg = self.config.get("ui", {})
        self.act_overlay = QAction("Show floating pill", self.menu, checkable=True)
        self.act_overlay.setChecked(ui_cfg.get("show_overlay", True))
        self.act_overlay.toggled.connect(self.overlay_toggled.emit)
        self.menu.addAction(self.act_overlay)

        self.menu.addSeparator()

        # 8. Settings / Config File
        if self.on_open_config:
            act_config = QAction("Settings...", self.menu)
            act_config.triggered.connect(self.on_open_config)
            self.menu.addAction(act_config)

        self.menu.addSeparator()

        # 9. Quit
        act_quit = QAction("Quit", self.menu)
        act_quit.triggered.connect(self.quit_requested.emit)
        self.menu.addAction(act_quit)

    def _on_mode_selected(self, mode: str):
        self.config.setdefault("hotkey", {})["mode"] = mode
        self._update_tooltip(False)
        self.mode_changed.emit(mode)

    def _refresh_audio_devices(self):
        """Populate the audio devices submenu."""
        self.device_menu.clear()
        dev_group = QActionGroup(self)
        dev_group.setExclusive(True)

        current_idx = self.config.get("audio", {}).get("device_index", None)

        # System Default option
        act_default = QAction("System Default Microphone", self.device_menu, checkable=True)
        act_default.setChecked(current_idx is None)
        act_default.triggered.connect(lambda: self.device_changed.emit(None))
        dev_group.addAction(act_default)
        self.device_menu.addAction(act_default)

        devices = list_audio_devices(refresh=True)
        for d in devices:
            name = f"Mic {d['index']}: {d['name'][:34]}"
            act = QAction(name, self.device_menu, checkable=True)
            act.setChecked(current_idx == d["index"])
            idx = d["index"]
            act.triggered.connect(lambda checked, idx=idx: self.device_changed.emit(idx))
            dev_group.addAction(act)
            self.device_menu.addAction(act)

    def show_message(self, title: str, message: str, icon=QSystemTrayIcon.Information):
        """Display Windows native toast / balloon notification."""
        self.tray_icon.showMessage(title, message, icon, 2500)

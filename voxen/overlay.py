"""
Floating recording pill overlay module for Voxen.
Built with PySide6, frameless, translucent, non-focus-stealing (WS_EX_NOACTIVATE & MA_NOACTIVATE),
featuring a real microphone-driven waveform visualizer, distinct recording/processing states,
multi-monitor DPI auto-centering, and idle auto-hide.
"""

import sys
import math
import time
import ctypes
import logging
from typing import List, Optional, Union

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QPoint, QRectF, QAbstractNativeEventFilter
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QGraphicsDropShadowEffect, QApplication
from PySide6.QtGui import QColor, QPainter, QBrush, QPen, QFont, QGuiApplication, QLinearGradient

logger = logging.getLogger("Voxen.Overlay")

# Baseline palette (matches the original Voxen design)
COLORS = {
    "accent":       "#A36BFF",
    "accent_dim":   "#2A2550",
    "record":       "#FF4D6D",
    "success":      "#34C759",
    "text_primary": "#FFFFFF",
    "text_muted":   "#999999",
    "waveform":     "#A36BFF",
    "waveform_dim": "#2A2550",
}

# Win32 Constants for non-activating window
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3


class RealMicWaveformVisualizer(QWidget):
    """
    Renders 24 vertical waveform bars driven strictly by real microphone samples.
    Uses interpolation + natural decay for smooth, jitter-free motion.
    """

    _VARIATION = [0.92, 1.05, 0.97, 1.08, 0.94, 1.03, 0.99, 1.06, 0.95, 1.02,
                  0.98, 1.04, 0.96, 1.01, 0.93, 1.07, 0.99, 1.05, 0.96, 1.02,
                  0.97, 1.03, 0.95, 1.01]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(228, 32)
        self.num_bars = 24
        self.levels = [0.02] * self.num_bars
        self._target_levels = [0.02] * self.num_bars
        self._last_audio_ts = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._step_decay)

    def start(self):
        self._last_audio_ts = time.time()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.levels = [0.02] * self.num_bars
        self._target_levels = [0.02] * self.num_bars
        self.update()

    def update_real_audio(self, overall_rms: float, sub_levels=None):
        now = time.time()
        self._last_audio_ts = now
        if sub_levels and len(sub_levels) == self.num_bars:
            for i in range(self.num_bars):
                raw = sub_levels[i]
                var = self._VARIATION[i % len(self._VARIATION)]
                self._target_levels[i] = max(0.02, min(1.0, raw * 2.2 * var))
        else:
            for i in range(self.num_bars):
                dist = abs(i - (self.num_bars - 1) / 2.0) / (self.num_bars / 2.0)
                factor = 1.0 - 0.5 * dist
                var = self._VARIATION[i % len(self._VARIATION)]
                self._target_levels[i] = max(0.02, min(1.0, overall_rms * factor * 3.5 * var))

        for i in range(self.num_bars):
            tgt = self._target_levels[i]
            if tgt > self.levels[i]:
                self.levels[i] += (tgt - self.levels[i]) * 0.45

    def _step_decay(self):
        changed = False
        stale = (time.time() - self._last_audio_ts) > 0.12
        for i in range(self.num_bars):
            target = self._target_levels[i]
            if stale:
                target = max(0.02, target * 0.85)
                self._target_levels[i] = target
            diff = target - self.levels[i]
            if abs(diff) > 0.001:
                self.levels[i] += diff * 0.35
                changed = True
            if self.levels[i] > 0.02:
                self.levels[i] = max(0.02, self.levels[i] - 0.004)
                changed = True

        if changed or any(lvl > 0.03 for lvl in self.levels):
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        bar_w = 6.0
        gap = 3.0
        total_w = self.num_bars * bar_w + (self.num_bars - 1) * gap
        start_x = (w - total_w) / 2.0

        purple_brush = QBrush(QColor(163, 107, 255))  # #A36BFF from reference
        painter.setBrush(purple_brush)
        painter.setPen(Qt.NoPen)

        for i in range(self.num_bars):
            lvl = self.levels[i]
            # Height between 4px and max height
            bar_h = max(4.0, lvl * (h - 4.0))
            x = start_x + i * (bar_w + gap)
            y = (h - bar_h) / 2.0
            painter.drawRoundedRect(QRectF(x, y, bar_w, bar_h), bar_w / 2.0, bar_w / 2.0)


class LightweightSpinner(QWidget):
    """Minimal, low-overhead rotating arc indicator for processing state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)  # 25 fps
        self._timer.timeout.connect(self._rotate)

    def start(self):
        self.angle = 0
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _rotate(self):
        self.angle = (self.angle + 24) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(0, 160, 255), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(QRectF(2, 2, 12, 12), -self.angle * 16, 260 * 16)


class FloatingPill(QWidget):
    """
    Polished floating pill overlay shown strictly during recording and processing.
    - Stays completely hidden when idle.
    - WS_EX_NOACTIVATE & MA_NOACTIVATE prevent stealing focus from target typing apps.
    - Handles monitor geometry, DPI scaling, and screen resolution changes.
    """

    # Thread-safe Qt signals
    state_signal = Signal(str, str)          # (state_name, display_text)
    audio_signal = Signal(float, object)     # (rms_float, optional_sub_levels_list)
    hide_signal = Signal()

    def __init__(self, position: str = "bottom_center", margin_bottom: int = 48):
        super().__init__()
        self.position_mode = position
        self.margin_bottom = margin_bottom
        self._drag_pos = QPoint()
        self._is_recording = False
        self._record_start_time = 0.0

        self._init_window_flags()
        self._init_ui()
        self._connect_signals()
        self._connect_screen_events()

        # Auto-hide timer for completed/error states
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

        # Elapsed recording timer (updates second counter: 0:02, 0:03...)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(500)
        self._elapsed_timer.timeout.connect(self._update_elapsed_time)

        # Ensure pill starts completely hidden when idle
        self.hide()

    def _init_window_flags(self):
        """Set frameless, always-on-top, non-activating window flags."""
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

    def showEvent(self, event):
        """Re-assert Windows extended styles on every show."""
        super().showEvent(event)
        self._apply_win32_noactivate()

    def _apply_win32_noactivate(self):
        """Apply Windows extended styles so clicks or shows never steal cursor focus."""
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
            )
        except Exception as e:
            logger.debug(f"Could not apply WS_EX_NOACTIVATE: {e}")

    def nativeEvent(self, eventType, message):
        """Intercept WM_MOUSEACTIVATE so mouse clicks never activate or steal focus."""
        if eventType == b"windows_generic_MSG":
            try:
                # MSG pointer unpacking
                msg_val = ctypes.wintypes.MSG.from_address(message.__int__())
                if msg_val.message == WM_MOUSEACTIVATE:
                    return True, MA_NOACTIVATE
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def _init_ui(self):
        self.setFixedHeight(40)
        self.setMinimumWidth(210)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 5, 14, 5)
        layout.setSpacing(9)

        # 1. State indicator (red recording dot or spinner)
        self.status_dot = QLabel(self)
        self.status_dot.setFixedSize(9, 9)
        self.status_dot.setStyleSheet("background-color: #ff3b30; border-radius: 4px;")

        self.spinner = LightweightSpinner(self)
        self.spinner.hide()

        # 2. Status & timer label
        self.label = QLabel("Listening", self)
        font = QFont("Segoe UI", 9, QFont.DemiBold)
        self.label.setFont(font)
        self.label.setStyleSheet("color: #FFFFFF;")

        self.timer_label = QLabel("", self)
        timer_font = QFont("Segoe UI", 8, QFont.Normal)
        self.timer_label.setFont(timer_font)
        self.timer_label.setStyleSheet("color: #999999; margin-left: -2px;")

        # 3. Real live waveform visualizer
        self.visualizer = RealMicWaveformVisualizer(self)

        layout.addWidget(self.status_dot)
        layout.addWidget(self.spinner)
        layout.addWidget(self.label)
        layout.addWidget(self.timer_label)
        layout.addStretch()
        layout.addWidget(self.visualizer)

        self._reposition_bottom_center()

    def _connect_signals(self):
        self.state_signal.connect(self._handle_state)
        self.audio_signal.connect(self._on_audio_data)
        self.hide_signal.connect(self._handle_hide)

    def _connect_screen_events(self):
        """Listen to screen geometry changes and DPI updates across monitors."""
        app = QGuiApplication.instance()
        if app:
            app.screenAdded.connect(self._on_screens_changed)
            app.screenRemoved.connect(self._on_screens_changed)
            app.primaryScreenChanged.connect(self._on_screens_changed)

        screen = QGuiApplication.primaryScreen()
        if screen:
            screen.geometryChanged.connect(self._reposition_bottom_center)
            screen.logicalDotsPerInchChanged.connect(self._reposition_bottom_center)

    def _on_screens_changed(self, *args):
        self._reposition_bottom_center()

    def _reposition_bottom_center(self):
        """Position pill centered at the bottom of the active/primary display."""
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        w = max(220, self.sizeHint().width())
        h = 40
        self.resize(w, h)

        x = geo.x() + (geo.width() - w) // 2
        y = geo.y() + geo.height() - h - self.margin_bottom
        self.move(x, y)

    @Slot(float, object)
    def _on_audio_data(self, rms: float, sub_levels: Optional[List[float]]):
        """Forward real mic samples to waveform."""
        if self._is_recording and self.isVisible():
            self.visualizer.update_real_audio(rms, sub_levels)

    def _update_elapsed_time(self):
        if self._is_recording:
            elapsed = int(time.time() - self._record_start_time)
            mins = elapsed // 60
            secs = elapsed % 60
            self.timer_label.setText(f"{mins}:{secs:02d}")
            self.adjustSize()

    @Slot(str, str)
    def _handle_state(self, state: str, text: str):
        """Update pill UI based on state machine."""
        self._hide_timer.stop()
        self.label.setText(text)

        if state == "RECORDING":
            self._is_recording = True
            self._record_start_time = time.time()
            self.timer_label.setText("0:00")
            self.timer_label.show()
            self._elapsed_timer.start()

            self.status_dot.show()
            self.status_dot.setStyleSheet(
                "background-color: #ff3b30; border-radius: 4px;"
            )
            self.spinner.stop()
            self.spinner.hide()

            self.visualizer.start()
            self.visualizer.show()

            self.adjustSize()
            self._reposition_bottom_center()
            self.show()

        elif state in ("TRANSCRIBING", "CLEANING"):
            self._is_recording = False
            self._elapsed_timer.stop()
            self.timer_label.hide()

            self.status_dot.hide()
            self.spinner.show()
            self.spinner.start()

            self.visualizer.stop()
            self.visualizer.hide()

            self.adjustSize()
            self._reposition_bottom_center()
            self.show()

        elif state == "SUCCESS":
            self._is_recording = False
            self._elapsed_timer.stop()
            self.timer_label.hide()

            self.spinner.stop()
            self.spinner.hide()
            self.status_dot.show()
            self.status_dot.setStyleSheet(
                "background-color: #34c759; border-radius: 4px;"
            )
            self.visualizer.stop()
            self.visualizer.hide()

            self.adjustSize()
            self._reposition_bottom_center()
            self.show()
            self._hide_timer.start(800)  # Auto-hide when idle

        elif state in ("SILENT", "ERROR"):
            self._is_recording = False
            self._elapsed_timer.stop()
            self.timer_label.hide()

            self.spinner.stop()
            self.spinner.hide()
            self.status_dot.show()
            self.status_dot.setStyleSheet(
                "background-color: #ff9500; border-radius: 4px;"
            )
            self.visualizer.stop()
            self.visualizer.hide()

            self.adjustSize()
            self._reposition_bottom_center()
            self.show()
            self._hide_timer.start(1200)  # Auto-hide when idle

        elif state == "IDLE":
            self._handle_hide()

    @Slot()
    def _handle_hide(self):
        """Hide pill completely when idle."""
        self._is_recording = False
        self._elapsed_timer.stop()
        self.visualizer.stop()
        self.spinner.stop()
        self.hide()

    def paintEvent(self, event):
        """Draw obsidian frosted glass rounded capsule."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect()
        # Dark obsidian acrylic
        painter.setBrush(QBrush(QColor(18, 18, 22, 235)))
        # Subtle glass highlight border
        painter.setPen(QPen(QColor(255, 255, 255, 32), 1.0))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 19.0, 19.0)

    # Allow dragging with mouse if user desires custom placement
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and not self._drag_pos.isNull():
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()


# Alias matching reference repository
Overlay = FloatingPill

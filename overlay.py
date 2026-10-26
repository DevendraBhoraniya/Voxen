"""
Floating recording pill overlay module for Voxen.
Premium minimal pill: idle dot, recording waveform, transcribing shimmer,
success checkmark, error shake. No text state labels.
"""

import sys
import math
import time
import ctypes
import ctypes.wintypes
import logging
from typing import List, Optional

from PySide6.QtCore import (
    Qt, QTimer, Signal, Slot, QPoint, QRectF, QPropertyAnimation, QEasingCurve,
)
from PySide6.QtWidgets import QWidget, QApplication
from PySide6.QtGui import (
    QColor, QPainter, QBrush, QPen, QFont, QGuiApplication, QLinearGradient,
    QFontDatabase,
)

logger = logging.getLogger("Voxen.Overlay")

# ---- design tokens ----
COLORS = {
    "bg":           "#0E0E10",
    "surface":      "#1A1A1F",
    "border":       "#2A2A35",
    "accent":       "#6B5EFF",
    "accent_dim":   "#3D3580",
    "record":       "#FF4D6D",
    "success":      "#3DFF9A",
    "text_primary": "#F0F0F5",
    "text_muted":   "#5A5A6E",
    "waveform":     "#6B5EFF",
    "waveform_dim": "#2A2550",
}
FONT_FAMILY = "Inter"
PILL_RADIUS = 28
PILL_H = 56
PILL_W_IDLE = 48
PILL_W_WIDE = 320

# Win32
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3


def set_click_through(hwnd, enable: bool):
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enable:
        style |= WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def load_font_family() -> str:
    families = QFontDatabase.families()
    if FONT_FAMILY in families:
        return FONT_FAMILY
    if "Segoe UI" in families:
        return "Segoe UI"
    return "Arial"


# ---- waveform visualizer with smoothing ----
class RealMicWaveformVisualizer(QWidget):
    SMOOTHING = 0.6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(232, 32)
        self.num_bars = 24
        self.levels = [0.02] * self.num_bars
        self._target = [0.02] * self.num_bars
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._step)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.levels = [0.02] * self.num_bars
        self._target = [0.02] * self.num_bars
        self.update()

    def update_real_audio(self, overall_rms: float, sub_levels=None):
        if sub_levels and len(sub_levels) == self.num_bars:
            for i in range(self.num_bars):
                raw = sub_levels[i]
                self._target[i] = max(0.02, min(1.0, raw * 2.2))
        else:
            for i in range(self.num_bars):
                dist = abs(i - (self.num_bars - 1) / 2.0) / (self.num_bars / 2.0)
                factor = 1.0 - 0.5 * dist
                self._target[i] = max(0.02, min(1.0, overall_rms * factor * 3.5))

    def _step(self):
        changed = False
        for i in range(self.num_bars):
            raw = self._target[i]
            self.levels[i] = self.SMOOTHING * self.levels[i] + (1 - self.SMOOTHING) * raw
            self._target[i] = max(0.02, self._target[i] - 0.06)
            if self.levels[i] > 0.03:
                changed = True
        if changed or any(l > 0.03 for l in self.levels):
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()
        bar_w = 3.0
        gap = 2.0
        total_w = self.num_bars * bar_w + (self.num_bars - 1) * gap
        start_x = (w - total_w) / 2.0

        for i in range(self.num_bars):
            lvl = self.levels[i]
            bar_h = max(4.0, lvl * 28.0)
            x = start_x + i * (bar_w + gap)
            y = (h - bar_h) / 2.0
            grad = QLinearGradient(x, y, x, y + bar_h)
            grad.setColorAt(0.0, QColor(COLORS["waveform"]))
            grad.setColorAt(1.0, QColor(COLORS["waveform_dim"]))
            painter.setBrush(QBrush(grad))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(QRectF(x, y, bar_w, bar_h), 2.0, 2.0)


# ---- spinner ----
class LightweightSpinner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
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
        pen = QPen(QColor(COLORS["accent"]), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(QRectF(2, 2, 12, 12), -self.angle * 16, 260 * 16)


# ---- shimmer widget ----
class ShimmerBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self._x = -40.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)

    def start(self):
        self._x = -40.0
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _step(self):
        self._x += 2.5
        if self._x > self.parent().width() + 40 if self.parent() else 360:
            self._x = -40.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        x = self._x
        grad = QLinearGradient(x, 0, x + 40, 0)
        grad.setColorAt(0.0, QColor(COLORS["accent"]))
        grad.setColorAt(0.5, QColor(COLORS["accent"]))
        grad.setColorAt(1.0, QColor(COLORS["accent"]))
        c = QColor(COLORS["accent"])
        c.setAlpha(153)  # 60%
        painter.setBrush(QBrush(c))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(x, 0, 40, 4), 2, 2)


# ---- main pill ----
class FloatingPill(QWidget):
    state_signal = Signal(str, str)
    audio_signal = Signal(float, object)
    hide_signal = Signal()

    def __init__(self, position: str = "bottom_center", margin_bottom: int = 48):
        super().__init__()
        self.position_mode = position
        self.margin_bottom = margin_bottom
        self._drag_pos = QPoint()
        self._is_recording = False
        self._record_start_time = 0.0
        self._state = "IDLE"
        self._pulse = 0.0
        self._pulse_dir = 1
        self._check_timer = None
        self._error_timer = None
        self._shake_timer = None
        self._shake_offset = 0
        self._shake_step = 0
        self._font = load_font_family()

        self._init_window_flags()
        self._init_ui()
        self._connect_signals()
        self._connect_screen_events()

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(50)
        self._pulse_timer.timeout.connect(self._pulse_step)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

        # click-through initially (idle)
        self._apply_win32_noactivate()
        QTimer.singleShot(0, lambda: set_click_through(int(self.winId()), True))

        self._reposition()
        self.show()
        self._set_state("IDLE")

    def _init_window_flags(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_win32_noactivate()

    def _apply_win32_noactivate(self):
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW)
        except Exception as e:
            logger.debug(f"Could not apply WS_EX_NOACTIVATE: {e}")

    def nativeEvent(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            try:
                msg_val = ctypes.wintypes.MSG.from_address(message.__int__())
                if msg_val.message == WM_MOUSEACTIVATE:
                    return True, MA_NOACTIVATE
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def _init_ui(self):
        self.setFixedHeight(PILL_H)
        self._width = PILL_W_IDLE
        self.resize(self._width, PILL_H)

        self.visualizer = RealMicWaveformVisualizer(self)
        self.visualizer.hide()

        self.spinner = LightweightSpinner(self)
        self.spinner.hide()

        self.shimmer = ShimmerBar(self)
        self.shimmer.hide()

        self._reposition()

    def _connect_signals(self):
        self.state_signal.connect(self._handle_state)
        self.audio_signal.connect(self._on_audio_data)
        self.hide_signal.connect(self._handle_hide)

    def _connect_screen_events(self):
        app = QGuiApplication.instance()
        if app:
            app.screenAdded.connect(self._reposition)
            app.screenRemoved.connect(self._reposition)
            app.primaryScreenChanged.connect(self._reposition)
        screen = QGuiApplication.primaryScreen()
        if screen:
            screen.geometryChanged.connect(self._reposition)
            screen.logicalDotsPerInchChanged.connect(self._reposition)

    def _reposition(self, *args):
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        w = self.width()
        h = PILL_H
        x = geo.x() + (geo.width() - w) // 2
        y = geo.y() + geo.height() - h - self.margin_bottom
        self.move(x, y)

    def _animate_width(self, target: int, duration: int = 200):
        anim = QPropertyAnimation(self, b"minimumWidth")
        anim.setDuration(duration)
        anim.setStartValue(self.width())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(lambda v: (self.resize(int(v), PILL_H), self._reposition()))
        anim.start()
        self._width_anim = anim

    def _on_audio_data(self, rms: float, sub_levels=None):
        if self._is_recording:
            self.visualizer.update_real_audio(rms, sub_levels)

    def _update_elapsed(self):
        if self._is_recording:
            elapsed = int(time.time() - self._record_start_time)
            self._elapsed_text = f"{elapsed // 60}:{elapsed % 60:02d}"
            self.update()

    def _pulse_step(self):
        self._pulse += 0.08 * self._pulse_dir
        if self._pulse >= 1.0:
            self._pulse = 1.0
            self._pulse_dir = -1
        elif self._pulse <= 0.0:
            self._pulse = 0.0
            self._pulse_dir = 1
        self.update()

    def _set_state(self, state: str):
        self._state = state
        self._is_recording = (state == "RECORDING")

        # click-through when idle
        try:
            set_click_through(int(self.winId()), state == "IDLE")
        except Exception:
            pass

        if state == "RECORDING":
            self._pulse = 0.0
            self._pulse_dir = 1
            self._pulse_timer.start()
            self._elapsed_timer.start()
            self._record_start_time = time.time()
            self._elapsed_text = "0:00"
            self.visualizer.start()
            self.visualizer.show()
            self.spinner.hide()
            self.spinner.stop()
            self.shimmer.hide()
            self.shimmer.stop()
            self._animate_width(PILL_W_WIDE, 200)

        elif state in ("TRANSCRIBING", "CLEANING"):
            self._pulse_timer.stop()
            self._elapsed_timer.stop()
            self.visualizer.hide()
            self.visualizer.stop()
            self.spinner.show()
            self.spinner.start()
            self.shimmer.show()
            self.shimmer.start()
            self.shimmer.setFixedWidth(PILL_W_WIDE)
            self._animate_width(PILL_W_WIDE, 200)

        elif state == "SUCCESS":
            self._pulse_timer.stop()
            self._elapsed_timer.stop()
            self.visualizer.hide()
            self.visualizer.stop()
            self.spinner.hide()
            self.spinner.stop()
            self.shimmer.hide()
            self.shimmer.stop()
            self._success = True
            self._animate_width(PILL_W_IDLE, 150)
            QTimer.singleShot(800, self._fade_to_idle)

        elif state == "ERROR":
            self._pulse_timer.stop()
            self._elapsed_timer.stop()
            self.visualizer.hide()
            self.visualizer.stop()
            self.spinner.hide()
            self.spinner.stop()
            self.shimmer.hide()
            self.shimmer.stop()
            self._error = True
            self._shake_step = 0
            self._shake_timer = QTimer(self)
            self._shake_timer.setInterval(50)
            self._shake_timer.timeout.connect(self._shake_step_fn)
            self._shake_timer.start()
            QTimer.singleShot(1200, self._fade_to_idle)

        elif state == "SILENT":
            self._fade_to_idle()

        else:  # IDLE
            self._pulse_timer.stop()
            self._elapsed_timer.stop()
            self.visualizer.hide()
            self.visualizer.stop()
            self.spinner.hide()
            self.spinner.stop()
            self.shimmer.hide()
            self.shimmer.stop()
            self._success = False
            self._error = False
            self._animate_width(PILL_W_IDLE, 150)

    def _fade_to_idle(self):
        self._success = False
        self._error = False
        if self._shake_timer:
            self._shake_timer.stop()
            self._shake_timer = None
        self._set_state("IDLE")

    def _shake_step_fn(self):
        if self._shake_step >= 6:
            self._shake_timer.stop()
            self._shake_timer = None
            self._shake_offset = 0
            self.update()
            return
        self._shake_offset = 6 if (self._shake_step % 2 == 0) else -6
        self._shake_step += 1
        self.update()

    @Slot(str, str)
    def _handle_state(self, state: str, text: str):
        self._set_state(state)

    @Slot(float, object)
    def _on_audio_data(self, rms: float, sub_levels=None):
        if self._is_recording:
            self.visualizer.update_real_audio(rms, sub_levels)

    @Slot()
    def _handle_hide(self):
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = PILL_H
        r = PILL_RADIUS

        # shadow
        # (approximated via layered rects not needed; keep simple)
        # background
        bg = QColor(COLORS["surface"])
        bg.setAlpha(217)  # 85%
        painter.setBrush(QBrush(bg))
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        rect = QRectF(0.5, 0.5, w - 1, h - 1)
        painter.drawRoundedRect(rect, r, r)

        # recording border glow
        if self._state == "RECORDING":
            painter.setPen(QPen(QColor(COLORS["accent_dim"]), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, r, r)

        # shake offset apply via translate (visual only)
        cx = w // 2 + self._shake_offset
        cy = h // 2

        if self._state == "RECORDING":
            # red pulsing dot left
            scale = 1.0 + 0.4 * self._pulse
            dot_r = 5 * scale
            painter.setBrush(QBrush(QColor(COLORS["record"])))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRectF(18 - dot_r, cy - dot_r, dot_r * 2, dot_r * 2))
            # waveform center
            self.visualizer.move((w - self.visualizer.width()) // 2, (h - self.visualizer.height()) // 2)
            # elapsed time right
            font = QFont(self._font, 11)
            font.setStyleStrategy(QFont.PreferAntialias)
            painter.setFont(font)
            painter.setPen(QColor(COLORS["text_muted"]))
            painter.drawText(QRectF(w - 52, 0, 48, h), Qt.AlignVCenter | Qt.AlignRight, self._elapsed_text)

        elif self._state in ("TRANSCRIBING", "CLEANING"):
            # spinner left
            self.spinner.move(18, (h - 16) // 2)
            # shimmer
            self.shimmer.move(0, (h - 4) // 2)
            self.shimmer.setFixedWidth(w)

        elif self._state == "SUCCESS":
            # checkmark
            font = QFont(self._font, 20, QFont.Bold)
            painter.setFont(font)
            painter.setPen(QColor(COLORS["success"]))
            painter.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "✓")

        elif self._state == "ERROR":
            font = QFont(self._font, 20, QFont.Bold)
            painter.setFont(font)
            painter.setPen(QColor(COLORS["record"]))
            painter.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "×")

        elif self._state == "IDLE":
            # small accent dot
            dot_r = 4
            glow = QColor(COLORS["accent"])
            glow.setAlpha(102)  # 40%
            painter.setBrush(QBrush(glow))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRectF(cx - dot_r - 2, cy - dot_r - 2, (dot_r + 2) * 2, (dot_r + 2) * 2))
            painter.setBrush(QBrush(QColor(COLORS["accent"])))
            painter.drawEllipse(QRectF(cx - dot_r, cy - dot_r, dot_r * 2, dot_r * 2))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._state != "IDLE":
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and not self._drag_pos.isNull() and self._state != "IDLE":
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

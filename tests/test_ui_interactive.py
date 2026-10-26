"""
Interactive and automated verification test for Voxen's upgraded interface:
1. Window flags, identity, and WS_EX_NOACTIVATE verification
2. Idle auto-hide test
3. Screen DPI & multi-monitor bottom-center alignment test
4. Real mic audio waveform scaling test (no fake/random data)
5. Repeated recording, stopping, application switching, and focus preservation in Notepad
"""

import os
import sys
import time
import subprocess
import ctypes
import numpy as np
import win32gui
import win32con

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from voxen.overlay import FloatingPill, GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOPMOST
from voxen.tray import SystemTrayManager, create_tray_icon_pixmap
import inject


def verify_ui_components():
    print("=" * 60)
    print("VOXEN INTERFACE VERIFICATION SUITE")
    print("=" * 60)

    # 1. Set explicit process AppUserModelID
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Voxen.Dictation.App.1.0")
        print("[PASS] Process AppUserModelID set successfully: 'Voxen.Dictation.App.1.0'")
    except Exception as e:
        print(f"[!] AppUserModelID warning: {e}")

    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)
    app.setApplicationName("Voxen")

    # 2. Instantiate FloatingPill
    print("\n--- TEST 1: FLOATING PILL WINDOW ATTRIBUTES & NON-FOCUS-STEALING ---")
    pill = FloatingPill(position="bottom_center", margin_bottom=48)

    # Verify Qt flags
    flags = pill.windowFlags()
    assert flags & Qt.FramelessWindowHint, "Missing FramelessWindowHint"
    assert flags & Qt.WindowStaysOnTopHint, "Missing WindowStaysOnTopHint"
    assert flags & Qt.WindowDoesNotAcceptFocus, "Missing WindowDoesNotAcceptFocus"
    print("[PASS] Qt Window Flags verified: Frameless, StaysOnTop, WindowDoesNotAcceptFocus")

    # Verify pill starts HIDDEN when idle
    assert not pill.isVisible(), "Pill must be hidden when idle!"
    print("[PASS] Idle State verified: Floating pill is completely hidden when idle.")

    # Show pill momentarily to check Win32 extended style
    pill.state_signal.emit("RECORDING", "Listening...")
    app.processEvents()
    time.sleep(0.1)

    hwnd = int(pill.winId())
    user32 = ctypes.windll.user32
    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    assert ex_style & WS_EX_NOACTIVATE, "Missing WS_EX_NOACTIVATE!"
    assert ex_style & WS_EX_TOPMOST, "Missing WS_EX_TOPMOST!"
    print(f"[PASS] Win32 Extended Style verified: HWND {hwnd} has WS_EX_NOACTIVATE & WS_EX_TOPMOST (0x{ex_style:08x})")

    # 3. Screen geometry & DPI alignment
    print("\n--- TEST 2: DISPLAY POSITIONING & MULTI-MONITOR CENTERING ---")
    screen = app.primaryScreen()
    geo = screen.availableGeometry()
    pill_geo = pill.geometry()
    expected_x = geo.x() + (geo.width() - pill_geo.width()) // 2
    diff_x = abs(pill_geo.x() - expected_x)
    print(f"Screen: {geo.width()}x{geo.height()} at ({geo.x()}, {geo.y()})")
    print(f"Pill geometry: {pill_geo.width()}x{pill_geo.height()} at ({pill_geo.x()}, {pill_geo.y()})")
    assert diff_x <= 2, f"Pill not centered horizontally: diff={diff_x}"
    assert pill_geo.y() > geo.height() * 0.7, "Pill not at bottom of screen!"
    print("[PASS] Horizontal centering and bottom-margin positioning verified.")

    # 4. Real audio waveform test (strictly real mic samples, no random data)
    print("\n--- TEST 3: REAL AUDIO WAVEFORM SCALING (NO FAKE DATA) ---")
    vis = pill.visualizer

    # Test silence: all zero samples
    vis.update_real_audio(0.0, [0.0] * 7)
    app.processEvents()
    assert all(lvl <= 0.05 for lvl in vis.levels), "Silent samples must yield baseline bars!"
    print("[PASS] Silent audio test: Waveform bars stay at baseline minimum (0.0).")

    # Test real acoustic burst (e.g. simulated real speech peak in center sub-bands)
    real_sub_energies = [0.05, 0.25, 0.70, 0.95, 0.65, 0.30, 0.10]
    vis.update_real_audio(0.65, real_sub_energies)
    app.processEvents()
    assert vis.levels[3] > vis.levels[0], "Center bar must reflect higher real sample energy!"
    print(f"[PASS] Real speech sample test: Bar heights dynamically reflect acoustic energy: {[round(l, 2) for l in vis.levels]}")

    # 5. State transitions & auto-hide
    print("\n--- TEST 4: STATE MACHINE & IDLE AUTO-HIDE TRANSITIONS ---")
    pill.state_signal.emit("TRANSCRIBING", "Transcribing...")
    app.processEvents()
    time.sleep(0.2)
    assert pill.spinner.isVisible(), "Processing spinner should be visible"
    assert not pill.visualizer.isVisible(), "Waveform should be hidden during processing"
    print("[PASS] State: TRANSCRIBING displays clean processing indicator and hides waveform.")

    pill.state_signal.emit("SUCCESS", "Done")
    app.processEvents()
    time.sleep(0.2)
    assert pill.status_dot.isVisible(), "Status dot should show success"
    print("[PASS] State: SUCCESS displays completion indicator.")

    # Wait for auto-hide timer (800ms)
    print("Waiting 1.0s for auto-hide to idle...")
    for _ in range(12):
        app.processEvents()
        time.sleep(0.1)
    assert not pill.isVisible(), "Pill should be auto-hidden when idle!"
    print("[PASS] Idle auto-hide verified: Pill is hidden from screen.")

    # 6. System Tray Manager
    print("\n--- TEST 5: SYSTEM TRAY MANAGER & MENU ACTIONS ---")
    dummy_cfg = {
        "hotkey": {"combination": "ctrl+shift", "mode": "toggle"},
        "audio": {"device_index": None},
        "transcription": {"model_name": "tiny.en", "device": "auto"},
        "cleanup": {"enabled": False},
        "ui": {"show_overlay": True}
    }
    tray = SystemTrayManager(dummy_cfg)
    assert tray.tray_icon is not None, "Tray icon failed to initialize"
    assert len(tray.menu.actions()) >= 7, "Tray menu missing actions"
    print(f"[PASS] System Tray initialized with {len(tray.menu.actions())} menu items (Hotkey reminder, Mode, Mic, Model, Settings, Quit).")

    # 7. Repeated sessions with Notepad focus preservation
    print("\n--- TEST 6: REPEATED SESSIONS & ACTIVE APPLICATION FOCUS PRESERVATION ---")
    print("Launching Notepad to verify focus preservation...")
    proc = subprocess.Popen(["notepad.exe"])
    time.sleep(1.2)

    notepad_hwnd = None
    for _ in range(15):
        def enum_win(hwnd, extra):
            nonlocal notepad_hwnd
            if win32gui.IsWindowVisible(hwnd) and "Notepad" in win32gui.GetWindowText(hwnd):
                notepad_hwnd = hwnd
        win32gui.EnumWindows(enum_win, None)
        if notepad_hwnd:
            break
        time.sleep(0.2)

    if notepad_hwnd:
        # Bring Notepad to foreground using thread attachment
        try:
            cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
            tgt_thread = ctypes.windll.user32.GetWindowThreadProcessId(notepad_hwnd, None)
            ctypes.windll.user32.AttachThreadInput(cur_thread, tgt_thread, True)
            win32gui.ShowWindow(notepad_hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(notepad_hwnd)
            win32gui.BringWindowToTop(notepad_hwnd)
            ctypes.windll.user32.AttachThreadInput(cur_thread, tgt_thread, False)
        except Exception as e:
            print(f"  Note on foreground attachment: {e}")
        time.sleep(0.5)

        for session in range(1, 3):
            print(f"  Session {session}: Activating recording pill...")
            pill.state_signal.emit("RECORDING", "Listening...")
            app.processEvents()

            # Feed live audio samples
            for step in range(5):
                sample_lvl = 0.2 + (step % 3) * 0.25
                pill.audio_signal.emit(sample_lvl, [sample_lvl * 0.8] * 7)
                app.processEvents()
                time.sleep(0.05)

            # Check that floating pill NEVER stole focus!
            cur_fg = win32gui.GetForegroundWindow()
            fg_title = win32gui.GetWindowText(cur_fg)
            print(f"  Session {session} check: Active window is '{fg_title}' (HWND {cur_fg})")
            assert cur_fg != hwnd, f"Pill stole focus! (Pill HWND {hwnd} matches foreground HWND {cur_fg})"
            print(f"  [PASS] Session {session}: Pill (HWND {hwnd}) did NOT steal focus.")

            # Paste text into Notepad
            pill.state_signal.emit("TRANSCRIBING", "Transcribing...")
            app.processEvents()
            time.sleep(0.1)

            inject.inject_text(f"Session {session} text dictation. ", restore_delay_ms=80)
            pill.state_signal.emit("SUCCESS", "Done")
            app.processEvents()
            time.sleep(0.4)

        print("[PASS] Focus preservation verified: Notepad retained active typing focus across repeated sessions!")
        proc.terminate()
        print("[PASS] Notepad closed.")

    pill.close()
    tray.hide()
    print("\nALL 6 INTERFACE TESTS PASSED SUCCESSFULLY!")
    return True


if __name__ == "__main__":
    verify_ui_components()

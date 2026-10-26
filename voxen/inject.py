"""
Safe text injection module for Voxen.
Saves user clipboard, injects text via Ctrl+V paste simulation into the focused
Windows application, and restores the original clipboard content seamlessly.
"""

import ctypes
import logging
import time
from typing import Optional
import win32clipboard
import win32con

logger = logging.getLogger("Voxen.Inject")

user32 = ctypes.windll.user32

# Virtual Key Codes
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002


def _open_clipboard_with_retry(max_attempts: int = 10, delay_sec: float = 0.015) -> bool:
    """Open Windows clipboard with retry mechanism in case another app locks it."""
    for attempt in range(max_attempts):
        try:
            win32clipboard.OpenClipboard(0)
            return True
        except Exception:
            time.sleep(delay_sec)
    return False


def get_clipboard_text() -> Optional[str]:
    """Retrieve current clipboard text safely, or None if unavailable."""
    if not _open_clipboard_with_retry():
        return None
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    except Exception as e:
        logger.debug(f"Failed to read clipboard text: {e}")
        return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def set_clipboard_text(text: str) -> bool:
    """Set text into the Windows clipboard safely."""
    if not _open_clipboard_with_retry():
        return False
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        return True
    except Exception as e:
        logger.error(f"Failed to set clipboard text: {e}")
        return False
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def release_modifier_keys():
    """Ensure modifier keys are physically and logically released before pasting."""
    for vk in (VK_SHIFT, VK_CONTROL, VK_MENU):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)


def simulate_paste():
    """Simulate Ctrl+V keyboard shortcut using Windows API."""
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.01)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.01)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def inject_text(
    text: str,
    restore_delay_ms: int = 120,
    post_paste_delay_ms: int = 40
) -> bool:
    """
    Safely inject transcribed text at the currently active typing cursor.
    1. Saves current clipboard text.
    2. Writes dictation text to clipboard.
    3. Releases any lingering hotkey modifiers.
    4. Triggers Ctrl+V paste.
    5. Waits for the target app to process paste.
    6. Restores original clipboard text.
    """
    if not text:
        return False

    # 1. Save previous clipboard text
    prev_text = get_clipboard_text()

    try:
        # 2. Put dictation text on clipboard
        if not set_clipboard_text(text):
            logger.error("Could not write dictation to clipboard.")
            return False

        # 3. Release any modifiers (from Ctrl+Shift hotkey)
        release_modifier_keys()

        # 4. Trigger paste
        simulate_paste()
        time.sleep(post_paste_delay_ms / 1000.0)

        # 5. Wait for target app to consume clipboard
        time.sleep(restore_delay_ms / 1000.0)

        return True

    finally:
        # 6. Restore original clipboard
        if prev_text is not None:
            set_clipboard_text(prev_text)
            logger.debug("Original clipboard content restored.")
        else:
            # If clipboard was empty before, empty it
            if _open_clipboard_with_retry():
                try:
                    win32clipboard.EmptyClipboard()
                except Exception:
                    pass
                finally:
                    win32clipboard.CloseClipboard()

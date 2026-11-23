"""
Safe text injection module for Voxen.
Saves user clipboard, injects text via Ctrl+V paste simulation into the focused
Windows application, and restores the original clipboard content seamlessly.

Data-safety rule: the clipboard is never overwritten when it holds non-text
content (images, file lists, HTML, ...). In that case injection aborts and
returns False without modifying the clipboard.
"""

import ctypes
import logging
import threading
import time
from typing import Optional
import win32clipboard
import win32con

logger = logging.getLogger("Voxen.Inject")

user32 = ctypes.windll.user32

# Serialize all clipboard access to avoid races between concurrent callers.
_clipboard_lock = threading.Lock()

# Virtual Key Codes
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002

# Clipboard formats that count as "plain text" and are safe to overwrite.
_TEXT_FORMATS = {win32con.CF_UNICODETEXT, win32con.CF_TEXT}


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
    """Retrieve current clipboard text safely, or None if unavailable (serialized)."""
    with _clipboard_lock:
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
    """Set text into the Windows clipboard safely (serialized + retried)."""
    with _clipboard_lock:
        for attempt in range(5):
            if not _open_clipboard_with_retry():
                time.sleep(0.01 * (attempt + 1))
                continue
            try:
                try:
                    win32clipboard.EmptyClipboard()
                except Exception:
                    time.sleep(0.01 * (attempt + 1))
                    continue
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                return True
            except Exception as e:
                logger.debug(f"set_clipboard_text attempt {attempt + 1} failed: {e}")
                time.sleep(0.01 * (attempt + 1))
            finally:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        logger.error("Could not write to clipboard after retries.")
        return False


def clipboard_has_non_text() -> bool:
    """
    Return True when the clipboard holds any format besides plain text.

    Used as a guard before overwriting the clipboard: images, file lists
    (CF_HDROP), HTML, and other formats must never be destroyed by dictation.
    Returns True (treat as unsafe) when the clipboard cannot be inspected.
    """
    with _clipboard_lock:
        if not _open_clipboard_with_retry():
            return True
        try:
            fmt = 0
            while True:
                fmt = win32clipboard.EnumClipboardFormats(fmt)
                if fmt == 0:
                    break
                if fmt not in _TEXT_FORMATS:
                    return True
            return False
        except Exception as e:
            logger.debug(f"Failed to enumerate clipboard formats: {e}")
            return True
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
    1. Aborts if the clipboard holds non-text content (no modification).
    2. Saves current clipboard text.
    3. Writes dictation text to clipboard.
    4. Releases any lingering hotkey modifiers.
    5. Triggers Ctrl+V paste.
    6. Waits for the target app to process paste.
    7. Restores original clipboard text (or empties it if it was empty).
    """
    if not text:
        return False

    # Guard: never clobber non-text clipboard content.
    if clipboard_has_non_text():
        logger.warning(
            "Clipboard holds non-text content; aborting injection to avoid data loss."
        )
        return False

    # 1. Save previous clipboard text
    prev_text = get_clipboard_text()

    clipboard_modified = False
    try:
        # 2. Put dictation text on clipboard
        if not set_clipboard_text(text):
            logger.error("Could not write dictation to clipboard.")
            return False
        clipboard_modified = True

        # 3. Release any modifiers (from Ctrl+Shift hotkey)
        release_modifier_keys()

        # Capture foreground window before paste
        hwnd_before = user32.GetForegroundWindow()

        # 4. Trigger paste
        simulate_paste()
        time.sleep(post_paste_delay_ms / 1000.0)

        # 5. Wait for target app to consume clipboard
        time.sleep(restore_delay_ms / 1000.0)

        # 6. Verify foreground window hasn't changed (detects focus loss)
        hwnd_after = user32.GetForegroundWindow()
        if hwnd_before != hwnd_after:
            # Get window titles for diagnostics
            def _get_title(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                return buf.value or "<unknown>"
            title_before = _get_title(hwnd_before)
            title_after = _get_title(hwnd_after)
            logger.warning(
                f"target window changed during paste — injection may have failed "
                f"(before='{title_before}', after='{title_after}'). "
                f"Note: this check detects focus changes only, not paste success "
                f"within the same window."
            )
            return False

        return True

    finally:
        # 7. Restore original clipboard only if we modified it.
        if clipboard_modified:
            if prev_text is not None:
                set_clipboard_text(prev_text)
                logger.debug("Original clipboard content restored.")
            else:
                # If clipboard was empty before, empty it.
                with _clipboard_lock:
                    if _open_clipboard_with_retry():
                        try:
                            win32clipboard.EmptyClipboard()
                        except Exception:
                            pass
                        finally:
                            try:
                                win32clipboard.CloseClipboard()
                            except Exception:
                                pass

"""
Unit tests for voxen.inject using a fake Win32 clipboard.
No real microphone, display, or Notepad is required; the real Windows clipboard
is NOT exercised here.
"""

from unittest.mock import MagicMock, patch

import pytest

import voxen.inject as inject


class FakeClipboard:
    """In-memory stand-in for the Win32 clipboard."""

    def __init__(self):
        self.formats = set()
        self.data = {}
        self.closed = True

    def OpenClipboard(self, hwnd):
        self.closed = False

    def CloseClipboard(self):
        self.closed = True

    def IsClipboardFormatAvailable(self, fmt):
        return fmt in self.formats

    def GetClipboardData(self, fmt):
        return self.data.get(fmt)

    def EnumClipboardFormats(self, fmt):
        # Win32 semantics: return next format after `fmt`, or 0 when none remain.
        for f in sorted(self.formats):
            if f > fmt:
                return f
        return 0

    def EmptyClipboard(self):
        self.formats.clear()
        self.data.clear()

    def SetClipboardData(self, fmt, data):
        self.formats.add(fmt)
        self.data[fmt] = data


@pytest.fixture
def fake_clipboard():
    fb = FakeClipboard()
    # Patch every win32clipboard call used by inject.py.
    with patch.object(inject.win32clipboard, "OpenClipboard", fb.OpenClipboard), \
         patch.object(inject.win32clipboard, "CloseClipboard", fb.CloseClipboard), \
         patch.object(inject.win32clipboard, "IsClipboardFormatAvailable", fb.IsClipboardFormatAvailable), \
         patch.object(inject.win32clipboard, "GetClipboardData", fb.GetClipboardData), \
         patch.object(inject.win32clipboard, "EnumClipboardFormats", fb.EnumClipboardFormats), \
         patch.object(inject.win32clipboard, "EmptyClipboard", fb.EmptyClipboard), \
         patch.object(inject.win32clipboard, "SetClipboardData", fb.SetClipboardData), \
         patch.object(inject.user32, "keybd_event", MagicMock()), \
         patch.object(inject.user32, "GetForegroundWindow", return_value=1), \
         patch.object(inject.user32, "GetWindowTextW", return_value="Notepad"):
        yield fb


def test_inject_text_saves_and_restores_clipboard(fake_clipboard):
    """Text clipboard is saved, overwritten, pasted, then restored."""
    fake_clipboard.formats.add(inject.win32con.CF_UNICODETEXT)
    fake_clipboard.data[inject.win32con.CF_UNICODETEXT] = "previous user content"

    result = inject.inject_text("dictated text", restore_delay_ms=0, post_paste_delay_ms=0)

    assert result is True
    # Restored to previous content.
    assert fake_clipboard.data.get(inject.win32con.CF_UNICODETEXT) == "previous user content"


def test_inject_text_aborts_on_non_text_clipboard(fake_clipboard):
    """Image/file-list format present: injection aborts, clipboard untouched."""
    # CF_BITMAP = 2, a non-text format.
    fake_clipboard.formats.add(2)

    result = inject.inject_text("dictated text", restore_delay_ms=0, post_paste_delay_ms=0)

    assert result is False
    # Clipboard was never written: no CF_UNICODETEXT added.
    assert inject.win32con.CF_UNICODETEXT not in fake_clipboard.formats


def test_inject_text_empty_clipboard(fake_clipboard):
    """Empty clipboard: injection succeeds and clipboard is left empty."""
    result = inject.inject_text("dictated text", restore_delay_ms=0, post_paste_delay_ms=0)

    assert result is True
    assert fake_clipboard.formats == set() or fake_clipboard.data.get(inject.win32con.CF_UNICODETEXT) == "dictated text"
    # After restore path, empty clipboard is re-emptied (no text to restore).
    assert fake_clipboard.data.get(inject.win32con.CF_UNICODETEXT) is None or len(fake_clipboard.formats) == 0


def test_inject_text_text_with_locale_metadata_passes(fake_clipboard):
    """CF_UNICODETEXT + CF_LOCALE (normal Windows text clipboard) is safe."""
    fake_clipboard.formats.add(inject.win32con.CF_UNICODETEXT)
    fake_clipboard.data[inject.win32con.CF_UNICODETEXT] = "previous user content"
    # CF_LOCALE = 16, harmless metadata Windows writes next to text.
    fake_clipboard.formats.add(16)

    result = inject.inject_text("dictated text", restore_delay_ms=0, post_paste_delay_ms=0)

    assert result is True
    assert fake_clipboard.data.get(inject.win32con.CF_UNICODETEXT) == "previous user content"


def test_inject_text_text_plus_bitmap_aborts(fake_clipboard):
    """CF_UNICODETEXT + CF_BITMAP: rich content present, abort without writes."""
    fake_clipboard.formats.add(inject.win32con.CF_UNICODETEXT)
    fake_clipboard.data[inject.win32con.CF_UNICODETEXT] = "previous user content"
    fake_clipboard.formats.add(2)  # CF_BITMAP

    result = inject.inject_text("dictated text", restore_delay_ms=0, post_paste_delay_ms=0)

    assert result is False
    # Abort happens before any write: previous content untouched, no new format added.
    assert fake_clipboard.data.get(inject.win32con.CF_UNICODETEXT) == "previous user content"
    assert fake_clipboard.formats == {inject.win32con.CF_UNICODETEXT, 2}

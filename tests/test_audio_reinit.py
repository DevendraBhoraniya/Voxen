"""
Unit tests for PortAudio re-initialization guard in voxen.audio.
No real microphone or audio device is required; sounddevice is mocked.
"""

from unittest.mock import MagicMock, patch

import pytest

import voxen.audio as audio


class FakeRecorder:
    """Minimal stand-in for AudioRecorder exposing stream state."""

    def __init__(self, recording=False, stream_active=False):
        self._recording = recording
        self._stream_active = stream_active

    @property
    def is_recording(self):
        return self._recording

    def _is_stream_active(self):
        return self._stream_active


def test_refresh_audio_devices_does_not_tear_down_backend():
    """Enumeration must not call sd._terminate / sd._initialize."""
    with patch.object(audio.sd, "_terminate") as m_term, \
         patch.object(audio.sd, "_initialize") as m_init, \
         patch.object(audio.sd, "query_devices", return_value=[]):
        result = audio.refresh_audio_devices()

    assert result is True
    m_term.assert_not_called()
    m_init.assert_not_called()


def test_safe_reinitialize_refuses_while_recording():
    """Re-init refused when a recording is active."""
    with patch.object(audio.sd, "_terminate") as m_term, \
         patch.object(audio.sd, "_initialize") as m_init:
        recorder = FakeRecorder(recording=True, stream_active=False)
        result = audio.safe_reinitialize_portaudio(recorder)

    assert result is False
    m_term.assert_not_called()
    m_init.assert_not_called()


def test_safe_reinitialize_refuses_while_stream_active():
    """Re-init refused when the stream is open/active."""
    with patch.object(audio.sd, "_terminate") as m_term, \
         patch.object(audio.sd, "_initialize") as m_init:
        recorder = FakeRecorder(recording=False, stream_active=True)
        result = audio.safe_reinitialize_portaudio(recorder)

    assert result is False
    m_term.assert_not_called()
    m_init.assert_not_called()


def test_safe_reinitialize_reinitializes_when_idle():
    """Re-init proceeds when no stream is open and not recording."""
    with patch.object(audio.sd, "_terminate") as m_term, \
         patch.object(audio.sd, "_initialize") as m_init:
        recorder = FakeRecorder(recording=False, stream_active=False)
        result = audio.safe_reinitialize_portaudio(recorder)

    assert result is True
    m_term.assert_called_once()
    m_init.assert_called_once()


def test_safe_reinitialize_refuses_none_recorder():
    """Re-init refused when no recorder is given."""
    with patch.object(audio.sd, "_terminate") as m_term:
        result = audio.safe_reinitialize_portaudio(None)

    assert result is False
    m_term.assert_not_called()

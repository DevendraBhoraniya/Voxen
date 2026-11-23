"""
Unit tests for voxen.config persistence.
Uses a temp directory; no microphone, display, or network required.
"""

import os
import tempfile

from voxen.config import load_config, save_config


def test_save_and_load_config_round_trip():
    """save_config writes atomically; load_config reads it back unchanged."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        data = {
            "stt": {"model": "base.en", "cpu_threads": 4, "beam_size": 2},
            "audio": {"device_index": 3, "target_rms_normalize": 0.05},
            "inject": {"restore_delay_ms": 120, "post_paste_delay_ms": 40},
            "ui": {"show_overlay": True},
        }
        assert save_config(path, data) is True
        loaded = load_config(path)
        assert loaded["stt"]["model"] == "base.en"
        assert loaded["audio"]["device_index"] == 3
        assert loaded["inject"]["restore_delay_ms"] == 120
        assert loaded["ui"]["show_overlay"] is True


def test_config_change_persists_across_reload():
    """A value changed in memory then saved is present after reload."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        save_config(path, {"audio": {"device_index": None}})
        loaded = load_config(path)
        assert loaded["audio"]["device_index"] is None

        # Simulate a settings/tray handler changing the value.
        loaded["audio"]["device_index"] = 7
        assert save_config(path, loaded) is True

        reloaded = load_config(path)
        assert reloaded["audio"]["device_index"] == 7


def test_save_config_atomic_no_partial_file_left():
    """No .tmp file should remain after a successful save."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        assert save_config(path, {"stt": {"model": "tiny.en"}}) is True
        leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        assert leftovers == [], f"Temp files left behind: {leftovers}"

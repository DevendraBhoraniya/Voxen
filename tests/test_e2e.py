"""
End-to-end verification for Voxen.

Stage tests need a real microphone, a cached Whisper model, and (for injection)
Notepad. They are skipped under CI instead of failing.
"""

import os
import sys
import time
import subprocess
import numpy as np
import win32gui
import win32con
import win32process
import pytest

from voxen.audio import AudioRecorder, list_audio_devices, get_default_input_device
from voxen.transcribe import WhisperTranscriber, detect_hardware
from voxen import inject

CI = os.environ.get("CI", "").lower() in ("1", "true", "yes", "on")
skip_hardware = pytest.mark.skipif(
    CI, reason="needs real microphone / cached Whisper model / Notepad"
)


def test_voxen_imports():
    """The package imports cleanly (no missing Ollama symbols)."""
    import voxen.audio
    import voxen.transcribe
    import voxen.inject
    import voxen.config
    assert True


def test_config_loads():
    """config.yaml parses without error."""
    from voxen.config import load_config

    cfg = load_config("config.yaml")
    assert isinstance(cfg, dict)


@skip_hardware
def test_stage_1_hardware_and_model_init():
    hw = detect_hardware()
    assert "cpu_cores" in hw

    transcriber = WhisperTranscriber(
        model_name="base.en",
        device="auto",
        compute_type="auto",
        cpu_threads=hw["recommended_threads"],
    )
    assert transcriber.actual_device in ("cpu", "cuda")


@skip_hardware
def test_stage_2_microphone_capture():
    devices = list_audio_devices()
    assert len(devices) > 0

    levels_recorded = []

    def level_cb(lvl):
        levels_recorded.append(lvl)

    recorder = AudioRecorder(
        sample_rate=16000,
        channels=1,
        audio_level_callback=level_cb,
    )

    recorder.start_recording()
    time.sleep(2.0)
    audio, duration, status = recorder.stop_recording()

    assert status.get("success", False) is True
    assert audio is not None
    rms = float(np.sqrt(np.mean(audio ** 2)))
    assert rms >= 0.0


@skip_hardware
def test_stage_3_transcription():
    audio_path = "test_speech.wav"
    if not os.path.exists(audio_path):
        pytest.skip(f"{audio_path} not present")

    import wave

    with wave.open(audio_path, "rb") as wf:
        framerate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    speech_audio = (
        np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    )

    if framerate != 16000:
        indices = np.round(
            np.arange(0, len(speech_audio), framerate / 16000)
        ).astype(int)
        indices = indices[indices < len(speech_audio)]
        speech_audio = speech_audio[indices]

    transcriber = WhisperTranscriber(model_name="base.en", cpu_threads=4)
    speech_text, speech_dur = transcriber.transcribe(speech_audio)
    assert len(speech_text) > 0


@skip_hardware
def test_stage_5_notepad_injection():
    proc = subprocess.Popen(["notepad.exe"])
    time.sleep(1.2)

    notepad_hwnd = None
    for _ in range(15):

        def enum_win(hwnd, extra):
            nonlocal notepad_hwnd
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if "Notepad" in title:
                    notepad_hwnd = hwnd

        win32gui.EnumWindows(enum_win, None)
        if notepad_hwnd:
            break

    if not notepad_hwnd:
        proc.kill()
        pytest.skip("Notepad window not found")

    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        cur_thread = kernel32.GetCurrentThreadId()
        tgt_thread = user32.GetWindowThreadProcessId(notepad_hwnd, None)
        user32.AttachThreadInput(cur_thread, tgt_thread, True)
        user32.ShowWindow(notepad_hwnd, win32con.SW_RESTORE)
        user32.SetForegroundWindow(notepad_hwnd)
        user32.BringWindowToTop(notepad_hwnd)
        user32.AttachThreadInput(cur_thread, tgt_thread, False)
    except Exception:
        pass
    time.sleep(0.8)

    test_dictation = "Voxen local dictation test: Hello from Voxen!"
    inject.set_clipboard_text("ORIGINAL_USER_CLIPBOARD_CONTENT")

    success = inject.inject_text(test_dictation, restore_delay_ms=150)
    assert success is True

    restored = inject.get_clipboard_text()
    assert restored == "ORIGINAL_USER_CLIPBOARD_CONTENT"

    time.sleep(1.5)
    proc.terminate()


if __name__ == "__main__":
    # Non-hardware checks always run; hardware stages are skipped under CI.
    test_voxen_imports()
    test_config_loads()
    print("Non-hardware checks passed.")

    if os.environ.get("CI") == "true":
        print("Skipping hardware stages under CI.")
        sys.exit(0)

    test_stage_1_hardware_and_model_init()
    test_stage_2_microphone_capture()
    test_stage_3_transcription()
    test_stage_5_notepad_injection()
    print("ALL STAGES VERIFIED SUCCESSFULLY!")

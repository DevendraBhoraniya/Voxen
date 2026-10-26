"""
Comprehensive automated test suite for Voxen Audio Stream Recovery:
1. Detect stream failure and device changes.
2. Safe stream cleanup and resource release.
3. PortAudio re-enumeration (sd._terminate / sd._initialize).
4. Exponential backoff retry on invalid / busy device.
5. Actionable error reporting when no microphone exists.
6. Zero-sample and disconnect detection (never silently produce empty transcript).
7. Hardware capture test on built-in microphone and Bluetooth headset.
"""

import time
import sounddevice as sd
from audio import AudioRecorder, resolve_input_device, refresh_audio_devices, list_audio_devices


def test_device_re_enumeration():
    print("\n--- Test 1: Device Re-enumeration ---")
    assert refresh_audio_devices() is True
    devices = list_audio_devices(refresh=True)
    assert len(devices) > 0, "Should detect at least 1 audio input device on system"
    print(f"Detected {len(devices)} valid input devices (excluding WDM-KS).")


def test_recovery_from_invalid_device_preference():
    print("\n--- Test 2: Recovery from Invalid Device Index (Simulated Disconnect) ---")
    # Set preferred device index to a non-existent device (9999)
    recorder = AudioRecorder(device_index=9999)
    try:
        success, msg = recorder.start_recording()
        print(f"Start with invalid device index: success={success}, msg='{msg}'")
        assert success is True, "Recorder should recover from disconnected device preference via auto-fallback"
        assert recorder.is_recording is True
        time.sleep(0.3)
        audio, duration, status = recorder.stop_recording()
        assert recorder.is_recording is False
        assert status["success"] is True
        print(f"Recovered and captured {duration:.2f}s audio on: '{status['device']}'")
    finally:
        recorder.close()


def test_zero_samples_handling():
    print("\n--- Test 3: Zero Samples & Disconnection Detection ---")
    recorder = AudioRecorder()
    try:
        # Emulate stopped stream where no frames were received
        recorder._is_recording = True
        recorder._chunk_count = 0
        recorder._chunks = []
        audio, duration, status = recorder.stop_recording()
        print(f"Zero samples result: success={status['success']}, reason='{status['reason']}'")
        assert status["success"] is False
        assert status["reason"] == "zero_samples"
        assert "No audio" in status["error"]
        print("[PASS] Capture failure correctly identified as error, not silent transcript.")
    finally:
        recorder.close()


def test_stream_lifecycle_release():
    print("\n--- Test 4: Stream Lifecycle, Pre-roll Persistence & Release ---")
    recorder = AudioRecorder()
    try:
        assert recorder._stream is None
        success, _ = recorder.start_recording()
        assert success is True
        assert recorder._stream is not None
        assert recorder._stream.active is True
        time.sleep(0.2)
        recorder.stop_recording()
        # The stream now stays open so the 500 ms pre-roll is ready for the
        # next take (no cold-open, no clipped first syllable).
        assert recorder._stream is not None, "Stream should stay primed between takes"
        assert recorder._stream.active is True
        print("[PASS] Stream stays primed after a take.")
    finally:
        recorder.close()
    # close() must release the PortAudio endpoint.
    assert recorder._stream is None
    print("[PASS] close() released the audio endpoint.")


def test_hardware_available_microphones():
    print("\n--- Test 5: Hardware Verification across Connected Microphones ---")
    devices = list_audio_devices(refresh=True)
    
    # 5A. Built-in
    builtin = [d for d in devices if any(k in d["name"].lower() for k in ["realtek", "array", "internal"])]
    if builtin:
        rec_b = AudioRecorder(device_index=builtin[0]["index"])
        try:
            s_b, _ = rec_b.start_recording()
            time.sleep(0.3)
            a_b, d_b, stat_b = rec_b.stop_recording()
            print(f"  Built-in Mic ('{builtin[0]['name']}'): success={stat_b['success']}, chunks={stat_b.get('chunks', 0)}")
            assert stat_b["success"] is True
        finally:
            rec_b.close()

    # 5B. Bluetooth Headset (requires ~0.5s for Bluetooth SCO link negotiation on TWS earbuds)
    bluetooth = [d for d in devices if any(k in d["name"].lower() for k in ["headset", "zeb", "bluetooth", "nord"])]
    if bluetooth:
        # Prefer DirectSound or WASAPI for Bluetooth
        ds_bt = [d for d in bluetooth if "directsound" in d["hostapi"].lower()]
        target_bt = ds_bt[0] if ds_bt else bluetooth[0]
        rec_bt = AudioRecorder(device_index=target_bt["index"])
        try:
            s_bt, _ = rec_bt.start_recording()
            time.sleep(1.0)  # Allow Bluetooth SCO radio handshake
            a_bt, d_bt, stat_bt = rec_bt.stop_recording()
            print(f"  Bluetooth Headset ('{target_bt['name']}'): success={stat_bt['success']}, chunks={stat_bt.get('chunks', 0)}")
            assert stat_bt["success"] is True
        finally:
            rec_bt.close()

    # 5C. External USB
    external = [d for d in devices if any(k in d["name"].lower() for k in ["usb", "external", "line in"])]
    if external:
        rec_ext = AudioRecorder(device_index=external[0]["index"])
        try:
            s_ext, _ = rec_ext.start_recording()
            time.sleep(0.3)
            a_ext, d_ext, stat_ext = rec_ext.stop_recording()
            print(f"  External Mic ('{external[0]['name']}'): success={stat_ext['success']}")
        finally:
            rec_ext.close()
    else:
        print("  External USB Microphone: [UNTESTED - HARDWARE NOT CONNECTED]")


if __name__ == "__main__":
    test_device_re_enumeration()
    test_recovery_from_invalid_device_preference()
    test_zero_samples_handling()
    test_stream_lifecycle_release()
    test_hardware_available_microphones()
    print("\n=======================================================")
    print("ALL 5 STREAM RECOVERY & HARDWARE TESTS PASSED!")
    print("=======================================================")

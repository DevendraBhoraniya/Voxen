"""
Utility to play back and inspect the last captured temporary audio recording.
Usage:
    uv run python play_last_capture.py [optional_path]
"""

import sys
import wave
import numpy as np
import sounddevice as sd


def inspect_and_play(wav_path: str = "temp_last_capture.wav"):
    try:
        with wave.open(wav_path, "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            framerate = wf.getframerate()
            frames = wf.getnframes()
            raw_data = wf.readframes(frames)

        duration = frames / float(framerate)
        int16_samples = np.frombuffer(raw_data, dtype=np.int16)
        float_samples = int16_samples.astype(np.float32) / 32768.0

        rms = float(np.sqrt(np.mean(float_samples ** 2)))
        peak = float(np.max(np.abs(float_samples)))

        print("=" * 60)
        print("VOXEN LAST CAPTURED AUDIO INSPECTOR & PLAYBACK")
        print("=" * 60)
        print(f"File Path:         {wav_path}")
        print(f"Sample Rate:       {framerate} Hz")
        print(f"Channels:          {channels}")
        print(f"Sample Width:      {sample_width * 8}-bit")
        print(f"Total Frames:      {frames}")
        print(f"Duration:          {duration:.2f} seconds")
        print(f"RMS Energy:        {rms:.5f}")
        print(f"Peak Amplitude:    {peak:.5f}")
        print("=" * 60)

        if rms < 0.0005:
            print("[WARN] Audio energy is near zero (silence/muted microphone).")
        else:
            print("[INFO] Playing audio through default speaker/headphones...")
            sd.play(float_samples, framerate)
            sd.wait()
            print("[SUCCESS] Playback completed.")

    except FileNotFoundError:
        print(f"[ERROR] Audio file '{wav_path}' not found. Make a dictation first!")
    except Exception as e:
        print(f"[ERROR] Failed to inspect/play audio: {e}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "temp_last_capture.wav"
    inspect_and_play(path)

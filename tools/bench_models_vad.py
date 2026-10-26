"""
Benchmark script comparing tiny.en, base.en, and large-v3-turbo
with VAD enabled vs disabled on identical reference audio.
"""

import time
import wave
import numpy as np
from voxen.audio import resample_to_16k, normalize_audio
from faster_whisper import WhisperModel

def load_wav(path):
    with wave.open(path, 'rb') as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return resample_to_16k(audio, sr)

audio = load_wav("test_speech.wav")
norm_audio = normalize_audio(audio, target_rms=0.08)

models_to_test = [
    ("tiny.en", "tiny.en"),
    ("base.en", "base.en"),
    ("large-v3-turbo", "large-v3-turbo")
]

print("=" * 70)
print(f"BENCHMARK ON REFERENCE AUDIO (Duration: {len(audio)/16000:.2f}s)")
print("=" * 70)

for m_key, m_name in models_to_test:
    print(f"\n[*] Loading model '{m_name}' on CPU int8 (4 worker threads)...")
    t0 = time.time()
    model = WhisperModel(m_name, device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
    load_dur = time.time() - t0
    print(f"    Loaded in {load_dur:.2f}s")

    # 1. Test with VAD Disabled
    t0 = time.time()
    segs_no_vad, _ = model.transcribe(
        norm_audio,
        language="en",
        beam_size=5,
        vad_filter=False,
        temperature=0.0
    )
    raw_no_vad = " ".join(s.text.strip() for s in segs_no_vad)
    dur_no_vad = time.time() - t0
    print(f"  [VAD=False] [{dur_no_vad:5.2f}s] (RTF: {dur_no_vad/(len(audio)/16000):.2f}x): '{raw_no_vad}'")

    # 2. Test with Padded VAD Enabled
    t0 = time.time()
    segs_vad, _ = model.transcribe(
        norm_audio,
        language="en",
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=250, speech_pad_ms=300, threshold=0.30),
        temperature=0.0
    )
    raw_vad = " ".join(s.text.strip() for s in segs_vad)
    dur_vad = time.time() - t0
    print(f"  [VAD=True ] [{dur_vad:5.2f}s] (RTF: {dur_vad/(len(audio)/16000):.2f}x): '{raw_vad}'")

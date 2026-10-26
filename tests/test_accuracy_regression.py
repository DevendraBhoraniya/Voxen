"""
Repeatable Speech-to-Text Accuracy and Regression Test Suite.
Validates Whisper accuracy, vocabulary prompting, conservative corrections,
VAD behavior, and latency benchmarks against reference audio.
"""

import time
import wave
import numpy as np
from voxen.audio import resample_to_16k, normalize_audio
from voxen.transcribe import (
    WhisperTranscriber,
    REFERENCE_VOCABULARY,
    DEFAULT_CORRECTIONS,
    apply_vocabulary_corrections
)


def load_wav_file(path: str) -> np.ndarray:
    """Load WAV file and return normalized 16kHz float32 mono audio."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return normalize_audio(resample_to_16k(audio, sr))


def calculate_wer(reference: str, hypothesis: str) -> float:
    """Compute Levenshtein Word Error Rate (WER) between reference and hypothesis."""
    import re
    ref_words = re.findall(r"\b\w+\b", reference.lower())
    hyp_words = re.findall(r"\b\w+\b", hypothesis.lower())

    d = np.zeros((len(ref_words) + 1, len(hyp_words) + 1), dtype=int)
    for i in range(len(ref_words) + 1):
        d[i][0] = i
    for j in range(len(hyp_words) + 1):
        d[0][j] = j

    for i in range(1, len(ref_words) + 1):
        for j in range(1, len(hyp_words) + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                d[i][j] = d[i - 1][j - 1]
            else:
                d[i][j] = 1 + min(d[i - 1][j], d[i][j - 1], d[i - 1][j - 1])

    return float(d[len(ref_words)][len(hyp_words)]) / max(1, len(ref_words))


def test_vocabulary_correction_map():
    """Verify conservative word-boundary correction dictionary on reference terms."""
    test_cases = [
        ("testing Voice but today", "testing Voxen today"),
        ("voice but is fast", "Voxen is fast"),
        ("Voicebut is great", "Voxen is great"),
        ("we use fast api for backend", "we use FastAPI for backend"),
        ("database is post gres", "database is PostgreSQL"),
        ("hosted on github", "hosted on GitHub"),
        ("using claude code and codex", "using Claude Code and codex"),
        ("local ollama instance", "local Ollama instance"),
        ("agentic ai pipeline with rag and api", "agentic AI pipeline with RAG and API"),
        ("stored in vector database", "stored in vector database"),
        # Negative tests: ensure ordinary English words are NOT corrupted
        ("the butter is soft", "the butter is soft"),
        ("rapid progress made", "rapid progress made"),
        ("dragon fruit is sweet", "dragon fruit is sweet"),
        ("capital city of france", "capital city of france"),
    ]

    for raw, expected in test_cases:
        corrected, changed = apply_vocabulary_corrections(raw, DEFAULT_CORRECTIONS)
        assert corrected == expected, f"Expected '{expected}', got '{corrected}'"
    print("[PASS] Vocabulary correction map passed all positive and negative boundary tests.")


def test_transcription_accuracy_and_latency():
    """Verify transcription on reference WAV file matches target accuracy and latency."""
    audio = load_wav_file("test_speech.wav")
    reference_text = "Voxen local speech to text dictation works perfectly."

    # Test base.en
    tr_base = WhisperTranscriber(model_name="base.en", cpu_threads=4)
    out_base, dur_base = tr_base.transcribe(audio)
    wer_base = calculate_wer(reference_text, out_base)

    print(f"\n[base.en] Output: '{out_base}' | Latency: {dur_base:.2f}s | WER: {wer_base:.2%}")
    assert wer_base == 0.0, f"Expected 0% WER for base.en, got {wer_base:.2%} ('{out_base}')"
    assert "Voxen" in out_base, "Voxen term must be recognized accurately"

    # Test tiny.en
    tr_tiny = WhisperTranscriber(model_name="tiny.en", cpu_threads=4)
    out_tiny, dur_tiny = tr_tiny.transcribe(audio)
    wer_tiny = calculate_wer(reference_text, out_tiny)

    print(f"[tiny.en] Output: '{out_tiny}' | Latency: {dur_tiny:.2f}s | WER: {wer_tiny:.2%}")
    assert wer_tiny == 0.0, f"Expected 0% WER for tiny.en, got {wer_tiny:.2%} ('{out_tiny}')"
    assert "Voxen" in out_tiny, "Voxen term must be recognized accurately"
    assert dur_tiny < 4.0, f"Expected tiny.en latency < 4.0s for 4s audio, got {dur_tiny:.2f}s"


def test_vad_comparison_on_reference_audio():
    """Verify speech is not truncated when VAD is enabled vs disabled."""
    audio = load_wav_file("test_speech.wav")
    tr = WhisperTranscriber(model_name="base.en", cpu_threads=4)

    # 1. Transcribe with VAD enabled
    tr.vad_filter = True
    out_vad, dur_vad = tr.transcribe(audio)

    # 2. Transcribe with VAD disabled
    tr.vad_filter = False
    out_no_vad, dur_no_vad = tr.transcribe(audio)

    print(f"\n[VAD=True ] '{out_vad}' ({dur_vad:.2f}s)")
    print(f"[VAD=False] '{out_no_vad}' ({dur_no_vad:.2f}s)")

    # Speech must not be lost or cut off by VAD
    assert "perfectly" in out_vad.lower(), "VAD must not cut off trailing speech"
    assert "perfectly" in out_no_vad.lower(), "Non-VAD baseline contains full sentence"


if __name__ == "__main__":
    print("=" * 65)
    print("RUNNING VOXEN ACCURACY & REGRESSION TEST SUITE")
    print("=" * 65)
    test_vocabulary_correction_map()
    test_vad_comparison_on_reference_audio()
    test_transcription_accuracy_and_latency()
    print("\n[ALL TESTS PASSED SUCCESSFULLY]")

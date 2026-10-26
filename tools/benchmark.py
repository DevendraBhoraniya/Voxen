"""
Voxen Diagnostic & Benchmark Script
=======================================
1. CUDA DLL detection (non-invasive)
2. VAD comparison (enabled vs disabled on identical audio)
3. tiny.en vs base.en accuracy + latency benchmark (10+ phrases)
4. Optimal transcription settings recommendation
5. WER (word error rate) report
"""

import os
import sys
import time
import wave
import struct
import math
import ctypes
import numpy as np
from typing import List, Tuple, Dict, Optional
from faster_whisper import WhisperModel

# Force offline mode: skip slow HuggingFace version-check HTTP request
os.environ.setdefault("HF_HUB_OFFLINE", "1")

# ─────────────────────────────────────────────────────────────────────────────
# 1. CUDA DIAGNOSTICS (never downloads anything)
# ─────────────────────────────────────────────────────────────────────────────

def check_cuda_dlls() -> Dict:
    """Check which CUDA runtime DLLs are present on PATH / system. No downloads."""
    dlls_to_check = [
        "cublas64_12.dll",
        "cublasLt64_12.dll",
        "cudart64_12.dll",
        "cudnn64_9.dll",
        "cudnn_ops64_9.dll",
        "cublas64_11.dll",
        "cudart64_110.dll",
        "nvcuda.dll",      # Driver DLL – always present if GPU driver installed
    ]

    found, missing = [], []
    for dll in dlls_to_check:
        try:
            ctypes.cdll.LoadLibrary(dll)
            found.append(dll)
        except OSError:
            missing.append(dll)

    # Check if nvidia-smi works (driver present)
    driver_ok = os.system("nvidia-smi -L >nul 2>&1") == 0

    try:
        import ctranslate2
        cuda_devices = ctranslate2.get_cuda_device_count()
    except Exception:
        cuda_devices = 0

    cuda_usable = "cublas64_12.dll" in found

    return {
        "found_dlls": found,
        "missing_dlls": missing,
        "driver_ok": driver_ok,
        "ctranslate2_cuda_devices": cuda_devices,
        "cuda_usable_for_whisper": cuda_usable,
        "recommendation": (
            "CUDA is fully usable" if cuda_usable
            else "GPU present but CUDA runtime missing. Install CUDA Toolkit 12.x from nvidia.com "
                 "or add the correct DLL directory to PATH. Voxen will use CPU (safe)."
        )
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. REFERENCE AUDIO GENERATION via Windows SAPI TTS
# ─────────────────────────────────────────────────────────────────────────────

REFERENCE_PHRASES = [
    ("p01", "This is a test."),
    ("p02", "Open the document and save it."),
    ("p03", "Schedule a meeting for tomorrow at three PM."),
    ("p04", "The quick brown fox jumps over the lazy dog."),
    ("p05", "Send an email to the team about the project update."),
    ("p06", "Please turn off the lights before you leave."),
    ("p07", "What is the weather forecast for this weekend?"),
    ("p08", "Add a new item to the shopping list."),
    ("p09", "Start a new paragraph here."),
    ("p10", "Copy the selected text and paste it below."),
    ("p11", "Remember to charge your laptop tonight."),
    ("p12", "The meeting is at two thirty in the conference room."),
]


def generate_wav(text: str, output_path: str) -> bool:
    """Generate WAV using Windows SAPI TTS (no internet)."""
    try:
        import win32com.client
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        stream  = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(output_path, 3, False)
        speaker.AudioOutputStream = stream
        speaker.Speak(text)
        stream.Close()
        return True
    except Exception as e:
        print(f"  TTS error for '{text}': {e}")
        return False


def load_wav_as_float32(path: str, target_sr: int = 16000) -> Optional[np.ndarray]:
    """Load a WAV file, downmix to mono, resample to 16 kHz float32."""
    try:
        with wave.open(path, "rb") as wf:
            sr = wf.getframerate()
            n_ch = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())

        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if n_ch > 1:
            data = data.reshape(-1, n_ch).mean(axis=1)

        # Simple nearest-neighbour resample if needed
        if sr != target_sr:
            ratio = target_sr / sr
            new_len = int(len(data) * ratio)
            indices = np.round(np.linspace(0, len(data) - 1, new_len)).astype(int)
            data = data[indices]

        return data
    except Exception as e:
        print(f"  Failed to load {path}: {e}")
        return None


def rms_db(audio: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(audio ** 2)))
    return 20 * math.log10(rms + 1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 3. WORD ERROR RATE
# ─────────────────────────────────────────────────────────────────────────────

def normalize_text(text: str) -> List[str]:
    """Lowercase, strip punctuation, split to words."""
    import re
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s']", "", text)
    return text.split()


def wer(reference: str, hypothesis: str) -> Tuple[float, int, int]:
    """Levenshtein WER. Returns (rate, edits, ref_len)."""
    ref = normalize_text(reference)
    hyp = normalize_text(hypothesis)
    n, m = len(ref), len(hyp)
    if n == 0:
        return (0.0, 0, 0) if m == 0 else (1.0, m, 0)

    dp = list(range(m + 1))
    for i in range(1, n + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, m + 1):
            if ref[i-1] == hyp[j-1]:
                dp[j] = prev[j-1]
            else:
                dp[j] = 1 + min(prev[j], dp[j-1], prev[j-1])

    return dp[m] / n, dp[m], n


# ─────────────────────────────────────────────────────────────────────────────
# 4. BENCHMARK RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def load_model(model_name: str, device: str, compute_type: str,
               cpu_threads: int = 4) -> WhisperModel:
    print(f"  Loading {model_name} [{device}/{compute_type}] ...", end=" ", flush=True)
    t0 = time.time()
    # local_files_only avoids a slow HF version-check HTTP request
    m = WhisperModel(model_name, device=device, compute_type=compute_type,
                     cpu_threads=cpu_threads, num_workers=1,
                     local_files_only=True)
    print(f"{time.time()-t0:.1f}s")
    return m


def transcribe_timed(model: WhisperModel, audio: np.ndarray,
                     vad_filter: bool, beam_size: int,
                     language: str = "en",
                     initial_prompt: Optional[str] = None) -> Tuple[str, float, float]:
    """
    Returns (transcript, inference_sec, audio_duration_sec)
    """
    audio_dur = len(audio) / 16000.0
    t0 = time.time()
    kwargs = dict(
        beam_size=beam_size,
        language=language,
        vad_filter=vad_filter,
        condition_on_previous_text=False,
        temperature=0.0,           # greedy – no sampling randomness
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
    )
    if vad_filter:
        kwargs["vad_parameters"] = dict(
            min_silence_duration_ms=300,
            speech_pad_ms=400,         # pad before/after speech
            threshold=0.35,            # lower = keep more speech
        )
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt

    segments, _ = model.transcribe(audio, **kwargs)
    text = " ".join(s.text.strip() for s in segments).strip()
    elapsed = time.time() - t0
    return text, elapsed, audio_dur


def run_benchmark(wav_dir: str, configs: List[Dict],
                  phrases: List[Tuple[str, str]]) -> List[Dict]:
    """
    configs: list of {"label": str, "model": str, "device": str,
                       "compute": str, "vad": bool, "beam": int}
    Returns list of result rows.
    """
    results = []

    for cfg in configs:
        label = cfg["label"]
        print(f"\n{'='*60}")
        print(f"CONFIG: {label}")
        print(f"{'='*60}")

        try:
            model = load_model(cfg["model"], cfg["device"], cfg["compute"],
                               cpu_threads=cfg.get("threads", 4))
        except Exception as e:
            print(f"  FAILED to load model: {e}")
            continue

        cfg_results = []
        for pid, ref_text in phrases:
            wav_path = os.path.join(wav_dir, f"{pid}.wav")
            audio = load_wav_as_float32(wav_path)
            if audio is None:
                continue

            audio_dur = len(audio) / 16000.0
            db = rms_db(audio)

            hyp, inf_sec, _ = transcribe_timed(
                model, audio,
                vad_filter=cfg["vad"],
                beam_size=cfg["beam"],
                language="en",
                initial_prompt=cfg.get("initial_prompt"),
            )

            rate, edits, ref_len = wer(ref_text, hyp)
            rtf = inf_sec / audio_dur if audio_dur > 0 else 0

            row = {
                "config": label,
                "phrase_id": pid,
                "reference": ref_text,
                "hypothesis": hyp,
                "wer": rate,
                "edits": edits,
                "ref_words": ref_len,
                "inf_sec": inf_sec,
                "audio_dur": audio_dur,
                "rtf": rtf,
                "rms_db": db,
                "vad": cfg["vad"],
            }
            cfg_results.append(row)
            results.append(row)

            ok = "✓" if rate == 0 else f"✗ WER={rate:.0%}"
            print(f"  [{pid}] {ok}  {inf_sec:.2f}s  RTF={rtf:.2f}x")
            print(f"         REF: {ref_text}")
            print(f"         HYP: {hyp}")

        if cfg_results:
            avg_wer = sum(r["wer"] for r in cfg_results) / len(cfg_results)
            avg_inf = sum(r["inf_sec"] for r in cfg_results) / len(cfg_results)
            avg_rtf = sum(r["rtf"] for r in cfg_results) / len(cfg_results)
            total_edits = sum(r["edits"] for r in cfg_results)
            total_words = sum(r["ref_words"] for r in cfg_results)
            corpus_wer = total_edits / total_words if total_words else 0
            print(f"\n  --- SUMMARY [{label}] ---")
            print(f"  Corpus WER: {corpus_wer:.1%}  |  Avg WER: {avg_wer:.1%}")
            print(f"  Avg inference: {avg_inf:.2f}s  |  Avg RTF: {avg_rtf:.2f}x")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 5. VAD COMPARISON
# ─────────────────────────────────────────────────────────────────────────────

def vad_comparison(model: WhisperModel, wav_dir: str,
                   phrases: List[Tuple[str, str]]) -> None:
    """Compare VAD on vs off on the same audio to show what VAD removes."""
    print("\n" + "="*60)
    print("VAD IMPACT ANALYSIS (base.en, beam=5)")
    print("="*60)
    issues = 0
    for pid, ref_text in phrases[:6]:  # first 6 as sample
        wav_path = os.path.join(wav_dir, f"{pid}.wav")
        audio = load_wav_as_float32(wav_path)
        if audio is None:
            continue

        hyp_vad_on,  t_on,  _ = transcribe_timed(model, audio, vad_filter=True,  beam_size=5)
        hyp_vad_off, t_off, _ = transcribe_timed(model, audio, vad_filter=False, beam_size=5)

        wer_on,  _, _ = wer(ref_text, hyp_vad_on)
        wer_off, _, _ = wer(ref_text, hyp_vad_off)

        vad_diff = "VAD HURTS" if wer_on > wer_off else ("same" if wer_on == wer_off else "VAD helps")
        if wer_on > wer_off:
            issues += 1

        print(f"\n  [{pid}] Ref:    {ref_text}")
        print(f"         VAD ON:  {hyp_vad_on}  WER={wer_on:.0%}  t={t_on:.2f}s")
        print(f"         VAD OFF: {hyp_vad_off}  WER={wer_off:.0%}  t={t_off:.2f}s  → {vad_diff}")

    print(f"\n  VAD hurt accuracy on {issues}/6 sample phrases.")
    print(f"  Recommendation: {'Disable VAD for short dictation (<10s)' if issues >= 2 else 'VAD is safe to use'}")


# ─────────────────────────────────────────────────────────────────────────────
# 6. OPTIMAL SETTINGS ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def latency_breakdown(model: WhisperModel, wav_dir: str,
                      pid: str = "p01", ref_text: str = "This is a test.") -> None:
    """Break down latency into load + VAD + inference for a single phrase."""
    wav_path = os.path.join(wav_dir, f"{pid}.wav")
    audio = load_wav_as_float32(wav_path)
    if audio is None:
        return

    print(f"\n{'='*60}")
    print(f"LATENCY BREAKDOWN  [{pid}: '{ref_text}']")
    print(f"{'='*60}")
    print(f"  Audio duration: {len(audio)/16000:.2f}s  |  RMS: {rms_db(audio):.1f} dBFS")

    for beam in [1, 3, 5]:
        t0 = time.time()
        hyp, inf, dur = transcribe_timed(model, audio, vad_filter=False, beam_size=beam)
        w, _, _ = wer(ref_text, hyp)
        print(f"  beam={beam}: {inf:.3f}s  WER={w:.0%}  → \"{hyp}\"")

    print()
    for beam in [1, 3, 5]:
        t0 = time.time()
        hyp, inf, dur = transcribe_timed(model, audio, vad_filter=True,  beam_size=beam)
        w, _, _ = wer(ref_text, hyp)
        print(f"  beam={beam} +VAD: {inf:.3f}s  WER={w:.0%}  → \"{hyp}\"")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    wav_dir = os.path.join(os.path.dirname(__file__), "bench_audio")
    os.makedirs(wav_dir, exist_ok=True)

    print("\n" + "="*60)
    print(" VOXEN DIAGNOSTIC & BENCHMARK")
    print("="*60)

    # ── 1. CUDA check ──────────────────────────────────────────────
    print("\n[1] CUDA / GPU DIAGNOSTICS")
    cuda_info = check_cuda_dlls()
    print(f"  GPU driver present:      {cuda_info['driver_ok']}")
    print(f"  ctranslate2 CUDA devs:   {cuda_info['ctranslate2_cuda_devices']}")
    print(f"  Found DLLs:              {cuda_info['found_dlls']}")
    print(f"  Missing DLLs:            {cuda_info['missing_dlls']}")
    print(f"  CUDA usable for Whisper: {cuda_info['cuda_usable_for_whisper']}")
    print(f"  -> {cuda_info['recommendation']}")

    device   = "cuda" if cuda_info["cuda_usable_for_whisper"] else "cpu"
    compute  = "float16" if device == "cuda" else "int8"
    print(f"\n  Using device: {device.upper()}  compute: {compute}")

    # ── 2. Generate reference WAVs ────────────────────────────────
    print("\n[2] GENERATING REFERENCE WAV FILES (Windows SAPI TTS)")
    generated = 0
    for pid, text in REFERENCE_PHRASES:
        path = os.path.join(wav_dir, f"{pid}.wav")
        if os.path.exists(path):
            print(f"  {pid}: already exists, skipping")
            generated += 1
        else:
            ok = generate_wav(text, path)
            if ok:
                print(f"  {pid}: OK")
                generated += 1
    print(f"  {generated}/{len(REFERENCE_PHRASES)} WAVs ready")

    if generated == 0:
        print("  ERROR: No WAV files generated. Check Windows TTS / pywin32.")
        sys.exit(1)

    # ── 3. VAD comparison on base.en ──────────────────────────────
    print("\n[3] VAD ANALYSIS")
    base_model = load_model("base.en", device, compute, cpu_threads=4)
    vad_comparison(base_model, wav_dir, REFERENCE_PHRASES)
    del base_model

    # ── 4. Latency breakdown ──────────────────────────────────────
    print("\n[4] LATENCY BREAKDOWN")
    base_model = load_model("base.en", device, compute, cpu_threads=4)
    latency_breakdown(base_model, wav_dir, "p01", REFERENCE_PHRASES[0][1])
    del base_model

    # ── 5. Main benchmark ─────────────────────────────────────────
    print("\n[5] ACCURACY & LATENCY BENCHMARK")

    # We test a matrix of: model × VAD × beam_size
    # VAD off is recommended for short clean dictation.
    # Beam 5 gives best accuracy on CPU; beam 1 is fastest.
    benchmark_configs = [
        # ── tiny.en ──────────────────────────────────────────────
        dict(label="tiny.en | beam=1 | VAD=off",
             model="tiny.en", device=device, compute=compute,
             vad=False, beam=1, threads=4),
        dict(label="tiny.en | beam=5 | VAD=off",
             model="tiny.en", device=device, compute=compute,
             vad=False, beam=5, threads=4),
        dict(label="tiny.en | beam=5 | VAD=on",
             model="tiny.en", device=device, compute=compute,
             vad=True,  beam=5, threads=4),
        # ── base.en ──────────────────────────────────────────────
        dict(label="base.en | beam=1 | VAD=off",
             model="base.en", device=device, compute=compute,
             vad=False, beam=1, threads=4),
        dict(label="base.en | beam=5 | VAD=off",
             model="base.en", device=device, compute=compute,
             vad=False, beam=5, threads=4),
        dict(label="base.en | beam=5 | VAD=on",
             model="base.en", device=device, compute=compute,
             vad=True,  beam=5, threads=4),
        # ── base.en with context prompt ──────────────────────────
        dict(label="base.en | beam=5 | VAD=off | prompt",
             model="base.en", device=device, compute=compute,
             vad=False, beam=5, threads=4,
             initial_prompt="Dictation. Proper nouns capitalized."),
    ]

    results = run_benchmark(wav_dir, benchmark_configs, REFERENCE_PHRASES)

    # ── 6. Summary table ─────────────────────────────────────────
    print("\n" + "="*60)
    print("FINAL COMPARISON TABLE")
    print("="*60)
    print(f"{'Config':<42} {'WER':>6} {'AvgInf':>7} {'AvgRTF':>7}")
    print("-"*65)

    from itertools import groupby
    key_fn = lambda r: r["config"]
    for cfg_label, rows in groupby(sorted(results, key=key_fn), key=key_fn):
        rows = list(rows)
        total_e = sum(r["edits"]    for r in rows)
        total_w = sum(r["ref_words"] for r in rows)
        cwer    = total_e / total_w if total_w else 0
        avg_inf = sum(r["inf_sec"]  for r in rows) / len(rows)
        avg_rtf = sum(r["rtf"]      for r in rows) / len(rows)
        print(f"  {cfg_label:<40} {cwer:>5.1%} {avg_inf:>7.2f}s {avg_rtf:>7.2f}x")

    # ── 7. Write recommendations ──────────────────────────────────
    print("\n" + "="*60)
    print("RECOMMENDATIONS")
    print("="*60)

    # Find best WER config
    from itertools import groupby as _g
    config_summary = {}
    for cfg_label, rows in _g(sorted(results, key=key_fn), key=key_fn):
        rows = list(rows)
        total_e = sum(r["edits"]    for r in rows)
        total_w = sum(r["ref_words"] for r in rows)
        config_summary[cfg_label] = {
            "wer": total_e / total_w if total_w else 1.0,
            "avg_inf": sum(r["inf_sec"] for r in rows) / len(rows),
        }

    best_acc  = min(config_summary, key=lambda k: config_summary[k]["wer"])
    best_fast = min(
        (k for k in config_summary if config_summary[k]["wer"] <= 0.10),
        key=lambda k: config_summary[k]["avg_inf"],
        default=best_acc
    )

    print(f"  Best accuracy:    {best_acc}  (WER={config_summary[best_acc]['wer']:.1%})")
    print(f"  Best balanced:    {best_fast}  "
          f"(WER={config_summary[best_fast]['wer']:.1%}, "
          f"avg={config_summary[best_fast]['avg_inf']:.2f}s)")
    print()
    print("  Optimal config.yaml changes:")
    print("    transcription:")
    print("      beam_size: 5         # was 1; +0.5s but much better accuracy")
    print("      vad_filter: false    # disable for short dictation (<15s)")
    print("      cpu_threads: 4       # use all available threads")
    print("      temperature: 0       # greedy decoding, no randomness")
    print()
    print("  CUDA status:")
    if cuda_info["cuda_usable_for_whisper"]:
        print("    CUDA is usable — GPU transcription is active.")
    elif cuda_info["driver_ok"]:
        print("    GPU driver OK but CUDA 12 runtime missing.")
        print("    Install: https://developer.nvidia.com/cuda-downloads")
        print("    (select Windows → x86_64 → exe(local), CUDA 12.x)")
    else:
        print("    No GPU detected or driver not installed. CPU mode is fine.")


if __name__ == "__main__":
    main()

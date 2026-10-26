# Voxen — Current Architecture & Upgrade Handoff

**Snapshot date:** 2026-10-26  
**Repo:** `F:\test\STT` (Windows 11, branch `master`, no git remote)  
**State:** clean working tree. Four commits on top of an initial VoiceBud import:

```
651dda2  refactor: reorganize into voxen package with clean folder hierarchy
6cd9ee1  fix: restore premium overlay and waveform tray icon
3f164ad  feat: accent robustness (prompt + corrections + capitalisation)
cad33dd  perf: tune Whisper (beam 2, best_of 1, tighter VAD padding)
```

This document describes the code on disk today, measured behavior on this machine, known defects, and a target architecture + migration plan.

---

## 1. What Voxen is

A fully local Windows dictation utility:

- Global hotkey (`Ctrl+Shift`) starts/stops recording (toggle or hold mode).
- Audio is captured, converted to 16 kHz mono, transcribed locally, optionally refined by a local Ollama model, and pasted into the focused Windows application.
- A frameless always-on-top "pill" overlay shows recording state and a live waveform.
- A system-tray menu switches microphone, STT model/engine, hotkey mode, cleanup, and overlay.
- No cloud calls. Network use is limited to downloading models (Hugging Face) and optional `localhost` Ollama.

## 2. Runtime environment (measured)

| Item | Value |
|---|---|
| OS | Windows 11 |
| CPU | AMD Ryzen 5 3550H, 4 cores / 8 threads |
| RAM | 13.9 GB |
| GPU | NVIDIA GTX 1650 (4 GB) + AMD Radeon Vega 8 |
| Python | 3.11.15 (uv-managed) |
| Package manager | uv 0.11.15, venv at `.venv` |
| CUDA status | `detect_hardware()` reports `has_cuda=True`, `cuda_usable=False` (`cublas64_12.dll` not found) — **effectively CPU-only** |
| Default input | `sd.default.device = [1, 4]` → idx 1 `Microphone Array (Realtek(R) Audio)` (MME, 44100 Hz, 2 ch) |
| Resolved input | idx 1 Realtek array (resolver follows the Windows default) |

Run commands:

```powershell
.\.venv\Scripts\python.exe -m voxen       # launch app
uv run voxen                              # via entry point
.\.venv\Scripts\python.exe -m py_compile voxen\*.py
uv sync
```

## 3. Dependency inventory

### Declared in `pyproject.toml`

| Package | Spec |
|---|---|
| faster-whisper | `>=1.0.0` |
| numpy | `>=1.26.0` |
| pynput | `>=1.8.0` |
| pyside6 | `>=6.6.0` |
| pywin32 | `>=306` |
| pyyaml | `>=6.0` |
| requests | `>=2.31.0` |
| sounddevice | `>=0.4.6` |

### Installed and verified

| Package | Version | Notes |
|---|---|---|
| PySide6 | 6.12.0 | Qt UI |
| sounddevice | 0.5.6 | PortAudio bindings |
| numpy | 2.4.6 | |
| faster-whisper | 1.2.1 | CTranslate2 backend + Silero VAD |
| ctranslate2 | 4.8.3 | |
| onnxruntime | 1.31.0 | pulled transitively |
| PyYAML | 6.0.3 | |
| requests | 2.34.2 | Ollama HTTP |
| pywin32 | 312 | clipboard + keybd_event |
| pynput | 1.8.2 | global hotkey |
| needle | 3.2.8 | **installed manually, no distribution metadata, not in `uv.lock`** |
| sherpa-onnx | 2.0.0 | **installed manually, not in `uv.lock`** |

`needle` and `sherpa-onnx` are imported by `voxen/transcribe.py` for the optional Whistle and Moonshine engines. A clean `uv sync` will produce an environment where selecting those engines raises `ImportError`. They must become declared optional extras (or be removed).

### Model storage

- Actual faster-whisper models live in the Hugging Face cache: `%USERPROFILE%\.cache\huggingface\hub` (~6.0 GB total).
- `WhisperModel(...)` is constructed **without** `download_root`, so `%LOCALAPPDATA%\Voxen\models` (~291 MB, legacy) is stale and unused by current code.
- Whistle and Moonshine also live in the HF cache.
- First run can require network access; there is no offline manifest, checksum, or progress UI.

## 4. Repository map (current)

```
F:\test\STT
├─ voxen/                      # Application package
│  ├─ __init__.py             # Package metadata (__version__, __app_name__)
│  ├─ __main__.py             # `python -m voxen` entry
│  ├─ main.py                 # Controller, diagnostics, pipeline, logging
│  ├─ config.py               # Config loading + correction merging
│  ├─ audio.py                # Device discovery, capture, DSP, recorder lifecycle
│  ├─ transcribe.py           # Whisper/Whistle/Moonshine engines, VAD, corrections
│  ├─ cleanup.py              # Optional Ollama refinement + guardrails
│  ├─ inject.py               # Clipboard save/paste/restore
│  ├─ hotkey.py               # Global hotkey listener
│  ├─ overlay.py              # PySide6 floating pill + waveform
│  └─ tray.py                 # System tray icon and menu
├─ tests/                     # Verification scripts
│  ├─ test_accuracy_regression.py
│  ├─ test_cleanup_formatting.py
│  ├─ test_e2e.py
│  ├─ test_regression_real_audio.py
│  ├─ test_stream_recovery.py
│  └─ test_ui_interactive.py
├─ tools/                     # Benchmarking and diagnostics
│  ├─ benchmark.py
│  ├─ bench_models_vad.py
│  └─ play_last_capture.py
├─ docs/
│  └─ ARCHITECTURE.md
├─ config.yaml                # Runtime configuration (no schema)
├─ pyproject.toml            # Dependencies + wheel packaging (packages = ["voxen"])
├─ README.md
├─ uv.lock
├─ .gitignore
├─ bench_audio/               # 12 TTS WAVs + one extra, 22.05 kHz (gitignored)
├─ temp_last_capture.wav      # output of every take, in CWD (gitignored, privacy issue)
└─ .venv/                    # virtual environment (gitignored)
```

There is no CI config, no installer, no `src/` layout beyond the flat `voxen/` package, and no single-instance guard.

## 5. Runtime architecture

### 5.1 Process and threads

```
┌────────────────────────────── Qt main thread ──────────────────────────────┐
│ QApplication.event loop                                                     │
│  ├─ FloatingPill (QWidget, timers, painting)                                │
│  ├─ SystemTrayManager (QSystemTrayIcon + QMenu)                             │
│  └─ VoiceBudApp (QObject controller; tray/hotkey signals)                   │
└───────────────▲───────────────────────────────┬────────────────────────────┘
                │ Qt signals                    │ start/stop calls
┌───────────────┴──────────────┐   ┌────────────▼───────────────────────────┐
│ pynput keyboard.Listener     │   │ PortAudio callback thread              │
│ (global hook, daemon)        │   │ 30 ms float32 blocks → chunks/preroll  │
└───────────────┬──────────────┘   └────────────┬───────────────────────────┘
                │ daemon threads                 │
                ▼                                ▼
        start/stop/toggle              AudioRecorder (lock-protected)
                │
                ▼
        daemon worker thread: _process_pipeline
          transcribe → optional Ollama → clipboard paste
```

- No worker pool, queue, cancellation, or backpressure. Each stop spawns a **new** daemon thread.
- `_is_processing` is a plain bool used to reject a new take while one is being processed.
- The tray "STT Model" switch runs on the Qt thread and synchronously loads a model.
- `self.transcriber` can be replaced by the tray while the pipeline thread is using it.

### 5.2 End-to-end data flow

```
Hotkey
  → AudioRecorder.start_recording()
      • uses primed stream if active, prepends 500 ms pre-roll
      • else opens device with 3 bounded retries (0, 80, 250 ms)
  → user speaks; callback appends 30 ms float32 blocks
  → AudioRecorder.stop_recording()
      • atomically snapshots chunks
      • resample native SR → 16 kHz (FIR anti-alias)
      • 50 Hz high-pass (remove_rumble)
      • RMS normalize to 0.08, max gain 15×
      • silence check on RAW RMS (< 0.0008)
      • writes temp_last_capture.wav (CWD) and last.wav (%LOCALAPPDATA%)
      • returns (float32 mono 16k, duration, status dict)
  → main.stop_recording()
      • handles too_short / zero_samples / silence
      • sets _is_processing, shows TRANSCRIBING
      • starts daemon _process_pipeline
  → transcriber.transcribe(audio)
      • Silence VAD (faster-whisper/Silero) → chunks ≤ 28 s at natural pauses
      • no speech → ""
      • each chunk → Whisper (vad_filter=False), beam 2, best_of 1, temp 0
      • collapse repeated n-grams
      • regex vocabulary corrections + accent corrections + fix_capitalisation
  → optional OllamaCleaner.clean()
      • skipped under 10 words / 20 chars
      • similarity + vocabulary guardrails; raw text on any failure
  → inject.inject_text()
      • save clipboard text → write transcript → release modifiers
      • keybd_event Ctrl+V → wait → restore clipboard
  → overlay SUCCESS / SILENT / ERROR, tray notification
```

### 5.3 Startup

1. `main()` sets the Windows AppUserModelID (`Voxen.Dictation.App.1.0`).
2. `app_config.load_config("config.yaml")` — resolves relative to CWD.
3. `run_first_run_diagnostics()` prints CPU/GPU, configured engine/model, default mic, resolved capture device (warns if <16 kHz or loopback), Ollama status, hotkey.
4. `QApplication` created; tray icon pixmap generated.
5. `VoxenApp` constructs: `AudioRecorder`, `create_transcriber(...)` (loads model on Qt thread), `OllamaCleaner`, `FloatingPill`, `SystemTrayManager`, `HotkeyListener`.
6. `recorder.start_stream()` primes the 500 ms pre-roll stream and logs the mic in use.
7. Tray toast: "Voxen Ready".

### 5.4 Shutdown

`VoxenApp.quit()` stops the hotkey listener, stops an active take, calls `recorder.close()`, hides tray/overlay, quits `QApplication`. Pipeline worker threads are daemon and are not joined.

## 6. Module contracts

### `voxen/main.py`

- **Logging:** console + `RotatingFileHandler` at `%LOCALAPPDATA%\Voxen\logs\voxen.log` (2 MB × 3). INFO level. Raw transcripts are logged at INFO (`[RAW WHISPER OUTPUT]`).
- `app_config.load_config(path)` — YAML → dict, `{}` on error. No validation, no migration, no save.
- `app_config.build_corrections(stt_cfg)` — merges `DEFAULT_CORRECTIONS` with user `replacements`, wrapping user keys in `(?i)\b…\b`.
- `run_first_run_diagnostics(config)` — console summary only.
- `VoxenApp(QObject)` — composition root and controller: `start_recording()`, `stop_recording()`, `toggle_recording()`, `_process_pipeline(audio, duration)`, tray/model/device/mode handlers, `quit()`.
- Config changes made from the tray are applied in memory only; nothing is written back to `config.yaml`.

### `voxen/audio.py`

Device helpers:

- `refresh_audio_devices()` — terminates/re-initializes PortAudio.
- `list_audio_devices(refresh=False)` — input-capable devices, excludes WDM-KS **and loopback devices** (`is_loopback_device`).
- `get_default_input_device()` — queries `sd.default.device[0]`.
- `resolve_input_device(preferred_index=None, refresh=False, exclude=None)` — priority: explicit index → Windows default → headset/USB fallback → first usable. All candidates are filtered through `is_loopback_device`. Returns `(index, name, hostapi, default_samplerate, max_input_channels)`.
- `is_loopback_device(name)` — keyword match against `LOOPBACK_KEYWORDS` (stereo mix, wave out, what u hear, loopback, vb-cable, cable output, virtual audio, wasapi loopback).
- `LOOPBACK_KEYWORDS` — exported constant.

DSP:

- `_fir_lowpass(cutoff, sr, taps)` — Hamming-windowed sinc.
- `resample_to_16k(audio, sr)` — 7 kHz anti-alias FIR for sr > 16 k, then linear interpolation; duration-preserving.
- `remove_rumble(audio, sr=16000, cutoff=50)` — subtracts sub-50 Hz content.
- `normalize_audio(audio, target_rms=0.08, max_gain=15)` — gain clamp, clip ±0.99.
- `save_wav_file(...)` — writes int16 mono WAV.
- `play_sound_cue(...)` — **no-op** (sound cues disabled).

`AudioRecorder`:

- Constructor: `sample_rate`, `channels`, `preroll_ms`, `device_index`, `silence_threshold_energy`, `target_rms_normalize`, `audio_level_callback`.
- `start_stream()` — resolves device, opens `sd.InputStream` at **native** SR (not 16 k), monophonic, 30 ms blocksize, primes pre-roll. Warns for <16 kHz or loopback devices.
- `start_recording()` — fast path reuses an active stream and prepends pre-roll; otherwise retries 3 times, excluding devices that failed.
- `stop_recording()` — sets `_is_recording=False` under the lock, snapshots chunks, resamples → HPF → normalizes, computes status. **Keeps the stream open** for the next take.
- `cancel_recording()` — drops chunks, keeps stream.
- `close()` — releases the stream.
- Status dict keys: `success`, `reason` (`ok`/`silent_speech`/`too_short`/`zero_samples`/`not_recording`), `is_silent`, `raw_rms`, `norm_rms`, `device`, `sample_rate`, `duration`, `wall_duration`, `chunks`, `wav_path`.
- Level callback is invoked **only while recording**.
- Every successful take writes `temp_last_capture.wav` in CWD and `%LOCALAPPDATA%\Voxen\last.wav`.

### `voxen/transcribe.py`

Top-level:

- `setup_cuda_dlls()` — adds `site-packages/nvidia/*/bin` to DLL search path; runs at import.
- Imports `faster_whisper.vad` at module import (requires `onnxruntime`).
- `SR = 16000`, `MAX_CHUNK_S = 28`.
- `speech_chunks(audio)` — Silero VAD (`max_speech_duration_s=25`, `min_silence_duration_ms=300`, `speech_pad_ms=100`), packs segments to ≤28 s. Returns `list[np.ndarray]` / `[]` / `None`.
- `collapse_repeats(text)` — removes any phrase repeated 3+ times.
- `apply_vocabulary_corrections(text, corrections)` — regex `subn` in dict order.
- `fix_capitalisation(text)` — case-normalizes technical terms using `ALWAYS_CAPITALISE` with negative lookaround (so `pythonic` is not changed).
- `DEFAULT_VOCABULARY`, `DEFAULT_INITIAL_PROMPT` (accent-aware prompt mentioning Indian English and technical terms), `DEFAULT_CORRECTIONS` (brand, framework, accent/spoken-form patterns).
- `detect_hardware()` — `ctranslate2.get_cuda_device_count()` + `cublas64_12.dll` load test; `cpu_threads = clamp(cores // 2, 1, 4)`. Recommended model: `large-v3-turbo` on CUDA, `base.en` on CPU.

`WhisperTranscriber`:

- Defaults: `model_name="base.en"`, `device="auto"`, `compute_type="auto"`, `cpu_threads=4`, `language="en"`, `vad_filter=True`, `beam_size=2`, `best_of=1`, `repetition_penalty=1.2`, `no_repeat_ngram_size=3`.
- Loads CUDA float16 when `device=auto` and CUDA is usable, else CPU int8. Tries `local_files_only=True` first, then downloads. No `download_root`.
- `transcribe(audio)`:
  - returns `("", 0.0)` below RMS 0.001
  - VAD chunks; `None` → full clip with Whisper VAD; `[]` → `""`
  - pre-chunked audio → `vad_filter=False`
  - `condition_on_previous_text=False`, `temperature=0.0`, `no_speech_threshold=0.6`
  - vocabulary `initial_prompt`
  - logs raw output; applies `collapse_repeats` + corrections + `fix_capitalisation`
  - returns `(text, elapsed_seconds)`

`WhistleTranscriber`:

- `needle.Whistle()` (needle 3.2.8); native keyword biasing; no VAD.
- Dependency not declared.

`MoonshineTranscriber`:

- `sherpa_onnx.OfflineRecognizer.from_moonshine(...)`, int8 ONNX.
- Downloads five files from Hugging Face at construction.
- Dependency not declared.

`create_transcriber(...)` — factory: `whistle`/`cactus` → Whistle; `moonshine` → Moonshine; everything else → Whisper.

### `voxen/cleanup.py`

- `OllamaCleaner` posts to `{base_url}/api/chat` (`stream=False`, `think=False`, `keep_alive`), timeout 4 s, temperature 0.1 then 0.0 retry.
- Skips if disabled, fewer than 10 words, or <20 chars.
- `is_online()` caches offline probes for 8 s.
- Guardrails: word-overlap `difflib` similarity ≥ 0.8, vocabulary restoration, question/hallucination checks, sanitizer (no em dashes/curly quotes/markdown).
- Returns raw text on any error/timeout/rejection.
- **Note:** the module claims privacy-safe logging, but `main.py`/`transcribe.py` log raw transcripts at INFO.

### `voxen/inject.py`

- `get_clipboard_text()` / `set_clipboard_text()` with retry (10 × 15 ms).
- `release_modifier_keys()` releases Shift/Ctrl/Alt via `keybd_event`.
- `simulate_paste()` sends Ctrl+V via `keybd_event`.
- `inject_text(text, restore_delay_ms=120, post_paste_delay_ms=40)`:
  1. `prev_text = get_clipboard_text()` (text only)
  2. write transcript
  3. release modifiers
  4. Ctrl+V
  5. sleep
  6. restore `prev_text`, or **`EmptyClipboard()` when `prev_text is None`**
- Returns `True` after issuing paste even if the target app did not accept it. `main.py` ignores the return value.
- **Data-loss defect:** if the clipboard held an image/files/HTML, `prev_text is None`, and the `finally` block empties the clipboard, destroying the user's original content.
- No UIPI/elevation handling; paste into elevated windows silently fails.

### `voxen/hotkey.py`

- `pynput.keyboard.Listener` global hook.
- `normalize_key()` maps modifiers and characters.
- `HotkeyListener(combination, mode, debounce_ms, on_trigger, on_start, on_stop)`.
- Toggle mode triggers on combo press; hold mode triggers start on press and stop when the combo is released.
- Callbacks run in fresh daemon threads.
- Combination is parsed from config only; no tray rebinding, no conflict detection, no suppression of the hotkey in the target app.

### `voxen/overlay.py`

- `FloatingPill(QWidget)` — frameless, topmost, `Qt.Tool`, `WindowDoesNotAcceptFocus`, translucent background, `WA_ShowWithoutActivating`.
- Re-applies `WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW` on every show.
- Signals: `state_signal(str, str)`, `audio_signal(float, object)`, `hide_signal()`.
- States rendered: `RECORDING`, `TRANSCRIBING`, `CLEANING`, `SUCCESS`, `ERROR`, `SILENT`.
- `RealMicWaveformVisualizer` — bars from `audio_signal`; decay timer; real RMS scaling.
- `LightweightSpinner` — rotating arc during processing.
- Elapsed timer, auto-hide, drag-to-move, multi-monitor bottom-center positioning, DPI-aware.
- Premium styling: `COLORS` design tokens, `QPropertyAnimation` for width transitions.
- **Defect:** `nativeEvent()` uses `ctypes.wintypes.MSG` but `wintypes` is never imported. The exception is swallowed, so the `WM_MOUSEACTIVATE` guard is dead code (the `WS_EX_NOACTIVATE` path still works).

### `voxen/tray.py`

- `SystemTrayManager(QObject)` signals: `toggle_recording_requested`, `mode_changed(str)`, `device_changed(object)`, `model_changed(str)`, `cleanup_toggled(bool)`, `overlay_toggled(bool)`, `quit_requested`.
- Menu: header/hotkey, start/stop, Hotkey Mode (toggle/hold), Microphone (System Default + every input device), STT Model (whistle, moonshine, distil-small.en, base.en, small.en, distil-large-v3, large-v3-turbo, tiny.en), Ollama cleanup toggle, overlay toggle, open config, quit.
- Tray icon: 5-bar waveform glyph (heights `[6, 12, 20, 12, 6]`), coral red when recording, electric azure when idle.
- Tray mutations update `self.config` in memory only; **they are lost on restart**.
- Model changes synchronously reconstruct the transcriber on the Qt thread.

### `config.yaml`

Sections: `stt`, `llm`, `hotkey`, `audio`, `inject`, `ui`.

Current effective defaults:

```yaml
stt:
  engine: faster-whisper
  model: base.en
  language: en
  device: auto            # → CPU int8 (CUDA not usable on this machine)
  compute_type: auto
  cpu_threads: 4
  beam_size: 2
  vocabulary: [~60 technical terms]
  replacements: {~15 ASR fixes}

llm:
  enabled: false
  model: llama3.2:3b
  base_url: http://localhost:11434
  timeout_seconds: 4.0    # not in file; code default
  temperature: 0.1
  min_words_for_cleanup: 10
  min_similarity: 0.8
  keep_alive: 30m
  system_prompt / example_input / example_output ...

hotkey:
  key: ctrl+shift
  mode: toggle
  debounce_ms: 300        # code default; not in file

audio:
  sample_rate: 16000      # NOT the capture rate; native SR is used
  channels: 1
  preroll_ms: 500
  device_index: null
  target_rms_normalize: 0.08
  silence_threshold_energy: 0.0008

inject:
  method: paste           # unused
  restore_clipboard: true # unused (always restores)

ui:
  show_overlay: true
  pill_position: bottom_center
  pill_margin_bottom: 48
  sound_cues: false       # unused (cues disabled)
```

Known config defects:

- `main.py` reads `self.config.get("injection", {})` but the file defines `inject:`. `restore_delay_ms` / `post_paste_delay_ms` are therefore always 120/40.
- `inject.restore_clipboard` is never read.
- `audio.sample_rate` does not set the capture rate.
- Tray changes are not persisted.
- No schema/validation/migration; defaults are duplicated across modules.
- Section aliases (`stt`/`transcription`, `llm`/`cleanup`) exist in code but not in the file.

## 7. State model (implicit)

There is no explicit state machine. State is spread across `AudioRecorder._is_recording`, `VoxenApp._is_processing`, tray icon/action text, and overlay string states.

Effective transitions:

```
IDLE ──hotkey──► RECORDING ──hotkey/release──► FINALIZING ──ok──► TRANSCRIBING
  ▲                  │                                              │
  │                  └──too_short/zero_samples/silent──► SILENT/ERROR
  │                                                                 │
  │                                          cleanup enabled? ──► REFINING
  │                                                                 ▼
  └──────────────────── SUCCESS / SILENT / ERROR ◄────────────── INJECTING
```

Invalid transitions are prevented only by ad-hoc bool checks. There is no cancellation token, no timeout for the pipeline, and no recovery if the worker thread dies.

## 8. Storage, network, privacy

### Written to disk

| Path | Content | Retention |
|---|---|---|
| `%LOCALAPPDATA%\Voxen\last.wav` | Every successful take, normalized 16 kHz mono | Last take only |
| `<CWD>\temp_last_capture.wav` | Same audio | Last take only |
| `%LOCALAPPDATA%\Voxen\logs\voxen.log` | Device, raw transcript, corrections, timings | 3 × 2 MB rotation |
| `%USERPROFILE%\.cache\huggingface\hub` | Models (~6 GB) | Persistent |
| `%LOCALAPPDATA%\Voxen\models` | Legacy models (~291 MB) | Stale |

### Network

- Hugging Face downloads on first use for Whisper (if not cached); Moonshine calls `hf_hub_download` at every construction; Whistle/needle may download on first use.
- Ollama at `localhost:11434` only when cleanup is enabled.
- No telemetry, analytics, or cloud API.

### Privacy risks

- Audio and full transcripts persist unencrypted.
- Raw transcripts are logged at INFO.
- The CWD WAV can land in Dropbox/OneDrive/Downloads if launched there.
- No "delete after N minutes" or "never persist audio" switch.

## 9. Measured behavior baseline

Keep these as regression guards.

### Microphone

- Windows default: Realtek array, MME, 44.1 kHz, 2 ch.
- Resolver returns idx 1, not the name-matched headset or loopback device.
- Live ambient capture on the Realtek array is full-band (4–8 kHz ≈ 25% of energy).
- The old failing headset capture had ~97% of energy below 1 kHz and <0.3% above 4 kHz.

### ASR accuracy

- 12 clean TTS phrases (`bench_audio/p01…p12`):
  - Whistle: 2 minor errors (e.g. "pasted" for "paste").
  - `base.en`: exact on the control phrase, 0% WER.
- Known-good 37.3 s real take: coherent transcript with `base.en`.
- Accent corrections + `fix_capitalisation` added; `pythonic` is not altered (negative lookaround verified).

### Latency

| Configuration | Audio | Time | Notes |
|---|---|---|---|
| `base.en` CPU int8, beam 2 | 37.3 s | 4.5 s | RTF 0.12 |
| `base.en` CPU int8, beam 2 | 11 s | 4.3 s | RTF 0.39 |
| `base.en` CPU int8, beam 2 | 3.4 s | ~3.3–3.9 s | RTF ~1.0 |
| `base.en` CPU int8, beam 5 | 37.3 s | 11–16 s | RTF 0.3–0.43 |
| `small.en` CPU int8 | 37.3 s | 47–64 s | RTF >1; not viable |
| Whistle CPU | 37.3 s | 14–24 s | worse on real audio |
| `distil-small.en` CPU int8 | 3.4 s | ~13 s | 3–4× slower than base.en |
| `distil-medium.en` CPU int8 | 37.3 s | 85.3 s | RTF 2.37; 20× slower |

### Model benchmarks on this CPU

Neither distil model is faster than `base.en` here. `base.en` remains the default. `distil-small.en` and `distil-medium.en` are available in the tray but not recommended.

## 10. Tests and benchmarks

All tests are script-style (`python tests/test_x.py`), with `assert` and prints. No pytest, no fixtures, no CI, no coverage, no markers for hardware.

| File | What it does | Dependencies |
|---|---|---|
| `tests/test_stream_recovery.py` | device enumeration, invalid-device fallback, zero samples, stream persistence/close, hardware capture | real mic |
| `tests/test_accuracy_regression.py` | WER on TTS + control phrase, VAD on/off, tiny vs base | HF model, `test_speech.wav` fixture |
| `tests/test_regression_real_audio.py` | WER on `temp_real_take_*.wav`, or `bench_audio/p01.wav` fallback | real takes or bench fixture |
| `tests/test_cleanup_formatting.py` | Ollama formatting/guardrails | live Ollama |
| `tests/test_e2e.py` | hardware → mic → STT → cleanup → Notepad injection | real mic, Notepad, Ollama optional |
| `tests/test_ui_interactive.py` | overlay flags, auto-hide, multi-monitor, waveform, focus | interactive GUI |
| `tools/benchmark.py` | CUDA check, VAD comparison, tiny/base WER/latency | bench_audio |
| `tools/bench_models_vad.py` | tiny/base/large × VAD | bench_audio |

Verified on 2026-10-26:

- `py_compile` clean for all current modules.
- `test_accuracy_regression.py` → all pass; exact control transcript; 0% WER.
- `test_stream_recovery.py` tests 1–4 → pass.
- `uv build` succeeds but produces a broken wheel (see P0-1).
- Config parses; correction merge works (case-insensitive user entries).
- `fix_capitalisation` regex uses negative lookaround; `pythonic` is not altered.

## 11. Known defects and risks (ranked)

### P0 — blocks "works properly" / release

1. **Wheel/package is broken.**
   - `pyproject.toml` uses `packages = ["voxen"]` now (fixed), but the project has never been installed from a wheel and verified. `voxen = "voxen.main:main"` entry point exists. Needs a clean-venv smoke test.
   - No `src/` layout, no package namespace beyond `voxen/`, no console-script test.
2. **Optional engines are not reproducible.**
   - `needle` and `sherpa-onnx` are imported by `voxen/transcribe.py` but absent from `pyproject.toml`/`uv.lock`.
   - A clean `uv sync` will produce an environment where selecting those engines raises `ImportError`.
3. **Config is inconsistent and not persisted.**
   - `inject:` vs `injection:` key mismatch; `restore_clipboard`, `sound_cues`, `audio.sample_rate` unused.
   - Tray/device/model/mode changes are lost on restart.
   - No schema/validation/migration; defaults are duplicated across modules.
4. **Clipboard data loss.**
   - `inject.py` snapshots only text; if the clipboard held an image/files/HTML, the `finally` block calls `EmptyClipboard()`, destroying it.
   - Paste success is not verified; the pipeline ignores `inject_text`'s return value.
5. **Privacy defaults.**
   - Every take is written to CWD and `%LOCALAPPDATA%`.
   - Raw transcripts are logged at INFO.
   - No retention/opt-out switch.

### P1 — correctness/UX/scalability

6. **No explicit state machine / worker queue.**
   - `_is_processing` is unsynchronized; model swaps can race with the pipeline; no cancellation; each stop spawns a thread.
7. **Model switch freezes the UI and may download at runtime.**
   - Synchronous load on the Qt thread; Moonshine calls HF at construction.
8. **`detect_hardware()` recommends `base.en` on CPU**, which is slower than real time on short clips (RTF ~1.0). Diagnostics can steer users to a bad default. (Updated from the stale `small.en` recommendation.)
9. **Hotkey limitations.**
   - Fixed `Ctrl+Shift` by default, conflicts with common shortcuts; no conflict detection; no suppression; no per-app profiles; no runtime rebinding.
10. **Injection robustness.**
    - `keybd_event` is legacy; no `SendInput`; no UIPI/elevation handling; no fallback to typing/UI Automation; no target-app awareness.
11. **Overlay dead code.**
    - `ctypes.wintypes` is not imported in `overlay.py`; `nativeEvent` always throws internally, so `WM_MOUSEACTIVATE` suppression is inactive.
12. **Audio device UX.**
    - Tray lists every MME/DirectSound/WASAPI duplicate with the same name; no capability/quality info, no "test mic", no hot-plug handling beyond retry, no narrowband warning in the menu.
13. **Test suite is not a safety net.**
    - No pytest, no CI, no unit tests for config/inject/hotkey/cleanup; tests need a mic, GUI, Ollama, and network.
    - `test_regression_real_audio.py` feeds a 22.05 kHz file to a 16 kHz pipeline without resampling (patched to use `bench_audio/p01.wav` fallback).
14. **No single-instance guard / autostart / installer.**
15. **No streaming/partial transcription.**
    - Batch-only; no live caption; no progressive paste.
16. **No crash/diagnostics bundle.**
    - File log exists, but no version/env/device snapshot, no redaction, no export.
17. **CUDA is not actually usable.**
    - `cublas64_12.dll` is not on the system and no `nvidia` package is installed. `detect_hardware()` now correctly reports `cuda_usable=False`. The earlier `True` was a false positive. The app always runs on CPU.

### P2 — hygiene

18. `README.md` still contains mojibake in places and stale claims.
19. No `LICENSE` / `THIRD_PARTY_NOTICES.md` (PySide6 is LGPL; distribution needs compliance).
20. `main.py` has unused imports (`Signal`, `Slot`, `WhisperTranscriber`, `WhistleTranscriber`); `play_sound_cue` is a no-op.
21. `dist/` build artifact is reproducible but ignored; no signing/versioning policy.
22. `*.log` is ignored, but `temp_last_capture.wav`, `last.wav`, `bench_audio/` results are not (now ignored via updated `.gitignore`).
23. Old `%LOCALAPPDATA%\Voxen\models` and HF cache duplicate models; no cleanup command.
24. HF cache is ~6 GB because every experimented model remains cached.
25. No `py.typed`, no type checking config, no linter/formatter config.
26. No i18n; English-only models/prompts.

## 12. Target architecture (proposed)

### Principles

- **One source of truth for state** (explicit state machine, not booleans).
- **Ports and adapters**: domain/application code must not import `PySide6`, `sounddevice`, `win32clipboard`, `requests`, or `faster_whisper` directly.
- **Config is a typed schema** with defaults, validation, migration, and atomic persistence.
- **Heavy work is off the UI thread**, through a single serialized worker with cancellation.
- **Engines are plugins** with declared dependencies and a model manager.
- **Privacy by default**: no audio retention unless explicitly enabled; redacted logs.

### Proposed package layout

```
src/voxen/
├─ domain/
│  ├─ models.py            # AudioClip, Transcript, Word, DeviceInfo, SessionState
│  ├─ errors.py            # typed error taxonomy
│  └─ events.py            # RecordingStarted, TranscriptReady, InjectionFailed, ...
├─ application/
│  ├─ dictation_service.py # orchestrates the session state machine
│  ├─ ports.py             # AudioSource, Transcriber, TextRefiner, TextInjector, ConfigStore
│  └─ pipeline.py          # transcribe → refine → inject, cancellation-aware
├─ infrastructure/
│  ├─ audio/sounddevice_source.py
│  ├─ audio/dsp.py
│  ├─ asr/whisper_engine.py
│  ├─ asr/whistle_engine.py
│  ├─ asr/moonshine_engine.py
│  ├─ asr/registry.py
│  ├─ refine/ollama_refiner.py
│  ├─ inject/win32_injector.py
│  ├─ hotkey/pynput_hotkey.py
│  └─ config/yaml_store.py
├─ presentation/
│  ├─ overlay.py
│  ├─ tray.py
│  └─ settings_window.py
├─ bootstrap/
│  ├─ app.py               # composition root
│  └─ cli.py
└─ resources/config.default.yaml
```

### Core interfaces

```python
class AudioSource(Protocol):
    def start(self) -> None: ...
    def stop(self) -> AudioClip: ...          # float32 mono 16 kHz + metadata
    def cancel(self) -> None: ...
    def devices(self) -> list[DeviceInfo]: ...
    def select(self, device_id: str | None) -> None: ...

class Transcriber(Protocol):
    name: str
    def warmup(self) -> None: ...
    def transcribe(self, clip: AudioClip, hint: TranscribeHint) -> Transcript: ...

class TextRefiner(Protocol):
    def refine(self, text: str, context: RefineContext) -> RefineResult: ...

class TextInjector(Protocol):
    def inject(self, text: str) -> InjectionResult: ...  # success/target/error

class ConfigStore(Protocol):
    def load(self) -> AppConfig: ...
    def save(self, config: AppConfig) -> None: ...
```

### Session state machine

```
IDLE → ARMING → RECORDING → FINALIZING → QUEUED → TRANSCRIBING
     → REFINING (optional) → INJECTING → IDLE
Any state → FAILED → IDLE (with typed error + user-visible message)
```

- One worker owns transcription; model swaps are queued, never concurrent.
- Cancellation token aborts a take or a pipeline stage.
- Every transition emits an event; UI/tray subscribe to events rather than being called directly.

### Model manager

- Manifest per engine: files, SHA256, size, license, download URL.
- Cache under `%LOCALAPPDATA%\Voxen\models\<engine>\<model>`; no reliance on ambient HF cache.
- Atomic download + resume + progress events; offline mode uses cache only.
- Warmup in the background after selection; show "loading" state.
- CPU/GPU policy: `base.en` default on CPU; benchmark `small.en` on CUDA before recommending; never recommend `small.en` on this CPU.

### Injection

- Snapshot **all** clipboard formats via OLE (`OleGetClipboard`/`IDataObject`) or at minimum do not empty when the format is non-text; restore exactly.
- Prefer `SendInput` over `keybd_event`.
- Detect foreground target and elevation; if UIPI blocks paste, fall back to UI Automation `ValuePattern`/typing or surface a clear error.
- Verify paste with a clipboard sequence number / target read-back where possible.
- Optional per-app profiles (terminal → type, Office → paste).

### Packaging & operations

- Proper `src/` layout, `hatchling` package config, extras:
  - `voxen[whisper]`, `[whistle]`, `[moonshine]`, `[dev]`.
- Build a signed installer (Inno Setup/MSIX) or a portable PyInstaller bundle with models downloaded on first run.
- Single-instance mutex; autostart option; versioned migrations.
- Structured JSON logs with redaction; diagnostics bundle command.

### Testing strategy

- `pytest` with markers: `unit`, `integration`, `hardware`, `network`.
- Fakes: `FakeAudioSource`, `FakeTranscriber`, `FakeClock`, `InMemoryClipboard`.
- Golden WAV fixtures + WER thresholds; a 12-phrase bench set as a gate.
- CI on `windows-latest` for unit tests; hardware tests local-only.
- Contract tests for every engine adapter; config migration tests.

## 13. Migration plan

Each phase must leave the app runnable and keep the current accuracy fixes.

### Phase 0 — Baseline & safety net (no behavior change)

- Commit/tag the current working tree so the rewrite is reversible.
- Add `pytest` and smoke tests for the pure functions: `resample_to_16k`, `remove_rumble`, `normalize_audio`, `speech_chunks`, `collapse_repeats`, `apply_vocabulary_corrections`, `fix_capitalisation`, config merge.
- Add a golden test that transcribes `bench_audio/p01…p12` and asserts WER thresholds.
- **Acceptance:** current app launches, tests pass, no functional diff.

### Phase 1 — Packaging & dependency truth

- Move to `src/voxen/` with a real package and entry point.
- Fix the wheel; verify `uv build` + install into a clean venv + `voxen --version`.
- Declare `needle`, `sherpa-onnx`, `huggingface-hub` as optional extras; ensure a clean `uv sync --extra whisper` works.
- Add `LICENSE` and `THIRD_PARTY_NOTICES.md`.
- **Acceptance:** clean-venv install runs the app; selecting each declared engine works or is hidden when unavailable.

### Phase 2 — Config v2

- Define `AppConfig` (pydantic or dataclasses) with one set of defaults.
- Version the YAML (`config_version`), migrate v1 → v2, write atomically.
- Fix `inject`/`injection`, persist tray changes, remove unused keys or implement them.
- **Acceptance:** changing mic/mode/model in the tray survives restart; invalid config yields actionable errors.

### Phase 3 — Core extraction & state machine

- Introduce `DictationService`, ports, events, and the explicit state machine.
- Move transcription to a single worker queue; model swaps queued; add cancellation.
- Make injection return a real result; surface failures.
- Fix clipboard preservation for non-text formats.
- **Acceptance:** recorded state transitions; no UI freeze on model switch; no clipboard loss in an image-clipboard test; pipeline failure shows a message.

### Phase 4 — Audio robustness

- Device capability probe (native SR, channels, host API, full-band check).
- Prefer WASAPI shared/exclusive with fallback; dedupe tray device list by physical endpoint.
- Hot-plug events; "test microphone" and live level meter in settings.
- Stop writing WAVs by default; add explicit "keep last take"/debug option + retention.
- **Acceptance:** changing devices (USB/Bluetooth) works without restart; narrowband device warns; no audio files by default.

### Phase 5 — Accuracy & engine registry

- Engine registry with lazy load, warmup, manifest, checksum, offline mode.
- Fix `detect_hardware` recommendations; benchmark `small.en` on CUDA before changing defaults.
- Expand the bench set (real mic + accents + technical vocabulary) with per-engine WER.
- Keep: follow Windows default mic, anti-aliased resampling, 50 Hz HPF, no double VAD, `base.en` CPU default, accent prompt + corrections + `fix_capitalisation`.
- **Acceptance:** bench WER ≤ baseline on every engine; warm latency budget documented.

### Phase 6 — UX, settings, operations

- Settings window (mic, hotkey, engine, cleanup, privacy, autostart).
- Single-instance, autostart, installer, code signing, update check (opt-in).
- Diagnostics bundle with redacted env/logs.
- **Acceptance:** install → first-run model download → dictate in Notepad without editing YAML.

### Phase 7 — Optional streaming

- Add a streaming engine (sherpa/whisper.cpp/Parakeet) behind the same `Transcriber` port.
- Partial captions in the overlay; commit on finalization.
- **Acceptance:** no regression to batch accuracy/latency; feature-flagged.

## 14. Verification recipes

```powershell
# Compile
.\.venv\Scripts\python.exe -m py_compile voxen\*.py

# Device resolution (must follow the Windows default, never loopback)
.\.venv\Scripts\python.exe -c "import sounddevice as sd; from voxen.audio import resolve_input_device, is_loopback_device; print(sd.default.device, resolve_input_device()); print('loopback check:', is_loopback_device('Stereo Mix'))"

# DSP invariants
.\.venv\Scripts\python.exe -c "import wave,numpy as np; from voxen.audio import resample_to_16k; w=wave.open('bench_audio/p01.wav','rb'); sr=w.getframerate(); x=np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16).astype(np.float32)/32768.; y=resample_to_16k(x,sr); print(len(x)/sr, len(y)/16000)"

# Accuracy gate
.\.venv\Scripts\python.exe tests\test_accuracy_regression.py

# Stream lifecycle
.\.venv\Scripts\python.exe tests\test_stream_recovery.py

# Capitalisation guard
.\.venv\Scripts\python.exe -c "from voxen.transcribe import fix_capitalisation; print(fix_capitalisation('i use pythonic code and python'))"

# Package install smoke test (after fixing packaging)
uv build
# create clean venv, pip install dist\*.whl, run `voxen`
```

## 15. Instructions for the next AI agent

1. **Read this document and the current files before editing.** The app is now a `voxen/` package; imports are relative inside the package and `voxen.*` from outside.
2. **Never run `git clean -fdx`, `git reset --hard`, or delete untracked files.** The working tree is clean and committed.
3. **Preserve these correctness decisions** (they fixed the "every word is wrong" bug):
   - Follow the Windows default microphone before name-keyword heuristics.
   - Never select an 8 kHz telephony or loopback endpoint silently; warn on <16 kHz.
   - Re-prime the stream when the device changes.
   - Anti-aliased resampling to 16 kHz; 50 Hz high-pass; RMS normalization with gain cap.
   - No double VAD on VAD-chunked audio.
   - `base.en` (not `tiny.en`, not `small.en`, not `distil-*`) is the CPU default; `beam_size=2`, `best_of=1`.
   - Merge built-in corrections with user replacements, case-insensitively.
   - Accent prompt + spoken-form corrections + `fix_capitalisation` after corrections.
4. **Keep the 16 kHz mono `float32` contract** at the `Transcriber` boundary.
5. **Do not reintroduce raw transcript logging at INFO**; redact it behind a debug flag.
6. **Do not claim a model is "recommended" without measuring RTF on this CPU.** Both distil models were measured and are slower.
7. Before changing defaults, run the accuracy gate and record WER + latency in the commit.
8. On this machine, CUDA is **not** actually usable (`cublas64_12.dll` missing). Treat the app as CPU-only unless CUDA is fixed.
9. The app has no remote. Use Conventional Commits and keep the package runnable until the migration is complete.
10. Ask the owner before: changing the default engine/model, enabling audio retention, adding telemetry, or changing the install/distribution format.

## 16. Open decisions for the owner

1. **Distribution:** portable folder, PyInstaller one-file, Inno Setup installer, or MSIX?
2. **Privacy:** keep the last take by default, or delete audio immediately and keep only transcripts?
3. **Default engine on this machine:** `base.en` CPU (known good) vs CUDA (needs cublas fix).
4. **Streaming:** is partial/live captioning required, or is batch dictation enough?
5. **Engine scope:** Whisper only, or keep Whistle/Moonshine/Parakeet as selectable plugins?
6. **Multilingual:** English-only, or auto-detect?
7. **Hotkey:** fixed `Ctrl+Shift`, or user-rebindable with conflict detection?
8. **Cleanup:** keep Ollama optional, or replace with a deterministic punctuation model / local LLM runtime?

---

### Appendix A — Current corrections

`DEFAULT_CORRECTIONS` in `voxen/transcribe.py` covers: Voxen brand, FastAPI, PostgreSQL, GitHub, Claude Code, Ollama, API/APIs, RAG, agentic AI, vector database(s), plus spoken-form accent corrections (word press → WordPress, data base → database, end point → endpoint, back end → backend, front end → frontend, open a i / open ai → OpenAI, deep seek → DeepSeek, on a miss → Ollama, long chain / lang chain → LangChain, pin cone → Pinecone, n eight n → n8n, vox in / vox en / voice bud → Voxen). `config.yaml` adds Alima/Olama → Ollama, Gvector → pgvector, voice bot/voice but/voice bud → Voxen, fast api, post gres, postgres, github, claude code, agentic ai.

### Appendix B — Vocabulary prompt

`stt.vocabulary` (~60 terms) is passed as:

- Whisper `initial_prompt` (accent-aware prompt mentioning Indian English and technical terms)
- Whistle `keywords`
- Ollama protected vocabulary
- tray/UI metadata (indirectly)

### Appendix C — Error/status taxonomy

`AudioRecorder.stop_recording()` status:

| reason | meaning | UI today |
|---|---|---|
| `ok` | speech captured | transcribe |
| `silent_speech` | success but raw RMS below threshold | "No speech detected" |
| `too_short` | <200 ms of audio | "Too short. Hold the hotkey…" |
| `zero_samples` | no callback blocks | "Microphone Error" |
| `not_recording` | stop without start | silent return |

Pipeline failures are currently a single `except Exception` → "Error during dictation"; there is no typed error surface.

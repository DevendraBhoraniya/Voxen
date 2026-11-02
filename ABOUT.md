# Voxen — Architecture & Project Overview

> **Voxen** is a fully local, offline Windows dictation application built with Python 3.11 and PySide6. It captures microphone audio, transcribes speech using faster-whisper (CTranslate2), optionally cleans text with a local Qwen2.5-0.5B LLM, and injects the result into any focused Windows application via clipboard paste.

---

## 1. What Is Voxen?

Voxen is a **desktop dictation tool** — the Windows equivalent of Wispr Flow. It runs entirely on the user's machine with zero cloud dependencies, zero telemetry, and zero network calls after initial model download. The user presses a global hotkey (Ctrl+Shift by default), speaks, releases, and the transcribed text appears at the cursor in whatever app is focused.

### Key Capabilities
- **Offline STT** via faster-whisper (CTranslate2) on CPU (int8) or CUDA
- **Semantic cleanup** via Qwen2.5-0.5B (Q4_K_M GGUF) — removes self-corrections, filler words, false starts; adds punctuation
- **Ollama fallback** — optional server-based cleanup if Ollama daemon is running
- **Global hotkey** — toggle or hold-to-talk mode, configurable combination
- **Floating pill overlay** — compact, animated waveform visualizer with status text
- **System tray** — model/mode/device selection, settings, quit
- **Settings window** — sidebar-navigated configuration (7 sections)
- **Pre-roll capture** — 500ms ring buffer so first syllable is never clipped
- **Audio normalization** — RMS normalization, DC/rumble removal, anti-aliased resampling
- **Loopback filtering** — automatically excludes Stereo Mix / WASAPI loopback devices

---

## 2. Project Structure

```
F:\test\STT\
├── voxen/                    # Main package
│   ├── __init__.py           # Package metadata (__version__, __app_name__)
│   ├── __main__.py           # Entry point: python -m voxen
│   ├── main.py               # Application controller (VoxenApp) + diagnostics
│   ├── audio.py              # AudioRecorder, device discovery, resampling, normalization
│   ├── transcribe.py         # WhisperTranscriber, VAD chunking, vocabulary corrections
│   ├── cleanup.py            # OllamaCleaner + LlamaCleaner (Qwen2.5-0.5B)
│   ├── inject.py             # Clipboard save/inject/restore via Win32 API
│   ├── hotkey.py             # Global keyboard hook (pynput) — toggle/hold modes
│   ├── overlay.py            # FloatingPill QWidget + RealMicWaveformVisualizer
│   ├── tray.py               # SystemTrayManager (QSystemTrayIcon + menus)
│   ├── settings_window.py    # Non-modal settings window with sidebar navigation
│   ├── theme.py              # Design tokens (colors, typography, spacing, radius)
│   └── config.py             # YAML config loader + correction merging
├── tools/
│   ├── setup_cleanup_model.py # One-time Qwen model download (~397 MB)
│   ├── benchmark.py          # Performance benchmarking
│   ├── bench_models_vad.py   # VAD model comparison
│   └── play_last_capture.py  # Audio playback utility
├── tests/
│   ├── test_accuracy_regression.py   # WER accuracy test (0% WER on test audio)
│   ├── test_stream_recovery.py       # Audio device recovery tests
│   ├── test_regression_real_audio.py  # Real microphone regression suite
│   ├── test_cleanup_formatting.py    # Cleanup formatting tests
│   ├── test_e2e.py                   # End-to-end integration tests
│   └── test_ui_interactive.py        # UI interaction tests
├── config.yaml               # Runtime configuration
├── pyproject.toml            # Package metadata + dependencies
├── ABOUT.md                  # This file
└── README.md                 # User-facing documentation
```

---

## 3. Architecture Overview

### 3.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        VoxenApp (main.py)                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │ Audio    │  │ Transcriber│  │ Cleaners │  │ Injector     │   │
│  │ Recorder │  │ (Whisper)  │  │ (Qwen +  │  │ (Clipboard)  │   │
│  │          │  │            │  │  Ollama) │  │              │   │
│  └────┬─────┘  └─────┬──────┘  └────┬─────┘  └──────┬───────┘   │
│       │              │              │               │           │
│  ┌────┴──────────────┴──────────────┴───────────────┴───────┐   │
│  │              Dictation Pipeline (_process_pipeline)       │   │
│  │  1. Capture audio → 2. Transcribe → 3. Clean → 4. Inject  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │ Overlay  │  │ Tray     │  │ Hotkey   │  │ Settings     │   │
│  │ (Pill)   │  │ (Menu)   │  │ (Global) │  │ (Window)     │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Data Flow (Dictation Pipeline)

```
1. User presses hotkey (Ctrl+Shift)
   └─► HotkeyListener._on_press() fires on_trigger callback

2. start_recording()
   └─► AudioRecorder.start_recording()
       └─► Prepends 500ms pre-roll from ring buffer
       └─► Begins capturing chunks via PortAudio callback

3. User releases hotkey
   └─► stop_recording()
       └─► AudioRecorder.stop_recording()
           └─► Resamples to 16kHz (anti-aliased FIR)
           └─► Removes rumble (50Hz high-pass)
           └─► Normalizes RMS to 0.05
           └─► Returns (audio, duration, status)

4. _process_pipeline() [background thread]
   └─► WhisperTranscriber.transcribe(audio)
       └─► VAD chunking (Silero VAD, threshold=0.50)
       └─► Whisper inference (beam_size=2, best_of=1)
       └─► Collapse repeats, vocabulary corrections
       └─► Returns raw_text
   └─► Cleanup (priority order):
       ├─► LlamaCleaner (Qwen2.5-0.5B) if available
       ├─► OllamaCleaner if Ollama daemon running
       └─► Skip if neither available
   └─► inject.inject_text(final_text)
       └─► Save clipboard → Set text → Simulate Ctrl+V → Restore clipboard

5. Overlay shows "Done" → auto-hides after 2s
```

### 3.3 Threading Model

| Thread | Purpose | Lifecycle |
|--------|---------|-----------|
| **Main (Qt)** | UI event loop, signal dispatch | App lifetime |
| **Hotkey** | pynput global listener | App lifetime |
| **Audio Callback** | PortAudio input callback (~30ms chunks) | Stream lifetime |
| **Model Loader** | Daemon thread loading Whisper at startup | One-shot |
| **Pipeline Worker** | STT + cleanup + injection per dictation | Per-dictation |
| **Qwen Loader** | LlamaCleaner model load (in __init__) | App lifetime |

### 3.4 Signal/Slot Architecture

```
VoxenApp (QObject)
├── model_ready: Signal()              → _on_model_ready()
├── model_load_failed: Signal(str)      → _on_model_load_failed(str)
├── overlay.state_signal: Signal(str,str) → FloatingPill state handler
├── overlay.audio_signal: Signal(float, object) → waveform update
├── overlay.hide_signal: Signal()       → hide pill
├── tray.toggle_recording_requested     → toggle_recording()
├── tray.mode_changed                  → _on_mode_changed(mode)
├── tray.device_changed                → _on_device_changed(idx)
├── tray.model_changed                 → _on_model_changed(key)
├── tray.cleanup_toggled               → _on_cleanup_toggled(bool)
├── tray.overlay_toggled               → _on_overlay_toggled(bool)
└── tray.quit_requested                → quit()
```

---

## 4. Module Descriptions

### `voxen/main.py` — Application Controller (VoxenApp)
- **Role**: Central orchestrator connecting all components
- **Key classes**: `VoxenApp(QObject)`, `run_first_run_diagnostics()`
- **Responsibilities**: Config loading, component initialization, async model loading, dictation pipeline, tray signal wiring, shutdown
- **Signals**: `model_ready`, `model_load_failed`
- **Size**: ~600 lines (candidate for splitting)

### `voxen/audio.py` — Audio Capture & Processing
- **Role**: Microphone capture, device discovery, audio preprocessing
- **Key classes**: `AudioRecorder`
- **Key functions**: `resample_to_16k()`, `normalize_audio()`, `remove_rumble()`, `is_loopback_device()`, `resolve_input_device()`, `list_audio_devices()`
- **Features**: Pre-roll ring buffer (500ms), PortAudio streaming, device hot-plug, loopback filtering, anti-aliased FIR resampling, RMS normalization
- **Thread safety**: `threading.Lock` around chunk buffers

### `voxen/transcribe.py` — Speech-to-Text Engine
- **Role**: Whisper transcription with VAD chunking and vocabulary corrections
- **Key classes**: `WhisperTranscriber`
- **Key functions**: `speech_chunks()`, `collapse_repeats()`, `apply_vocabulary_corrections()`, `create_transcriber()`
- **VAD**: Silero VAD (bundled with faster-whisper), threshold=0.50
- **Corrections**: Regex-based vocabulary replacements (FastAPI, PostgreSQL, Voxen, etc.)
- **Parameters**: beam_size=2, best_of=1, temperature=0.0, no_speech_threshold=0.7

### `voxen/cleanup.py` — Text Cleanup
- **Role**: Optional text refinement (two engines)
- **Key classes**: `OllamaCleaner` (server-based), `LlamaCleaner` (local Qwen2.5-0.5B)
- **Priority**: LlamaCleaner → OllamaCleaner → skip
- **LlamaCleaner**: Q4_K_M GGUF, 400MB RAM, 0.3-0.8s/clip, CPU-only
- **Guard**: MIN_WORDS=4 (skips short phrases), safety length check (2.5x)

### `voxen/inject.py` — Clipboard Injection
- **Role**: Safe text injection into focused Windows app
- **Key functions**: `inject_text()`, `set_clipboard_text()`, `get_clipboard_text()`, `simulate_paste()`
- **Flow**: Save clipboard → Set text → Release modifiers → Ctrl+V → Restore clipboard
- **Retry logic**: 10 attempts with 15ms delay for clipboard lock

### `voxen/hotkey.py` — Global Keyboard Hook
- **Role**: Global hotkey monitoring via pynput
- **Key classes**: `HotkeyListener`
- **Modes**: toggle (press to start/stop) or hold (press and hold to record)
- **Features**: Debounce (300ms), key normalization (left/right variants), combination parsing
- **Thread**: Spawns daemon thread per trigger

### `voxen/overlay.py` — Floating Pill UI
- **Role**: Compact, animated overlay shown during recording/processing
- **Key classes**: `FloatingPill`, `RealMicWaveformVisualizer`, `LightweightSpinner`
- **Features**: Frameless, always-on-top, non-activating (WS_EX_NOACTIVATE), DPI-aware, multi-monitor support
- **Waveform**: 11 bars, time-based sine wave animation, RMS-influenced, ~30 FPS
- **States**: IDLE (hidden), RECORDING, TRANSCRIBING, CLEANING, SUCCESS, ERROR, SILENT

### `voxen/tray.py` — System Tray
- **Role**: System tray icon and context menu
- **Key classes**: `SystemTrayManager`
- **Features**: Model selector, mode toggle, device selector, cleanup toggle, overlay toggle, settings, quit
- **Notifications**: "Voxen Ready", error messages

### `voxen/settings_window.py` — Settings UI
- **Role**: Non-modal settings window with sidebar navigation
- **Sections**: General, Dictation, Audio, Transcription, Text cleanup, Privacy, About
- **Features**: Device selection, model selection, mode toggle, cleanup toggle

### `voxen/theme.py` — Design Tokens
- **Role**: Centralized color, typography, spacing, radius tokens
- **Colors**: Dark theme with purple accent (#A36BFF), red recording (#FF4D6D)
- **Spacing**: XS(4), SM(8), MD(12), LG(16), XL(24), XXL(32)
- **Typography**: Segoe UI, sizes 10-24px, weights 400-700

### `voxen/config.py` — Configuration
- **Role**: YAML config loading + correction merging
- **Functions**: `load_config()`, `build_corrections()`
- **Safe**: Uses `yaml.safe_load()`

---

## 5. Configuration (config.yaml)

```yaml
stt:
  model: base.en                    # Whisper model (offline, cached)
  language: en                      # English (avoids language misdetection)
  device: auto                      # cuda or cpu (auto-detect)
  compute_type: auto                 # float16 (GPU) or int8 (CPU)
  cpu_threads: 4                    # Worker threads
  beam_size: 2                      # Beam search width
  vocabulary: [...]                 # ~90 technical terms for biasing
  replacements: {...}               # Word-level corrections

llm:
  enabled: false                    # Ollama cleanup (off by default)
  llama_cleanup: true               # Qwen2.5-0.5B local cleanup (on)
  model: llama3.2:3b                # Ollama model name
  base_url: http://localhost:11434  # Ollama endpoint
  min_words_for_cleanup: 10         # Skip short utterances

hotkey:
  key: ctrl+shift                   # Global hotkey combination
  mode: toggle                      # toggle | hold

audio:
  sample_rate: 16000                # Target sample rate
  channels: 1                       # Mono
  preroll_ms: 500                   # Pre-roll buffer
  device_index: null                # null = auto-detect
  target_rms_normalize: 0.05        # Normalization target (reduced for accuracy)
  silence_threshold_energy: 0.0015  # Silence detection threshold

inject:
  method: paste                     # Clipboard + Ctrl+V
  restore_clipboard: true            # Restore original clipboard after

ui:
  show_overlay: true                # Show floating pill
  pill_position: bottom_center      # Screen position
  pill_margin_bottom: 48            # Bottom margin
  sound_cues: false                 # Audio feedback
```

---

## 6. Dependencies

### Core (pyproject.toml)
| Package | Version | Purpose |
|---------|---------|---------|
| faster-whisper | ≥1.0.0 | STT engine (CTranslate2) |
| huggingface-hub | ≥0.20.0 | Model download (setup only) |
| numpy | ≥1.26.0 | Audio processing |
| pynput | ≥1.8.0 | Global hotkey listener |
| pyside6 | ≥6.6.0 | Qt UI framework |
| pywin32 | ≥306 | Windows clipboard API |
| pyyaml | ≥6.0 | Config parsing |
| requests | ≥2.31.0 | Ollama HTTP client |
| sounddevice | ≥0.4.6 | PortAudio wrapper |

### Optional
| Extra | Package | Purpose |
|-------|---------|---------|
| llama | llama-cpp-python ≥0.3.0 | Qwen2.5-0.5B inference |

### External (not pip)
| Tool | Purpose |
|------|---------|
| Ollama | Optional LLM server (llama3.2:3b) |
| CUDA Toolkit | Optional GPU acceleration |

---

## 7. Audio Processing Pipeline

```
Raw Microphone (44100 Hz, 1 ch)
    │
    ▼
┌─────────────────┐
│ Resample to 16k │  Anti-aliased FIR low-pass (7 kHz) + linear interpolation
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Remove Rumble   │  50Hz high-pass FIR (511 taps) — removes DC + HVAC noise
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Normalize RMS   │  Target 0.05 RMS, max gain 8x, clip to ±0.99
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ VAD Chunking    │  Silero VAD (threshold=0.50, min_silence=400ms)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Whisper STT     │  base.en, beam_size=2, best_of=1, temp=0.0
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Post-process    │  Collapse repeats, vocabulary corrections, capitalization
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Cleanup (opt)   │  LlamaCleaner (Qwen) → OllamaCleaner → skip
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Inject          │  Clipboard save → set text → Ctrl+V → restore clipboard
└─────────────────┘
```

---

## 8. Security Considerations

| Area | Status | Notes |
|------|--------|-------|
| Secrets/credentials | ✅ None stored | No API keys, passwords, or tokens in code |
| Config parsing | ✅ Safe | Uses `yaml.safe_load()` |
| Eval/exec | ✅ None | No dynamic code execution |
| Shell commands | ⚠️ Minor | `os.system()` for opening config file (hardcoded path) |
| Thread safety | ⚠️ Partial | Hotkey spawns unbounded threads; audio callback has lock |
| Input validation | ⚠️ Missing | Config values not type-checked |
| Clipboard race | ⚠️ Possible | No mutex around clipboard operations |
| Network access | ✅ None at runtime | Fully offline after model download |
| File permissions | ✅ OK | Logs in %LOCALAPPDATA%, models in user cache |

---

## 9. Performance Characteristics

### Latency Breakdown (typical 5s dictation)
| Stage | Duration | Notes |
|-------|----------|-------|
| Audio capture | 5.0s | Real-time (user speaks) |
| Resample + normalize | ~0.05s | NumPy vectorized |
| VAD chunking | ~0.1s | Silero ONNX |
| Whisper STT | ~3.5s | base.en on CPU int8, 4 threads |
| LlamaCleaner | ~0.5s | Qwen2.5-0.5B Q4_K_M |
| Injection | ~0.2s | Clipboard + paste simulation |
| **Total turnaround** | **~4.3s** | From release to text appearing |

### Resource Usage
| Resource | Value |
|----------|-------|
| RAM (idle) | ~200 MB (Whisper model loaded) |
| RAM (with Qwen) | ~600 MB |
| CPU (idle) | ~0% |
| CPU (transcribing) | ~80-100% (4 threads) |
| Disk (models) | ~141 MB (base.en) + 397 MB (Qwen, optional) |
| Disk (cache) | ~141 MB (HF cache) |

### Optimization Notes
- `beam_size=2` (down from 5) — minimal accuracy loss, 2x faster
- `best_of=1` — no candidate re-ranking
- `temperature=0.0` — deterministic, no sampling
- `condition_on_previous_text=False` — prevents context drift
- Pre-roll buffer prevents first-syllable clipping
- VAD filtering skips silence (reduces Whisper input size)

---

## 10. Testing

| Test File | Coverage | Status |
|-----------|----------|--------|
| test_accuracy_regression.py | WER accuracy (0% on test audio) | ✅ Pass |
| test_stream_recovery.py | Audio device recovery (5 tests) | ✅ Pass |
| test_regression_real_audio.py | Real mic accuracy + WER | ✅ Pass |
| test_cleanup_formatting.py | Cleanup output validation | ✅ Pass |
| test_e2e.py | End-to-end pipeline | ✅ Pass |
| test_ui_interactive.py | UI interaction tests | ✅ Pass |

### Test Audio
- `test_speech.wav` — "Voxen local speech to text dictation works perfectly." (committed fixture)

---

## 11. Build & Run

### Prerequisites
- Python 3.11+
- Windows 10/11
- Microphone

### Setup
```bash
# Install dependencies
uv sync  # or: pip install -e .

# (Optional) Download Qwen cleanup model
python tools/setup_cleanup_model.py

# Run
uv run voxen  # or: python -m voxen
```

### Packaging
```bash
# Build wheel
python -m build

# The pyproject.toml includes config.yaml in the wheel
```

---

## 12. Known Limitations

1. **Windows-only** — Uses Win32 API (clipboard, hotkeys, window styles)
2. **CPU-bound** — No GPU acceleration without CUDA toolkit
3. **English-only** — `language: en` hardcoded (configurable but untested)
4. **Single microphone** — No multi-mic mixing
5. **No streaming STT** — Full utterance processed after release (not real-time)
6. **Clipboard dependency** — Injection fails if target app blocks clipboard
7. **No undo** — Injected text cannot be undone via app (relies on target app undo)

---

## 13. Future Roadmap

- [ ] GPU acceleration (CUDA) for sub-1s latency
- [ ] Streaming transcription (real-time partial results)
- [ ] Multi-language support
- [ ] Custom wake word / voice activation
- [ ] Plugin system for custom cleanup rules
- [ ] macOS/Linux port (replace Win32 APIs)
- [ ] Auto-update mechanism
- [ ] Telemetry (opt-in, local-only analytics)

---

*Generated for Voxen v0.1.0 — Last updated: 2026-11-02*
*Fully offline. No cloud. No telemetry. Your voice stays on your machine.*
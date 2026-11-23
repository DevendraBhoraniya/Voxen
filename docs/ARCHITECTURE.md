# Voxen — Architecture (current code)

**Snapshot date:** 2026-11-23  
**Repo:** `F:\test\STT` (Windows 11, branch `main`, remote `DevendraBhoraniya/Voxen`)  
**Verified:** every statement below points to a file/function present in the
working tree; claims that could not be executed (real mic, real Windows
clipboard, second Voxen instance) are marked unverified.

---

## 1. What Voxen is

A fully local, offline Windows dictation utility (Wispr Flow clone). One user
presses a global hotkey, the app records microphone audio, transcribes it
offline with faster-whisper (CTranslate2), optionally cleans the text with a
local Qwen2.5-0.5B model, and pastes the result into the focused application
via the Windows clipboard.

**Assumed load:** one user, one machine, one instance at a time. No server, no
background jobs, no cloud calls after model download. The only shared state is
the Windows clipboard and the microphone device.

---

## 2. Runtime environment (this machine)

| Item | Value |
|------|-------|
| OS | Windows 11 |
| Python | 3.11.15 (uv-managed venv at `.venv`) |
| Package manager | uv |
| CUDA | `detect_hardware()` reports `cuda_usable=False` (`cublas64_12.dll` not found) — effectively CPU-only |
| Default input | Realtek microphone array via the Windows default device |

Run:
```
.\.venv\Scripts\python.exe -m voxen       # launch app
uv run voxen                              # via entry point
.\.venv\Scripts\python.exe -m compileall -q voxen tests tools
uv sync --extra test
uv run pytest tests/ -rs -q               # CI=true skips hardware stages
```

---

## 3. Dependency inventory

Declared in `pyproject.toml`: faster-whisper, huggingface-hub, numpy, pynput,
pyside6, pywin32, pyyaml, sounddevice. Optional: `llama` (llama-cpp-python),
`test` (pytest).

`requests` is NOT a direct dependency and is not imported anywhere in `voxen/`
(grep confirmed). It may arrive transitively (e.g. via huggingface-hub) but is
not referenced by Voxen code.

`needle` and `sherpa-onnx` (Whistle/Moonshine) referenced in older docs are
NOT installed and NOT referenced in current code.

---

## 4. Repository map

```
F:\test\STT\
├─ voxen/                      # Application package
│  ├─ __init__.py              # __version__, __app_name__
│  ├─ __main__.py              # python -m voxen entry -> voxen.main:main
│  ├─ main.py                  # VoxenApp controller, pipeline, logging, mutex
│  ├─ config.py                # load_config, save_config (atomic), validation
│  ├─ audio.py                 # AudioRecorder, device discovery, DSP
│  ├─ transcribe.py            # WhisperTranscriber (faster-whisper), VAD, corrections
│  ├─ cleanup.py               # LlamaCleaner (Qwen2.5-0.5B, local only)
│  ├─ inject.py                # Clipboard save/paste/restore with non-text guard
│  ├─ hotkey.py                # HotkeyListener (pynput, bounded queue)
│  ├─ overlay.py               # FloatingPill (PySide6, WS_EX_NOACTIVATE)
│  ├─ tray.py                  # SystemTrayManager
│  └─ settings_window.py       # Settings UI (faster-whisper models only)
├─ tests/                      # pytest suite (hardware stages skip under CI)
├─ tools/                      # benchmarking / diagnostics
├─ docs/ARCHITECTURE.md        # this file
├─ config.yaml                 # Runtime configuration
├─ pyproject.toml              # Dependencies, entry point, packaging
├─ uv.lock                     # Lockfile (regenerated; requests removed)
├─ .github/workflows/ci.yml    # CI: windows-latest, uv sync --extra test, pytest
├─ README.md
└─ .gitignore
```

---

## 5. Module contracts (verified against source)

### voxen/main.py — `VoxenApp(QObject)`
- `main()` (line ~587): sets the Windows AppUserModelID, loads `config.yaml`,
  creates a **named mutex** `Voxen.Dictation.App.1.0` via `win32event.CreateMutex`;
  if `ERROR_ALREADY_EXISTS` it prints a message and `sys.exit(1)`. The mutex
  import is guarded (`try/except ImportError`) so non-Windows degrades. The
  actual second-instance behavior is unverified (requires running the Qt app
  twice).
- `VoxenApp.__init__` builds `AudioRecorder`, `LlamaCleaner`, `FloatingPill`,
  `SystemTrayManager`, `HotkeyListener`, and starts a single long-lived
  `PipelineWorker` thread reading from a bounded `queue.Queue(maxsize=2)`.
- `_load_model_async()` loads the faster-whisper model off the Qt thread and
  emits `model_ready` / `model_load_failed`.
- `stop_recording()` hands `(audio, duration)` to the pipeline worker.
- `_process_pipeline()`: transcribe → LlamaCleaner cleanup → `inject.inject_text()`.
- Config-mutating handlers (`_on_mode_changed`, `_on_device_changed`,
  `_on_model_changed`, `_on_cleanup_toggled`, `_on_overlay_toggled`) call
  `app_config.save_config(self.config_path, self.config)` so tray/settings
  changes persist to disk.
- Reads the `inject:` section (not `injection:`) for
  `restore_delay_ms` / `post_paste_delay_ms` (line ~532).
- `quit()` stops the hotkey listener, recorder, pipeline, tray, overlay, then
  `QApplication.quit()`.

### voxen/config.py — `load_config`, `save_config`
- `load_config(path="config.yaml")`: `yaml.safe_load`, validates/coerces
  `stt`, `audio`, `llm`, and `inject` sections; returns `{}` on failure.
- `save_config(path, data)`: writes to a temp file in the same directory then
  `os.replace()` for atomic persistence.

### voxen/audio.py — `AudioRecorder`
- `refresh_audio_devices()` only re-queries `sd.query_devices()`. It does **not**
  call `sd._terminate()`/`sd._initialize()` (removed — that killed the open
  stream when the tray mic menu opened).
- `safe_reinitialize_portaudio(recorder)`: re-initializes PortAudio only when
  `not recorder.is_recording and not recorder._is_stream_active()`; refuses
  with a warning otherwise.
- `stop_recording()`: resamples to 16 kHz (anti-aliased FIR), 50 Hz high-pass
  rumble removal, RMS normalization, writes the last take ONLY to
  `%LOCALAPPDATA%\Voxen\last.wav` (no CWD `temp_last_capture.wav` write).

### voxen/transcribe.py — `WhisperTranscriber`
- faster-whisper `base.en` CPU int8 default; VAD chunking (Silero) into
  ≤28 s windows; `beam_size=2`, `best_of=1`, `temperature=0.0`,
  `condition_on_previous_text=False`.
- Applies `collapse_repeats` and vocabulary corrections. Logs raw output length
  at **DEBUG** (redacted — the actual transcript is never logged at INFO).
- `fix_capitalisation()` / `ALWAYS_CAPITALISE` were **deleted** (grep confirmed
  no callers).

### voxen/cleanup.py — `LlamaCleaner`
- Local Qwen2.5-0.5B (Q4_K_M GGUF) cleanup only. Ollama was removed entirely
  (commit `fd8f167`); `OllamaCleaner`, `check_ollama_status`,
  `sanitize_formatting` no longer exist.
- Skips short clips (`MIN_WORDS=4`) and high-confidence/no-correction-marker
  takes. Safety length check (output ≤ 2.5× input). Never raises.

### voxen/inject.py — `inject_text`
- Before overwriting, `clipboard_has_non_text()` enumerates clipboard formats;
  if anything besides plain text is present (image, file list, HTML), injection
  **aborts and returns False without modifying the clipboard**.
- Tracks `clipboard_modified`; the `finally` block restores/empties only when
  Voxen actually wrote to the clipboard.

### voxen/hotkey.py — `HotkeyListener`
- pynput global hook, toggle or hold mode, 300 ms debounce. Callbacks run in a
  single bounded worker thread (`queue.Queue(maxsize=2)`).

### voxen/overlay.py — `FloatingPill`
- Frameless, topmost, `WS_EX_NOACTIVATE` / `WA_ShowWithoutActivating` so it
  never steals focus. States: IDLE, RECORDING, TRANSCRIBING, CLEANING,
  SUCCESS, ERROR, SILENT.
- The `nativeEvent`/`WM_MOUSEACTIVATE` branch was **deleted** (it never worked
  — `ctypes.wintypes` was missing — and `WS_EX_NOACTIVATE` already provides
  focus protection).

### voxen/tray.py — `SystemTrayManager`
- Menu: start/stop dictation, hotkey mode, microphone selector, STT model
  selector (faster-whisper models only), cleanup toggle, overlay toggle,
  settings, quit. Tray mutations call `save_config` via `VoxenApp` handlers.

### voxen/settings_window.py — `SettingsWindow`
- Sections: General, Dictation, Audio, Transcription, Text cleanup, Privacy,
  About. The Whistle/Moonshine engine combobox and non-Whisper model entries
  were removed (only faster-whisper is implemented).
- Cleanup info checks `_QWEN_MODEL_PATH.exists()` instead of constructing
  `LlamaCleaner` (avoids loading a ~400 MB model just to open settings).

---

## 6. Storage, network, privacy

### Written to disk
| Path | Content | Retention |
|------|---------|-----------|
| `%LOCALAPPDATA%\Voxen\last.wav` | Last take, normalized 16 kHz mono | Last take only |
| `%LOCALAPPDATA%\Voxen\logs\voxen.log` | Rotating log (3 × 2 MB), INFO level | Rotating |
| `%USERPROFILE%\.cache\huggingface\hub` | faster-whisper models | Persistent |

No audio is written to the current working directory (removed — it could sync
to OneDrive/Dropbox). Raw transcripts are logged at **DEBUG with length only**;
the actual dictated text is not persisted in logs.

### Network
- Hugging Face download on first use for faster-whisper (if not cached).
- No Ollama, no telemetry, no cloud API after model download.

### Privacy risks (verified design intent)
- Audio and transcripts persist unencrypted in `%LOCALAPPDATA%\Voxen\last.wav`
  and the log (length-only at DEBUG, raw at DEBUG only).
- Clipboard injection aborts on non-text content to avoid destroying the user's
  clipboard.

---

## 7. Threading model

| Thread | Purpose |
|--------|---------|
| Main (Qt) | UI event loop, signal dispatch |
| Hotkey | pynput listener + bounded worker queue |
| Audio callback | PortAudio input (~30 ms blocks) |
| Pipeline worker | Single long-lived thread, bounded queue (maxsize=2) |
| Model loader | Off-thread faster-whisper load at startup |

No per-dictation thread spawning; no unbounded thread growth.

---

## 8. Tests

pytest suite under `tests/`. Hardware/GUI stages (mic capture, Whisper
transcription, Notepad injection, UI display) are skipped under `CI=true`
instead of failing. Non-hardware tests (config persistence, clipboard guard,
audio re-init guard, vocabulary corrections, stream recovery) run without
hardware.

```
CI=true uv run pytest tests/ -rs -q
```

No CI existed before this change; `.github/workflows/ci.yml` now runs on
`windows-latest` and fails on any test failure. The CI result has not been
observed from this environment.

---

## 9. Removed since the previous architecture doc

- Ollama / `OllamaCleaner` / `check_ollama_status` / `sanitize_formatting`
  (engine removed; LlamaCleaner is the sole cleanup path).
- Whistle / Moonshine / `needle` / `sherpa-onnx` (not implemented, not imported).
- `fix_capitalisation()` / `ALWAYS_CAPITALISE` (dead code, deleted).
- `sd._terminate()`/`sd._initialize()` in device enumeration (removed).
- `nativeEvent`/`WM_MOUSEACTIVATE` branch in overlay (dead, deleted).
- Writing `temp_last_capture.wav` to CWD (removed).
- Logging raw transcripts at INFO (redacted to DEBUG length).

---

## 10. Unverified

- Real Windows microphone capture across devices (Bluetooth, USB, narrowband).
- Real Windows clipboard behavior (the non-text guard is unit-tested with a
  mock; the live clipboard was not exercised in this session).
- Single-instance mutex with two running Voxen instances.
- `uv sync --extra test` into a fully fresh, non-project venv (uv 0.13.9
  `--active` quirk; verified in the project venv instead).
- CI run result (`.github/workflows/ci.yml` created; Actions tab not checked).

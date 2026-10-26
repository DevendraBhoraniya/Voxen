# Voxen 🎙️

A lightweight, fully local Windows equivalent of Wispr Flow. Press **Ctrl+Shift** to dictate speech into any active Windows application (Notepad, Word, Chrome, Slack, etc.) with real-time audio visualization, zero telemetry, and seamless clipboard injection.

---

## Key Features

- **Global Hotkey:** Press `Ctrl+Shift` to start recording. Press again to stop and paste (or switch to `Hold-to-Talk` mode in tray).
- **100% Local Inference:** Powered by `faster-whisper` (CTranslate2). Audio never leaves your PC.
- **Hardware Optimized:**
  - Auto-detects usable NVIDIA CUDA GPU acceleration (`float16`).
  - Throttled CPU mode (`int8` quantization, 2 worker threads) designed specifically for 4-core CPUs and 6–8 GB RAM systems so Chrome and Microsoft Word run smoothly with zero stutter.
  - Pre-cached local Whisper models (`base.en`, `tiny.en`, `small.en`) work completely offline.
- **Floating Recording Pill (PySide6):**
  - Acrylic dark glass aesthetic, frameless, and always on top.
  - Live audio waveform bars driven by real microphone RMS samples.
  - Built with Windows `WS_EX_NOACTIVATE` so the overlay never steals cursor focus from Word or Notepad.
- **Safe Clipboard Injection:**
  - Saves your existing clipboard contents.
  - Safely injects Unicode transcript via paste simulation (`Ctrl+V`).
  - Automatically restores your previous clipboard contents without permanent overwrite.
- **Optional Ollama Refinement:**
  - Connects to local Ollama (`llama3.2:3b`, etc.) to clean fillers (`um`, `uh`) and punctuation.
  - Guardrails reject hallucinations, never answer dictated questions, and preserve raw text on timeout or error.
- **System Tray Management:**
  - Switch microphones on the fly.
  - Switch between Toggle mode and Hold-to-Talk mode.
  - Toggle Ollama cleanup and overlay visibility.

---

## File Architecture

| File | Purpose |
|---|---|
| [`main.py`](file:///f:/test/STT/main.py) | Application entrypoint, hardware diagnostics, and thread orchestrator. |
| [`audio.py`](file:///f:/test/STT/audio.py) | Low-latency audio stream, RMS meter callbacks, and device discovery. |
| [`hotkey.py`](file:///f:/test/STT/hotkey.py) | Global keyboard hook (`pynput`) supporting `Ctrl+Shift` with debounce. |
| [`transcribe.py`](file:///f:/test/STT/transcribe.py) | Local `faster-whisper` engine, VAD filter, silence handling, and CPU/CUDA auto-selection. |
| [`cleanup.py`](file:///f:/test/STT/cleanup.py) | Ollama localhost API client with anti-hallucination guardrails. |
| [`inject.py`](file:///f:/test/STT/inject.py) | Safe Windows clipboard save/paste/restore injection. |
| [`overlay.py`](file:///f:/test/STT/overlay.py) | PySide6 floating pill with live audio visualizer and `WS_EX_NOACTIVATE`. |
| [`tray.py`](file:///f:/test/STT/tray.py) | Windows system tray icon and interactive device/mode menu. |
| [`config.yaml`](file:///f:/test/STT/config.yaml) | YAML configuration file for user customization. |
| [`test_e2e.py`](file:///f:/test/STT/test_e2e.py) | 5-stage automated verification test suite. |

---

## Quick Start & Launch

### 1. Requirements & Setup

- Windows 10 / 11
- Managed with [**uv**](https://github.com/astral-sh/uv)
- Virtual environment: `.venv`

#### Install & Sync Dependencies

```powershell
uv sync
```

### 2. Launch Voxen

Using `uv run`:
```powershell
uv run voxen
```
*(or `uv run python main.py` / `.\.venv\Scripts\python main.py`)*

### 3. Verification Test Suite

To run the automated 5-stage verification test suite inside `.venv`:

```powershell
uv run python test_e2e.py
```

### 4. How to Dictate

1. Focus any text input field (e.g. Notepad, Word, Web Browser, VS Code).
2. Press **`Ctrl+Shift`**.
3. A floating pill appears with live audio meters showing "Listening...".
4. Speak your sentence.
5. Press **`Ctrl+Shift`** again to finish dictation.
6. Voxen transcribes the audio, pulses "Done", pastes the text directly at your cursor, and restores your clipboard!

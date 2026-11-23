# Voxen

## Status

Working prototype. Windows only. CPU-tested. Single user. Not production software.

This project's code is MIT-licensed. PySide6 (the UI framework) is LGPL-licensed; see LICENSE and note the PySide6 LGPL obligation if you distribute the app.

## What it does

Local hotkey dictation for Windows. Press a global hotkey to start recording microphone audio; press again to stop. Voxen transcribes the audio offline with faster-whisper (CTranslate2) and pastes the result into the focused application via the clipboard. After the Whisper model is cached, everything runs offline - no cloud calls and no telemetry code in voxen/.

## Known limitations

- Windows only; developed and tested on one machine (Ryzen 5 3550H, GTX 1650).
- GPU acceleration is not active because cublas64_12.dll is missing on the test machine; the app runs on CPU int8.
- Audio input overflow warnings occur during capture; some words may be dropped on long takes.
- Text cleanup (Qwen via llama-cpp-python) is optional and disabled unless llama_cpp is installed.
- Clipboard guard aborts injection when the clipboard holds non-text content; the transcript is not retained after an abort.
- Hotkey and microphone behavior with live streams has not been verified on hardware beyond the author's machine.

## Accuracy and speed

Not yet measured. See benchmarks/results.md.

## Install and run

Requirements: Windows 10/11, Python 3.11+, a microphone, uv.

Install dependencies (verified in this session):

```powershell
uv sync --extra test
```

Launch (entry point defined in pyproject.toml; GUI launch not executed in this session):

```powershell
uv run voxen
```

(or `uv run python -m voxen`)

## Tests

Run the suite inside .venv:

```powershell
uv run pytest tests/ -rs -q
```

Unit-style tests (config, clipboard, audio re-init, vocabulary, stream recovery) run without hardware. The e2e microphone/Notepad stages and the GUI overlay test require a real microphone and display; skip them if your environment lacks those.

## Roadmap

- Fix dropped audio / input overflow on long takes
- Add GPU runtime (requires cublas64_12.dll)
- Run the benchmark and fill benchmarks/results.md with real numbers
- Retain transcript on failed clipboard injection

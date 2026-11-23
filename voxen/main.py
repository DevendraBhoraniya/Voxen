"""
Voxen - Local Voice Dictation for Windows.
Main orchestrator: connects PySide6 UI, system tray, global hotkeys,
faster-whisper STT, Ollama cleanup, and safe clipboard injection.
"""

import os
import sys
import time
import logging
import threading
from typing import Dict
import yaml

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QApplication

from . import config as app_config
from .audio import (
    AudioRecorder,
    get_default_input_device,
    is_loopback_device,
    list_audio_devices,
    play_sound_cue,
    resolve_input_device,
)
from .hotkey import HotkeyListener
from .transcribe import (
    DEFAULT_CORRECTIONS,
    WhisperTranscriber,
    create_transcriber,
    detect_hardware,
)
from .cleanup import LlamaCleaner
from . import inject
from .overlay import FloatingPill
from .settings_window import SettingsWindow
from .tray import SystemTrayManager

# Configure logging: console plus a rotating file log under
# %LOCALAPPDATA%\Voxen\logs so a bad take can be diagnosed after the fact
# (device used, raw transcript, corrections, timings).
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
_LOG_DATEFMT = "%H:%M:%S"
_log_dir = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Voxen", "logs"
)
_handlers = [logging.StreamHandler()]
try:
    os.makedirs(_log_dir, exist_ok=True)
    _handlers.append(
        RotatingFileHandler(
            os.path.join(_log_dir, "voxen.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    )
except Exception as _log_err:  # never block startup on a logging problem
    print(f"Warning: could not open log file in {_log_dir}: {_log_err}")

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    datefmt=_LOG_DATEFMT,
    handlers=_handlers,
)
logger = logging.getLogger("Voxen.Main")


def run_first_run_diagnostics(config: Dict):
    """Run comprehensive system diagnostics and hardware assessment."""
    print("=" * 65)
    print("           VOXEN - LOCAL WINDOWS DICTATION")
    print("=" * 65)

    # 1. Hardware & Compute
    hw = detect_hardware()
    stt_cfg = config.get("stt") or config.get("transcription", {})
    cfg_engine = stt_cfg.get("engine", "faster-whisper")
    cfg_model = stt_cfg.get("model", "base.en")
    print(f"[*] CPU Cores:            {hw['cpu_cores']} (Whisper worker threads: {hw['recommended_threads']})")
    print(f"[*] GPU Acceleration:     {hw['cuda_name']} (Usable: {hw['cuda_usable']})")
    print(f"[*] Configured STT:       {cfg_engine.upper()} -> {cfg_model}")

    # 2. Audio Devices
    def_mic = get_default_input_device()
    mic_name = def_mic['name'] if def_mic else "No microphone detected"
    print(f"[*] Default Microphone:   {mic_name}")

    resolved = resolve_input_device(preferred_index=(config.get("audio") or {}).get("device_index"))
    if resolved:
        r_idx, r_name, r_api, r_sr, _r_ch = resolved
        print(f"[*] Capture Device:       #{r_idx} {r_name} ({r_api}, {r_sr} Hz)")
        if is_loopback_device(r_name):
            print("WARNING: Selected input device appears to be a loopback/system audio device.")
            print("Voxen will transcribe system audio instead of your voice.")
            print("Fix: open Windows Sound Settings > Recording > set your microphone as Default.")
        if r_sr < 16000:
            print(
                "    -> WARNING: this microphone is narrowband (<16 kHz). "
                "Speech above 4 kHz is lost and dictation accuracy will suffer."
            )
            print("       Pick a full-band microphone in the tray or set audio.device_index.")
    else:
        print("[*] Capture Device:       none usable")

    # 3. Cleanup Engine Status (LlamaCleaner / Qwen2.5-0.5B)
    # 3b. LlamaCleaner (Qwen2.5-0.5B local) Status
    from .cleanup import _QWEN_MODEL_PATH
    if _QWEN_MODEL_PATH.exists():
        print("[*] Cleanup Engine:       Qwen2.5-0.5B (local) — ready")
    else:
        print("[*] Cleanup Engine:       Not available — run: python tools/setup_cleanup_model.py")

    # 3c. Offline model cache verification
    import os
    hf_cache = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    model_name = stt_cfg.get("model", "base.en")
    model_dir = os.path.join(hf_cache, f"models--Systran--faster-whisper-{model_name}")
    if os.path.exists(model_dir):
        print(f"[*] Whisper Model:        {model_name} — cached (offline OK)")
    else:
        print(f"[*] Whisper Model:        {model_name} — NOT CACHED (will fail offline)")
        print("    -> Run: python -c \"from faster_whisper import WhisperModel; WhisperModel('{}', device='cpu', compute_type='int8', local_files_only=False)\"".format(model_name))

    # Silero VAD is bundled with faster-whisper (no download needed)
    from faster_whisper.utils import get_assets_path
    vad_model_path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
    if os.path.exists(vad_model_path):
        print("[*] Silero VAD:           bundled with faster-whisper (offline OK)")
    else:
        print("[*] Silero VAD:           MISSING - faster-whisper installation may be corrupt")

    # 4. Hotkey info
    hotkey_cfg = config.get("hotkey", {})
    combo = hotkey_cfg.get("combination", "ctrl+shift")
    mode = hotkey_cfg.get("mode", "toggle")
    print(f"[*] Configured Hotkey:    {combo.upper()} (Mode: {mode.title()})")
    print("=" * 65)


class VoxenApp(QObject):
    """Voxen application controller managing threads and events."""

    model_ready = Signal()
    model_load_failed = Signal(str)

    def __init__(self, config: Dict, config_path: str = "config.yaml"):
        super().__init__()
        self.config = config
        self.config_path = config_path
        self._is_processing = False
        self._shutdown_event = threading.Event()
        self._lock = threading.Lock()
        self.overlay = None
        self._settings_window = None

        # Bounded pipeline queue + single long-lived worker thread.
        # Replaces per-dictation thread spawning to avoid unbounded growth.
        import queue as _queue
        self._pipeline_queue: "_queue.Queue" = _queue.Queue(maxsize=2)
        self._pipeline_worker = threading.Thread(
            target=self._pipeline_worker_loop, daemon=True, name="PipelineWorker"
        )
        self._pipeline_worker.start()

        # 1. Initialize Audio Recorder
        audio_cfg = self.config.get("audio", {})
        self.recorder = AudioRecorder(
            sample_rate=audio_cfg.get("sample_rate", 16000),
            channels=audio_cfg.get("channels", 1),
            preroll_ms=audio_cfg.get("preroll_ms", 500),
            device_index=audio_cfg.get("device_index", None),
            silence_threshold_energy=audio_cfg.get("silence_threshold_energy", 0.0008),
            target_rms_normalize=audio_cfg.get("target_rms_normalize", 0.08),
            audio_level_callback=self._on_audio_level
        )

        # 2. Transcriber starts as None — loaded asynchronously to avoid
        #    blocking the Qt event loop for ~6s on startup.
        self.transcriber = None

        # 3. Initialize LlamaCleaner (Qwen2.5-0.5B, fast local semantic cleanup)
        clean_cfg = self.config.get("llm") or self.config.get("cleanup", {})
        clean_threads = clean_cfg.get("cleanup_threads", None)
        self._llama_cleaner = LlamaCleaner(n_threads=clean_threads)
        if self._llama_cleaner.is_available:
            logger.info(f"LlamaCleaner ready (Qwen2.5-0.5B, threads={self._llama_cleaner._n_threads})")
            # Warm up model at startup to avoid cold-start latency on first dictation
            self._llama_cleaner.warmup()
        else:
            logger.info("LlamaCleaner not available — run tools/setup_cleanup_model.py")

        # 4. Initialize Overlay Pill
        ui_cfg = self.config.get("ui", {})
        self.overlay = FloatingPill(
            position=ui_cfg.get("pill_position", "bottom_center"),
            margin_bottom=ui_cfg.get("pill_margin_bottom", 48)
        )
        # Show loading state while model loads in background
        if self.config.get("ui", {}).get("show_overlay", True):
            self.overlay.state_signal.emit("TRANSCRIBING", "Loading model...")

        # Connect async model load signals
        self.model_ready.connect(self._on_model_ready)
        self.model_load_failed.connect(self._on_model_load_failed)

        # 5. Initialize System Tray
        self.tray = SystemTrayManager(
            config=self.config,
            on_open_config=self._open_config_file,
            on_open_settings=self._open_settings_window
        )
        self._connect_tray_signals()
        self.tray.show()

        # 6. Initialize Global Hotkey Listener
        hotkey_cfg = self.config.get("hotkey", {})
        hotkey_key = hotkey_cfg.get("key") or hotkey_cfg.get("combination", "ctrl+shift")
        self.hotkey_listener = HotkeyListener(
            combination=hotkey_key,
            mode=hotkey_cfg.get("mode", "toggle"),
            debounce_ms=hotkey_cfg.get("debounce_ms", 300),
            on_trigger=self.toggle_recording,
            on_start=self.start_recording,
            on_stop=self.stop_recording
        )
        self.hotkey_listener.start()

        # 7. Prime background stream with 500ms pre-roll now that overlay is ready
        self.recorder.start_stream()
        logger.info(
            f"Microphone in use: '{self.recorder.active_device_name}' "
            f"at {self.recorder.active_sample_rate} Hz x{self.recorder.active_channels} ch"
        )

        # Startup notification
        combo_name = str(hotkey_key).upper()
        self.tray.show_message(
            "Voxen Ready",
            f"Press {combo_name} to dictate into any Windows application."
        )

        # 8. Load STT model asynchronously so UI appears instantly
        threading.Thread(target=self._load_model_async, daemon=True).start()

    def _connect_tray_signals(self):
        self.tray.toggle_recording_requested.connect(self.toggle_recording)
        self.tray.mode_changed.connect(self._on_mode_changed)
        self.tray.device_changed.connect(self._on_device_changed)
        self.tray.model_changed.connect(self._on_model_changed)
        self.tray.cleanup_toggled.connect(self._on_cleanup_toggled)
        self.tray.overlay_toggled.connect(self._on_overlay_toggled)
        self.tray.quit_requested.connect(self.quit)

    def _load_model_async(self):
        """Runs in a daemon thread. Loads the transcriber then signals ready."""
        try:
            stt_cfg = self.config.get("stt") or self.config.get("transcription", {})
            vocab = stt_cfg.get("vocabulary") or []
            prompt = ", ".join(vocab) + "." if vocab else stt_cfg.get("initial_prompt", None)
            correction_dict = app_config.build_corrections(stt_cfg)

            t = create_transcriber(
                model_name=stt_cfg.get("model", stt_cfg.get("model_name", "base.en")),
                device=stt_cfg.get("device", "auto"),
                compute_type=stt_cfg.get("compute_type", "auto"),
                cpu_threads=stt_cfg.get("cpu_threads", 4),
                language=stt_cfg.get("language", "en"),
                vad_filter=stt_cfg.get("vad_filter", True),
                beam_size=stt_cfg.get("beam_size", 5),
                initial_prompt=prompt,
                corrections=correction_dict,
            )
            if self._shutdown_event.is_set():
                return
            # Warm up: run a silent 0.1s clip so first real take is faster
            import numpy as np
            dummy = np.zeros(1600, dtype=np.float32)
            try:
                t.transcribe(dummy)
            except Exception:
                pass
            if self._shutdown_event.is_set():
                return
            self.transcriber = t
            # Signal UI on Qt thread
            self.model_ready.emit()
        except Exception as exc:
            self.model_load_failed.emit(str(exc))

    def _on_model_ready(self):
        if self.config.get("ui", {}).get("show_overlay", True):
            self.overlay.state_signal.emit("IDLE", "Ready")
        logger.info("Model ready.")

    def _on_model_load_failed(self, err):
        if self.config.get("ui", {}).get("show_overlay", True):
            self.overlay.state_signal.emit("ERROR", "Model load failed")
        logger.error(f"Model failed to load: {err}")

    def _open_config_file(self):
        """Open config.yaml in Windows default editor.

        config_path is derived from a hardcoded relative path ("config.yaml")
        resolved against the current working directory, so it can never contain
        untrusted input. os.startfile() avoids any shell/command injection.
        """
        config_path = os.path.abspath("config.yaml")
        if os.path.exists(config_path):
            os.startfile(config_path)

    def _open_settings_window(self):
        """Open (or focus) the settings window."""
        if self._settings_window is None:
            self._settings_window = SettingsWindow(
                config=self.config,
                on_device_changed=self._on_device_changed,
                on_model_changed=self._on_model_changed,
                on_mode_changed=self._on_mode_changed,
                on_cleanup_toggled=self._on_cleanup_toggled,
                on_overlay_toggled=self._on_overlay_toggled,
            )
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _on_audio_level(self, rms: float, sub_levels=None):
        """Forward real audio level samples to UI safely (recording only)."""
        if not self.recorder.is_recording:
            return
        overlay = getattr(self, "overlay", None)
        if overlay is not None and self.config.get("ui", {}).get("show_overlay", True):
            overlay.audio_signal.emit(rms, sub_levels)
        sw = getattr(self, "_settings_window", None)
        if sw is not None and sw.isVisible():
            sw.audio_level.emit(rms, sub_levels)

    def _on_model_changed(self, model_key: str):
        """Switch speech recognition model/engine on the fly."""
        stt_cfg = self.config.setdefault("stt", {})
        vocab = stt_cfg.get("vocabulary") or []
        prompt = ", ".join(vocab) + "." if vocab else stt_cfg.get("initial_prompt", None)

        replacements = stt_cfg.get("replacements") or stt_cfg.get("corrections", None)
        correction_dict = app_config.build_corrections(stt_cfg)

        try:
            stt_cfg["model"] = model_key
            self.transcriber = create_transcriber(
                model_name=model_key,
                device=stt_cfg.get("device", "auto"),
                compute_type=stt_cfg.get("compute_type", "auto"),
                cpu_threads=stt_cfg.get("cpu_threads", 4),
                language=stt_cfg.get("language", "en"),
                vad_filter=stt_cfg.get("vad_filter", True),
                beam_size=stt_cfg.get("beam_size", 5),
                initial_prompt=prompt,
                corrections=correction_dict
            )
            actual_dev = getattr(self.transcriber, "actual_device", "cpu").upper()
            self.tray.show_message("STT Model Changed", f"Active: {model_key} on {actual_dev}")
            logger.info(f"Switched model to: {model_key} on {actual_dev}")
            app_config.save_config(self.config_path, self.config)

        except Exception as e:
            logger.error(f"Failed to switch model to {model_key}: {e}", exc_info=True)
            self.tray.show_message("Model Error", f"Failed to load {model_key}: {e}")

    def _on_mode_changed(self, mode: str):
        self.config.setdefault("hotkey", {})["mode"] = mode
        self.hotkey_listener.set_mode(mode)
        logger.info(f"Dictation mode set to: {mode}")
        app_config.save_config(self.config_path, self.config)

    def _on_device_changed(self, dev_idx):
        self.config.setdefault("audio", {})["device_index"] = dev_idx
        self.recorder.set_device(dev_idx)
        name = f"Device #{dev_idx}" if dev_idx is not None else "Default Device"
        self.tray.show_message("Microphone Changed", f"Active microphone: {name}")
        app_config.save_config(self.config_path, self.config)

    def _on_cleanup_toggled(self, enabled: bool):
        self.config.setdefault("llm", {})["llama_cleanup"] = enabled
        status = "Enabled" if enabled else "Disabled"
        self.tray.show_message("Cleanup", f"Text refinement {status}.")
        app_config.save_config(self.config_path, self.config)

    def _on_overlay_toggled(self, enabled: bool):
        self.config.setdefault("ui", {})["show_overlay"] = enabled
        if not enabled:
            self.overlay.hide_signal.emit()
        app_config.save_config(self.config_path, self.config)

    def toggle_recording(self):
        """Toggle dictation state."""
        with self._lock:
            if self.recorder.is_recording:
                self.stop_recording()
            else:
                self.start_recording()

    def start_recording(self):
        """Begin audio capture using robust recovery strategy."""
        if self._is_processing or self.recorder.is_recording:
            return

        # Ignore hotkey presses while model is still loading
        if self.transcriber is None:
            logger.warning("Hotkey pressed while model is still loading — ignoring.")
            return

        logger.info("Starting dictation recording...")

        started, msg = self.recorder.start_recording()
        if not started:
            logger.error(f"Failed to start audio capture: {msg}")
            self.tray.set_recording_state(False)
            if self.config.get("ui", {}).get("show_overlay", True):
                self.overlay.state_signal.emit("ERROR", "Mic error")
            self.tray.show_message(
                "Microphone Error",
                f"Cannot access microphone: {msg}\nPlease verify your audio devices."
            )
            return

        self.tray.set_recording_state(True)
        if self.config.get("ui", {}).get("show_overlay", True):
            self.overlay.state_signal.emit("RECORDING", "Listening...")

    def stop_recording(self):
        """Stop audio capture and initiate asynchronous pipeline."""
        if not self.recorder.is_recording:
            return

        logger.info("Stopping dictation recording...")
        self.tray.set_recording_state(False)
        audio, duration, status = self.recorder.stop_recording()

        # Handle capture failures explicitly (never silently produce an empty transcript)
        if not status.get("success", False):
            reason = status.get("reason", "unknown")
            err_msg = status.get("error", "Failed to capture audio from microphone.")
            logger.warning(f"Audio capture failed: reason='{reason}', error='{err_msg}'")

            if reason == "too_short":
                # Not a hardware fault: the user simply released the key
                # before saying anything. Do not alarm them with a mic error.
                if self.config.get("ui", {}).get("show_overlay", True):
                    self.overlay.state_signal.emit("SILENT", "Too short")
                self.tray.show_message(
                    "Nothing Recorded",
                    "The take was too short. Hold the hotkey while you speak."
                )
                return

            if self.config.get("ui", {}).get("show_overlay", True):
                self.overlay.state_signal.emit("ERROR", "Capture failed")
            self.tray.show_message("Microphone Error", f"{err_msg}\nPlease verify your microphone.")
            return

        # Handle silence (recording succeeded, but user did not speak)
        if status.get("is_silent", False) or audio is None:
            logger.info("No audible speech captured.")
            if self.config.get("ui", {}).get("show_overlay", True):
                self.overlay.state_signal.emit("SILENT", "No speech detected")
            return

        # Hand off to background worker thread so UI is never blocked
        self._is_processing = True
        if self.config.get("ui", {}).get("show_overlay", True):
            self.overlay.state_signal.emit("TRANSCRIBING", "Transcribing...")

        try:
            self._pipeline_queue.put_nowait((audio, duration))
        except Exception:
            logger.warning("Dictation pipeline dropped: worker queue full.")
            self._is_processing = False
            if self.config.get("ui", {}).get("show_overlay", True):
                self.overlay.state_signal.emit("ERROR", "Busy")

    def _pipeline_worker_loop(self):
        import queue as _queue
        while True:
            try:
                item = self._pipeline_queue.get()
            except Exception:
                continue
            if item is None:
                break
            audio, duration = item
            try:
                self._process_pipeline(audio, duration)
            except Exception as e:
                logger.error(f"Pipeline worker error: {e}", exc_info=True)
            finally:
                self._is_processing = False
                self._pipeline_queue.task_done()

    def _process_pipeline(self, audio, duration):
        """Background worker for STT, optional Ollama cleanup, and injection."""
        t_pipeline_start = time.time()
        try:
            if self.transcriber is None:
                logger.warning("Transcriber not ready yet.")
                if self.config.get("ui", {}).get("show_overlay", True):
                    self.overlay.state_signal.emit("ERROR", "Model not loaded")
                return

            # 1. Transcribe with local faster-whisper
            raw_text, stt_dur = self.transcriber.transcribe(audio)

            if not raw_text.strip():
                if self.config.get("ui", {}).get("show_overlay", True):
                    self.overlay.state_signal.emit("SILENT", "No speech detected")
                return

            final_text = raw_text
            cleanup_dur = 0.0

            # 2. Cleanup: LlamaCleaner (local Qwen) is the only cleanup engine.
            clean_cfg = self.config.get("llm") or self.config.get("cleanup", {})
            llama_cleanup_enabled = clean_cfg.get("llama_cleanup", True)
            if llama_cleanup_enabled and self._llama_cleaner.is_available:
                if self.config.get("ui", {}).get("show_overlay", True):
                    self.overlay.state_signal.emit("CLEANING", "Refining text...")
                t_clean = time.time()
                final_text = self._llama_cleaner.clean(raw_text, getattr(self.transcriber, "last_avg_logprob", -1.0))
                cleanup_dur = time.time() - t_clean

            # 3. Inject into focused window
            t_inject = time.time()
            inj_cfg = self.config.get("inject", {})
            injected = inject.inject_text(
                final_text,
                restore_delay_ms=inj_cfg.get("restore_delay_ms", 120),
                post_paste_delay_ms=inj_cfg.get("post_paste_delay_ms", 40)
            )
            inject_dur = time.time() - t_inject

            if injected:
                if self.config.get("ui", {}).get("show_overlay", True):
                    self.overlay.state_signal.emit("SUCCESS", "Done")
            else:
                logger.warning("Injection failed — paste did not plausibly land.")
                if self.config.get("ui", {}).get("show_overlay", True):
                    self.overlay.state_signal.emit("ERROR", "Paste failed — check focus")
            total_latency = time.time() - t_pipeline_start
            rtf = stt_dur / duration if duration > 0 else 0

            logger.info(
                f"[LATENCY BREAKDOWN] Audio: {duration:.2f}s | "
                f"STT: {stt_dur:.2f}s (RTF: {rtf:.2f}x) | "
                f"Cleanup: {cleanup_dur:.2f}s | "
                f"Inject: {inject_dur:.2f}s | "
                f"TOTAL TURNAROUND: {total_latency:.2f}s"
            )

        except Exception as e:
            logger.error(f"Error in dictation pipeline: {e}", exc_info=True)
            if self.config.get("ui", {}).get("show_overlay", True):
                self.overlay.state_signal.emit("ERROR", "Error during dictation")

    def quit(self):
        """Clean shutdown."""
        self._shutdown_event.set()
        logger.info("Shutting down Voxen...")
        self.hotkey_listener.stop()
        if self.recorder.is_recording:
            self.recorder.stop_recording()
        if self._settings_window is not None:
            self._settings_window.close()
            self._settings_window = None
        try:
            self._pipeline_queue.put_nowait(None)
        except Exception:
            pass
        self.recorder.close()
        self.tray.hide()
        self.overlay.close()
        QApplication.quit()


def main():
    # Set Voxen as explicit application identity in Windows
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Voxen.Dictation.App.1.0")
    except Exception:
        pass

    # Single-instance guard: a named mutex prevents two Voxen processes from
    # contending over the microphone and clipboard. Non-Windows or missing
    # pywin32 silently skips the guard.
    try:
        import win32api
        import win32event
        _mutex = win32event.CreateMutex(None, False, "Voxen.Dictation.App.1.0")
        if win32api.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            print("Voxen is already running. Only one instance may run at a time.")
            sys.exit(1)
    except ImportError:
        pass
    except Exception:
        pass

    config_path = "config.yaml"
    config = app_config.load_config(config_path)
    run_first_run_diagnostics(config)

    app = QApplication(sys.argv)
    app.setApplicationName("Voxen")
    app.setApplicationDisplayName("Voxen")
    app.setOrganizationName("Voxen")
    app.setQuitOnLastWindowClosed(False)

    from .tray import create_tray_icon_pixmap
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(create_tray_icon_pixmap(recording=False)))

    voxen = VoxenApp(config, config_path=config_path)

    logger.info("Voxen running. Press Ctrl+Shift to dictate.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

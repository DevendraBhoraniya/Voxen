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
    WhistleTranscriber,
    create_transcriber,
    detect_hardware,
)
from .cleanup import OllamaCleaner, check_ollama_status
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

    # 3. Ollama Status
    ollama_cfg = config.get("cleanup", {})
    endpoint = ollama_cfg.get("endpoint", "http://127.0.0.1:11434")
    ollama_info = check_ollama_status(endpoint)
    if ollama_info["available"]:
        models_str = ", ".join(ollama_info["models"]) if ollama_info["models"] else "None found"
        print(f"[*] Ollama Status:        Online (Models: {models_str})")
    else:
        print(f"[*] Ollama Status:        Offline/Not Running ({ollama_info['error']})")
        print("    -> Note: Voxen will run fully local Whisper STT without cleanup delay.")

    # 4. Hotkey info
    hotkey_cfg = config.get("hotkey", {})
    combo = hotkey_cfg.get("combination", "ctrl+shift")
    mode = hotkey_cfg.get("mode", "toggle")
    print(f"[*] Configured Hotkey:    {combo.upper()} (Mode: {mode.title()})")
    print("=" * 65)


class VoxenApp(QObject):
    """Voxen application controller managing threads and events."""

    def __init__(self, config: Dict):
        super().__init__()
        self.config = config
        self._is_processing = False
        self._lock = threading.Lock()
        self.overlay = None
        self._settings_window = None

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

        # 2. Initialize STT Transcriber (Whistle or faster-whisper)
        stt_cfg = self.config.get("stt") or self.config.get("transcription", {})
        vocab = stt_cfg.get("vocabulary") or []
        prompt = ", ".join(vocab) + "." if vocab else stt_cfg.get("initial_prompt", None)

        replacements = stt_cfg.get("replacements") or stt_cfg.get("corrections", None)
        correction_dict = app_config.build_corrections(stt_cfg)

        self.transcriber = create_transcriber(
            engine=stt_cfg.get("engine", "faster-whisper"),
            model_name=stt_cfg.get("model", stt_cfg.get("model_name", "base.en")),
            device=stt_cfg.get("device", "auto"),
            compute_type=stt_cfg.get("compute_type", "auto"),
            cpu_threads=stt_cfg.get("cpu_threads", 4),
            language=stt_cfg.get("language", "en"),
            vad_filter=stt_cfg.get("vad_filter", True),
            beam_size=stt_cfg.get("beam_size", 5),
            initial_prompt=prompt,
            vocabulary=vocab,
            corrections=correction_dict
        )

        # 3. Initialize Ollama Cleaner
        clean_cfg = self.config.get("llm") or self.config.get("cleanup", {})
        self.cleaner = OllamaCleaner(
            enabled=clean_cfg.get("enabled", False),
            endpoint=clean_cfg.get("base_url", clean_cfg.get("endpoint", "http://127.0.0.1:11434")),
            model=clean_cfg.get("model", "llama3.2:3b"),
            timeout_seconds=clean_cfg.get("timeout_seconds", 4.0),
            temperature=clean_cfg.get("temperature", 0.1),
            min_words=clean_cfg.get("min_words_for_cleanup", 10),
            min_similarity=clean_cfg.get("min_similarity", 0.8),
            keep_alive=clean_cfg.get("keep_alive", "30m"),
            vocabulary=vocab,
            example_input=clean_cfg.get("example_input", None),
            example_output=clean_cfg.get("example_output", None),
            remove_fillers=clean_cfg.get("remove_fillers", True),
            system_prompt=clean_cfg.get("system_prompt", None)
        )

        # 4. Initialize Overlay Pill
        ui_cfg = self.config.get("ui", {})
        self.overlay = FloatingPill(
            position=ui_cfg.get("pill_position", "bottom_center"),
            margin_bottom=ui_cfg.get("pill_margin_bottom", 48)
        )

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

    def _connect_tray_signals(self):
        self.tray.toggle_recording_requested.connect(self.toggle_recording)
        self.tray.mode_changed.connect(self._on_mode_changed)
        self.tray.device_changed.connect(self._on_device_changed)
        self.tray.model_changed.connect(self._on_model_changed)
        self.tray.cleanup_toggled.connect(self._on_cleanup_toggled)
        self.tray.overlay_toggled.connect(self._on_overlay_toggled)
        self.tray.quit_requested.connect(self.quit)

    def _open_config_file(self):
        """Open config.yaml in Windows default editor."""
        config_path = os.path.abspath("config.yaml")
        if os.path.exists(config_path):
            os.system(f'start "" "{config_path}"')

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
            if model_key == "whistle":
                stt_cfg["engine"] = "whistle"
                stt_cfg["model"] = "whistle.cact"
                self.transcriber = create_transcriber(
                    engine="whistle",
                    vocabulary=vocab,
                    corrections=correction_dict,
                    language=stt_cfg.get("language", "en")
                )
                self.tray.show_message("STT Engine Changed", "Active: whistle.cact (Cactus Compute, 16.9 MB)")
                logger.info("Switched to Whistle engine (whistle.cact)")
            elif model_key == "moonshine":
                stt_cfg["engine"] = "moonshine"
                stt_cfg["model"] = "moonshine-base-en"
                self.transcriber = create_transcriber(
                    engine="moonshine",
                    model_name="moonshine-base-en",
                    corrections=correction_dict,
                    cpu_threads=stt_cfg.get("cpu_threads", 4)
                )
                self.tray.show_message("STT Engine Changed", "Active: moonshine-base-en (Useful Sensors ONNX)")
                logger.info("Switched to Moonshine engine (moonshine-base-en)")
            else:
                stt_cfg["engine"] = "faster-whisper"
                stt_cfg["model"] = model_key
                self.transcriber = create_transcriber(
                    engine="faster-whisper",
                    model_name=model_key,
                    device=stt_cfg.get("device", "auto"),
                    compute_type=stt_cfg.get("compute_type", "auto"),
                    cpu_threads=stt_cfg.get("cpu_threads", 4),
                    language=stt_cfg.get("language", "en"),
                    vad_filter=stt_cfg.get("vad_filter", True),
                    beam_size=stt_cfg.get("beam_size", 5),
                    initial_prompt=prompt,
                    vocabulary=vocab,
                    corrections=correction_dict
                )
                actual_dev = getattr(self.transcriber, "actual_device", "cpu").upper()
                self.tray.show_message("STT Model Changed", f"Active: {model_key} on {actual_dev}")
                logger.info(f"Switched model to: {model_key} on {actual_dev}")

        except Exception as e:
            logger.error(f"Failed to switch model to {model_key}: {e}", exc_info=True)
            self.tray.show_message("Model Error", f"Failed to load {model_key}: {e}")

    def _on_mode_changed(self, mode: str):
        self.config.setdefault("hotkey", {})["mode"] = mode
        self.hotkey_listener.set_mode(mode)
        logger.info(f"Dictation mode set to: {mode}")

    def _on_device_changed(self, dev_idx):
        self.config.setdefault("audio", {})["device_index"] = dev_idx
        self.recorder.set_device(dev_idx)
        name = f"Device #{dev_idx}" if dev_idx is not None else "Default Device"
        self.tray.show_message("Microphone Changed", f"Active microphone: {name}")

    def _on_cleanup_toggled(self, enabled: bool):
        self.config.setdefault("cleanup", {})["enabled"] = enabled
        self.cleaner.enabled = enabled
        status = "Enabled" if enabled else "Disabled"
        self.tray.show_message("Ollama Cleanup", f"Text refinement {status}.")

    def _on_overlay_toggled(self, enabled: bool):
        self.config.setdefault("ui", {})["show_overlay"] = enabled
        if not enabled:
            self.overlay.hide_signal.emit()

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

        threading.Thread(
            target=self._process_pipeline,
            args=(audio, duration),
            daemon=True
        ).start()

    def _process_pipeline(self, audio, duration):
        """Background worker for STT, optional Ollama cleanup, and injection."""
        t_pipeline_start = time.time()
        try:
            # 1. Transcribe with local faster-whisper
            raw_text, stt_dur = self.transcriber.transcribe(audio)

            if not raw_text.strip():
                if self.config.get("ui", {}).get("show_overlay", True):
                    self.overlay.state_signal.emit("SILENT", "No speech detected")
                return

            final_text = raw_text
            cleanup_dur = 0.0

            # 2. Optional Ollama Cleanup
            if self.cleaner.enabled:
                if self.config.get("ui", {}).get("show_overlay", True):
                    self.overlay.state_signal.emit("CLEANING", "Refining text...")
                t_clean = time.time()
                final_text = self.cleaner.clean(raw_text)
                cleanup_dur = time.time() - t_clean

            # 3. Inject into focused window
            if self.config.get("ui", {}).get("show_overlay", True):
                self.overlay.state_signal.emit("SUCCESS", "Done")

            t_inject = time.time()
            inj_cfg = self.config.get("injection", {})
            inject.inject_text(
                final_text,
                restore_delay_ms=inj_cfg.get("restore_delay_ms", 120),
                post_paste_delay_ms=inj_cfg.get("post_paste_delay_ms", 40)
            )
            inject_dur = time.time() - t_inject
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
        finally:
            self._is_processing = False

    def quit(self):
        """Clean shutdown."""
        logger.info("Shutting down Voxen...")
        self.hotkey_listener.stop()
        if self.recorder.is_recording:
            self.recorder.stop_recording()
        if self._settings_window is not None:
            self._settings_window.close()
            self._settings_window = None
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

    config = app_config.load_config("config.yaml")
    run_first_run_diagnostics(config)

    app = QApplication(sys.argv)
    app.setApplicationName("Voxen")
    app.setApplicationDisplayName("Voxen")
    app.setOrganizationName("Voxen")
    app.setQuitOnLastWindowClosed(False)

    from .tray import create_tray_icon_pixmap
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(create_tray_icon_pixmap(recording=False)))

    voxen = VoxenApp(config)

    logger.info("Voxen running. Press Ctrl+Shift to dictate.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

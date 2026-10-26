"""
Audio recording and device management module for Voxen.
Handles low-latency microphone capture, live RMS level monitoring,
and silence detection.
"""

import collections
import logging
import os
import threading
import time
import winsound
from typing import Callable, Dict, List, Optional, Tuple
import numpy as np
import sounddevice as sd

logger = logging.getLogger("Voxen.Audio")


# Device names that indicate a loopback / system-audio capture endpoint rather
# than a real microphone. Selecting one makes Voxen transcribe system audio
# (TTS, media, game sound) instead of the user's voice.
LOOPBACK_KEYWORDS = [
    "stereo mix", "wave out", "what u hear", "loopback",
    "vb-cable", "cable output", "virtual audio", "wasapi loopback",
]


def is_loopback_device(name: str) -> bool:
    """Return True when *name* looks like a loopback/system-audio device."""
    if not name:
        return False
    lowered = name.lower()
    return any(k in lowered for k in LOOPBACK_KEYWORDS)


def refresh_audio_devices() -> bool:
    """
    Safely re-enumerate Windows audio devices by re-initializing PortAudio.
    Ensures newly plugged USB mics or Bluetooth headsets are detected immediately.
    """
    try:
        sd._terminate()
        sd._initialize()
        logger.debug("PortAudio backend re-initialized successfully.")
        return True
    except Exception as e:
        logger.error(f"Error re-initializing PortAudio backend: {e}")
        return False


def _fir_lowpass(cutoff_hz: float, sample_rate: int, taps: int = 129) -> np.ndarray:
    """Windowed-sinc linear-phase low-pass kernel (float32)."""
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    kernel = np.sinc(2.0 * cutoff_hz / sample_rate * n) * np.hamming(taps)
    kernel /= np.sum(kernel)
    return kernel.astype(np.float32)


def resample_to_16k(audio: np.ndarray, orig_sr: int) -> np.ndarray:
    """
    Resample mono float32 audio to 16,000 Hz.

    When the device runs at 44.1/48 kHz the signal is low-pass filtered at
    7 kHz before decimation. Plain linear interpolation (the previous
    implementation) aliased 8-24 kHz energy back into the 0-8 kHz speech
    band, which measurably degrades ASR accuracy.
    """
    if audio is None or len(audio) == 0:
        return audio
    audio = np.asarray(audio, dtype=np.float32)
    if orig_sr == 16000 or orig_sr <= 0:
        return audio

    # Anti-alias before downsampling (no-op for 8 kHz telephony input).
    if orig_sr > 16000:
        audio = np.convolve(
            audio, _fir_lowpass(7000.0, orig_sr), mode="same"
        ).astype(np.float32)

    duration = len(audio) / orig_sr
    target_length = max(1, int(round(duration * 16000)))
    if target_length == len(audio):
        return audio
    orig_times = np.linspace(0.0, duration, len(audio), endpoint=False)
    target_times = np.linspace(0.0, duration, target_length, endpoint=False)
    return np.interp(target_times, orig_times, audio).astype(np.float32)


def remove_rumble(
    audio: np.ndarray,
    sample_rate: int = 16000,
    cutoff_hz: float = 50.0
) -> np.ndarray:
    """
    Remove DC offset and sub-50 Hz desk/HVAC rumble.

    Laptop arrays and headsets often carry most of their energy in the
    bottom octave. That rumble carries no words but skews the level meter
    and wastes ASR capacity. A linear-phase FIR keeps the speech band and
    the speaker's fundamental (85-250 Hz) intact.
    """
    if audio is None or len(audio) == 0 or cutoff_hz <= 0:
        return audio
    low = np.convolve(
        audio, _fir_lowpass(cutoff_hz, sample_rate, taps=511), mode="same"
    )
    return (audio - low).astype(np.float32)


def save_wav_file(audio: np.ndarray, file_path: str = "temp_last_capture.wav", sample_rate: int = 16000) -> str:
    """Save normalized float32 audio as a standard 16-bit PCM WAV file."""
    import wave
    int16_data = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(file_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16_data.tobytes())
    return file_path


def normalize_audio(audio: np.ndarray, target_rms: float = 0.08, max_gain: float = 15.0) -> np.ndarray:
    """
    Safely normalize audio RMS level so quiet headset/laptop mics reach
    optimal Whisper dynamic range without clipping.
    """
    if audio is None or len(audio) == 0:
        return audio
    current_rms = float(np.sqrt(np.mean(audio ** 2)))
    if current_rms < 1e-4:
        return audio
    gain = min(max_gain, target_rms / current_rms)
    normalized = audio * gain
    return np.clip(normalized, -0.99, 0.99).astype(np.float32)


def list_audio_devices(refresh: bool = False) -> List[Dict]:
    """
    Return all available audio input devices, excluding unstable WDM-KS devices.
    """
    if refresh:
        refresh_audio_devices()

    devices = []
    try:
        hostapis = sd.query_hostapis()
        all_devices = sd.query_devices()
        wdm_apis = [i for i, ha in enumerate(hostapis) if "wdm-ks" in ha["name"].lower()]

        for idx, dev in enumerate(all_devices):
            if dev.get("max_input_channels", 0) > 0 and dev.get("hostapi") not in wdm_apis and not is_loopback_device(dev.get("name", "")):
                hostapi_name = hostapis[dev["hostapi"]]["name"] if dev["hostapi"] < len(hostapis) else "Unknown"
                devices.append({
                    "index": idx,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "default_samplerate": dev["default_samplerate"],
                    "hostapi": hostapi_name,
                    "is_default": idx == sd.default.device[0]
                })
    except Exception as e:
        logger.error(f"Error querying audio devices: {e}")
    return devices


def get_default_input_device() -> Optional[Dict]:
    """Return the system default input device information."""
    try:
        def_idx = sd.default.device[0]
        if def_idx is not None and def_idx >= 0:
            dev = sd.query_devices(def_idx)
            return {
                "index": def_idx,
                "name": dev["name"],
                "channels": dev.get("max_input_channels", 1),
                "default_samplerate": int(dev.get("default_samplerate", 44100))
            }
    except Exception as e:
        logger.error(f"Error querying default audio device: {e}")
    return None


def resolve_input_device(
    preferred_index: Optional[int] = None,
    refresh: bool = False,
    exclude: Optional[set] = None
) -> Optional[Tuple[int, str, str, int, int]]:
    """
    Resolve the optimal working input device:
    Returns (device_index, device_name, hostapi_name, sample_rate, channels).

    Priority:
    1. Explicitly configured/selected device index.
    2. Windows default input device (what "auto" means to the user).
    3. External headset/USB microphone, only as a fallback.
    4. First usable input device.

    The previous order checked name keywords before the Windows default, so a
    plugged-in headset silently replaced the user's chosen microphone. Several
    Bluetooth headsets expose an 8 kHz telephony endpoint; recording those
    destroys every consonant above 4 kHz and makes the transcript unusable.
    """
    if refresh:
        refresh_audio_devices()

    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        wdm_apis = [i for i, ha in enumerate(hostapis) if "wdm-ks" in ha["name"].lower()]
        excluded = set(exclude or ())

        valid = []
        for idx, d in enumerate(devices):
            if idx in excluded:
                continue
            if d.get("max_input_channels", 0) > 0 and d.get("hostapi") not in wdm_apis and not is_loopback_device(d.get("name", "")):
                api_name = hostapis[d["hostapi"]]["name"]
                valid.append((idx, d["name"], api_name, int(d["default_samplerate"]), d["max_input_channels"]))

        if not valid:
            logger.warning("No valid audio input devices found.")
            return None

        # 1. Explicit choice always wins.
        if preferred_index is not None:
            match = [v for v in valid if v[0] == preferred_index]
            if match:
                return match[0]
            logger.warning(f"Preferred device index {preferred_index} unavailable; resolving fallback...")

        # 2. Follow the Windows default input device.
        def_idx = sd.default.device[0]
        if def_idx is not None and def_idx >= 0:
            match = [v for v in valid if v[0] == def_idx]
            if match:
                return match[0]
            logger.warning(f"Windows default input device {def_idx} is unavailable; using fallback...")

        # 3. Fallback: connected Headset / Earbuds / Bluetooth / USB microphone
        headset_keywords = ["headset", "buds", "nord", "zeb", "airpods", "wireless", "bluetooth", "usb"]
        exclude_internal = ["array", "realtek", "stereo mix", "mapper"]

        headset_candidates = [
            v for v in valid
            if any(k in v[1].lower() for k in headset_keywords) and not any(ex in v[1].lower() for ex in exclude_internal)
        ]
        if headset_candidates:
            # Prefer DirectSound or WASAPI for robust sample rate handling
            ds_headset = [v for v in headset_candidates if "directsound" in v[2].lower()]
            if ds_headset:
                return ds_headset[0]
            wasapi_headset = [v for v in headset_candidates if "wasapi" in v[2].lower()]
            if wasapi_headset:
                return wasapi_headset[0]
            return headset_candidates[0]

        # 4. Fallback to first valid device
        return valid[0]

    except Exception as e:
        logger.error(f"Error resolving input device: {e}")
        return None


def play_sound_cue(cue_type: str = "start"):
    """Audio cues disabled per user request."""
    pass


class AudioRecorder:
    """
    Robust thread-safe microphone recorder with dynamic stream lifecycle,
    continuous 500ms pre-roll ring buffer, bounded exponential backoff recovery,
    device re-enumeration, and health diagnostics.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        preroll_ms: int = 500,
        device_index: Optional[int] = None,
        silence_threshold_energy: float = 0.0008,
        target_rms_normalize: float = 0.08,
        audio_level_callback: Optional[Callable] = None
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.preroll_ms = preroll_ms
        self.device_index = device_index
        self.silence_threshold_energy = silence_threshold_energy
        self.target_rms_normalize = target_rms_normalize
        self.audio_level_callback = audio_level_callback

        self._active_device_index: Optional[int] = None
        self._active_device_name: str = ""
        self._active_sr: int = 16000
        self._active_channels: int = 1

        self._stream: Optional[sd.InputStream] = None
        self._chunks: List[np.ndarray] = []
        self._blocksize: int = int(self.sample_rate * 0.03)  # ~30ms blocks
        preroll_blocks = max(1, int(self.sample_rate * (preroll_ms / 1000.0) / self._blocksize))
        self._preroll = collections.deque(maxlen=preroll_blocks)
        self._lock = threading.Lock()
        self._is_recording = False
        self._start_time: float = 0.0
        self._last_chunk_ts: float = 0.0
        self._chunk_count: int = 0
        self._stream_error: Optional[str] = None
        self.level: float = 0.0

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def active_device_name(self) -> str:
        return self._active_device_name

    @property
    def active_sample_rate(self) -> int:
        return self._active_sr

    @property
    def active_channels(self) -> int:
        return self._active_channels

    def set_device(self, device_index: Optional[int]):
        """
        Change the active microphone and re-prime the stream immediately.

        Previously this only stored the index. Because the pre-roll stream was
        opened at startup and never reopened, selecting a different microphone
        in the tray had no effect until the app was restarted -- the recorder
        kept reading the old device.
        """
        if self._is_recording:
            self.stop_recording()
        self.device_index = device_index
        self._cleanup_stream()
        started = self.start_stream()
        logger.info(
            f"Audio device preference set to: {device_index} "
            f"(stream {'re-primed' if started else 'will open on next take'})"
        )

    def _is_stream_active(self) -> bool:
        """Safely check if stream is open and active without throwing PortAudioError."""
        if self._stream is None:
            return False
        try:
            return not self._stream.closed and bool(self._stream.active)
        except Exception:
            return False

    def is_stream_alive(self) -> bool:
        """Check if audio stream is currently active and receiving data."""
        if not self._is_recording:
            return False
        return self._is_stream_active() and (time.time() - self._last_chunk_ts < 1.2)

    def _cleanup_stream(self):
        """Safely close and release the audio stream resources."""
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _stream_callback(self, indata, frames, time_info, status):
        """
        Internal PortAudio callback.
        Entire body is protected so no exception escapes into PortAudio CFFI layer.
        """
        try:
            if status:
                logger.warning(f"Audio stream status flag: {status}")
                if status.input_overflow:
                    logger.debug("PortAudio buffer overflow detected.")

            # Safe mono channel extraction
            if indata.ndim == 2:
                chunk = indata[:, 0].copy() if indata.shape[1] > 0 else indata.flatten().copy()
            else:
                chunk = indata.flatten().copy()

            chunk_rms = float(np.sqrt(np.mean(chunk ** 2)))
            self.level = chunk_rms

            with self._lock:
                if self._is_recording:
                    self._chunks.append(chunk)
                    self._chunk_count += 1
                else:
                    self._preroll.append(chunk)
                self._last_chunk_ts = time.time()

            # Compute real audio levels for visualizer (only while recording;
            # the primed idle stream would otherwise wake the GUI 33x/second).
            if self.audio_level_callback and self._is_recording:
                rms = chunk_rms
                normalized_rms = min(1.0, max(0.0, (rms - 0.001) / 0.12))

                num_bars = 24
                sub_len = max(1, len(chunk) // num_bars)
                sub_levels = []
                for b in range(num_bars):
                    sub = chunk[b * sub_len : (b + 1) * sub_len]
                    sub_rms = float(np.sqrt(np.mean(sub ** 2))) if len(sub) > 0 else 0.0
                    val = min(1.0, max(0.0, (sub_rms - 0.0008) / 0.12))
                    sub_levels.append(val)

                try:
                    self.audio_level_callback(normalized_rms, sub_levels)
                except TypeError:
                    try:
                        self.audio_level_callback(normalized_rms)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Error inside audio stream callback: {e}", exc_info=True)
            self._stream_error = str(e)

    def start_stream(self) -> bool:
        """Proactively open background audio stream to prime the 500ms pre-roll buffer."""
        if self._is_stream_active():
            return True
        dev_tuple = resolve_input_device(preferred_index=self.device_index, refresh=False)
        if not dev_tuple:
            return False
        dev_idx, dev_name, hostapi, native_sr, max_ch = dev_tuple
        channels_to_open = min(1, max_ch)
        if is_loopback_device(dev_name):
            logger.warning(
                f"Resolved input device '{dev_name}' looks like a loopback/system-audio "
                "endpoint. Voxen will transcribe system audio instead of your voice. "
                "Set your microphone as the Windows default recording device."
            )
        if native_sr < 16000:
            logger.warning(
                f"Microphone '{dev_name}' reports only {native_sr} Hz; "
                "speech above 4 kHz will be lost and accuracy will suffer. "
                "Select a full-band microphone in the tray if available."
            )
        try:
            self._cleanup_stream()
            self._active_device_index = dev_idx
            self._active_device_name = dev_name
            self._active_sr = native_sr
            self._active_channels = channels_to_open
            self._stream = sd.InputStream(
                samplerate=self._active_sr,
                channels=channels_to_open,
                device=self._active_device_index,
                dtype="float32",
                blocksize=int(self._active_sr * 0.03),
                callback=self._stream_callback
            )
            self._stream.start()
            logger.info(f"Background audio stream primed on '{dev_name}' ({hostapi}) with 500ms pre-roll.")
            return True
        except Exception as e:
            logger.debug(f"Could not prime background stream: {e}")
            return False

    def start_recording(self) -> Tuple[bool, str]:
        """
        Start capturing audio using bounded exponential backoff recovery.
        Prepend 500ms pre-roll buffer so the first syllable is never clipped.
        Returns: (success: bool, status_message: str)
        """
        if self._is_recording:
            return True, "Already recording"

        # Fast-path: stream is already primed and active!
        if self._is_stream_active():
            with self._lock:
                self._chunks = list(self._preroll)
                self._preroll.clear()
                self._chunk_count = len(self._chunks)
                self._stream_error = None
                self._is_recording = True
                self._start_time = time.time()
                self._last_chunk_ts = time.time()
            logger.info(f"Audio recording started instantly ({self._chunk_count} pre-roll frames prepended).")
            return True, "Recording started"

        with self._lock:
            self._chunks.clear()
            self._chunk_count = 0
            self._stream_error = None

        self._cleanup_stream()

        # Bounded exponential backoff retry loop (up to 3 attempts)
        backoff_delays = [0.0, 0.08, 0.25]
        last_error = "Unknown error"
        failed_devices = set()

        for attempt, delay in enumerate(backoff_delays, start=1):
            if delay > 0:
                time.sleep(delay)

            # Re-enumerate devices if previous attempt failed, and never retry
            # the same device that just failed to open.
            refresh_needed = (attempt > 1)
            target_pref = self.device_index if attempt == 1 else None
            dev_tuple = resolve_input_device(
                preferred_index=target_pref,
                refresh=refresh_needed,
                exclude=failed_devices
            )

            if not dev_tuple:
                last_error = "No available microphone found in Windows"
                continue

            dev_idx, dev_name, hostapi, native_sr, max_ch = dev_tuple
            channels_to_open = min(1, max_ch)
            if native_sr < 16000:
                logger.warning(
                    f"Microphone '{dev_name}' only supports {native_sr} Hz "
                    "(narrowband); transcription accuracy will be poor."
                )

            try:
                self._active_device_index = dev_idx
                self._active_device_name = dev_name
                self._active_sr = native_sr
                self._active_channels = channels_to_open

                self._stream = sd.InputStream(
                    samplerate=self._active_sr,
                    channels=channels_to_open,
                    device=self._active_device_index,
                    dtype="float32",
                    blocksize=int(self._active_sr * 0.03),  # 30ms frames
                    callback=self._stream_callback
                )
                self._stream.start()

                if self._is_stream_active():
                    self._is_recording = True
                    self._start_time = time.time()
                    self._last_chunk_ts = time.time()
                    logger.info(
                        f"Audio stream started: Device #{dev_idx} '{dev_name}' ({hostapi}) at {native_sr} Hz [attempt {attempt}]"
                    )
                    return True, "Recording started"
                else:
                    self._cleanup_stream()
                    last_error = "Stream opened but did not transition to active state"
                    failed_devices.add(dev_idx)

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                failed_devices.add(dev_idx)
                logger.warning(f"Audio start attempt {attempt}/{len(backoff_delays)} failed ({last_error})")
                self._cleanup_stream()

        self._is_recording = False
        logger.error(f"Failed to start audio stream after {len(backoff_delays)} attempts: {last_error}")
        return False, last_error

    def stop_recording(self) -> Tuple[Optional[np.ndarray], float, Dict]:
        """
        Stop audio capture, resample to 16 kHz, remove rumble, normalize gain,
        and return audio with explicit status telemetry.

        The PortAudio stream is intentionally kept open after a take so the
        500 ms pre-roll ring buffer is ready for the next one. Closing it here
        (the old behaviour) meant only the very first dictation had pre-roll;
        every later take cold-opened the device and clipped the first syllable.
        Returns:
            (audio_array: Optional[np.ndarray], duration: float, status_dict: Dict)
        """
        # Flip the recording flag and snapshot chunks atomically. Setting the
        # flag outside the lock let a final callback block land in the pre-roll
        # and drop the last ~30 ms (often the final consonant).
        with self._lock:
            was_recording = self._is_recording
            self._is_recording = False
            wall_duration = max(0.0, time.time() - self._start_time) if was_recording else 0.0
            captured_chunks = list(self._chunks)
            chunk_count = self._chunk_count
            self._chunks.clear()
            self._chunk_count = 0

        if not was_recording and not captured_chunks:
            return None, 0.0, {
                "success": False,
                "reason": "not_recording",
                "error": "Recorder was not in active recording state."
            }

        # 1. Zero samples failure check
        if chunk_count == 0 or not captured_chunks:
            logger.error("Capture failure: 0 audio chunks received from microphone.")
            return None, wall_duration, {
                "success": False,
                "reason": "zero_samples",
                "error": "No audio received from microphone. Check your connection.",
                "device": self._active_device_name
            }

        # 2. Too short check (<200ms)
        min_samples = int(self._active_sr * 0.2)
        raw_audio = np.concatenate(captured_chunks, axis=0)
        if len(raw_audio) < min_samples:
            logger.info("Recording too short (<200ms), discarded.")
            return None, wall_duration, {
                "success": False,
                "reason": "too_short",
                "error": "Recording too short (<200ms).",
                "device": self._active_device_name
            }

        # 3. Stream error check
        if self._stream_error:
            logger.warning(f"Stream error recorded during capture: {self._stream_error}")

        # 4. Process valid audio: resample (anti-aliased), remove rumble, normalize
        raw_rms = float(np.sqrt(np.mean(raw_audio ** 2)))
        audio_16k = resample_to_16k(raw_audio, self._active_sr)
        audio_16k = remove_rumble(audio_16k, 16000)
        duration = len(audio_16k) / 16000.0
        normalized_audio = normalize_audio(audio_16k, target_rms=self.target_rms_normalize)
        norm_rms = float(np.sqrt(np.mean(normalized_audio ** 2)))

        is_silent = raw_rms < self.silence_threshold_energy

        # Save temporary WAV for verification and playback
        wav_path = "temp_last_capture.wav"
        try:
            save_wav_file(normalized_audio, wav_path, 16000)
            appdata = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Voxen")
            os.makedirs(appdata, exist_ok=True)
            save_wav_file(normalized_audio, os.path.join(appdata, "last.wav"), 16000)
            logger.info(f"Saved temporary capture to '{wav_path}' and '{appdata}\\last.wav'.")
        except Exception as e:
            logger.warning(f"Could not save temporary WAV file: {e}")

        logger.info(
            f"Captured {duration:.2f}s audio on '{self._active_device_name}' "
            f"({self._active_sr} Hz, {self._active_channels} ch) "
            f"(Raw RMS: {raw_rms:.5f} -> Norm RMS: {norm_rms:.5f}, Silent: {is_silent}, Chunks: {chunk_count})"
        )

        return normalized_audio, duration, {
            "success": True,
            "reason": "ok" if not is_silent else "silent_speech",
            "is_silent": is_silent,
            "raw_rms": raw_rms,
            "norm_rms": norm_rms,
            "device": self._active_device_name,
            "sample_rate": self._active_sr,
            "duration": duration,
            "wall_duration": wall_duration,
            "chunks": chunk_count,
            "wav_path": wav_path
        }

    def cancel_recording(self):
        """Discard the current take, but keep the stream primed for the next."""
        with self._lock:
            self._is_recording = False
            self._chunks.clear()
            self._chunk_count = 0
        logger.info("Audio recording cancelled; stream remains primed.")

    def close(self):
        """Release the microphone and PortAudio resources (app shutdown)."""
        with self._lock:
            self._is_recording = False
            self._chunks.clear()
            self._chunk_count = 0
        self._cleanup_stream()
        logger.info("Audio stream closed.")



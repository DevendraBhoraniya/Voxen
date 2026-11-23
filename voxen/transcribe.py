"""
Local speech-to-text transcription module for Voxen.
Uses faster-whisper (CTranslate2) with auto CUDA/CPU detection,
VAD filtering, and CPU thread throttling for smooth multitasking.
"""

import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple
from faster_whisper.vad import VadOptions, get_speech_timestamps
import numpy as np

logger = logging.getLogger("Voxen.Transcribe")


def setup_cuda_dlls():
    """Add NVIDIA pip wheel bin directories to Windows DLL search path."""
    import sys
    venv_site = os.path.join(sys.prefix, "Lib", "site-packages")
    nvidia_base = os.path.join(venv_site, "nvidia")
    if os.path.isdir(nvidia_base):
        for sub in os.listdir(nvidia_base):
            bin_dir = os.path.join(nvidia_base, sub, "bin")
            if os.path.isdir(bin_dir):
                try:
                    os.add_dll_directory(bin_dir)
                except Exception:
                    pass
                if bin_dir not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


# Prime CUDA runtime DLLs immediately on Windows
setup_cuda_dlls()

SR = 16000
MAX_CHUNK_S = 28  # Stay under Whisper's 30s window to prevent context contamination

# Split long speech at natural pauses; Silero VAD also eliminates silence hallucinations
_VAD = VadOptions(max_speech_duration_s=25, min_silence_duration_ms=300, speech_pad_ms=100)


def speech_chunks(audio: np.ndarray) -> Optional[List[np.ndarray]]:
    """
    Speech regions packed into <=28s chunks, cut at natural pauses.

    Returns:
        list of chunks (possibly empty when VAD finds no speech),
        or None when the VAD itself failed and the caller should fall back
        to transcribing the full clip with Whisper's internal VAD.
    """
    if audio is None or len(audio) == 0:
        return []
    try:
        segs = get_speech_timestamps(audio, _VAD)
    except Exception as e:
        logger.warning(f"VAD chunking exception ({e}); using full audio")
        return None

    if not segs:
        return []

    chunks = []
    for seg in segs:
        if chunks and seg["end"] - chunks[-1][0] <= MAX_CHUNK_S * SR:
            chunks[-1][1] = seg["end"]
        else:
            chunks.append([seg["start"], seg["end"]])
    return [audio[a:b] for a, b in chunks]


def collapse_repeats(text: str) -> str:
    """Safety net: a phrase repeated 3+ times in a row is a whisper loop, keep one copy."""
    return re.sub(r"(\b.{4,120}?[.?!,]?)(?:\s+\1){2,}", r"\1", text, flags=re.IGNORECASE)

# Reference technical vocabulary from prompt requirements
REFERENCE_VOCABULARY: List[str] = [
    "API", "RAG", "retrieval", "embeddings", "vector database",
    "Python", "Claude Code", "Codex", "Ollama", "Voxen",
    "GitHub", "PostgreSQL", "FastAPI", "agentic AI",
    "toggle", "hold"
]

DEFAULT_INITIAL_PROMPT = (
    "Voice dictation. Technical vocabulary: "
    + ", ".join(REFERENCE_VOCABULARY)
    + "."
)

# Conservative word-boundary regex corrections for recurring STT errors
DEFAULT_CORRECTIONS: Dict[str, str] = {
    # Voxen brand
    r"\b[Vv]oice\s*[Bb]ut\b": "Voxen",
    r"\b[Vv]oice\s*[Bb]ud\b": "Voxen",
    r"\b[Vv]oicebut\b": "Voxen",
    r"\b[Vv]oicebud\b": "Voxen",
    # Hotkey mode names (confirmed misrecognition safety net)
    r"(?i)\bdogal\b": "toggle",
    # Frameworks & Libraries
    r"\b[Ff]ast\s*[Aa][Pp][Ii]\b": "FastAPI",
    r"\b[Ff]astapi\b": "FastAPI",
    r"\b[Pp]ost\s*[Gg]res\b": "PostgreSQL",
    r"\b[Pp]ostgresql\b": "PostgreSQL",
    r"\b[Pp]ostgres\b": "PostgreSQL",
    r"\b[Gg]it\s*[Hh]ub\b": "GitHub",
    r"\b[Gg]ithub\b": "GitHub",
    r"\b[Cc]laude\s*[Cc]ode\b": "Claude Code",
    r"\b[Oo]llama\b": "Ollama",
    r"\b[Aa]pi\b": "API",
    r"\b[Aa][Pp][Ii]s\b": "APIs",
    r"\b[Rr]ag\b": "RAG",
    r"\b[Aa]gentic\s*[Aa][Ii]\b": "agentic AI",
    r"\b[Vv]ector\s*[Dd]atabase\b": "vector database",
    r"\b[Vv]ector\s*[Dd]atabases\b": "vector databases",
}


def apply_vocabulary_corrections(text: str, corrections: Dict[str, str]) -> Tuple[str, bool]:
    """
    Apply conservative word-boundary regex corrections for known technical jargon
    and brand names without corrupting ordinary vocabulary.
    """
    if not text:
        return text, False
    corrected = text
    changed = False
    for pattern, replacement in corrections.items():
        new_text, count = re.subn(pattern, replacement, corrected)
        if count > 0:
            corrected = new_text
            changed = True
    return corrected, changed


def detect_hardware() -> Dict:
    """Detect available compute hardware and return optimal configuration."""
    has_cuda = False
    cuda_name = "None"
    cuda_usable = False
    cuda_reason = ""

    try:
        import ctranslate2
        cuda_count = ctranslate2.get_cuda_device_count()
        if cuda_count > 0:
            has_cuda = True
            cuda_name = f"NVIDIA GPU (Devices: {cuda_count})"
            # Instant verification: check if CUDA runtime cublas DLL is loadable
            import ctypes
            try:
                ctypes.cdll.LoadLibrary("cublas64_12.dll")
                cuda_usable = True
            except OSError as e:
                cuda_usable = False
                cuda_reason = "cublas64_12.dll not found in PATH"
                logger.info(f"NVIDIA GPU present, but cublas64_12.dll missing: {e}")
    except Exception as e:
        logger.debug(f"CUDA detection failed: {e}")

    cpu_cores = os.cpu_count() or 4
    # On 4-core machine, reserve 2 cores for Word/Chrome and 2 for Whisper
    recommended_threads = max(1, min(4, cpu_cores // 2))

    # Model recommendation:
    # If usable CUDA: "large-v3-turbo" (float16) matching reference repository
    # If CPU: "small.en" (int8)
    recommended_model = "large-v3-turbo" if cuda_usable else "base.en"  # base.en: measured faster than distil-small.en on this CPU
    recommended_device = "cuda" if cuda_usable else "cpu"
    recommended_compute = "float16" if cuda_usable else "int8"

    return {
        "has_cuda": has_cuda,
        "cuda_usable": cuda_usable,
        "cuda_name": cuda_name,
        "cuda_reason": cuda_reason,
        "cpu_cores": cpu_cores,
        "recommended_threads": recommended_threads,
        "recommended_model": recommended_model,
        "recommended_device": recommended_device,
        "recommended_compute": recommended_compute
    }


class WhisperTranscriber:
    """Manages faster-whisper model lifecycle and audio transcription."""

    def __init__(
        self,
        # base.en is the accuracy/CPU-speed sweet spot for dictation. tiny.en
        # (the previous default) mis-hears heavily, and small.en runs slower
        # than real time on a typical laptop CPU.
        # base.en: accuracy/CPU-speed sweet spot for dictation on this machine.
        model_name: str = "base.en",
        device: str = "auto",
        compute_type: str = "auto",
        cpu_threads: int = 4,
        language: Optional[str] = "en",
        vad_filter: bool = True,
        beam_size: int = 2,
        best_of: int = 1,
        repetition_penalty: float = 1.2,
        no_repeat_ngram_size: int = 3,
        initial_prompt: Optional[str] = DEFAULT_INITIAL_PROMPT,
        corrections: Optional[Dict[str, str]] = None
    ):
        self.model_name = model_name
        self.requested_device = device
        self.requested_compute = compute_type
        self.cpu_threads = cpu_threads
        self.language = language
        self.vad_filter = vad_filter
        self.beam_size = beam_size
        self.best_of = best_of
        self.repetition_penalty = repetition_penalty
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self.initial_prompt = initial_prompt
        self.corrections = corrections if corrections is not None else dict(DEFAULT_CORRECTIONS)
        self.last_raw_transcript = ""
        self.last_avg_logprob = -1.0

        self.actual_device = "cpu"
        self.actual_compute = "int8"
        self.model = None
        self._init_model()

    def _init_model(self):
        """Initialize or load the local WhisperModel with fallback handling."""
        from faster_whisper import WhisperModel

        hw = detect_hardware()

        if self.requested_device == "auto":
            target_device = hw["recommended_device"]
        else:
            target_device = self.requested_device.lower()

        if self.requested_compute == "auto":
            target_compute = "float16" if target_device == "cuda" else "int8"
        else:
            target_compute = self.requested_compute.lower()

        logger.info(f"Loading Whisper model '{self.model_name}' on {target_device} ({target_compute})...")

        # Attempt loading target device if cuda
        if target_device == "cuda" and hw.get("cuda_usable", False):
            try:
                self.model = WhisperModel(
                    self.model_name,
                    device="cuda",
                    compute_type=target_compute,
                    cpu_threads=self.cpu_threads
                )
                self.actual_device = "cuda"
                self.actual_compute = target_compute
                logger.info(f"Loaded '{self.model_name}' successfully on CUDA.")
                return
            except Exception as e:
                logger.warning(f"Failed to load model on CUDA ({e}). Falling back to CPU int8...")

        # CPU mode with int8 quantization
        try:
            # Offline-only: never download, use cached model only
            self.model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=self.cpu_threads,
                local_files_only=True
            )
            self.actual_device = "cpu"
            self.actual_compute = "int8"
            logger.info(f"Loaded '{self.model_name}' successfully on CPU int8 ({self.cpu_threads} worker threads).")
        except Exception as e:
            logger.critical(f"Fatal error loading Whisper model on CPU: {e}", exc_info=True)
            raise

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000
    ) -> Tuple[str, float]:
        """
        Transcribe audio array (float32 at 16000 Hz).
        Returns:
            (transcript_text: str, processing_time_seconds: float)
        """
        if audio is None or len(audio) == 0:
            return "", 0.0

        # Quick silence check: if all zeros or negligible energy, skip
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < 0.001:
            logger.info("Audio RMS below audible threshold, skipping transcription.")
            return "", 0.0

        start_t = time.time()

        try:
            kwargs = {
                "beam_size": self.beam_size,
                "best_of": self.best_of,
                "language": self.language or "en",
                "vad_filter": self.vad_filter,
                "condition_on_previous_text": False,
                "temperature": 0.0,
                "no_speech_threshold": 0.7,
                "repetition_penalty": self.repetition_penalty,
                "no_repeat_ngram_size": self.no_repeat_ngram_size
            }
            if self.initial_prompt:
                kwargs["initial_prompt"] = self.initial_prompt

            vad_params = dict(
                min_silence_duration_ms=400,
                speech_pad_ms=200,  # Reduced padding to avoid capturing trailing noise
                threshold=0.50      # Higher threshold to filter noise and reduce hallucination
            )

            # Split speech at natural pauses into <= 28s independent windows.
            # speech_chunks() returns None when the VAD itself failed and []
            # when it found no speech at all.
            chunks = speech_chunks(audio)
            if chunks is None:
                # VAD failure: let Whisper do its own voice activity detection
                # on the whole clip rather than giving up on the take.
                logger.warning("VAD unavailable; transcribing full clip with Whisper VAD.")
                chunks = [audio]
                kwargs["vad_filter"] = self.vad_filter
                if self.vad_filter:
                    kwargs["vad_parameters"] = vad_params
            elif not chunks:
                logger.info("No speech detected in audio; returning empty transcript.")
                self.last_raw_transcript = ""
                return "", time.time() - start_t
            else:
                # Chunks are already trimmed to speech, so running Whisper's
                # VAD a second time only risks clipping soft consonants.
                kwargs["vad_filter"] = False

            texts = []
            logprobs = []
            for chunk in chunks:
                if len(chunk) == 0:
                    continue
                segments, info = self.model.transcribe(chunk, **kwargs)
                for seg in segments:
                    t = seg.text.strip()
                    if t:
                        texts.append(t)
                        logprobs.append(seg.avg_logprob)
                        logger.debug(
                            f"Segment [{seg.start:.2f}s - {seg.end:.2f}s] "
                            f"(avg_logprob: {seg.avg_logprob:.3f}, no_speech_prob: {seg.no_speech_prob:.3f}): '{t}'"
                        )

            # Track average logprob for cleanup skip decision
            self.last_avg_logprob = (sum(logprobs) / len(logprobs)) if logprobs else -1.0

            raw_transcript = " ".join(texts).strip()
            self.last_raw_transcript = raw_transcript
            duration = time.time() - start_t

            # Log raw Whisper output length at DEBUG (redacted: never log the
            # actual transcript at INFO, which would persist dictated text).
            logger.debug(f"[RAW WHISPER OUTPUT] {len(raw_transcript)} chars")

            # Collapse any 3+ repetition loops from Whisper
            deduped_transcript = collapse_repeats(raw_transcript)

            # Apply conservative vocabulary corrections for technical terms
            corrected_transcript, was_corrected = apply_vocabulary_corrections(deduped_transcript, self.corrections)
            if was_corrected:
                logger.debug(f"[VOCABULARY CORRECTION] {len(raw_transcript)} chars -> {len(corrected_transcript)} chars")

            logger.info(f"Transcription completed in {duration:.2f}s ({len(corrected_transcript)} chars)")
            return corrected_transcript, duration

        except Exception as e:
            logger.error(f"Error during transcription: {e}", exc_info=True)
            return "", time.time() - start_t



def create_transcriber(
    model_name: str = "base.en",
    device: str = "auto",
    compute_type: str = "auto",
    cpu_threads: int = 4,
    language: Optional[str] = "en",
    vad_filter: bool = True,
    beam_size: int = 2,
    best_of: int = 1,
    repetition_penalty: float = 1.2,
    no_repeat_ngram_size: int = 3,
    initial_prompt: Optional[str] = DEFAULT_INITIAL_PROMPT,
    corrections: Optional[Dict[str, str]] = None,
) -> object:
    """Factory function to instantiate WhisperTranscriber (offline-only, faster-whisper)."""
    return WhisperTranscriber(
        model_name=model_name,
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        language=language,
        vad_filter=vad_filter,
        beam_size=beam_size,
        best_of=best_of,
        repetition_penalty=repetition_penalty,
        no_repeat_ngram_size=no_repeat_ngram_size,
        initial_prompt=initial_prompt,
        corrections=corrections
    )

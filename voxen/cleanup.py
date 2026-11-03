"""
Local text refinement module for Voxen using Qwen2.5-0.5B (llama.cpp).
Fast offline semantic cleanup: removes self-corrections, filler words, false starts.
Adds punctuation and capitalisation. Never raises.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("Voxen.Cleanup")

_QWEN_MODEL_PATH = (
    Path(os.environ["LOCALAPPDATA"]) / "Voxen" / "models" / "qwen"
    / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
)

_SYSTEM_PROMPT = (
    "You are a dictation cleanup assistant.\n"
    "Rules:\n"
    "1. Keep only the FINAL intended version when the speaker corrects themselves.\n"
    "   Example: 'meeting at 8 no no 9 PM' → 'meeting at 9 PM'\n"
    "2. Remove filler words: um, uh, like, you know, okay so, I mean.\n"
    "3. Remove abandoned false starts.\n"
    "4. Add correct punctuation and capitalisation.\n"
    "5. Do NOT add or invent any information.\n"
    "6. Output ONLY the cleaned text. No explanation.\n"
    "7. Keep all technical terms exactly as given.\n"
    "8. If already clean, return it unchanged."
)


class LlamaCleaner:
    """
    Fast offline semantic cleanup using Qwen2.5-0.5B-Instruct (Q4_K_M GGUF).

    Removes self-corrections, filler words, false starts.
    Adds punctuation and capitalisation.

    RAM:   ~400 MB
    Speed: ~0.3–0.8s per clip on CPU
    Setup: run tools/setup_cleanup_model.py once to download the model.
    """

    MIN_WORDS = 4   # skip cleanup for very short clips

    def __init__(self, n_threads=None):
        self._llm = None
        self._available = False
        if n_threads is None:
            n_threads = max(1, (os.cpu_count() or 4) // 2)
        self._n_threads = n_threads
        self._try_load()

    def _try_load(self) -> None:
        if not _QWEN_MODEL_PATH.exists():
            return
        try:
            from llama_cpp import Llama
            self._llm = Llama(
                model_path=str(_QWEN_MODEL_PATH),
                n_ctx=512,
                n_threads=self._n_threads,
                n_gpu_layers=0,   # CPU only
                verbose=False,
                use_mlock=False,
            )
            self._available = True
        except Exception as exc:
            logger.warning(f"LlamaCleaner failed to load: {exc}")

    def warmup(self) -> None:
        '''Run a dummy cleanup to warm up the model/KV cache.'''
        if self._available:
            try:
                self._llm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": "um so I was thinking we should meet at 8 no no 9 PM"},
                    ],
                    max_tokens=64, temperature=0.0, top_p=1.0,
                )
            except Exception:
                pass

    @property
    def is_available(self) -> bool:
        return self._available

    def clean(self, text: str, avg_logprob: float = -1.0) -> str:
        """
        Clean raw transcript. Returns original text on any failure.
        Never raises.
        """
        if not self._available:
            return text
        if not text or len(text.split()) < self.MIN_WORDS:
            logger.debug(f"LlamaCleaner: skipped (too short, {len(text.split())} words < {self.MIN_WORDS})")
            return text
        lower = text.lower()
        correction_markers = ["no wait", "i mean", "sorry", "scratch that", "no no", "actually let", "let me rephrase", "um", "uh"]
        has_correction = any(m in lower for m in correction_markers)
        # Skip if high confidence (avg_logprob above threshold) and no correction markers
        logprob_threshold = -0.3
        if avg_logprob > logprob_threshold and not has_correction:
            logger.info(f"LlamaCleaner: skipped (high-confidence avg_logprob={avg_logprob:.3f}, no correction markers)")
            return text
        if not has_correction:
            logger.info(f"LlamaCleaner: skipped (no correction markers)")
            return text

        try:
            resp = self._llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": text},
                ],
                max_tokens=256,
                temperature=0.0,
                top_p=1.0,
                stop=["<|im_end|>", "\n\n"],
            )
            cleaned = resp["choices"][0]["message"]["content"].strip()
            # Safety: if output is empty or suspiciously long, return original
            if not cleaned or len(cleaned) > len(text) * 2.5:
                return text
            logger.info(f"LlamaCleaner: cleaned ({len(text.split())} words)")
            return cleaned
        except Exception as exc:
            logger.warning(f"LlamaCleaner.clean failed: {exc}")
            return text

"""
Optional text refinement module for Voxen using local Ollama Chat API.
Formats punctuation, capitalization, and paragraph boundaries at natural pauses
while strictly preserving meaning, technical vocabulary, numbers, and intent.
Never answers questions, paraphrases, or injects conversational preamble.
"""

import difflib
import logging
import re
import time
from typing import Dict, List, Optional, Tuple, Set
import requests

logger = logging.getLogger("Voxen.Cleanup")

DISALLOWED_PREFIXES = [
    "here is", "here's", "sure", "certainly", "as an ai", "i cannot",
    "i can", "the cleaned text", "transcription:", "corrected:", "output:",
    "here is the cleaned", "here is the corrected", "note:", "below is",
    "cleaned transcript:", "formatted text:", "cleaned text:"
]

COMMON_FILLERS = {"um", "uh", "er", "ah", "like", "you know"}


def _words(s: str) -> List[str]:
    """Extract lowercased word tokens."""
    return re.findall(r"[a-z0-9']+", s.lower().replace("’", "'"))


def similarity(raw: str, cleaned: str) -> float:
    """Word-level similarity (0.0 to 1.0). Minor edits keep it high; hallucination lowers it."""
    return difflib.SequenceMatcher(None, _words(raw), _words(cleaned), autojunk=False).ratio()


def restore_vocabulary(raw: str, out: str, vocabulary: Optional[List[str]]) -> str:
    """Put back vocabulary words the LLM may have accidentally respelled."""
    if not vocabulary:
        return out
    vocab = {w.lower() for term in vocabulary for w in _words(term)}
    if not vocab:
        return out
    raw_tokens = re.findall(r"[A-Za-z0-9']+", raw.replace("\u2019", "'"))
    out_matches = list(re.finditer(r"[A-Za-z0-9']+", out))
    sm = difflib.SequenceMatcher(
        None,
        [t.lower() for t in raw_tokens],
        [m.group().lower() for m in out_matches],
        autojunk=False
    )
    fixes = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace":
            continue
        if i2 - i1 == j2 - j1:
            for r, m in zip(raw_tokens[i1:i2], out_matches[j1:j2]):
                if r.lower() in vocab:
                    fixes.append((m.start(), m.end(), r))
        elif i2 - i1 == 1 and raw_tokens[i1].lower() in vocab:
            fixes.append((out_matches[j1].start(), out_matches[j2 - 1].end(), raw_tokens[i1]))
    for start, end, word in reversed(fixes):
        out = out[:start] + word + out[end:]
    return out


def clean_base_url(endpoint: str) -> str:
    """Normalize endpoint to base URL (e.g. http://127.0.0.1:11434)."""
    return endpoint.split("/api")[0].rstrip("/")


def check_ollama_status(endpoint_base: str = "http://127.0.0.1:11434") -> Dict:
    """Check if local Ollama daemon is reachable and list installed models."""
    base = clean_base_url(endpoint_base)
    url = f"{base}/api/tags"
    try:
        resp = requests.get(url, timeout=1.2)
        if resp.status_code == 200:
            models_data = resp.json().get("models", [])
            model_names = [m.get("name") for m in models_data if m.get("name")]
            return {
                "available": True,
                "models": model_names,
                "error": None
            }
    except Exception as e:
        return {
            "available": False,
            "models": [],
            "error": str(e)
        }
    return {"available": False, "models": [], "error": "Unknown status"}


def check_model_parameters(endpoint_base: str, model_name: str) -> Tuple[bool, str]:
    """
    Check if the configured model is installed and accessible via Ollama API.
    Uses /api/show to inspect model capabilities.
    """
    base = clean_base_url(endpoint_base)
    url = f"{base}/api/show"
    try:
        resp = requests.post(url, json={"name": model_name}, timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            details = data.get("details", {})
            param_info = data.get("parameters", "")
            logger.debug(f"Model '{model_name}' verified: format={details.get('format')}, family={details.get('family')}")
            return True, "Model verified"
        elif resp.status_code == 404:
            return False, f"Model '{model_name}' not found in Ollama"
        else:
            return False, f"Ollama /api/show returned HTTP {resp.status_code}"
    except Exception as e:
        return False, f"Error querying model info: {type(e).__name__}"


def sanitize_formatting(text: str) -> str:
    """
    Normalize typographic quirks:
    - Replace em dashes (—) and en dashes (–) with standard hyphens (-) or commas
    - Replace curly double quotes (“ ”) with straight quotes (")
    - Replace curly single quotes (‘ ’) with straight quotes (')
    - Strip whole-text wrapping quotation marks
    - Strip markdown code blocks if present
    """
    if not text:
        return ""

    out = text

    # Strip code block fences
    if out.startswith("```") and out.endswith("```"):
        lines = out.splitlines()
        if len(lines) >= 2:
            out = "\n".join(lines[1:-1]).strip()

    # Replace em-dashes and en-dashes
    out = out.replace("—", " - ")
    out = out.replace("–", "-")

    # Replace curly quotes
    out = out.replace("“", '"').replace("”", '"')
    out = out.replace("‘", "'").replace("’", "'")

    # Clean double spaces caused by dash replacement
    out = re.sub(r" +", " ", out)

    # Strip wrapping outer quotes
    trimmed = out.strip()
    if (trimmed.startswith('"') and trimmed.endswith('"')) or (trimmed.startswith("'") and trimmed.endswith("'")):
        if trimmed.count('"') == 2 or trimmed.count("'") == 2:
            trimmed = trimmed[1:-1].strip()
            out = trimmed

    return out.strip()


class OllamaCleaner:
    """
    Cleans speech transcripts using local Ollama Chat API (/api/chat)
    with strict guardrails, lazy detection, parameter validation,
    and privacy-preserving telemetry.
    """

    def __init__(
        self,
        enabled: bool = False,
        endpoint: str = "http://127.0.0.1:11434",
        model: str = "llama3.2:3b",
        timeout_seconds: float = 4.0,
        temperature: float = 0.1,
        min_char_length: int = 20,
        min_words: int = 10,
        min_similarity: float = 0.8,
        keep_alive: str = "30m",
        vocabulary: Optional[List[str]] = None,
        example_input: Optional[str] = None,
        example_output: Optional[str] = None,
        remove_fillers: bool = True,
        system_prompt: Optional[str] = None
    ):
        self.enabled = enabled
        self.base_url = clean_base_url(endpoint)
        self.chat_endpoint = f"{self.base_url}/api/chat"
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.min_char_length = min_char_length
        self.min_words = min_words
        self.min_similarity = min_similarity
        self.keep_alive = keep_alive
        self.vocabulary = list(vocabulary or [])
        self.example_input = example_input
        self.example_output = example_output
        self.remove_fillers = remove_fillers

        self.system_prompt = system_prompt or (
            "You format dictated speech into clean written text. You are an editor, not a writer.\n"
            "Rules:\n"
            "- Remove filler words (um, uh, you know, 'like' used as filler) and accidental repeated words.\n"
            "- Fix punctuation and capitalization. Split run-on speech into proper sentences.\n"
            "- Fix small grammar slips only (e.g. 'things that gives' -> 'things that give').\n"
            "- Never change how a word is spelled, and never replace a word with a different one.\n"
            "- Group sentences into paragraphs of 2 to 4 sentences, separated by a blank line.\n"
            "- Keep every word the speaker said, in the same order, except fillers and accidental repeats.\n"
            "- Do not use em dashes, semicolons, bullet points or markdown.\n"
            "- Output only the formatted text."
        )

        # Lazy connection cache
        self._last_health_check: float = 0.0
        self._is_online: bool = False
        self._model_verified: bool = False
        self._health_ttl: float = 8.0  # Seconds between offline probes

    def is_online(self, force: bool = False) -> bool:
        """
        Lazily check if Ollama daemon is reachable.
        Re-probes at most every self._health_ttl seconds if offline.
        """
        now = time.time()
        if not force and self._is_online and (now - self._last_health_check < 45.0):
            return True
        if not force and not self._is_online and (now - self._last_health_check < self._health_ttl):
            return False

        self._last_health_check = now
        status = check_ollama_status(self.base_url)
        was_online = self._is_online
        self._is_online = status.get("available", False)

        if self._is_online and not was_online:
            models = status.get("models", [])
            logger.info(f"Ollama daemon detected online (models: {', '.join(models) if models else 'none'}).")
            # Verify configured model
            self._model_verified = self.model in models or any(m.startswith(self.model) for m in models)
            if not self._model_verified:
                logger.warning(f"Configured model '{self.model}' not listed in Ollama models. Will verify on demand.")

        return self._is_online

    def validate_cleanup(self, raw: str, cleaned: str) -> Tuple[bool, str]:
        """
        Comprehensive comparison of input and output.
        Rejects suspicious changes, conversational hallucinations, question answering,
        and significant vocabulary/number drops.
        Returns: (is_valid: bool, rejection_reason: str)
        """
        if not cleaned:
            return False, "empty_output"

        lower_cleaned = cleaned.lower().strip()
        lower_raw = raw.lower().strip()

        # 1. Reject conversational preamble / chat hallucination
        for prefix in DISALLOWED_PREFIXES:
            if lower_cleaned.startswith(prefix):
                return False, f"conversational_prefix ('{prefix}')"

        # 2. Length ratio sanity check (0.45 <= ratio <= 1.70)
        len_ratio = len(cleaned) / max(1, len(raw))
        if len_ratio < 0.45 or len_ratio > 1.70:
            return False, f"abnormal_length_ratio ({len_ratio:.2f})"

        # 3. Question Answering Guardrail:
        # Detect if input was an interrogative question that the model answered instead of formatting
        interrogative_starts = (
            "what", "how", "why", "when", "where", "who", "which",
            "is there", "can you", "could you", "do you", "are we",
            "will the", "should we", "would it", "does it"
        )
        is_raw_question = any(lower_raw.startswith(q) for q in interrogative_starts) or lower_raw.endswith("?")
        if is_raw_question:
            # Cleaned text must end with a question mark or contain interrogative phrasing
            has_qmark = "?" in cleaned
            has_question_word = any(q.split()[0] in lower_cleaned.split() for q in interrogative_starts)
            if not has_qmark and not has_question_word:
                return False, "answered_question_instead_of_formatting"

        # 4. Extract content words and check preservation
        raw_tokens = re.findall(r"\b[a-zA-Z0-9_-]{3,}\b", lower_raw)
        # Filter out common filler words
        raw_content = [t for t in raw_tokens if t not in COMMON_FILLERS]
        cleaned_tokens = set(re.findall(r"\b[a-zA-Z0-9_-]{3,}\b", lower_cleaned))

        if raw_content:
            preserved_count = sum(1 for w in raw_content if w in cleaned_tokens)
            overlap_ratio = preserved_count / len(raw_content)
            if overlap_ratio < 0.65:
                return False, f"low_vocabulary_preservation ({overlap_ratio:.2f} < 0.65)"

        # 5. Number and Entity Preservation Check
        # Ensure all spoken or digit numbers in raw are preserved
        raw_numbers = set(re.findall(r"\b\d+\b", lower_raw))
        cleaned_numbers = set(re.findall(r"\b\d+\b", lower_cleaned))
        # Check digit numbers
        for num in raw_numbers:
            if num not in cleaned_numbers:
                # Check if it was converted to words or vice-versa
                word_to_num = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                               "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
                if not any(k in lower_cleaned for k, v in word_to_num.items() if v == num):
                    return False, f"dropped_number ('{num}')"

        return True, "valid"

    def is_valid_cleanup(self, raw: str, cleaned: str) -> bool:
        """Backwards compatible boolean validation check."""
        valid, _ = self.validate_cleanup(raw, cleaned)
        return valid

    def clean(self, raw_text: str) -> str:
        """
        Refine text using local Ollama Chat API.
        Guaranteed to return raw_text on any failure, timeout, or guardrail rejection.
        Privacy-safe: never logs raw dictation text at INFO/WARN levels.
        """
        raw_clean = raw_text.strip()
        if not self.enabled or not raw_clean:
            return raw_clean

        # Skip utterances under configured word count (zero latency on short phrases)
        words = raw_clean.split()
        if len(words) < self.min_words or len(raw_clean) < self.min_char_length:
            logger.debug("Skipping cleanup for short utterance.")
            return raw_clean

        # Lazy check if Ollama is available
        if not self.is_online():
            logger.info("Ollama offline: falling back to raw transcript (zero latency).")
            return raw_clean

        # Prepare messages with system prompt, protected vocabulary, and worked example
        system_content = self.system_prompt
        if self.vocabulary:
            system_content += (
                "\nThese names and terms are already spelled correctly. Never change, "
                "split or respell them: " + ", ".join(self.vocabulary) + "."
            )

        messages = [{"role": "system", "content": system_content}]
        if self.example_input and self.example_output:
            messages.append({"role": "user", "content": self.example_input.strip()})
            messages.append({"role": "assistant", "content": self.example_output.strip()})
        messages.append({"role": "user", "content": raw_clean})

        for temp in (float(self.temperature), 0.0):
            t_start = time.time()
            try:
                resp = requests.post(
                    self.chat_endpoint,
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "think": False,  # disable reasoning mode for fast cleanup
                        "keep_alive": self.keep_alive,
                        "options": {"temperature": temp}
                    },
                    timeout=self.timeout_seconds
                )
                latency = time.time() - t_start

                if resp.status_code == 200:
                    raw_content = resp.json().get("message", {}).get("content", "").strip()
                    sanitized = sanitize_formatting(raw_content)
                    restored = restore_vocabulary(raw_clean, sanitized, self.vocabulary)
                    ratio = similarity(raw_clean, restored)
                    is_valid, reason = self.validate_cleanup(raw_clean, restored)

                    if is_valid and ratio >= self.min_similarity:
                        para_count = restored.count("\n\n") + 1
                        logger.info(
                            f"Ollama cleanup completed in {latency:.2f}s "
                            f"(chars: {len(raw_clean)} -> {len(restored)}, paragraphs: {para_count}, sim: {ratio:.2f})"
                        )
                        return restored

                    logger.warning(
                        f"Cleanup modified too many words (similarity {ratio:.2f}, reason: {reason}) — retrying."
                    )
                elif resp.status_code == 404:
                    logger.warning(f"Model '{self.model}' not found in Ollama (HTTP 404).")
                    break
            except requests.exceptions.Timeout:
                logger.warning(f"Ollama request timed out after {self.timeout_seconds}s.")
                break
            except Exception as e:
                logger.warning(f"Ollama cleanup error ({type(e).__name__}: {e})")
                break

        logger.info("Cleanup kept rewording; using raw transcript.")
        return raw_clean


# Alias matching reference repository
Cleaner = OllamaCleaner

"""Configuration loading, validation, and atomic persistence for Voxen."""

import logging
import os
import tempfile
from typing import Dict

import yaml

logger = logging.getLogger("Voxen.Config")


def _validate_config(data: Dict) -> Dict:
    """Coerce and clamp config fields to safe types/ranges. Never raises."""
    if not isinstance(data, dict):
        return {}
    stt = data.get("stt")
    if not isinstance(stt, dict):
        stt = {}
        data["stt"] = stt

    # cpu_threads: int, fallback 4, clamp 1..16
    ct = stt.get("cpu_threads", 4)
    if not isinstance(ct, int) or isinstance(ct, bool):
        try:
            ct = int(ct)
        except (TypeError, ValueError):
            ct = 4
    stt["cpu_threads"] = max(1, min(16, ct))

    # beam_size: must be in {1,2,5}, fallback 2
    bs = stt.get("beam_size", 2)
    if bs not in (1, 2, 5):
        try:
            bs = int(bs)
        except (TypeError, ValueError):
            bs = 2
        if bs not in (1, 2, 5):
            bs = 2
    stt["beam_size"] = bs

    # audio section
    audio = data.get("audio")
    if not isinstance(audio, dict):
        audio = {}
        data["audio"] = audio
    if not isinstance(audio.get("target_rms_normalize", 0.05), (int, float)):
        audio["target_rms_normalize"] = 0.05
    if not isinstance(audio.get("silence_threshold_energy", 0.0015), (int, float)):
        audio["silence_threshold_energy"] = 0.0015
    if not isinstance(audio.get("preroll_ms", 500), int):
        try:
            audio["preroll_ms"] = int(audio.get("preroll_ms", 500))
        except (TypeError, ValueError):
            audio["preroll_ms"] = 500

    # llm section
    llm = data.get("llm")
    if not isinstance(llm, dict):
        llm = {}
        data["llm"] = llm
    if "llama_cleanup" in llm and not isinstance(llm["llama_cleanup"], bool):
        llm["llama_cleanup"] = bool(llm["llama_cleanup"])
    if "cleanup_threads" in llm:
        try:
            ct = int(llm["cleanup_threads"])
            llm["cleanup_threads"] = max(1, min(16, ct))
        except (TypeError, ValueError):
            llm["cleanup_threads"] = max(1, (os.cpu_count() or 4) // 2)

    # inject section (clipboard save/paste/restore timing)
    inject = data.get("inject")
    if not isinstance(inject, dict):
        inject = {}
        data["inject"] = inject
    if not isinstance(inject.get("restore_delay_ms", 120), int):
        try:
            inject["restore_delay_ms"] = int(inject.get("restore_delay_ms", 120))
        except (TypeError, ValueError):
            inject["restore_delay_ms"] = 120
    if not isinstance(inject.get("post_paste_delay_ms", 40), int):
        try:
            inject["post_paste_delay_ms"] = int(inject.get("post_paste_delay_ms", 40))
        except (TypeError, ValueError):
            inject["post_paste_delay_ms"] = 40

    return data


def load_config(config_path: str = "config.yaml") -> Dict:
    """Load configuration from a YAML file, returning {} on failure.

    Validates and coerces critical fields after load so malformed config
    never crashes downstream code.
    """
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict):
                    validated = _validate_config(data)
                    return validated
        except Exception as e:
            logger.error(f"Error loading {config_path}: {e}")
    return {}


def build_corrections(stt_cfg: Dict) -> Dict[str, str]:
    """Merge built-in vocabulary corrections with user replacements (case-insensitive)."""
    import re

    from .transcribe import DEFAULT_CORRECTIONS

    merged: Dict[str, str] = dict(DEFAULT_CORRECTIONS)
    replacements = stt_cfg.get("replacements") or stt_cfg.get("corrections") or {}
    if isinstance(replacements, dict):
        for k, v in replacements.items():
            merged[rf"(?i)\b{re.escape(str(k))}\b"] = str(v)
    return merged


def save_config(path: str = "config.yaml", data: Dict = None) -> bool:
    """
    Atomically persist config to *path*.

    Writes to a temp file in the same directory then os.replace(), so a crash
    mid-write never leaves a partially-written config.yaml.
    """
    if data is None:
        data = {}
    abspath = os.path.abspath(path)
    directory = os.path.dirname(abspath) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix="config.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp, abspath)
        tmp = None
        logger.debug(f"Saved config to {abspath}")
        return True
    except Exception as e:
        logger.error(f"Error saving config to {abspath}: {e}")
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except Exception:
                pass

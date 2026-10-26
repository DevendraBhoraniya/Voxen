"""Configuration loading and correction merging for Voxen."""

import logging
import os
from typing import Dict

import yaml

logger = logging.getLogger("Voxen.Config")


def load_config(config_path: str = "config.yaml") -> Dict:
    """Load configuration from a YAML file, returning {} on failure."""
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict):
                    return data
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

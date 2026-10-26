"""
Global keyboard hook module for Voxen.
Handles configurable combinations like Ctrl+Shift, Ctrl+Space in both
toggle and hold-to-talk modes with debounce protection.
"""

import logging
import threading
import time
from typing import Callable, Optional, Set
from pynput import keyboard

logger = logging.getLogger("Voxen.Hotkey")


def normalize_key(key) -> str:
    """Normalize keyboard keys across left/right variants and special keys."""
    if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
        return "ctrl"
    if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
        return "shift"
    if key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr):
        return "alt"
    if key in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r):
        return "cmd"
    if key == keyboard.Key.space:
        return "space"
    if hasattr(key, "char") and key.char:
        return key.char.lower()
    return str(key).lower().replace("key.", "")


class HotkeyListener:
    """Monitors global hotkeys using pynput with toggle and hold modes."""

    def __init__(
        self,
        combination: str = "ctrl+shift",
        mode: str = "toggle",
        debounce_ms: int = 300,
        on_trigger: Optional[Callable[[], None]] = None,
        on_start: Optional[Callable[[], None]] = None,
        on_stop: Optional[Callable[[], None]] = None
    ):
        self.combination_str = combination
        self.mode = mode.lower()  # "toggle" or "hold"
        self.debounce_sec = debounce_ms / 1000.0
        self.on_trigger = on_trigger
        self.on_start = on_start
        self.on_stop = on_stop

        self.required_keys: Set[str] = set()
        self._parse_combination(combination)

        self._active_keys: Set[str] = set()
        self._combo_active = False
        self._last_trigger_time = 0.0
        self._lock = threading.Lock()
        self._listener: Optional[keyboard.Listener] = None

    def _parse_combination(self, combo: str):
        parts = [p.strip().lower() for p in combo.split("+")]
        self.required_keys = set(parts)
        logger.info(f"Configured hotkey: {self.required_keys} (Mode: {self.mode})")

    def set_mode(self, mode: str):
        """Update mode ('toggle' or 'hold')."""
        self.mode = mode.lower()
        logger.info(f"Hotkey mode updated to: {self.mode}")

    def set_combination(self, combo: str):
        """Update hotkey combination."""
        self._parse_combination(combo)

    def _on_press(self, key):
        norm = normalize_key(key)
        with self._lock:
            self._active_keys.add(norm)
            is_matched = self.required_keys.issubset(self._active_keys)

            if is_matched and not self._combo_active:
                now = time.time()
                if (now - self._last_trigger_time) < self.debounce_sec:
                    return

                self._combo_active = True
                self._last_trigger_time = now

                if self.mode == "toggle":
                    logger.debug("Hotkey toggle triggered")
                    if self.on_trigger:
                        threading.Thread(target=self.on_trigger, daemon=True).start()
                elif self.mode == "hold":
                    logger.debug("Hotkey hold-down start triggered")
                    if self.on_start:
                        threading.Thread(target=self.on_start, daemon=True).start()

    def _on_release(self, key):
        norm = normalize_key(key)
        with self._lock:
            self._active_keys.discard(norm)
            is_matched = self.required_keys.issubset(self._active_keys)

            if not is_matched and self._combo_active:
                self._combo_active = False

                if self.mode == "hold":
                    logger.debug("Hotkey hold release stop triggered")
                    if self.on_stop:
                        threading.Thread(target=self.on_stop, daemon=True).start()

    def start(self):
        """Start global keyboard hook."""
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release
        )
        self._listener.daemon = True
        self._listener.start()
        logger.info("Global hotkey listener started.")

    def stop(self):
        """Stop global keyboard hook."""
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
            logger.info("Global hotkey listener stopped.")

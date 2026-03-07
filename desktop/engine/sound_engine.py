from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

try:
    from PySide6.QtMultimedia import QSoundEffect
except Exception:  # pragma: no cover - multimedia backend can be absent in CI
    QSoundEffect = None


class SoundEngine:
    """Best-effort sound notifications with a safe beep fallback."""

    def __init__(self, enabled: bool = True, base_dir: str | Path | None = None):
        self._enabled = bool(enabled)
        root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[1] / "resources" / "sounds"
        self._profile_paths = {
            "order_filled": root / "cash_register.wav",
            "limit_breached": root / "low_alert.wav",
            "connection_lost": root / "thud.wav",
            "connection_failed": root / "thud.wav",
        }
        self._effects: dict[str, object] = {}

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def play(self, event: str) -> None:
        if not self._enabled:
            return
        if QSoundEffect is None:
            app = QApplication.instance()
            if app is not None:
                app.beep()
            return

        path = self._profile_paths.get(event)
        if path is None or not path.exists():
            app = QApplication.instance()
            if app is not None:
                app.beep()
            return

        effect = self._effects.get(event)
        if effect is None:
            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(str(path)))
            effect.setVolume(0.8)
            self._effects[event] = effect
        effect.play()
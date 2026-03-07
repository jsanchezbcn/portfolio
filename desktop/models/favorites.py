from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class FavoriteSymbol:
    symbol: str
    sec_type: str
    exchange: str


class FavoritesStore:
    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path is not None else Path(__file__).resolve().parents[1] / "config" / "favorites.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[FavoriteSymbol]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8") or "[]")
        except (OSError, json.JSONDecodeError):
            return []
        favorites: list[FavoriteSymbol] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            sec_type = str(item.get("sec_type") or "STK").strip().upper()
            exchange = str(item.get("exchange") or "SMART").strip().upper()
            if symbol:
                favorites.append(FavoriteSymbol(symbol=symbol, sec_type=sec_type, exchange=exchange))
        return favorites

    def save(self, favorites: list[FavoriteSymbol]) -> Path:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(fav) for fav in favorites]
        self._path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return self._path

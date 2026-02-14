from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from models.unified_position import UnifiedPosition


class BrokerAdapter(ABC):
    """Abstract contract for broker position and Greeks fetchers."""

    @abstractmethod
    async def fetch_positions(self, account_id: str) -> List[UnifiedPosition]:
        """Fetch and normalize account positions."""

        raise NotImplementedError

    @abstractmethod
    async def fetch_greeks(self, positions: List[UnifiedPosition]) -> List[UnifiedPosition]:
        """Populate or refresh option Greeks for normalized positions."""

        raise NotImplementedError

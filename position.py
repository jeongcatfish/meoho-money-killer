import asyncio
import copy
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class Position:
    market: str
    side: str
    entry_price: float
    amount: float
    tp: float
    sl: float
    status: PositionStatus
    opened_at: float
    order_uuid: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


class PositionManager:
    def __init__(self) -> None:
        self._position: Optional[Position] = None
        self._lock = asyncio.Lock()

    async def get(self) -> Optional[Position]:
        async with self._lock:
            return copy.deepcopy(self._position)

    async def has_open(self) -> bool:
        async with self._lock:
            return self._position is not None and self._position.status == PositionStatus.OPEN

    async def open_position(self, position: Position) -> None:
        async with self._lock:
            if self._position and self._position.status == PositionStatus.OPEN:
                raise RuntimeError("Position already open.")
            self._position = position

    async def close_position(self) -> None:
        async with self._lock:
            if not self._position:
                return
            self._position.status = PositionStatus.CLOSED

    async def replace_with_recovered(
        self, market: str, entry_price: float, amount: float, tp: float, sl: float
    ) -> None:
        async with self._lock:
            self._position = Position(
                market=market,
                side="LONG",
                entry_price=entry_price,
                amount=amount,
                tp=tp,
                sl=sl,
                status=PositionStatus.OPEN,
                opened_at=time.time(),
                order_uuid="RECOVERED",
            )

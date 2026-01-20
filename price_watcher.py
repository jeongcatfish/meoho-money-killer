import asyncio
import logging
import math

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import Settings
from position import PositionManager, PositionStatus
from telemetry import AppTelemetry, error_message
from upbit_client import OrderNotFilledError, UpbitClient


class PriceWatcher:
    def __init__(
        self,
        position_manager: PositionManager,
        upbit_client: UpbitClient,
        settings: Settings,
        telemetry: AppTelemetry | None = None,
    ) -> None:
        self._position_manager = position_manager
        self._upbit_client = upbit_client
        self._settings = settings
        self._telemetry = telemetry
        self._logger = logging.getLogger(__name__)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_price: float | None = None

    @property
    def last_price(self) -> float | None:
        return self._last_price

    async def ensure_running(self) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                return
            self._task = asyncio.create_task(self._run())

    async def _fetch_price(self, market: str) -> float:
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(Exception),
            stop=stop_after_attempt(self._settings.price_retry_attempts),
            wait=wait_exponential(
                multiplier=1,
                min=self._settings.price_retry_wait_min,
                max=self._settings.price_retry_wait_max,
            ),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await asyncio.to_thread(self._upbit_client.get_ticker, market)
        raise RuntimeError("Price retry loop exited unexpectedly.")

    async def _close_position(
        self,
        reason: str,
        market: str,
        amount: float,
        trigger_price: float | None,
        entry_price: float | None = None,
    ) -> None:
        self._logger.info("Trigger %s: closing position %s %.8f", reason, market, amount)
        filled_order = None
        try:
            order = await asyncio.to_thread(self._upbit_client.place_market_sell, market, amount)
            if self._telemetry:
                self._telemetry.record_api_ok()
            order_uuid = order.get("uuid")
            if not order_uuid:
                raise RuntimeError("Missing order uuid for close.")
            filled_order = await asyncio.to_thread(
                self._upbit_client.wait_order_filled, order_uuid
            )
            if self._telemetry:
                self._telemetry.record_api_ok()
            await self._position_manager.close_position()
        except Exception as exc:
            if self._telemetry:
                self._telemetry.record_api_error(error_message(exc))
            raise
        close_price = None
        if filled_order:
            close_price = self._upbit_client.calculate_avg_price(filled_order)
        if (
            close_price is None
            or not math.isfinite(close_price)
            or close_price <= 0
        ) and trigger_price is not None and math.isfinite(trigger_price):
            close_price = trigger_price
        roi = None
        if (
            entry_price is not None
            and math.isfinite(entry_price)
            and entry_price > 0
            and close_price is not None
            and math.isfinite(close_price)
            and close_price > 0
        ):
            roi = close_price / entry_price - 1
        if self._telemetry:
            self._telemetry.add_event(f"Closed {market} {reason}", kind="close", roi=roi)
        self._logger.info("Position closed: %s", reason)

    async def _run(self) -> None:
        while True:
            position = await self._position_manager.get()
            if not position or position.status != PositionStatus.OPEN:
                return
            if position.tp <= 0 or position.sl <= 0:
                self._logger.warning("TP/SL not set; stopping watcher.")
                return

            try:
                price = await self._fetch_price(position.market)
                self._last_price = price
                if self._telemetry:
                    self._telemetry.record_api_ok()
            except Exception as exc:
                self._logger.error("Ticker fetch failed.", exc_info=True)
                if self._telemetry:
                    self._telemetry.record_api_error(error_message(exc))
                await asyncio.sleep(self._settings.price_poll_sec)
                continue

            tp_price = position.entry_price * (1 + position.tp)
            sl_price = position.entry_price * (1 - position.sl)

            if price > tp_price or math.isclose(price, tp_price, rel_tol=1e-6, abs_tol=1e-6):
                try:
                    await self._close_position(
                        "TP",
                        position.market,
                        position.amount,
                        trigger_price=price,
                        entry_price=position.entry_price,
                    )
                except (OrderNotFilledError, Exception) as exc:
                    self._logger.error("Failed to close on TP.", exc_info=True)
                    if self._telemetry:
                        self._telemetry.record_api_error(error_message(exc))
                        self._telemetry.add_event(
                            f"Close failed {position.market} TP", level="error"
                        )
            elif price < sl_price or math.isclose(
                price, sl_price, rel_tol=1e-6, abs_tol=1e-6
            ):
                try:
                    await self._close_position(
                        "SL",
                        position.market,
                        position.amount,
                        trigger_price=price,
                        entry_price=position.entry_price,
                    )
                except (OrderNotFilledError, Exception) as exc:
                    self._logger.error("Failed to close on SL.", exc_info=True)
                    if self._telemetry:
                        self._telemetry.record_api_error(error_message(exc))
                        self._telemetry.add_event(
                            f"Close failed {position.market} SL", level="error"
                        )

            await asyncio.sleep(self._settings.price_poll_sec)

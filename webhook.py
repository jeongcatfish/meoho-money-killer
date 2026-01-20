import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from config import Settings
from position import Position, PositionStatus
from telemetry import error_message
from upbit_client import OrderNotFilledError, UpbitAPIError, UpbitClient

router = APIRouter()
logger = logging.getLogger(__name__)


def _order_state(order: dict) -> str:
    state = order.get("state") or order.get("status") or ""
    return str(state).lower()


async def _fetch_order(upbit_client: UpbitClient, order_uuid: str) -> dict:
    return await asyncio.to_thread(upbit_client.get_order, order_uuid)


async def _safe_cancel_order(upbit_client: UpbitClient, order_uuid: str) -> None:
    try:
        await asyncio.to_thread(upbit_client.cancel_order, order_uuid)
    except Exception:
        logger.warning("Order cancel failed.", exc_info=True)


async def _resolve_filled_order(
    upbit_client: UpbitClient, settings: Settings, order_uuid: str
) -> dict:
    try:
        return await asyncio.to_thread(upbit_client.wait_order_filled, order_uuid)
    except OrderNotFilledError as exc:
        logger.warning("Order fill timeout; re-checking order status (%s).", exc)
    except Exception:
        logger.warning("Order fill check failed; re-checking order status.", exc_info=True)

    order = await _fetch_order(upbit_client, order_uuid)
    state = _order_state(order)
    filled_volume = upbit_client.extract_filled_volume(order)
    if state == "done":
        return order

    if filled_volume > 0:
        await _safe_cancel_order(upbit_client, order_uuid)
        await asyncio.sleep(settings.order_fill_poll_sec)
        return await _fetch_order(upbit_client, order_uuid)

    await _safe_cancel_order(upbit_client, order_uuid)
    await asyncio.sleep(settings.order_fill_poll_sec)
    order = await _fetch_order(upbit_client, order_uuid)
    state = _order_state(order)
    filled_volume = upbit_client.extract_filled_volume(order)
    if state == "done" or filled_volume > 0:
        return order
    raise OrderNotFilledError("Order not filled within timeout.")


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request) -> dict:
    raw_body = await request.body()
    logger.debug("Webhook raw: %s", raw_body.decode(errors="replace"))
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    required_fields = ["market", "action", "signal_id", "tp", "sl", "price"]
    missing = [field for field in required_fields if field not in payload]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {', '.join(missing)}")

    if payload.get("action") != "BUY":
        raise HTTPException(status_code=400, detail="Only BUY action supported")

    try:
        price_krw = float(payload["price"])
        tp = float(payload["tp"])
        sl = float(payload["sl"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid numeric fields")

    if price_krw <= 0 or tp <= 0 or sl <= 0:
        raise HTTPException(status_code=400, detail="price, tp, sl must be positive")

    market = str(payload["market"])
    signal_id = str(payload["signal_id"])

    settings = request.app.state.settings
    signal_guard = request.app.state.signal_guard
    position_manager = request.app.state.position_manager
    upbit_client = request.app.state.upbit_client
    price_watcher = request.app.state.price_watcher
    order_lock = request.app.state.order_lock
    telemetry = request.app.state.telemetry

    telemetry.record_webhook(signal_id)

    if price_krw < settings.min_order_krw:
        raise HTTPException(status_code=400, detail="Price below minimum order size")

    async with order_lock:
        is_new_signal = await signal_guard.register(signal_id)
        if not is_new_signal:
            raise HTTPException(status_code=409, detail="Duplicate signal_id")

        if await position_manager.has_open():
            raise HTTPException(status_code=409, detail="Position already open")

        order_uuid = None
        try:
            order = await asyncio.to_thread(upbit_client.place_market_buy, market, price_krw)
            order_uuid = order.get("uuid")
            if not order_uuid:
                raise RuntimeError("Missing order uuid.")
            filled_order = await _resolve_filled_order(upbit_client, settings, order_uuid)
            entry_price = upbit_client.calculate_avg_price(filled_order)
            filled_volume = upbit_client.extract_filled_volume(filled_order)
            if entry_price <= 0 or filled_volume <= 0:
                logger.warning("Fill data incomplete; reloading order snapshot.")
                filled_order = await _fetch_order(upbit_client, order_uuid)
                entry_price = upbit_client.calculate_avg_price(filled_order)
                filled_volume = upbit_client.extract_filled_volume(filled_order)
            if entry_price <= 0 or filled_volume <= 0:
                raise RuntimeError("Invalid fill data.")
            state = _order_state(filled_order)
            if state != "done":
                logger.warning(
                    "Order not marked done; using executed volume %.8f", filled_volume
                )
            telemetry.record_api_ok()
        except UpbitAPIError as exc:
            logger.error("Order failed.", exc_info=True)
            telemetry.record_api_error(error_message(exc))
            if order_uuid:
                await _safe_cancel_order(upbit_client, order_uuid)
            status_code = exc.status_code
            if status_code >= 500:
                status_code = 502
            detail = exc.user_message()
            telemetry.add_event(f"Order failed {market}: {detail}", level="error")
            raise HTTPException(status_code=status_code, detail=detail)
        except (OrderNotFilledError, Exception) as exc:
            logger.error("Order failed.", exc_info=True)
            telemetry.record_api_error(error_message(exc))
            if order_uuid:
                await _safe_cancel_order(upbit_client, order_uuid)
            telemetry.add_event(f"Order failed {market}", level="error")
            raise HTTPException(status_code=500, detail="Order failed")

        position = Position(
            market=market,
            side="LONG",
            entry_price=entry_price,
            amount=filled_volume,
            tp=tp,
            sl=sl,
            status=PositionStatus.OPEN,
            opened_at=time.time(),
            order_uuid=order_uuid,
        )
        await position_manager.open_position(position)
        logger.info("Position opened at %.8f", entry_price)
        telemetry.add_event(f"Opened {market}", kind="open", roi=0.0)

        await price_watcher.ensure_running()
        return {"status": "ok", "position": position.to_dict()}

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from position import Position, PositionStatus
from upbit_client import OrderNotFilledError, UpbitAPIError

router = APIRouter()
logger = logging.getLogger(__name__)


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
            try:
                filled_order = await asyncio.to_thread(
                    upbit_client.wait_order_filled, order_uuid
                )
            except OrderNotFilledError:
                logger.warning("Order fill timeout; re-checking order status.")
                filled_order = await asyncio.to_thread(upbit_client.get_order, order_uuid)
                state = filled_order.get("state") or filled_order.get("status")
                if state != "done":
                    raise
            entry_price = upbit_client.calculate_avg_price(filled_order)
            filled_volume = upbit_client.extract_filled_volume(filled_order)
            if entry_price <= 0 or filled_volume <= 0:
                raise RuntimeError("Invalid fill data.")
        except UpbitAPIError as exc:
            logger.error("Order failed.", exc_info=True)
            if order_uuid:
                try:
                    await asyncio.to_thread(upbit_client.cancel_order, order_uuid)
                except Exception:
                    logger.error("Order cancel failed.", exc_info=True)
            status_code = exc.status_code
            if status_code >= 500:
                status_code = 502
            detail = exc.user_message()
            raise HTTPException(status_code=status_code, detail=detail)
        except (OrderNotFilledError, Exception):
            logger.error("Order failed.", exc_info=True)
            if order_uuid:
                try:
                    await asyncio.to_thread(upbit_client.cancel_order, order_uuid)
                except Exception:
                    logger.error("Order cancel failed.", exc_info=True)
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

        await price_watcher.ensure_running()
        return {"status": "ok", "position": position.to_dict()}

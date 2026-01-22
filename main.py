import asyncio
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from config import get_settings
from position import PositionManager
from price_watcher import PriceWatcher
from signal_guard import SignalGuard
from telemetry import AppTelemetry, error_message
from upbit_client import UpbitClient
from webhook import router as webhook_router

app = FastAPI()
app.include_router(webhook_router)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.on_event("startup")
async def startup() -> None:
    settings = get_settings()
    _configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    app.state.settings = settings
    app.state.position_manager = PositionManager()
    app.state.signal_guard = SignalGuard(settings.signal_ttl_sec)
    app.state.upbit_client = UpbitClient(settings)
    app.state.telemetry = AppTelemetry(max_events=5)
    app.state.order_lock = asyncio.Lock()
    app.state.price_watcher = PriceWatcher(
        app.state.position_manager,
        app.state.upbit_client,
        settings,
        telemetry=app.state.telemetry,
    )

    await _recover_position(app)
    logger.info("Startup complete.")


async def _recover_position(app: FastAPI) -> None:
    logger = logging.getLogger(__name__)
    settings = app.state.settings
    upbit_client = app.state.upbit_client
    position_manager = app.state.position_manager
    telemetry = app.state.telemetry

    try:
        accounts = await asyncio.to_thread(upbit_client.get_accounts)
        telemetry.record_api_ok()
    except Exception as exc:
        telemetry.record_api_error(error_message(exc))
        raise
    holdings = [
        acct
        for acct in accounts
        if acct.get("currency") != "KRW" and float(acct.get("balance") or 0) > 0
    ]
    if not holdings:
        return

    if settings.recovery_skip:
        logger.warning("Recovery skipped by RECOVERY_SKIP.")
        return

    if settings.recovery_market:
        if "-" not in settings.recovery_market:
            raise RuntimeError("RECOVERY_MARKET must be like KRW-BTC.")
        base, currency = settings.recovery_market.split("-", 1)
        if base != "KRW":
            raise RuntimeError("RECOVERY_MARKET base must be KRW.")
        matched = [acct for acct in holdings if acct.get("currency") == currency]
        if not matched:
            raise RuntimeError("RECOVERY_MARKET not found in holdings.")
        if len(holdings) > 1:
            logger.warning("Additional holdings exist; recovering only %s", currency)
        holding = matched[0]
        market = settings.recovery_market
    else:
        logger.warning("Holdings detected but recovery is disabled; skipping.")
        return

    entry_price = float(holding.get("avg_buy_price") or 0.0)
    amount = float(holding.get("balance") or 0.0)
    if entry_price <= 0:
        logger.warning("avg_buy_price missing; estimating entry price from ticker.")
        try:
            entry_price = await asyncio.to_thread(upbit_client.get_ticker, market)
            telemetry.record_api_ok()
        except Exception as exc:
            telemetry.record_api_error(error_message(exc))
            raise
    await position_manager.replace_with_recovered(
        market, entry_price, amount, settings.recovery_tp, settings.recovery_sl
    )
    logger.warning("Recovered position for %s with avg price %.8f", market, entry_price)
    telemetry.add_event(f"Recovered {market}", level="warn", kind="open")
    if settings.recovery_tp > 0 and settings.recovery_sl > 0:
        await app.state.price_watcher.ensure_running()
    else:
        logger.warning("RECOVERY_TP/RECOVERY_SL not set; watcher not started.")


@app.get("/status")
async def status(request: Request) -> dict:
    position = await request.app.state.position_manager.get()
    price = request.app.state.price_watcher.last_price
    telemetry = request.app.state.telemetry
    return {
        "position": position.to_dict() if position else None,
        "last_price": price,
        "server_time": time.time(),
        "webhook": telemetry.webhook.to_dict(),
        "api": telemetry.api.to_dict(),
        "events": telemetry.get_events(),
    }


@app.get("/account/balances")
async def account_balances(request: Request) -> dict:
    try:
        accounts = await asyncio.to_thread(request.app.state.upbit_client.get_accounts)
        request.app.state.telemetry.record_api_ok()
    except Exception as exc:
        request.app.state.telemetry.record_api_error(error_message(exc))
        logging.getLogger(__name__).error("Account fetch failed.", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch accounts")
    return {"accounts": accounts}


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    from pathlib import Path

    return Path("templates/index.html").read_text(encoding="utf-8")

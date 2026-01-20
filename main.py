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
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trade Execution Monitor</title>
  <style>
    :root {
      --bg-1: #f7f0e2;
      --bg-2: #e3efe8;
      --ink: #12212b;
      --muted: #5b6b73;
      --accent: #ff8a3d;
      --accent-2: #1f8a70;
      --accent-3: #f2c94c;
      --card: rgba(255, 255, 255, 0.92);
      --line: rgba(18, 33, 43, 0.12);
      --shadow: 0 24px 50px rgba(18, 33, 43, 0.18);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Avenir", "Futura", "Trebuchet MS", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #fff3dc 0%, var(--bg-1) 45%),
        radial-gradient(circle at bottom right, #d9f1e7 0%, var(--bg-2) 55%);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 32px;
    }
    body::before,
    body::after {
      content: "";
      position: fixed;
      pointer-events: none;
      z-index: 0;
    }
    body::before {
      top: -140px;
      right: -120px;
      width: 360px;
      height: 360px;
      background: radial-gradient(circle, rgba(255, 138, 61, 0.35), rgba(255, 138, 61, 0));
    }
    body::after {
      bottom: -180px;
      left: -120px;
      width: 420px;
      height: 420px;
      background: radial-gradient(circle, rgba(31, 138, 112, 0.28), rgba(31, 138, 112, 0));
    }
    .app {
      width: min(1100px, 96vw);
      display: grid;
      gap: 24px;
      position: relative;
      z-index: 1;
    }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(220px, 0.6fr);
      gap: 20px;
      align-items: center;
    }
    .hero-copy h1 {
      margin: 8px 0;
      font-size: 32px;
      letter-spacing: 0.3px;
    }
    .hero-copy p {
      margin: 0;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.5;
    }
    .kicker {
      text-transform: uppercase;
      letter-spacing: 3px;
      font-size: 11px;
      color: var(--accent-2);
      font-weight: 700;
    }
    .hero-card {
      background: linear-gradient(140deg, rgba(255, 255, 255, 0.95), rgba(247, 241, 231, 0.92));
      border-radius: 18px;
      border: 1px solid var(--line);
      padding: 18px;
      box-shadow: var(--shadow);
    }
    .hero-value {
      margin-top: 10px;
      font-size: 24px;
      font-weight: 700;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
      min-height: 210px;
    }
    .card::after {
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(120deg, rgba(255, 138, 61, 0.08), transparent 60%);
      opacity: 0;
      transition: opacity 0.3s ease;
      pointer-events: none;
    }
    .card:hover::after { opacity: 1; }
    .spotlight {
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.96), rgba(255, 247, 230, 0.98));
      border-color: rgba(255, 138, 61, 0.3);
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 1.2px;
    }
    .value {
      margin-top: 10px;
      font-size: 22px;
      font-weight: 700;
    }
    .value.large {
      font-size: 34px;
      color: var(--accent-2);
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }
    .metric {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 10px 0;
      border-top: 1px dashed var(--line);
    }
    .metric:first-of-type { border-top: none; }
    .metric-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .metric-value {
      font-weight: 700;
    }
    .metric-value.pos { color: #d04a3a; }
    .metric-value.neg { color: #1f5fa8; }
    .metric-value.zero { color: var(--muted); }
    .mono {
      font-family: "JetBrains Mono", "Menlo", monospace;
      font-size: 12px;
      word-break: break-all;
    }
    .pill {
      padding: 6px 12px;
      border-radius: 999px;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 1px;
      background: rgba(18, 33, 43, 0.08);
      border: 1px solid var(--line);
      color: var(--muted);
      font-weight: 700;
    }
    .pill[data-state="open"] {
      background: rgba(31, 138, 112, 0.15);
      color: #1f6b50;
      border-color: rgba(31, 138, 112, 0.35);
    }
    .pill[data-state="closed"] {
      background: rgba(18, 33, 43, 0.08);
      color: #55626b;
    }
    .pill[data-state="none"] {
      background: rgba(255, 138, 61, 0.12);
      color: #8a4c1d;
      border-color: rgba(255, 138, 61, 0.25);
    }
    .pill[data-state="ok"] {
      background: rgba(31, 138, 112, 0.16);
      color: #1f6b50;
      border-color: rgba(31, 138, 112, 0.35);
    }
    .pill[data-state="warn"] {
      background: rgba(242, 201, 76, 0.2);
      color: #7a5a00;
      border-color: rgba(242, 201, 76, 0.45);
    }
    .pill[data-state="error"] {
      background: rgba(208, 74, 58, 0.14);
      color: #b03a2e;
      border-color: rgba(208, 74, 58, 0.35);
    }
    .metric.stack {
      grid-template-columns: 1fr;
      gap: 8px;
      align-items: start;
    }
    .metric.stack .metric-value {
      text-align: left;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .metric-row {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
    }
    .bar {
      width: 100%;
      height: 8px;
      border-radius: 999px;
      background: rgba(18, 33, 43, 0.08);
      border: 1px solid rgba(18, 33, 43, 0.12);
      overflow: hidden;
    }
    .bar-fill {
      height: 100%;
      width: 0%;
      border-radius: inherit;
      transition: width 0.3s ease;
    }
    .bar[data-tone="tp"] .bar-fill {
      background: linear-gradient(90deg, rgba(31, 138, 112, 0.85), rgba(31, 138, 112, 0.5));
    }
    .bar[data-tone="sl"] .bar-fill {
      background: linear-gradient(90deg, rgba(255, 138, 61, 0.85), rgba(255, 138, 61, 0.5));
    }
    .sparkline {
      margin-top: 12px;
      display: grid;
      gap: 10px;
    }
    .sparkline-item {
      display: grid;
      gap: 6px;
    }
    .sparkline-label {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .sparkline-canvas {
      width: 100%;
      height: 64px;
      border-radius: 12px;
      background: rgba(18, 33, 43, 0.04);
      border: 1px solid rgba(18, 33, 43, 0.08);
    }
    .activity {
      margin-top: 10px;
      display: grid;
      gap: 8px;
    }
    .activity-row {
      display: grid;
      gap: 6px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      border-left-width: 4px;
      border-left-color: transparent;
      background: rgba(18, 33, 43, 0.04);
    }
    .activity-row[data-action="open"] {
      background: rgba(31, 138, 112, 0.08);
      border-color: rgba(31, 138, 112, 0.3);
      border-left-color: rgba(31, 138, 112, 0.7);
    }
    .activity-row[data-action="close"] {
      background: rgba(255, 138, 61, 0.08);
      border-color: rgba(255, 138, 61, 0.3);
      border-left-color: rgba(255, 138, 61, 0.7);
    }
    .activity-row[data-level="error"] {
      background: rgba(208, 74, 58, 0.08);
      border-color: rgba(208, 74, 58, 0.3);
    }
    .activity-row[data-level="warn"] {
      background: rgba(242, 201, 76, 0.16);
      border-color: rgba(242, 201, 76, 0.4);
    }
    .activity-time {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .activity-msg {
      font-weight: 700;
    }
    .activity-empty {
      padding: 14px;
      border-radius: 12px;
      border: 1px dashed var(--line);
      color: var(--muted);
      font-size: 13px;
    }
    button {
      border: none;
      padding: 10px 16px;
      border-radius: 999px;
      background: linear-gradient(120deg, var(--accent), var(--accent-2));
      color: #0b141a;
      font-weight: 700;
      letter-spacing: 0.3px;
      cursor: pointer;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
      box-shadow: 0 12px 24px rgba(255, 138, 61, 0.25);
    }
    button:hover { transform: translateY(-1px); }
    button:active { transform: translateY(0); }
    button:focus-visible {
      outline: 2px solid var(--accent-2);
      outline-offset: 2px;
    }
    .ghost {
      background: rgba(18, 33, 43, 0.06);
      color: var(--ink);
      border: 1px solid var(--line);
      box-shadow: none;
    }
    .accounts {
      margin-top: 12px;
      display: grid;
      gap: 10px;
    }
    .accounts-row {
      padding: 12px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(18, 33, 43, 0.04);
      display: grid;
      gap: 10px;
    }
    .accounts-row.krw {
      background: rgba(242, 201, 76, 0.18);
      border-color: rgba(242, 201, 76, 0.4);
    }
    .accounts-main {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      flex-wrap: wrap;
    }
    .accounts-currency {
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 0.3px;
    }
    .accounts-balance {
      font-size: 16px;
      font-weight: 700;
      color: var(--accent-2);
    }
    .accounts-sub {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 8px;
    }
    .accounts-label {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .accounts-value {
      font-weight: 700;
    }
    .accounts-empty {
      padding: 14px;
      border-radius: 12px;
      border: 1px dashed var(--line);
      color: var(--muted);
      font-size: 13px;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      margin-top: 6px;
    }
    .footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 12px 16px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.7);
    }
    .footer-text {
      color: var(--muted);
      font-size: 13px;
    }
    .hero-card,
    .card,
    .footer {
      opacity: 0;
      transform: translateY(16px);
    }
    body.ready .hero-card,
    body.ready .card,
    body.ready .footer {
      animation: rise 0.6s ease forwards;
    }
    body.ready .grid .card:nth-child(2) { animation-delay: 0.08s; }
    body.ready .grid .card:nth-child(3) { animation-delay: 0.16s; }
    body.ready .footer { animation-delay: 0.22s; }
    @keyframes rise {
      from { opacity: 0; transform: translateY(16px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @media (max-width: 600px) {
      body { padding: 20px; }
      .hero { grid-template-columns: 1fr; }
      .hero-copy h1 { font-size: 26px; }
      .value.large { font-size: 28px; }
      .footer { flex-direction: column; align-items: flex-start; }
    }
    @media (prefers-reduced-motion: reduce) {
      * { animation: none !important; transition: none !important; }
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="hero">
      <div class="hero-copy">
        <div class="kicker">PINE SLAVE</div>
        <h1>Trade Execution Monitor</h1>
        <p>Entry via TradingView signals only. Exits and risk control are managed here.</p>
      </div>
      <div class="hero-card">
        <div class="label">Server</div>
        <div class="hero-value" id="server-time">--</div>
        <div class="meta" id="updated">Waiting for data...</div>
      </div>
    </div>
    <div class="grid">
      <section class="card">
        <div class="card-head">
          <div class="label">Position</div>
          <div class="pill" id="position-status" data-state="none">--</div>
        </div>
        <div class="metric">
          <span class="metric-label">Market</span>
          <span class="metric-value" id="position-market">--</span>
        </div>
        <div class="metric">
          <span class="metric-label">Entry</span>
          <span class="metric-value" id="position-entry">--</span>
        </div>
        <div class="metric">
          <span class="metric-label">Elapsed</span>
          <span class="metric-value" id="position-elapsed">--</span>
        </div>
        <div class="metric">
          <span class="metric-label">TP / SL</span>
          <span class="metric-value" id="position-tp-sl">--</span>
        </div>
        <div class="metric">
          <span class="metric-label">Unrealized PnL (KRW)</span>
          <span class="metric-value" id="position-pnl">--</span>
        </div>
        <div class="metric">
          <span class="metric-label">ROI</span>
          <span class="metric-value" id="position-roi">--</span>
        </div>
        <div class="metric stack">
          <div class="metric-row">
            <span class="metric-label">To TP</span>
            <span class="metric-value" id="distance-tp-label">--</span>
          </div>
          <div class="bar" data-tone="tp">
            <div class="bar-fill" id="distance-tp-bar"></div>
          </div>
        </div>
        <div class="metric stack">
          <div class="metric-row">
            <span class="metric-label">To SL</span>
            <span class="metric-value" id="distance-sl-label">--</span>
          </div>
          <div class="bar" data-tone="sl">
            <div class="bar-fill" id="distance-sl-bar"></div>
          </div>
        </div>
      </section>
      <section class="card spotlight">
        <div class="label">Live Price</div>
        <div class="value large" id="last-price">--</div>
        <div class="metric">
          <span class="metric-label">Amount</span>
          <span class="metric-value" id="position-amount">--</span>
        </div>
        <div class="metric">
          <span class="metric-label">Order UUID</span>
          <span class="metric-value mono" id="position-uuid">--</span>
        </div>
        <div class="sparkline">
          <div class="sparkline-item">
            <div class="sparkline-label">ROI Trend</div>
            <canvas class="sparkline-canvas" id="roi-sparkline"></canvas>
          </div>
          <div class="sparkline-item">
            <div class="sparkline-label">PnL Trend (KRW)</div>
            <canvas class="sparkline-canvas" id="pnl-sparkline"></canvas>
          </div>
        </div>
      </section>
      <section class="card">
        <div class="card-head">
          <div>
            <div class="label">Accounts</div>
            <div class="meta" id="balances-meta">Press refresh to load balances.</div>
          </div>
          <button class="ghost" onclick="fetchBalances()">Refresh</button>
        </div>
        <div class="accounts" id="balances">
          <div class="accounts-empty">No balances loaded yet.</div>
        </div>
      </section>
      <section class="card">
        <div class="card-head">
          <div>
            <div class="label">System</div>
            <div class="meta" id="webhook-meta">Waiting for webhook...</div>
          </div>
          <div class="pill" id="api-status" data-state="none">API --</div>
        </div>
        <div class="metric stack">
          <span class="metric-label">Last Webhook</span>
          <span class="metric-value" id="last-webhook">--</span>
        </div>
        <div class="metric stack">
          <span class="metric-label">Signal ID</span>
          <span class="metric-value mono" id="last-signal-id">--</span>
        </div>
        <div class="metric stack">
          <span class="metric-label">Last API Error</span>
          <span class="metric-value mono" id="last-api-error">--</span>
        </div>
      </section>
      <section class="card">
        <div class="card-head">
          <div>
            <div class="label">Activity</div>
            <div class="meta" id="activity-meta">No events yet.</div>
          </div>
        </div>
        <div class="activity" id="activity-log">
          <div class="activity-empty">No recent fills or closes.</div>
        </div>
      </section>
    </div>
    <div class="footer">
      <div class="label">Notes</div>
      <div class="footer-text">Single position only. Exits are handled by the server.</div>
    </div>
  </div>
  <script>
    function formatNumber(value, digits) {
      if (value === null || value === undefined || value === "") {
        return "--";
      }
      const num = Number(value);
      if (!Number.isFinite(num)) {
        return "--";
      }
      return num.toLocaleString(undefined, {
        maximumFractionDigits: digits,
        minimumFractionDigits: 0
      });
    }

    function formatSignedNumber(value, digits) {
      if (value === null || value === undefined || value === "") {
        return "--";
      }
      const num = Number(value);
      if (!Number.isFinite(num)) {
        return "--";
      }
      const sign = num > 0 ? "+" : "";
      return sign + num.toLocaleString(undefined, {
        maximumFractionDigits: digits,
        minimumFractionDigits: 0
      });
    }

    function formatPercent(value) {
      if (value === null || value === undefined || value === "") {
        return "--";
      }
      const num = Number(value);
      if (!Number.isFinite(num)) {
        return "--";
      }
      return (num * 100).toFixed(2) + "%";
    }

    function formatSignedPercent(value) {
      if (value === null || value === undefined || value === "") {
        return "--";
      }
      const num = Number(value);
      if (!Number.isFinite(num)) {
        return "--";
      }
      const sign = num > 0 ? "+" : "";
      return sign + (num * 100).toFixed(2) + "%";
    }

    function setSignedMetric(elementId, value, formatter, emptyText) {
      const el = document.getElementById(elementId);
      el.classList.remove("pos", "neg", "zero");
      if (!Number.isFinite(value)) {
        el.textContent = emptyText || "--";
        return;
      }
      const cls = value > 0 ? "pos" : value < 0 ? "neg" : "zero";
      el.classList.add(cls);
      el.textContent = formatter(value);
    }

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value));
    }

    function formatDuration(seconds) {
      const total = Number(seconds);
      if (!Number.isFinite(total)) {
        return "--";
      }
      let remaining = Math.max(0, Math.floor(total));
      const days = Math.floor(remaining / 86400);
      remaining -= days * 86400;
      const hours = Math.floor(remaining / 3600);
      remaining -= hours * 3600;
      const mins = Math.floor(remaining / 60);
      remaining -= mins * 60;
      const parts = [];
      if (days) {
        parts.push(days + "d");
      }
      if (hours || days) {
        parts.push(hours + "h");
      }
      if (mins || hours || days) {
        parts.push(mins + "m");
      }
      parts.push(remaining + "s");
      return parts.join(" ");
    }

    function formatTimestamp(timestamp, serverTime) {
      if (timestamp === null || timestamp === undefined || timestamp === "" || timestamp <= 0) {
        return "--";
      }
      const ts = Number(timestamp);
      if (!Number.isFinite(ts)) {
        return "--";
      }
      const clock = new Date(ts * 1000).toLocaleTimeString();
      const base = Number(serverTime);
      if (!Number.isFinite(base)) {
        return clock;
      }
      const age = Math.max(0, base - ts);
      return clock + " (" + formatDuration(age) + " ago)";
    }

    function setBar(barId, labelId, ratio) {
      const bar = document.getElementById(barId);
      const label = document.getElementById(labelId);
      if (!bar || !label) {
        return;
      }
      if (!Number.isFinite(ratio)) {
        bar.style.width = "0%";
        label.textContent = "--";
        return;
      }
      const clamped = clamp(ratio, 0, 1);
      bar.style.width = (clamped * 100).toFixed(0) + "%";
      label.textContent = formatPercent(clamped);
    }

    function updateApiStatus(api, serverTime) {
      const badge = document.getElementById("api-status");
      const errorEl = document.getElementById("last-api-error");
      if (!badge || !errorEl) {
        return;
      }
      if (!api || typeof api !== "object") {
        badge.dataset.state = "none";
        badge.textContent = "API --";
        errorEl.textContent = "--";
        return;
      }
      const lastOk = Number(api.last_ok_at);
      const lastError = Number(api.last_error_at);
      const message = api.last_error_message || "--";
      errorEl.textContent = message;

      let state = "none";
      let label = "API --";
      if (Number.isFinite(lastError) && (!Number.isFinite(lastOk) || lastError >= lastOk)) {
        state = "error";
        label = "API Error";
      } else if (Number.isFinite(lastOk)) {
        const age = Number.isFinite(serverTime) ? serverTime - lastOk : 0;
        if (Number.isFinite(age) && age > 60) {
          state = "warn";
          label = "API Stale";
        } else {
          state = "ok";
          label = "API OK";
        }
      }
      badge.dataset.state = state;
      badge.textContent = label;
    }

    function updateWebhook(webhook, serverTime) {
      const webhookMeta = document.getElementById("webhook-meta");
      const lastWebhook = document.getElementById("last-webhook");
      const lastSignal = document.getElementById("last-signal-id");
      if (!lastWebhook || !lastSignal || !webhookMeta) {
        return;
      }
      if (!webhook || typeof webhook !== "object") {
        lastWebhook.textContent = "--";
        lastSignal.textContent = "--";
        webhookMeta.textContent = "Waiting for webhook...";
        return;
      }
      lastWebhook.textContent = formatTimestamp(webhook.last_received_at, serverTime);
      lastSignal.textContent = webhook.last_signal_id || "--";
      if (Number.isFinite(webhook.last_received_at) && Number.isFinite(serverTime)) {
        const age = serverTime - webhook.last_received_at;
        webhookMeta.textContent = "Last signal " + formatDuration(age) + " ago";
      } else {
        webhookMeta.textContent = "Waiting for webhook...";
      }
    }

    function renderEvents(events, serverTime) {
      const container = document.getElementById("activity-log");
      const meta = document.getElementById("activity-meta");
      if (!container || !meta) {
        return;
      }
      clearNode(container);
      if (!Array.isArray(events) || events.length === 0) {
        const empty = document.createElement("div");
        empty.className = "activity-empty";
        empty.textContent = "No recent fills or closes.";
        container.appendChild(empty);
        meta.textContent = "No events yet.";
        return;
      }
      const list = events.slice().reverse();
      meta.textContent = list.length + " recent events · " + new Date().toLocaleTimeString();
      list.forEach((event) => {
        const row = document.createElement("div");
        row.className = "activity-row";
        row.dataset.level = event.level || "info";
        if (typeof event.kind === "string" && event.kind) {
          row.dataset.action = event.kind.toLowerCase();
        }
        const timeEl = document.createElement("div");
        timeEl.className = "activity-time";
        timeEl.textContent = formatTimestamp(event.ts, serverTime);
        const msgEl = document.createElement("div");
        msgEl.className = "activity-msg";
        let message = event.message || "--";
        const roiValue = Number(event.roi);
        if (Number.isFinite(roiValue)) {
          message += " · ROI " + formatSignedPercent(roiValue);
        }
        msgEl.textContent = message;
        row.appendChild(timeEl);
        row.appendChild(msgEl);
        container.appendChild(row);
      });
    }

    function prepareCanvas(canvas) {
      const ratio = window.devicePixelRatio || 1;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (!width || !height) {
        return null;
      }
      const targetWidth = Math.floor(width * ratio);
      const targetHeight = Math.floor(height * ratio);
      if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
        canvas.width = targetWidth;
        canvas.height = targetHeight;
      }
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      return { ctx, width, height };
    }

    function drawSparkline(canvasId, data, color) {
      const canvas = document.getElementById(canvasId);
      if (!canvas) {
        return;
      }
      const prepared = prepareCanvas(canvas);
      if (!prepared) {
        return;
      }
      const ctx = prepared.ctx;
      const width = prepared.width;
      const height = prepared.height;
      ctx.clearRect(0, 0, width, height);
      ctx.lineWidth = 1;
      ctx.strokeStyle = "rgba(18, 33, 43, 0.12)";
      ctx.beginPath();
      ctx.moveTo(0, height / 2);
      ctx.lineTo(width, height / 2);
      ctx.stroke();

      if (!Array.isArray(data) || data.length < 2) {
        return;
      }
      const min = Math.min(...data);
      const max = Math.max(...data);
      const range = max - min || 1;
      if (min <= 0 && max >= 0) {
        const zeroY = height - ((0 - min) / range) * height;
        ctx.strokeStyle = "rgba(18, 33, 43, 0.2)";
        ctx.beginPath();
        ctx.moveTo(0, zeroY);
        ctx.lineTo(width, zeroY);
        ctx.stroke();
      }

      ctx.lineWidth = 2;
      ctx.strokeStyle = color;
      ctx.beginPath();
      data.forEach((value, index) => {
        const x = (index / (data.length - 1)) * width;
        const y = height - ((value - min) / range) * height;
        if (index === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();
    }

    const MAX_HISTORY = 120;
    let lastPositionKey = null;
    const roiHistory = [];
    const pnlHistory = [];

    function syncHistory(positionKey) {
      if (positionKey === lastPositionKey) {
        return;
      }
      roiHistory.length = 0;
      pnlHistory.length = 0;
      lastPositionKey = positionKey;
    }

    function pushHistory(list, value) {
      if (!Number.isFinite(value)) {
        return;
      }
      list.push(value);
      if (list.length > MAX_HISTORY) {
        list.shift();
      }
    }

    function updateSparklines() {
      drawSparkline("roi-sparkline", roiHistory, "#1f8a70");
      drawSparkline("pnl-sparkline", pnlHistory, "#ff8a3d");
    }

    function setStatus(state, label) {
      const statusEl = document.getElementById("position-status");
      statusEl.dataset.state = state;
      statusEl.textContent = label;
    }

    function clearNode(node) {
      while (node.firstChild) {
        node.removeChild(node.firstChild);
      }
    }

    function buildBalanceMeta(label, value, digits) {
      const wrap = document.createElement("div");
      const metaLabel = document.createElement("div");
      metaLabel.className = "accounts-label";
      metaLabel.textContent = label;
      const metaValue = document.createElement("div");
      metaValue.className = "accounts-value mono";
      metaValue.textContent = formatNumber(value, digits);
      wrap.appendChild(metaLabel);
      wrap.appendChild(metaValue);
      return wrap;
    }

    const hiddenCurrencies = new Set(["BTT", "APENFT"]);

    function renderBalances(accounts) {
      const container = document.getElementById("balances");
      clearNode(container);

      if (!Array.isArray(accounts) || accounts.length === 0) {
        const empty = document.createElement("div");
        empty.className = "accounts-empty";
        empty.textContent = "No balances returned.";
        container.appendChild(empty);
        document.getElementById("balances-meta").textContent = "No assets";
        return;
      }

      const visibleAccounts = accounts.filter(
        (account) => !hiddenCurrencies.has(account.currency)
      );

      document.getElementById("balances-meta").textContent =
        visibleAccounts.length + " assets · " + new Date().toLocaleTimeString();

      if (visibleAccounts.length === 0) {
        const empty = document.createElement("div");
        empty.className = "accounts-empty";
        empty.textContent = "No visible balances.";
        container.appendChild(empty);
        return;
      }

      visibleAccounts.forEach((account) => {
        const row = document.createElement("div");
        row.className = "accounts-row";
        if (account.currency === "KRW") {
          row.classList.add("krw");
        }

        const balanceDigits = account.currency === "KRW" ? 2 : 8;

        const main = document.createElement("div");
        main.className = "accounts-main";

        const currency = document.createElement("div");
        currency.className = "accounts-currency";
        currency.textContent = account.currency || "--";

        const balance = document.createElement("div");
        balance.className = "accounts-balance";
        balance.textContent = formatNumber(account.balance, balanceDigits);

        main.appendChild(currency);
        main.appendChild(balance);

        const sub = document.createElement("div");
        sub.className = "accounts-sub";
        sub.appendChild(buildBalanceMeta("Locked", account.locked, balanceDigits));
        sub.appendChild(buildBalanceMeta("Avg Buy", account.avg_buy_price, 8));

        row.appendChild(main);
        row.appendChild(sub);
        container.appendChild(row);
      });
    }

    async function fetchStatus() {
      try {
        const res = await fetch("/status");
        if (!res.ok) {
          throw new Error("Status fetch failed");
        }
        const data = await res.json();
        const position = data.position;
        const serverTime = Number(data.server_time);
        document.getElementById("updated").textContent =
          "Updated " + new Date().toLocaleTimeString();
        document.getElementById("server-time").textContent =
          Number.isFinite(serverTime) ? new Date(serverTime * 1000).toLocaleTimeString() : "--";
        document.getElementById("last-price").textContent = formatNumber(data.last_price, 4);
        updateWebhook(data.webhook, serverTime);
        updateApiStatus(data.api, serverTime);
        renderEvents(data.events, serverTime);

        if (!position) {
          setStatus("none", "No Position");
          document.getElementById("position-market").textContent = "--";
          document.getElementById("position-entry").textContent = "--";
          document.getElementById("position-elapsed").textContent = "--";
          document.getElementById("position-tp-sl").textContent = "--";
          document.getElementById("position-amount").textContent = "--";
          document.getElementById("position-uuid").textContent = "--";
          setSignedMetric("position-pnl", Number.NaN, formatSignedNumber);
          setSignedMetric("position-roi", Number.NaN, formatSignedPercent);
          setBar("distance-tp-bar", "distance-tp-label", Number.NaN);
          setBar("distance-sl-bar", "distance-sl-label", Number.NaN);
          syncHistory(null);
          updateSparklines();
          return;
        }

        const state = (position.status || "OPEN").toLowerCase();
        const isOpen = state === "open";
        setStatus(state, position.status || "OPEN");
        document.getElementById("position-market").textContent = position.market;
        document.getElementById("position-entry").textContent =
          formatNumber(position.entry_price, 6);
        document.getElementById("position-tp-sl").textContent =
          formatPercent(position.tp) + " / " + formatPercent(position.sl);
        document.getElementById("position-amount").textContent =
          formatNumber(position.amount, 8);
        document.getElementById("position-uuid").textContent = position.order_uuid;

        const entryPrice = Number(position.entry_price);
        const amount = Number(position.amount);
        const lastPrice = Number(data.last_price);
        let pnlValue = Number.NaN;
        let roiValue = Number.NaN;
        if (
          isOpen &&
          Number.isFinite(entryPrice) &&
          entryPrice > 0 &&
          Number.isFinite(amount) &&
          Number.isFinite(lastPrice) &&
          lastPrice > 0
        ) {
          pnlValue = (lastPrice - entryPrice) * amount;
          roiValue = lastPrice / entryPrice - 1;
        }
        setSignedMetric("position-pnl", pnlValue, (val) => formatSignedNumber(val, 2));
        setSignedMetric("position-roi", roiValue, formatSignedPercent);

        const now = Number.isFinite(serverTime) ? serverTime : Date.now() / 1000;
        if (isOpen && Number.isFinite(position.opened_at)) {
          document.getElementById("position-elapsed").textContent =
            formatDuration(now - position.opened_at);
        } else {
          document.getElementById("position-elapsed").textContent = "--";
        }

        let tpRemaining = Number.NaN;
        let slRemaining = Number.NaN;
        const tpValue = Number(position.tp);
        const slValue = Number(position.sl);
        if (
          isOpen &&
          Number.isFinite(entryPrice) &&
          entryPrice > 0 &&
          Number.isFinite(lastPrice) &&
          lastPrice > 0 &&
          Number.isFinite(tpValue) &&
          Number.isFinite(slValue) &&
          tpValue > 0 &&
          slValue > 0
        ) {
          const tpPrice = entryPrice * (1 + tpValue);
          const slPrice = entryPrice * (1 - slValue);
          const tpRange = tpPrice - entryPrice;
          const slRange = entryPrice - slPrice;
          if (tpRange > 0) {
            tpRemaining = (tpPrice - lastPrice) / tpRange;
          }
          if (slRange > 0) {
            slRemaining = (lastPrice - slPrice) / slRange;
          }
        }
        setBar("distance-tp-bar", "distance-tp-label", tpRemaining);
        setBar("distance-sl-bar", "distance-sl-label", slRemaining);

        const positionKey = isOpen
          ? String(position.order_uuid || "--") + ":" + String(position.opened_at || "--")
          : null;
        syncHistory(positionKey);
        if (isOpen) {
          pushHistory(pnlHistory, pnlValue);
          pushHistory(roiHistory, roiValue);
        }
        updateSparklines();
      } catch (err) {
        document.getElementById("updated").textContent = "Status unavailable";
      } finally {
        document.body.classList.add("ready");
      }
    }

    async function fetchBalances() {
      try {
        const res = await fetch("/account/balances");
        if (!res.ok) {
          throw new Error("Balances fetch failed");
        }
        const data = await res.json();
        renderBalances(data.accounts);
      } catch (err) {
        renderBalances([]);
        document.getElementById("balances-meta").textContent = "Unavailable";
      }
    }

    fetchStatus();
    setInterval(fetchStatus, 500);
  </script>
</body>
</html>
    """

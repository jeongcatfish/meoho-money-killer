import asyncio
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from config import get_settings
from position import PositionManager
from price_watcher import PriceWatcher
from signal_guard import SignalGuard
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
    app.state.order_lock = asyncio.Lock()
    app.state.price_watcher = PriceWatcher(
        app.state.position_manager, app.state.upbit_client, settings
    )

    await _recover_position(app)
    logger.info("Startup complete.")


async def _recover_position(app: FastAPI) -> None:
    logger = logging.getLogger(__name__)
    settings = app.state.settings
    upbit_client = app.state.upbit_client
    position_manager = app.state.position_manager

    accounts = await asyncio.to_thread(upbit_client.get_accounts)
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
        entry_price = await asyncio.to_thread(upbit_client.get_ticker, market)
    await position_manager.replace_with_recovered(
        market, entry_price, amount, settings.recovery_tp, settings.recovery_sl
    )
    logger.warning("Recovered position for %s with avg price %.8f", market, entry_price)
    if settings.recovery_tp > 0 and settings.recovery_sl > 0:
        await app.state.price_watcher.ensure_running()
    else:
        logger.warning("RECOVERY_TP/RECOVERY_SL not set; watcher not started.")


@app.get("/status")
async def status(request: Request) -> dict:
    position = await request.app.state.position_manager.get()
    price = request.app.state.price_watcher.last_price
    return {
        "position": position.to_dict() if position else None,
        "last_price": price,
        "server_time": time.time(),
    }


@app.get("/account/balances")
async def account_balances(request: Request) -> dict:
    try:
        accounts = await asyncio.to_thread(request.app.state.upbit_client.get_accounts)
    except Exception:
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
          <span class="metric-label">TP / SL</span>
          <span class="metric-value" id="position-tp-sl">--</span>
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
    </div>
    <div class="footer">
      <div class="label">Notes</div>
      <div class="footer-text">Single position only. Exits are handled by the server.</div>
    </div>
  </div>
  <script>
    function formatNumber(value, digits) {
      const num = Number(value);
      if (!Number.isFinite(num)) {
        return "--";
      }
      return num.toLocaleString(undefined, {
        maximumFractionDigits: digits,
        minimumFractionDigits: 0
      });
    }

    function formatPercent(value) {
      const num = Number(value);
      if (!Number.isFinite(num)) {
        return "--";
      }
      return (num * 100).toFixed(2) + "%";
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

      document.getElementById("balances-meta").textContent =
        accounts.length + " assets · " + new Date().toLocaleTimeString();

      accounts.forEach((account) => {
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
        document.getElementById("updated").textContent =
          "Updated " + new Date().toLocaleTimeString();
        document.getElementById("server-time").textContent =
          data.server_time ? new Date(data.server_time * 1000).toLocaleTimeString() : "--";
        document.getElementById("last-price").textContent = formatNumber(data.last_price, 4);

        if (!position) {
          setStatus("none", "No Position");
          document.getElementById("position-market").textContent = "--";
          document.getElementById("position-entry").textContent = "--";
          document.getElementById("position-tp-sl").textContent = "--";
          document.getElementById("position-amount").textContent = "--";
          document.getElementById("position-uuid").textContent = "--";
          return;
        }

        const state = (position.status || "OPEN").toLowerCase();
        setStatus(state, position.status || "OPEN");
        document.getElementById("position-market").textContent = position.market;
        document.getElementById("position-entry").textContent =
          formatNumber(position.entry_price, 6);
        document.getElementById("position-tp-sl").textContent =
          formatPercent(position.tp) + " / " + formatPercent(position.sl);
        document.getElementById("position-amount").textContent =
          formatNumber(position.amount, 8);
        document.getElementById("position-uuid").textContent = position.order_uuid;
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
    setInterval(fetchStatus, 2000);
  </script>
</body>
</html>
    """

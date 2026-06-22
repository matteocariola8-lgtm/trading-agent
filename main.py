import os
import threading
import time
from collections import deque
from datetime import datetime

import pytz
import requests
from flask import Flask, jsonify, send_file, request as flask_request

import agent
import daily_report

app = Flask(__name__)

# Rolling intraday equity history (up to 240 snapshots ≈ 2 hours at 30s refresh)
_equity_history: deque = deque(maxlen=240)


@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "dashboard.html"))


@app.route("/api/chart")
def api_chart():
    symbol = flask_request.args.get("symbol", "QQQ")
    bars = []
    try:
        r = requests.get(
            f"{agent.ALPACA_DATA_URL}/stocks/bars",
            headers=agent.alpaca_headers,
            params={"symbols": symbol, "timeframe": "1Min", "limit": 120, "feed": "iex"},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json().get("bars", {}).get(symbol, [])
        bars = [{
            "t": b["t"][11:16],
            "o": round(b["o"], 2),
            "h": round(b["h"], 2),
            "l": round(b["l"], 2),
            "c": round(b["c"], 2),
            "v": b["v"],
        } for b in raw]
    except Exception:
        pass
    return jsonify({"symbol": symbol, "bars": bars})


@app.route("/api/data")
def api_data():
    account, positions = {}, []
    try:
        r = requests.get(f"{agent.ALPACA_BASE_URL}/account",
                         headers=agent.alpaca_headers, timeout=10)
        r.raise_for_status()
        account = r.json()
    except Exception:
        pass

    try:
        r = requests.get(f"{agent.ALPACA_BASE_URL}/positions",
                         headers=agent.alpaca_headers, timeout=10)
        r.raise_for_status()
        positions = r.json()
    except Exception:
        pass

    equity = account.get("equity")
    if equity is not None:
        _equity_history.append({
            "t": datetime.now().strftime("%H:%M:%S"),
            "v": float(equity),
        })

    mem = agent.load_memory()

    today = datetime.now().date().isoformat()
    today_midnight = today + "T00:00:00Z"

    mem_trades_today = [
        t for t in mem.get("trades", [])
        if t.get("date", "")[:10] == today
    ]
    scalp_trades_today = [t for t in mem_trades_today if t.get("tag") == "SCALP"]
    last_scalp_time = scalp_trades_today[-1].get("date") if scalp_trades_today else None

    # Filled orders from Alpaca today
    orders_today = []
    try:
        r = requests.get(
            f"{agent.ALPACA_BASE_URL}/orders",
            headers=agent.alpaca_headers,
            params={"status": "filled", "after": today_midnight, "limit": 50},
            timeout=10,
        )
        r.raise_for_status()
        raw_orders = r.json()
        orders_today = [{
            "id":         o.get("id"),
            "symbol":     o.get("symbol"),
            "side":       o.get("side"),
            "qty":        o.get("filled_qty"),
            "price":      o.get("filled_avg_price"),
            "type":       o.get("order_class", "simple"),
            "filled_at":  (o.get("filled_at") or "")[:19].replace("T", " "),
        } for o in raw_orders if o.get("filled_at")]
    except Exception:
        pass

    return jsonify({
        "equity":             equity,
        "last_equity":        account.get("last_equity"),
        "cash":               account.get("cash"),
        "positions":          positions,
        "trades":             daily_report.get_todays_trades(),
        "mem_trades_today":   mem_trades_today,
        "orders_today":       orders_today,
        "market_open":        agent.is_market_open(),
        "updated_at":         datetime.now().isoformat(),
        "signals":            mem.get("last_signals", {}),
        "decisions":          mem.get("last_decisions", {}),
        "regime":             mem.get("last_regime", {}),
        "scalp_active":       mem.get("last_scalp_active", False),
        "scalp_trades_today": len(scalp_trades_today),
        "scalp_max_daily":    agent.SCALP_MAX_DAILY_TRADES,
        "scalp_target_pct":   agent.SCALP_TARGET_PCT,
        "scalp_stop_pct":     agent.SCALP_STOP_PCT,
        "last_scalp_time":    last_scalp_time,
        "last_cycle_at":      mem.get("last_cycle_at"),
        "loop_interval":      agent.LOOP_INTERVAL,
        "equity_history":     list(_equity_history),
    })


def _trading_loop():
    while True:
        try:
            agent.run_cycle()
        except Exception as e:
            agent.log.error(f"Trading loop error: {e}")
        time.sleep(agent.LOOP_INTERVAL)


def _scheduler_loop():
    et = pytz.timezone("America/New_York")
    sent_today = None
    while True:
        now   = datetime.now(et)
        today = now.date()
        if now.hour == 18 and now.minute == 0 and sent_today != today:
            try:
                daily_report.send_report()
                sent_today = today
                agent.log.info("Daily report sent.")
            except Exception as e:
                agent.log.error(f"Daily report failed: {e}")
        time.sleep(30)


if __name__ == "__main__":
    threading.Thread(target=_trading_loop, daemon=True).start()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    agent.log.info(f"Server starting on port {port}")
    app.run(host="0.0.0.0", port=port)

import os
import threading
import time
from collections import deque
from datetime import datetime

import pytz
import requests
from flask import Flask, jsonify, send_file

import agent
import daily_report

app = Flask(__name__)

# Rolling intraday equity history (up to 240 snapshots ≈ 2 hours at 30s refresh)
_equity_history: deque = deque(maxlen=240)


@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "dashboard.html"))


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
            "t": datetime.now().strftime("%H:%M"),
            "v": float(equity),
        })

    mem = agent.load_memory()

    today = datetime.now().date().isoformat()
    scalp_trades_today = sum(
        1 for t in mem.get("trades", [])
        if t.get("tag") == "SCALP" and t.get("date", "")[:10] == today
    )

    return jsonify({
        "equity":             equity,
        "last_equity":        account.get("last_equity"),
        "cash":               account.get("cash"),
        "positions":          positions,
        "trades":             daily_report.get_todays_trades(),
        "market_open":        agent.is_market_open(),
        "updated_at":         datetime.now().isoformat(),
        "signals":            mem.get("last_signals", {}),
        "decisions":          mem.get("last_decisions", {}),
        "regime":             mem.get("last_regime", {}),
        "scalp_active":       mem.get("last_scalp_active", False),
        "scalp_trades_today": scalp_trades_today,
        "last_cycle_at":      mem.get("last_cycle_at"),
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

"""
main.py — Server Flask + API per la dashboard del trading agent.

Espone endpoint REST che leggono memory.json (scritto da agent.py) e
servono dashboard.html. Non esegue trading — è puro layer di
presentazione/API, così agent.py e main.py possono girare come processi
separati su Railway (uno col loop di trading, uno col server web) o
insieme in locale per test.

Endpoint:
  GET /                    → serve dashboard.html
  GET /api/status          → stato generale: regime, ultimo ciclo, universo
  GET /api/positions       → posizioni aperte + P&L
  GET /api/signals         → tutti i segnali dell'ultimo ciclo (universo intero)
  GET /api/shortlist       → solo la shortlist mandata a Claude nell'ultimo ciclo
  GET /api/decisions       → ultime decisioni AI (BUY/SELL/HOLD + motivazione)
  GET /api/equity_history  → serie storica equity (derivata dai trade risolti)
  GET /api/orders_today    → conteggio ordini piazzati oggi (normali + scalp)
  GET /api/kelly           → parametri Kelly correnti per ETF
"""

import json
import os
from datetime import date, datetime

from flask import Flask, jsonify, send_from_directory

MEMORY_FILE = os.environ.get("MEMORY_FILE", "memory.json")
STATIC_DIR  = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)


def load_memory() -> dict:
    try:
        with open(MEMORY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "dashboard.html")


@app.route("/api/status")
def api_status():
    mem = load_memory()
    positions = mem.get("last_positions", {})
    cash      = mem.get("last_cash", 0)
    signals   = mem.get("last_signals", {})

    market_value = 0.0
    for sym, qty in positions.items():
        price = (signals.get(sym) or {}).get("price") or 0
        market_value += price * qty

    return jsonify({
        "regime":         mem.get("last_regime", {}),
        "universe_size":  mem.get("universe_size", 0),
        "scalp_active":   mem.get("last_scalp_active", False),
        "last_cycle_at":  mem.get("last_cycle_at"),
        "cash":           cash,
        "market_value":   round(market_value, 2),
        "total_equity":   round(cash + market_value, 2),
        "correlations":   mem.get("last_correlations", {}),
    })


@app.route("/api/positions")
def api_positions():
    mem = load_memory()
    positions = mem.get("last_positions", {})
    signals   = mem.get("last_signals", {})
    trades    = mem.get("trades", [])

    out = []
    for sym, qty in positions.items():
        s = signals.get(sym, {})
        price = s.get("price")

        entry_price = None
        for t in reversed(trades):
            if t.get("symbol") == sym and t.get("side") == "BUY" and not t.get("resolved"):
                entry_price = t.get("entry_price")
                break

        pnl = pnl_pct = None
        if price and entry_price:
            pnl     = round((price - entry_price) * qty, 2)
            pnl_pct = round((price - entry_price) / entry_price * 100, 2)

        out.append({
            "symbol": sym, "name": s.get("name", sym), "category": s.get("category"),
            "leverage": s.get("leverage", 1), "qty": qty, "price": price,
            "entry_price": entry_price, "pnl": pnl, "pnl_pct": pnl_pct,
        })
    return jsonify(out)


@app.route("/api/signals")
def api_signals():
    mem = load_memory()
    signals = mem.get("last_signals", {})
    out = [{"symbol": sym, **data} for sym, data in signals.items() if "error" not in data]
    out.sort(key=lambda x: x.get("mom_rank") or 9999)
    return jsonify(out)


@app.route("/api/shortlist")
def api_shortlist():
    mem = load_memory()
    shortlist = mem.get("last_shortlist", [])
    signals   = mem.get("last_signals", {})
    decisions = mem.get("last_decisions", {})
    out = []
    for sym in shortlist:
        s = signals.get(sym, {})
        d = decisions.get(sym, {})
        out.append({
            "symbol": sym, "name": s.get("name", sym), "category": s.get("category"),
            "leverage": s.get("leverage", 1), "price": s.get("price"),
            "mom_rank": s.get("mom_rank"), "vol_confirms": s.get("vol_confirms"),
            "rsi": s.get("rsi"), "action": d.get("action", "HOLD"),
            "qty": d.get("qty", 0), "reason": d.get("reason", ""),
        })
    return jsonify(out)


@app.route("/api/decisions")
def api_decisions():
    mem = load_memory()
    decisions = mem.get("last_decisions", {})
    signals   = mem.get("last_signals", {})
    out = []
    for sym, d in decisions.items():
        s = signals.get(sym, {})
        out.append({
            "symbol": sym, "name": s.get("name", sym), "leverage": s.get("leverage", 1),
            "action": d.get("action", "HOLD"), "qty": d.get("qty", 0),
            "reason": d.get("reason", ""),
        })
    return jsonify(out)


@app.route("/api/equity_history")
def api_equity_history():
    """Ricostruisce una serie storica approssimata di equity dai trade
    risolti (pnl cumulato). Non è un vero time-series di conto (richiederebbe
    snapshot giornalieri separati), ma dà un andamento indicativo."""
    mem = load_memory()
    trades = [t for t in mem.get("trades", []) if t.get("resolved")]
    trades.sort(key=lambda t: t.get("date", ""))

    cumulative = 0.0
    series = []
    for t in trades:
        pnl_pct = t.get("pnl_pct", 0) or 0
        entry   = t.get("entry_price", 0) or 0
        qty     = t.get("qty", 0) or 0
        pnl_abs = pnl_pct * entry * qty
        cumulative += pnl_abs
        series.append({
            "date": t.get("date", "")[:10],
            "symbol": t.get("symbol"),
            "cumulative_pnl": round(cumulative, 2),
        })
    return jsonify(series)


@app.route("/api/trade_log")
def api_trade_log():
    """Storico completo operazioni, diviso per stato:
    - aperte: posizioni equity attualmente detenute (da last_positions)
    - in_essere: trade non ancora risolti (resolved=False) — aperti ma
      non ancora valutati (risoluzione a 5 giorni di mercato, vedi
      resolve_trades() in agent.py). Include anche gli scalp non chiusi.
    - chiuse: trade risolti, con esito e P&L finale
    Le opzioni hanno la loro sezione separata (vedi /api/options_positions)."""
    mem = load_memory()
    trades = mem.get("trades", [])
    positions = mem.get("last_positions", {})
    signals = mem.get("last_signals", {})

    aperte = []
    for sym, qty in positions.items():
        s = signals.get(sym, {})
        price = s.get("price")
        entry_price = None
        entry_date = None
        # Prima scelta: trade BUY non ancora risolto (caso normale)
        for t in reversed(trades):
            if t.get("symbol") == sym and t.get("side") == "BUY" and not t.get("resolved"):
                entry_price = t.get("entry_price")
                entry_date = t.get("date", "")[:10]
                break
        # Fallback: il ciclo di risoluzione a 5 giorni può marcare un trade
        # come "resolved" prima che la posizione fisica venga chiusa — in
        # quel caso non c'è nessun BUY non risolto da trovare, ma la
        # posizione esiste comunque. Usiamo il BUY più recente per quel
        # simbolo, risolto o no, pur di mostrare un entry price plausibile
        # invece di lasciarlo vuoto.
        if entry_price is None:
            for t in reversed(trades):
                if t.get("symbol") == sym and t.get("side") == "BUY":
                    entry_price = t.get("entry_price")
                    entry_date = t.get("date", "")[:10]
                    break
        pnl_pct = None
        if price and entry_price:
            pnl_pct = round((price - entry_price) / entry_price * 100, 2)
        aperte.append({
            "symbol": sym, "name": s.get("name", sym), "leverage": s.get("leverage", 1),
            "qty": qty, "entry_price": entry_price, "entry_date": entry_date,
            "current_price": price, "pnl_pct": pnl_pct,
        })

    in_essere = []
    for t in trades:
        if not t.get("resolved"):
            sym = t.get("symbol")
            s = signals.get(sym, {})
            in_essere.append({
                "symbol": sym, "name": s.get("name", sym), "side": t.get("side"),
                "qty": t.get("qty"), "entry_price": t.get("entry_price"),
                "date": t.get("date", "")[:10], "regime": t.get("regime"),
                "tag": t.get("tag", "NORMALE"), "reason": t.get("reason", ""),
            })
    in_essere.sort(key=lambda x: x.get("date", ""), reverse=True)

    chiuse = []
    for t in trades:
        if t.get("resolved"):
            chiuse.append({
                "symbol": t.get("symbol"), "side": t.get("side"), "qty": t.get("qty"),
                "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"),
                "pnl_pct": t.get("pnl_pct"), "correct": t.get("correct"),
                "date": t.get("date", "")[:10], "tag": t.get("tag", "NORMALE"),
            })
    chiuse.sort(key=lambda x: x.get("date", ""), reverse=True)

    return jsonify({
        "aperte": aperte,
        "in_essere": in_essere[:50],   # cap: evita risposte enormi con universo ampio
        "chiuse": chiuse[:50],
        "riepilogo": {
            "totale_chiuse": len(chiuse),
            "vinte": sum(1 for t in chiuse if t.get("correct")),
            "perse": sum(1 for t in chiuse if t.get("correct") is False),
        }
    })


@app.route("/api/orders_today")
def api_orders_today():
    mem = load_memory()
    today = date.today().isoformat()
    trades_today = [t for t in mem.get("trades", []) if t.get("date", "")[:10] == today]
    normal = [t for t in trades_today if t.get("tag") != "SCALP"]
    scalp  = [t for t in trades_today if t.get("tag") == "SCALP"]
    return jsonify({
        "total": len(trades_today), "normal": len(normal), "scalp": len(scalp),
        "scalp_max_daily": 3,
    })


@app.route("/api/kelly")
def api_kelly():
    mem = load_memory()
    kelly = mem.get("kelly", {})
    signals = mem.get("last_signals", {})
    out = []
    for sym, kp in kelly.items():
        s = signals.get(sym, {})
        out.append({
            "symbol": sym, "name": s.get("name", sym), "leverage": s.get("leverage", 1),
            "win_rate": kp.get("win_rate"), "avg_win": kp.get("avg_win"),
            "avg_loss": kp.get("avg_loss"),
        })
    out.sort(key=lambda x: x.get("win_rate") or 0, reverse=True)
    return jsonify(out)


@app.route("/api/options_positions")
def api_options_positions():
    """Posizioni opzioni aperte (richiede l'integrazione di options_engine.py
    in agent.py — finché non è collegato, mem['options_trades'] è vuoto e
    questo endpoint ritorna [] senza errori."""
    mem = load_memory()
    trades = [t for t in mem.get("options_trades", []) if not t.get("closed")]
    return jsonify(trades)


@app.route("/api/options_risk_summary")
def api_options_risk_summary():
    mem = load_memory()
    trades = [t for t in mem.get("options_trades", []) if not t.get("closed")]
    total_risk = sum(t.get("max_risk", 0) for t in trades)
    cash = mem.get("last_cash", 0)
    signals = mem.get("last_signals", {})
    positions = mem.get("last_positions", {})
    market_value = sum((signals.get(s) or {}).get("price", 0) * q
                       for s, q in positions.items())
    equity = cash + market_value
    cap_pct = 15
    return jsonify({
        "total_max_risk": round(total_risk, 2),
        "cap_pct": cap_pct,
        "cap_value": round(equity * cap_pct / 100, 2),
        "positions_count": len(trades),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)

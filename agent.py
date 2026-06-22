"""
Quantitative ETF Trading Agent

Academic basis:
  Momentum   — Jegadeesh & Titman (1993); Asness et al. (2013) "Value and Momentum Everywhere"
  Regime     — Hamilton (1989) HMM-inspired; volatility-clustering (Mandelbrot 1963)
  Volume     — Granville OBV (1963); Blume, Easley & O'Hara (1994)
  Sizing     — Kelly (1956); half-Kelly via MacLean, Thorp & Ziemba (2010)
  Risk-on/off— Ilmanen (2011) cross-asset correlation regimes

Strategy flow each cycle:
  1. Fetch 300 days of OHLCV for all ETFs (Alpaca data API)
  2. Compute: realized vol, ADX, momentum (12M–1M Jegadeesh-Titman),
              OBV slope, RSI, ATR, rolling cross-asset correlations
  3. Classify market regime (TRENDING_BULL / TRENDING_BEAR / HIGH_VOL / LATERAL)
  4. Resolve prior trade outcomes → update per-ETF half-Kelly parameters
  5. Package everything into a structured prompt → send to Claude
  6. Execute Claude's decisions; log + persist to memory.json
"""

import json
import logging
import os
import smtplib
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import requests
from dotenv import load_dotenv
import anthropic

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL")        # paper trading: orders/positions/account
ALPACA_DATA_URL   = "https://data.alpaca.markets/v2"    # market data: bars / quotes
FINNHUB_API_KEY   = os.getenv("FINNHUB_API_KEY")

EMAIL_SENDER      = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD    = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER    = "matteo.cariola8@gmail.com"

ETFS              = ["SPY", "QQQ", "GLD", "TLT", "UUP"]
LOOP_INTERVAL     = 300          # seconds between cycles (5 min is enough for daily-bar strategy)
MEMORY_FILE       = "memory.json"
BARS_LIMIT        = 310          # fetch > 252 to cover 12-month momentum
MAX_SHARES        = 5            # hard cap per trade; half-Kelly may produce fewer
MIN_ADX_FOR_RSI   = 25           # RSI valid as directional signal only above this

# Scalping mode
SCALP_SYMBOLS          = ["SPY", "QQQ"]
SCALP_TIMEFRAME        = "5Min"
SCALP_BARS             = 100              # ~8 hours of 5-min data; ≥ RSI(14)+lookback
SCALP_RSI_OVERSOLD     = 35
SCALP_RSI_OVERBOUGHT   = 65
SCALP_TARGET_PCT       = 0.004           # 0.4% take-profit
SCALP_STOP_PCT         = 0.002           # 0.2% stop-loss
SCALP_SIZE_PCT         = 0.10            # 10% of portfolio equity per trade
SCALP_MAX_DAILY_TRADES = 3

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("trades.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── HTTP clients ──────────────────────────────────────────────────────────────

alpaca_headers = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "accept":              "application/json",
    "content-type":        "application/json",
}
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ══════════════════════════════════════════════════════════════════════════════
# DATA LAYER
# ══════════════════════════════════════════════════════════════════════════════

def get_historical_bars(symbols: list[str], limit: int = BARS_LIMIT) -> dict[str, dict]:
    """Returns {sym: {c, h, l, v arrays}} sorted oldest→newest."""
    from datetime import timedelta
    start_date = (datetime.utcnow() - timedelta(days=500)).strftime("%Y-%m-%dT00:00:00Z")
    raw_bars: dict[str, list] = {s: [] for s in symbols}

    params = {
        "symbols":    ",".join(symbols),
        "timeframe":  "1Day",
        "start":      start_date,
        "limit":      1000,          # per-page cap; we paginate if needed
        "adjustment": "split",
        "feed":       "iex",
    }
    log.info(f"Fetching bars: symbols={symbols} start={start_date}")

    try:
        page = 0
        while True:
            r = requests.get(f"{ALPACA_DATA_URL}/stocks/bars",
                             headers=alpaca_headers, params=params, timeout=20)
            log.info(f"Bars API status={r.status_code} url={r.url}")
            if not r.ok:
                log.error(f"Bars API HTTP {r.status_code}: {r.text[:400]}")
                break
            payload = r.json()
            page += 1

            # Log raw structure on first page so we can diagnose format issues
            if page == 1:
                keys = list(payload.keys())
                sample_sym = next(iter(payload.get("bars", {})), None)
                sample_len = len(payload["bars"].get(sample_sym, [])) if sample_sym else 0
                log.info(f"Bars response keys={keys} sample_sym={sample_sym} "
                         f"sample_bars_count={sample_len}")

            for sym, bars_list in payload.get("bars", {}).items():
                raw_bars.setdefault(sym, []).extend(bars_list)

            next_token = payload.get("next_page_token")
            if not next_token:
                break
            params["page_token"] = next_token
            log.info(f"Paginating bars, page={page+1}")

        out = {}
        for sym, bars_list in raw_bars.items():
            # Keep only the most recent `limit` bars
            bars_list = bars_list[-limit:]
            if len(bars_list) < 30:
                log.warning(f"{sym}: only {len(bars_list)} bars received — signals will be NaN")
                continue
            log.info(f"{sym}: {len(bars_list)} bars loaded "
                     f"({bars_list[0]['t']} → {bars_list[-1]['t']})")
            out[sym] = {
                "c": np.array([b["c"] for b in bars_list]),
                "h": np.array([b["h"] for b in bars_list]),
                "l": np.array([b["l"] for b in bars_list]),
                "v": np.array([b["v"] for b in bars_list]),
                "t": [b["t"] for b in bars_list],
            }
        return out
    except Exception as e:
        log.error(f"Error fetching bars: {e}", exc_info=True)
        return {}


def get_current_quotes() -> dict[str, float | None]:
    syms = ",".join(ETFS)
    try:
        r = requests.get(f"{ALPACA_DATA_URL}/stocks/quotes/latest",
                         headers=alpaca_headers,
                         params={"symbols": syms, "feed": "iex"}, timeout=10)
        r.raise_for_status()
        return {s: (q.get("ap") or q.get("bp"))
                for s, q in r.json().get("quotes", {}).items()}
    except Exception as e:
        log.error(f"Error fetching quotes: {e}")
        return {s: None for s in ETFS}


def get_positions() -> dict[str, float]:
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/positions", headers=alpaca_headers, timeout=10)
        r.raise_for_status()
        return {p["symbol"]: float(p["qty"]) for p in r.json()}
    except Exception as e:
        log.error(f"Error fetching positions: {e}")
        return {}


def get_account_cash() -> float:
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/account", headers=alpaca_headers, timeout=10)
        r.raise_for_status()
        return float(r.json().get("cash", 0))
    except Exception as e:
        log.error(f"Error fetching account: {e}")
        return 0.0


def get_market_news() -> list[dict]:
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_API_KEY}",
            timeout=10)
        r.raise_for_status()
        return [{"headline": i.get("headline", ""), "summary": i.get("summary", "")}
                for i in r.json()[:8]]
    except Exception as e:
        log.error(f"Error fetching news: {e}")
        return []


def is_market_open() -> bool:
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/clock", headers=alpaca_headers, timeout=10)
        r.raise_for_status()
        return r.json().get("is_open", False)
    except Exception as e:
        log.error(f"Error checking market clock: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def realized_vol(closes: np.ndarray, window: int = 20) -> float:
    """Annualized realized volatility of log returns."""
    if len(closes) < window + 1:
        return float("nan")
    lr = np.diff(np.log(closes[-(window + 1):]))
    return float(np.std(lr, ddof=1) * np.sqrt(252))


def momentum(closes: np.ndarray) -> dict:
    """
    Jegadeesh-Titman (1993): 12-month return excluding last month.
    Skip-month avoids microstructure reversal contaminating the signal.
    Combined score = (mom_12m - mom_1m) / vol  — vol-adjusted cross-sectional rank input.
    """
    r = {"mom_12m": float("nan"), "mom_1m": float("nan"), "combined": float("nan")}
    if len(closes) >= 252:
        r["mom_12m"] = float(closes[-22] / closes[-252] - 1)
    if len(closes) >= 22:
        r["mom_1m"] = float(closes[-1] / closes[-22] - 1)
    if not any(np.isnan([r["mom_12m"], r["mom_1m"]])):
        vol = realized_vol(closes, 20)
        vol = vol if (vol and not np.isnan(vol)) else 0.15
        r["combined"] = (r["mom_12m"] - r["mom_1m"]) / max(vol, 0.01)
    return r


def _wilder_smooth(arr: np.ndarray, period: int) -> np.ndarray:
    s = float(np.sum(arr[:period]))
    out = [s]
    for x in arr[period:]:
        s = s - s / period + float(x)
        out.append(s)
    return np.array(out)


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """Wilder ADX (1978). > 25 = trending; < 20 = lateral."""
    if len(close) < period * 2 + 2:
        return float("nan")
    tr     = np.maximum(high[1:] - low[1:],
             np.maximum(np.abs(high[1:] - close[:-1]),
                        np.abs(low[1:]  - close[:-1])))
    up     = high[1:] - high[:-1]
    down   = low[:-1] - low[1:]
    pdm    = np.where((up > down) & (up > 0), up, 0.0)
    ndm    = np.where((down > up) & (down > 0), down, 0.0)
    atr_s  = _wilder_smooth(tr,  period)
    pdm_s  = _wilder_smooth(pdm, period)
    ndm_s  = _wilder_smooth(ndm, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = np.where(atr_s > 0, 100 * pdm_s / atr_s, 0.0)
        ndi = np.where(atr_s > 0, 100 * ndm_s / atr_s, 0.0)
        dx  = np.where((pdi + ndi) > 0, 100 * np.abs(pdi - ndi) / (pdi + ndi), 0.0)
    return float(_wilder_smooth(dx, period)[-1])


def obv_signal(closes: np.ndarray, volumes: np.ndarray) -> dict:
    """
    On-Balance Volume (Granville 1963).
    Blume et al. (1994): volume informativeness is highest when price trend and
    OBV agree — volume-confirmed breakout is more reliable than unconfirmed.
    slope > 0.02 = volume confirming uptrend; < -0.02 = confirming downtrend.
    """
    if len(closes) < 11:
        return {"slope": 0.0, "confirms": False}
    signs = np.sign(np.diff(closes))
    obv   = np.concatenate([[0.0], np.cumsum(signs * volumes[1:])])
    raw_slope = (obv[-1] - obv[-10]) / 9
    norm      = np.mean(np.abs(obv[-20:])) or 1.0
    slope     = float(raw_slope / norm)
    mom_1m    = closes[-1] / closes[-22] - 1 if len(closes) >= 22 else 0
    confirms  = (mom_1m > 0 and slope > 0.02) or (mom_1m < 0 and slope < -0.02)
    return {"slope": round(slope, 4), "confirms": confirms}


def rsi(closes: np.ndarray, period: int = 14) -> float:
    """
    RSI used as directional momentum — NOT mean-reversion.
    RSI > 55 in trending market = momentum continuation (valid only if ADX > 25).
    """
    if len(closes) < period + 1:
        return float("nan")
    d     = np.diff(closes[-(period * 2):])
    gains = np.where(d > 0, d, 0.0)
    loss  = np.where(d < 0, -d, 0.0)
    ag    = np.mean(gains[-period:])
    al    = np.mean(loss[-period:])
    if al == 0:
        return 100.0
    return float(100 - 100 / (1 + ag / al))


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """ATR used only for volatility regime calibration — no fixed stops."""
    if len(close) < period + 1:
        return float("nan")
    tr = np.maximum(high[1:] - low[1:],
         np.maximum(np.abs(high[1:] - close[:-1]),
                    np.abs(low[1:]  - close[:-1])))
    return float(np.mean(tr[-period:]))


def rolling_correlations(bars: dict, window: int = 60) -> dict:
    """
    60-day rolling pairwise return correlations.
    Regime interpretation (Ilmanen 2011):
      SPY-GLD < -0.1 = risk-on (stocks vs haven); > +0.1 = risk-off (flight to safety)
      SPY-TLT < -0.2 = risk-on;  > 0  = risk-off
      SPY-QQQ > 0.85 = broad equity participation (healthy risk-on)
      GLD-TLT both positive with SPY negative = strong risk-off
    """
    rets, min_len = {}, None
    for sym, d in bars.items():
        if len(d["c"]) >= window + 1:
            r = np.diff(np.log(d["c"][-(window + 1):]))
            rets[sym] = r
            min_len = len(r) if min_len is None else min(min_len, len(r))
    corr = {}
    for a, b in [("SPY","QQQ"),("SPY","GLD"),("SPY","TLT"),("GLD","TLT"),("SPY","UUP")]:
        if a in rets and b in rets:
            ra, rb = rets[a][-min_len:], rets[b][-min_len:]
            corr[f"{a}_{b}"] = round(float(np.corrcoef(ra, rb)[0, 1]), 3)
        else:
            corr[f"{a}_{b}"] = float("nan")
    return corr


# ══════════════════════════════════════════════════════════════════════════════
# REGIME DETECTION  (Hamilton 1989 HMM-inspired, simplified)
# ══════════════════════════════════════════════════════════════════════════════

def detect_regime(bars: dict, corr: dict) -> dict:
    """
    2-step classification:
      1. Volatility state: is realized vol in the top 30% of its own 1-year distribution?
         → HIGH_VOL → reduce all sizing to 25%
      2. Trend state via ADX:
         ADX < 20                → LATERAL  (sizing 50%)
         ADX ≥ 20 + risk-off    → TRENDING_BEAR (favor GLD/TLT)
         ADX ≥ 20 + risk-on     → TRENDING_BULL (favor SPY/QQQ)
    """
    spy = bars.get("SPY", {})
    if not spy or len(spy["c"]) < 60:
        return {"regime": "UNKNOWN", "adx_val": float("nan"),
                "rv": float("nan"), "rv_pctile": float("nan")}

    rv_now = realized_vol(spy["c"], 20)

    # Rolling percentile: sample every 5 bars to keep it fast
    samples = [realized_vol(spy["c"][:i], 20)
               for i in range(41, len(spy["c"]) + 1, 5)]
    samples = [v for v in samples if not np.isnan(v)]
    rv_pctile = float(np.mean(np.array(samples) < rv_now)) if samples else 0.5

    adx_val  = adx(spy["h"], spy["l"], spy["c"])
    mom_spy  = momentum(spy["c"])
    spy_gld  = corr.get("SPY_GLD", 0.0) or 0.0
    spy_tlt  = corr.get("SPY_TLT", 0.0) or 0.0

    base = {"adx_val": round(adx_val, 1) if not np.isnan(adx_val) else None,
            "rv": round(rv_now, 3), "rv_pctile": round(rv_pctile, 2)}

    if rv_pctile > 0.70:
        return {**base, "regime": "HIGH_VOL",      "sizing_mult": 0.25}
    if not np.isnan(adx_val) and adx_val < 20:
        return {**base, "regime": "LATERAL",        "sizing_mult": 0.50}
    # Risk-off: GLD/TLT moving WITH market OR SPY in short-term downtrend
    risk_off = (spy_gld > 0.15 or spy_tlt > 0.05) and (mom_spy["mom_1m"] or 0) < -0.01
    if risk_off:
        return {**base, "regime": "TRENDING_BEAR",  "sizing_mult": 0.75}
    return     {**base, "regime": "TRENDING_BULL",  "sizing_mult": 1.00}


# ══════════════════════════════════════════════════════════════════════════════
# FACTOR SIGNALS
# ══════════════════════════════════════════════════════════════════════════════

def generate_signals(bars: dict, regime: dict) -> dict:
    """Compute all factor scores per ETF. Claude does the final weighting."""
    adx_spy = regime.get("adx_val") or float("nan")

    # Cross-sectional momentum rank (lower rank number = stronger momentum)
    combined_scores = {}
    for sym in ETFS:
        if sym in bars:
            m = momentum(bars[sym]["c"])
            combined_scores[sym] = m["combined"]
    ranked = sorted(
        [s for s in combined_scores if not np.isnan(combined_scores[s])],
        key=lambda s: combined_scores[s], reverse=True
    )

    signals = {}
    for sym in ETFS:
        d = bars.get(sym, {})
        if not d or len(d["c"]) < 30:
            signals[sym] = {"error": "insufficient_data"}
            continue

        mom  = momentum(d["c"])
        obv  = obv_signal(d["c"], d["v"])
        rv   = realized_vol(d["c"], 20)
        rsi_ = rsi(d["c"])
        atr_ = atr(d["h"], d["l"], d["c"])

        rsi_dir = None
        if not np.isnan(adx_spy) and adx_spy > MIN_ADX_FOR_RSI:
            if rsi_ > 55:
                rsi_dir = "bullish_continuation"
            elif rsi_ < 45:
                rsi_dir = "bearish_continuation"

        def fmt(x): return round(x, 4) if x is not None and not np.isnan(x) else None

        signals[sym] = {
            "price":        fmt(d["c"][-1]),
            "mom_12m":      fmt(mom["mom_12m"]),
            "mom_1m":       fmt(mom["mom_1m"]),
            "mom_combined": fmt(mom["combined"]),
            "mom_rank":     ranked.index(sym) + 1 if sym in ranked else None,
            "obv_slope":    obv["slope"],
            "vol_confirms": obv["confirms"],
            "rsi":          fmt(rsi_),
            "rsi_signal":   rsi_dir,
            "realized_vol": fmt(rv),
            "atr":          fmt(atr_),
        }
    return signals


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY & ADAPTIVE LEARNING
# ══════════════════════════════════════════════════════════════════════════════

def load_memory() -> dict:
    try:
        with open(MEMORY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "trades":  [],
            "outcomes": {s: [] for s in ETFS},
            "kelly":    {s: {"win_rate": 0.5, "avg_win": 0.012, "avg_loss": 0.012}
                         for s in ETFS},
        }


def save_memory(mem: dict):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2, default=str)


def resolve_trades(mem: dict, bars: dict) -> dict:
    """
    5 trading days after a signal: was the direction right?
    Updates per-symbol win_rate, avg_win, avg_loss for Kelly sizing.
    """
    updated = []
    for t in mem.get("trades", []):
        if t.get("resolved"):
            updated.append(t)
            continue
        try:
            days = (date.today() - date.fromisoformat(t["date"][:10])).days
        except Exception:
            t["resolved"] = True
            updated.append(t)
            continue
        if days < 5:
            updated.append(t)
            continue
        sym = t.get("symbol")
        ep  = t.get("entry_price", 0)
        if not sym or not ep or sym not in bars:
            t["resolved"] = True
            updated.append(t)
            continue
        cur     = float(bars[sym]["c"][-1])
        pnl_pct = (cur - ep) / ep * (1 if t["side"] == "BUY" else -1)
        correct = pnl_pct > 0
        t.update({"exit_price": cur, "pnl_pct": round(pnl_pct, 4),
                  "correct": correct, "resolved": True})

        outcomes = mem["outcomes"].setdefault(sym, [])
        outcomes.append({"correct": correct, "pnl": pnl_pct,
                         "regime": t.get("regime", "?")})
        mem["outcomes"][sym] = outcomes[-50:]  # keep last 50

        # Recompute Kelly parameters from most recent 30 resolved trades
        recent = mem["outcomes"][sym][-30:]
        wins   = [o["pnl"] for o in recent if o["correct"]]
        losses = [abs(o["pnl"]) for o in recent if not o["correct"]]
        mem["kelly"][sym] = {
            "win_rate": round(len(wins) / len(recent), 3),
            "avg_win":  round(float(np.mean(wins))  if wins   else 0.012, 4),
            "avg_loss": round(float(np.mean(losses)) if losses else 0.012, 4),
        }
        updated.append(t)

    mem["trades"] = updated[-200:]  # cap log at 200 trades
    return mem


def half_kelly(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Kelly (1956): f* = (p*b - q) / b  where b = avg_win/avg_loss, q = 1-p
    Half-Kelly (MacLean et al. 2010): halve for parameter uncertainty,
    smoother wealth path, lower drawdown risk.
    Capped at 20% of capital per position.
    """
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    b = avg_win / avg_loss
    f = (win_rate * b - (1 - win_rate)) / b
    return max(0.0, min(f / 2, 0.20))


def compute_sizing(mem: dict, signals: dict, cash: float, regime: dict) -> dict[str, int]:
    """Share count per ETF: Kelly fraction × regime multiplier × cash, floored to int."""
    mult = regime.get("sizing_mult", 1.0)
    out  = {}
    for sym in ETFS:
        kp    = mem["kelly"].get(sym, {"win_rate": 0.5, "avg_win": 0.012, "avg_loss": 0.012})
        frac  = half_kelly(kp["win_rate"], kp["avg_win"], kp["avg_loss"]) * mult
        price = (signals.get(sym) or {}).get("price") or 0
        if price <= 0:
            out[sym] = 0
            continue
        out[sym] = min(int(cash * frac / price), MAX_SHARES)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

def build_prompt(signals, regime, corr, positions, cash, news, sizing, mem) -> str:
    reg  = regime["regime"]
    mult = regime.get("sizing_mult", 1.0)

    corr_lines = "\n".join(
        f"  {k}: {v:+.2f}" for k, v in corr.items() if not np.isnan(v)
    )

    sig_lines = []
    for sym in ETFS:
        s  = signals.get(sym, {})
        kp = mem["kelly"].get(sym, {})
        outcomes = mem["outcomes"].get(sym, [])
        n_wins   = sum(1 for o in outcomes if o.get("correct"))
        sig_lines.append(
            f"  {sym} | price=${s.get('price','?')}"
            f" | mom_rank=#{s.get('mom_rank','?')}"
            f" mom_12m={s.get('mom_12m','?')} mom_1m={s.get('mom_1m','?')} combined={s.get('mom_combined','?')}"
            f" | OBV_slope={s.get('obv_slope','?')} vol_confirms={s.get('vol_confirms','?')}"
            f" | RSI={s.get('rsi','?')} rsi_signal={s.get('rsi_signal') or 'inactive'}"
            f" | realized_vol={s.get('realized_vol','?')} ATR={s.get('atr','?')}"
            f" | kelly: wr={kp.get('win_rate',0.5):.0%} avgW={kp.get('avg_win',0):.2%} avgL={kp.get('avg_loss',0):.2%}"
            f" | suggested_shares={sizing.get(sym,0)}"
            f" | history={n_wins}/{len(outcomes)} correct"
        )

    pos_text  = "\n".join(f"  {s}: {q} shares" for s, q in positions.items()) or "  None"
    news_text = "\n".join(f"  - {n['headline']}" for n in news) or "  No news."

    return f"""You are a quantitative ETF trading agent. Use the multi-factor analysis below to make a disciplined, factor-driven decision for each ETF.

═══ ACCOUNT ════════════════════════════════════════════
Cash: ${cash:.2f}
Positions:
{pos_text}

═══ MARKET REGIME ══════════════════════════════════════
Regime:       {reg}
Sizing mult:  {mult:.0%} of Kelly (HIGH_VOL=25%, LATERAL=50%, TRENDING=75-100%)
ADX (SPY):    {regime.get('adx_val', 'N/A')}   [<20=lateral, 20-25=weak, >25=strong trend]
Realized vol: {regime.get('rv', 'N/A')}         annualized (SPY 20D)
Vol pctile:   {regime.get('rv_pctile', 'N/A')}  (>0.70 triggered HIGH_VOL)

═══ CROSS-ASSET CORRELATIONS (60D) ═════════════════════
{corr_lines}
Interpretation:
  SPY_GLD < -0.1 = risk-on;  > +0.15 = flight-to-safety (risk-off)
  SPY_TLT < -0.2 = risk-on;  > 0     = bonds bid up (risk-off)
  SPY_QQQ > 0.85 = broad equity participation (healthy)

═══ FACTOR SIGNALS PER ETF ═════════════════════════════
mom_rank #1 = strongest Jegadeesh-Titman momentum (12M skip-1M, vol-adjusted).
OBV slope: volume trend direction. vol_confirms=True required for high conviction.
RSI: directional continuation signal, ONLY valid when ADX > {MIN_ADX_FOR_RSI} (inactive otherwise).
suggested_shares: half-Kelly allocation already adjusted for regime sizing mult.

{chr(10).join(sig_lines)}

═══ NEWS (tie-breaker only) ════════════════════════════
{news_text}

═══ DECISION FRAMEWORK ════════════════════════════════
1. HIGH_VOL or LATERAL regime → prefer HOLD; only act on rank #1-2 with vol_confirms=True.
2. TRENDING_BULL → BUY top 2-3 momentum ETFs when vol_confirms=True; SELL bottom-ranked ETFs you hold.
3. TRENDING_BEAR / risk-off correlations → favor GLD and TLT; reduce SPY and QQQ.
4. Never exceed suggested_shares. HOLD has qty=0. BUY qty must fit in available cash.
5. SELL qty must not exceed current position size.
6. Provide a concise, factor-specific reason (cite: regime, momentum rank, OBV, RSI if active).

Respond ONLY with this JSON (no markdown, no extra text):
{{
  "SPY": {{"action": "BUY"|"SELL"|"HOLD", "qty": <int>, "reason": "<factor-based reason>"}},
  "QQQ": {{"action": "BUY"|"SELL"|"HOLD", "qty": <int>, "reason": "<factor-based reason>"}},
  "GLD": {{"action": "BUY"|"SELL"|"HOLD", "qty": <int>, "reason": "<factor-based reason>"}},
  "TLT": {{"action": "BUY"|"SELL"|"HOLD", "qty": <int>, "reason": "<factor-based reason>"}},
  "UUP": {{"action": "BUY"|"SELL"|"HOLD", "qty": <int>, "reason": "<factor-based reason>"}}
}}"""


def ask_claude(prompt: str) -> dict[str, dict]:
    raw = ""
    try:
        msg = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=768,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Claude returned invalid JSON: {e}\nRaw: {raw[:300]}")
        return {s: {"action": "HOLD", "qty": 0, "reason": "parse error"} for s in ETFS}
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return {s: {"action": "HOLD", "qty": 0, "reason": "api error"} for s in ETFS}


# ══════════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def place_order(symbol: str, action: str, qty: int) -> bool:
    if qty <= 0:
        return False
    try:
        r = requests.post(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers,
            json={"symbol": symbol, "qty": str(qty), "side": action.lower(),
                  "type": "market", "time_in_force": "day"},
            timeout=10,
        )
        r.raise_for_status()
        log.info(f"ORDER PLACED | {action} {qty} {symbol} | order_id={r.json().get('id')}")
        return True
    except requests.HTTPError as e:
        log.error(f"Order failed {action} {qty} {symbol}: {e} | {r.text}")
        return False
    except Exception as e:
        log.error(f"Order error {action} {qty} {symbol}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _build_pnl_block(positions: dict, signals: dict, mem: dict, cash: float) -> str:
    """Returns a plain-text P&L summary for the email body."""
    lines = []
    total_market = 0.0
    for sym, qty_pos in positions.items():
        current_price = (signals.get(sym) or {}).get("price")
        if not current_price:
            lines.append(f"  {sym}: {qty_pos} sh  (price unavailable)")
            continue
        market_val = current_price * qty_pos
        total_market += market_val
        # Find most recent unresolved BUY for this symbol to compute unrealized P&L
        entry_price = None
        for t in reversed(mem.get("trades", [])):
            if t.get("symbol") == sym and t.get("side") == "BUY" and not t.get("resolved"):
                entry_price = t.get("entry_price")
                break
        if entry_price and entry_price > 0:
            pnl      = (current_price - entry_price) * qty_pos
            pnl_pct  = (current_price - entry_price) / entry_price * 100
            sign     = "+" if pnl >= 0 else ""
            lines.append(
                f"  {sym}: {qty_pos} sh  @${current_price:.2f}"
                f"  (entry ${entry_price:.2f})"
                f"  P&L {sign}${pnl:.2f} ({sign}{pnl_pct:.2f}%)"
            )
        else:
            lines.append(f"  {sym}: {qty_pos} sh  @${current_price:.2f}  (entry unknown)")
    total_portfolio = cash + total_market
    lines.append(f"\n  Cash:            ${cash:>12.2f}")
    lines.append(f"  Market value:    ${total_market:>12.2f}")
    lines.append(f"  Total portfolio: ${total_portfolio:>12.2f}")
    return "\n".join(lines) if lines else "  No open positions."


def send_trade_email(
    symbol: str,
    action: str,
    qty: int,
    price: float,
    reason: str,
    cash: float,
    positions: dict,
    signals: dict,
    mem: dict,
) -> None:
    """Send a BUY/SELL notification to EMAIL_RECEIVER via Gmail SMTP."""
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        log.warning("EMAIL_SENDER/EMAIL_PASSWORD not set — skipping trade email.")
        return

    timestamp    = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    trade_value  = price * qty
    pnl_block    = _build_pnl_block(positions, signals, mem, cash)

    subject = f"[Trading Agent] {action} {qty}x {symbol} @ ${price:.2f}"
    body = f"""
Trading Agent — Order Notification
====================================
Timestamp  : {timestamp}
Ticker     : {symbol}
Action     : {action}
Shares     : {qty}
Price      : ${price:.2f}
Trade value: ${trade_value:.2f}

AI Motivation:
  {reason}

Portfolio P&L (post-order):
{pnl_block}

─────────────────────────────────────
Automated notification — ETF Trading Agent (paper trading)
""".strip()

    msg = MIMEMultipart()
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        log.info(f"Trade email sent: {action} {qty} {symbol}")
    except Exception as e:
        log.error(f"Failed to send trade email: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SCALPING MODE  (intraday, 5-min bars, HIGH_VOL + no vol_confirms)
# ══════════════════════════════════════════════════════════════════════════════

def get_intraday_bars(symbols: list[str]) -> dict[str, dict]:
    """Returns {sym: {c, h, l, v, t}} of recent 5-min bars, oldest→newest."""
    params = {
        "symbols":   ",".join(symbols),
        "timeframe": SCALP_TIMEFRAME,
        "limit":     SCALP_BARS,
        "feed":      "iex",
    }
    try:
        r = requests.get(f"{ALPACA_DATA_URL}/stocks/bars",
                         headers=alpaca_headers, params=params, timeout=15)
        r.raise_for_status()
        out = {}
        for sym, bars_list in r.json().get("bars", {}).items():
            if len(bars_list) < 20:
                log.warning(f"[SCALP] {sym}: only {len(bars_list)} intraday bars — skip")
                continue
            out[sym] = {
                "c": np.array([b["c"] for b in bars_list]),
                "h": np.array([b["h"] for b in bars_list]),
                "l": np.array([b["l"] for b in bars_list]),
                "v": np.array([b["v"] for b in bars_list]),
                "t": [b["t"] for b in bars_list],
            }
        return out
    except Exception as e:
        log.error(f"[SCALP] Error fetching intraday bars: {e}")
        return {}


def get_account_equity() -> float:
    """Returns total portfolio equity (cash + long market value)."""
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/account", headers=alpaca_headers, timeout=10)
        r.raise_for_status()
        return float(r.json().get("equity", 0))
    except Exception as e:
        log.error(f"[SCALP] Error fetching account equity: {e}")
        return 0.0


def scalp_signals(bars_5m: dict) -> dict[str, dict]:
    """
    RSI(14) + 5-period momentum on 5-min bars.
    BUY  : RSI < SCALP_RSI_OVERSOLD  AND mom_5p < 0  (oversold, confirming)
    SELL : RSI > SCALP_RSI_OVERBOUGHT AND mom_5p > 0  (overbought, confirming)
    """
    out = {}
    for sym, d in bars_5m.items():
        closes  = d["c"]
        rsi_val = rsi(closes, period=14)
        mom_5p  = float(closes[-1] / closes[-6] - 1) if len(closes) >= 6 else float("nan")
        price   = float(closes[-1])

        if np.isnan(rsi_val) or np.isnan(mom_5p):
            out[sym] = {"rsi": None, "mom_5p": None, "signal": "NONE", "price": price}
            continue

        if rsi_val < SCALP_RSI_OVERSOLD and mom_5p < 0:
            signal = "BUY"
        elif rsi_val > SCALP_RSI_OVERBOUGHT and mom_5p > 0:
            signal = "SELL"
        else:
            signal = "NONE"

        out[sym] = {
            "rsi":    round(rsi_val, 1),
            "mom_5p": round(mom_5p, 5),
            "signal": signal,
            "price":  price,
        }
        log.info(f"[SCALP] {sym}: RSI={rsi_val:.1f} mom_5p={mom_5p:.4%} → {signal}")
    return out


def count_scalp_trades_today(mem: dict) -> int:
    today = date.today().isoformat()
    return sum(
        1 for t in mem.get("trades", [])
        if t.get("tag") == "SCALP" and t.get("date", "")[:10] == today
    )


def place_scalp_order(symbol: str, side: str, qty: int, price: float) -> bool:
    """Bracket order with fixed TP and SL derived from SCALP_TARGET_PCT / SCALP_STOP_PCT."""
    if qty <= 0:
        return False
    if side == "buy":
        tp_price   = round(price * (1 + SCALP_TARGET_PCT), 2)
        stop_price = round(price * (1 - SCALP_STOP_PCT), 2)
    else:
        tp_price   = round(price * (1 - SCALP_TARGET_PCT), 2)
        stop_price = round(price * (1 + SCALP_STOP_PCT), 2)

    body = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
        "order_class":   "bracket",
        "take_profit":   {"limit_price": str(tp_price)},
        "stop_loss":     {"stop_price":  str(stop_price)},
    }
    try:
        r = requests.post(f"{ALPACA_BASE_URL}/orders",
                          headers=alpaca_headers, json=body, timeout=10)
        r.raise_for_status()
        log.info(
            f"[SCALP] ORDER | {side.upper()} {qty} {symbol} "
            f"entry~${price:.2f} TP=${tp_price:.2f} SL=${stop_price:.2f} "
            f"| order_id={r.json().get('id')}"
        )
        return True
    except requests.HTTPError as e:
        log.error(f"[SCALP] Order failed {side} {qty} {symbol}: {e} | {r.text}")
        return False
    except Exception as e:
        log.error(f"[SCALP] Order error {side} {qty} {symbol}: {e}")
        return False


def run_scalp_mode(mem: dict, cash: float, positions: dict) -> dict:
    """
    Intraday scalping engine.
    Activated by run_cycle() when regime=HIGH_VOL and no ETF has vol_confirms=True.
    Uses bracket orders; max SCALP_MAX_DAILY_TRADES per calendar day.
    """
    trades_today = count_scalp_trades_today(mem)
    if trades_today >= SCALP_MAX_DAILY_TRADES:
        log.info(f"[SCALP] Daily cap reached ({trades_today}/{SCALP_MAX_DAILY_TRADES}) — skip.")
        return mem

    log.info(f"[SCALP] Mode ACTIVE — {trades_today}/{SCALP_MAX_DAILY_TRADES} trades today")

    bars_5m = get_intraday_bars(SCALP_SYMBOLS)
    if not bars_5m:
        log.warning("[SCALP] No intraday data — aborting.")
        return mem

    equity = get_account_equity()
    if equity <= 0:
        log.warning("[SCALP] Could not fetch portfolio equity — aborting.")
        return mem

    target_value = equity * SCALP_SIZE_PCT
    sigs = scalp_signals(bars_5m)

    for sym, sig in sigs.items():
        if trades_today >= SCALP_MAX_DAILY_TRADES:
            break
        if sig["signal"] == "NONE":
            continue

        price = sig["price"]
        if not price or price <= 0:
            continue

        qty = max(1, int(target_value / price))

        if sig["signal"] == "BUY":
            if positions.get(sym, 0) > 0:
                log.info(f"[SCALP] {sym}: already holding — skip BUY")
                continue
            placed = place_scalp_order(sym, "buy", qty, price)
        else:
            held = int(positions.get(sym, 0))
            if held <= 0:
                log.info(f"[SCALP] {sym}: no position to sell — skip SELL")
                continue
            qty = min(qty, held)
            placed = place_scalp_order(sym, "sell", qty, price)

        if placed:
            trades_today += 1
            direction = "oversold bounce" if sig["signal"] == "BUY" else "overbought reversal"
            reason = (
                f"SCALP {sig['signal']}: RSI={sig['rsi']} ({direction}), "
                f"mom_5p={sig['mom_5p']:.4%} | "
                f"TP={SCALP_TARGET_PCT:.1%} SL={SCALP_STOP_PCT:.1%}"
            )
            mem["trades"].append({
                "date":        datetime.now().isoformat(),
                "symbol":      sym,
                "side":        sig["signal"],
                "qty":         qty,
                "entry_price": price,
                "regime":      "HIGH_VOL",
                "tag":         "SCALP",
                "reason":      reason,
                "resolved":    False,
            })
            save_memory(mem)
            log.info(f"[SCALP] Trade recorded: {sig['signal']} {qty} {sym} — {reason}")
            send_trade_email(
                sym, f"SCALP_{sig['signal']}", qty, price, reason,
                cash, positions, {sym: {"price": price}}, mem,
            )

    return mem


# ══════════════════════════════════════════════════════════════════════════════
# MAIN CYCLE
# ══════════════════════════════════════════════════════════════════════════════

def run_cycle():
    log.info("=== Trading cycle start ===")

    if not is_market_open():
        log.info("Market closed — skipping.")
        return

    bars = get_historical_bars(ETFS)
    if not bars:
        log.warning("No bar data — skipping cycle.")
        return

    mem = load_memory()
    mem = resolve_trades(mem, bars)
    save_memory(mem)

    positions = get_positions()
    cash      = get_account_cash()
    news      = get_market_news()
    corr      = rolling_correlations(bars)
    regime    = detect_regime(bars, corr)
    signals   = generate_signals(bars, regime)
    sizing    = compute_sizing(mem, signals, cash, regime)

    log.info(f"Regime={regime['regime']} ADX={regime.get('adx_val')} "
             f"rv={regime.get('rv')} rv_pctile={regime.get('rv_pctile')} "
             f"sizing_mult={regime.get('sizing_mult')}")
    log.info(f"Correlations: {corr}")
    for sym in ETFS:
        s = signals.get(sym, {})
        if "error" not in s:
            log.info(
                f"Signal {sym}: rank=#{s.get('mom_rank')} "
                f"mom_combined={s.get('mom_combined')} "
                f"vol_confirms={s.get('vol_confirms')} "
                f"RSI={s.get('rsi')}({s.get('rsi_signal') or '-'}) "
                f"suggested={sizing.get(sym)}sh"
            )

    # Scalping mode: HIGH_VOL regime AND no ETF has volume-confirmed signal
    vol_confirms_any = any(
        signals.get(sym, {}).get("vol_confirms", False)
        for sym in ETFS
        if "error" not in signals.get(sym, {})
    )
    if regime["regime"] == "HIGH_VOL" and not vol_confirms_any:
        log.info("[SCALP] Conditions met (HIGH_VOL + no vol_confirms) — entering scalp mode")
        mem = run_scalp_mode(mem, cash, positions)

    prompt    = build_prompt(signals, regime, corr, positions, cash, news, sizing, mem)
    decisions = ask_claude(prompt)

    for sym, dec in decisions.items():
        action = dec.get("action", "HOLD").upper()
        qty    = int(dec.get("qty", 0))
        reason = dec.get("reason", "")
        log.info(f"DECISION | {sym}: {action} {qty}sh — {reason}")

        if action in ("BUY", "SELL") and qty > 0:
            placed = place_order(sym, action, qty)
            if placed:
                price = (signals.get(sym) or {}).get("price") or 0
                mem["trades"].append({
                    "date":        datetime.now().isoformat(),
                    "symbol":      sym,
                    "side":        action,
                    "qty":         qty,
                    "entry_price": price,
                    "regime":      regime["regime"],
                    "reason":      reason,
                    "resolved":    False,
                })
                save_memory(mem)
                send_trade_email(sym, action, qty, price, reason,
                                 cash, positions, signals, mem)

    log.info("=== Cycle complete ===\n")


def main():
    log.info("Quantitative ETF agent started.")
    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Agent stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error in cycle: {e}")
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    main()

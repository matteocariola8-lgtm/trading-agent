"""
agent.py — Quantitative Multi-Asset ETF Trading Agent (v2)

Novità rispetto alla v1 (5 ETF USA):
  - Universo ampliato a 70+ ETF: mercato USA broad+settoriale, mercati
    sviluppati/emergenti, materie prime, obbligazionario, valute,
    volatilità, prodotti a leva/inversi 2x/3x (vedi etf_universe.py)
  - Con un universo così ampio, mandare tutti i segnali a Claude in un
    prompt unico non è più praticabile (costo, qualità delle decisioni).
    Si calcola quindi un "conviction score" locale per ogni ETF e si
    manda a Claude solo la shortlist (top N per conviction + posizioni
    aperte), con contesto di regime macro invariato.
  - Sizing Kelly ora "leverage-aware": la frazione di capitale per ETF a
    leva è dampenata proporzionalmente alla leva (1/|leverage|) e il tetto
    massimo per singola posizione è più basso — la leva non è "stesso
    rischio più guadagno", è rischio strutturalmente diverso (volatility
    decay). Vedi half_kelly() e compute_sizing().
  - Il prompt istruisce esplicitamente Claude a trattare i prodotti a
    leva come tattici di brevissimo periodo, mai come posizioni core.

Academic basis (invariata dalla v1):
  Momentum   — Jegadeesh & Titman (1993); Asness et al. (2013)
  Regime     — Hamilton (1989) HMM-inspired; volatility-clustering (Mandelbrot 1963)
  Volume     — Granville OBV (1963); Blume, Easley & O'Hara (1994)
  Sizing     — Kelly (1956); half-Kelly via MacLean, Thorp & Ziemba (2010)
  Risk-on/off— Ilmanen (2011) cross-asset correlation regimes
  Leva ETF   — Cheng & Madhavan (2009) "The Dynamics of Leveraged and
               Inverse ETFs"; decadimento da ribilanciamento giornaliero
"""

import json
import logging
import os
import smtplib
import time
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import requests
from dotenv import load_dotenv
import anthropic

from etf_universe import (
    ALL_SYMBOLS, REGIME_REFERENCE_BASKET, SCALP_SYMBOLS as SCALP_SYMBOLS_UNIVERSE,
    category_of, leverage_of, name_of, is_leveraged,
)

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL")
ALPACA_DATA_URL   = "https://data.alpaca.markets/v2"
FINNHUB_API_KEY   = os.getenv("FINNHUB_API_KEY")

EMAIL_SENDER      = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD    = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER    = "matteo.cariola8@gmail.com"

ETFS              = ALL_SYMBOLS          # universo completo (70+ ETF, vedi etf_universe.py)
LOOP_INTERVAL     = 300
MEMORY_FILE       = os.environ.get("MEMORY_FILE", "memory.json")
BARS_LIMIT        = 310
MAX_SHARES        = 5                    # tetto base; ridotto ulteriormente per ETF a leva
MIN_ADX_FOR_RSI   = 25

SHORTLIST_SIZE    = 20                   # quanti ETF (per conviction) mandare a Claude ogni ciclo
ALPACA_SYMBOLS_PER_REQUEST = 190         # limite prudenziale per chiamata bars (paginazione a parte)

# Scalping mode
SCALP_SYMBOLS          = SCALP_SYMBOLS_UNIVERSE
SCALP_TIMEFRAME         = "5Min"
SCALP_BARS              = 100
SCALP_RSI_OVERSOLD      = 35
SCALP_RSI_OVERBOUGHT    = 65
SCALP_TARGET_PCT        = 0.004
SCALP_STOP_PCT          = 0.002
SCALP_SIZE_PCT          = 0.10
SCALP_SIZE_PCT_LEVERAGED = 0.04          # size ridotta per TQQQ/SQQQ in scalping
SCALP_MAX_DAILY_TRADES  = 3

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

def _chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def get_historical_bars(symbols: list[str], limit: int = BARS_LIMIT) -> dict[str, dict]:
    """Returns {sym: {c, h, l, v arrays}} sorted oldest→newest.
    Con 70+ simboli, l'endpoint Alpaca viene chiamato a blocchi
    (ALPACA_SYMBOLS_PER_REQUEST per volta) per restare sotto ai limiti
    pratici di query string / risposta."""
    start_date = (datetime.utcnow() - timedelta(days=500)).strftime("%Y-%m-%dT00:00:00Z")
    out: dict[str, dict] = {}

    for batch in _chunk(symbols, ALPACA_SYMBOLS_PER_REQUEST):
        raw_bars: dict[str, list] = {s: [] for s in batch}
        params = {
            "symbols":    ",".join(batch),
            "timeframe":  "1Day",
            "start":      start_date,
            "limit":      1000,
            "adjustment": "split",
            "feed":       "iex",
        }
        log.info(f"Fetching bars: {len(batch)} symbols, start={start_date}")
        try:
            page = 0
            while True:
                r = requests.get(f"{ALPACA_DATA_URL}/stocks/bars",
                                 headers=alpaca_headers, params=params, timeout=30)
                if not r.ok:
                    log.error(f"Bars API HTTP {r.status_code}: {r.text[:300]}")
                    break
                payload = r.json()
                page += 1
                for sym, bars_list in payload.get("bars", {}).items():
                    raw_bars.setdefault(sym, []).extend(bars_list)
                next_token = payload.get("next_page_token")
                if not next_token:
                    break
                params["page_token"] = next_token
        except Exception as e:
            log.error(f"Error fetching bars batch: {e}", exc_info=True)
            continue

        for sym, bars_list in raw_bars.items():
            bars_list = bars_list[-limit:]
            if len(bars_list) < 30:
                log.warning(f"{sym}: only {len(bars_list)} bars — skip (signals would be NaN)")
                continue
            out[sym] = {
                "c": np.array([b["c"] for b in bars_list]),
                "h": np.array([b["h"] for b in bars_list]),
                "l": np.array([b["l"] for b in bars_list]),
                "v": np.array([b["v"] for b in bars_list]),
                "t": [b["t"] for b in bars_list],
            }

    log.info(f"Bars loaded for {len(out)}/{len(symbols)} symbols")
    return out


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


def get_account_equity() -> float:
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/account", headers=alpaca_headers, timeout=10)
        r.raise_for_status()
        return float(r.json().get("equity", 0))
    except Exception as e:
        log.error(f"Error fetching account equity: {e}")
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
# TECHNICAL INDICATORS  (invariati dalla v1)
# ══════════════════════════════════════════════════════════════════════════════

def realized_vol(closes: np.ndarray, window: int = 20) -> float:
    if len(closes) < window + 1:
        return float("nan")
    lr = np.diff(np.log(closes[-(window + 1):]))
    return float(np.std(lr, ddof=1) * np.sqrt(252))


def momentum(closes: np.ndarray) -> dict:
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
    """Wilder (1978) smoothing. NOTA: seme iniziale come MEDIA (non somma)
    dei primi `period` valori — con seme a somma, la ricorrenza produce
    un output sulla scala "somma di periodo" invece che "media mobile".
    Per TR/+DM/-DM questo era innocuo perché ADX ne fa il rapporto
    (pdi/ndi) e la scala si cancella; ma applicato una seconda volta per
    smussare DX in ADX finale, senza un rapporto a cancellare la scala,
    il bug gonfiava l'ADX fino a 3-400 invece del range corretto 0-100 —
    bug presente nella versione precedente di agent.py, corretto qui."""
    s = float(np.mean(arr[:period]))
    out = [s]
    for x in arr[period:]:
        s = s - s / period + float(x) / period
        out.append(s)
    return np.array(out)


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
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
    if len(closes) < 11:
        return {"slope": 0.0, "confirms": False}
    signs = np.sign(np.diff(closes))
    obv   = np.concatenate([[0.0], np.cumsum(signs * volumes[1:])])
    raw_slope = (obv[-1] - obv[-10]) / 9
    norm      = np.mean(np.abs(obv[-20:])) or 1.0
    slope     = float(raw_slope / norm)
    mom_1m    = closes[-1] / closes[-22] - 1 if len(closes) >= 22 else 0
    confirms  = (mom_1m > 0 and slope > 0.02) or (mom_1m < 0 and slope < -0.02)
    # bool() esplicito: numpy.bool_ (risultato di confronti su array) non
    # è serializzabile nativamente da json.dump — senza il cast diventa
    # la STRINGA "True"/"False" invece del booleano JSON true/false,
    # e lato dashboard "False" (stringa non vuota) risulta truthy in JS.
    return {"slope": round(slope, 4), "confirms": bool(confirms)}


def rsi(closes: np.ndarray, period: int = 14) -> float:
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
    if len(close) < period + 1:
        return float("nan")
    tr = np.maximum(high[1:] - low[1:],
         np.maximum(np.abs(high[1:] - close[:-1]),
                    np.abs(low[1:]  - close[:-1])))
    return float(np.mean(tr[-period:]))


def rolling_correlations(bars: dict, window: int = 60) -> dict:
    """Correlazioni solo sul basket di riferimento macro (vedi
    etf_universe.REGIME_REFERENCE_BASKET) — su 70+ ETF calcolare tutte le
    coppie non aggiungerebbe segnale utile e sarebbe costoso."""
    rets, min_len = {}, None
    for sym in REGIME_REFERENCE_BASKET:
        d = bars.get(sym)
        if d and len(d["c"]) >= window + 1:
            r = np.diff(np.log(d["c"][-(window + 1):]))
            rets[sym] = r
            min_len = len(r) if min_len is None else min(min_len, len(r))
    corr = {}
    pairs = [("SPY", "QQQ"), ("SPY", "GLD"), ("SPY", "TLT"), ("GLD", "TLT"),
             ("SPY", "UUP"), ("SPY", "EFA"), ("SPY", "EEM")]
    for a, b in pairs:
        if a in rets and b in rets:
            ra, rb = rets[a][-min_len:], rets[b][-min_len:]
            corr[f"{a}_{b}"] = round(float(np.corrcoef(ra, rb)[0, 1]), 3)
        else:
            corr[f"{a}_{b}"] = float("nan")
    return corr


# ══════════════════════════════════════════════════════════════════════════════
# REGIME DETECTION  (invariato — ancorato a SPY come proxy macro)
# ══════════════════════════════════════════════════════════════════════════════

def detect_regime(bars: dict, corr: dict) -> dict:
    spy = bars.get("SPY", {})
    if not spy or len(spy["c"]) < 60:
        return {"regime": "UNKNOWN", "adx_val": float("nan"),
                "rv": float("nan"), "rv_pctile": float("nan")}

    rv_now = realized_vol(spy["c"], 20)
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
    risk_off = (spy_gld > 0.15 or spy_tlt > 0.05) and (mom_spy["mom_1m"] or 0) < -0.01
    if risk_off:
        return {**base, "regime": "TRENDING_BEAR",  "sizing_mult": 0.75}
    return     {**base, "regime": "TRENDING_BULL",  "sizing_mult": 1.00}


# ══════════════════════════════════════════════════════════════════════════════
# FACTOR SIGNALS  (ora sull'intero universo di 70+ ETF)
# ══════════════════════════════════════════════════════════════════════════════

def generate_signals(bars: dict, regime: dict) -> dict:
    """Calcola i fattori per ogni ETF nell'universo disponibile."""
    adx_spy = regime.get("adx_val") or float("nan")

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
            "category":     category_of(sym),
            "leverage":     leverage_of(sym),
            "name":         name_of(sym),
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


def conviction_score(sym: str, s: dict) -> float:
    """Punteggio locale usato per selezionare la shortlist da mandare a
    Claude (non sostituisce la decisione finale, solo il filtro). Più
    alto = più "interessante" da valutare, sia long che short.
    Prodotti a leva vengono leggermente penalizzati nel punteggio grezzo
    (non nella sizing, quella è gestita a parte) per non far dominare la
    shortlist con nomi ad alta beta solo perché si muovono di più."""
    if "error" in s or s.get("mom_combined") is None:
        return -999.0
    score = abs(s["mom_combined"])
    if s.get("vol_confirms"):
        score *= 1.3
    if s.get("rsi_signal"):
        score *= 1.15
    lev = abs(s.get("leverage", 1))
    if lev > 1:
        score *= 0.85  # leggero damping, non esclusione
    return score


def build_shortlist(signals: dict, positions: dict, n: int = SHORTLIST_SIZE) -> list[str]:
    """Top N per conviction score + qualunque ETF con posizione aperta
    (le posizioni aperte vanno sempre valutate per un'eventuale uscita,
    anche se non sono più tra le più "interessanti")."""
    scored = sorted(
        (sym for sym in ETFS if sym in signals),
        key=lambda sym: conviction_score(sym, signals[sym]),
        reverse=True,
    )
    shortlist = list(scored[:n])
    for sym in positions:
        if sym in signals and sym not in shortlist:
            shortlist.append(sym)
    return shortlist


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY & ADAPTIVE LEARNING  (leverage-aware sizing)
# ══════════════════════════════════════════════════════════════════════════════

def _default_kelly() -> dict:
    return {s: {"win_rate": 0.5, "avg_win": 0.012, "avg_loss": 0.012} for s in ETFS}


def load_memory() -> dict:
    try:
        with open(MEMORY_FILE) as f:
            mem = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        mem = {"trades": [], "outcomes": {}, "kelly": {}}

    # Garantisce entry di default per eventuali nuovi simboli aggiunti
    # all'universo dopo che memory.json esisteva già.
    for s in ETFS:
        mem.setdefault("outcomes", {}).setdefault(s, [])
        mem.setdefault("kelly", {}).setdefault(
            s, {"win_rate": 0.5, "avg_win": 0.012, "avg_loss": 0.012}
        )
    return mem


def save_memory(mem: dict):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2, default=str)


def resolve_trades(mem: dict, bars: dict) -> dict:
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
        mem["outcomes"][sym] = outcomes[-50:]

        recent = mem["outcomes"][sym][-30:]
        wins   = [o["pnl"] for o in recent if o["correct"]]
        losses = [abs(o["pnl"]) for o in recent if not o["correct"]]
        mem["kelly"][sym] = {
            "win_rate": round(len(wins) / len(recent), 3),
            "avg_win":  round(float(np.mean(wins))  if wins   else 0.012, 4),
            "avg_loss": round(float(np.mean(losses)) if losses else 0.012, 4),
        }
        updated.append(t)

    mem["trades"] = updated[-500:]  # cap alzato: universo più ampio genera più trade
    return mem


def half_kelly(win_rate: float, avg_win: float, avg_loss: float, leverage: int = 1) -> float:
    """
    Kelly (1956) half-Kelly, ora leverage-aware:
      - frazione base come nella v1, capped al 20%
      - per |leverage| > 1: la frazione viene ulteriormente divisa per
        |leverage|, e il cap massimo scende dal 20% al 20%/|leverage|.
        Motivazione: un ETF 3x a parità di P&L direzionale corretto porta
        3x la volatilità del sottostante, quindi l'equivalente-rischio di
        una posizione Kelly-ottimale è una frazione di capitale molto più
        piccola (Cheng & Madhavan 2009 sul decadimento da ribilanciamento).
    """
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    b = avg_win / avg_loss
    f = (win_rate * b - (1 - win_rate)) / b
    f = max(0.0, f / 2)
    lev = max(1, abs(leverage))
    cap = 0.20 / lev
    return min(f / lev, cap)


def compute_sizing(mem: dict, signals: dict, cash: float, regime: dict,
                    symbols: list[str]) -> dict[str, int]:
    """Share count per ETF nella shortlist: Kelly leverage-aware ×
    moltiplicatore di regime × cash, troncato a intero."""
    mult = regime.get("sizing_mult", 1.0)
    out  = {}
    for sym in symbols:
        kp    = mem["kelly"].get(sym, {"win_rate": 0.5, "avg_win": 0.012, "avg_loss": 0.012})
        lev   = leverage_of(sym)
        frac  = half_kelly(kp["win_rate"], kp["avg_win"], kp["avg_loss"], lev) * mult
        price = (signals.get(sym) or {}).get("price") or 0
        if price <= 0:
            out[sym] = 0
            continue
        max_shares = MAX_SHARES if abs(lev) <= 1 else max(1, MAX_SHARES // abs(lev))
        out[sym] = min(int(cash * frac / price), max_shares)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE INTEGRATION  (prompt dinamico sulla shortlist)
# ══════════════════════════════════════════════════════════════════════════════

def build_prompt(shortlist, signals, regime, corr, positions, cash, news, sizing, mem) -> str:
    reg  = regime["regime"]
    mult = regime.get("sizing_mult", 1.0)

    corr_lines = "\n".join(
        f"  {k}: {v:+.2f}" for k, v in corr.items() if not np.isnan(v)
    )

    sig_lines = []
    for sym in shortlist:
        s  = signals.get(sym, {})
        if "error" in s:
            continue
        kp = mem["kelly"].get(sym, {})
        outcomes = mem["outcomes"].get(sym, [])
        n_wins   = sum(1 for o in outcomes if o.get("correct"))
        lev_tag  = f" [LEVA {s['leverage']}x]" if abs(s.get("leverage", 1)) > 1 else ""
        held     = positions.get(sym, 0)
        sig_lines.append(
            f"  {sym}{lev_tag} ({s.get('category')}) | price=${s.get('price','?')}"
            f" | mom_rank=#{s.get('mom_rank','?')}"
            f" mom_12m={s.get('mom_12m','?')} mom_1m={s.get('mom_1m','?')} combined={s.get('mom_combined','?')}"
            f" | OBV_slope={s.get('obv_slope','?')} vol_confirms={s.get('vol_confirms','?')}"
            f" | RSI={s.get('rsi','?')} rsi_signal={s.get('rsi_signal') or 'inactive'}"
            f" | realized_vol={s.get('realized_vol','?')} ATR={s.get('atr','?')}"
            f" | kelly: wr={kp.get('win_rate',0.5):.0%} avgW={kp.get('avg_win',0):.2%} avgL={kp.get('avg_loss',0):.2%}"
            f" | suggested_shares={sizing.get(sym,0)}"
            f" | held={held} | history={n_wins}/{len(outcomes)} correct"
        )

    pos_text  = "\n".join(f"  {s}: {q} shares" for s, q in positions.items()) or "  None"
    news_text = "\n".join(f"  - {n['headline']}" for n in news) or "  No news."

    json_schema = ",\n".join(
        f'  "{sym}": {{"action": "BUY"|"SELL"|"HOLD", "qty": <int>, "reason": "<factor-based reason>"}}'
        for sym in shortlist
    )

    return f"""You are a quantitative multi-asset ETF trading agent. The universe includes broad market, sector, international, commodity, bond, currency, and LEVERAGED/INVERSE ETFs. Use the multi-factor analysis below for a disciplined, factor-driven decision on each ETF in the shortlist.

═══ ACCOUNT ════════════════════════════════════════════
Cash: ${cash:.2f}
Positions:
{pos_text}

═══ MARKET REGIME ══════════════════════════════════════
Regime:       {reg}
Sizing mult:  {mult:.0%} of Kelly (HIGH_VOL=25%, LATERAL=50%, TRENDING=75-100%)
ADX (SPY):    {regime.get('adx_val', 'N/A')}
Realized vol: {regime.get('rv', 'N/A')}
Vol pctile:   {regime.get('rv_pctile', 'N/A')}

═══ CROSS-ASSET CORRELATIONS (60D) ═════════════════════
{corr_lines}

═══ SHORTLIST — top {len(shortlist)} per conviction score + posizioni aperte ═════
suggested_shares è già leverage-aware (frazione Kelly divisa per |leva|, tetto ridotto).

{chr(10).join(sig_lines)}

═══ NEWS (tie-breaker only) ════════════════════════════
{news_text}

═══ DECISION FRAMEWORK ════════════════════════════════
1. HIGH_VOL o LATERAL → preferisci HOLD; agisci solo su rank #1-2 con vol_confirms=True.
2. TRENDING_BULL → BUY sui migliori per momentum con vol_confirms=True; SELL sui peggiori che detieni.
3. TRENDING_BEAR / correlazioni risk-off → favorisci GLD/TLT/bond; riduci equity ad alta beta.
4. ETF CONTRASSEGNATI [LEVA Nx]: trattali SOLO come posizioni tattiche di brevissimo periodo (max
   pochi giorni). Il ribilanciamento giornaliero causa decadimento da volatilità anche se la
   direzione è corretta — NON tenerli come posizione core. Agisci su un ETF a leva SOLO se:
   regime è TRENDING (non HIGH_VOL né LATERAL), mom_rank è tra i migliori nella sua categoria,
   E vol_confirms=True. In caso di dubbio, preferisci l'equivalente non a leva se presente nella
   shortlist.
5. Non superare mai suggested_shares. HOLD ha qty=0. BUY deve stare nel cash disponibile.
6. SELL non può superare la posizione attualmente detenuta (held).
7. Motivazione concisa e basata sui fattori (regime, momentum rank, OBV, RSI se attivo, e se
   rilevante il motivo per cui un ETF a leva è/non è giustificato in questo momento).

Rispondi SOLO con questo JSON (nessun markdown, nessun testo extra), una chiave per ogni ETF della shortlist:
{{
{json_schema}
}}"""


def ask_claude(prompt: str, shortlist: list[str]) -> dict[str, dict]:
    raw = ""
    fallback = {s: {"action": "HOLD", "qty": 0, "reason": "fallback"} for s in shortlist}
    try:
        msg = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,           # shortlist più ampia richiede più margine di output
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        decisions = json.loads(raw)
        # Validazione: solo simboli della shortlist, default HOLD per assenti
        out = {}
        for sym in shortlist:
            d = decisions.get(sym, {"action": "HOLD", "qty": 0, "reason": "not returned"})
            out[sym] = d
        return out
    except json.JSONDecodeError as e:
        log.error(f"Claude returned invalid JSON: {e}\nRaw: {raw[:300]}")
        return fallback
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return fallback


# ══════════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION  (invariato)
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
# NOTIFICATIONS  (invariato)
# ══════════════════════════════════════════════════════════════════════════════

def _build_pnl_block(positions: dict, signals: dict, mem: dict, cash: float) -> str:
    lines = []
    total_market = 0.0
    for sym, qty_pos in positions.items():
        current_price = (signals.get(sym) or {}).get("price")
        if not current_price:
            lines.append(f"  {sym}: {qty_pos} sh  (price unavailable)")
            continue
        market_val = current_price * qty_pos
        total_market += market_val
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


def send_trade_email(symbol, action, qty, price, reason, cash, positions, signals, mem) -> None:
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        log.warning("EMAIL_SENDER/EMAIL_PASSWORD not set — skipping trade email.")
        return

    timestamp    = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    trade_value  = price * qty
    pnl_block    = _build_pnl_block(positions, signals, mem, cash)
    lev = leverage_of(symbol)
    lev_warning = (f"\n⚠️  ETF A LEVA {lev}x — posizione tattica, non core.\n"
                   if abs(lev) > 1 else "")

    subject = f"[Trading Agent] {action} {qty}x {symbol} @ ${price:.2f}"
    body = f"""
Trading Agent — Order Notification
====================================
Timestamp  : {timestamp}
Ticker     : {symbol} ({name_of(symbol)})
Action     : {action}
Shares     : {qty}
Price      : ${price:.2f}
Trade value: ${trade_value:.2f}
{lev_warning}
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
# SCALPING MODE
# ══════════════════════════════════════════════════════════════════════════════

def get_intraday_bars(symbols: list[str]) -> dict[str, dict]:
    params = {"symbols": ",".join(symbols), "timeframe": SCALP_TIMEFRAME,
              "limit": SCALP_BARS, "feed": "iex"}
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


def scalp_signals(bars_5m: dict) -> dict[str, dict]:
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

        out[sym] = {"rsi": round(rsi_val, 1), "mom_5p": round(mom_5p, 5),
                    "signal": signal, "price": price}
        log.info(f"[SCALP] {sym}: RSI={rsi_val:.1f} mom_5p={mom_5p:.4%} → {signal}")
    return out


def count_scalp_trades_today(mem: dict) -> int:
    today = date.today().isoformat()
    return sum(
        1 for t in mem.get("trades", [])
        if t.get("tag") == "SCALP" and t.get("date", "")[:10] == today
    )


def place_scalp_order(symbol: str, side: str, qty: int, price: float) -> bool:
    if qty <= 0:
        return False
    if side == "buy":
        tp_price   = round(price * (1 + SCALP_TARGET_PCT), 2)
        stop_price = round(price * (1 - SCALP_STOP_PCT), 2)
    else:
        tp_price   = round(price * (1 - SCALP_TARGET_PCT), 2)
        stop_price = round(price * (1 + SCALP_STOP_PCT), 2)

    body = {
        "symbol": symbol, "qty": str(qty), "side": side, "type": "market",
        "time_in_force": "day", "order_class": "bracket",
        "take_profit": {"limit_price": str(tp_price)},
        "stop_loss":   {"stop_price":  str(stop_price)},
    }
    try:
        r = requests.post(f"{ALPACA_BASE_URL}/orders",
                          headers=alpaca_headers, json=body, timeout=10)
        r.raise_for_status()
        log.info(f"[SCALP] ORDER | {side.upper()} {qty} {symbol} "
                 f"entry~${price:.2f} TP=${tp_price:.2f} SL=${stop_price:.2f} "
                 f"| order_id={r.json().get('id')}")
        return True
    except requests.HTTPError as e:
        log.error(f"[SCALP] Order failed {side} {qty} {symbol}: {e} | {r.text}")
        return False
    except Exception as e:
        log.error(f"[SCALP] Order error {side} {qty} {symbol}: {e}")
        return False


def run_scalp_mode(mem: dict, cash: float, positions: dict) -> dict:
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

    sigs = scalp_signals(bars_5m)

    for sym, sig in sigs.items():
        if trades_today >= SCALP_MAX_DAILY_TRADES:
            break
        if sig["signal"] == "NONE":
            continue

        price = sig["price"]
        if not price or price <= 0:
            continue

        size_pct = SCALP_SIZE_PCT_LEVERAGED if is_leveraged(sym) else SCALP_SIZE_PCT
        target_value = equity * size_pct
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
            lev_tag = f" [LEVA {leverage_of(sym)}x]" if is_leveraged(sym) else ""
            reason = (
                f"SCALP{lev_tag} {sig['signal']}: RSI={sig['rsi']} ({direction}), "
                f"mom_5p={sig['mom_5p']:.4%} | TP={SCALP_TARGET_PCT:.1%} SL={SCALP_STOP_PCT:.1%}"
            )
            mem["trades"].append({
                "date": datetime.now().isoformat(), "symbol": sym, "side": sig["signal"],
                "qty": qty, "entry_price": price, "regime": "HIGH_VOL", "tag": "SCALP",
                "reason": reason, "resolved": False,
            })
            save_memory(mem)
            log.info(f"[SCALP] Trade recorded: {sig['signal']} {qty} {sym} — {reason}")
            send_trade_email(sym, f"SCALP_{sig['signal']}", qty, price, reason,
                             cash, positions, {sym: {"price": price}}, mem)

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

    shortlist = build_shortlist(signals, positions)
    sizing    = compute_sizing(mem, signals, cash, regime, shortlist)

    log.info(f"Regime={regime['regime']} ADX={regime.get('adx_val')} "
             f"rv={regime.get('rv')} rv_pctile={regime.get('rv_pctile')} "
             f"sizing_mult={regime.get('sizing_mult')}")
    log.info(f"Shortlist ({len(shortlist)}): {shortlist}")

    # Scalping mode: HIGH_VOL regime AND maggioranza della shortlist senza vol_confirms
    valid_shortlist = [sym for sym in shortlist if "error" not in signals.get(sym, {})]
    no_vol_count = sum(
        1 for sym in valid_shortlist
        if not signals.get(sym, {}).get("vol_confirms", False)
    )
    scalp_active = (regime["regime"] == "HIGH_VOL"
                    and valid_shortlist
                    and no_vol_count / len(valid_shortlist) >= 0.6)
    if scalp_active:
        log.info(f"[SCALP] Conditions met (HIGH_VOL + {no_vol_count}/{len(valid_shortlist)} "
                 f"shortlist senza vol_confirms) — entering scalp mode")
        mem = run_scalp_mode(mem, cash, positions)

    prompt    = build_prompt(shortlist, signals, regime, corr, positions, cash, news, sizing, mem)
    decisions = ask_claude(prompt, shortlist)

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
                    "date": datetime.now().isoformat(), "symbol": sym, "side": action,
                    "qty": qty, "entry_price": price, "regime": regime["regime"],
                    "reason": reason, "resolved": False,
                })
                save_memory(mem)
                send_trade_email(sym, action, qty, price, reason, cash, positions, signals, mem)

    # Persist dashboard state — salva SEMPRE l'intero universo di segnali
    # (non solo la shortlist) così la dashboard può mostrare tutto.
    mem["last_signals"]      = signals
    mem["last_shortlist"]    = shortlist
    mem["last_decisions"]    = decisions
    mem["last_regime"]       = regime
    mem["last_correlations"] = corr
    mem["last_scalp_active"] = scalp_active
    mem["last_positions"]    = positions
    mem["last_cash"]         = cash
    mem["universe_size"]     = len(ETFS)
    mem["last_cycle_at"]     = datetime.now().isoformat()
    save_memory(mem)

    log.info("=== Cycle complete ===\n")


def main():
    log.info(f"Quantitative multi-asset ETF agent v2 started — universe: {len(ETFS)} ETF.")
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

"""
options_engine.py — Modulo opzioni per il trading agent.

Copre i 3 livelli supportati da Alpaca (nessuna vendita "nuda"/scoperta
esiste sulla piattaforma — la copertura è imposta dal broker stesso):

  Level 1: covered call (richiede possedere >=100 azioni del sottostante
           per contratto) e cash-secured put (richiede cash riservato
           sufficiente a comprare 100 azioni allo strike se assegnato)
  Level 2: Level 1 + acquisto call/put (rischio = premio pagato)
  Level 3: Level 1+2 + spread verticali — NON IMPLEMENTATO in questo
           modulo (multi-leg order più complessi, da fare come step
           successivo separato)

Ogni azione ha rischio massimo CALCOLABILE e LIMITATO:
  - Buy call/put:        rischio max = premio pagato × 100 × contratti
  - Covered call (sell):  rischio max = quello che avresti comunque
                           possedendo le azioni (il titolo può andare a
                           zero), MENO il premio incassato — non superiore
                           al rischio di detenere il sottostante
  - Cash-secured put:     rischio max = (strike − premio incassato) × 100
                           × contratti, SE il sottostante va a zero

Design:
  - Nessuna dipendenza da agent.py — modulo standalone da integrare
  - Usa lo stesso concetto di "shortlist" già presente in agent.py v2:
    le opzioni si valutano solo sui simboli già nella shortlist equity,
    non su tutto l'universo (le catene opzioni sono costose da
    interrogare e la maggior parte dei 71 ETF non ha comunque mercato
    opzioni liquido)
  - NOTIONAL_AT_RISK_CAP: tetto aggregato al rischio massimo totale
    apribile in opzioni in un ciclo, indipendente da quanto Claude
    "vorrebbe" fare — è una barriera di sicurezza lato codice, non
    aggirabile dal prompt.
"""

import logging
import os
from datetime import datetime, timedelta

import requests

log = logging.getLogger(__name__)

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL")
ALPACA_OPTIONS_DATA_URL = "https://data.alpaca.markets/v1beta1/options"

alpaca_headers = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "accept":              "application/json",
    "content-type":        "application/json",
}

# ── Configurazione rischio ──────────────────────────────────────────────────

MIN_DTE = 7          # giorni minimi a scadenza (evita rumore/gamma estremo del 0DTE)
MAX_DTE = 45          # giorni massimi a scadenza (contratti troppo lontani = illiquidi, capitale bloccato)
MIN_OPEN_INTEREST = 50   # soglia minima di liquidità per considerare un contratto
DELTA_RANGE_BUY = (0.30, 0.60)     # per acquisto call/put: delta moderato, non troppo OTM né troppo ITM
DELTA_RANGE_COVERED_CALL = (0.20, 0.40)   # covered call: delta più basso, meno probabilità di assegnazione
DELTA_RANGE_CSP = (-0.30, -0.15)          # cash-secured put: delta assoluto basso, income-oriented

MAX_CONTRACTS_PER_POSITION = 3
NOTIONAL_AT_RISK_CAP_PCT = 0.15   # tetto: rischio massimo aggregato opzioni <= 15% dell'equity totale
                                    # per ciclo — barriera di sicurezza indipendente dalle decisioni AI


# ══════════════════════════════════════════════════════════════════════════
# DATA LAYER — contratti e catena opzioni
# ══════════════════════════════════════════════════════════════════════════

def is_options_enabled(symbol: str) -> bool:
    """Verifica se un simbolo ha contratti opzione disponibili, via
    l'attributo `options_enabled` esposto dall'endpoint Assets di Alpaca.
    Molti dei 71 ETF dell'universo (in particolare paesi/settori meno
    liquidi) probabilmente non hanno mercato opzioni — questa funzione va
    chiamata prima di provare a costruire una catena, per evitare
    richieste inutili."""
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/assets/{symbol}",
                         headers=alpaca_headers, timeout=10)
        r.raise_for_status()
        attrs = r.json().get("attributes", [])
        return "options_enabled" in attrs
    except Exception as e:
        log.warning(f"[OPTIONS] Impossibile verificare options_enabled per {symbol}: {e}")
        return False


def fetch_option_contracts(symbol: str, current_price: float) -> list[dict]:
    """Recupera i contratti disponibili per un sottostante, filtrati per
    finestra di scadenza (MIN_DTE-MAX_DTE giorni)."""
    today = datetime.utcnow().date()
    exp_gte = (today + timedelta(days=MIN_DTE)).isoformat()
    exp_lte = (today + timedelta(days=MAX_DTE)).isoformat()

    params = {
        "underlying_symbols": symbol,
        "expiration_date_gte": exp_gte,
        "expiration_date_lte": exp_lte,
        "limit": 100,
    }
    contracts = []
    try:
        while True:
            r = requests.get(f"{ALPACA_BASE_URL}/options/contracts",
                             headers=alpaca_headers, params=params, timeout=15)
            r.raise_for_status()
            payload = r.json()
            contracts.extend(payload.get("option_contracts", []))
            token = payload.get("page_token")
            if not token:
                break
            params["page_token"] = token
    except Exception as e:
        log.error(f"[OPTIONS] Errore fetch contratti {symbol}: {e}")
        return []

    # Filtro liquidità di base — apertura interesse minima
    liquid = [c for c in contracts
              if int(c.get("open_interest") or 0) >= MIN_OPEN_INTEREST]
    log.info(f"[OPTIONS] {symbol}: {len(contracts)} contratti totali, "
             f"{len(liquid)} sopra soglia liquidità")
    return liquid


def fetch_greeks_snapshot(contract_symbols: list[str]) -> dict[str, dict]:
    """Recupera greeks (delta, gamma, theta, vega) e IV per una lista di
    contratti via lo snapshot endpoint. Ritorna {contract_symbol: {...}}.
    Se lo snapshot fallisce per un batch, quel batch viene semplicemente
    escluso (i contratti restanti procedono senza greeks — verranno
    scartati a valle da select_candidates, che richiede il delta)."""
    if not contract_symbols:
        return {}
    out = {}
    # batch di 50 per chiamata, prudenziale
    for i in range(0, len(contract_symbols), 50):
        batch = contract_symbols[i:i + 50]
        try:
            r = requests.get(
                f"{ALPACA_OPTIONS_DATA_URL}/snapshots",
                headers=alpaca_headers,
                params={"symbols": ",".join(batch)},
                timeout=15,
            )
            r.raise_for_status()
            snapshots = r.json().get("snapshots", {})
            for sym, snap in snapshots.items():
                greeks = snap.get("greeks", {})
                out[sym] = {
                    "delta": greeks.get("delta"),
                    "theta": greeks.get("theta"),
                    "vega":  greeks.get("vega"),
                    "iv":    snap.get("impliedVolatility"),
                    "bid":   (snap.get("latestQuote") or {}).get("bp"),
                    "ask":   (snap.get("latestQuote") or {}).get("ap"),
                }
        except Exception as e:
            log.warning(f"[OPTIONS] Errore snapshot greeks (batch {i}): {e}")
            continue
    return out


# ══════════════════════════════════════════════════════════════════════════
# SELEZIONE CANDIDATI
# ══════════════════════════════════════════════════════════════════════════

def select_candidates(contracts: list[dict], greeks: dict[str, dict],
                       option_type: str, delta_range: tuple) -> list[dict]:
    """Filtra i contratti per tipo (call/put) e range di delta desiderato,
    arricchendoli con i dati greeks/quote. Ritorna lista ordinata per
    vicinanza al centro del delta_range (il candidato "più tipico")."""
    lo, hi = min(delta_range), max(delta_range)
    out = []
    for c in contracts:
        if c.get("type") != option_type:
            continue
        g = greeks.get(c["symbol"])
        if not g or g.get("delta") is None:
            continue
        d = g["delta"]
        if not (lo <= d <= hi or lo <= abs(d) <= hi):
            continue
        out.append({**c, **g})

    def dist_from_center(c):
        center = (lo + hi) / 2
        return abs(abs(c["delta"]) - abs(center))

    out.sort(key=dist_from_center)
    return out


# ══════════════════════════════════════════════════════════════════════════
# CALCOLO RISCHIO MASSIMO — sempre limitato, per costruzione
# ══════════════════════════════════════════════════════════════════════════

def max_risk_buy(premium: float, contracts: int) -> float:
    """Acquisto call/put: rischio massimo = premio pagato. Non puoi
    perdere più di quanto hai pagato."""
    return premium * 100 * contracts


def max_risk_covered_call(current_price: float, premium: float, contracts: int) -> float:
    """Covered call: possiedi già le azioni, quindi il rischio "aggiuntivo"
    della call è zero (anzi, incassi premio) — ma il rischio TOTALE della
    posizione (azioni + call) resta quello di detenere il sottostante,
    ridotto dal premio incassato. Ritorna il rischio della gamba azionaria
    equivalente, al netto del premio."""
    return max(0.0, (current_price * 100 - premium * 100)) * contracts


def max_risk_cash_secured_put(strike: float, premium: float, contracts: int) -> float:
    """Cash-secured put: se il sottostante va a zero, sei obbligato a
    comprare a strike — rischio massimo = (strike - premio incassato) ×
    100 × contratti. Il cash è già riservato per costruzione (altrimenti
    Alpaca rifiuta l'ordine)."""
    return max(0.0, (strike - premium)) * 100 * contracts


def aggregate_options_risk_ok(proposed_risk: float, current_options_risk: float,
                                total_equity: float) -> bool:
    """Barriera di sicurezza lato codice: il rischio massimo aggregato di
    TUTTE le posizioni opzioni (esistenti + proposta) non può superare
    NOTIONAL_AT_RISK_CAP_PCT dell'equity totale. Questo controllo non
    dipende dal prompt/da Claude — è imposto qui, prima di piazzare
    qualsiasi ordine."""
    cap = total_equity * NOTIONAL_AT_RISK_CAP_PCT
    return (current_options_risk + proposed_risk) <= cap


# ══════════════════════════════════════════════════════════════════════════
# INTEGRAZIONE PROMPT — blocco testuale da aggiungere al prompt equity
# ══════════════════════════════════════════════════════════════════════════

def build_options_prompt_block(symbol: str, current_price: float,
                                 calls: list[dict], puts: list[dict],
                                 covered_call_candidates: list[dict],
                                 csp_candidates: list[dict],
                                 has_shares: bool, cash_available: float) -> str:
    """Genera il blocco di testo opzioni per un singolo sottostante, da
    accodare al prompt equity esistente di agent.py."""
    lines = [f"OPZIONI DISPONIBILI SU {symbol} (prezzo corrente ${current_price:.2f}):"]

    if calls:
        c = calls[0]
        lines.append(
            f"  BUY CALL: strike ${c['strike_price']} scad. {c['expiration_date']} "
            f"| delta={c['delta']:.2f} premio~${c.get('ask', 0):.2f} "
            f"| rischio max = premio × 100 × qty"
        )
    if puts:
        p = puts[0]
        lines.append(
            f"  BUY PUT: strike ${p['strike_price']} scad. {p['expiration_date']} "
            f"| delta={p['delta']:.2f} premio~${p.get('ask', 0):.2f} "
            f"| rischio max = premio × 100 × qty"
        )
    if has_shares and covered_call_candidates:
        cc = covered_call_candidates[0]
        lines.append(
            f"  SELL COVERED CALL (richiede >=100 azioni possedute): "
            f"strike ${cc['strike_price']} scad. {cc['expiration_date']} "
            f"| delta={cc['delta']:.2f} premio incassato~${cc.get('bid', 0):.2f}"
        )
    elif covered_call_candidates:
        lines.append("  SELL COVERED CALL: non disponibile — non possiedi azioni del sottostante")

    if csp_candidates and cash_available > 0:
        csp = csp_candidates[0]
        strike = float(csp["strike_price"])
        required_cash = strike * 100
        if cash_available >= required_cash:
            lines.append(
                f"  SELL CASH-SECURED PUT: strike ${csp['strike_price']} "
                f"scad. {csp['expiration_date']} | delta={csp['delta']:.2f} "
                f"premio incassato~${csp.get('bid', 0):.2f} "
                f"| richiede ${required_cash:.0f} cash riservato"
            )
        else:
            lines.append(
                f"  SELL CASH-SECURED PUT: non disponibile — servirebbero "
                f"${required_cash:.0f} riservati, cash disponibile ${cash_available:.0f}"
            )

    lines.append(
        "  NOTA: nessuna vendita scoperta è possibile su questa piattaforma — "
        "covered call richiede le azioni, cash-secured put richiede il cash. "
        "Il rischio massimo di ogni azione è sempre calcolabile in anticipo."
    )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# ESECUZIONE ORDINE
# ══════════════════════════════════════════════════════════════════════════

def place_option_order(contract_symbol: str, side: str, qty: int) -> bool:
    """Piazza un ordine option single-leg (buy o sell) a mercato.
    side: 'buy' (apre long call/put) o 'sell' (apre covered call /
    cash-secured put — Alpaca valida automaticamente la copertura e
    rifiuta l'ordine se non sei coperto, es. non possiedi le azioni)."""
    if qty <= 0:
        return False
    try:
        r = requests.post(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers,
            json={
                "symbol": contract_symbol, "qty": str(qty), "side": side,
                "type": "market", "time_in_force": "day",
            },
            timeout=10,
        )
        r.raise_for_status()
        log.info(f"[OPTIONS] ORDINE PIAZZATO | {side} {qty} {contract_symbol} "
                 f"| order_id={r.json().get('id')}")
        return True
    except requests.HTTPError as e:
        # Alpaca rifiuta qui se manca la copertura (azioni o cash) — non è
        # un errore da "correggere", è la barriera di sicurezza della
        # piattaforma che ha funzionato come previsto.
        log.error(f"[OPTIONS] Ordine rifiutato {side} {qty} {contract_symbol}: {e} | {r.text}")
        return False
    except Exception as e:
        log.error(f"[OPTIONS] Errore ordine {side} {qty} {contract_symbol}: {e}")
        return False


if __name__ == "__main__":
    # Smoke test con dati sintetici — nessuna chiamata di rete
    print("Test calcolo rischio massimo:")
    print(f"  Buy call premio=2.50, 2 contratti: rischio max = "
          f"${max_risk_buy(2.50, 2):.2f}")
    print(f"  Covered call su AAPL@$180, premio=3.00, 1 contratto: "
          f"esposizione netta = ${max_risk_covered_call(180, 3.00, 1):.2f}")
    print(f"  Cash-secured put strike=170, premio=2.00, 1 contratto: "
          f"rischio max = ${max_risk_cash_secured_put(170, 2.00, 1):.2f}")

    print("\nTest barriera di sicurezza aggregata:")
    ok = aggregate_options_risk_ok(proposed_risk=3000, current_options_risk=2000,
                                     total_equity=50000)
    print(f"  Rischio proposto 3000 + esistente 2000 su equity 50000 "
          f"(cap 15% = 7500): consentito = {ok}")
    ok2 = aggregate_options_risk_ok(proposed_risk=6000, current_options_risk=2000,
                                      total_equity=50000)
    print(f"  Rischio proposto 6000 + esistente 2000 su equity 50000 "
          f"(cap 15% = 7500): consentito = {ok2}")

"""
etf_universe.py

Definizione centrale dell'universo ETF gestito dal bot — non più solo i
5 ETF americani originali (SPY/QQQ/GLD/TLT/UUP), ma un paniere ampio che
copre: mercato USA broad + settoriale, mercati internazionali sviluppati
ed emergenti, materie prime, obbligazionario, valute, volatilità, e
prodotti a leva/inversi sui principali indici.

IMPORTANTE — vincolo strutturale: Alpaca è un broker USA e opera SOLO su
titoli quotati su borse USA. "Copertura di tutto il mercato mondiale" qui
significa: ETF quotati USA che danno ESPOSIZIONE a mercati internazionali
(es. EFA per Europa/Asia sviluppati, EEM per emergenti) — non trading
diretto su borse estere (Milano, Francoforte, Tokyo, ecc.), che Alpaca
non supporta.

Ogni ETF ha metadati:
  - category:  raggruppamento tematico (per la UI e per il ranking)
  - leverage:  moltiplicatore di leva (1 = nessuna leva, 2/3 = leva
               long, -1/-2/-3 = inverso/leva inversa)
  - name:      nome leggibile per la dashboard

Il campo `leverage` è usato da agent.py per:
  1. Ridurre il sizing Kelly proporzionalmente alla leva (decadimento da
     volatilità — un 3x non è "3 volte più bot", è strutturalmente più
     rischioso anche a parità di direzione corretta)
  2. Istruire Claude nel prompt a trattare questi prodotti come tattici
     di brevissimo periodo, non da tenere in portafoglio per giorni
  3. Etichettare chiaramente la leva nella dashboard (mai nasconderla)
"""

UNIVERSE = {
    # ── Mercato USA broad ──────────────────────────────────────────────
    "SPY":  {"category": "broad_market", "leverage": 1, "name": "S&P 500"},
    "VOO":  {"category": "broad_market", "leverage": 1, "name": "S&P 500 (Vanguard)"},
    "QQQ":  {"category": "broad_market", "leverage": 1, "name": "Nasdaq 100"},
    "DIA":  {"category": "broad_market", "leverage": 1, "name": "Dow Jones Industrial"},
    "IWM":  {"category": "broad_market", "leverage": 1, "name": "Russell 2000 (small cap)"},

    # ── Settori USA (SPDR Select Sector) ───────────────────────────────
    "XLK":  {"category": "sector", "leverage": 1, "name": "Tecnologia"},
    "XLF":  {"category": "sector", "leverage": 1, "name": "Finanziario"},
    "XLE":  {"category": "sector", "leverage": 1, "name": "Energia"},
    "XLV":  {"category": "sector", "leverage": 1, "name": "Salute"},
    "XLI":  {"category": "sector", "leverage": 1, "name": "Industriale"},
    "XLY":  {"category": "sector", "leverage": 1, "name": "Consumo discrezionale"},
    "XLP":  {"category": "sector", "leverage": 1, "name": "Beni di consumo primari"},
    "XLU":  {"category": "sector", "leverage": 1, "name": "Utility"},
    "XLB":  {"category": "sector", "leverage": 1, "name": "Materiali"},
    "XLRE": {"category": "sector", "leverage": 1, "name": "Immobiliare"},
    "XLC":  {"category": "sector", "leverage": 1, "name": "Comunicazioni"},

    # ── Mercati sviluppati internazionali ──────────────────────────────
    "EFA":  {"category": "intl_developed", "leverage": 1, "name": "Europa/Asia sviluppati"},
    "VEA":  {"category": "intl_developed", "leverage": 1, "name": "Mercati sviluppati ex-USA"},
    "EWJ":  {"category": "intl_developed", "leverage": 1, "name": "Giappone"},
    "EWG":  {"category": "intl_developed", "leverage": 1, "name": "Germania"},
    "EWU":  {"category": "intl_developed", "leverage": 1, "name": "Regno Unito"},
    "EWA":  {"category": "intl_developed", "leverage": 1, "name": "Australia"},
    "EWC":  {"category": "intl_developed", "leverage": 1, "name": "Canada"},

    # ── Mercati emergenti ───────────────────────────────────────────────
    "EEM":  {"category": "intl_emerging", "leverage": 1, "name": "Mercati emergenti"},
    "VWO":  {"category": "intl_emerging", "leverage": 1, "name": "Mercati emergenti (Vanguard)"},
    "FXI":  {"category": "intl_emerging", "leverage": 1, "name": "Cina large cap"},
    "MCHI": {"category": "intl_emerging", "leverage": 1, "name": "Cina broad"},
    "INDA": {"category": "intl_emerging", "leverage": 1, "name": "India"},
    "EWZ":  {"category": "intl_emerging", "leverage": 1, "name": "Brasile"},
    "EWY":  {"category": "intl_emerging", "leverage": 1, "name": "Corea del Sud"},
    "EWT":  {"category": "intl_emerging", "leverage": 1, "name": "Taiwan"},

    # ── Materie prime ───────────────────────────────────────────────────
    "GLD":  {"category": "commodity", "leverage": 1, "name": "Oro"},
    "SLV":  {"category": "commodity", "leverage": 1, "name": "Argento"},
    "USO":  {"category": "commodity", "leverage": 1, "name": "Petrolio"},
    "UNG":  {"category": "commodity", "leverage": 1, "name": "Gas naturale"},
    "DBA":  {"category": "commodity", "leverage": 1, "name": "Agricoltura"},
    "DBC":  {"category": "commodity", "leverage": 1, "name": "Materie prime broad"},
    "CPER": {"category": "commodity", "leverage": 1, "name": "Rame"},

    # ── Obbligazionario ─────────────────────────────────────────────────
    "TLT":  {"category": "bond", "leverage": 1, "name": "Treasury USA 20+ anni"},
    "IEF":  {"category": "bond", "leverage": 1, "name": "Treasury USA 7-10 anni"},
    "SHY":  {"category": "bond", "leverage": 1, "name": "Treasury USA 1-3 anni"},
    "LQD":  {"category": "bond", "leverage": 1, "name": "Corporate investment grade"},
    "HYG":  {"category": "bond", "leverage": 1, "name": "Corporate high yield"},
    "TIP":  {"category": "bond", "leverage": 1, "name": "Treasury inflation-protected"},
    "AGG":  {"category": "bond", "leverage": 1, "name": "Obbligazionario aggregato USA"},
    "MUB":  {"category": "bond", "leverage": 1, "name": "Municipal bond"},

    # ── Valute ───────────────────────────────────────────────────────────
    "UUP":  {"category": "currency", "leverage": 1, "name": "Dollaro USA (Index)"},
    "FXE":  {"category": "currency", "leverage": 1, "name": "Euro"},
    "FXY":  {"category": "currency", "leverage": 1, "name": "Yen giapponese"},
    "FXB":  {"category": "currency", "leverage": 1, "name": "Sterlina britannica"},
    "FXA":  {"category": "currency", "leverage": 1, "name": "Dollaro australiano"},

    # ── Volatilità ───────────────────────────────────────────────────────
    "VXX":  {"category": "volatility", "leverage": 1, "name": "Volatilità VIX short-term"},

    # ── Leva LONG 3x ─────────────────────────────────────────────────────
    "TQQQ": {"category": "leveraged_bull", "leverage": 3, "name": "Nasdaq 100 3x Long"},
    "UPRO": {"category": "leveraged_bull", "leverage": 3, "name": "S&P 500 3x Long"},
    "SOXL": {"category": "leveraged_bull", "leverage": 3, "name": "Semiconduttori 3x Long"},
    "TNA":  {"category": "leveraged_bull", "leverage": 3, "name": "Russell 2000 3x Long"},
    "FAS":  {"category": "leveraged_bull", "leverage": 3, "name": "Finanziario 3x Long"},
    "SPXL": {"category": "leveraged_bull", "leverage": 3, "name": "S&P 500 3x Long (alt)"},
    "TECL": {"category": "leveraged_bull", "leverage": 3, "name": "Tecnologia 3x Long"},

    # ── Leva SHORT/inversa 3x ────────────────────────────────────────────
    "SQQQ": {"category": "leveraged_bear", "leverage": -3, "name": "Nasdaq 100 3x Short"},
    "SPXU": {"category": "leveraged_bear", "leverage": -3, "name": "S&P 500 3x Short"},
    "SOXS": {"category": "leveraged_bear", "leverage": -3, "name": "Semiconduttori 3x Short"},
    "TZA":  {"category": "leveraged_bear", "leverage": -3, "name": "Russell 2000 3x Short"},
    "FAZ":  {"category": "leveraged_bear", "leverage": -3, "name": "Finanziario 3x Short"},
    "SPXS": {"category": "leveraged_bear", "leverage": -3, "name": "S&P 500 3x Short (alt)"},

    # ── Leva 2x (long e inversa) ─────────────────────────────────────────
    "SSO":  {"category": "leveraged_bull", "leverage": 2, "name": "S&P 500 2x Long"},
    "QLD":  {"category": "leveraged_bull", "leverage": 2, "name": "Nasdaq 100 2x Long"},
    "DDM":  {"category": "leveraged_bull", "leverage": 2, "name": "Dow Jones 2x Long"},
    "SDS":  {"category": "leveraged_bear", "leverage": -2, "name": "S&P 500 2x Short"},
    "QID":  {"category": "leveraged_bear", "leverage": -2, "name": "Nasdaq 100 2x Short"},
    "DXD":  {"category": "leveraged_bear", "leverage": -2, "name": "Dow Jones 2x Short"},

    # ── Azioni singole USA — Tecnologia ───────────────────────────────────
    "AAPL": {"category": "stock_tech", "leverage": 1, "name": "Apple"},
    "MSFT": {"category": "stock_tech", "leverage": 1, "name": "Microsoft"},
    "GOOGL":{"category": "stock_tech", "leverage": 1, "name": "Alphabet (Google)"},
    "AMZN": {"category": "stock_tech", "leverage": 1, "name": "Amazon"},
    "META": {"category": "stock_tech", "leverage": 1, "name": "Meta Platforms"},
    "NVDA": {"category": "stock_tech", "leverage": 1, "name": "Nvidia"},
    "TSLA": {"category": "stock_tech", "leverage": 1, "name": "Tesla"},
    "NFLX": {"category": "stock_tech", "leverage": 1, "name": "Netflix"},
    "ADBE": {"category": "stock_tech", "leverage": 1, "name": "Adobe"},
    "CRM":  {"category": "stock_tech", "leverage": 1, "name": "Salesforce"},
    "AVGO": {"category": "stock_tech", "leverage": 1, "name": "Broadcom"},
    "AMD":  {"category": "stock_tech", "leverage": 1, "name": "AMD"},
    "INTC": {"category": "stock_tech", "leverage": 1, "name": "Intel"},
    "QCOM": {"category": "stock_tech", "leverage": 1, "name": "Qualcomm"},
    "MU":   {"category": "stock_tech", "leverage": 1, "name": "Micron Technology"},

    # ── Azioni singole USA — Finanza ───────────────────────────────────────
    "JPM":  {"category": "stock_finance", "leverage": 1, "name": "JPMorgan Chase"},
    "BAC":  {"category": "stock_finance", "leverage": 1, "name": "Bank of America"},
    "GS":   {"category": "stock_finance", "leverage": 1, "name": "Goldman Sachs"},
    "MS":   {"category": "stock_finance", "leverage": 1, "name": "Morgan Stanley"},
    "WFC":  {"category": "stock_finance", "leverage": 1, "name": "Wells Fargo"},
    "V":    {"category": "stock_finance", "leverage": 1, "name": "Visa"},
    "MA":   {"category": "stock_finance", "leverage": 1, "name": "Mastercard"},
    "AXP":  {"category": "stock_finance", "leverage": 1, "name": "American Express"},

    # ── Azioni singole USA — Salute ──────────────────────────────────────
    "JNJ":  {"category": "stock_health", "leverage": 1, "name": "Johnson & Johnson"},
    "UNH":  {"category": "stock_health", "leverage": 1, "name": "UnitedHealth"},
    "PFE":  {"category": "stock_health", "leverage": 1, "name": "Pfizer"},
    "ABBV": {"category": "stock_health", "leverage": 1, "name": "AbbVie"},
    "MRK":  {"category": "stock_health", "leverage": 1, "name": "Merck"},
    "LLY":  {"category": "stock_health", "leverage": 1, "name": "Eli Lilly"},
    "TMO":  {"category": "stock_health", "leverage": 1, "name": "Thermo Fisher Scientific"},

    # ── Azioni singole USA — Consumo ─────────────────────────────────────
    "WMT":  {"category": "stock_consumer", "leverage": 1, "name": "Walmart"},
    "PG":   {"category": "stock_consumer", "leverage": 1, "name": "Procter & Gamble"},
    "KO":   {"category": "stock_consumer", "leverage": 1, "name": "Coca-Cola"},
    "PEP":  {"category": "stock_consumer", "leverage": 1, "name": "PepsiCo"},
    "MCD":  {"category": "stock_consumer", "leverage": 1, "name": "McDonald's"},
    "NKE":  {"category": "stock_consumer", "leverage": 1, "name": "Nike"},
    "HD":   {"category": "stock_consumer", "leverage": 1, "name": "Home Depot"},
    "COST": {"category": "stock_consumer", "leverage": 1, "name": "Costco"},

    # ── Azioni singole USA — Industriale ed Energia ────────────────────────
    "BA":   {"category": "stock_industrial", "leverage": 1, "name": "Boeing"},
    "CAT":  {"category": "stock_industrial", "leverage": 1, "name": "Caterpillar"},
    "GE":   {"category": "stock_industrial", "leverage": 1, "name": "General Electric"},
    "HON":  {"category": "stock_industrial", "leverage": 1, "name": "Honeywell"},
    "LMT":  {"category": "stock_industrial", "leverage": 1, "name": "Lockheed Martin"},
    "XOM":  {"category": "stock_energy", "leverage": 1, "name": "ExxonMobil"},
    "CVX":  {"category": "stock_energy", "leverage": 1, "name": "Chevron"},
    "COP":  {"category": "stock_energy", "leverage": 1, "name": "ConocoPhillips"},

    # ── ADR — esposizione indiretta a EUROPA (titoli quotati USA) ──────────
    "ASML": {"category": "adr_europe", "leverage": 1, "name": "ASML (Olanda, semiconduttori)"},
    "SAP":  {"category": "adr_europe", "leverage": 1, "name": "SAP (Germania, software)"},
    "NVO":  {"category": "adr_europe", "leverage": 1, "name": "Novo Nordisk (Danimarca, farma)"},
    "UL":   {"category": "adr_europe", "leverage": 1, "name": "Unilever (Regno Unito, largo consumo)"},
    "SHEL": {"category": "adr_europe", "leverage": 1, "name": "Shell (Regno Unito, energia)"},
    "TTE":  {"category": "adr_europe", "leverage": 1, "name": "TotalEnergies (Francia, energia)"},
    "SNY":  {"category": "adr_europe", "leverage": 1, "name": "Sanofi (Francia, farma)"},
    "AZN":  {"category": "adr_europe", "leverage": 1, "name": "AstraZeneca (Regno Unito, farma)"},

    # ── ADR — esposizione indiretta ad ASIA (titoli quotati USA) ────────────
    "TSM":  {"category": "adr_asia", "leverage": 1, "name": "Taiwan Semiconductor (Taiwan)"},
    "BABA": {"category": "adr_asia", "leverage": 1, "name": "Alibaba (Cina)"},
    "JD":   {"category": "adr_asia", "leverage": 1, "name": "JD.com (Cina)"},
    "SONY": {"category": "adr_asia", "leverage": 1, "name": "Sony (Giappone)"},
    "TM":   {"category": "adr_asia", "leverage": 1, "name": "Toyota (Giappone)"},
    "SE":   {"category": "adr_asia", "leverage": 1, "name": "Sea Limited (Singapore)"},
    "INFY": {"category": "adr_asia", "leverage": 1, "name": "Infosys (India)"},
}

ALL_SYMBOLS = sorted(UNIVERSE.keys())

# Basket ristretto usato per il calcolo di regime/correlazioni (troppo
# costoso e poco utile calcolare correlazioni incrociate su 90+ ticker —
# questi restano i riferimenti macro, come nella versione originale, con
# l'aggiunta di due proxy internazionali).
REGIME_REFERENCE_BASKET = ["SPY", "QQQ", "GLD", "TLT", "UUP", "EFA", "EEM"]

# Simboli su cui gira la modalità scalping intraday — tenuti volutamente
# pochi e liquidi: lo scalping su prodotti a leva è più rischioso per via
# della microstruttura più rumorosa; si include comunque la coppia
# leva/inversa più liquida (TQQQ/SQQQ) ma con size ridotta (vedi agent.py).
SCALP_SYMBOLS = ["SPY", "QQQ", "IWM", "TQQQ", "SQQQ"]


def category_of(symbol: str) -> str:
    return UNIVERSE.get(symbol, {}).get("category", "unknown")


def leverage_of(symbol: str) -> int:
    return UNIVERSE.get(symbol, {}).get("leverage", 1)


def name_of(symbol: str) -> str:
    return UNIVERSE.get(symbol, {}).get("name", symbol)


def is_leveraged(symbol: str) -> bool:
    return abs(leverage_of(symbol)) > 1


if __name__ == "__main__":
    print(f"Universo totale: {len(ALL_SYMBOLS)} ETF")
    from collections import Counter
    counts = Counter(category_of(s) for s in ALL_SYMBOLS)
    for cat, n in sorted(counts.items()):
        print(f"  {cat}: {n}")

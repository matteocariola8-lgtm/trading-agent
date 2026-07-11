# Come integrare `performance_tracker.py` in `agent.py`

Questo file NON sostituisce `agent.py` — è una guida con gli snippet esatti
da inserire. Adatta i nomi delle funzioni ai tuoi reali (qui uso nomi
plausibili in base a quanto mi hai descritto: selezione strategia via
Claude, chiusura posizioni, regime di mercato HIGH_VOL ecc.).

## 1. Copia il file nel repo

Metti `performance_tracker.py` nella stessa cartella di `agent.py`
(es. root del progetto trading bot).

## 2. Import e inizializzazione (in cima ad `agent.py`)

```python
from performance_tracker import PerformanceTracker

# Path scelto per persistere su Railway: se usi un volume montato,
# punta lì (es. "/data/trades_history.jsonl") cosi' i dati sopravvivono
# ai redeploy. Se non hai un volume, il file si resetta ad ogni deploy:
# in tal caso considera di committare periodicamente il file nel repo
# o scriverlo anche su Google Drive/Sheet.
tracker = PerformanceTracker(
    os.environ.get("TRADES_HISTORY_PATH", "trades_history.jsonl")
)
```

## 3. Nel punto dove chiudi un trade

Cerca la funzione che gestisce la chiusura di una posizione (probabilmente
dove leggi il fill di Alpaca) e aggiungi:

```python
tracker.log_trade(
    strategy=strategy_used,        # es. "momentum_ma_crossover"
    symbol=symbol,                 # es. "SPY"
    market_regime=current_regime,  # es. "HIGH_VOL" - quello che già calcoli
    pnl_pct=trade_pnl_pct,         # P&L percentuale del trade chiuso
    outcome="win" if trade_pnl_pct > 0 else ("loss" if trade_pnl_pct < 0 else "breakeven"),
)
```

## 4. Nel punto dove costruisci il prompt di selezione strategia

Cerca dove costruisci il messaggio da mandare a Claude per scegliere la
strategia (probabilmente una f-string o un template). Prima di quella
chiamata:

```python
performance_context = tracker.format_for_prompt(window=20)

strategy_selection_prompt = f"""{performance_context}

{strategy_selection_prompt}
"""
```

Cioè: antepponi il blocco di performance storica al prompt che già esiste,
senza toccare il resto della logica.

## 5. Commit dei due file via GitHub web (se lavori da iPad)

1. Vai su https://github.com/matteocariola8-lgtm/[nome-repo-trading-bot]
2. Clicca **Add file → Upload files** (in alto a destra sopra la lista file)
3. Trascina `performance_tracker.py`
4. In fondo alla pagina, scrivi il messaggio di commit (es. "Aggiunge performance tracker per apprendimento strategia")
5. Seleziona **Commit directly to the main branch**
6. Clicca **Commit changes**
7. Apri `agent.py` dal repo (clicca sul file) → icona matita in alto a destra (**Edit this file**)
8. Incolla gli snippet dei punti 2, 3, 4 nei punti giusti
9. In fondo, commit message + **Commit directly to the main branch** → **Commit changes**
10. Railway farà auto-redeploy se hai il collegamento GitHub attivo — controlla la tab **Deployments** dopo un paio di minuti per confermare che il deploy sia andato a buon fine

## Nota importante

Questo modulo dà al bot "memoria" delle performance passate — ma la
decisione finale su come usarla resta a Claude nel prompt di selezione
strategia. Non è apprendimento automatico in senso stretto (nessun
parametro si aggiorna da solo): è un meccanismo di feedback esplicito.
Se in futuro vuoi un vero adattamento automatico dei parametri (es.
soglie RSI, take-profit/stop-loss), quello è uno step successivo separato
e più delicato da validare in paper trading prima di portarlo live.

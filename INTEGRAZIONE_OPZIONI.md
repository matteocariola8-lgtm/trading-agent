# Integrazione options_engine.py in agent.py / main.py / dashboard.html

## Perché questo è un file di integrazione e non un rewrite completo

Il modulo opzioni tocca ogni pezzo del sistema esistente (prompt, memoria,
esecuzione ordini, dashboard). Un rewrite completo di agent.py che
combinasse 71 ETF + shortlist + opzioni in un colpo solo sarebbe troppo
grande da testare con affidabilità in un solo passaggio. Questa guida ti
dà gli snippet esatti da inserire nei punti giusti, così puoi integrare
con controllo — e testare ogni pezzo separatamente prima del prossimo.

## 1. Copia `options_engine.py` nel repo

Stessa cartella di `agent.py`.

## 2. Import in agent.py

```python
import options_engine as oe
```

## 3. Nel ciclo principale, dopo aver calcolato la shortlist equity

Per ogni simbolo della shortlist, prova a costruire il blocco opzioni
(silenziosamente skippato se il simbolo non ha opzioni o non ci sono
candidati validi):

```python
def build_options_context(shortlist, signals, positions, cash, mem):
    """Ritorna {symbol: blocco_testo_opzioni} solo per i simboli che
    hanno effettivamente contratti opzione disponibili e candidati validi."""
    options_blocks = {}
    for sym in shortlist:
        if not oe.is_options_enabled(sym):
            continue
        price = (signals.get(sym) or {}).get("price")
        if not price:
            continue

        contracts = oe.fetch_option_contracts(sym, price)
        if not contracts:
            continue

        contract_symbols = [c["symbol"] for c in contracts]
        greeks = oe.fetch_greeks_snapshot(contract_symbols)

        calls = oe.select_candidates(contracts, greeks, "call", oe.DELTA_RANGE_BUY)
        puts  = oe.select_candidates(contracts, greeks, "put", oe.DELTA_RANGE_BUY)
        ccs   = oe.select_candidates(contracts, greeks, "call", oe.DELTA_RANGE_COVERED_CALL)
        csps  = oe.select_candidates(contracts, greeks, "put", oe.DELTA_RANGE_CSP)

        has_shares = positions.get(sym, 0) >= 100  # 1 contratto copre 100 azioni
        block = oe.build_options_prompt_block(
            sym, price, calls, puts, ccs, csps, has_shares, cash
        )
        options_blocks[sym] = block
    return options_blocks
```

## 4. Estendi `build_prompt()` per includere i blocchi opzioni

Nel punto dove costruisci `sig_lines` in `build_prompt()`, dopo aver
generato il blocco per un simbolo, se `options_blocks.get(sym)` esiste,
appendilo:

```python
options_context = build_options_context(shortlist, signals, positions, cash, mem)

# ... dentro build_prompt, dopo il blocco fattori equity di ogni simbolo ...
if sym in options_context:
    sig_lines.append(options_context[sym])
```

E estendi lo schema JSON atteso per includere un campo opzionale
`options_action` per ogni simbolo:

```python
json_schema = ",\n".join(
    f'  "{sym}": {{"action": "BUY"|"SELL"|"HOLD", "qty": <int>, "reason": "...", '
    f'"options_action": {{"type": "buy_call"|"buy_put"|"sell_covered_call"|"sell_csp"|"none", '
    f'"contract_symbol": "<simbolo contratto o null>", "contracts": <int>}}}}'
    for sym in shortlist
)
```

## 5. Barriera di sicurezza PRIMA di piazzare qualsiasi ordine opzioni

Questo è il punto più importante — non è negoziabile, va SEMPRE
controllato prima di chiamare `place_option_order()`:

```python
def compute_current_options_risk(mem: dict) -> float:
    """Somma il rischio massimo di tutte le posizioni opzioni aperte,
    da tracciare in mem['options_trades'] (struttura analoga a
    mem['trades'] ma per opzioni)."""
    total = 0.0
    for t in mem.get("options_trades", []):
        if not t.get("closed"):
            total += t.get("max_risk", 0)
    return total


# Nel punto dove esegui la decisione options_action:
if dec.get("options_action", {}).get("type") not in (None, "none"):
    oa = dec["options_action"]
    contract = oa["contract_symbol"]
    n_contracts = min(oa.get("contracts", 1), oe.MAX_CONTRACTS_PER_POSITION)

    # Calcola il rischio max della singola proposta (dipende dal tipo)
    # ... recupera premio/strike dal contratto scelto ...
    proposed_risk = ...  # oe.max_risk_buy / max_risk_covered_call / max_risk_cash_secured_put

    equity = cash + market_value  # stesso calcolo già presente altrove
    current_risk = compute_current_options_risk(mem)

    if not oe.aggregate_options_risk_ok(proposed_risk, current_risk, equity):
        log.warning(f"[OPTIONS] Blocco: rischio aggregato supererebbe il "
                     f"{oe.NOTIONAL_AT_RISK_CAP_PCT:.0%} dell'equity — skip")
    else:
        side = "buy" if oa["type"] in ("buy_call", "buy_put") else "sell"
        placed = oe.place_option_order(contract, side, n_contracts)
        if placed:
            mem.setdefault("options_trades", []).append({
                "date": datetime.now().isoformat(), "symbol": sym,
                "contract_symbol": contract, "type": oa["type"],
                "contracts": n_contracts, "max_risk": proposed_risk,
                "closed": False,
            })
            save_memory(mem)
```

## 6. Estensioni a main.py (nuovi endpoint)

```python
@app.route("/api/options_positions")
def api_options_positions():
    mem = load_memory()
    trades = [t for t in mem.get("options_trades", []) if not t.get("closed")]
    return jsonify(trades)

@app.route("/api/options_risk_summary")
def api_options_risk_summary():
    mem = load_memory()
    trades = [t for t in mem.get("options_trades", []) if not t.get("closed")]
    total_risk = sum(t.get("max_risk", 0) for t in trades)
    equity = mem.get("last_cash", 0)  # + market value, come altrove
    return jsonify({
        "total_max_risk": round(total_risk, 2),
        "cap_pct": 15,
        "positions_count": len(trades),
    })
```

## 7. Estensione dashboard.html (sezione compatta)

Aggiungi una card nella sezione hero o una nuova sezione dedicata, sullo
stesso pattern visivo delle altre (badge leva → badge tipo opzione:
`BUY CALL` / `BUY PUT` / `COVERED CALL` / `CSP`, colore ambra come la
leva, dato che sono comunque strumenti a rischio/complessità elevati):

```html
<section>
  <div class="section-head">
    <h2>Posizioni opzioni</h2>
    <span class="meta" id="options-risk-meta">— rischio max aggregato</span>
  </div>
  <div class="card" style="padding:0; overflow-x:auto;">
    <table>
      <thead>
        <tr><th>Sottostante</th><th>Tipo</th><th>Contratti</th><th>Rischio max</th></tr>
      </thead>
      <tbody id="options-body">
        <tr><td colspan="4" class="empty-state">Nessuna posizione opzioni aperta</td></tr>
      </tbody>
    </table>
  </div>
</section>
```

Con il corrispondente JS (stesso pattern di `loadPositions()`):

```javascript
async function loadOptionsPositions() {
  const rows = await fetchJSON('/api/options_positions');
  const summary = await fetchJSON('/api/options_risk_summary');
  const tbody = document.getElementById('options-body');
  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">Nessuna posizione opzioni aperta</td></tr>';
  } else {
    tbody.innerHTML = rows.map(r => `
      <tr>
        <td class="ticker">${r.symbol}</td>
        <td><span class="lev-badge">${r.type.replace('_', ' ').toUpperCase()}</span></td>
        <td class="num">${r.contracts}</td>
        <td class="num">$${r.max_risk.toFixed(2)}</td>
      </tr>
    `).join('');
  }
  if (summary) {
    document.getElementById('options-risk-meta').textContent =
      `rischio max aggregato $${summary.total_max_risk.toFixed(0)} (tetto ${summary.cap_pct}% equity)`;
  }
}
// aggiungi loadOptionsPositions() a refreshAll()
```

## Cosa NON è incluso in questa consegna

- **Spread verticali (Level 3)**: order multi-leg, costruzione più
  complessa (due gambe con rapporto di prezzo). Se vuoi procedere, è un
  modulo separato da costruire dopo aver validato che il resto funziona.
- **Gestione esercizio/assegnazione automatica**: Alpaca gestisce
  l'esercizio automatico dei contratti ITM a scadenza — il codice non
  interviene su questo, ma `mem['options_trades']` andrebbe aggiornato
  quando una posizione si chiude per esercizio/assegnazione (le NTA su
  paper arrivano il giorno dopo, non in tempo reale — vedi doc Alpaca).
  Per ora questo tracking va fatto manualmente o con un job separato che
  polla `/v2/account/activities` il giorno successivo.
- **Test contro dati opzioni reali**: ho testato la logica di selezione,
  calcolo rischio e barriera di sicurezza con dati sintetici. Non ho
  potuto verificare il formato esatto della risposta di
  `/v1beta1/options/snapshots` (greeks/IV) contro l'API reale — se il
  campo `impliedVolatility` o la struttura `greeks` non corrispondono
  esattamente a quanto documentato, `fetch_greeks_snapshot()` andrà
  aggiustata sulla base dell'errore reale che vedrai nei log.

## Prossimo passo consigliato

Prima di integrare tutto insieme, testa `options_engine.py` da solo
contro l'API paper reale: chiama `fetch_option_contracts("SPY", 550)` e
`fetch_greeks_snapshot([...])` in isolamento, controlla che la struttura
dati torni come previsto, POI collega il resto.

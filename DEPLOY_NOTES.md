# Trading Bot v2 — Note di rilascio e deploy

## Cosa è cambiato rispetto alla versione attuale

**Universo ETF**: da 5 (SPY/QQQ/GLD/TLT/UUP) a **71 ETF** su 10 categorie
— mercato USA broad, settoriale, internazionale sviluppato/emergente,
materie prime, obbligazionario, valute, volatilità, leva 2x/3x long e
short. Definizione centralizzata in `etf_universe.py` — per aggiungere o
togliere ETF in futuro, si tocca solo quel file.

**Selezione shortlist**: con 71 ETF non è più praticabile (né utile)
mandare tutto a Claude in un prompt unico. Ogni ciclo calcola un
"conviction score" locale per ogni ETF e seleziona i migliori 20 + le
posizioni aperte (che vanno sempre valutate per un'eventuale uscita anche
se non più tra le più interessanti). Solo questa shortlist va a Claude.

**Sizing leverage-aware**: la frazione di capitale Kelly per un ETF a
leva viene divisa per `|leva|` e il tetto massimo per posizione si
riduce proporzionalmente (20% → 6.7% per un 3x). Motivazione: un ETF a
leva non è "stesso rischio, guadagno moltiplicato" — il ribilanciamento
giornaliero causa decadimento da volatilità (Cheng & Madhavan, 2009)
anche quando la direzione è corretta. Vedi `half_kelly()` in agent.py.

**Prompt a Claude**: ogni ETF a leva nella shortlist è taggato
`[LEVA Nx]` e il framework decisionale istruisce esplicitamente a
trattarli come tattici di brevissimo periodo, mai come posizione core,
e ad agire solo con regime TRENDING confermato + momentum forte +
conferma di volume.

**Dashboard e API completamente ricostruite** (`main.py` + `dashboard.html`)
dato che i file originali non erano disponibili. Nuovo design: navy/oro
coerente col brand Sagoma Finanziaria, font DM Serif Display + Inter +
JetBrains Mono, con:
- Regime di mercato, equity totale, correlazioni cross-asset
- Shortlist con badge leva ben visibile e categoria
- Posizioni aperte con P&L
- "Note dell'analista AI": le motivazioni di Claude per ogni decisione,
  presentate come citazioni — è la cosa più utile per capire *perché*
  il bot ha agito, non solo *cosa* ha fatto
- Refresh automatico ogni 30 secondi

## Bug corretto (presente nella versione precedente, non introdotto da me)

`_wilder_smooth()`, usata per calcolare l'ADX, aveva un problema di
normalizzazione: il seme iniziale della ricorsione era una **somma**
invece che una **media**. Per gli indicatori intermedi (TR, +DM, -DM)
questo era innocuo perché l'ADX ne fa il rapporto e la scala si
cancella — ma applicato una seconda volta per smussare il DX nell'ADX
finale, senza un rapporto a cancellare la scala, il valore restituito
poteva essere 3-4 volte più grande del corretto (range 0-100). L'ho
verificato con un test: prima del fix, ADX = 330.9; dopo, 23.6 — ordine
di grandezza coerente.

**Perché conta**: le soglie di regime (`ADX < 20` = laterale, `> 25` =
trend forte) con ADX sistematicamente gonfiato quasi non facevano mai
scattare "LATERAL", con conseguenze dirette sul sizing (`sizing_mult`).
Se il bot attualmente in produzione gira ancora con il codice vecchio,
vale la pena saperlo — non è bloccante ma è un bias sistematico nel
regime detection.

## File consegnati

- `etf_universe.py` — nuovo, definizione dell'universo ETF
- `agent.py` — riscritto (v2)
- `main.py` — ricostruito da zero (i file originali non erano disponibili)
- `dashboard.html` — ricostruita da zero

## Come testare prima di caricare su GitHub/Railway

Ho testato in locale (sandbox, senza rete verso Alpaca/Anthropic reali)
con dati sintetici: pipeline indicatori → regime → segnali → shortlist →
sizing → prompt, e il server Flask con le API. Tutto risponde
correttamente e la dashboard è HTML/JS sintatticamente valida. **Non ho
potuto testare contro Alpaca, Anthropic, o Railway reali** — mancava
l'accesso di rete nel mio ambiente. Prima di considerarlo definitivo:

1. Carica i 4 file nel repo (stesso posto di prima)
2. Verifica che `requirements.txt` includa `flask` (main.py lo richiede;
   se il vecchio main.py lo usava già, dovrebbe già esserci)
3. Testa in locale se possibile: `python main.py` con un `memory.json`
   vuoto o di prova, apri `http://localhost:8000`
4. Fai girare `agent.py` per almeno un ciclo con il mercato aperto e
   controlla i log (`trades.log`) per errori — in particolare la
   chiamata a `get_historical_bars()` con 71 simboli: Alpaca pagina le
   richieste ma non ho potuto verificare il comportamento reale con
   il tuo piano/rate limit
5. Controlla che il prompt a Claude (visibile nei log se aggiungi un
   `log.info(prompt)` temporaneo) non superi lunghezze problematiche —
   con 20 ETF in shortlist dovrebbe stare abbondantemente sotto i limiti,
   ma è la prima cosa da guardare se `ask_claude()` inizia a fallire

## Cosa NON ho toccato

- `daily_report.py` — non l'ho mai visto, non l'ho toccato
- La logica di scheduling/deploy su Railway resta la stessa
- Le variabili d'ambiente richieste sono le stesse di prima (nessuna
  nuova chiave necessaria)

## Promemoria sul rischio leva

Lo dicevo prima di scrivere il codice e lo ripeto ora che è scritto:
il codice imposta dei limiti prudenziali (sizing ridotto, tag espliciti,
istruzioni di trattamento tattico a Claude), ma **non elimina il rischio
strutturale della leva**. Se il piano resta di andare live a settembre
2026 con soldi veri, vale la pena valutare — prima di quella data — se
tenere la leva anche in produzione o limitarla ancora di più rispetto a
qui, sulla base di come si comporta effettivamente in paper trading nei
prossimi mesi.

# ETF Paper Trading Agent

Agente automatico che opera su Alpaca Paper Trading ogni 60 secondi usando Claude AI come decision maker.

## ETF gestiti
SPY, QQQ, GLD, TLT, UUP

## Setup

```bash
# 1. Installa dipendenze
pip install -r requirements.txt

# 2. Inserisci la tua chiave Anthropic in .env
# Sostituisci "la_tua_chiave_anthropic" con la chiave reale da console.anthropic.com
nano .env

# 3. Avvia l'agente
python agent.py
```

## Configurazione `.env`

| Variabile | Descrizione |
|-----------|-------------|
| `ALPACA_API_KEY` | Chiave API Alpaca (paper) |
| `ALPACA_SECRET_KEY` | Secret Alpaca |
| `ALPACA_BASE_URL` | Endpoint paper trading Alpaca |
| `FINNHUB_API_KEY` | Chiave API Finnhub per le notizie |
| `ANTHROPIC_API_KEY` | Chiave API Anthropic per Claude |

## Logica del ciclo (ogni 60s)

1. Controlla se il mercato USA è aperto tramite l'API Alpaca clock
2. Legge i prezzi aggiornati dei 5 ETF da Alpaca (feed IEX)
3. Legge le ultime 10 notizie di mercato da Finnhub
4. Invia prezzi, posizioni, cash disponibile e notizie a `claude-sonnet-4-6`
5. Claude risponde con BUY / SELL / HOLD + quantità per ogni ETF
6. Gli ordini vengono eseguiti come market order sul paper account
7. Tutto viene loggato su `trades.log` e console

## File di log

`trades.log` — contiene ogni decisione presa e ogni ordine eseguito, con timestamp.

## Note

- L'agente è attivo solo durante gli orari di mercato USA (9:30–16:00 ET, lun–ven).
- Gli ordini sono **solo paper trading**: nessun denaro reale viene movimentato.
- Modifica `LOOP_INTERVAL` in `agent.py` per cambiare la frequenza del ciclo.

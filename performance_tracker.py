"""
performance_tracker.py

Modulo standalone per dare "memoria" al bot di trading.
Traccia l'esito di ogni trade (per strategia e per regime di mercato) e
produce un riepilogo testuale da iniettare nel prompt di selezione
strategia inviato a Claude, così le decisioni future sono condizionate
sulle performance reali passate, non solo sugli indicatori del momento.

Design:
- Storage: file JSONL append-only (una riga = un trade chiuso).
  Scelto JSONL invece di un DB per semplicità di deploy su Railway
  (nessuna dipendenza extra, leggibile anche a mano).
- Nessuna dipendenza esterna oltre la stdlib.
- Pensato per essere importato da agent.py e da performance_logger.py
  esistente, senza sostituirli.

Uso tipico in agent.py:

    from performance_tracker import PerformanceTracker

    tracker = PerformanceTracker("trades_history.jsonl")

    # ... quando un trade si chiude ...
    tracker.log_trade(
        strategy="momentum_ma_crossover",
        symbol="SPY",
        market_regime="TREND_UP",   # o HIGH_VOL, RANGE, ecc.
        pnl_pct=0.42,
        outcome="win",
        notes="MA crossover confermato da volume"
    )

    # ... prima di chiedere a Claude quale strategia usare ...
    performance_summary = tracker.format_for_prompt(window=20)
    prompt = f"{performance_summary}\n\n{resto_del_prompt_esistente}"
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean


class PerformanceTracker:
    def __init__(self, storage_path: str = "trades_history.jsonl"):
        self.storage_path = storage_path
        # crea il file se non esiste, cosi' le letture successive non falliscono
        if not os.path.exists(self.storage_path):
            open(self.storage_path, "a").close()

    # ------------------------------------------------------------------
    # Scrittura
    # ------------------------------------------------------------------
    def log_trade(
        self,
        strategy: str,
        symbol: str,
        market_regime: str,
        pnl_pct: float,
        outcome: str,  # "win" | "loss" | "breakeven"
        notes: str = "",
    ) -> None:
        """Registra un trade chiuso. Va chiamato nel punto del codice
        dove il bot chiude una posizione (paper o live)."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "symbol": symbol,
            "market_regime": market_regime,
            "pnl_pct": pnl_pct,
            "outcome": outcome,
            "notes": notes,
        }
        with open(self.storage_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    # ------------------------------------------------------------------
    # Lettura / aggregazione
    # ------------------------------------------------------------------
    def _load_trades(self) -> list:
        trades = []
        with open(self.storage_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    # riga corrotta: la saltiamo invece di far crashare il bot
                    continue
        return trades

    def get_summary(self, window: int = 20) -> dict:
        """
        Calcola, per ogni strategia, statistiche sugli ultimi `window` trade
        (globalmente e spezzate per market_regime).

        Ritorna una struttura tipo:
        {
          "momentum_ma_crossover": {
              "count": 20, "win_rate": 0.55, "avg_pnl_pct": 0.31,
              "by_regime": {
                  "HIGH_VOL": {"count": 8, "win_rate": 0.375, "avg_pnl_pct": -0.05},
                  "TREND_UP": {"count": 12, "win_rate": 0.66, "avg_pnl_pct": 0.55},
              }
          },
          ...
        }
        """
        trades = self._load_trades()
        by_strategy = defaultdict(list)
        for t in trades:
            by_strategy[t["strategy"]].append(t)

        summary = {}
        for strategy, strategy_trades in by_strategy.items():
            # ultimi N trade di quella strategia, in ordine cronologico
            recent = strategy_trades[-window:]
            summary[strategy] = self._aggregate(recent)
            summary[strategy]["by_regime"] = {}

            by_regime = defaultdict(list)
            for t in recent:
                by_regime[t["market_regime"]].append(t)
            for regime, regime_trades in by_regime.items():
                summary[strategy]["by_regime"][regime] = self._aggregate(regime_trades)

        return summary

    @staticmethod
    def _aggregate(trades: list) -> dict:
        if not trades:
            return {"count": 0, "win_rate": None, "avg_pnl_pct": None}
        wins = sum(1 for t in trades if t["outcome"] == "win")
        return {
            "count": len(trades),
            "win_rate": round(wins / len(trades), 3),
            "avg_pnl_pct": round(mean(t["pnl_pct"] for t in trades), 3),
        }

    # ------------------------------------------------------------------
    # Output per il prompt
    # ------------------------------------------------------------------
    def format_for_prompt(self, window: int = 20) -> str:
        """Genera un blocco di testo pronto da iniettare nel prompt
        di selezione strategia. Se non ci sono ancora abbastanza dati,
        lo dichiara esplicitamente invece di inventare numeri."""
        summary = self.get_summary(window=window)

        if not summary:
            return (
                "PERFORMANCE STORICA: nessun dato disponibile ancora. "
                "Scegli la strategia solo in base alle condizioni di mercato attuali."
            )

        lines = [f"PERFORMANCE STORICA (ultimi {window} trade per strategia):"]
        for strategy, stats in summary.items():
            if stats["count"] == 0:
                continue
            lines.append(
                f"- {strategy}: {stats['count']} trade, "
                f"win rate {stats['win_rate']*100:.0f}%, "
                f"P&L medio {stats['avg_pnl_pct']:+.2f}%"
            )
            for regime, rstats in stats.get("by_regime", {}).items():
                if rstats["count"] == 0:
                    continue
                lines.append(
                    f"    · in regime {regime}: {rstats['count']} trade, "
                    f"win rate {rstats['win_rate']*100:.0f}%, "
                    f"P&L medio {rstats['avg_pnl_pct']:+.2f}%"
                )

        lines.append(
            "\nUsa questi dati per pesare la scelta della prossima strategia: "
            "preferisci strategie con win rate e P&L medio migliori nel regime "
            "di mercato attuale, evita quelle che stanno sistematicamente perdendo "
            "nello stesso regime, anche se il segnale tecnico sembra valido."
        )
        return "\n".join(lines)


if __name__ == "__main__":
    # Demo/smoke test rapido eseguibile con: python performance_tracker.py
    t = PerformanceTracker("demo_trades.jsonl")
    t.log_trade("momentum_ma_crossover", "SPY", "TREND_UP", 0.5, "win")
    t.log_trade("momentum_ma_crossover", "SPY", "HIGH_VOL", -0.3, "loss")
    t.log_trade("rsi_mean_reversion", "QQQ", "HIGH_VOL", -0.1, "loss")
    print(t.format_for_prompt())

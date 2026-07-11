"""
weekly_report.py

Report settimanale del trading agent, via email — riusa la stessa struttura
di memory.json e le stesse credenziali SMTP già usate in agent.py.

NON sostituisce daily_report.py: fa un'aggregazione su 7 giorni invece che
giornaliera, con focus su: performance per ETF, per regime di mercato,
confronto trade normali vs scalping, e l'evoluzione dei parametri Kelly
(prova concreta che il bot sta "imparando" o meno).

Uso autonomo:
    python weekly_report.py

Variabili .env richieste (le stesse di agent.py):
    EMAIL_SENDER, EMAIL_PASSWORD  -> credenziali Gmail SMTP
    (EMAIL_RECEIVER è hardcoded come in agent.py, cambialo se serve)

Scheduling: vedi SCHEDULING.md per come farlo girare ogni lunedì mattina
senza toccare il loop principale di agent.py.
"""

import json
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

MEMORY_FILE     = os.getenv("MEMORY_FILE", "memory.json")
EMAIL_SENDER    = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER  = "matteo.cariola8@gmail.com"
REPORT_DAYS     = 7


def load_memory(path: str = MEMORY_FILE) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"trades": [], "outcomes": {}, "kelly": {}}


def _parse_date(iso_str: str):
    try:
        return datetime.fromisoformat(iso_str[:19])
    except Exception:
        return None


def filter_last_n_days(trades: list, days: int) -> list:
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for t in trades:
        d = _parse_date(t.get("date", ""))
        if d and d >= cutoff:
            out.append(t)
    return out


def aggregate(trades: list) -> dict:
    """Statistiche aggregate su un elenco di trade (risolti e non)."""
    resolved = [t for t in trades if t.get("resolved")]
    open_    = [t for t in trades if not t.get("resolved")]

    wins   = [t for t in resolved if t.get("correct")]
    losses = [t for t in resolved if t.get("correct") is False]

    total_pnl_pct = sum(t.get("pnl_pct", 0) for t in resolved)
    avg_pnl_pct   = (total_pnl_pct / len(resolved)) if resolved else 0.0

    return {
        "total_trades":    len(trades),
        "resolved_count":  len(resolved),
        "open_count":      len(open_),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(len(wins) / len(resolved), 3) if resolved else None,
        "avg_pnl_pct":     round(avg_pnl_pct, 4),
    }


def per_symbol_breakdown(trades: list) -> dict:
    by_symbol = {}
    for t in trades:
        sym = t.get("symbol", "?")
        by_symbol.setdefault(sym, []).append(t)
    return {sym: aggregate(tl) for sym, tl in by_symbol.items()}


def per_regime_breakdown(trades: list) -> dict:
    by_regime = {}
    for t in trades:
        regime = t.get("regime", "UNKNOWN")
        by_regime.setdefault(regime, []).append(t)
    return {regime: aggregate(tl) for regime, tl in by_regime.items()}


def scalp_vs_normal(trades: list) -> dict:
    scalp  = [t for t in trades if t.get("tag") == "SCALP"]
    normal = [t for t in trades if t.get("tag") != "SCALP"]
    return {"scalp": aggregate(scalp), "normal": aggregate(normal)}


def kelly_snapshot(mem: dict) -> dict:
    """Stato attuale dei parametri Kelly per ETF — mostra se il bot
    sta effettivamente ricalibrando il rischio sugli esiti recenti."""
    return mem.get("kelly", {})


def format_stats_line(label: str, s: dict) -> str:
    if s["resolved_count"] == 0:
        return f"  {label}: nessun trade risolto in questo periodo ({s['open_count']} ancora aperti)"
    wr = f"{s['win_rate']*100:.0f}%" if s["win_rate"] is not None else "N/A"
    return (
        f"  {label}: {s['resolved_count']} risolti "
        f"({s['wins']}W/{s['losses']}L, win rate {wr}), "
        f"P&L medio {s['avg_pnl_pct']*100:+.2f}%, "
        f"{s['open_count']} ancora aperti"
    )


def build_report_text(mem: dict) -> str:
    all_trades  = mem.get("trades", [])
    week_trades = filter_last_n_days(all_trades, REPORT_DAYS)

    overall  = aggregate(week_trades)
    by_sym   = per_symbol_breakdown(week_trades)
    by_reg   = per_regime_breakdown(week_trades)
    scalp_vs = scalp_vs_normal(week_trades)
    kelly    = kelly_snapshot(mem)

    lines = []
    lines.append(f"Report settimanale — {datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"Periodo: ultimi {REPORT_DAYS} giorni")
    lines.append("=" * 50)
    lines.append("")
    lines.append("PERFORMANCE COMPLESSIVA")
    lines.append(format_stats_line("Tutti i trade", overall))
    lines.append("")

    lines.append("PER STRATEGIA (normale vs scalping)")
    lines.append(format_stats_line("Trade normali (daily-bar)", scalp_vs["normal"]))
    lines.append(format_stats_line("Scalping intraday", scalp_vs["scalp"]))
    lines.append("")

    lines.append("PER ETF")
    for sym in sorted(by_sym.keys()):
        lines.append(format_stats_line(sym, by_sym[sym]))
    lines.append("")

    lines.append("PER REGIME DI MERCATO")
    for regime in sorted(by_reg.keys()):
        lines.append(format_stats_line(regime, by_reg[regime]))
    lines.append("")

    lines.append("PARAMETRI KELLY ATTUALI (rischio calibrato per ETF)")
    if kelly:
        for sym, kp in kelly.items():
            lines.append(
                f"  {sym}: win_rate={kp.get('win_rate', 0)*100:.0f}%  "
                f"avg_win={kp.get('avg_win', 0)*100:.2f}%  "
                f"avg_loss={kp.get('avg_loss', 0)*100:.2f}%"
            )
    else:
        lines.append("  Nessun dato Kelly disponibile ancora.")
    lines.append("")

    lines.append("-" * 50)
    lines.append(
        "Nota: 'risolti' = trade valutati 5 giorni di mercato dopo l'apertura "
        "(vedi resolve_trades() in agent.py). I trade più recenti della settimana "
        "possono risultare ancora aperti e non contribuiscono a win rate/P&L."
    )

    return "\n".join(lines)


def send_weekly_email(report_text: str) -> None:
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        print("EMAIL_SENDER/EMAIL_PASSWORD non configurati — report stampato solo su console.")
        print(report_text)
        return

    msg = MIMEMultipart()
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg["Subject"] = f"[Trading Agent] Report settimanale — {datetime.now().strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(report_text, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        print("Report settimanale inviato con successo.")
    except Exception as e:
        print(f"Invio report settimanale fallito: {e}")


def main():
    mem = load_memory()
    report_text = build_report_text(mem)
    send_weekly_email(report_text)


if __name__ == "__main__":
    main()

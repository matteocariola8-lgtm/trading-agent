import os
import re
import base64
from datetime import date, datetime
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL")
RECIPIENT         = "matteo.cariola8@gmail.com"
TOKEN_PATH        = os.getenv("GOOGLE_TOKEN_PATH", "token.json")

_alpaca_headers = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "accept": "application/json",
}


def _gmail_service():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_todays_trades(log_path="trades.log") -> list[dict]:
    today = date.today().isoformat()
    trades = []
    try:
        with open(log_path) as f:
            for line in f:
                if today not in line or "ORDER PLACED" not in line:
                    continue
                m = re.search(r"ORDER PLACED \| (\w+) (\d+) (\w+) \| order_id=(\S+)", line)
                if m:
                    trades.append({
                        "time":     line[:23],
                        "side":     m.group(1),
                        "qty":      int(m.group(2)),
                        "symbol":   m.group(3),
                        "order_id": m.group(4),
                    })
    except FileNotFoundError:
        pass
    return trades


def _get_account() -> dict:
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/account", headers=_alpaca_headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _get_positions() -> list[dict]:
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/positions", headers=_alpaca_headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def _build_html(account: dict, positions: list[dict], trades: list[dict]) -> str:
    equity      = float(account.get("equity",      0))
    last_equity = float(account.get("last_equity", 0))
    cash        = float(account.get("cash",        0))
    pnl         = equity - last_equity
    pnl_pct     = (pnl / last_equity * 100) if last_equity else 0
    pnl_color   = "#22c55e" if pnl >= 0 else "#ef4444"
    sign        = lambda n: "+" if n >= 0 else ""

    def pos_row(p):
        upl    = float(p.get("unrealized_pl",  0))
        uplpct = float(p.get("unrealized_plpc", 0)) * 100
        clr    = "#22c55e" if upl >= 0 else "#ef4444"
        mv     = float(p.get("market_value", 0))
        return f"""<tr>
          <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb">{p['symbol']}</td>
          <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;text-align:right">{p['qty']}</td>
          <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;text-align:right">${mv:,.2f}</td>
          <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;text-align:right;color:{clr}">
            {sign(upl)}${upl:,.2f} ({sign(uplpct)}{uplpct:.2f}%)</td>
        </tr>"""

    def trade_row(t):
        clr = "#22c55e" if t["side"] == "BUY" else "#ef4444"
        return f"""<tr>
          <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;color:#6b7280">{t['time']}</td>
          <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;font-weight:700;color:{clr}">{t['side']}</td>
          <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb">{t['symbol']}</td>
          <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;text-align:right">{t['qty']}</td>
        </tr>"""

    pos_rows   = "".join(pos_row(p) for p in positions)   or \
        '<tr><td colspan="4" style="padding:12px 14px;color:#9ca3af">No open positions</td></tr>'
    trade_rows = "".join(trade_row(t) for t in trades)    or \
        '<tr><td colspan="4" style="padding:12px 14px;color:#9ca3af">No trades today</td></tr>'

    th = "padding:9px 14px;text-align:left;font-size:12px;font-weight:600;color:#6b7280;background:#f9fafb"

    return f"""<html><body style="font-family:Arial,sans-serif;background:#f3f4f6;margin:0;padding:24px">
<div style="max-width:620px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1)">

  <div style="background:#111827;padding:24px 28px">
    <h1 style="color:#fff;margin:0;font-size:20px;font-weight:700">Trading Agent — Daily Report</h1>
    <p  style="color:#9ca3af;margin:4px 0 0;font-size:14px">{date.today().strftime('%A, %d %B %Y')}</p>
  </div>

  <div style="padding:24px 28px">
    <div style="display:flex;gap:12px;margin-bottom:28px">
      <div style="flex:1;background:#f9fafb;border-radius:8px;padding:16px">
        <p style="margin:0;color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:.05em">Portfolio Value</p>
        <p style="margin:6px 0 0;font-size:22px;font-weight:700">${equity:,.2f}</p>
      </div>
      <div style="flex:1;background:#f9fafb;border-radius:8px;padding:16px">
        <p style="margin:0;color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:.05em">Daily P&L</p>
        <p style="margin:6px 0 0;font-size:22px;font-weight:700;color:{pnl_color}">
          {sign(pnl)}${pnl:,.2f}<span style="font-size:14px;font-weight:400"> ({sign(pnl_pct)}{pnl_pct:.2f}%)</span></p>
      </div>
      <div style="flex:1;background:#f9fafb;border-radius:8px;padding:16px">
        <p style="margin:0;color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:.05em">Cash</p>
        <p style="margin:6px 0 0;font-size:22px;font-weight:700">${cash:,.2f}</p>
      </div>
    </div>

    <h2 style="font-size:14px;font-weight:700;color:#111827;margin:0 0 10px">Open Positions</h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:24px">
      <thead><tr>
        <th style="{th}">Symbol</th><th style="{th};text-align:right">Qty</th>
        <th style="{th};text-align:right">Market Value</th><th style="{th};text-align:right">Unrealized P&L</th>
      </tr></thead>
      <tbody>{pos_rows}</tbody>
    </table>

    <h2 style="font-size:14px;font-weight:700;color:#111827;margin:0 0 10px">Trades Today ({len(trades)})</h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px">
      <thead><tr>
        <th style="{th}">Time</th><th style="{th}">Side</th>
        <th style="{th}">Symbol</th><th style="{th};text-align:right">Qty</th>
      </tr></thead>
      <tbody>{trade_rows}</tbody>
    </table>
  </div>

  <div style="padding:16px 28px;background:#f9fafb;border-top:1px solid #e5e7eb">
    <p style="margin:0;font-size:12px;color:#9ca3af">Generated by Trading Agent · {datetime.now().strftime('%H:%M')} ET</p>
  </div>
</div>
</body></html>"""


def send_report():
    account   = _get_account()
    positions = _get_positions()
    trades    = get_todays_trades()
    html      = _build_html(account, positions, trades)

    msg = MIMEText(html, "html")
    msg["to"]      = RECIPIENT
    msg["subject"] = f"Trading Agent Report — {date.today().strftime('%d %b %Y')}"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    _gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()


if __name__ == "__main__":
    send_report()
    print("Report sent.")

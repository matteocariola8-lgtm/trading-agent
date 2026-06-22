import os
import time
import json
import logging
import requests
from datetime import datetime, date
from dotenv import load_dotenv
import anthropic

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

ETFS = ["SPY", "QQQ", "GLD", "TLT", "UUP"]
LOOP_INTERVAL = 60  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("trades.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

alpaca_headers = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "accept": "application/json",
    "content-type": "application/json",
}

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def get_etf_prices() -> dict[str, float | None]:
    symbols = ",".join(ETFS)
    url = f"{ALPACA_BASE_URL}/stocks/quotes/latest?symbols={symbols}&feed=iex"
    try:
        resp = requests.get(url, headers=alpaca_headers, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("quotes", {})
        return {sym: (q.get("ap") or q.get("bp")) for sym, q in data.items()}
    except Exception as e:
        log.error(f"Error fetching prices: {e}")
        return {sym: None for sym in ETFS}


def get_positions() -> dict[str, float]:
    url = f"{ALPACA_BASE_URL}/positions"
    try:
        resp = requests.get(url, headers=alpaca_headers, timeout=10)
        resp.raise_for_status()
        return {p["symbol"]: float(p["qty"]) for p in resp.json()}
    except Exception as e:
        log.error(f"Error fetching positions: {e}")
        return {}


def get_account_cash() -> float:
    url = f"{ALPACA_BASE_URL}/account"
    try:
        resp = requests.get(url, headers=alpaca_headers, timeout=10)
        resp.raise_for_status()
        return float(resp.json().get("cash", 0))
    except Exception as e:
        log.error(f"Error fetching account: {e}")
        return 0.0


def get_market_news() -> list[dict]:
    today = date.today().isoformat()
    url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_API_KEY}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        items = resp.json()[:10]
        return [{"headline": i.get("headline", ""), "summary": i.get("summary", "")} for i in items]
    except Exception as e:
        log.error(f"Error fetching news: {e}")
        return []


def ask_claude(prices: dict, positions: dict, cash: float, news: list[dict]) -> dict[str, dict]:
    news_text = "\n".join(f"- {n['headline']}" for n in news) or "No news available."
    prices_text = "\n".join(
        f"  {sym}: ${price:.2f}" if price else f"  {sym}: N/A"
        for sym, price in prices.items()
    )
    positions_text = "\n".join(
        f"  {sym}: {qty} shares" for sym, qty in positions.items()
    ) or "  None"

    prompt = f"""You are a paper trading agent managing an ETF portfolio. Analyze the data below and decide an action for each ETF.

Available cash: ${cash:.2f}

Current ETF prices:
{prices_text}

Current positions:
{positions_text}

Recent market news:
{news_text}

For each ETF ({', '.join(ETFS)}), respond with a JSON object in this exact format:
{{
  "SPY": {{"action": "BUY"|"SELL"|"HOLD", "qty": <integer shares>, "reason": "<brief reason>"}},
  "QQQ": {{"action": "BUY"|"SELL"|"HOLD", "qty": <integer shares>, "reason": "<brief reason>"}},
  "GLD": {{"action": "BUY"|"SELL"|"HOLD", "qty": <integer shares>, "reason": "<brief reason>"}},
  "TLT": {{"action": "BUY"|"SELL"|"HOLD", "qty": <integer shares>, "reason": "<brief reason>"}},
  "UUP": {{"action": "BUY"|"SELL"|"HOLD", "qty": <integer shares>, "reason": "<brief reason>"}}
}}

Rules:
- BUY qty must leave enough cash for the purchase (price * qty <= available cash / number of buys)
- SELL qty must not exceed current position size
- HOLD always has qty 0
- Keep individual position sizes modest (1-5 shares per trade) given this is paper trading
- Respond ONLY with the JSON object, no other text."""

    try:
        message = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Claude returned invalid JSON: {e}\nRaw: {raw}")
        return {sym: {"action": "HOLD", "qty": 0, "reason": "parse error"} for sym in ETFS}
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return {sym: {"action": "HOLD", "qty": 0, "reason": "api error"} for sym in ETFS}


def place_order(symbol: str, action: str, qty: int) -> bool:
    if qty <= 0:
        return False
    url = f"{ALPACA_BASE_URL}/orders"
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": action.lower(),
        "type": "market",
        "time_in_force": "day",
    }
    try:
        resp = requests.post(url, headers=alpaca_headers, json=payload, timeout=10)
        resp.raise_for_status()
        order = resp.json()
        log.info(f"ORDER PLACED | {action} {qty} {symbol} | order_id={order.get('id')}")
        return True
    except requests.HTTPError as e:
        log.error(f"Order failed {action} {qty} {symbol}: {e} | {resp.text}")
        return False
    except Exception as e:
        log.error(f"Order error {action} {qty} {symbol}: {e}")
        return False


def is_market_open() -> bool:
    url = f"{ALPACA_BASE_URL}/clock"
    try:
        resp = requests.get(url, headers=alpaca_headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("is_open", False)
    except Exception as e:
        log.error(f"Error checking market clock: {e}")
        return False


def run_cycle():
    log.info("=== Starting trading cycle ===")

    if not is_market_open():
        log.info("Market is closed — skipping cycle.")
        return

    prices = get_etf_prices()
    positions = get_positions()
    cash = get_account_cash()

    log.info(f"Cash: ${cash:.2f} | Positions: {positions} | Prices: {prices}")

    news = get_market_news()
    decisions = ask_claude(prices, positions, cash, news)

    for symbol, decision in decisions.items():
        action = decision.get("action", "HOLD").upper()
        qty = int(decision.get("qty", 0))
        reason = decision.get("reason", "")
        log.info(f"DECISION | {symbol}: {action} {qty} shares — {reason}")

        if action in ("BUY", "SELL") and qty > 0:
            place_order(symbol, action, qty)

    log.info("=== Cycle complete ===\n")


def main():
    log.info("Trading agent started.")
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

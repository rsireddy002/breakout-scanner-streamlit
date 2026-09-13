"""
run_scan.py — Standalone entry point for the GitHub Actions cron job.

Runs one scan pass over the current F&O universe and sends a Telegram
alert for any signal not already alerted. No Streamlit involved — this is
meant to run headless, on a schedule, via automation.

Run:
    python run_scan.py
"""

import json
import os
from datetime import datetime
from datetime import time as dtime

from upstox_screener import (
    IST,
    format_signal_message,
    get_fno_tickers,
    scan_all,
    send_telegram_message,
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALERTED_SIGNALS_FILE = os.path.join(_BASE_DIR, "alerted_signals.json")

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def is_market_hours(now=None):
    """True during NSE trading hours (IST), Mon-Fri. Does not account for
    market holidays — the schedule will just find nothing to alert on and
    exit quickly on those days."""
    now = now or datetime.now(tz=IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def load_alerted_signals():
    if os.path.exists(ALERTED_SIGNALS_FILE):
        with open(ALERTED_SIGNALS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_alerted_signals(signals):
    # Cap the file size — keep only the most recent 500 keys.
    trimmed = sorted(signals)[-500:]
    with open(ALERTED_SIGNALS_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


def main():
    if not is_market_hours():
        print("Outside market hours (IST) — skipping scan.")
        return

    tickers = get_fno_tickers()
    print(f"Scanning {len(tickers)} F&O tickers...")
    results, errors = scan_all(tickers)

    alerted = load_alerted_signals()
    new_count = 0
    for r in results:
        key = f"{r['ticker']}|{r['signal_time']}"
        if key not in alerted:
            try:
                send_telegram_message(format_signal_message(r))
                new_count += 1
            except Exception as exc:
                print(f"Telegram send failed for {r['ticker']}: {exc}")
            alerted.add(key)

    save_alerted_signals(alerted)

    print(
        f"Scan complete: {len(results)} signal(s) found, "
        f"{new_count} new alert(s) sent, {len(errors)} ticker error(s)."
    )
    if errors:
        for e in errors[:10]:
            print(" -", e)


if __name__ == "__main__":
    main()
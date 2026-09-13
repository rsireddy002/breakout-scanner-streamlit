# Breakout Scanner (Streamlit, Upstox, NSE)

Python/Streamlit rewrite of the breakout screener — same Upstox-backed
screening logic as the Node/React build, no Node or React involved.

## Project layout

```
breakout-scanner-streamlit/
├── app.py               # Streamlit UI — header, results table, chart panel
├── upstox_screener.py   # Upstox API calls + the breakout screen itself
├── requirements.txt
├── .env.example         # copy to .env and paste your Upstox token in
└── .gitignore
```

## Setup

```powershell
cd breakout-scanner-streamlit
python -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

In `.env`, set `UPSTOX_ACCESS_TOKEN=your_actual_token`. Upstox tokens expire
daily — paste a fresh one in each morning before running.

## Run

```powershell
python -m streamlit run app.py
```

Streamlit will open your browser to `http://localhost:8501` automatically.
Click **Scan Now** to run the screen.

## How the screen works

Same logic as the Node version, ported directly:

- 4h candles come straight from Upstox's V3 historical API
  (`unit=hours, interval=4`) — no manual aggregation from 1h data.
- Daily candles (`unit=days, interval=1`) back the liquidity, price-level,
  and trend checks.
- Ticker symbols resolve to Upstox instrument keys via
  `/v2/instruments/search` (filtered to `NSE_EQ`), cached in memory for the
  life of the process.
- Both candle fetchers drop the most recent candle if it isn't actually
  complete yet (a 4h window not yet closed, or today's still-open daily
  session).

Checks enforced, in order:

1. Consolidation range ≤ 12% over the prior 10 completed 4h candles
2. Close ≥ 2% above the consolidation high
3. Candle body ≥ 5% of the open
4. Volume ≥ 1.5× the prior 10-candle average
5. 20-day average daily volume ≥ 500,000
6. ~~Market cap~~ — **dropped.** Upstox doesn't expose fundamentals data.
   Numbering is kept as-is (skips 6) to match the original spec.
7. Close within 10% of the 20-day or 50-day high
8. Close above both the 20-day and 50-day SMA

## Differences from the Node/React version

- One process instead of two — no separate backend/frontend, no proxy config.
- Scan errors (expired token, a ticker that fails to resolve, etc.) are
  surfaced per-ticker in an expandable panel under the results table, rather
  than being silently swallowed — worth checking if a scan comes back with
  fewer results than expected.
- Chart is Plotly (matching your other Streamlit dashboards) instead of
  lightweight-charts — same visual idea: breakout candle in emerald,
  consolidation range as a rose-tinted horizontal band.
- Uses `ThreadPoolExecutor` (4 workers) for concurrent ticker scanning
  instead of the Node backend's manual concurrency limiter.

## Notes

- The ticker universe lives in `INDIA_TICKERS` in `upstox_screener.py`.
- This tool is for educational and research purposes only — not financial
  advice.

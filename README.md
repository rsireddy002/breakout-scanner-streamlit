# Breakout Scanner (Streamlit, Upstox, NSE F&O)

Python/Streamlit breakout screener for the full NSE F&O universe, backed
entirely by the Upstox V3 API, with optional Telegram alerts and a
GitHub Actions cron job for fully headless operation.

## Project layout

## Setup (local)

```powershell
cd breakout-scanner-streamlit
python -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

In `.env`, set:
- `UPSTOX_ACCESS_TOKEN` — use an **Analytics Token**, not a standard daily
  OAuth token. Generate it once from account.upstox.com → Developer Apps →
  your app → **Analytics** tab → Generate Token. It's read-only, valid for
  **1 year**, and needs no daily refresh — set it once here and in the
  other two places below (Streamlit Cloud secrets, GitHub Actions secret)
  and you're done until it expires next year.
  **Only one Analytics Token can be active per account at a time** —
  generating a new one silently revokes the current one and breaks all
  three deployments until updated. Don't regenerate unless you actually
  need to.
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional. Leave blank to
  disable Telegram alerts entirely; nothing else breaks if they're unset.

## Run (local, interactive)

```powershell
python -m streamlit run app.py
```

Opens `http://localhost:8501`. Click **Scan Now** to run the screen.

## How the screen works

Runs on **5-minute intraday candles** (recalibrated from an earlier 4h-candle
version — see `upstox_screener.py`'s tunable constants at the top of the
file for the exact numbers and reasoning):

- Intraday candles come straight from Upstox's V3 historical API
  (`unit=minutes, interval=5`) — no manual aggregation.
- Daily candles (`unit=days, interval=1`) back the liquidity, price-level,
  and trend checks — these run on daily data regardless of the intraday
  entry timeframe.
- The ticker universe is the **full current F&O list** (~200 stocks),
  built dynamically from Upstox's public instrument master and cached
  locally for 24h (`fno_universe_cache.json`) — not a hardcoded list, so
  it self-corrects through symbol renames and contract changes (e.g. the
  Tata Motors demerger into TMPV/TMCV).
- Ticker symbols resolve to Upstox instrument keys via
  `/v2/instruments/search` (filtered to `NSE_EQ`), cached in memory for
  the life of the process.
- Both candle fetchers drop the most recent candle if it isn't actually
  complete yet.

Checks enforced (numbering kept from the original 8-check spec — check 6,
market cap, is dropped since Upstox doesn't expose fundamentals data):

1. Consolidation range ≤ 1.5% over the prior 10 completed candles
2. Close ≥ 0.3% above the consolidation high
3. Candle body ≥ 0.3% of the open
4. Volume ≥ 1.5× the prior 10-candle average
5. 20-day average daily volume ≥ 500,000
7. Close within 10% of the 20-day or 50-day high
8. Close above both the 20-day and 50-day SMA

Each passing result includes `signal_time` — the breakout candle's own
timestamp, shown in the table and in Telegram alerts, so a signal is
never confused with the time it was merely re-displayed.

## Telegram alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (reuses the existing
`Dryarapureddy_bot` setup from `llm-gatekeeper`) and every new signal gets
pushed automatically. Alerts are deduped by `ticker + signal_time` so the
same signal doesn't re-fire on every scan — in the Streamlit app, dedup
state lives in the browser session; in the GitHub Actions automation, it's
persisted to `alerted_signals.json` in the repo.

## Cloud deployment (Streamlit Community Cloud)

Deployed from this repo directly — connect it at share.streamlit.io,
main file `app.py`, and set the same three variables in **Advanced
settings → Secrets** (TOML format) instead of `.env`:

```toml
UPSTOX_ACCESS_TOKEN = "..."
TELEGRAM_BOT_TOKEN = "..."
TELEGRAM_CHAT_ID = "..."
```

With the Analytics Token, this is a one-time setup — no daily secret
updates required.

Note: the deployed app only scans when someone opens it and clicks
**Scan Now** — there's no background polling inside the Streamlit app
itself. For actual hands-off scanning, see the GitHub Actions automation
below.

## Fully automated scanning (GitHub Actions)

`.github/workflows/scan.yml` runs `run_scan.py` on a schedule
(`*/5 3-10 * * 1-5` — every 5 minutes, roughly market hours, Mon-Fri,
UTC), independent of the Streamlit app entirely. The script itself checks
IST market hours precisely and exits immediately outside them.

**Setup:** add three repository secrets (Settings → Secrets and variables
→ Actions → New repository secret): `UPSTOX_ACCESS_TOKEN` (the Analytics
Token — set once, valid a year), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

For manual testing outside market hours, trigger the workflow from the
Actions tab (**Run workflow**) with the **"Run even outside market
hours"** checkbox ticked — this sets `FORCE_SCAN=true`, bypassing the
IST market-hours check for that one run.

Each run persists `fno_universe_cache.json` (so the ~30-50MB instrument
master isn't re-downloaded every 5 minutes) and `alerted_signals.json`
(dedup state) back to the repo as commits by `github-actions[bot]`.

**Caveat:** GitHub Actions scheduled runs are best-effort, not guaranteed
to fire exactly on time — GitHub's own docs note runs can be delayed or
occasionally skipped during high load. Treat "every 5 minutes" as a
target cadence, not a hard guarantee.

## Notes

- The ticker universe logic lives in `get_fno_tickers()` /
  `_build_fno_tickers()` in `upstox_screener.py`.
- This tool is for educational and research purposes only — not financial
  advice.
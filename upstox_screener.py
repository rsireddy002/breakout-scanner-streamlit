"""
upstox_screener.py — Breakout screening logic backed by the Upstox V3 API.

Runs on 5-minute intraday candles (recalibrated from the original 4h-candle
spec — thresholds for checks 1-4 are scaled down for intraday volatility;
checks 5, 7, 8 run on daily candles and are unaffected by the entry
timeframe). Check 6 (market cap) is intentionally dropped — Upstox doesn't
expose fundamentals data. The gap in numbering is kept on purpose so it
still maps back to the original 8-check spec.
"""

import gzip
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

UPSTOX_BASE = "https://api.upstox.com"
IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Tunable parameters — adjust these as you validate/backtest, no need to
# touch the logic below.
# ---------------------------------------------------------------------------

CANDLE_UNIT = "minutes"
CANDLE_INTERVAL = 5  # Upstox V3 supports 1-300 for the "minutes" unit

CONSOLIDATION_LOOKBACK = 10       # prior completed candles used for the range
CONSOLIDATION_MAX_RANGE_PCT = 1.5  # check 1 — was 12% at 4h
BREAKOUT_ABOVE_RANGE_PCT = 0.3     # check 2 — was 2% at 4h
BREAKOUT_SIZE_MIN_PCT = 0.3        # check 3 — was 5% at 4h; matches the
                                   # 0.3% min_move validated in the morning
                                   # fade strategy backtest
RELATIVE_VOLUME_MIN = 1.5          # check 4 — unchanged; matches the RVOL
                                   # >=150% threshold used in fno-scanner
LIQUIDITY_MIN_AVG_VOL = 500_000    # check 5 — daily-based, timeframe-agnostic
PRICE_LEVEL_WITHIN_PCT = 10        # check 7 — daily-based, timeframe-agnostic

# ---------------------------------------------------------------------------
# F&O universe — dynamically built from Upstox's public instrument master,
# same approach as fno_universe.py in fno-scanner-strategy-update: every
# stock with a live NSE_FO futures contract, cross-referenced against NSE_EQ
# for its cash trading symbol. Cached locally for 24h (F&O list changes
# rarely outside contract review cycles).
# ---------------------------------------------------------------------------

INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
FNO_INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FNO_UNIVERSE_CACHE_FILE = os.path.join(_BASE_DIR, "fno_universe_cache.json")
FNO_UNIVERSE_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


def _download_instrument_master():
    """Download and parse Upstox's public instrument master (gzip JSON, no auth needed)."""
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=120)
    resp.raise_for_status()
    return json.loads(gzip.decompress(resp.content))


def _build_fno_tickers(instruments):
    """From the full instrument list, derive the set of NSE cash trading
    symbols that currently have a live F&O futures contract."""
    fo_underlying_names = set()
    for inst in instruments:
        if inst.get("segment") == "NSE_FO" and inst.get("instrument_type") == "FUT":
            name = (inst.get("name") or "").upper()
            if name and name not in FNO_INDEX_NAMES:
                fo_underlying_names.add(name)

    tickers = set()
    for inst in instruments:
        if inst.get("segment") == "NSE_EQ" and inst.get("instrument_type") == "EQ":
            name = (inst.get("name") or "").upper()
            if name in fo_underlying_names:
                trading_symbol = inst.get("trading_symbol")
                if trading_symbol:
                    tickers.add(trading_symbol)

    return sorted(tickers)


def get_fno_tickers(force_refresh=False):
    """Load the current F&O ticker universe, using the local 24h cache when fresh."""
    if not force_refresh and os.path.exists(FNO_UNIVERSE_CACHE_FILE):
        with open(FNO_UNIVERSE_CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        age = time.time() - cached.get("generated_at", 0)
        if age < FNO_UNIVERSE_CACHE_MAX_AGE_SECONDS and cached.get("tickers"):
            return cached["tickers"]

    instruments = _download_instrument_master()
    tickers = _build_fno_tickers(instruments)

    with open(FNO_UNIVERSE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.time(), "tickers": tickers}, f)

    return tickers


_instrument_key_cache = {}


class UpstoxError(Exception):
    """Raised for any Upstox API failure (auth, rate limit, bad response)."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_token():
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise UpstoxError(
            "UPSTOX_ACCESS_TOKEN is not set — copy .env.example to .env and paste a token in"
        )
    return token


def _upstox_get(url, params=None, max_retries=4):
    token = _get_token()
    for attempt in range(max_retries + 1):
        resp = requests.get(
            url,
            params=params,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 401:
            raise UpstoxError(
                "Upstox access token expired or invalid — refresh UPSTOX_ACCESS_TOKEN in .env"
            )
        if resp.status_code == 429:
            if attempt == max_retries:
                raise UpstoxError(f"Upstox API error {resp.status_code}: {resp.text[:200]}")
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else (2 ** attempt)
            time.sleep(min(wait, 30))
            continue
        if not resp.ok:
            raise UpstoxError(f"Upstox API error {resp.status_code}: {resp.text[:200]}")

        payload = resp.json()
        if payload.get("status") != "success":
            raise UpstoxError(f"Upstox API returned an error: {str(payload)[:200]}")
        return payload


def ist_date_string(dt=None):
    """Format a datetime as YYYY-MM-DD in IST, regardless of the machine's own timezone."""
    if dt is None:
        dt = datetime.now(tz=IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(IST)
    else:
        dt = dt.astimezone(IST)
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Instrument key resolution (NSE equities only), cached per process.
# ---------------------------------------------------------------------------

def resolve_instrument_key(symbol):
    if symbol in _instrument_key_cache:
        return _instrument_key_cache[symbol]

    url = f"{UPSTOX_BASE}/v2/instruments/search"
    payload = _upstox_get(url, params={"query": symbol, "exchanges": "NSE", "segments": "EQ"})
    matches = payload.get("data", [])

    exact = next(
        (
            m for m in matches
            if m.get("segment") == "NSE_EQ" and (m.get("trading_symbol") or "").upper() == symbol.upper()
        ),
        None,
    )
    if not exact:
        raise UpstoxError(f'Could not resolve an NSE_EQ instrument key for "{symbol}"')

    _instrument_key_cache[symbol] = exact["instrument_key"]
    return exact["instrument_key"]


# ---------------------------------------------------------------------------
# Candle fetching (native intraday candles via Upstox V3 — no manual aggregation)
# ---------------------------------------------------------------------------

def _fetch_candles(instrument_key, unit, interval, from_date, to_date):
    url = f"{UPSTOX_BASE}/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
    payload = _upstox_get(url)
    rows = (payload.get("data") or {}).get("candles") or []

    # Each row: [timestamp, open, high, low, close, volume, open_interest]
    candles = [
        {
            "date": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
        }
        for row in rows
    ]
    candles.sort(key=lambda c: c["date"])  # Upstox returns newest-first
    return candles


def fetch_intraday_candles(instrument_key):
    """Native intraday candles (CANDLE_UNIT/CANDLE_INTERVAL) for the last ~10
    calendar days, with any still-forming candle dropped. 10 days is plenty
    of buffer past weekends/holidays for a 10-candle lookback; Upstox's V3
    API allows up to a month of history for minute intervals <=15."""
    to_date = ist_date_string()
    from_date = ist_date_string(datetime.now(tz=IST) - timedelta(days=10))
    candles = _fetch_candles(instrument_key, CANDLE_UNIT, CANDLE_INTERVAL, from_date, to_date)

    now = datetime.now(tz=IST)
    while candles:
        last = candles[-1]
        start = datetime.fromisoformat(last["date"])
        end = start + timedelta(minutes=CANDLE_INTERVAL)
        if end > now:
            candles.pop()  # still-forming candle — not "completed" yet
        else:
            break
    return candles


def fetch_daily_candles(instrument_key):
    """Daily candles for the last ~200 calendar days, excluding today's still-open session."""
    to_date = ist_date_string()
    from_date = ist_date_string(datetime.now(tz=IST) - timedelta(days=200))
    candles = _fetch_candles(instrument_key, "days", 1, from_date, to_date)

    today = ist_date_string()
    return [c for c in candles if ist_date_string(datetime.fromisoformat(c["date"])) != today]


# ---------------------------------------------------------------------------
# Screening logic
# ---------------------------------------------------------------------------

def screen_ticker(symbol):
    """Run the breakout screen on one ticker. Returns a stats dict, or None if
    it fails any check. Raises UpstoxError for real API failures (auth, etc.)
    so those don't get silently confused with "didn't pass"."""
    instrument_key = resolve_instrument_key(symbol)

    # --- Intraday candles ---------------------------------------------------------
    candles = fetch_intraday_candles(instrument_key)
    if len(candles) < CONSOLIDATION_LOOKBACK + 1:
        return None

    current = candles[-1]
    lookback = candles[-(CONSOLIDATION_LOOKBACK + 1):-1]  # prior N completed candles

    # Check 1: Consolidation
    consolidation_high = max(max(c["open"], c["close"]) for c in lookback)
    consolidation_low = min(min(c["open"], c["close"]) for c in lookback)
    consolidation_range_pct = (consolidation_high - consolidation_low) / consolidation_low * 100
    if consolidation_range_pct > CONSOLIDATION_MAX_RANGE_PCT:
        return None

    # Check 2: Breakout above range
    if current["close"] < consolidation_high * (1 + BREAKOUT_ABOVE_RANGE_PCT / 100):
        return None
    breakout_pct = (current["close"] - consolidation_high) / consolidation_high * 100

    # Check 3: Breakout size
    breakout_size_pct = abs(current["close"] - current["open"]) / current["open"] * 100
    if breakout_size_pct < BREAKOUT_SIZE_MIN_PCT:
        return None

    # Check 4: Relative volume
    avg_vol_lookback = sum(c["volume"] for c in lookback) / len(lookback)
    if avg_vol_lookback <= 0:
        return None
    relative_volume = current["volume"] / avg_vol_lookback
    if relative_volume < RELATIVE_VOLUME_MIN:
        return None

    # --- Daily candles for checks 5, 7, 8 ----------------------------------------
    daily_candles = fetch_daily_candles(instrument_key)
    if len(daily_candles) < 50:
        return None

    # Check 5: Liquidity
    last_20_vol = [c["volume"] for c in daily_candles[-20:]]
    avg_vol_20 = sum(last_20_vol) / len(last_20_vol)
    if avg_vol_20 < LIQUIDITY_MIN_AVG_VOL:
        return None

    # Check 6 (market cap) intentionally omitted — not available via Upstox.

    # Check 7: Price level
    closes = [c["close"] for c in daily_candles]
    high_20d = max(closes[-20:])
    high_50d = max(closes[-50:])
    price_level_floor = 1 - PRICE_LEVEL_WITHIN_PCT / 100
    within_high_20 = current["close"] >= high_20d * price_level_floor
    within_high_50 = current["close"] >= high_50d * price_level_floor
    if not within_high_20 and not within_high_50:
        return None
    pct_from_high_20 = (current["close"] - high_20d) / high_20d * 100
    pct_from_high_50 = (current["close"] - high_50d) / high_50d * 100
    pct_from_high = (
        pct_from_high_20 if abs(pct_from_high_20) <= abs(pct_from_high_50) else pct_from_high_50
    )

    # Check 8: Trend
    sma_20 = sum(closes[-20:]) / 20
    sma_50 = sum(closes[-50:]) / 50
    if not (current["close"] > sma_20 and current["close"] > sma_50):
        return None

    return {
        "ticker": symbol,
        "price": current["close"],
        "breakout_pct": breakout_pct,
        "breakout_size_pct": breakout_size_pct,
        "relative_volume": relative_volume,
        "pct_from_high": pct_from_high,
        "consolidation_high": consolidation_high,
        "consolidation_low": consolidation_low,
        "signal_time": current["date"],
    }


def send_telegram_message(text):
    """Send a Telegram alert via the bot API, if TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID are configured. No-ops (returns False) if either is
    missing, so Telegram alerts are entirely optional. Raises on a genuine
    send failure so the caller can decide how to surface it."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
    resp.raise_for_status()
    return True


def format_signal_message(result):
    """Render one breakout result as a Telegram-friendly message."""
    signal_dt = datetime.fromisoformat(result["signal_time"])
    signal_str = signal_dt.strftime("%b %d, %I:%M %p")
    return (
        f"\U0001F680 *Breakout: {result['ticker']}*\n"
        f"Price: {result['price']:.2f}\n"
        f"Breakout: +{result['breakout_pct']:.2f}% above range\n"
        f"Candle body: {result['breakout_size_pct']:.2f}%\n"
        f"RVOL: {result['relative_volume']:.2f}x\n"
        f"Signal time: {signal_str}"
    )


def scan_all(tickers=None, max_workers=3):
    """Screen every ticker concurrently (thread pool — this is I/O bound).
    Defaults to the current full F&O universe (~200 stocks) if no list is
    given. Lower concurrency (3) than you might expect — Upstox sits behind
    Cloudflare, which rate-limits bursty request patterns from a single IP
    more aggressively than the origin API itself does; combined with the
    retry-with-backoff in _upstox_get, this keeps a fresh IP (e.g. a new
    EC2 instance's first scan) from tripping Cloudflare's edge limits.
    Returns (passing_results, error_messages). Passing results are sorted
    by relative volume, descending, capped at 50."""
    tickers = tickers if tickers is not None else get_fno_tickers()
    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_symbol = {executor.submit(screen_ticker, t): t for t in tickers}
        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
            except UpstoxError as exc:
                errors.append(f"{symbol}: {exc}")
            except Exception as exc:  # noqa: BLE001 — surface anything unexpected per-ticker
                errors.append(f"{symbol}: {exc}")

    results.sort(key=lambda r: r["relative_volume"], reverse=True)
    return results[:50], errors


def get_chart_data(symbol):
    """Intraday candle window + consolidation range for one ticker, for charting."""
    instrument_key = resolve_instrument_key(symbol)
    candles = fetch_intraday_candles(instrument_key)

    if len(candles) < CONSOLIDATION_LOOKBACK + 1:
        raise UpstoxError("Not enough intraday candle data for this ticker")

    lookback = candles[-(CONSOLIDATION_LOOKBACK + 1):-1]
    consolidation_high = max(max(c["open"], c["close"]) for c in lookback)
    consolidation_low = min(min(c["open"], c["close"]) for c in lookback)

    # ~1.3 trading sessions of 5-min bars for visual context around the breakout.
    window = candles[-100:]
    breakout_index = len(window) - 1

    return {
        "candles": window,
        "consolidation_high": consolidation_high,
        "consolidation_low": consolidation_low,
        "breakout_index": breakout_index,
    }
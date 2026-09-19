"""
app.py — Breakout Scanner (Streamlit, Upstox-backed, NSE only)

Setup:
    python -m pip install -r requirements.txt
    copy .env.example .env   (then paste a fresh UPSTOX_ACCESS_TOKEN in)

Run:
    python -m streamlit run app.py
"""

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from upstox_screener import (
    IST,
    UpstoxError,
    format_signal_message,
    get_chart_data,
    get_fno_tickers,
    scan_all,
    send_telegram_message,
)

st.set_page_config(page_title="Breakout Scanner", layout="wide")

# --- Dark theme styling (slate / emerald / rose, matching the original design) ---
st.markdown(
    """
    <style>
    .stApp { background-color: #0f172a; color: #e2e8f0; }
    .scanner-title { font-size: 1.6rem; font-weight: 600; color: #f1f5f9; }
    .scanner-footer { text-align: center; color: #64748b; font-size: 0.8rem; margin-top: 2rem;
                       padding-top: 1rem; border-top: 1px solid #334155; }
    div.stButton > button {
        background: linear-gradient(to right, #10b981, #14b8a6);
        color: white; border: none; border-radius: 999px; padding: 0.5rem 1.5rem; font-weight: 600;
    }
    div.stButton > button:hover { opacity: 0.9; color: white; }
    </style>
    """,
    unsafe_allow_html=True,
)

if "results" not in st.session_state:
    st.session_state.results = []
if "last_scanned" not in st.session_state:
    st.session_state.last_scanned = None
if "scan_errors" not in st.session_state:
    st.session_state.scan_errors = []
if "alerted_signals" not in st.session_state:
    st.session_state.alerted_signals = set()

# --- Header -------------------------------------------------------------------
col_title, col_scan = st.columns([4, 1])
with col_title:
    st.markdown('<div class="scanner-title">Breakout Scanner</div>', unsafe_allow_html=True)
    if st.session_state.last_scanned:
        st.caption(f"Last scanned: {st.session_state.last_scanned}")
with col_scan:
    scan_clicked = st.button("Scan Now", use_container_width=True)

if scan_clicked:
    with st.spinner("Loading current F&O universe…"):
        try:
            tickers = get_fno_tickers()
        except Exception as exc:
            st.error(f"Could not load the F&O universe: {exc}")
            tickers = None

    if tickers:
        with st.spinner(f"Scanning {len(tickers)} F&O tickers…"):
            results, errors = scan_all(tickers)
        st.session_state.results = results
        st.session_state.scan_errors = errors
        st.session_state.last_scanned = datetime.now(tz=IST).strftime("%I:%M:%S %p")

        new_alert_count = 0
        for r in results:
            signal_key = f"{r['ticker']}|{r['signal_time']}"
            if signal_key not in st.session_state.alerted_signals:
                try:
                    if send_telegram_message(format_signal_message(r)):
                        new_alert_count += 1
                except Exception:
                    pass  # a Telegram hiccup shouldn't block the scan results
                st.session_state.alerted_signals.add(signal_key)
        if new_alert_count:
            st.toast(f"Sent {new_alert_count} new Telegram alert(s)")

st.divider()

# --- Results table --------------------------------------------------------------
results = st.session_state.results

if not results:
    st.info("No breakouts found — try scanning again.")
else:
    df = pd.DataFrame(results).sort_values("relative_volume", ascending=False)
    signal_dt = pd.to_datetime(df["signal_time"])
    display_df = pd.DataFrame(
        {
            "Ticker": df["ticker"],
            "Signal Time": signal_dt.dt.strftime("%b %d, %I:%M %p"),
            "Price": df["price"].round(2),
            "Breakout %": df["breakout_pct"].map(lambda v: f"+{v:.2f}%"),
            "Breakout Size %": df["breakout_size_pct"].map(lambda v: f"{v:.2f}%"),
            "Relative Volume": df["relative_volume"].map(lambda v: f"{v:.2f}\u00d7"),
            "% from 20d/50d High": df["pct_from_high"].map(lambda v: f"{'+' if v >= 0 else ''}{v:.2f}%"),
        }
    )
    st.dataframe(display_df, use_container_width=True, hide_index=True)

if st.session_state.scan_errors:
    with st.expander(f"{len(st.session_state.scan_errors)} ticker(s) had errors during the scan"):
        for err in st.session_state.scan_errors:
            st.text(err)

st.divider()

# --- Chart panel ------------------------------------------------------------------
st.subheader("Chart")
ticker_options = [r["ticker"] for r in results]

if not ticker_options:
    st.caption("No flagged tickers yet — run a scan.")
else:
    selected = st.selectbox("Select a flagged ticker", ticker_options)

    chart_data = None
    try:
        with st.spinner(f"Loading chart for {selected}…"):
            chart_data = get_chart_data(selected)
    except UpstoxError as exc:
        st.error(str(exc))

    if chart_data:
        candles = chart_data["candles"]
        dates = [c["date"] for c in candles]
        opens = [c["open"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        closes = [c["close"] for c in candles]

        fig = go.Figure()

        fig.add_trace(
            go.Candlestick(
                x=dates, open=opens, high=highs, low=lows, close=closes,
                increasing_line_color="#22c55e", increasing_fillcolor="#22c55e",
                decreasing_line_color="#ef4444", decreasing_fillcolor="#ef4444",
                name=selected,
            )
        )

        # Highlight the breakout candle in emerald, regardless of direction.
        b_idx = chart_data["breakout_index"]
        fig.add_trace(
            go.Candlestick(
                x=[dates[b_idx]], open=[opens[b_idx]], high=[highs[b_idx]],
                low=[lows[b_idx]], close=[closes[b_idx]],
                increasing_line_color="#10b981", increasing_fillcolor="#10b981",
                decreasing_line_color="#10b981", decreasing_fillcolor="#10b981",
                showlegend=False, name="Breakout candle",
            )
        )

        # Consolidation range band (rose, 15% opacity).
        fig.add_hrect(
            y0=chart_data["consolidation_low"], y1=chart_data["consolidation_high"],
            fillcolor="rgba(244, 63, 94, 0.15)", line_width=0,
        )

        fig.update_layout(
            paper_bgcolor="#1e293b",
            plot_bgcolor="#1e293b",
            font_color="#cbd5e1",
            xaxis_rangeslider_visible=False,
            height=500,
            margin=dict(l=20, r=20, t=20, b=20),
            showlegend=False,
        )
        fig.update_xaxes(gridcolor="#334155")
        fig.update_yaxes(gridcolor="#334155")

        st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        c1.metric("Consolidation Low", f"{chart_data['consolidation_low']:.2f}")
        c2.metric("Consolidation High", f"{chart_data['consolidation_high']:.2f}")

st.markdown(
    '<div class="scanner-footer">For educational and research purposes only. Not financial advice.</div>',
    unsafe_allow_html=True,
)

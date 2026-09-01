"""
backtest_analytics.py
---------------------
Shared, strategy-agnostic performance analytics for the optimizer framework.

WHY THIS MODULE EXISTS
----------------------
The drawdown analysis originally lived inside
``backtests/SupertrendBacktestXAUUSD_M1.py`` as two functions:

    calculate_drawdown_episodes(trades_df, initial_capital)
    calculate_drawdown_statistics(episodes_df, last_timestamp=None)

Those are the reference implementation.  This module re-hosts the *same*
algorithm in a reusable place so that any backtest can produce the identical
"Drawdown Analysis" / "Drawdown Episodes" tabs without copy/pasting code.

The single generalisation is that the equity series is no longer hardcoded to
``exit_timestamp`` / ``net_pnl``.  Both the timestamp column and the P&L column
are resolved per-strategy, so engines with a different trade-log shape work
too — for example the NIFTY credit-spread engine, which books rupee P&L on
``profit_with_hedges_inr`` and closes positions on ``exit_timestamp``.

Everything here is additive: existing strategies keep their exact numbers
because the defaults resolve to the same columns they always used.

PUBLIC API
----------
    resolve_series_columns(trades_df, pnl_col=None, time_col=None)
    build_equity_curve(trades_df, initial_capital, ...)      -> DataFrame
    calculate_drawdown_episodes(trades_df, initial_capital, ...) -> (episodes, equity)
    calculate_drawdown_statistics(episodes_df, last_timestamp=None) -> DataFrame
    summarize_drawdown(episodes_df, equity_df)               -> dict
    write_drawdown_sheets(writer, trades_df, initial_capital, ...) -> dict

NORMALISED EQUITY CURVE COLUMNS
-------------------------------
``build_equity_curve`` always emits the same column names regardless of the
strategy's own naming, so downstream consumers (Excel writers, charts, the
optimizer) never have to branch on strategy:

    exit_timestamp | net_pnl | cumulative_pnl | account_balance
                   | running_peak | drawdown_$ | drawdown_%
"""

import numpy as np
import pandas as pd


# =============================================================================
# COLUMN RESOLUTION
# =============================================================================

#: Candidate P&L columns, most specific first.  ``net_pnl`` stays first so the
#: XAUUSD/Supertrend family resolves exactly as it did before this module existed.
DEFAULT_PNL_COLUMNS = (
    "net_pnl",
    "profit_with_hedges_inr",
    "profit_with_hedges_points",
    "profit_in_inr",
    "profit_points",
    "pnl",
    "profit",
    "realized_pnl",
)

#: Candidate close-timestamp columns, most specific first.
DEFAULT_TIME_COLUMNS = (
    "exit_timestamp",
    "exit_time",
    "exit_datetime",
    "close_time",
    "timestamp",
)

#: Episode table schema — kept stable so reports stay comparable across engines.
EPISODE_COLUMNS = [
    "DD #", "Start Datetime", "Peak Equity", "Trough Datetime", "Trough Equity",
    "Drawdown $", "Drawdown %", "Recovery Start", "Recovery End",
    "Drawdown Duration", "Recovery Duration", "Total Episode Duration", "Status",
]

_DURATION_COLUMNS = ("Drawdown Duration", "Recovery Duration", "Total Episode Duration")


def resolve_series_columns(trades_df: pd.DataFrame, pnl_col=None, time_col=None):
    """
    Work out which columns carry the trade close time and the realised P&L.

    Args:
        trades_df: Trade log, one row per closed trade (or per exit event).
        pnl_col:   Explicit P&L column name.  ``None`` = auto-detect.
        time_col:  Explicit timestamp column name.  ``None`` = auto-detect.

    Returns:
        (time_col, pnl_col)

    Raises:
        KeyError if an explicitly requested column is missing, or if
        auto-detection finds no usable candidate.
    """
    if pnl_col is not None and pnl_col not in trades_df.columns:
        raise KeyError(f"P&L column '{pnl_col}' not present in trades_df")
    if time_col is not None and time_col not in trades_df.columns:
        raise KeyError(f"Timestamp column '{time_col}' not present in trades_df")

    if pnl_col is None:
        pnl_col = next((c for c in DEFAULT_PNL_COLUMNS if c in trades_df.columns), None)
    if time_col is None:
        time_col = next((c for c in DEFAULT_TIME_COLUMNS if c in trades_df.columns), None)

    if pnl_col is None:
        raise KeyError(
            "Could not resolve a P&L column. Pass pnl_col= explicitly. "
            f"Tried: {', '.join(DEFAULT_PNL_COLUMNS)}"
        )
    if time_col is None:
        raise KeyError(
            "Could not resolve a timestamp column. Pass time_col= explicitly. "
            f"Tried: {', '.join(DEFAULT_TIME_COLUMNS)}"
        )
    return time_col, pnl_col


# =============================================================================
# EQUITY CURVE
# =============================================================================

def build_equity_curve(
    trades_df: pd.DataFrame,
    initial_capital: float,
    pnl_col=None,
    time_col=None,
    include_seed_row: bool = False,
) -> pd.DataFrame:
    """
    Build a chronological equity curve with running peak and drawdown columns.

    Args:
        trades_df:        Trade log.
        initial_capital:  Starting account balance, in the same unit as the P&L
                          column (dollars for XAUUSD, rupees for the credit
                          spread when ``show_pnl_in_rupees`` is on).
        pnl_col/time_col: See :func:`resolve_series_columns`.
        include_seed_row: When True the synthetic "start of backtest" row that
                          seeds the running peak at ``initial_capital`` is kept
                          in the output.  Episode detection needs it; report
                          tabs do not.

    Returns:
        DataFrame with the normalised columns documented at module level.
        Empty DataFrame (same columns) when there is nothing to plot.
    """
    empty = pd.DataFrame(columns=[
        "exit_timestamp", "net_pnl", "cumulative_pnl",
        "account_balance", "running_peak", "drawdown_$", "drawdown_%",
    ])
    if trades_df is None or trades_df.empty:
        return empty

    time_col, pnl_col = resolve_series_columns(trades_df, pnl_col, time_col)

    eq = trades_df[[time_col, pnl_col]].copy()
    eq.columns = ["exit_timestamp", "net_pnl"]
    eq["exit_timestamp"] = pd.to_datetime(eq["exit_timestamp"], errors="coerce")
    eq["net_pnl"] = pd.to_numeric(eq["net_pnl"], errors="coerce")
    # Trades that never produced a close timestamp or a numeric P&L cannot sit
    # on an equity curve; dropping them keeps the curve monotonic in time.
    eq = eq.dropna(subset=["exit_timestamp", "net_pnl"])
    if eq.empty:
        return empty

    eq = eq.sort_values("exit_timestamp").reset_index(drop=True)
    eq["cumulative_pnl"] = eq["net_pnl"].cumsum()
    eq["account_balance"] = float(initial_capital) + eq["cumulative_pnl"]

    # Prepend the opening balance so cummax anchors on initial_capital rather
    # than on the balance after the first trade.
    initial_row = pd.DataFrame({
        "exit_timestamp": [eq["exit_timestamp"].iloc[0] - pd.Timedelta(seconds=1)],
        "net_pnl": [0.0],
        "cumulative_pnl": [0.0],
        "account_balance": [float(initial_capital)],
    })
    eq = pd.concat([initial_row, eq], ignore_index=True)

    eq["running_peak"] = eq["account_balance"].cummax()
    eq["drawdown_$"] = eq["account_balance"] - eq["running_peak"]
    eq["drawdown_%"] = np.where(
        eq["running_peak"] > 0,
        (eq["account_balance"] - eq["running_peak"]) / eq["running_peak"] * 100.0,
        0.0,
    )

    if include_seed_row:
        return eq
    return eq.iloc[1:].reset_index(drop=True)


# =============================================================================
# DRAWDOWN EPISODES
# =============================================================================

def calculate_drawdown_episodes(
    trades_df: pd.DataFrame,
    initial_capital: float,
    pnl_col=None,
    time_col=None,
):
    """
    Identify every distinct drawdown episode on the chronological equity curve.

    An episode starts when equity falls below the running peak and ends when
    equity recovers to or exceeds that peak.  Nested dips inside an unrecovered
    episode are *not* double-counted — the episode simply tracks the deepest
    trough until recovery.

    This is the reference algorithm from ``SupertrendBacktestXAUUSD_M1.py``,
    unchanged except that the equity series is resolved via
    :func:`build_equity_curve` so any strategy's trade log can drive it.

    Args:
        trades_df:        Trade log.
        initial_capital:  Opening balance, in P&L units.
        pnl_col/time_col: See :func:`resolve_series_columns`.

    Returns:
        (episodes_df, equity_curve_df)
        episodes_df has one row per episode using :data:`EPISODE_COLUMNS`.
        equity_curve_df is the equity curve without the synthetic seed row.
    """
    if trades_df is None or trades_df.empty:
        return pd.DataFrame(columns=EPISODE_COLUMNS), pd.DataFrame()

    eq = build_equity_curve(
        trades_df, initial_capital,
        pnl_col=pnl_col, time_col=time_col, include_seed_row=True,
    )
    if eq.empty:
        return pd.DataFrame(columns=EPISODE_COLUMNS), pd.DataFrame()

    episodes = []
    in_drawdown = False
    dd_num = 0
    peak_equity = 0.0
    peak_datetime = None
    trough_equity = float("inf")
    trough_datetime = None

    balances = eq["account_balance"].values
    peaks = eq["running_peak"].values
    timestamps = eq["exit_timestamp"].values

    for i in range(len(eq)):
        balance = balances[i]
        peak = peaks[i]
        ts = pd.Timestamp(timestamps[i])

        if not in_drawdown:
            if balance < peak - 1e-8:
                # New drawdown episode begins
                in_drawdown = True
                dd_num += 1
                peak_equity = peak
                # Peak datetime = last time account_balance actually sat at the peak
                mask = np.isclose(balances[:i], peak_equity, atol=1e-6)
                if np.any(mask):
                    peak_datetime = pd.Timestamp(timestamps[:i][mask][-1])
                else:
                    peak_datetime = ts
                trough_equity = balance
                trough_datetime = ts
        else:
            if balance < trough_equity:
                trough_equity = balance
                trough_datetime = ts

            if balance >= peak_equity - 1e-8:
                # Recovered
                recovery_end = ts
                dd_dollar = round(trough_equity - peak_equity, 2)
                dd_pct = (
                    round((trough_equity - peak_equity) / peak_equity * 100.0, 4)
                    if peak_equity > 0 else 0.0
                )
                dd_duration = recovery_end - peak_datetime
                recovery_duration = recovery_end - trough_datetime

                episodes.append({
                    "DD #": dd_num,
                    "Start Datetime": peak_datetime,
                    "Peak Equity": round(peak_equity, 2),
                    "Trough Datetime": trough_datetime,
                    "Trough Equity": round(trough_equity, 2),
                    "Drawdown $": dd_dollar,
                    "Drawdown %": round(dd_pct, 2),
                    "Recovery Start": trough_datetime,
                    "Recovery End": recovery_end,
                    "Drawdown Duration": dd_duration,
                    "Recovery Duration": recovery_duration,
                    "Total Episode Duration": dd_duration,
                    "Status": "RECOVERED",
                })
                in_drawdown = False
                trough_equity = float("inf")
                trough_datetime = None

    # Drawdown still open when the backtest ended
    if in_drawdown:
        dd_dollar = round(trough_equity - peak_equity, 2)
        dd_pct = (
            round((trough_equity - peak_equity) / peak_equity * 100.0, 4)
            if peak_equity > 0 else 0.0
        )
        episodes.append({
            "DD #": dd_num,
            "Start Datetime": peak_datetime,
            "Peak Equity": round(peak_equity, 2),
            "Trough Datetime": trough_datetime,
            "Trough Equity": round(trough_equity, 2),
            "Drawdown $": dd_dollar,
            "Drawdown %": round(dd_pct, 2),
            "Recovery Start": trough_datetime,
            "Recovery End": pd.NaT,
            "Drawdown Duration": pd.NaT,
            "Recovery Duration": pd.NaT,
            "Total Episode Duration": pd.NaT,
            "Status": "OPEN / NOT RECOVERED",
        })

    episodes_df = pd.DataFrame(episodes) if episodes else pd.DataFrame(columns=EPISODE_COLUMNS)
    eq_output = eq.iloc[1:].reset_index(drop=True)
    return episodes_df, eq_output


def calculate_drawdown_statistics(episodes_df: pd.DataFrame, last_timestamp=None) -> pd.DataFrame:
    """
    Compute the 19 overall drawdown summary statistics from an episode table.

    Args:
        episodes_df:    Output of :func:`calculate_drawdown_episodes`.
        last_timestamp: Last trade close timestamp.  Used to charge the still-open
                        drawdown its partial duration in "Total Time in Drawdown".

    Returns:
        DataFrame with 'Metric' and 'Value' columns.
    """
    if episodes_df is None or episodes_df.empty:
        return pd.DataFrame({"Metric": ["Total Drawdown Count"], "Value": [0]})

    total_count = len(episodes_df)

    max_dd_dollar = episodes_df["Drawdown $"].min()
    max_dd_pct = episodes_df["Drawdown %"].min()
    max_dd_row = episodes_df.loc[episodes_df["Drawdown $"].idxmin()]

    recovered = episodes_df[episodes_df["Status"] == "RECOVERED"]
    open_dd = episodes_df[episodes_df["Status"] != "RECOVERED"]
    num_recovered = len(recovered)
    num_open = len(open_dd)
    pct_recovered = round((num_recovered / total_count) * 100.0, 2) if total_count > 0 else 0.0

    dd_durations = recovered["Drawdown Duration"].dropna()
    rec_durations = recovered["Recovery Duration"].dropna()

    # Total Time in Drawdown: recovered durations + partial time for open episodes
    total_time_in_dd = pd.Timedelta(0)
    for _, row in episodes_df.iterrows():
        if pd.notna(row["Drawdown Duration"]):
            total_time_in_dd += row["Drawdown Duration"]
        elif last_timestamp is not None and pd.notna(row["Start Datetime"]):
            total_time_in_dd += (pd.Timestamp(last_timestamp) - pd.Timestamp(row["Start Datetime"]))

    longest_dd = str(dd_durations.max()) if len(dd_durations) > 0 else "N/A"
    avg_dd = str(dd_durations.mean()) if len(dd_durations) > 0 else "N/A"
    median_dd = str(dd_durations.median()) if len(dd_durations) > 0 else "N/A"

    longest_rec = str(rec_durations.max()) if len(rec_durations) > 0 else "N/A"
    avg_rec = str(rec_durations.mean()) if len(rec_durations) > 0 else "N/A"
    median_rec = str(rec_durations.median()) if len(rec_durations) > 0 else "N/A"
    total_rec_time = str(rec_durations.sum()) if len(rec_durations) > 0 else "N/A"

    stats = [
        {"Metric": "Total Drawdown Count", "Value": total_count},
        {"Metric": "Maximum Drawdown ($)", "Value": max_dd_dollar},
        {"Metric": "Maximum Drawdown (%)", "Value": f"{max_dd_pct}%"},
        {"Metric": "Total Time in Drawdown", "Value": str(total_time_in_dd)},
        {"Metric": "Longest Drawdown Duration", "Value": longest_dd},
        {"Metric": "Average Drawdown Duration", "Value": avg_dd},
        {"Metric": "Median Drawdown Duration", "Value": median_dd},
        {"Metric": "Longest Recovery Duration", "Value": longest_rec},
        {"Metric": "Average Recovery Duration", "Value": avg_rec},
        {"Metric": "Median Recovery Duration", "Value": median_rec},
        {"Metric": "Total Recovery Time", "Value": total_rec_time},
        {"Metric": "Number of Recovered Drawdowns", "Value": num_recovered},
        {"Metric": "Number of Open/Unrecovered Drawdowns", "Value": num_open},
        {"Metric": "Percentage of Drawdowns Recovered", "Value": f"{pct_recovered}%"},
        {"Metric": "Maximum Drawdown Start Datetime", "Value": str(max_dd_row["Start Datetime"])},
        {"Metric": "Maximum Drawdown Trough Datetime", "Value": str(max_dd_row["Trough Datetime"])},
        {"Metric": "Maximum Drawdown Recovery Datetime",
         "Value": str(max_dd_row["Recovery End"]) if pd.notna(max_dd_row["Recovery End"]) else "NOT RECOVERED"},
        {"Metric": "Maximum Drawdown Duration",
         "Value": str(max_dd_row["Drawdown Duration"]) if pd.notna(max_dd_row["Drawdown Duration"]) else "ONGOING"},
        {"Metric": "Maximum Recovery Duration",
         "Value": str(max_dd_row["Recovery Duration"]) if pd.notna(max_dd_row["Recovery Duration"]) else "ONGOING"},
    ]

    return pd.DataFrame(stats)


def summarize_drawdown(episodes_df: pd.DataFrame, equity_df: pd.DataFrame = None) -> dict:
    """
    Reduce the episode table to flat numeric metrics for the optimizer.

    The optimizer's ranking functions only read scalars out of the
    "Technical Statistics" tab, so this returns plain numbers (never Timedelta
    or strings) that a strategy can append to its statistics rows.

    Returns:
        dict of metric-name -> number.  Always contains every key, with zeros
        when there were no drawdowns, so optimizer result tables stay
        rectangular across batches.
    """
    out = {
        "Max Drawdown $": 0.0,
        "Max Drawdown %": 0.0,
        "Total Drawdown Count": 0,
        "Recovered Drawdown Count": 0,
        "Open Drawdown Count": 0,
        "Longest Drawdown Days": 0.0,
        "Average Drawdown Days": 0.0,
        "Longest Recovery Days": 0.0,
    }
    if episodes_df is None or episodes_df.empty:
        return out

    out["Max Drawdown $"] = float(episodes_df["Drawdown $"].min())
    out["Max Drawdown %"] = float(episodes_df["Drawdown %"].min())
    out["Total Drawdown Count"] = int(len(episodes_df))
    out["Recovered Drawdown Count"] = int((episodes_df["Status"] == "RECOVERED").sum())
    out["Open Drawdown Count"] = int((episodes_df["Status"] != "RECOVERED").sum())

    dd_durations = episodes_df["Drawdown Duration"].dropna()
    rec_durations = episodes_df["Recovery Duration"].dropna()
    if len(dd_durations) > 0:
        out["Longest Drawdown Days"] = round(float(dd_durations.max().total_seconds() / 86400.0), 2)
        out["Average Drawdown Days"] = round(float(dd_durations.mean().total_seconds() / 86400.0), 2)
    if len(rec_durations) > 0:
        out["Longest Recovery Days"] = round(float(rec_durations.max().total_seconds() / 86400.0), 2)

    return out


# =============================================================================
# EXCEL REPORTING
# =============================================================================

def write_drawdown_sheets(
    writer,
    trades_df: pd.DataFrame,
    initial_capital: float,
    pnl_col=None,
    time_col=None,
    analysis_sheet: str = "Drawdown Analysis",
    episodes_sheet: str = "Drawdown Episodes",
    currency_symbol: str = "$",
    x_axis_title: str = "Trade Exit Time",
    chart_anchor: str = "D1",
) -> dict:
    """
    Write the "Drawdown Analysis" and "Drawdown Episodes" tabs into an open
    ``pd.ExcelWriter`` (openpyxl engine).

    Layout matches the reference XAUUSD report:
      * Drawdown Analysis  — summary statistics table at the top, the underwater
        curve data below it, and an AreaChart of drawdown_$ anchored top-right.
      * Drawdown Episodes  — one row per episode, durations rendered as text.

    When the strategy produced no drawdown at all, both sheets are still created
    with their headers so downstream report readers never hit a missing tab.

    Args:
        writer:           Open ``pd.ExcelWriter`` using the openpyxl engine.
        trades_df:        Trade log.
        initial_capital:  Opening balance in P&L units.
        pnl_col/time_col: See :func:`resolve_series_columns`.
        currency_symbol:  Rendered in the chart's Y axis label.
        chart_anchor:     Cell the underwater chart is anchored at.

    Returns:
        dict with 'episodes', 'statistics', 'equity_curve' and 'summary'
        (the :func:`summarize_drawdown` scalars), so callers can feed the same
        numbers into their own statistics tab without recomputing.
    """
    # Imported lazily so this module stays importable without openpyxl present.
    from openpyxl.chart import AreaChart, Reference

    episodes_df, eq_curve = calculate_drawdown_episodes(
        trades_df, initial_capital, pnl_col=pnl_col, time_col=time_col
    )

    if episodes_df.empty:
        pd.DataFrame(columns=["Metric", "Value"]).to_excel(
            writer, sheet_name=analysis_sheet, index=False)
        pd.DataFrame(columns=EPISODE_COLUMNS).to_excel(
            writer, sheet_name=episodes_sheet, index=False)
        return {
            "episodes": episodes_df,
            "statistics": pd.DataFrame(columns=["Metric", "Value"]),
            "equity_curve": eq_curve,
            "summary": summarize_drawdown(episodes_df, eq_curve),
        }

    last_ts = eq_curve["exit_timestamp"].max() if not eq_curve.empty else None
    dd_stats = calculate_drawdown_statistics(episodes_df, last_timestamp=last_ts)
    dd_stats.to_excel(writer, sheet_name=analysis_sheet, index=False, startrow=0)

    # Underwater curve data sits below the statistics table
    uw_start_row = len(dd_stats) + 3
    underwater_data = eq_curve[
        ["exit_timestamp", "account_balance", "running_peak", "drawdown_$", "drawdown_%"]
    ].copy()
    underwater_data.to_excel(writer, sheet_name=analysis_sheet,
                             index=False, startrow=uw_start_row)

    dd_sheet = writer.book[analysis_sheet]
    dd_chart = AreaChart()
    dd_chart.title = "Underwater / Drawdown Curve"
    dd_chart.style = 13
    dd_chart.y_axis.title = f"Drawdown ({currency_symbol})"
    dd_chart.x_axis.title = x_axis_title

    uw_header_row = uw_start_row + 1  # openpyxl rows are 1-indexed
    uw_data_end = uw_header_row + len(underwater_data)

    # Col D (4) = drawdown_$ | Col A (1) = exit_timestamp
    dd_data_ref = Reference(dd_sheet, min_col=4, min_row=uw_header_row, max_row=uw_data_end)
    dd_cats_ref = Reference(dd_sheet, min_col=1, min_row=uw_header_row + 1, max_row=uw_data_end)

    dd_chart.add_data(dd_data_ref, titles_from_data=True)
    dd_chart.set_categories(dd_cats_ref)
    dd_chart.width = 30
    dd_chart.height = 12
    dd_sheet.add_chart(dd_chart, chart_anchor)

    # Episodes tab — Timedelta columns are stringified so Excel renders them
    ep_export = episodes_df.copy()
    for col in _DURATION_COLUMNS:
        if col in ep_export.columns:
            ep_export[col] = ep_export[col].apply(lambda x: str(x) if pd.notna(x) else "")
    ep_export.to_excel(writer, sheet_name=episodes_sheet, index=False)

    return {
        "episodes": episodes_df,
        "statistics": dd_stats,
        "equity_curve": eq_curve,
        "summary": summarize_drawdown(episodes_df, eq_curve),
    }

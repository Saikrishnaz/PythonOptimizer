"""
NIFTY Supertrend Credit Spread Backtest Strategy
==================================================
This backtest implements the NIFTY Supertrend Credit Spread strategy as documented in:
  - "NIFTY Supertrend Credit Spread Strategy Note (V1)"
  - "Overnight Hedge Management Framework"

Strategy Summary:
-----------------
1. Chart Setup:
   - Timeframe: 15 Minutes
   - Indicator: SuperTrend (ATR Length=16, Multiplier=1.72, Source=HL2)
   - Market: NIFTY 50 Weekly Options (NEXT weekly expiry only)

2. Long Entry:
   - SuperTrend flips from Short → Long (signal candle)
   - Wait for buffer candle, confirm break above: Signal Candle High + 24 pts (rounded)
   - Reference strike = round UP trigger level to nearest 50-pt strike
   - Bull Put Credit Spread: Sell PE short_distance below ref, Buy PE long_distance below ref
     (defaults: 150 / 300 via spread_distance; override with short_distance/long_distance)

3. Short Entry:
   - SuperTrend flips from Long → Short (signal candle)
   - Wait for buffer candle, confirm break below: Signal Candle Low − 24 pts (rounded)
   - Reference strike = round DOWN trigger level to nearest 50-pt strike
   - Bear Call Credit Spread: Sell CE short_distance above ref, Buy CE long_distance above ref

4. Exit Logic:
   - Method 1: SuperTrend flip with buffer confirmation (±24 pt)
   - Method 2: OMS Scaling (40% at 31% profit, 40% at 69% profit, 20% at 98% profit)
   - Method 3: SuperTrend SL Buffer (Immediate exit if spot breaches SuperTrend value ± supertrend_sl_buffer)
   - Time Exit: Force close at exit_time (15:30) on expiry day

5. Overnight Hedge:
   - If carrying overnight, buy additional 23% protective options at 15:25, exit at 09:20 next morning

6. Risk Rules:
   - Only ONE active position at a time
   - No new entry or position reversal after 15:15 (no_entry_after)
   - Trade next weekly expiry only

Optimizer / Walk-Forward Integration
------------------------------------
This script is driven by the shared optimizer (worker.py -> main(**params)),
so it follows the framework's output contract:

* Report      : BACKTEST_CREDIT_SPREAD_<symbol>.xlsx in the current working
                directory (the worker chdir's into each batch folder), or an
                explicit `output_path`.
* Metrics     : the "Technical Statistics" tab carries the canonical metric
                names the optimizer ranks on — Total Trades, Net Profit,
                Win Rate %, Profit Factor, Sharpe Ratio, Overall Max Drawdown —
                alongside the credit-spread-specific rows, which are kept.
                Because this variant models real costs it also publishes
                "Net PnL After Costs" and "Brokerage Ratio %", which the
                optimizer's `priority_score` ranking reads directly.
* Drawdown    : "Drawdown Analysis" and "Drawdown Episodes" tabs are produced
                by the shared backtest_analytics module (same implementation
                the XAUUSD Supertrend report uses).
* Backtest    : the window can be supplied nested (Backtest_period) or flat
  window       (start_date / end_date), so walk-forward steps can override it
                in whichever style script_analyzer detects.
* Parameters  : the OMS scaling targets are also exposed as flat scalars
                (target_1_percent / target_2_percent / target_3_percent) so the
                optimizer can sweep them; they override targets_credit_spread.
* Option data : `option_symbol_format` switches the option CSV filename
                convention between vendors ("ddMMMyy" / "yymmdd").

Every one of those additions is backward-compatible: running this file directly
with the CONFIG at the bottom produces exactly the same trades and P&L as before.
"""

import pandas as pd
import numpy as np
import pandas_ta as pdt
from backtesting import Backtest, Strategy
from datetime import datetime, time as dtime, date as ddate
import os
import sys
import math
from openpyxl.chart import LineChart, Reference

# --- Shared analytics module -------------------------------------------------
# The optimizer's worker loads this file with importlib from an arbitrary
# working directory, so neither this folder nor the project root is guaranteed
# to be on sys.path. Add both, then import the shared drawdown analytics that
# every backtest in this project shares (see backtest_analytics.py).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for _candidate_path in (_THIS_DIR, _PROJECT_ROOT):
    if _candidate_path not in sys.path:
        sys.path.insert(0, _candidate_path)

from backtest_analytics import (            # noqa: E402
    calculate_drawdown_episodes,
    calculate_drawdown_statistics,
    summarize_drawdown,
    write_drawdown_sheets,
)


# --- OHLC Consolidate for 5 Min Intraday Data ---
def ohlc_consolidate(df: pd.DataFrame, timevalue: str, Isvolume: bool = True) -> pd.DataFrame:
    df = df.copy()
    if 'timestamp' in df.columns:
        df.set_index('timestamp', inplace=True)
    df.index = pd.to_datetime(df.index)

    # Filter time range
    df = df[(df.index.time >= dtime(9, 15)) & (df.index.time < dtime(15, 30))]

    # Resample
    ohlc_df = df.resample(
        timevalue, offset=(pd.Timestamp('09:15:00') - pd.Timestamp('00:00:00'))
    ).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last'
    })

    if Isvolume and 'volume' in df.columns:
        resampled_volume = df['volume'].resample(
            timevalue, offset=(pd.Timestamp('09:15:00') - pd.Timestamp('00:00:00'))
        ).sum()
        ohlc_df['volume'] = resampled_volume
    else:
        ohlc_df['volume'] = 0

    ohlc_df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
    return ohlc_df


# --- SuperTrend Indicator ---
def compute_supertrend(df: pd.DataFrame, length: int = 5, multiplier: float = 1.5) -> pd.DataFrame:
    """
    Compute SuperTrend using pandas_ta.
    Adds columns: SUPERT (value) and SUPERTd (direction: +1 bullish, -1 bearish).
    pandas_ta internally uses HL2 as source by default.
    """
    df = df.copy()
    supertrend = pdt.supertrend(df['high'], df['low'], df['close'], length=length, multiplier=multiplier)
    
    st_val_col = f'SUPERT_{length}_{multiplier}'
    st_dir_col = f'SUPERTd_{length}_{multiplier}'

    df['SUPERT'] = supertrend[st_val_col]
    df['SUPERTd'] = supertrend[st_dir_col]

    return df


def round_up_to_strike(level, step=50):
    """Round level UP to nearest strike price."""
    return int(math.ceil(level / step) * step)


def round_down_to_strike(level, step=50):
    """Round level DOWN to nearest strike price."""
    return int(math.floor(level / step) * step)


def get_strike(ltp, step_size):
    """Round LTP to nearest strike price."""
    remainder = ltp % step_size
    if remainder >= step_size / 2:
        return int(ltp + (step_size - remainder))
    else:
        return int(ltp - remainder)


# =============================================================================
# OPTION SYMBOL (CSV FILENAME) FORMATS
# =============================================================================
# Different data vendors name their option files differently. Every format here
# is <SYMBOL><expiry part><strike><CE|PE>; only the expiry part varies, so each
# entry just renders that part from the expiry date.
#
# Pick one with the `option_symbol_format` config option.
OPTION_SYMBOL_FORMATS = {
    # NIFTY02JUN2622900CE  — day, upper-case month abbreviation, 2-digit year
    "ddMMMyy": lambda dt: f"{dt.strftime('%d')}{dt.strftime('%b').upper()}{dt.strftime('%y')}",
    # NIFTY2003059650CE    — 2-digit year, month, day (all numeric)
    "yymmdd": lambda dt: dt.strftime("%y%m%d"),
}

#: Spellings accepted for each format, so a config typo like "YYMMDD" or
#: "dd-MMM-yy" still resolves instead of failing mid-backtest.
_SYMBOL_FORMAT_ALIASES = {
    "ddmmmyy": "ddMMMyy",
    "ddmmmyy_strike": "ddMMMyy",
    "dd-mmm-yy": "ddMMMyy",
    "yymmdd": "yymmdd",
    "yymmdd_strike": "yymmdd",
    "yy-mm-dd": "yymmdd",
}


def resolve_symbol_format(fmt) -> str:
    """Normalise a user-supplied option_symbol_format into a key of OPTION_SYMBOL_FORMATS."""
    if fmt is None:
        return "ddMMMyy"
    key = str(fmt).strip()
    if key in OPTION_SYMBOL_FORMATS:
        return key
    resolved = _SYMBOL_FORMAT_ALIASES.get(key.lower().replace(" ", ""))
    if resolved is None:
        raise ValueError(
            f"Unknown option_symbol_format {fmt!r}. "
            f"Valid values: {', '.join(OPTION_SYMBOL_FORMATS)}"
        )
    return resolved


def build_option_symbol(symbol, expiry_val, strike, option_type, fmt="ddMMMyy") -> str:
    """
    Build the option CSV filename key (without the .csv extension).

    Examples for NIFTY, expiry 2020-03-05, strike 9650, CE:
        fmt="ddMMMyy" -> NIFTY05MAR209650CE
        fmt="yymmdd"  -> NIFTY2003059650CE

    Args:
        symbol:      Underlying, e.g. "NIFTY".
        expiry_val:  Expiry as a date/datetime, or a "YYYY-MM-DD" string.
        strike:      Strike price (coerced to int).
        option_type: "CE" or "PE".
        fmt:         Key or alias from OPTION_SYMBOL_FORMATS.
    """
    if isinstance(expiry_val, str):
        expiry_dt = datetime.strptime(expiry_val, "%Y-%m-%d")
    elif hasattr(expiry_val, "strftime"):
        expiry_dt = expiry_val
    else:
        expiry_dt = pd.to_datetime(expiry_val)

    expiry_part = OPTION_SYMBOL_FORMATS[resolve_symbol_format(fmt)](expiry_dt)
    return f"{str(symbol).upper()}{expiry_part}{int(strike)}{str(option_type).upper()}"


# --- Trade Record Template ---
def default_records():
    return {
        "signal_timestamp": None,        # Time when SuperTrend flip signal candle appeared
        "signal_type": None,             # "long" or "short"
        "buffer_candle": {"high": None, "low": None},  # High/Low of buffer candle + 10 pts
        "exit_buffer_candle": {"high": None, "low": None},  # Exit buffer candle levels
        "supertrend_value": None,        # SuperTrend value at entry
        "supertrend_direction": None,    # "BULLISH" or "BEARISH" at entry
        "spot_price_at_entry": None,     # Spot Close at signal candle
        "spot_price_at_exit": None,      # Spot Close at exit
        "symbol": None,                  # "NIFTY"
        "ohlc_entry": {"open": None, "high": None, "low": None, "close": None},
        "ohlc_exit": {"open": None, "high": None, "low": None, "close": None},
        "expiry_date": {"current_expiry": None, "next_expiry": None, "far_expiry": None, "trade_expiry": None},
        "rolled_from_0dte": False,
        "synthetic_legs": "",       # "short" / "long" / "short+long" when a leg was priced from intrinsic
        "credit_spread": {
            "entry": None,
            "exit1": None, "exit1_time": None, "exit1_qty": None, "exit1_short": None, "exit1_long": None,
            "exit2": None, "exit2_time": None, "exit2_qty": None, "exit2_short": None, "exit2_long": None,
            "exit3": None, "exit3_time": None, "exit3_qty": None, "exit3_short": None, "exit3_long": None,
            "final_exit": None, "final_exit_time": None, "final_exit_qty": None, "final_exit_short": None, "final_exit_long": None,
        },
        "remaining_qty": None,
        "hedged_qty": None,              # Additional hedge qty if carrying overnight
        "entry_time": None,
        "strike": None,                  # Reference strike
        "option_type": None,             # "CE" or "PE"
        "entry_type": None,              # "Bull Put Credit Spread" or "Bear Call Credit Spread"
        "long_option": {
            "strike_price": None, "option_type": None,
            "entry_price": None, "exit_price": None,
            "options_data": None
        },
        "short_option": {
            "strike_price": None, "option_type": None,
            "entry_price": None, "exit_price": None,
            "options_data": None
        },
        "exit_signal": None,
        "exit_signal_timestamp":None,
        "exit_supertrend_value":None,
        "exit_timestamp": None,
        "entry_price": None,             # Credit spread entry value
        "exit_price": None,              # Credit spread exit value
        "profit_points": None,           # entry_price - exit_price (credit spread)
        "profit_in_inr": None, # Rupee PNL correctly weighted for partial exits
        "reason_for_exit": None,         # "SuperTrend Flip" / "Time Exit" / "Target 3 Reached"
        "spread_data": None,             # Merged DF of short and long option prices
        "trade_number_today": None,
        
        # Capital and Qty tracking
        "total_qty": None,
        "capital_used": None,
        "lots": None,
        "margin_per_lot": None,
        "span_per_lot": None,
        "exposure_per_lot": None,
        "premium_received": None,
        "trading_capital_at_entry": None,
        "position_id": None,
        
        # Hedge Tracking
        "active_hedge": None
    }


def merge_expires(data, symbol):
    exp_collection = {"NIFTY_EXPIRES":[
        "2019-01-31",
        "2019-02-14",
        "2019-02-21",
        "2019-02-28",
        "2019-03-07",
        "2019-03-14",
        "2019-03-20",
        "2019-03-28",
        "2019-04-04",
        "2019-04-11",
        "2019-04-18",
        "2019-04-25",
        "2019-05-02",
        "2019-05-09",
        "2019-05-16",
        "2019-05-23",
        "2019-05-30",
        "2019-06-06",
        "2019-06-13",
        "2019-06-20",
        "2019-06-27",
        "2019-07-04",
        "2019-07-11",
        "2019-07-18",
        "2019-07-25",
        "2019-08-01",
        "2019-08-08",
        "2019-08-14",
        "2019-08-22",
        "2019-08-29",
        "2019-09-05",
        "2019-09-12",
        "2019-09-19",
        "2019-09-26",
        "2019-10-03",
        "2019-10-10",
        "2019-10-17",
        "2019-10-24",
        "2019-10-31",
        "2019-11-07",
        "2019-11-14",
        "2019-11-21",
        "2019-11-28",
        "2019-12-05",
        "2019-12-12",
        "2019-12-19",
        "2019-12-26",
        "2020-01-02",
        "2020-01-09",
        "2020-01-16",
        "2020-01-23",
        "2020-01-30",
        "2020-02-06",
        "2020-02-13",
        "2020-02-20",
        "2020-02-27",
        "2020-03-05",
        "2020-03-12",
        "2020-03-19",
        "2020-03-26",
        "2020-04-01",
        "2020-04-09",
        "2020-04-16",
        "2020-04-23",
        "2020-04-30",
        "2020-05-07",
        "2020-05-14",
        "2020-05-21",
        "2020-05-28",
        "2020-06-04",
        "2020-06-11",
        "2020-06-18",
        "2020-06-25",
        "2020-07-02",
        "2020-07-09",
        "2020-07-16",
        "2020-07-23",
        "2020-07-30",
        "2020-08-06",
        "2020-08-13",
        "2020-08-20",
        "2020-08-27",
        "2020-09-03",
        "2020-09-10",
        "2020-09-17",
        "2020-09-24",
        "2020-10-01",
        "2020-10-08",
        "2020-10-15",
        "2020-10-22",
        "2020-10-29",
        "2020-11-05",
        "2020-11-12",
        "2020-11-19",
        "2020-11-26",
        "2020-12-03",
        "2020-12-10",
        "2020-12-17",
        "2020-12-24",
        "2020-12-31",
        "2021-01-07",
        "2021-01-14",
        "2021-01-21",
        "2021-01-28",
        "2021-02-04",
        "2021-02-11",
        "2021-02-18",
        "2021-02-25",
        "2021-03-04",
        "2021-03-10",
        "2021-03-18",
        "2021-03-25",
        "2021-04-01",
        "2021-04-08",
        "2021-04-15",
        "2021-04-22",
        "2021-04-29",
        "2021-05-06",
        "2021-05-12",
        "2021-05-20",
        "2021-05-27",
        "2021-06-03",
        "2021-06-10",
        "2021-06-17",
        "2021-06-24",
        "2021-07-01",
        "2021-07-08",
        "2021-07-15",
        "2021-07-22",
        "2021-07-29",
        "2021-08-05",
        "2021-08-12",
        "2021-08-18",
        "2021-08-26",
        "2021-09-02",
        "2021-09-09",
        "2021-09-16",
        "2021-09-23",
        "2021-09-30",
        "2021-10-07",
        "2021-10-14",
        "2021-10-21",
        "2021-10-28",
        "2021-11-03",
        "2021-11-11",
        "2021-11-18",
        "2021-11-25",
        "2021-12-02",
        "2021-12-09",
        "2021-12-16",
        "2021-12-23",
        "2021-12-30",
        "2022-01-06",
        "2022-01-13",
        "2022-01-20",
        "2022-01-27",
        "2022-02-03",
        "2022-02-10",
        "2022-02-17",
        "2022-02-24",
        "2022-03-03",
        "2022-03-10",
        "2022-03-17",
        "2022-03-24",
        "2022-03-31",
        "2022-04-07",
        "2022-04-13",
        "2022-04-21",
        "2022-04-28",
        "2022-05-05",
        "2022-05-12",
        "2022-05-19",
        "2022-05-26",
        "2022-06-02",
        "2022-06-09",
        "2022-06-16",
        "2022-06-23",
        "2022-06-30",
        "2022-07-07",
        "2022-07-14",
        "2022-07-21",
        "2022-07-28",
        "2022-08-04",
        "2022-08-11",
        "2022-08-18",
        "2022-08-25",
        "2022-09-01",
        "2022-09-08",
        "2022-09-15",
        "2022-09-22",
        "2022-09-29",
        "2022-10-06",
        "2022-10-13",
        "2022-10-20",
        "2022-10-27",
        "2022-11-03",
        "2022-11-10",
        "2022-11-17",
        "2022-11-24",
        "2022-12-01",
        "2022-12-08",
        "2022-12-15",
        "2022-12-22",
        "2022-12-29",
        "2023-01-05",
        "2023-01-12",
        "2023-01-19",
        "2023-01-25",
        "2023-02-02",
        "2023-02-09",
        "2023-02-16",
        "2023-02-23",
        "2023-03-02",
        "2023-03-09",
        "2023-03-16",
        "2023-03-23",
        "2023-03-29",
        "2023-03-30",
        "2023-04-06",
        "2023-04-13",
        "2023-04-20",
        "2023-04-27",
        "2023-05-04",
        "2023-05-11",
        "2023-05-18",
        "2023-05-25",
        "2023-06-01",
        "2023-06-08",
        "2023-06-15",
        "2023-06-22",
        "2023-06-28",
        "2023-06-29",
        "2023-07-06",
        "2023-07-13",
        "2023-07-20",
        "2023-07-27",
        "2023-08-03",
        "2023-08-10",
        "2023-08-17",
        "2023-08-24",
        "2023-08-31",
        "2023-09-07",
        "2023-09-14",
        "2023-09-21",
        "2023-09-28",
        "2023-10-05",
        "2023-10-12",
        "2023-10-19",
        "2023-10-26",
        "2023-11-02",
        "2023-11-09",
        "2023-11-16",
        "2023-11-23",
        "2023-11-30",
        "2023-12-07",
        "2023-12-14",
        "2023-12-21",
        "2023-12-28",
        "2024-01-04",
        "2024-01-11",
        "2024-01-18",
        "2024-01-25",
        "2024-02-01",
        "2024-02-08",
        "2024-02-15",
        "2024-02-22",
        "2024-02-29",
        "2024-03-07",
        "2024-03-14",
        "2024-03-21",
        "2024-03-28",
        "2024-04-04",
        "2024-04-10",
        "2024-04-18",
        "2024-04-25",
        "2024-05-02",
        "2024-05-09",
        "2024-05-16",
        "2024-05-23",
        "2024-05-30",
        "2024-06-06",
        "2024-06-13",
        "2024-06-20",
        "2024-06-27",
        "2024-07-04",
        "2024-07-11",
        "2024-07-18",
        "2024-07-25",
        "2024-08-01",
        "2024-08-08",
        "2024-08-14",
        "2024-08-22",
        "2024-08-29",
        "2024-09-05",
        "2024-09-12",
        "2024-09-19",
        "2024-09-26",
        "2024-10-03",
        "2024-10-10",
        "2024-10-17",
        "2024-10-24",
        "2024-10-31",
        "2024-11-07",
        "2024-11-14",
        "2024-11-21",
        "2024-11-28",
        "2024-12-05",
        "2024-12-12",
        "2024-12-19",
        "2024-12-26",
        "2025-01-02",
        "2025-01-09",
        "2025-01-16",
        "2025-01-23",
        "2025-01-30",
        "2025-02-06",
        "2025-02-13",
        "2025-02-20",
        "2025-02-27",
        "2025-03-06",
        "2025-03-13",
        "2025-03-20",
        "2025-03-27",
        "2025-04-03",
        "2025-04-09",
        "2025-04-17",
        "2025-04-24",
        "2025-04-30",
        "2025-05-08",
        "2025-05-15",
        "2025-05-22",
        "2025-05-29",
        "2025-06-05",
        "2025-06-12",
        "2025-06-19",
        "2025-06-26",
        "2025-07-03",
        "2025-07-10",
        "2025-07-17",
        "2025-07-24",
        "2025-07-31",
        "2025-08-07",
        "2025-08-14",
        "2025-08-21",
        "2025-08-28",
        "2025-09-02",
        "2025-09-09",
        "2025-09-16",
        "2025-09-23",
        "2025-09-25",
        "2025-09-30",
        "2025-10-07",
        "2025-10-14",
        "2025-10-20",
        "2025-10-28",
        "2025-11-04",
        "2025-11-11",
        "2025-11-18",
        "2025-11-25",
        "2025-12-02",
        "2025-12-09",
        "2025-12-16",
        "2025-12-23",
        "2025-12-30",
        "2026-01-06",
        "2026-01-13",
        "2026-01-20",
        "2026-01-27",
        "2026-02-03",
        "2026-02-10",
        "2026-02-17",
        "2026-02-24",
        "2026-03-02",
        "2026-03-10",
        "2026-03-17",
        "2026-03-24",
        "2026-03-30",
        "2026-04-07",
        "2026-04-13",
        "2026-04-21",
        "2026-04-28",
        "2026-05-05",
        "2026-05-12",
        "2026-05-19",
        "2026-05-26",
        "2026-06-02",
        "2026-06-09",
        "2026-06-16",
        "2026-06-23",
        "2026-06-30",
        "2026-07-07",
        "2026-07-14",
        "2026-07-21",
        "2026-07-28",
        "2026-08-04",
        "2026-08-11",
        "2026-08-18",
        "2026-08-25",
        "2026-09-01",
        "2026-09-08",
        "2026-09-15",
        "2026-09-22",
        "2026-09-29",
        "2026-10-06",
        "2026-10-13",
        "2026-10-19",
        "2026-10-27",
        "2026-11-03",
        "2026-11-09",
        "2026-11-17",
        "2026-11-23",
        "2026-12-01",
        "2026-12-08",
        "2026-12-15",
        "2026-12-22",
        "2026-12-29"
    ]}

    data = data.copy()
    data.index = pd.to_datetime(data.index)
    data['datetime'] = pd.to_datetime(data.index)

    # 1) Add a pure date column to data
    data["trade_date"] = data.index.normalize()  # strips time -> 2024-01-04 00:00:00
    # 2) Prepare expiry df with a date column
    exp_df = pd.DataFrame({
        "expiry": pd.to_datetime(exp_collection[f"{symbol}_EXPIRES"])
    }).sort_values("expiry")

    exp_df["expiry_date"] = exp_df["expiry"].dt.normalize()
    exp_df["next_expiry"] = exp_df["expiry"].shift(-1)
    exp_df["far_expiry"] = exp_df["expiry"].shift(-2)     # the one after next

    # FIX (Bug 5): the hardcoded calendar has holes (e.g. 2025-11-11 -> 2026-01-06)
    # that silently deleted months of trading. Warn loudly about every gap.
    gaps = exp_df["expiry"].diff().dt.days
    for i, g in gaps.items():
        if pd.notna(g) and g > 10:
            print(f"!! EXPIRY CALENDAR GAP: {exp_df['expiry'].iloc[i-1].date()} -> "
                  f"{exp_df['expiry'].iloc[i].date()} ({int(g)} days). "
                  f"Entries in this window will trade far-dated options or be skipped. FIX THE CALENDAR.")

    # 3) merge_asof on date, not full timestamp
    final_df = pd.merge_asof(
        data.sort_values("trade_date"),
        exp_df[["expiry_date", "expiry", "next_expiry", "far_expiry"]].sort_values("expiry_date"),
        left_on="trade_date",
        right_on="expiry_date",
        direction="forward"
    )

    # 4) Clean up
    final_df = final_df.drop(columns=["trade_date", "expiry_date"])
    final_df.set_index(keys="datetime", inplace=True)
    final_df["expiry"] = pd.to_datetime(final_df["expiry"]).dt.date
    final_df["next_expiry"] = pd.to_datetime(final_df["next_expiry"]).dt.date
    final_df["far_expiry"] = pd.to_datetime(final_df["far_expiry"]).dt.date
    final_df.dropna(subset=["expiry"], inplace=True)
    final_df.sort_values(by="datetime", inplace=True)
    return final_df

    

# =============================================================================
# FIXED EXECUTION LAYER — trigger-time fills, real costs, live-parity rules
# =============================================================================
#
# WHAT WAS WRONG (verified against BACKTEST_CREDIT_SPREAD_NIFTY workbook):
#
#  BUG 1 — OPTION FILLS PRICED BEFORE THE TRIGGERING EVENT (the big one):
#     15-min bars are labeled by bar START. Decisions used the completed bar's
#     High/Low (known only at bar close), but option fills were fetched with
#     `timestamp <= bar_label` — i.e. marks from the bar's OPENING minute, up
#     to 15 minutes BEFORE the breakout / SL breach that triggered the fill.
#     Entries booked pre-breakout (richer) credits; SL exits (88% of all
#     position endings) were priced at pre-adverse-move marks. Both directions
#     flattered the strategy.
#     FIX: locate the trigger MINUTE on 1-min spot data inside the bar, then
#     price every leg at the option's first 1-min mark AT OR AFTER that minute.
#
#  BUG 2 — DEAD EXPIRY TIME-EXIT: `current_time >= 15:20` was never true on a
#     15-min grid whose last label is 15:15 → "Time Exit on Expiry" fired ZERO
#     times in 4.5 years. FIX: compare against bar END; hard guard for any
#     position that somehow survives past its expiry date.
#
#  BUG 3 — ZERO COSTS: no brokerage / STT / exchange charges / GST / stamp and
#     no bid-ask. FIX: per-leg slippage applied at fill time + statutory cost
#     model per position, exported per trade.
#
#  BUG 4 — IMPOSSIBLE MARKS TRADED: inverted spreads (short < long) and
#     negative credits from stale illiquid prints were accepted as entries.
#     FIX: entry sanity gate; rejects logged loudly to a "Skipped Entries" tab.
#
#  BUG 5 — SILENT SAMPLE HOLES: missing expiry-calendar dates (e.g. Nov'25 →
#     Jan'26 gap) silently killed months of trading via file-not-found skips.
#     FIX: calendar gap warnings at load + every skipped entry logged.
#
#  BUG 6 — WRONG LOT SIZE (65 fixed for 2022-2026). FIX: date-dependent NIFTY
#     lot size (VERIFY the changeover dates against NSE circulars before
#     trusting INR numbers) with a config override.
#
#  BUG 7 — `total_qty` in target exits recomputed from class defaults instead
#     of the position's actual qty. FIX: use the position's recorded qty.
#
#  Also: intrabar events (SL / flip / T1-T3 / time exit) are now resolved in
#  TIME ORDER within the bar instead of a fixed priority that ignored which
#  actually happened first.
# =============================================================================

# --------------------------- module-level helpers ----------------------------

def find_spot_cross_minute(spot_ts, spot_hi, spot_lo, start_ts, end_ts, level, direction, active_mask=None):
    """First 1-min spot candle in [start_ts, end_ts) crossing `level`.
    direction 'up': high >= level ; 'down': low <= level.
    spot_ts must be int64 ns, sorted. Returns pd.Timestamp or None.

    active_mask (optional, bool array aligned with spot_ts): minutes marked
    False are NOT evaluated (session halt). Because the test is high>=level /
    low<=level rather than a strict cross, a level breached during a halted
    stretch is still caught on the first active minute if spot is still
    beyond it then — and ignored if spot has come back inside."""
    lo_i = np.searchsorted(spot_ts, np.int64(pd.Timestamp(start_ts).value), side="left")
    hi_i = np.searchsorted(spot_ts, np.int64(pd.Timestamp(end_ts).value), side="left")
    if lo_i >= hi_i:
        return None
    if direction == "up":
        cond = spot_hi[lo_i:hi_i] >= level
    else:
        cond = spot_lo[lo_i:hi_i] <= level
    if active_mask is not None:
        cond = cond & active_mask[lo_i:hi_i]
    hits = np.nonzero(cond)[0]
    if len(hits) == 0:
        return None
    return pd.Timestamp(spot_ts[lo_i + hits[0]])


FILL_MAX_GAP_MINUTES = 5   # default bound for at/after fills; 0 = unbounded (legacy)


def option_price_at_or_after(df, ts, max_gap_minutes=None):
    """Fill price for an option leg: first 1-min Close AT OR AFTER `ts`
    (the honest fill — never a mark from before the trigger).

    LOOKAHEAD BOUND: the first print after `ts` must be on the SAME DAY and
    within `max_gap_minutes` of `ts`. An illiquid leg can go hours or days
    without a print; taking that next print would fill today's order at a
    price from the future. When nothing prints inside the window this returns
    (None, None) and the caller applies its own fallback (skip the entry /
    use the last pre-trigger mark and log it). max_gap_minutes=0 disables the
    bound (the previous, unbounded behaviour)."""
    if df is None or df.empty:
        return None, None
    ts = pd.Timestamp(ts)
    tvals = df["timestamp"].values
    i = np.searchsorted(tvals, np.datetime64(ts), side="left")
    if i < len(df):
        row = df.iloc[i]
        used = pd.Timestamp(row["timestamp"])
        gap = FILL_MAX_GAP_MINUTES if max_gap_minutes is None else int(max_gap_minutes)
        if gap > 0:
            if used.date() != ts.date() or (used - ts) > pd.Timedelta(minutes=gap):
                return None, None
        return float(row["Close"]), used
    # Series ended before the trigger. Returning the last (pre-trigger) mark
    # here would be a silent lookahead; let the caller decide the fallback.
    return None, None


def option_price_at_or_before(df, ts):
    """For SCHEDULED actions (15:20 expiry close, 15:25 hedge entry, 09:20
    hedge exit) the mark at/just-before the scheduled time is legitimate."""
    if df is None or df.empty:
        return None, None
    sub = df[df["timestamp"] <= pd.Timestamp(ts)]
    if sub.empty:
        return None, None
    row = sub.iloc[-1]
    return float(row["Close"]), pd.Timestamp(row["timestamp"])


# Lot size BY DATE RANGE, keyed on CONTRACT EXPIRY (pass the expiry, not the
# trade date). Ranges are "YYYY-MM-DD:YYYY-MM-DD" (inclusive) -> lot size.
# !! VERIFY these changeover dates against NSE circulars before trusting INR
# output — they are best-effort and the historical dates matter. !!
DEFAULT_LOT_SIZES = {
    "NIFTY": {
        "2000-01-01:2024-04-25": 50,
        "2024-04-26:2024-11-19": 25,
        "2024-11-20:2025-12-31": 75,
        "2026-01-01:2099-12-31": 65,
    },
    "BANKNIFTY": {
        "2000-01-01:2023-06-30": 25,
        "2023-07-01:2024-11-19": 15,
        "2024-11-20:2099-12-31": 30,
    },
}


def _parse_lot_spec(spec, symbol="NIFTY"):
    """Normalise lot_size input into a sorted list of (start, end, size).
    Accepts: int (flat), {"start:end": size}, {"SYMBOL": {...}}, or None -> defaults."""
    if spec is None:
        spec = DEFAULT_LOT_SIZES.get(symbol, 50)
    if isinstance(spec, dict) and symbol in spec:
        spec = spec[symbol]
    if isinstance(spec, (int, float, str)) and str(spec).strip().lstrip("-").isdigit():
        return [(ddate(2000, 1, 1), ddate(2099, 12, 31), int(spec))]
    if not isinstance(spec, dict):
        raise ValueError(f"lot_size must be an int or a {{'YYYY-MM-DD:YYYY-MM-DD': size}} dict, got {spec!r}")
    out = []
    for rng, size in spec.items():
        a, b = [x.strip() for x in str(rng).split(":")]
        out.append((pd.Timestamp(a).date(), pd.Timestamp(b).date(), int(size)))
    out.sort()
    for (a1, b1, _), (a2, b2, _) in zip(out, out[1:]):
        if a2 <= b1:
            raise ValueError(f"lot_size ranges overlap: {a1}..{b1} and {a2}..{b2}")
    return out


def lot_size_for(d, lot_spec):
    """Lot size in force for date d given a parsed spec (list of (start, end, size))."""
    d = pd.Timestamp(d).date()
    for a, b, size in lot_spec:
        if a <= d <= b:
            return size
    # outside every range: nearest edge, with a loud note once
    if d < lot_spec[0][0]:
        return lot_spec[0][2]
    return lot_spec[-1][2]


def nifty_lot_size(d, override=None):
    """Backward-compatible wrapper around DEFAULT_LOT_SIZES['NIFTY']."""
    if override is not None:
        return int(override)
    return lot_size_for(d, _parse_lot_spec(None, "NIFTY"))


# ---------------------------------------------------------------------------
# Broker-style margin for a DEFINED-RISK CREDIT SPREAD (reusable):
#
#   Total blocked = SPAN(spread) + Exposure(short leg)
#     SPAN(spread) ~= max loss = width x lot x span_factor
#     Exposure     = exposure_pct x spot x lot   (NOT reduced by the hedge)
#   Premium received is credited to the ledger, not netted from the block.
#
# Calibrated on Zerodha + Angel One (Sep-2026 NIFTY, lot 65, spot ~24,000):
#   150-wide CE spread: SPAN 8,484 / 8,845 vs max-loss 9,750 (0.87 / 0.91)
#   250-wide PE spread: SPAN 17,251 vs max-loss 16,250 (1.06)
#   exposure 31,197 / 31,089 = 2.0% x 24,000 x 65 in every case, naked or hedged
#   totals 39,681 / 39,933 / 48,448 = SPAN + exposure exactly.
# Exposure % is a schedule because SEBI/NSE have changed it over the years —
# verify the history for your backtest window.
# ---------------------------------------------------------------------------
DEFAULT_MARGIN = dict(
    span_factor=1.0,                              # SPAN ~= width x lot x this
    exposure_pct=[("2000-01-01", 0.02)],          # 2% of notional on the short leg
)


def margin_components(spread_width, lot_size, spot, margin_cfg, on_date=None):
    """Per-lot margin breakdown for a credit spread. Returns a dict."""
    span = float(spread_width) * float(lot_size) * float(margin_cfg.get("span_factor", 1.0))
    exp_pct = _rate_on(margin_cfg.get("exposure_pct", 0.02), on_date)
    exposure = exp_pct * float(spot) * float(lot_size)
    return {"span": span, "exposure": exposure, "exposure_pct": exp_pct, "total": span + exposure}


def margin_per_lot_for(spread_width, lot_size, spot, margin_cfg, on_date=None):
    return margin_components(spread_width, lot_size, spot, margin_cfg, on_date)["total"]


DEFAULT_COSTS = dict(
    apply_charges=True,           # master switch; False = frictionless charges
    brokerage_per_order=20.0,     # flat discount-broker rate, per executed order
    # Rates that changed over the backtest window are SCHEDULES: list of
    # (effective_from "YYYY-MM-DD", rate), applied by fill date. A plain float
    # is still accepted and used flat. VERIFY against NSE/CBDT circulars.
    exchange_txn_pct=[("2000-01-01", 0.0005),     # NSE options ~0.05% of premium
                      ("2024-10-01", 0.00035)],   # 0.03503% from 1-Oct-2024
    stt_sell_pct=[("2000-01-01", 0.0005),         # 0.05% of premium sold
                  ("2023-04-01", 0.000625),       # 0.0625% from FY24
                  ("2024-10-01", 0.001)],         # 0.1% from 1-Oct-2024
    gst_pct=0.18,                 # GST on (brokerage + exchange charges)
    stamp_buy_pct=0.00003,        # stamp duty on buy-side premium
    sebi_pct=0.000001,            # SEBI fees on turnover
    slippage_per_leg=0.50,        # Rs per leg, applied ADVERSELY at every fill
)

# Charge line items, in the order they are reported. Keys match charge_components().
CHARGE_COMPONENTS = ("brokerage", "exchange_txn", "stt", "gst", "stamp_duty", "sebi_fees")

# Human-readable label per component, used for the report's statistics rows.
CHARGE_LABELS = {
    "brokerage": "Charges - Brokerage",
    "exchange_txn": "Charges - Exchange Txn",
    "stt": "Charges - STT",
    "gst": "Charges - GST",
    "stamp_duty": "Charges - Stamp Duty",
    "sebi_fees": "Charges - SEBI Fees",
}


def _rate_on(rate_or_schedule, on_date):
    """Resolve a flat rate or a [(from_date, rate), ...] schedule for a date."""
    if not isinstance(rate_or_schedule, (list, tuple)):
        return float(rate_or_schedule)
    d = pd.Timestamp(on_date).date() if on_date is not None else ddate.today()
    rate = float(rate_or_schedule[0][1])
    for eff, r in sorted(rate_or_schedule, key=lambda x: pd.Timestamp(x[0])):
        if pd.Timestamp(eff).date() <= d:
            rate = float(r)
    return rate


def charge_components(sell_turnover, buy_turnover, n_orders, costs, on_date=None):
    """
    Every statutory / brokerage charge for one set of fills, itemised in INR.

    Itemised rather than pre-summed so the report can show where the money
    actually went; estimate_charges() is still the total. Slippage is NOT a
    component here — it is applied directly to fill prices, so it never exists
    as a separate debit.

    With costs["apply_charges"] switched off every component is zero, which
    answers "what would this strategy make in a frictionless market" without
    having to zero six individual rate knobs (and get one of them wrong).
    """
    if not costs.get("apply_charges", True):
        return {name: 0.0 for name in CHARGE_COMPONENTS}

    brokerage = n_orders * costs["brokerage_per_order"]
    exch = _rate_on(costs["exchange_txn_pct"], on_date) * (sell_turnover + buy_turnover)
    return {
        "brokerage": brokerage,
        "exchange_txn": exch,
        "stt": _rate_on(costs["stt_sell_pct"], on_date) * sell_turnover,
        "gst": costs["gst_pct"] * (brokerage + exch),
        "stamp_duty": costs["stamp_buy_pct"] * buy_turnover,
        "sebi_fees": costs["sebi_pct"] * (sell_turnover + buy_turnover),
    }


def estimate_charges(sell_turnover, buy_turnover, n_orders, costs, on_date=None):
    """Statutory + brokerage charges in INR (slippage is applied separately,
    directly at fill prices). Zero when costs["apply_charges"] is off."""
    return sum(charge_components(sell_turnover, buy_turnover, n_orders, costs, on_date).values())


def resolve_leg_distances(short_distance, long_distance, spread_distance, step):
    """Turn whatever the optimizer handed us into a valid (short, long) pair.

    Returns (short, long, notes) where notes is a list of human-readable
    adjustments. Returns (None, None, notes) only when nothing can be derived
    (e.g. a leg is missing AND spread_distance is 0).

    Rules, in order:
      both > 0                 -> as given (spread_distance ignored)
      short > 0, long == 0     -> long  = short + spread_distance
      short == 0, long > 0     -> short = long  - spread_distance  (floored at one step)
      short == 0, long > 0,
        spread_distance == 0   -> short = 0 = ATM (the reference strike itself),
                                  long as given, width = long. Near leg at the
                                  money: SELL ATM for credit, BUY ATM for debit.
      both == 0                -> short = spread_distance, long = 2*spread_distance
      short >= long after that -> WARN, long = short + spread_distance
      not a multiple of step   -> WARN, rounded to nearest step
    """
    notes = []
    atm_near = False
    sd = int(short_distance or 0)
    ld = int(long_distance or 0)
    w = int(spread_distance or 0)
    step = int(step or 50)

    if sd > 0 and ld > 0:
        pass                                            # explicit pair
    elif sd > 0 and ld == 0:
        if w <= 0:
            return None, None, ["long_distance=0 and spread_distance=0: cannot derive long leg"]
        ld = sd + w
        notes.append(f"long_distance not set -> short + spread_distance = {sd}+{w} = {ld}")
    elif sd == 0 and ld > 0 and w <= 0:
        # ATM near leg: nothing to derive — short_distance=0 IS the instruction.
        atm_near = True
        notes.append(f"short_distance=0 and spread_distance=0 -> near leg at ATM (reference strike), "
                     f"far leg at {ld}, width {ld}")
    elif sd == 0 and ld > 0:
        sd = ld - w
        if sd < step:
            notes.append(f"long - spread_distance = {ld}-{w} = {ld - w} < {step}; short floored to {step}")
            sd = step
        else:
            notes.append(f"short_distance not set -> long - spread_distance = {ld}-{w} = {sd}")
    else:                                               # both 0 -> legacy
        if w <= 0:
            return None, None, ["all of short/long/spread_distance are 0"]
        sd, ld = w, 2 * w
        notes.append(f"legacy: short = spread_distance = {sd}, long = 2x = {ld}")

    if sd >= ld:
        if w <= 0:
            return None, None, [f"inverted legs short={sd} >= long={ld} and spread_distance=0: cannot repair"]
        new_ld = sd + w
        notes.append(f"WARNING inverted/equal legs (short={sd}, long={ld}) -> long := short + spread_distance = {new_ld}")
        ld = new_ld

    def _snap(x, label, allow_zero=False):
        r = int(round(x / step) * step)
        if r < step and not (allow_zero and r == 0):
            r = step
        if r != x:
            notes.append(f"WARNING {label}={x} is not a multiple of {step}; snapped to {r}")
        return r
    sd = _snap(sd, "short_distance", allow_zero=atm_near)
    ld = _snap(ld, "long_distance")
    if sd >= ld:                                        # snapping could collapse them
        ld = sd + step
        notes.append(f"WARNING legs collided after snapping; long := {ld}")
    return sd, ld, notes


class CREDITSPREAD(Strategy):
    # --- Class Variables (set before bt.run()) ---
    symbol = "NIFTY"
    step_size = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25}
    OPTIONS_PATH = r"C:\Users\SHRENIK\Desktop\Datasets\Niftyoptions_data_parquet\NIFTY Options"
    option_symbol_format = "ddMMMyy"  # Option CSV naming; see OPTION_SYMBOL_FORMATS
    buffer_point = 10
    supertrend_sl_buffer = 10
    spread_distance = 150
    legs_resolved = False   # set by main() after resolve_leg_distances(); allows short_distance=0 (ATM)
    # LOOKAHEAD BOUND for at/after fills (entries, SL / flip exits, target
    # partials): the next print must be on the same day and within this many
    # minutes of the trigger. 0 = unbounded (legacy). See option_price_at_or_after.
    max_fill_gap_minutes = FILL_MAX_GAP_MINUTES
    # Leg offsets FROM THE REFERENCE STRIKE (ints, 0 = not set). Resolution rules
    # (see resolve_leg_distances()):
    #   short>0, long>0            -> use both as given; spread_distance ignored
    #   short>0, long=0            -> long  = short + spread_distance
    #   short=0, long>0            -> short = long  - spread_distance
    #   short>=long (inverted)     -> WARN, long = short + spread_distance
    #   both 0                     -> legacy: short = spread_distance, long = 2*spread_distance
    short_distance = 0
    long_distance = 0
    leg_notes = []
    requested_legs = (0, 0)
    # Which expiry to trade: "near" = current (nearest, can be 0-DTE on expiry day),
    # "next" = the one after (legacy behaviour), "far" = the one after next.
    expiry_selection = "next"
    last_bar_ts = None                # set in main(): timestamp of the final bar (end-of-data close)
    near_skip_0dte = True             # near + expiry day -> use next expiry instead of the 0-DTE contract
    lot_size = None                   # parsed in main() -> lot_spec (list of ranges)
    lot_spec = None
    hedges_qty_percent = 20
    # ---- CONFIG SWITCH: overnight hedge strike -------------------------------
    # 0   -> hedge = extra qty of the position's own LONG leg (current behaviour)
    # >0  -> hedge = option at reference_strike -/+ hedge_distance (PE: ref - d,
    #        CE: ref + d), same expiry / type, loaded at hedge time. If that file
    #        is missing or has no mark, falls back to the long leg and logs it.
    hedge_distance = 0
    # ---- CONFIG SWITCH: per-position max structural loss ---------------------
    # 0   -> lots sized purely from margin (current behaviour)
    # >0  -> lots = min(margin-based lots,
    #                   floor(capital * pct/100 / ((width - credit) * lot)))
    #        i.e. the worst-case loss of one position can never exceed pct% of
    #        trading capital. Margin sizing still applies as an upper bound.
    max_loss_per_position_pct = 0
    # ---- CONFIG SWITCH: spread type ------------------------------------------
    # "credit" (default, unchanged): long signal -> Bull Put credit spread,
    #           short signal -> Bear Call credit spread. Sell near, buy far.
    # "debit" : long signal -> Bull Call DEBIT spread (BUY CE at ref + short_distance,
    #           SELL CE at ref + long_distance); short signal -> Bear Put DEBIT
    #           spread (BUY PE at ref - short_distance, SELL PE at ref - long_distance).
    #           Net premium is PAID: max loss = debit x qty, max profit =
    #           (width - debit) x qty. Internally the spread is still stored as
    #           short - long, i.e. a NEGATIVE "credit" = -debit, so every leg-based
    #           P&L formula stays valid without change.
    #           Sizing: capital requirement = debit x lot (no SPAN); the
    #           structural max loss is the debit. Targets = % of MAX PROFIT
    #           (same meaning as for credit: target 31 = 31% of the most the
    #           spread can make). Overnight hedge is disabled (buying more of
    #           the near leg is not a hedge). Note: without
    #           max_loss_per_position_pct the margin-based sizing will buy a
    #           LOT of debit spreads because the premium is small — set the cap.
    spread_type = "credit"
    # ---- CONFIG SWITCH: position mode ----------------------------------------
    # "positional" (default, unchanged): carry overnight, hedge at close.
    # "intraday" : every open position is force-closed at intraday_exit_time on
    #              the SAME day ("Intraday Time Exit"). Nothing carries overnight,
    #              so no overnight hedge is ever opened.
    position_mode = "positional"
    intraday_exit_time = None      # None -> uses exit_time (15:20 by default)
    # ---- CONFIG SWITCH: session halt (e.g. closing-auction spikes) ----------
    # session_halt_mode:
    #   "off"   (default, unchanged)
    #   "halt"  keep positions but FREEZE all signal evaluation — no SL, no
    #           flip, no targets, no entries — for minutes inside
    #           [session_halt_start, session_halt_end). Evaluation resumes at
    #           halt end: if spot is still beyond the SL / flip level at the
    #           first active minute, the exit fires there; if it has come back
    #           inside, nothing happens. A window that wraps midnight
    #           (15:15 -> 09:20) means "from 15:15 today until 09:20 tomorrow".
    #   "close" square off any open position at session_halt_start
    #           ("Session Close Exit", scheduled fill at/before that minute)
    #           and freeze entries/evaluation until session_halt_end.
    # Scheduled exits (expiry-day exit_time, intraday_exit_time) are NOT
    # frozen — a position cannot be left to expire because of a halt. The
    # overnight hedge is unaffected (its times are already fixed).
    session_halt_mode = "off"
    session_halt_start = dtime(15, 15)
    session_halt_end = dtime(9, 20)
    spot_m1_active = None          # bool mask over the 1-min spot bars; None = no halt
    # ---- CONFIG SWITCH: targets ----------------------------------------------
    # True (default, unchanged): OMS scaling at t1/t2/t3.
    # False: no partial target exits at all — the whole position is booked only
    #        on SuperTrend flip, SuperTrend SL, or a time exit.
    use_targets = True
    # ---- CONFIG SWITCH: how a SuperTrend flip against the position is handled
    # "breakout" (default, unchanged): wait until spot breaks the flip candle
    #             +/- buffer, then close AND open the opposite spread at that
    #             same minute (same-minute reversal, 4 legs at one print).
    # "immediate": close at the FIRST evaluated minute after the flip bar
    #             closes (i.e. one minute after the trend change; halted
    #             minutes are skipped). No reversal at that minute. The new
    #             direction must then earn its entry through the normal
    #             breakout rule (flip candle high/low +/- buffer). If the trend
    #             flips back before that breakout, the flip-back candle becomes
    #             the signal candle and re-entry follows its breakout.
    flip_exit_mode = "breakout"
    # ---- CONFIG SWITCH: exit sanity gate ------------------------------------
    # "off"       (default, unchanged): exit legs are booked at whatever the
    #             option file printed, even if that print is impossible.
    # "intrinsic": every exit leg is floored at its intrinsic value at the fill
    #             minute (CE: spot - strike, PE: strike - spot, spot = that
    #             minute's close), and the exit spread is clipped to
    #             [0, width] (credit) / [-width, 0] (debit). Needs only spot
    #             and strike — works exactly when the option file has no valid
    #             print (crash days). Every adjustment is logged and counted.
    exit_sanity_gate = "off"
    # ---- CONFIG SWITCH: missing option files ---------------------------------
    # "skip"      (default, unchanged): entry skipped, logged `option_file_missing`.
    # "intrinsic": a leg whose file is missing/empty is priced from spot for the
    #             whole life of the trade as max(intrinsic, 0.05) at each 1-min
    #             close — NO time value. Entry, exits, targets and the audit
    #             then run on that synthetic series unchanged. The trade is
    #             tagged `synthetic_legs` (short / long / both) in the report
    #             and logged `synthetic_leg_intrinsic` in Skipped Entries, so
    #             its P&L can be examined separately. Meant for diagnosis, not
    #             for reported results: an OTM leg is worth 0.05 under this
    #             rule, so a synthetic SHORT leg gives almost no credit and a
    #             synthetic LONG leg costs almost nothing.
    missing_option_pricing = "skip"
    exit_gate_adjustments = 0
    exit_gate_inr = 0.0
    hedges_allowed = True             # False -> never open the 15:25 overnight hedge

    trading_capital = 1_000_000
    capital_utilization_percent = 50
    margin = None                     # DEFAULT_MARGIN merged with user overrides (set in main)
    compound_capital = False          # True: realised net P&L is added to trading_capital

    no_entry_after = dtime(15, 15)
    end_of_day_candle = dtime(15, 15)
    exit_time = dtime(15, 20)
    hedge_entry_time = dtime(15, 25)
    hedge_exit_time = dtime(9, 20)
    targets_credit_spread = {"t1": 40, "t2": 80, "t3": 98}
    # Qty booked at each target as % of TOTAL position qty. t3 is always the
    # remainder (whatever is left), so only t1 and t2 are used for sizing; t3
    # is informational and must equal 100 - t1 - t2. Defaults reproduce the
    # previous hard-coded 40 / 40 / 20 split exactly.
    targets_qty_credit_spread = {"t1": 40, "t2": 40, "t3": 20}
    partial_qty_adjustments = 0   # count of partial exits where lot rounding changed the requested qty

    # FIX (Bug 1): 1-min spot arrays for intrabar trigger detection
    spot_m1_ts = None                 # int64 ns array
    spot_m1_high = None
    spot_m1_low = None
    # REPORT ADD-ON (intrinsic audit): open/close of the same 1-min spot bars.
    # Never read by the trading logic — only by build_intrinsic_audit().
    spot_m1_open = None
    spot_m1_close = None
    tf_minutes = 15

    # FIX (Bug 3): costs; FIX (Bug 4/5): loud skip log
    costs = dict(DEFAULT_COSTS)
    min_entry_credit = 2.0            # reject entries whose net credit is below this

    signals = []
    hedges_log = []
    skipped_log = []
    # Run-wide charge total per line item (spread legs AND hedge legs), so the
    # report can itemise costs instead of publishing one opaque total.
    charges_breakdown = {}

    # ------------------------------------------------------------------
    def _charges(self, sell_turnover, buy_turnover, n_orders, on_date=None):
        """Charges for one set of fills, accrued into the run-wide breakdown.

        Every charge in the backtest flows through here, so the itemised
        totals always reconcile with what was actually debited.
        """
        parts = charge_components(sell_turnover, buy_turnover, n_orders, self.costs, on_date)
        for name, value in parts.items():
            CREDITSPREAD.charges_breakdown[name] = (
                CREDITSPREAD.charges_breakdown.get(name, 0.0) + value
            )
        return sum(parts.values())

    # ------------------------------------------------------------------
    def generate_symbol(self, option_type, expiry_val, strike):
        """
        Build the options CSV filename key, in whichever vendor naming
        convention `option_symbol_format` selects:

            "ddMMMyy" -> NIFTY02JUN2622900CE   (default)
            "yymmdd"  -> NIFTY2003059650CE
        """
        return build_option_symbol(
            self.symbol, expiry_val, strike, option_type,
            fmt=self.option_symbol_format,
        )

    def _log_skip(self, ts, reason, detail=""):
        CREDITSPREAD.skipped_log.append({
            "timestamp": ts, "reason": reason, "detail": detail
        })
        print(f"[SKIPPED ENTRY] {ts} | {reason} | {detail}")

    # ---- fill helpers (slippage applied adversely per leg) ----
    def _fill_sell(self, df, ts, scheduled=False):
        px, used = (option_price_at_or_before(df, ts) if scheduled
                    else option_price_at_or_after(df, ts, self.max_fill_gap_minutes))
        if px is None:
            return None, None
        return max(px - self.costs["slippage_per_leg"], 0.05), used

    def _fill_buy(self, df, ts, scheduled=False):
        px, used = (option_price_at_or_before(df, ts) if scheduled
                    else option_price_at_or_after(df, ts, self.max_fill_gap_minutes))
        if px is None:
            return None, None
        return px + self.costs["slippage_per_leg"], used

    # ------------------------------------------------------------------
    def _force_close_active_hedge(self, exit_timestamp, scheduled=False):
        if self.current_trade.get("active_hedge") is None:
            return
        active_hedge = self.current_trade["active_hedge"]
        long_df = self.current_trade["long_option"].get("options_data")
        # hedge_distance > 0 stores its own option series; None -> long leg (unchanged)
        if active_hedge.get("options_data") is not None:
            long_df = active_hedge["options_data"]
        exit_price, _ = self._fill_sell(long_df, exit_timestamp, scheduled=scheduled)
        if exit_price is None:
            exit_price, _ = self._fill_sell(long_df, exit_timestamp, scheduled=True)
        if exit_price is None:
            exit_price = active_hedge["entry_price"]
        qty = active_hedge["qty"]
        charges = self._charges(
            sell_turnover=exit_price * qty,
            buy_turnover=active_hedge["entry_price"] * qty,
            n_orders=2, on_date=exit_timestamp)
        CREDITSPREAD.hedges_log.append({
            "position_id": self.current_trade.get("position_id"),
            "hedge_entry_time": datetime.combine(active_hedge["entry_date"], active_hedge["entry_time"]),
            "hedge_exit_time": exit_timestamp,
            "hedge_strike": active_hedge.get("strike", self.current_trade["long_option"]["strike_price"]),
            "hedge_source": active_hedge.get("source", "long_leg"),
            "entry_price": active_hedge["entry_price"],
            "exit_price": exit_price,
            "qty": qty,
            "points_captured": exit_price - active_hedge["entry_price"],
            "charges_inr": charges,
        })
        self.current_trade["active_hedge"] = None

    # ------------------------------------------------------------------
    def trade_finished(self):
        """Finalize the trade: one signals-row per exit event, WITH charges.
        Entry-leg charges are attached to the first exit row (Bug 3 fix)."""
        if self.current_trade["exit_price"] is None:
            self.current_trade["exit_price"] = self.current_trade["entry_price"]

        short_entry = self.current_trade["short_option"]["entry_price"]
        long_entry = self.current_trade["long_option"]["entry_price"]
        credit_entry = self.current_trade["entry_price"]
        total_qty = self.current_trade["total_qty"] or 0

        first_row_done = False
        for exit_prefix, exit_reason in [
            ("exit1", "Target 1"),
            ("exit2", "Target 2"),
            ("exit3", "Target 3"),
            ("final_exit", self.current_trade["reason_for_exit"]),
        ]:
            qty = self.current_trade["credit_spread"].get(f"{exit_prefix}_qty")
            if qty is None or qty <= 0:
                continue
            short_exit = self.current_trade["credit_spread"].get(f"{exit_prefix}_short")
            long_exit = self.current_trade["credit_spread"].get(f"{exit_prefix}_long")
            exit_spread = self.current_trade["credit_spread"].get(exit_prefix)
            exit_time = self.current_trade["credit_spread"].get(f"{exit_prefix}_time")
            spot_exit = self.current_trade["credit_spread"].get(f"{exit_prefix}_spot")
            if spot_exit is None:
                spot_exit = self.data.Close[-1]

            short_points = (short_entry - short_exit) if short_exit is not None else None
            long_points = (long_exit - long_entry) if long_exit is not None else None
            pnl_inr = 0
            if short_points is not None and long_points is not None:
                pnl_inr = (short_points + long_points) * qty

            # ---- Bug 3 fix: statutory charges for this exit event ----
            sell_turn = (long_exit or 0.0) * qty          # exit: sell the long leg
            buy_turn = (short_exit or 0.0) * qty          # exit: buy back the short leg
            n_orders = 2
            if not first_row_done:
                sell_turn += (short_entry or 0.0) * total_qty   # entry: sold short leg
                buy_turn += (long_entry or 0.0) * total_qty     # entry: bought long leg
                n_orders += 2
            charges = self._charges(sell_turn, buy_turn, n_orders, on_date=exit_time)
            first_row_done = True

            credit_points_captured = (credit_entry - exit_spread) if exit_spread is not None else None

            CREDITSPREAD.signals.append({
                "signal_timestamp": self.current_trade["signal_timestamp"],
                "signal_type": self.current_trade["signal_type"],
                "entry_breakout_high": self.current_trade["buffer_candle"]["high"],
                "entry_breakout_low": self.current_trade["buffer_candle"]["low"],
                "supertrend_value": self.current_trade["supertrend_value"],
                "supertrend_direction": self.current_trade["supertrend_direction"],
                "spot_price_at_entry": self.current_trade["spot_price_at_entry"],
                "spot_price_at_exit": spot_exit,
                "symbol": self.current_trade["symbol"],
                "current_expiry": self.current_trade["expiry_date"]["current_expiry"],
                "trade_expiry": self.current_trade["expiry_date"]["trade_expiry"],
                "rolled_from_0dte": bool(self.current_trade.get("rolled_from_0dte", False)),
                "reference_strike": self.current_trade["strike"],
                "option_type": self.current_trade["option_type"],
                "entry_type": self.current_trade["entry_type"],
                "short_strike": self.current_trade["short_option"]["strike_price"],
                "long_strike": self.current_trade["long_option"]["strike_price"],
                "entry_time": self.current_trade["entry_time"],
                "credit_spread_entry": credit_entry,
                "short_entry_price": short_entry,
                "long_entry_price": long_entry,
                "exit_timestamp": exit_time,
                "credit_spread_exit": exit_spread,
                "credit_points_captured": credit_points_captured,
                "reason_for_exit": exit_reason,
                "trade_number_today": self.current_trade["trade_number_today"],
                "total_qty": total_qty,
                "lots": self.current_trade["lots"],
                "lot_size": self.current_trade["eff_lot_size"],
                "margin_per_lot": self.current_trade["margin_per_lot"],
                "span_margin": (self.current_trade["span_per_lot"] or 0) * (self.current_trade["lots"] or 0),
                "exposure_margin": (self.current_trade["exposure_per_lot"] or 0) * (self.current_trade["lots"] or 0),
                "premium_received": self.current_trade["premium_received"],
                "capital_used": self.current_trade["capital_used"],
                "trading_capital_at_entry": self.current_trade["trading_capital_at_entry"],
                "exit_time": exit_time,
                "exit_qty": qty,
                "short_exit_price": short_exit,
                "long_exit_price": long_exit,
                "short_points": short_points,
                "long_points": long_points,
                "profit_in_inr": pnl_inr,
                "charges_inr": charges,
                "net_profit_in_inr": pnl_inr - charges,
                "position_id": self.current_trade.get("position_id"),
                "structural_max_loss_inr": self.current_trade.get("structural_max_loss_inr"),
                "sizing_mode": self.current_trade.get("sizing_mode"),
                "spread_type": self.spread_type,
                "net_premium": credit_entry,   # +credit received / -debit paid (per unit)
                "synthetic_legs": self.current_trade.get("synthetic_legs", ""),
            })

        if self.compound_capital:
            # net of charges for THIS position (all exit rows just appended)
            pid = self.current_trade.get("position_id")
            net = sum(r["net_profit_in_inr"] for r in CREDITSPREAD.signals if r.get("position_id") == pid)
            CREDITSPREAD.trading_capital += net

        self.current_trade = default_records()

    # ------------------------------------------------------------------
    def _close_full(self, trigger_ts, reason, spot_level, scheduled=False):
        """Close the full remaining position with legs priced at/after the
        trigger minute (Bug 1 fix). Scheduled closes price at the schedule."""
        short_df = self.current_trade["short_option"].get("options_data")
        long_df = self.current_trade["long_option"].get("options_data")
        short_px, used_ts = self._fill_buy(short_df, trigger_ts, scheduled=scheduled)
        long_px, _ = self._fill_sell(long_df, trigger_ts, scheduled=scheduled)
        if short_px is None or long_px is None:
            # No mark at/after the trigger (series ended). Use the last mark
            # BEFORE it and say so — better than the old behaviour of booking
            # the exit at the entry price (a fake zero-P&L trade).
            short_px, used_ts = self._fill_buy(short_df, trigger_ts, scheduled=True)
            long_px, _ = self._fill_sell(long_df, trigger_ts, scheduled=True)
            self._log_skip(trigger_ts, "stale_exit_mark",
                           f"pos {self.current_trade.get('position_id')} {reason}: no option mark at/after trigger; used last available")
            if short_px is None or long_px is None:
                short_px = self.current_trade["short_option"]["entry_price"]
                long_px = self.current_trade["long_option"]["entry_price"]
                used_ts = trigger_ts
        short_px, long_px = self._apply_exit_gate(short_px, long_px, trigger_ts,
                                                  self.current_trade["remaining_qty"], reason)
        spread_val = short_px - long_px
        self.current_trade["exit_timestamp"] = trigger_ts
        self.current_trade["reason_for_exit"] = reason
        self.current_trade["exit_price"] = spread_val
        cs = self.current_trade["credit_spread"]
        cs["final_exit_time"] = trigger_ts
        cs["final_exit"] = spread_val
        cs["final_exit_qty"] = self.current_trade["remaining_qty"]
        cs["final_exit_short"] = short_px
        cs["final_exit_long"] = long_px
        cs["final_exit_spot"] = spot_level
        self.current_trade["remaining_qty"] = 0
        self._force_close_active_hedge(trigger_ts, scheduled=scheduled)
        self.trade_finished()

    def _target_threshold(self, entry_credit, tkey):
        """Spread level (short - long) at which target `tkey` fires. The scan
        always tests `credit_spread <= thr`, for both spread types.
        credit: thr = C x (1 - t%)            (spread has decayed t% of the credit
                                               = t% of max profit)
        debit : thr = -(D + t% x (W - D))     (spread value has gained t% of
                                               max profit; stored as -value)"""
        t = self.targets_credit_spread[tkey] / 100.0
        if self.spread_type == "debit":
            D = -float(entry_credit)                       # entry_credit is -debit
            W = float(self._leg_offsets()[1] - self._leg_offsets()[0])
            return -(D + t * (W - D))
        return entry_credit * (1.0 - t)

    def _partial_exit(self, which, row_ts, spread_row):
        """Book a partial target exit at the spread row that actually crossed
        the threshold (fill == detection mark; slippage applied)."""
        cs = self.current_trade["credit_spread"]
        total_qty = self.current_trade["total_qty"]          # Bug 7 fix
        slip = self.costs["slippage_per_leg"]
        # LOOKAHEAD FIX: `spread_row` is the 1-min close that first crossed the
        # threshold. Live, that print is only observable once the minute has
        # closed, so the order fills on the NEXT available mark. Fall back to
        # the detection row only if nothing later exists in the series.
        spread_df = self.current_trade.get("spread_data")
        fill_row = spread_row
        detect_ts = pd.Timestamp(row_ts)
        if spread_df is not None and not spread_df.empty:
            nxt = spread_df[spread_df["timestamp"] > detect_ts]
            if not nxt.empty:
                cand = nxt.iloc[0]
                cand_ts = pd.Timestamp(cand["timestamp"])
                gap = int(self.max_fill_gap_minutes or 0)
                # LOOKAHEAD BOUND: the next mark must be the same day and within
                # max_fill_gap_minutes. A gap in either leg's prints would
                # otherwise fill this order at a price from hours/days later.
                if gap <= 0 or (cand_ts.date() == detect_ts.date()
                                and (cand_ts - detect_ts) <= pd.Timedelta(minutes=gap)):
                    fill_row = cand
                    row_ts = cand_ts
                else:
                    self._log_skip(detect_ts, "partial_fill_no_next_mark",
                                   f"pos {self.current_trade.get('position_id')} {which}: next mark at {cand_ts} is "
                                   f"{(cand_ts - detect_ts)} after detection (> {gap} min); filled at detection mark")
        short_px = float(fill_row["Close_short"]) + slip    # buy back short
        long_px = max(float(fill_row["Close_long"]) - slip, 0.05)  # sell long
        short_px, long_px = self._apply_exit_gate(short_px, long_px, row_ts, qty, which)
        spread_val = short_px - long_px
        eff_lot = self.current_trade.get("eff_lot_size") or self.lot_spec[-1][2]
        if which == "exit3":
            qty = self.current_trade["remaining_qty"]
        else:
            # BUG FIX: partial exits must be whole lots (int(900*0.4)=360 is
            # 7.2 lots — unfillable). Round down to lot multiples, min 1 lot.
            # Qty % per target comes from targets_qty_credit_spread (t1 / t2);
            # default 40 / 40 keeps the old behaviour exactly.
            qkey = "t1" if which == "exit1" else "t2"
            q_pct = float(self.targets_qty_credit_spread.get(qkey, 40))
            # integer arithmetic: int(650 * 0.70) == 454 in floating point and
            # that silently drops a whole lot. (qty * pct) // 100 has no such issue.
            requested = int((int(total_qty) * int(round(q_pct))) // 100)
            qty = max(eff_lot, (requested // eff_lot) * eff_lot)
            qty = min(qty, self.current_trade["remaining_qty"])
            if qty != requested:
                # Lot rounding / small size changed the split. Count it and log it
                # so "40/40/20" is never assumed when the book is 1-2 lots.
                CREDITSPREAD.partial_qty_adjustments = getattr(CREDITSPREAD, "partial_qty_adjustments", 0) + 1
                self._log_skip(row_ts, "partial_exit_qty_adjusted",
                               f"pos {self.current_trade.get('position_id')} {which}: requested {q_pct:.0f}% = {requested} "
                               f"of {total_qty}, booked {qty} (lot {eff_lot}, remaining before {self.current_trade['remaining_qty']})")
        cs[which] = spread_val
        cs[f"{which}_time"] = row_ts
        cs[f"{which}_qty"] = qty
        cs[f"{which}_short"] = short_px
        cs[f"{which}_long"] = long_px
        cs[f"{which}_spot"] = self.data.High[-1] if self.current_trade["signal_type"] == "long" else self.data.Low[-1]
        self.current_trade["remaining_qty"] -= qty
        if which == "exit3" or self.current_trade["remaining_qty"] <= 0:
            self.current_trade["remaining_qty"] = 0
            self.current_trade["exit_timestamp"] = row_ts
            self.current_trade["exit_price"] = spread_val
            self.current_trade["reason_for_exit"] = "Target 3 Reached"
            self._force_close_active_hedge(row_ts, scheduled=False)
            self.trade_finished()
            return True
        return False

    def _before_halt(self, t):
        """If a session halt is on and the scheduled exit time `t` falls at or
        after session_halt_start, pull it to ONE MINUTE BEFORE the halt starts,
        so an expiry-day / intraday exit is never left inside a frozen window.
        Halt off -> returns t unchanged."""
        if self.session_halt_mode == "off" or t is None:
            return t
        a = self.session_halt_start
        if t >= a:
            m = a.hour * 60 + a.minute - 1
            return dtime(m // 60, m % 60)
        return t

    def _expiry_exit_time(self):
        return self._before_halt(self.exit_time)

    def _synthetic_option_df(self, option_type, strike):
        """1-min option series built from the spot closes: max(intrinsic, 0.05).
        Same columns as a parsed option file so every fill routine works."""
        if self.spot_m1_ts is None or self.spot_m1_close is None:
            return None
        spot = np.asarray(self.spot_m1_close, dtype=float)
        intr = np.maximum(spot - float(strike), 0.0) if str(option_type).upper() == "CE" else np.maximum(float(strike) - spot, 0.0)
        px = np.maximum(intr, 0.05)
        ts = pd.to_datetime(self.spot_m1_ts)
        return pd.DataFrame({"Date": ts.strftime("%Y%m%d"), "Time": ts.strftime("%H:%M"),
                             "Open": px, "High": px, "Low": px, "Close": px,
                             "Volume": 0, "IO": 0, "timestamp": ts})

    def _spot_close_at(self, ts):
        """1-min spot close of the bar containing ts (None if no spot data)."""
        if self.spot_m1_ts is None or self.spot_m1_close is None or len(self.spot_m1_ts) == 0:
            return None
        i = int(np.searchsorted(self.spot_m1_ts, np.int64(pd.Timestamp(ts).value), side="right")) - 1
        i = min(max(i, 0), len(self.spot_m1_ts) - 1)
        return float(self.spot_m1_close[i])

    def _apply_exit_gate(self, short_px, long_px, ts, qty, label):
        """Floor each exit leg at intrinsic and clip the spread to the
        structural range. Returns (short_px, long_px). No-op when off."""
        if self.exit_sanity_gate != "intrinsic":
            return short_px, long_px
        spot = self._spot_close_at(ts)
        if spot is None:
            return short_px, long_px
        opt = self.current_trade["option_type"]
        ss = self.current_trade["short_option"]["strike_price"]
        ls = self.current_trade["long_option"]["strike_price"]
        def _intr(k):
            return max(spot - k, 0.0) if opt == "CE" else max(k - spot, 0.0)
        s_floor, l_floor = _intr(ss), _intr(ls)
        new_s, new_l = max(short_px, s_floor), max(long_px, l_floor)
        so, lo = self._leg_offsets()
        width = float(lo - so)
        spread = new_s - new_l
        if self.spread_type == "debit":
            clipped = min(max(spread, -width), 0.0)
        else:
            clipped = min(max(spread, 0.0), width)
        if abs(clipped - spread) > 1e-9:
            # keep the floored short leg, move the long leg to honour the clip
            new_l = new_s - clipped
        if abs(new_s - short_px) > 1e-9 or abs(new_l - long_px) > 1e-9:
            old_val, new_val = short_px - long_px, new_s - new_l
            inr = (new_val - old_val) * qty          # + = raw P&L was overstated (P&L = (entry - exit) x qty)
            CREDITSPREAD.exit_gate_adjustments = getattr(CREDITSPREAD, "exit_gate_adjustments", 0) + 1
            CREDITSPREAD.exit_gate_inr = getattr(CREDITSPREAD, "exit_gate_inr", 0.0) + inr
            self._log_skip(ts, "exit_floored_at_intrinsic",
                           f"pos {self.current_trade.get('position_id')} {label}: spot {spot:.2f}; short {short_px:.2f}->{new_s:.2f} "
                           f"(intrinsic {s_floor:.2f}), long {long_px:.2f}->{new_l:.2f} (intrinsic {l_floor:.2f}); "
                           f"spread {old_val:.2f}->{new_val:.2f}; P&L overstated by {inr:,.0f} INR")
        return new_s, new_l

    def _first_active_minute(self, start_ts, end_ts):
        """First 1-min spot bar in [start_ts, end_ts) that is not halted."""
        lo_i = np.searchsorted(self.spot_m1_ts, np.int64(pd.Timestamp(start_ts).value), side="left")
        hi_i = np.searchsorted(self.spot_m1_ts, np.int64(pd.Timestamp(end_ts).value), side="left")
        if lo_i >= hi_i:
            return None
        if self.spot_m1_active is None:
            return pd.Timestamp(self.spot_m1_ts[lo_i])
        act = np.nonzero(self.spot_m1_active[lo_i:hi_i])[0]
        return pd.Timestamp(self.spot_m1_ts[lo_i + act[0]]) if len(act) else None

    def _intraday_exit_time(self):
        return self._before_halt(self.intraday_exit_time or self.exit_time)

    def _halt_active_rows(self, ts_series):
        """Boolean Series: True where the timestamp is OUTSIDE the session halt
        (evaluation allowed). All True when the halt is off."""
        if self.session_halt_mode == "off":
            return pd.Series(True, index=ts_series.index)
        t = pd.to_datetime(ts_series).dt.time
        a, b = self.session_halt_start, self.session_halt_end
        if a <= b:
            in_halt = (t >= a) & (t < b)                  # same-day window
        else:
            in_halt = (t >= a) | (t < b)                  # wraps overnight
        return ~in_halt

    def _maybe_open_evening_hedge(self, current_date, current_time, bar_end=None):
        # The hedge must go on at the LAST bar of the session. Don't rely solely
        # on `end_of_day_candle` matching the bar label (a config of 15:20/15:30
        # on a 15-min grid whose last label is 15:15 silently disabled hedging
        # for the whole run). The bar whose end reaches the 15:30 close is the
        # last bar regardless of timeframe.
        is_last_bar = current_time >= self.end_of_day_candle
        if bar_end is not None:
            be = pd.Timestamp(bar_end)
            is_last_bar = is_last_bar or be.date() > current_date or be.time() >= dtime(15, 30)
        if (self.hedges_allowed and
                self.spread_type == "credit" and self.position_mode == "positional" and
                self.current_trade["entry_price"] is not None and
                self.current_trade.get("remaining_qty", 0) > 0 and
                self.current_trade.get("active_hedge") is None and
                is_last_bar):
            CREDITSPREAD.hedges_opened_count = getattr(CREDITSPREAD, "hedges_opened_count", 0) + 1
            eff_lot = self.current_trade.get("eff_lot_size") or self.lot_spec[-1][2]
            hedge_lots = max(1, int((self.current_trade["remaining_qty"] / eff_lot) * self.hedges_qty_percent / 100))
            hedge_qty = hedge_lots * eff_lot
            long_df = self.current_trade["long_option"].get("options_data")
            target_entry_time = datetime.combine(current_date, self.hedge_entry_time)
            # ---- hedge_distance switch (0 = long leg, unchanged) ----
            hedge_df, hedge_strike, hedge_src = long_df, self.current_trade["long_option"]["strike_price"], "long_leg"
            if int(self.hedge_distance or 0) > 0:
                alt_df, alt_strike, alt_key = self._load_hedge_option(self.hedge_distance)
                if alt_df is not None:
                    hedge_df, hedge_strike, hedge_src = alt_df, alt_strike, "hedge_distance"
                else:
                    self._log_skip(target_entry_time, "hedge_file_missing_fallback_long_leg",
                                   f"pos {self.current_trade.get('position_id')} {alt_key}: using long leg for hedge")
            entry_price, _ = self._fill_buy(hedge_df, target_entry_time, scheduled=True)
            if entry_price is None and hedge_src == "hedge_distance":
                # no mark on the alternative strike at hedge time -> fall back to long leg
                self._log_skip(target_entry_time, "hedge_no_mark_fallback_long_leg",
                               f"pos {self.current_trade.get('position_id')} strike {hedge_strike}: using long leg for hedge")
                hedge_df, hedge_strike, hedge_src = long_df, self.current_trade["long_option"]["strike_price"], "long_leg"
                entry_price, _ = self._fill_buy(hedge_df, target_entry_time, scheduled=True)
            if entry_price is None:
                entry_price = self.current_trade["long_option"]["entry_price"]
            self.current_trade["active_hedge"] = {
                "entry_date": current_date,
                "entry_time": self.hedge_entry_time,
                "entry_price": entry_price,
                "qty": hedge_qty,
                "strike": hedge_strike,
                "source": hedge_src,
                # None when hedging with the long leg -> close path uses long leg data (unchanged)
                "options_data": hedge_df if hedge_src == "hedge_distance" else None,
            }

    def _load_hedge_option(self, distance):
        """hedge_distance > 0: load the option `distance` pts from the reference
        strike on the same side/type/expiry as the position. Returns
        (df, strike, key) or (None, strike, key) when the file is unusable.
        Used ONLY by the overnight hedge; never touches spread legs."""
        step = self.step_size.get(self.symbol, 50)
        d = int(round(float(distance) / step) * step)
        ref = self.current_trade["strike"]
        opt = self.current_trade["option_type"]
        strike = ref - d if opt == "PE" else ref + d
        expiry = self.current_trade["expiry_date"]["trade_expiry"]
        key = self.generate_symbol(opt, expiry, strike)
        path = os.path.join(self.OPTIONS_PATH, key + ".parquet")
        if not os.path.exists(path):
            return None, strike, key
        try:
            df = pd.read_parquet(path)
            df.columns = ["Date", "Time", "Open", "High", "Low", "Close", "Volume", "IO"]
            df["timestamp"] = pd.to_datetime(df["Date"].astype(str) + " " + df["Time"].astype(str),
                                             format="%Y%m%d %H:%M")
            df.sort_values(by="timestamp", inplace=True)
            if df.empty:
                return None, strike, key
            return df, strike, key
        except Exception:
            return None, strike, key

    def _post_entry_same_bar_exits(self, trigger_ts, bar_end, current_time, current_date):
        """Entry-bar exit blindness fix (same class of bug as the gold engine):
        after an intrabar entry, scan the REMAINDER of the bar for SL / target /
        expiry-time exits. SL scanning starts AT the entry minute — if the entry
        minute's range also spans the SL, the stop counts (worst-case intrabar
        ordering; 1-min data cannot tell you which came first).
        The SL level uses SUPERT[-2]: the last bar COMPLETED before this bar
        started forming — exactly what a live system would have."""
        if self.current_trade["entry_price"] is None:
            return
        st_val = self.data["SUPERT"][-2] if len(self.data) >= 2 else None
        trend_dir = "long" if (len(self.data) >= 2 and self.data["SUPERTd"][-2] == 1) else "short"
        spread_df = self.current_trade.get("spread_data")
        trade_expiry = self.current_trade["expiry_date"]["trade_expiry"]
        cursor = pd.Timestamp(trigger_ts)

        while self.current_trade["entry_price"] is not None:
            events = []
            if trade_expiry is not None and current_date >= trade_expiry:
                sched = pd.Timestamp(datetime.combine(current_date, self._expiry_exit_time()))
                if sched < bar_end:
                    events.append((max(sched, cursor), 0, "TimeExit", None))
            elif self.position_mode == "intraday":
                # intraday mode: every day is an exit day
                sched = pd.Timestamp(datetime.combine(current_date, self._intraday_exit_time()))
                if sched < bar_end:
                    events.append((max(sched, cursor), 0, "IntradayExit", None))
            if self.session_halt_mode == "close":
                sched = pd.Timestamp(datetime.combine(current_date, self.session_halt_start))
                if cursor <= sched < bar_end:
                    events.append((sched, 0, "SessionCloseExit", None))
            if st_val is not None and trend_dir == self.current_trade["signal_type"]:
                if self.current_trade["signal_type"] == "long":
                    lvl = st_val - self.supertrend_sl_buffer
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               cursor, bar_end, lvl, "down", active_mask=self.spot_m1_active)
                else:
                    lvl = st_val + self.supertrend_sl_buffer
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               cursor, bar_end, lvl, "up", active_mask=self.spot_m1_active)
                if m is not None:
                    events.append((m, 1, "SL", lvl))
            if self.use_targets and spread_df is not None and not spread_df.empty:
                entry_credit = self.current_trade["entry_price"]
                win = spread_df[(spread_df["timestamp"] >= cursor) & (spread_df["timestamp"] < bar_end)]
                if self.session_halt_mode != "off" and not win.empty:
                    win = win[self._halt_active_rows(win["timestamp"]).values]
                if not win.empty and entry_credit and (entry_credit > 0 or self.spread_type == "debit"):
                    for i, (tkey, which) in enumerate([("t1", "exit1"), ("t2", "exit2"), ("t3", "exit3")]):
                        if self.current_trade["credit_spread"].get(which) is not None:
                            continue
                        thr = self._target_threshold(entry_credit, tkey)
                        hit = win[win["credit_spread"] <= thr]
                        if not hit.empty:
                            row = hit.iloc[0]
                            events.append((pd.Timestamp(row["timestamp"]), 3 + i, which, row))
            if not events:
                break
            events.sort(key=lambda e: (e[0], e[1]))
            ts, _, kind, payload = events[0]
            if kind == "TimeExit":
                self._close_full(ts, "Time Exit on Expiry", self.data.Close[-1], scheduled=True)
                return
            if kind == "IntradayExit":
                self._close_full(ts, "Intraday Time Exit", self.data.Close[-1], scheduled=True)
                return
            if kind == "SessionCloseExit":
                self._close_full(ts, "Session Close Exit", self.data.Close[-1], scheduled=True)
                return
            if kind == "SL":
                sig_type = self.current_trade["signal_type"]
                self._close_full(ts, "SuperTrend SL Hit", payload, scheduled=False)
                if current_time < self.no_entry_after:
                    favorable = ((sig_type == "long" and self.data["Close"][-1] > st_val) or
                                 (sig_type == "short" and self.data["Close"][-1] < st_val))
                    if favorable:
                        self.pending_sl_reentry = {
                            "signal_type": sig_type,
                            "st_val": st_val,
                            "buffer_high": self.data["High"][-1] + self.buffer_point,
                            "buffer_low": self.data["Low"][-1] - self.buffer_point,
                        }
                return
            finished = self._partial_exit(kind, ts, payload)
            if finished:
                return
            cursor = ts + pd.Timedelta(minutes=1)

        # survived the entry bar — evening hedge if this was the last candle
        self._maybe_open_evening_hedge(current_date, current_time, bar_end)

    # ------------------------------------------------------------------
    def init(self):
        self.current_trade = default_records()
        self.pending_sl_reentry = None
        self.trades_today = 0
        self.trade_counter = 0
        self.current_date = None
        self.prev_date = None

    # ------------------------------------------------------------------
    def next(self):
        if len(self.data) < 10:
            return

        bar_start = pd.Timestamp(self.data.index[-1])
        bar_end = bar_start + pd.Timedelta(minutes=self.tf_minutes)
        current_time = bar_start.time()
        current_date = bar_start.date()
        close = self.data.Close[-1]
        step = self.step_size.get(self.symbol, 50)

        if self.current_date != current_date:
            self.prev_date = self.current_date
            self.current_date = current_date
            self.trades_today = 0

        # =====================================================================
        # OVERNIGHT HEDGE (scheduled actions -> priced at the schedule)
        # =====================================================================
        if self.current_trade.get("active_hedge") is not None:
            active_hedge = self.current_trade["active_hedge"]
            if current_date > active_hedge["entry_date"] and current_time >= self.hedge_exit_time:
                self._force_close_active_hedge(
                    datetime.combine(current_date, self.hedge_exit_time), scheduled=True)

        # NOTE: evening hedge entry moved to AFTER exit processing — the
        # original opened a 15:25 hedge before checking whether the position
        # was still alive during the bar, producing hedges on dead positions.

        # =====================================================================
        # EXIT MANAGEMENT — intrabar events resolved in TIME order (Bug 1 fix)
        # =====================================================================
        if self.current_trade["entry_price"] is not None:

            # ---- arm / reset the flip-exit pending state (completed-bar info) ----
            signal_val = self.data["signal"][-2] if len(self.data) >= 2 else None
            if signal_val is not None and pd.notna(signal_val) and signal_val == self.current_trade["signal_type"]:
                self.current_trade["exit_signal"] = None
                self.current_trade["exit_buffer_candle"] = {"high": None, "low": None}
            st_long_exit = (self.current_trade["signal_type"] == "long" and signal_val == "short")
            st_short_exit = (self.current_trade["signal_type"] == "short" and signal_val == "long")
            if (st_long_exit or st_short_exit) and self.current_trade["exit_signal"] is None:
                self.current_trade["exit_signal"] = signal_val
                self.current_trade["exit_signal_timestamp"] = self.data.index[-2]
                self.current_trade["exit_supertrend_value"] = self.data["SUPERT"][-2]
                self.current_trade["exit_buffer_candle"]["high"] = self.data["High"][-2] + self.buffer_point
                self.current_trade["exit_buffer_candle"]["low"] = self.data["Low"][-2] - self.buffer_point

            st_val = self.data["SUPERT"][-2] if len(self.data) >= 2 else None
            trend_dir = "long" if (len(self.data) >= 2 and self.data["SUPERTd"][-2] == 1) else "short"
            spread_df = self.current_trade.get("spread_data")
            trade_expiry = self.current_trade["expiry_date"]["trade_expiry"]

            cursor = bar_start
            while self.current_trade["entry_price"] is not None:
                events = []   # (ts, priority, kind, payload)

                # -- Expiry-day time exit (Bug 2 fix: compare against bar END) --
                if trade_expiry is not None and current_date >= trade_expiry:
                    if current_date > trade_expiry:
                        # overdue guard — should never happen, close immediately
                        events.append((cursor, 0, "TimeExit", None))
                    else:
                        sched = pd.Timestamp(datetime.combine(current_date, self._expiry_exit_time()))
                        if sched < bar_end:
                            events.append((max(sched, cursor), 0, "TimeExit", None))
                elif self.position_mode == "intraday":
                    # intraday mode: every day is an exit day
                    sched = pd.Timestamp(datetime.combine(current_date, self._intraday_exit_time()))
                    if sched < bar_end:
                        events.append((max(sched, cursor), 0, "IntradayExit", None))
                if self.session_halt_mode == "close":
                    sched = pd.Timestamp(datetime.combine(current_date, self.session_halt_start))
                    if cursor <= sched < bar_end:
                        events.append((sched, 0, "SessionCloseExit", None))

                # -- SuperTrend hard SL: trigger minute on 1-min spot --
                if st_val is not None and trend_dir == self.current_trade["signal_type"]:
                    if self.current_trade["signal_type"] == "long":
                        lvl = st_val - self.supertrend_sl_buffer
                        m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                                   cursor, bar_end, lvl, "down", active_mask=self.spot_m1_active)
                    else:
                        lvl = st_val + self.supertrend_sl_buffer
                        m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                                   cursor, bar_end, lvl, "up", active_mask=self.spot_m1_active)
                    if m is not None:
                        events.append((m, 1, "SL", lvl))

                # -- Flip-exit breakout: trigger minute on 1-min spot --
                if self.flip_exit_mode == "immediate" and self.current_trade["exit_signal"] in ("long", "short"):
                    m = self._first_active_minute(cursor, bar_end)
                    if m is not None:
                        events.append((m, 2, "FlipNow", (self.current_trade["exit_signal"], None)))
                elif self.current_trade["exit_signal"] == "long" and self.current_trade["exit_buffer_candle"]["high"] is not None:
                    lvl = self.current_trade["exit_buffer_candle"]["high"]
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               cursor, bar_end, lvl, "up", active_mask=self.spot_m1_active)
                    if m is not None:
                        events.append((m, 2, "Flip", ("long", lvl)))
                elif self.current_trade["exit_signal"] == "short" and self.current_trade["exit_buffer_candle"]["low"] is not None:
                    lvl = self.current_trade["exit_buffer_candle"]["low"]
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               cursor, bar_end, lvl, "down", active_mask=self.spot_m1_active)
                    if m is not None:
                        events.append((m, 2, "Flip", ("short", lvl)))

                # -- Targets: first spread row in window crossing threshold --
                if self.use_targets and spread_df is not None and not spread_df.empty:
                    entry_credit = self.current_trade["entry_price"]
                    win = spread_df[(spread_df["timestamp"] >= cursor) & (spread_df["timestamp"] < bar_end)]
                    if self.session_halt_mode != "off" and not win.empty:
                        win = win[self._halt_active_rows(win["timestamp"]).values]
                    if not win.empty and entry_credit and (entry_credit > 0 or self.spread_type == "debit"):
                        for i, (tkey, which) in enumerate([("t1", "exit1"), ("t2", "exit2"), ("t3", "exit3")]):
                            if self.current_trade["credit_spread"].get(which) is not None:
                                continue
                            thr = self._target_threshold(entry_credit, tkey)
                            hit = win[win["credit_spread"] <= thr]
                            if not hit.empty:
                                row = hit.iloc[0]
                                events.append((pd.Timestamp(row["timestamp"]), 3 + i, which, row))

                if not events:
                    break
                events.sort(key=lambda e: (e[0], e[1]))
                ts, _, kind, payload = events[0]

                if kind == "TimeExit":
                    self._close_full(ts, "Time Exit on Expiry", close, scheduled=True)
                    return
                if kind == "IntradayExit":
                    self._close_full(ts, "Intraday Time Exit", close, scheduled=True)
                    return
                if kind == "SessionCloseExit":
                    self._close_full(ts, "Session Close Exit", close, scheduled=True)
                    return

                if kind == "SL":
                    sig_type = self.current_trade["signal_type"]
                    self._close_full(ts, "SuperTrend SL Hit", payload, scheduled=False)
                    # False-SL re-entry: decided at bar close, acted on later bars
                    if current_time < self.no_entry_after:
                        favorable = ((sig_type == "long" and self.data["Close"][-1] > st_val) or
                                     (sig_type == "short" and self.data["Close"][-1] < st_val))
                        if favorable:
                            self.pending_sl_reentry = {
                                "signal_type": sig_type,
                                "st_val": st_val,
                                "buffer_high": self.data["High"][-1] + self.buffer_point,
                                "buffer_low": self.data["Low"][-1] - self.buffer_point,
                            }
                    return

                if kind == "FlipNow":
                    flip_dir, _ = payload
                    new_high = self.current_trade["exit_buffer_candle"]["high"]
                    new_low = self.current_trade["exit_buffer_candle"]["low"]
                    new_signal_time = self.current_trade["exit_signal_timestamp"]
                    new_st = self.current_trade["exit_supertrend_value"]
                    i_spot = np.searchsorted(self.spot_m1_ts, np.int64(pd.Timestamp(ts).value), side="left")
                    spot_now = float(self.spot_m1_close[i_spot]) if (self.spot_m1_close is not None and i_spot < len(self.spot_m1_ts)) else close
                    self._close_full(ts, "SuperTrend Flip", spot_now, scheduled=False)
                    # Arm the opposite direction as a normal pending entry: it
                    # enters only when spot breaks the flip candle +/- buffer.
                    self.current_trade["signal_type"] = flip_dir
                    self.current_trade["buffer_candle"]["high"] = new_high
                    self.current_trade["buffer_candle"]["low"] = new_low
                    self.current_trade["signal_timestamp"] = new_signal_time
                    self.current_trade["supertrend_value"] = new_st
                    self.current_trade["supertrend_direction"] = "BULLISH" if flip_dir == "long" else "BEARISH"
                    if current_time < self.no_entry_after:
                        scan_from = ts + pd.Timedelta(minutes=1)
                        if flip_dir == "long":
                            m2 = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                                        scan_from, bar_end, new_high, "up", active_mask=self.spot_m1_active)
                            if m2 is not None:
                                self.execute_entry(True, False, new_high, trigger_ts=m2)
                                self._post_entry_same_bar_exits(m2, bar_end, current_time, current_date)
                        else:
                            m2 = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                                        scan_from, bar_end, new_low, "down", active_mask=self.spot_m1_active)
                            if m2 is not None:
                                self.execute_entry(False, True, new_low, trigger_ts=m2)
                                self._post_entry_same_bar_exits(m2, bar_end, current_time, current_date)
                    # not broken out yet -> the pending signal persists into the
                    # next bar and the normal entry scan takes it from there
                    return

                if kind == "Flip":
                    flip_dir, lvl = payload
                    new_high = self.current_trade["exit_buffer_candle"]["high"]
                    new_low = self.current_trade["exit_buffer_candle"]["low"]
                    new_signal_time = self.current_trade["exit_signal_timestamp"]
                    new_st = self.current_trade["exit_supertrend_value"]
                    self._close_full(ts, "SuperTrend Flip", lvl, scheduled=False)
                    if current_time < self.no_entry_after:
                        self.current_trade["signal_type"] = "long" if flip_dir == "long" else "short"
                        self.current_trade["buffer_candle"]["high"] = new_high
                        self.current_trade["buffer_candle"]["low"] = new_low
                        self.current_trade["signal_timestamp"] = new_signal_time
                        self.current_trade["supertrend_value"] = new_st
                        self.current_trade["supertrend_direction"] = "BULLISH" if flip_dir == "long" else "BEARISH"
                        self.execute_entry(breaks_above=(flip_dir == "long"),
                                           breaks_below=(flip_dir == "short"),
                                           close=lvl, trigger_ts=ts)
                        self._post_entry_same_bar_exits(ts, bar_end, current_time, current_date)
                    return

                # Target partial exit
                finished = self._partial_exit(kind, ts, payload)
                if finished:
                    return
                cursor = ts + pd.Timedelta(minutes=1)

            # Position survived the bar — now (and only now) place the
            # scheduled 15:25 overnight hedge if this is the last candle.
            if self.last_bar_ts is not None and bar_start >= self.last_bar_ts:
                # END OF DATA: the backtest stops after this bar. A position left
                # open here used to vanish from the report. Close it at the last
                # available mark and label it so it is never mistaken for a
                # strategy exit. Its hedge is closed by _close_full.
                self._close_full(bar_end - pd.Timedelta(minutes=1), "End of Data (forced)",
                                 self.data.Close[-1], scheduled=True)
                return
            self._maybe_open_evening_hedge(current_date, current_time, bar_end)
            return  # still in trade, no entry logic

        # =====================================================================
        # ENTRY LOGIC (only if NOT in a trade)
        # =====================================================================
        if current_time >= self.no_entry_after:
            self.pending_sl_reentry = None
            return

        # --- False SL Re-Entry ---
        if self.pending_sl_reentry is not None:
            # LOOKAHEAD FIX: bar [-1] is the bar being scanned minute-by-minute;
            # its SuperTrend direction only exists at its close. Use [-2].
            current_trend = "long" if self.data["SUPERTd"][-2] == 1 else "short"
            if self.pending_sl_reentry["signal_type"] != current_trend:
                self.pending_sl_reentry = None
            else:
                if self.pending_sl_reentry["signal_type"] == "long":
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               bar_start, bar_end, self.pending_sl_reentry["buffer_high"], "up", active_mask=self.spot_m1_active)
                else:
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               bar_start, bar_end, self.pending_sl_reentry["buffer_low"], "down", active_mask=self.spot_m1_active)
                if m is not None:
                    sig_type = self.pending_sl_reentry["signal_type"]
                    self.current_trade["signal_type"] = sig_type
                    self.current_trade["signal_timestamp"] = self.data.index[-1]
                    self.current_trade["supertrend_value"] = self.data["SUPERT"][-2]
                    self.current_trade["supertrend_direction"] = "BULLISH" if self.data["SUPERTd"][-2] == 1 else "BEARISH"
                    self.current_trade["buffer_candle"]["high"] = self.pending_sl_reentry["buffer_high"]
                    self.current_trade["buffer_candle"]["low"] = self.pending_sl_reentry["buffer_low"]
                    breakout_price = (self.pending_sl_reentry["buffer_high"] if sig_type == "long"
                                      else self.pending_sl_reentry["buffer_low"])
                    self.pending_sl_reentry = None
                    self.execute_entry(sig_type == "long", sig_type == "short", breakout_price, trigger_ts=m)
                    self._post_entry_same_bar_exits(m, bar_end, current_time, current_date)
                    return

        # --- SuperTrend flip signal (completed candle [-2]) ---
        signal_val = self.data["signal"][-2] if len(self.data) >= 2 else None
        if signal_val is not None and not (isinstance(signal_val, float) and np.isnan(signal_val)):
            self.current_trade["signal_timestamp"] = self.data.index[-2]
            self.current_trade["signal_type"] = signal_val
            self.current_trade["supertrend_value"] = self.data["SUPERT"][-2]
            self.current_trade["supertrend_direction"] = "BULLISH" if self.data["SUPERTd"][-2] == 1 else "BEARISH"
            self.current_trade["buffer_candle"]["high"] = self.data["High"][-2] + self.buffer_point
            self.current_trade["buffer_candle"]["low"] = self.data["Low"][-2] - self.buffer_point

        if self.current_trade["signal_type"] is None or self.current_trade["buffer_candle"]["high"] is None:
            return

        # LOOKAHEAD FIX (was SUPERTd[-1]): the direction of the bar we are about
        # to trade inside is unknown until that bar closes. Gate on the last
        # COMPLETED bar. If the trend flips against us inside this bar, the
        # same-bar SL scan in _post_entry_same_bar_exits handles it honestly.
        current_trend = "long" if self.data["SUPERTd"][-2] == 1 else "short"
        if self.current_trade["signal_type"] != current_trend:
            self.current_trade["signal_type"] = None
            self.current_trade["buffer_candle"] = {"high": None, "low": None}
            return

        # --- Breakout: find the trigger MINUTE inside this bar (Bug 1 fix) ---
        if self.current_trade["signal_type"] == "long":
            m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                       bar_start, bar_end, self.current_trade["buffer_candle"]["high"], "up", active_mask=self.spot_m1_active)
            if m is not None:
                self.execute_entry(True, False, self.current_trade["buffer_candle"]["high"], trigger_ts=m)
                self._post_entry_same_bar_exits(m, bar_end, current_time, current_date)
        else:
            m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                       bar_start, bar_end, self.current_trade["buffer_candle"]["low"], "down", active_mask=self.spot_m1_active)
            if m is not None:
                self.execute_entry(False, True, self.current_trade["buffer_candle"]["low"], trigger_ts=m)
                self._post_entry_same_bar_exits(m, bar_end, current_time, current_date)

    # ------------------------------------------------------------------
    def _leg_offsets(self):
        """(short_offset, long_offset) from the reference strike.
        Legacy: (spread_distance, 2*spread_distance). Override with
        short_distance / long_distance to size each leg independently."""
        sd = int(self.short_distance or 0)
        ld = int(self.long_distance or 0)
        if getattr(self, "legs_resolved", False):
            return sd, ld          # main() already validated; sd may be 0 = ATM near leg
        if sd > 0 and ld > 0:
            return sd, ld
        # not resolved through main() (e.g. direct class use): fall back to legacy
        return int(self.spread_distance), int(2 * self.spread_distance)

    def execute_entry(self, breaks_above, breaks_below, close, trigger_ts=None):
        if self.data.index[-1].time() >= self.no_entry_after:
            self.current_trade = default_records()
            return
        if trigger_ts is None:
            trigger_ts = pd.Timestamp(self.data.index[-1])
        step = self.step_size.get(self.symbol, 50)
        short_off, long_off = self._leg_offsets()

        if breaks_above:
            self.current_trade["option_type"] = "PE"
            self.current_trade["entry_type"] = "Bull Put Credit Spread"
            trigger_level = self.current_trade["buffer_candle"]["high"]
            ref_strike = round_up_to_strike(trigger_level, step)
            short_strike = ref_strike - short_off     # sell PE short_off below ref
            long_strike = ref_strike - long_off       # buy  PE long_off  below ref
        elif breaks_below:
            self.current_trade["option_type"] = "CE"
            self.current_trade["entry_type"] = "Bear Call Credit Spread"
            trigger_level = self.current_trade["buffer_candle"]["low"]
            ref_strike = round_down_to_strike(trigger_level, step)
            short_strike = ref_strike + short_off     # sell CE short_off above ref
            long_strike = ref_strike + long_off       # buy  CE long_off  above ref
        else:
            return

        if self.spread_type == "debit":
            # Debit spread: BUY the near leg (short_off from ref), SELL the far leg
            # (long_off from ref), on the side the trend is heading. The engine's
            # "short_option" is always the leg we SELL and "long_option" the leg we
            # BUY, so only the strikes and option type change here.
            if breaks_above:
                self.current_trade["option_type"] = "CE"
                self.current_trade["entry_type"] = "Bull Call Debit Spread"
                long_strike = ref_strike + short_off      # buy  CE short_off above ref (near)
                short_strike = ref_strike + long_off      # sell CE long_off  above ref (far)
            else:
                self.current_trade["option_type"] = "PE"
                self.current_trade["entry_type"] = "Bear Put Debit Spread"
                long_strike = ref_strike - short_off      # buy  PE short_off below ref (near)
                short_strike = ref_strike - long_off      # sell PE long_off  below ref (far)

        self.current_trade["strike"] = ref_strike
        self.current_trade["short_option"]["strike_price"] = short_strike
        self.current_trade["short_option"]["option_type"] = self.current_trade["option_type"]
        self.current_trade["long_option"]["strike_price"] = long_strike
        self.current_trade["long_option"]["option_type"] = self.current_trade["option_type"]

        cols = self.data.df.columns
        self.current_trade["expiry_date"]["current_expiry"] = self.data["expiry"][-1] if "expiry" in cols else None
        self.current_trade["expiry_date"]["next_expiry"] = self.data["next_expiry"][-1] if "next_expiry" in cols else None
        self.current_trade["expiry_date"]["far_expiry"] = self.data["far_expiry"][-1] if "far_expiry" in cols else None

        # Expiry selection: near / next / far. Everything downstream reads
        # expiry_date["trade_expiry"], so the choice is made in exactly one place.
        sel = (self.expiry_selection or "next").lower()
        sel_key = {"near": "current_expiry", "next": "next_expiry", "far": "far_expiry"}.get(sel)
        if sel_key is None:
            raise ValueError(f"expiry_selection must be near/next/far, got {self.expiry_selection!r}")
        trade_expiry = self.current_trade["expiry_date"][sel_key]
        # "near" on expiry day would be a 0-DTE contract. Roll to the next
        # expiry instead (near_skip_0dte=True, default). The roll is recorded
        # on the trade so the report can show which entries were rolled.
        rolled_0dte = False
        if (sel == "near" and self.near_skip_0dte and trade_expiry is not None
                and not pd.isna(trade_expiry)
                and pd.Timestamp(trade_expiry).date() <= trigger_ts.date()):
            trade_expiry = self.current_trade["expiry_date"]["next_expiry"]
            rolled_0dte = True
        if trade_expiry is None or pd.isna(trade_expiry):   # None / NaN / NaT at the calendar's end
            self._log_skip(trigger_ts, f"no_{sel}_expiry",
                           f"expiry calendar has no '{sel}' expiry here" + (" (0-DTE roll)" if rolled_0dte else ""))
            self.current_trade = default_records()
            return
        self.current_trade["expiry_date"]["trade_expiry"] = trade_expiry
        self.current_trade["rolled_from_0dte"] = rolled_0dte

        # Bug 5 fix: warn loudly when the calendar hands us a far-dated "weekly".
        # Normal maximum DTE: near 6 (+1 holiday shift), next 13 (+1), far 20 (+1).
        dte = (pd.Timestamp(trade_expiry).date() - trigger_ts.date()).days
        _max_dte = {"near": 8, "next": 14, "far": 21}[sel]   # near can be 7 on expiry day after the 0-DTE roll
        if dte > _max_dte:
            self._log_skip(trigger_ts, "stale_expiry_calendar",
                           f"next expiry {trade_expiry} is {dte} days out — calendar gap; entry taken anyway, verify calendar")

        SHORT_KEY = self.generate_symbol(self.current_trade["option_type"], trade_expiry, short_strike)
        LONG_KEY = self.generate_symbol(self.current_trade["option_type"], trade_expiry, long_strike)
        SHORT_FILE = os.path.join(self.OPTIONS_PATH, SHORT_KEY + ".parquet")
        LONG_FILE = os.path.join(self.OPTIONS_PATH, LONG_KEY + ".parquet")

        synth = []   # legs priced from intrinsic because their file is missing/empty
        short_missing = not os.path.exists(SHORT_FILE)
        long_missing = not os.path.exists(LONG_FILE)
        if (short_missing or long_missing) and self.missing_option_pricing != "intrinsic":
            self._log_skip(trigger_ts, "option_file_missing", f"{SHORT_KEY} / {LONG_KEY}")
            self.current_trade = default_records()
            return

        try:
            def _load(path):
                D = pd.read_parquet(path)
                D.columns = ["Date", "Time", "Open", "High", "Low", "Close", "Volume", "IO"]
                D["timestamp"] = pd.to_datetime(D["Date"].astype(str) + " " + D["Time"].astype(str),
                                                format="%Y%m%d %H:%M")
                D.sort_values(by="timestamp", inplace=True)
                return D
            SHORT_DF = None if short_missing else _load(SHORT_FILE)
            LONG_DF = None if long_missing else _load(LONG_FILE)
            if self.missing_option_pricing == "intrinsic":
                if SHORT_DF is None or SHORT_DF.empty:
                    SHORT_DF = self._synthetic_option_df(self.current_trade["option_type"], short_strike); synth.append("short")
                if LONG_DF is None or LONG_DF.empty:
                    LONG_DF = self._synthetic_option_df(self.current_trade["option_type"], long_strike); synth.append("long")
                if SHORT_DF is None or LONG_DF is None:
                    self._log_skip(trigger_ts, "option_file_missing", f"{SHORT_KEY} / {LONG_KEY} (no spot series to synthesise)")
                    self.current_trade = default_records()
                    return
            if SHORT_DF.empty or LONG_DF.empty:
                self._log_skip(trigger_ts, "option_file_empty", f"{SHORT_KEY} / {LONG_KEY}")
                self.current_trade = default_records()
                return

            # ---- Bug 1 fix: price legs at/after the TRIGGER minute ----
            short_opt_entry_price, _ = self._fill_sell(SHORT_DF, trigger_ts)   # we SELL the short leg
            long_opt_entry_price, _ = self._fill_buy(LONG_DF, trigger_ts)      # we BUY the long leg
            if short_opt_entry_price is None or long_opt_entry_price is None:
                self._log_skip(trigger_ts, "no_option_marks_at_trigger", f"{SHORT_KEY} / {LONG_KEY}")
                self.current_trade = default_records()
                return
        except Exception as e:
            self._log_skip(trigger_ts, "option_parse_error", f"{SHORT_KEY}/{LONG_KEY}: {e}")
            self.current_trade = default_records()
            return

        initial_credit = short_opt_entry_price - long_opt_entry_price
        spread_width = long_off - short_off

        if self.spread_type == "debit":
            # ---- debit gate: net premium PAID must be positive and below width ----
            net_debit = -initial_credit                    # long - short
            if net_debit < self.min_entry_credit:
                self._log_skip(trigger_ts, "debit_too_small_or_inverted",
                               f"debit={net_debit:.2f} (long={long_opt_entry_price}, short={short_opt_entry_price})")
                self.current_trade = default_records()
                return
            if net_debit >= spread_width:
                self._log_skip(trigger_ts, "debit_exceeds_width",
                               f"debit={net_debit:.2f} >= width={spread_width} — no profit possible / bad marks")
                self.current_trade = default_records()
                return
        else:
            # ---- Bug 4 fix: reject impossible / worthless marks ----
            if initial_credit < self.min_entry_credit:
                self._log_skip(trigger_ts, "credit_too_small_or_inverted",
                               f"credit={initial_credit:.2f} (short={short_opt_entry_price}, long={long_opt_entry_price})")
                self.current_trade = default_records()
                return
            if initial_credit >= spread_width:
                self._log_skip(trigger_ts, "credit_exceeds_width",
                               f"credit={initial_credit:.2f} >= width={spread_width} — bad marks")
                self.current_trade = default_records()
                return

        # ---- Sizing: everything derived from capital, by the lot size in force
        # for THIS contract (keyed on expiry) and the margin the spread needs.
        eff_lot = lot_size_for(trade_expiry, self.lot_spec)
        if self.spread_type == "debit":
            # A debit spread blocks the premium paid, not SPAN on the width.
            mc = {"span": 0.0, "exposure": 0.0, "exposure_pct": 0.0, "total": (-initial_credit) * eff_lot}
        else:
            mc = margin_components(spread_width, eff_lot, close, self.margin, on_date=trigger_ts)
        margin_lot = mc["total"]
        allocated_capital = self.trading_capital * (self.capital_utilization_percent / 100.0)
        lots_to_trade = int(allocated_capital // margin_lot) if margin_lot > 0 else 0
        if lots_to_trade < 1:
            self._log_skip(trigger_ts, "insufficient_capital",
                           f"allocated={allocated_capital:.0f} < margin/lot={margin_lot:.0f} "
                           f"(SPAN {mc['span']:.0f} + exposure {mc['exposure']:.0f} @ spot {close:.0f}, lot {eff_lot})")
            self.current_trade = default_records()
            return

        # ---- max_loss_per_position_pct switch (0 = margin sizing only, unchanged) ----
        # Worst case for a credit spread held to expiry = (width - credit) per unit.
        if self.spread_type == "debit":
            structural_loss_lot = (-float(initial_credit)) * eff_lot          # max loss = debit paid
        else:
            structural_loss_lot = max(float(spread_width) - float(initial_credit), 0.0) * eff_lot
        sizing_mode = "margin"
        pct = float(self.max_loss_per_position_pct or 0)
        if pct > 0 and structural_loss_lot > 0:
            risk_budget = self.trading_capital * pct / 100.0
            risk_lots = int(risk_budget // structural_loss_lot)
            if risk_lots < 1:
                self._log_skip(trigger_ts, "risk_budget_too_small",
                               f"{pct}% of {self.trading_capital:.0f} = {risk_budget:.0f} < structural max loss/lot "
                               f"{structural_loss_lot:.0f} ((width {spread_width} - credit {initial_credit:.2f}) x lot {eff_lot})")
                self.current_trade = default_records()
                return
            if risk_lots < lots_to_trade:
                lots_to_trade = risk_lots
                sizing_mode = "max_loss_pct"

        self.trades_today += 1
        self.trade_counter += 1
        self.current_trade["position_id"] = self.trade_counter

        total_qty_trade = lots_to_trade * eff_lot
        capital_used_trade = lots_to_trade * margin_lot

        self.current_trade["structural_max_loss_inr"] = structural_loss_lot * lots_to_trade
        self.current_trade["sizing_mode"] = sizing_mode
        self.current_trade["synthetic_legs"] = "+".join(synth) if synth else ""
        if synth:
            self._log_skip(trigger_ts, "synthetic_leg_intrinsic",
                           f"pos {self.current_trade['position_id']} {self.current_trade['entry_type']}: "
                           f"{' & '.join(synth)} leg(s) priced from intrinsic (file missing: "
                           f"{SHORT_KEY if 'short' in synth else ''}{' / ' if len(synth) == 2 else ''}{LONG_KEY if 'long' in synth else ''}); "
                           f"entry short {short_opt_entry_price:.2f} long {long_opt_entry_price:.2f} credit {initial_credit:.2f}")

        self.current_trade["eff_lot_size"] = eff_lot
        self.current_trade["lots"] = lots_to_trade
        self.current_trade["margin_per_lot"] = margin_lot
        self.current_trade["span_per_lot"] = mc["span"]
        self.current_trade["exposure_per_lot"] = mc["exposure"]
        self.current_trade["premium_received"] = float(initial_credit) * total_qty_trade
        self.current_trade["trading_capital_at_entry"] = float(self.trading_capital)
        self.current_trade["total_qty"] = total_qty_trade
        self.current_trade["capital_used"] = capital_used_trade
        self.current_trade["remaining_qty"] = total_qty_trade

        self.current_trade["spot_price_at_entry"] = close
        self.current_trade["ohlc_entry"]["open"] = self.data["Open"][-1]
        self.current_trade["ohlc_entry"]["high"] = self.data["High"][-1]
        self.current_trade["ohlc_entry"]["low"] = self.data["Low"][-1]
        self.current_trade["ohlc_entry"]["close"] = self.data["Close"][-1]
        self.current_trade["symbol"] = self.symbol
        self.current_trade["entry_time"] = trigger_ts
        self.current_trade["short_option"]["entry_price"] = short_opt_entry_price
        self.current_trade["long_option"]["entry_price"] = long_opt_entry_price
        self.current_trade["long_option"]["options_data"] = LONG_DF
        self.current_trade["short_option"]["options_data"] = SHORT_DF
        self.current_trade["trade_number_today"] = self.trades_today
        self.current_trade["entry_price"] = initial_credit
        self.current_trade["credit_spread"]["entry"] = initial_credit

        spread_df = pd.merge(
            SHORT_DF[["timestamp", "Close"]],
            LONG_DF[["timestamp", "Close"]],
            on="timestamp", suffixes=("_short", "_long"))
        spread_df["credit_spread"] = spread_df["Close_short"] - spread_df["Close_long"]
        if initial_credit != 0:
            spread_df["credit_spread_percent"] = ((initial_credit - spread_df["credit_spread"]) / initial_credit) * 100
        else:
            spread_df["credit_spread_percent"] = 0.0
        # Targets can only trigger AFTER entry — never on pre-entry rows
        spread_df = spread_df[spread_df["timestamp"] > trigger_ts].reset_index(drop=True)
        self.current_trade["spread_data"] = spread_df


# =============================================================================
# REPORT ADD-ON — INTRINSIC VALUE AUDIT OF EVERY FILL
# =============================================================================
# Pure post-processing over the finished trade log. It reads the recorded
# fills and the 1-min spot bars and NEVER feeds anything back into the
# strategy, so the trades / P&L / statistics are byte-identical with or
# without it. It exists to answer one question per fill:
#
#     "Could this option really have traded at this price at this minute?"
#
# Two hard bounds hold in a real market, whatever the data file says:
#   * an option never trades below its intrinsic value
#       CE intrinsic = max(spot - strike, 0), PE intrinsic = max(strike - spot, 0)
#   * a vertical spread is never worth more than its width (or less than 0)
#
# For every leg fill (entry short/long, each exit event short/long, hedge
# buy/sell) the audit records the spot OHLC of the fill minute, the intrinsic
# value at the minute close, the LOWEST intrinsic anywhere in that minute
# (CE at the minute low / PE at the minute high — the most lenient bound, so a
# breach is unambiguous), the booked price (slippage included, i.e. exactly
# what hit the P&L), and a verdict:
#     OK                          booked >= lowest possible intrinsic
#     IMPOSSIBLE - in our favour  a BUY filled below intrinsic (we underpaid)
#     IMPOSSIBLE - against us     a SELL filled below intrinsic (we were underpaid)
# `pnl_distortion_inr` is how much the booked P&L is overstated (+) or
# understated (-) by that leg versus flooring it at intrinsic.
#
# The P&L tab then restates every exit event and hedge three ways:
#     actual_pnl_inr     as booked (before charges — same basis as profit_in_inr)
#     intrinsic_pnl_inr  every leg replaced by its intrinsic value at the minute
#                        close (what the position was "really" worth)
#     floored_pnl_inr    booked prices, but each leg floored at its lowest
#                        possible intrinsic and the spread clipped to [0, width]
#                        — the P&L the exit sanity gate would have produced
# Spot for a fill is the 1-min bar containing the recorded fill timestamp.
# Scheduled fills (expiry close, hedge legs) are priced "at or before" the
# schedule, so for those the bar is the scheduled minute itself.

def _spot_bar_index(ts, spot_ts):
    """Index of the 1-min spot bar containing `ts` (bar labelled by start).
    Falls back to the nearest bar on either side; None if no spot data."""
    if spot_ts is None or len(spot_ts) == 0:
        return None
    v = np.int64(pd.Timestamp(ts).value)
    i = int(np.searchsorted(spot_ts, v, side="right")) - 1
    if i < 0:
        i = 0
    return min(i, len(spot_ts) - 1)


def _intrinsic(option_type, strike, spot):
    if spot is None or strike is None or (isinstance(spot, float) and np.isnan(spot)):
        return np.nan
    if str(option_type).upper() == "CE":
        return max(float(spot) - float(strike), 0.0)
    return max(float(strike) - float(spot), 0.0)


def build_intrinsic_audit(signals, hedges_log, spot_ts, spot_open, spot_high, spot_low, spot_close):
    """Return (fills_df, pnl_df, summary_df). Safe on empty inputs."""
    fills = []

    def _bar(ts):
        i = _spot_bar_index(ts, spot_ts)
        if i is None:
            return None, np.nan, np.nan, np.nan, np.nan
        return (pd.Timestamp(spot_ts[i]), float(spot_open[i]), float(spot_high[i]),
                float(spot_low[i]), float(spot_close[i]))

    def _leg(position_id, event, leg, side, option_type, strike, ts, booked, qty, expiry=None):
        """Audit one fill; returns dict with intrinsic bounds and verdict."""
        bar_ts, o, h, l, c = _bar(ts)
        opt = str(option_type).upper()
        intr_close = _intrinsic(opt, strike, c)
        # lowest / highest intrinsic anywhere in the minute
        intr_min = _intrinsic(opt, strike, l if opt == "CE" else h)
        intr_max = _intrinsic(opt, strike, h if opt == "CE" else l)
        booked = np.nan if booked is None else float(booked)
        below_min = bool(pd.notna(booked) and pd.notna(intr_min) and booked < intr_min - 1e-9)
        below_close = bool(pd.notna(booked) and pd.notna(intr_close) and booked < intr_close - 1e-9)
        if below_min:
            verdict = "IMPOSSIBLE - in our favour" if side == "BUY" else "IMPOSSIBLE - against us"
            gap = intr_min - booked
            distortion = gap * qty if side == "BUY" else -gap * qty
        else:
            verdict, distortion = "OK", 0.0
        rec = {
            "position_id": position_id, "event": event, "leg": leg, "side": side,
            "option_type": opt, "strike": strike, "expiry": expiry,
            "fill_timestamp": pd.Timestamp(ts) if ts is not None else None,
            "spot_bar": bar_ts, "spot_open": o, "spot_high": h, "spot_low": l, "spot_close": c,
            "intrinsic_at_close": intr_close, "intrinsic_min_in_minute": intr_min,
            "intrinsic_max_in_minute": intr_max,
            "booked_price": booked,
            "extrinsic_vs_close": (booked - intr_close) if pd.notna(booked) and pd.notna(intr_close) else np.nan,
            "below_intrinsic_at_close": below_close,
            "below_intrinsic_min": below_min,
            "verdict": verdict, "qty": qty,
            "pnl_distortion_inr": distortion,
        }
        fills.append(rec)
        return rec

    pnl_rows = []
    seen_entry = set()
    for r in signals:
        pid = r.get("position_id")
        opt = r.get("option_type")
        ss, ls = r.get("short_strike"), r.get("long_strike")
        width = abs(float(ls) - float(ss)) if ss is not None and ls is not None else np.nan
        exp = r.get("trade_expiry")
        total_qty = r.get("total_qty") or 0
        qty = r.get("exit_qty") or 0
        # ---- entry legs: once per position (the log has one row per exit event)
        if pid not in seen_entry:
            seen_entry.add(pid)
            e_s = _leg(pid, "Entry", "Short", "SELL", opt, ss, r.get("entry_time"), r.get("short_entry_price"), total_qty, exp)
            e_l = _leg(pid, "Entry", "Long", "BUY", opt, ls, r.get("entry_time"), r.get("long_entry_price"), total_qty, exp)
        else:
            e_s = next(f for f in fills if f["position_id"] == pid and f["event"] == "Entry" and f["leg"] == "Short")
            e_l = next(f for f in fills if f["position_id"] == pid and f["event"] == "Entry" and f["leg"] == "Long")
        # ---- exit legs for this event
        ev = r.get("reason_for_exit")
        x_s = _leg(pid, ev, "Short", "BUY", opt, ss, r.get("exit_timestamp"), r.get("short_exit_price"), qty, exp)
        x_l = _leg(pid, ev, "Long", "SELL", opt, ls, r.get("exit_timestamp"), r.get("long_exit_price"), qty, exp)

        # ---- three P&L views for this exit event
        def _spread(s, l, lo=0.0, hi=None):
            if pd.isna(s) or pd.isna(l):
                return np.nan
            v = s - l
            if hi is not None and pd.notna(hi):
                v = min(max(v, lo), hi)
            return v
        actual_entry = _spread(e_s["booked_price"], e_l["booked_price"])
        actual_exit = _spread(x_s["booked_price"], x_l["booked_price"])
        intr_entry = _spread(e_s["intrinsic_at_close"], e_l["intrinsic_at_close"])
        intr_exit = _spread(x_s["intrinsic_at_close"], x_l["intrinsic_at_close"])
        def _floor(f):
            b, m = f["booked_price"], f["intrinsic_min_in_minute"]
            return b if pd.isna(m) or pd.isna(b) else max(b, m)
        if str(r.get("spread_type", "credit")) == "debit":
            # debit: short - long lives in [-width, 0]
            fl_entry = _spread(_floor(e_s), _floor(e_l), -width if pd.notna(width) else -np.inf, 0.0)
            fl_exit = _spread(_floor(x_s), _floor(x_l), -width if pd.notna(width) else -np.inf, 0.0)
        else:
            fl_entry = _spread(_floor(e_s), _floor(e_l), 0.0, width)
            fl_exit = _spread(_floor(x_s), _floor(x_l), 0.0, width)
        actual_pnl = (actual_entry - actual_exit) * qty if pd.notna(actual_entry) and pd.notna(actual_exit) else np.nan
        intr_pnl = (intr_entry - intr_exit) * qty if pd.notna(intr_entry) and pd.notna(intr_exit) else np.nan
        fl_pnl = (fl_entry - fl_exit) * qty if pd.notna(fl_entry) and pd.notna(fl_exit) else np.nan
        pnl_rows.append({
            "position_id": pid, "type": "Spread", "event": ev, "option_type": opt,
            "short_strike": ss, "long_strike": ls, "width": width,
            "entry_time": r.get("entry_time"), "exit_timestamp": r.get("exit_timestamp"), "qty": qty,
            "booked_entry_spread": actual_entry, "booked_exit_spread": actual_exit,
            "intrinsic_entry_spread": intr_entry, "intrinsic_exit_spread": intr_exit,
            "floored_entry_spread": fl_entry, "floored_exit_spread": fl_exit,
            "exit_spread_above_width": bool(pd.notna(actual_exit) and pd.notna(width) and abs(actual_exit) > width + 1e-9),
            "exit_spread_negative": bool(pd.notna(actual_exit) and (actual_exit < -1e-9 if str(r.get("spread_type", "credit")) != "debit" else actual_exit > 1e-9)),
            "any_leg_below_intrinsic": bool(e_s["below_intrinsic_min"] or e_l["below_intrinsic_min"]
                                            or x_s["below_intrinsic_min"] or x_l["below_intrinsic_min"]),
            "actual_pnl_inr": actual_pnl,
            "intrinsic_pnl_inr": intr_pnl,
            "floored_pnl_inr": fl_pnl,
            "actual_minus_intrinsic_inr": actual_pnl - intr_pnl if pd.notna(actual_pnl) and pd.notna(intr_pnl) else np.nan,
            "distortion_vs_floored_inr": actual_pnl - fl_pnl if pd.notna(actual_pnl) and pd.notna(fl_pnl) else np.nan,
        })

    # ---- hedges: extra long-leg contracts bought at close, sold next morning
    pos_meta = {}
    for r in signals:
        pos_meta.setdefault(r.get("position_id"), (r.get("option_type"), r.get("long_strike"), r.get("trade_expiry")))
    for h in hedges_log or []:
        pid = h.get("position_id")
        opt, lstrike, exp = pos_meta.get(pid, (None, None, None))
        if opt is None or lstrike is None:
            continue
        hq = h.get("qty") or 0
        if h.get("hedge_strike") is not None:
            lstrike = h.get("hedge_strike")   # hedge_distance > 0: hedge sits on its own strike
        hb = _leg(pid, "Hedge Entry", "Hedge", "BUY", opt, lstrike, h.get("hedge_entry_time"), h.get("entry_price"), hq, exp)
        hs = _leg(pid, "Hedge Exit", "Hedge", "SELL", opt, lstrike, h.get("hedge_exit_time"), h.get("exit_price"), hq, exp)
        def _fl(f):
            b, m = f["booked_price"], f["intrinsic_min_in_minute"]
            return b if pd.isna(m) or pd.isna(b) else max(b, m)
        a = (hs["booked_price"] - hb["booked_price"]) * hq
        i = ((hs["intrinsic_at_close"] - hb["intrinsic_at_close"]) * hq
             if pd.notna(hs["intrinsic_at_close"]) and pd.notna(hb["intrinsic_at_close"]) else np.nan)
        f = (_fl(hs) - _fl(hb)) * hq
        pnl_rows.append({
            "position_id": pid, "type": "Hedge", "event": "Hedge", "option_type": opt,
            "short_strike": None, "long_strike": lstrike, "width": np.nan,
            "entry_time": h.get("hedge_entry_time"), "exit_timestamp": h.get("hedge_exit_time"), "qty": hq,
            "booked_entry_spread": hb["booked_price"], "booked_exit_spread": hs["booked_price"],
            "intrinsic_entry_spread": hb["intrinsic_at_close"], "intrinsic_exit_spread": hs["intrinsic_at_close"],
            "floored_entry_spread": _fl(hb), "floored_exit_spread": _fl(hs),
            "exit_spread_above_width": False, "exit_spread_negative": False,
            "any_leg_below_intrinsic": bool(hb["below_intrinsic_min"] or hs["below_intrinsic_min"]),
            "actual_pnl_inr": a, "intrinsic_pnl_inr": i, "floored_pnl_inr": f,
            "actual_minus_intrinsic_inr": a - i if pd.notna(i) else np.nan,
            "distortion_vs_floored_inr": a - f,
        })

    fills_df = pd.DataFrame(fills)
    pnl_df = pd.DataFrame(pnl_rows)

    # ---- summary block
    rows = []
    def _add(k, v):
        rows.append({"Metric": k, "Value": v})
    if not fills_df.empty:
        _add("Fills audited (legs)", int(len(fills_df)))
        _add("Fills below intrinsic (min-in-minute)", int(fills_df["below_intrinsic_min"].sum()))
        _add("Fills below intrinsic (at close)", int(fills_df["below_intrinsic_at_close"].sum()))
        _add("  ... BUY fills below intrinsic (in our favour)", int(((fills_df["side"] == "BUY") & fills_df["below_intrinsic_min"]).sum()))
        _add("  ... SELL fills below intrinsic (against us)", int(((fills_df["side"] == "SELL") & fills_df["below_intrinsic_min"]).sum()))
        for ev in ("Entry", "Hedge Entry", "Hedge Exit"):
            sub = fills_df[fills_df["event"] == ev]
            if len(sub):
                _add(f"  ... {ev} fills below intrinsic", int(sub["below_intrinsic_min"].sum()))
        xsub = fills_df[~fills_df["event"].isin(["Entry", "Hedge Entry", "Hedge Exit"])]
        if len(xsub):
            _add("  ... Exit-event fills below intrinsic", int(xsub["below_intrinsic_min"].sum()))
        _add("Net P&L distortion from impossible fills (INR, + = booked P&L overstated)", float(fills_df["pnl_distortion_inr"].sum()))
        _add("  ... overstated (in our favour) INR", float(fills_df.loc[fills_df["pnl_distortion_inr"] > 0, "pnl_distortion_inr"].sum()))
        _add("  ... understated (against us) INR", float(fills_df.loc[fills_df["pnl_distortion_inr"] < 0, "pnl_distortion_inr"].sum()))
    if not pnl_df.empty:
        sp = pnl_df[pnl_df["type"] == "Spread"]
        hg = pnl_df[pnl_df["type"] == "Hedge"]
        _add("Spread exit events", int(len(sp)))
        _add("Spread exits with spread > width (impossible)", int(sp["exit_spread_above_width"].sum()))
        _add("Spread exits with spread < 0 (impossible)", int(sp["exit_spread_negative"].sum()))
        _add("Spread actual P&L before charges (INR)", float(sp["actual_pnl_inr"].sum()))
        _add("Spread intrinsic P&L (INR)", float(sp["intrinsic_pnl_inr"].sum()))
        _add("Spread floored P&L = sanity-gated (INR)", float(sp["floored_pnl_inr"].sum()))
        _add("Spread actual - floored (INR, + = booked overstated)", float(sp["distortion_vs_floored_inr"].sum()))
        if len(hg):
            _add("Hedge actual P&L before charges (INR)", float(hg["actual_pnl_inr"].sum()))
            _add("Hedge intrinsic P&L (INR)", float(hg["intrinsic_pnl_inr"].sum()))
            _add("Hedge floored P&L (INR)", float(hg["floored_pnl_inr"].sum()))
        # time-of-day split of the exit events (where the fills are least reliable)
        xt = pd.to_datetime(sp["exit_timestamp"], errors="coerce").dt.time
        early = xt.apply(lambda t: t is not None and pd.notna(t) and dtime(9, 15) <= t <= dtime(9, 17))
        _add("Exit events at 09:15-09:17", int(early.sum()))
        _add("  ... their actual P&L (INR)", float(sp.loc[early, "actual_pnl_inr"].sum()))
        _add("  ... their floored P&L (INR)", float(sp.loc[early, "floored_pnl_inr"].sum()))
        _add("  ... their fills below intrinsic", int(fills_df[(~fills_df["event"].isin(["Entry", "Hedge Entry", "Hedge Exit"]))
                                                             & fills_df["fill_timestamp"].apply(lambda t: pd.notna(t) and dtime(9, 15) <= pd.Timestamp(t).time() <= dtime(9, 17))]["below_intrinsic_min"].sum()) if not fills_df.empty else 0)
        _add("Exit events after 09:17", int((~early).sum()))
        _add("  ... their actual P&L (INR)", float(sp.loc[~early, "actual_pnl_inr"].sum()))
        _add("  ... their floored P&L (INR)", float(sp.loc[~early, "floored_pnl_inr"].sum()))
    _add("Note", "Booked prices include slippage. Intrinsic uses the 1-min spot bar containing the fill timestamp. "
                 "Verdicts use the LOWEST intrinsic in that minute, so every flag is unambiguous.")
    summary_df = pd.DataFrame(rows)
    return fills_df, pnl_df, summary_df


def signal_generator(data: pd.DataFrame) -> pd.DataFrame:
    """Generate SuperTrend flip signals."""
    # BUG FIX: the old shift(1) != 1 test flagged row 0 (NaN shift) as a
    # signal. A flip requires a real opposite direction on the previous bar.
    prev = data["SUPERTd"].shift(1)
    sig = pd.Series(np.nan, index=data.index, dtype="object")
    sig[(data["SUPERTd"] == 1) & (prev == -1)] = "long"
    sig[(data["SUPERTd"] == -1) & (prev == 1)] = "short"
    data["signal"] = sig
    return data


# ------------------------------------------------------------------------------------------------
# Performance Metrics
# ------------------------------------------------------------------------------------------------
def recovery_days(cum_pnl):
    """Calculate number of days to recover from max drawdown."""
    peak = cum_pnl.cummax()
    drawdown = cum_pnl - peak

    if drawdown.min() >= 0:
        return 0

    max_dd_idx = drawdown.idxmin()
    peak_before_dd = peak[:max_dd_idx].iloc[-1]
    dd_start_idx = peak[:max_dd_idx][peak[:max_dd_idx] == peak_before_dd].index[-1]

    recovery_point = cum_pnl[max_dd_idx:]
    recovered = recovery_point[recovery_point >= peak_before_dd]

    if recovered.empty:
        return (cum_pnl.index[-1] - dd_start_idx).days

    recovery_idx = recovered.index[0]
    recovery_duration = (recovery_idx - dd_start_idx).days
    return recovery_duration


def calculate_metrics(trades_df, capital=10_000_000, extra_metrics=None):
    """
    Calculate comprehensive performance metrics from trades DataFrame.

    Args:
        trades_df:     Aggregated trade log (one row per position).
        capital:       Account capital used for CAGR / ROI.
        extra_metrics: Optional {metric_name: numeric_value} appended to the
                       statistics rows. Used to publish drawdown-episode
                       numbers, cost totals and other optimizer inputs without
                       changing any of the metrics computed below.
    """
    if trades_df.empty:
        print("\nNo trades executed during this period.")
        return

    total_qty = trades_df.iloc[0].get("total_qty", 1)  # For PNL calculation
    
    trades_df['entry_time'] = pd.to_datetime(trades_df['entry_time'])
    trades_df['exit_timestamp'] = pd.to_datetime(trades_df['exit_timestamp'])
    trades_df['month'] = trades_df['exit_timestamp'].dt.to_period('M')
    trades_df['year'] = trades_df['exit_timestamp'].dt.year

    # Basic metrics
    total_trades = len(trades_df)
    winning_trades = len(trades_df[trades_df['profit_points'] > 0])
    losing_trades = len(trades_df[trades_df['profit_points'] <= 0])
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    net_profit = trades_df['profit_points'].sum()
    avg_trade = trades_df['profit_points'].mean()
    highest_profit = trades_df['profit_points'].max()
    highest_loss = trades_df['profit_points'].min()

    # Profit Factor
    gross_profit = trades_df[trades_df['profit_points'] > 0]['profit_points'].sum()
    gross_loss = abs(trades_df[trades_df['profit_points'] <= 0]['profit_points'].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

    # Monthly & Yearly PNL
    monthly_pnl = trades_df.groupby('month')['profit_points'].sum()
    yearly_pnl = trades_df.groupby('year')['profit_points'].sum()
    monthly_trades_count = trades_df.groupby('month').size()

    # Drawdown
    cum_pnl = trades_df['profit_points'].cumsum()
    peak = cum_pnl.cummax()
    drawdown = cum_pnl - peak
    max_drawdown = drawdown.min()

    # Annualized drawdown per year
    drawdown_data = []
    for year, group in trades_df.groupby('year'):
        year_cum = group['profit_points'].cumsum()
        year_peak = year_cum.cummax()
        year_dd = (year_cum - year_peak).min()
        drawdown_data.append((year, year_dd))

    # Recovery days
    cum_pnl_dated = trades_df['profit_points'].cumsum()
    cum_pnl_dated.index = trades_df['exit_timestamp']
    recovery = recovery_days(cum_pnl_dated)

    # Sharpe Ratio (annualized, assuming ~252 trading days)
    daily_pnl = trades_df.groupby(trades_df['exit_timestamp'].dt.date)['profit_points'].sum()
    # BUG FIX: days with no exit are 0-P&L days, not missing days. Excluding
    # them inflated Sharpe (~2.7 vs ~2.0 on the frictionless run).
    if len(daily_pnl) > 1:
        _bdays = pd.bdate_range(min(daily_pnl.index), max(daily_pnl.index))
        daily_pnl = daily_pnl.reindex(_bdays.date, fill_value=0.0)
    if daily_pnl.std() > 0:
        sharpe = (daily_pnl.mean() / daily_pnl.std()) * np.sqrt(252)
    else:
        sharpe = 0

    # CAGR
    first_date = trades_df['entry_time'].min()
    last_date = trades_df['exit_timestamp'].max()
    years = (last_date - first_date).days / 365.25
    if years > 0 and capital + net_profit > 0:
        cagr = ((capital + net_profit) / capital) ** (1 / years) - 1
    else:
        cagr = 0

    # Consecutive wins/losses
    results = (trades_df['profit_points'] > 0).astype(int)
    max_consec_wins = 0
    max_consec_losses = 0
    current_streak = 0
    current_type = None
    for r in results:
        if r == current_type:
            current_streak += 1
        else:
            current_streak = 1
            current_type = r
        if r == 1:
            max_consec_wins = max(max_consec_wins, current_streak)
        else:
            max_consec_losses = max(max_consec_losses, current_streak)

    # ROI per year
    roi_data = (yearly_pnl / capital) * 100

    # --- Build Summary Rows ---
    summary_rows = []

    for period, pnl in monthly_pnl.items():
        summary_rows.append({
            'entry_time': None, 'exit_timestamp': None,
            'profit_points': pnl, 'entry_type': f'Monthly PnL ({period})'
        })

    for year, pnl in yearly_pnl.items():
        summary_rows.append({
            'entry_time': None, 'exit_timestamp': None,
            'profit_points': pnl, 'entry_type': f'Total Year PnL ({year})'
        })

    for year, dd in drawdown_data:
        summary_rows.append({
            'entry_time': None, 'exit_timestamp': None,
            'profit_points': dd, 'entry_type': f'Max Drawdown ({year})'
        })

    for year, roi in roi_data.items():
        summary_rows.append({
            'entry_time': None, 'exit_timestamp': None,
            'profit_points': roi, 'entry_type': f'ROI % ({year})'
        })

    for period, count in monthly_trades_count.items():
        summary_rows.append({
            'entry_time': None, 'exit_timestamp': None,
            'profit_points': count, 'entry_type': f'Trades in ({period})'
        })

    # Overall metrics
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': total_trades, 'entry_type': 'Total Trades'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': win_rate, 'entry_type': 'Win Rate %'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': net_profit, 'entry_type': 'Net Profit (points)'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': cagr * 100, 'entry_type': 'CAGR %'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': profit_factor, 'entry_type': 'Profit Factor'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': max_drawdown, 'entry_type': 'Overall Max Drawdown'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': sharpe, 'entry_type': 'Sharpe Ratio'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': avg_trade, 'entry_type': 'Average Trade (points)'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': highest_profit, 'entry_type': 'Highest Single Trade Profit'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': highest_loss, 'entry_type': 'Highest Single Trade Loss'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': max_consec_wins, 'entry_type': 'Max Consecutive Wins'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': max_consec_losses, 'entry_type': 'Max Consecutive Losses'})
    summary_rows.append({'entry_time': None, 'exit_timestamp': None, 'profit_points': recovery, 'entry_type': 'Recovery Days from MaxDD'})

    # -------------------------------------------------------------------------
    # OPTIMIZER-COMPATIBLE METRICS (purely additive — nothing above is changed)
    # -------------------------------------------------------------------------
    # worker.parse_technical_stats() normalises the "Technical Statistics" tab
    # into the canonical keys the optimizer ranks on ("Net Profit", "Profit
    # Factor", "Sharpe Ratio", "Overall Max Drawdown", ...). The rows above use
    # credit-spread wording ("Net Profit (points)"), which does not normalise to
    # anything the ranking functions read, so publish the canonical names too.
    # Values are identical to their credit-spread-worded twins.
    def _stat(metric, value):
        summary_rows.append({
            'entry_time': None, 'exit_timestamp': None,
            'profit_points': value, 'entry_type': metric,
        })

    _stat('Net Profit', net_profit)
    _stat('Average Trade', avg_trade)
    _stat('Gross Profit', gross_profit)
    _stat('Gross Loss', gross_loss)
    _stat('Winning Trades', winning_trades)
    _stat('Losing Trades', losing_trades)
    _stat('Return on Capital %', (net_profit / capital * 100.0) if capital else 0.0)

    # Caller-supplied metrics (drawdown episode summary, cost totals, ...).
    if extra_metrics:
        max_dd_pct = extra_metrics.get('Max Drawdown %')
        if max_dd_pct:
            _stat('Calmar Ratio', (cagr * 100.0) / abs(float(max_dd_pct)))
        for metric_name, metric_value in extra_metrics.items():
            _stat(metric_name, metric_value)

    summary_df = pd.DataFrame(summary_rows)
    
    # Clean up trades_df before concat
    trades_out = trades_df.drop(columns=["year", "month"], errors="ignore")
    final_df = pd.concat([trades_out, summary_df], ignore_index=True)

    return final_df


def _coerce_time(value, fallback):
    """
    Accept a datetime.time, an 'HH:MM' / 'HH:MM:SS' string, or None.

    The optimizer serialises times to 'HH:MM:SS' strings in params.json, so a
    strategy driven by the optimizer may receive either form.
    """
    if value is None:
        return fallback
    if isinstance(value, dtime):
        return value
    if isinstance(value, str):
        text = value.strip()
        fmt = "%H:%M:%S" if text.count(":") == 2 else "%H:%M"
        return datetime.strptime(text, fmt).time()
    if hasattr(value, "time"):
        return value.time()
    return fallback


def _resolve_backtest_period(Backtest_period, start_date, end_date):
    """
    Merge the nested and flat forms of the backtest window into one dict.

    The walk-forward engine overrides dates in whichever style
    script_analyzer detected:
        nested -> Backtest_period={"start_date": ..., "end_date": ...}
        flat   -> start_date=..., end_date=...
    Flat arguments win when both are supplied, because that is how a
    flat-style walk-forward step hands its window to the strategy.
    """
    period = dict(Backtest_period or {})
    if start_date is not None:
        period["start_date"] = start_date
    if end_date is not None:
        period["end_date"] = end_date
    return period


def _write_empty_report(output_filename, capital):
    """
    Emit a minimal, well-formed report when a run produced no trades.

    Without this the optimizer sees a batch with no .xlsx at all and has to
    guess; with it every batch publishes the same statistics schema, so
    optimizer result tables stay rectangular and rank correctly (a zero-trade
    run simply scores at the bottom).
    """
    zero_stats = pd.DataFrame([
        {"Metric": "Total Trades", "Value": 0},
        {"Metric": "Win Rate %", "Value": 0.0},
        {"Metric": "Net Profit", "Value": 0.0},
        {"Metric": "Net Profit (points)", "Value": 0.0},
        {"Metric": "Net PnL After Costs", "Value": 0.0},
        {"Metric": "Brokerage Ratio %", "Value": 0.0},
        {"Metric": "CAGR %", "Value": 0.0},
        {"Metric": "Profit Factor", "Value": 0.0},
        {"Metric": "Overall Max Drawdown", "Value": 0.0},
        {"Metric": "Sharpe Ratio", "Value": 0.0},
        {"Metric": "Average Trade", "Value": 0.0},
        {"Metric": "Max Drawdown $", "Value": 0.0},
        {"Metric": "Max Drawdown %", "Value": 0.0},
        {"Metric": "Total Drawdown Count", "Value": 0},
        {"Metric": "Trading Capital", "Value": capital},
    ])
    try:
        with pd.ExcelWriter(output_filename, engine="openpyxl") as writer:
            skipped_df = pd.DataFrame(CREDITSPREAD.skipped_log)
            if skipped_df.empty:
                skipped_df = pd.DataFrame(columns=["timestamp", "reason", "detail"])
            skipped_df.to_excel(writer, sheet_name="Skipped Entries", index=False)
            zero_stats.to_excel(writer, sheet_name="Technical Statistics", index=False)
            write_drawdown_sheets(writer, pd.DataFrame(), capital)
        print(f"No-trade report written to {output_filename}")
    except Exception as exc:
        print(f"Warning: could not write empty report: {exc}")


def main(symbol="NIFTY", step_size=None, Options_dir_Path="", Spot_data_path="",
         Backtest_period=None,
         Timeframe="15min", ATR_len=16, ATR_mult=1.72, buffer_point=10, spread_distance=150,
         short_distance=0, long_distance=0, expiry_selection="next", near_skip_0dte=True,
         lot_size=None, hedges_qty_percent=23, hedges_allowed=True,
         hedge_distance=0,                 # 0 = hedge with the long leg (unchanged); >0 = strike at ref -/+ pts
         max_loss_per_position_pct=0,      # 0 = margin sizing (unchanged); >0 = cap lots so worst case <= pct% of capital
         spread_type="credit",             # "credit" (unchanged) | "debit"
         position_mode="positional",       # "positional" (unchanged) | "intraday" = force-close same day
         intraday_exit_time=None,          # intraday mode close time; None -> exit_time
         use_targets=True,                 # True (unchanged) | False = book everything only on flip / SL / time exit
         max_fill_gap_minutes=FILL_MAX_GAP_MINUTES,  # at/after fills must print within N min same day; 0 = unbounded (legacy)
         flip_exit_mode="breakout",        # "breakout" (unchanged: close+reverse at flip-candle breakout) | "immediate" (close 1 min after flip, re-enter on breakout)
         exit_sanity_gate="off",           # "off" (unchanged) | "intrinsic" = floor exit legs at intrinsic, clip spread to structural range
         missing_option_pricing="skip",    # "skip" (unchanged) | "intrinsic" = price a missing leg from spot intrinsic (diagnostic; tagged + logged)
         session_halt_mode="off",          # "off" (unchanged) | "halt" = freeze evaluation | "close" = square off at start, then freeze
         session_halt_start=None,          # e.g. dtime(15, 15); None -> 15:15
         session_halt_end=None,            # e.g. dtime(9, 20);  None -> 09:20 (next day when start > end)
         targets_credit_spread=None,
         trading_capital=10_000_000, capital_utilization_percent=50,
         margin=None, compound_capital=False, show_pnl_in_rupees=True,
         supertrend_sl_buffer=10, costs=None,
         min_entry_credit=2.0,
         # ------------------------------------------------------------------
         # Optimizer / walk-forward additions. All default to None or to the
         # value the strategy already used, so an existing CONFIG produces
         # byte-identical results.
         # ------------------------------------------------------------------
         option_symbol_format="ddMMMyy",  # Option CSV naming convention (see below)
         start_date=None,              # Flat-style window override (WFO)
         end_date=None,                # Flat-style window override (WFO)
         inclusive_end_date=False,     # True -> a bare end date includes that whole session
         target_1_percent=None,        # Flat override for targets_credit_spread["t1"]
         target_2_percent=None,        # Flat override for targets_credit_spread["t2"]
         target_3_percent=None,        # Flat override for targets_credit_spread["t3"]
         targets_qty_credit_spread=None,   # {"t1": 40, "t2": 40, "t3": 20} qty % booked per target (None = 40/40/20 as before)
         target_1_qty_percent=None,    # Flat override for targets_qty_credit_spread["t1"]
         target_2_qty_percent=None,    # Flat override for targets_qty_credit_spread["t2"]
         target_3_qty_percent=None,    # Flat override for targets_qty_credit_spread["t3"] (informational: t3 = remainder)
         no_entry_after=None,          # Latest time a new position may be opened
         exit_time=None,               # Forced exit time on expiry day
         end_of_day_candle=None,       # Candle that triggers the overnight hedge
         hedge_entry_time=None,        # Time the overnight hedge is bought
         hedge_exit_time=None,         # Time the overnight hedge is sold next morning
         apply_charges=True,           # False -> run with zero statutory/brokerage charges
         slippage_per_leg=None,        # Flat override for costs["slippage_per_leg"]
         brokerage_per_order=None,     # Flat override for costs["brokerage_per_order"]
         output_path=None,             # Explicit Excel report path
         save_debug_data=True,         # Write data_debug.csv alongside the report
         **kwargs):

    if kwargs:
        # The optimizer may pass through parameters this strategy does not use
        # (e.g. metric columns carried along by a walk-forward selection).
        # Absorb them rather than raising, but say so.
        print(f"Ignoring unsupported parameters: {sorted(kwargs)}")

    if step_size is None:
        step_size = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25}

    # Validate up front so a bad format fails immediately with a clear message,
    # instead of silently missing every option file mid-backtest.
    option_symbol_format = resolve_symbol_format(option_symbol_format)

    # Flat t1/t2/t3 overrides let the optimizer sweep the OMS scaling targets,
    # which are otherwise locked inside a nested dict it cannot generate.
    targets = dict(targets_credit_spread or {"t1": 31, "t2": 69, "t3": 98})
    for target_key, override in (("t1", target_1_percent),
                                 ("t2", target_2_percent),
                                 ("t3", target_3_percent)):
        if override is not None:
            targets[target_key] = override

    # Qty split per target, same nested + flat pattern. t3 is always the
    # remainder, so t1 + t2 must be <= 100; t3 is checked for consistency only.
    qty_targets = dict(targets_qty_credit_spread or {"t1": 40, "t2": 40, "t3": 20})
    for target_key, override in (("t1", target_1_qty_percent),
                                 ("t2", target_2_qty_percent),
                                 ("t3", target_3_qty_percent)):
        if override is not None:
            qty_targets[target_key] = override
    q1, q2 = float(qty_targets.get("t1", 40)), float(qty_targets.get("t2", 40))
    if q1 < 0 or q2 < 0 or q1 + q2 > 100:
        raise ValueError(f"targets_qty_credit_spread: t1 + t2 must be within 0..100 (got t1={q1}, t2={q2})")
    q3_expected = 100.0 - q1 - q2
    q3_given = qty_targets.get("t3")
    if q3_given is not None and abs(float(q3_given) - q3_expected) > 1e-9:
        print(f"NOTE: target_3_qty_percent={q3_given} ignored — t3 is always the remainder ({q3_expected:.0f}%).")
    qty_targets["t3"] = q3_expected
    print(f"OMS qty split: T1 {q1:.0f}% / T2 {q2:.0f}% / T3 {q3_expected:.0f}% of position qty "
          f"(rounded down to whole lots, min 1 lot; adjustments are logged)")

    # --- Configure Strategy Class Parameters ---
    CREDITSPREAD.symbol = symbol
    CREDITSPREAD.step_size = step_size
    CREDITSPREAD.OPTIONS_PATH = Options_dir_Path
    CREDITSPREAD.option_symbol_format = option_symbol_format
    CREDITSPREAD.buffer_point = buffer_point
    CREDITSPREAD.supertrend_sl_buffer = supertrend_sl_buffer
    CREDITSPREAD.spread_distance = spread_distance
    # ---- Leg offsets: resolved ONCE here, before any data is loaded.
    # Whatever the optimizer sampled is repaired into a valid credit-spread pair
    # (see resolve_leg_distances). Every adjustment is printed and written to the
    # report/return dict, so a run is never silently different from its params.
    _step = CREDITSPREAD.step_size.get(symbol, 50)
    _req_short, _req_long = int(short_distance or 0), int(long_distance or 0)
    short_distance, long_distance, _leg_notes = resolve_leg_distances(
        short_distance, long_distance, spread_distance, _step)
    for _n in _leg_notes:
        print(f"LEGS: {_n}")
    if short_distance is None:
        print("!! SKIPPED RUN — cannot resolve legs: " + "; ".join(_leg_notes))
        return {
            "status": "skipped", "skip_reason": "unresolvable_legs: " + "; ".join(_leg_notes),
            "symbol": symbol,
            "requested_short_distance": _req_short, "requested_long_distance": _req_long,
            "effective_short_distance": None, "effective_long_distance": None,
            "total_trades": 0, "total_exit_events": 0, "net_pnl": 0.0,
            "gross_pnl_before_costs": 0.0, "total_charges": 0.0, "charges_enabled": True,
            "charges_breakdown": {}, "hedge_charges": 0.0, "pnl_unit": "INR",
            "max_drawdown": 0.0, "max_drawdown_pct": 0.0, "drawdown_episodes": 0,
            "skipped_entries": 0, "report_path": None,
        }
    CREDITSPREAD.short_distance = short_distance
    CREDITSPREAD.long_distance = long_distance
    CREDITSPREAD.legs_resolved = True
    CREDITSPREAD.leg_notes = list(_leg_notes)
    CREDITSPREAD.requested_legs = (_req_short, _req_long)
    print(f"Legs (effective): short at ±{short_distance}, long at ±{long_distance}, width {long_distance - short_distance}"
          + ("  [ADJUSTED from requested — see LEGS lines above]" if any("WARNING" in n for n in _leg_notes) else ""))
    expiry_selection = (expiry_selection or "next").strip().lower()
    if expiry_selection not in ("near", "next", "far"):
        raise ValueError(f"expiry_selection must be 'near', 'next' or 'far' (got {expiry_selection!r})")
    CREDITSPREAD.expiry_selection = expiry_selection
    if isinstance(near_skip_0dte, str):
        near_skip_0dte = near_skip_0dte.strip().lower() in ("1", "true", "yes", "y", "on")
    CREDITSPREAD.near_skip_0dte = bool(near_skip_0dte)
    if expiry_selection == "near":
        print("near_skip_0dte=%s -> on expiry day entries %s" % (
            CREDITSPREAD.near_skip_0dte,
            "roll to NEXT expiry (no 0-DTE trades)" if CREDITSPREAD.near_skip_0dte else "trade the 0-DTE contract"))
    print(f"Expiry selection: {expiry_selection} "
          f"({ {'near': 'current expiry', 'next': 'expiry after current', 'far': 'two expiries out'}[expiry_selection] })")
    CREDITSPREAD.lot_size = lot_size
    CREDITSPREAD.lot_spec = _parse_lot_spec(lot_size, symbol)
    print("Lot sizes (by contract expiry): " +
          ", ".join(f"{a}..{b} = {sz}" for a, b, sz in CREDITSPREAD.lot_spec))
    CREDITSPREAD.hedges_qty_percent = hedges_qty_percent
    CREDITSPREAD.hedge_distance = int(hedge_distance or 0)
    if CREDITSPREAD.hedge_distance > 0:
        print(f"Overnight hedge strike: reference -/+ {CREDITSPREAD.hedge_distance} pts (hedge_distance); "
              f"falls back to the long leg when that file is missing")
    else:
        print("Overnight hedge strike: the position's long leg (hedge_distance=0)")
    # ---- three mode switches ----
    _st = str(spread_type or "credit").strip().lower()
    if _st not in ("credit", "debit"):
        raise ValueError(f"spread_type must be 'credit' or 'debit', got {spread_type!r}")
    CREDITSPREAD.spread_type = _st
    _pm = str(position_mode or "positional").strip().lower()
    if _pm not in ("positional", "intraday"):
        raise ValueError(f"position_mode must be 'positional' or 'intraday', got {position_mode!r}")
    CREDITSPREAD.position_mode = _pm
    if isinstance(use_targets, str):
        use_targets = use_targets.strip().lower() in ("1", "true", "yes", "y", "on")
    CREDITSPREAD.use_targets = bool(use_targets)
    print(f"Spread type: {_st.upper()} | Position mode: {_pm} | Targets: {'ON' if CREDITSPREAD.use_targets else 'OFF (book all on flip / SL / time exit)'}")
    if _st == "debit":
        print("NOTE: debit spread — BUY near leg (short_distance), SELL far leg (long_distance); "
              "net premium stored as NEGATIVE credit; targets = % of max profit; sizing = debit x lot; "
              "overnight hedge disabled. Set max_loss_per_position_pct or the margin-based sizing will "
              "buy a very large number of lots.")
    if _pm == "intraday":
        print("NOTE: intraday mode — every position is force-closed at "
              f"{(intraday_exit_time or exit_time or dtime(15, 20))} the same day; no overnight hedge.")
    _fm = str(flip_exit_mode or "breakout").strip().lower()
    if _fm not in ("breakout", "immediate"):
        raise ValueError(f"flip_exit_mode must be 'breakout' or 'immediate', got {flip_exit_mode!r}")
    CREDITSPREAD.flip_exit_mode = _fm
    _eg = str(exit_sanity_gate or "off").strip().lower()
    if _eg not in ("off", "intrinsic"):
        raise ValueError(f"exit_sanity_gate must be 'off' or 'intrinsic', got {exit_sanity_gate!r}")
    CREDITSPREAD.exit_sanity_gate = _eg
    _mp = str(missing_option_pricing or "skip").strip().lower()
    if _mp not in ("skip", "intrinsic"):
        raise ValueError(f"missing_option_pricing must be 'skip' or 'intrinsic', got {missing_option_pricing!r}")
    CREDITSPREAD.missing_option_pricing = _mp
    if _mp == "intrinsic":
        print("Missing option files: legs priced from spot intrinsic (max(intrinsic, 0.05), no time value). "
              "Positions tagged `synthetic_legs`; see Skipped Entries reason `synthetic_leg_intrinsic`. DIAGNOSTIC ONLY.")
    CREDITSPREAD.exit_gate_adjustments = 0
    CREDITSPREAD.exit_gate_inr = 0.0
    print("Exit sanity gate: " + ("OFF — exits booked at raw option prints" if _eg == "off"
                                  else "INTRINSIC — exit legs floored at intrinsic, spread clipped to structural range (adjustments logged)"))
    print("Flip exit: " + ("close + reverse at flip-candle breakout (same minute)" if _fm == "breakout"
                           else "close 1 min after the SuperTrend flip; opposite entry only on its breakout"))
    CREDITSPREAD.max_fill_gap_minutes = int(max_fill_gap_minutes or 0)
    print(f"Fill bound: at/after fills must print within {CREDITSPREAD.max_fill_gap_minutes} min on the same day"
          if CREDITSPREAD.max_fill_gap_minutes > 0 else
          "Fill bound: OFF (max_fill_gap_minutes=0) — unbounded next-print fills (lookahead risk on illiquid legs)")
    CREDITSPREAD.max_loss_per_position_pct = float(max_loss_per_position_pct or 0)
    if CREDITSPREAD.max_loss_per_position_pct > 0:
        print(f"Sizing cap: worst-case loss per position <= {CREDITSPREAD.max_loss_per_position_pct}% of trading capital "
              f"(lots = min(margin lots, floor(capital x pct / ((width - credit) x lot))))")
    else:
        print("Sizing cap: none (max_loss_per_position_pct=0) — margin-based sizing only")
    # Overnight-hedge master switch. Accepts bool or the strings the optimizer
    # serialises ("true"/"false"/"1"/"0"). Off = the hedge opener never runs;
    # the spread logic is identical either way.
    if isinstance(hedges_allowed, str):
        hedges_allowed = hedges_allowed.strip().lower() in ("1", "true", "yes", "y", "on")
    CREDITSPREAD.hedges_allowed = bool(hedges_allowed)
    if not CREDITSPREAD.hedges_allowed:
        print("NOTE: hedges_allowed=False — overnight hedges are DISABLED.")
    CREDITSPREAD.targets_credit_spread = targets
    CREDITSPREAD.targets_qty_credit_spread = qty_targets
    CREDITSPREAD.partial_qty_adjustments = 0

    CREDITSPREAD.costs = dict(DEFAULT_COSTS)
    if costs:
        CREDITSPREAD.costs.update(costs)
    # Flat cost overrides — the optimizer cannot sweep values nested in the
    # `costs` dict, so expose the two that matter most as top-level scalars.
    if slippage_per_leg is not None:
        CREDITSPREAD.costs["slippage_per_leg"] = float(slippage_per_leg)
    if brokerage_per_order is not None:
        CREDITSPREAD.costs["brokerage_per_order"] = float(brokerage_per_order)
    # Master charges switch. Off means brokerage/STT/exchange/GST/stamp/SEBI are
    # all zero; slippage is a fill-price effect and stays under slippage_per_leg,
    # so set that to 0 as well for a fully frictionless run.
    CREDITSPREAD.costs["apply_charges"] = bool(apply_charges)
    if not CREDITSPREAD.costs["apply_charges"]:
        print("NOTE: apply_charges=False — statutory and brokerage charges are "
              f"DISABLED. Slippage is separate and still Rs "
              f"{CREDITSPREAD.costs['slippage_per_leg']}/leg.")
    CREDITSPREAD.min_entry_credit = min_entry_credit
    CREDITSPREAD.trading_capital = float(trading_capital)
    CREDITSPREAD.initial_capital = float(trading_capital)
    CREDITSPREAD.capital_utilization_percent = float(capital_utilization_percent)
    CREDITSPREAD.margin = dict(DEFAULT_MARGIN)
    if margin:
        CREDITSPREAD.margin.update(margin)
    if isinstance(compound_capital, str):
        compound_capital = compound_capital.strip().lower() in ("1", "true", "yes", "y", "on")
    CREDITSPREAD.compound_capital = bool(compound_capital)
    _w0 = CREDITSPREAD.long_distance - CREDITSPREAD.short_distance
    _lot_now = CREDITSPREAD.lot_spec[-1][2]
    _mc = margin_components(_w0, _lot_now, 24000, CREDITSPREAD.margin)
    print(f"Sizing: capital {trading_capital:,.0f} x {capital_utilization_percent}% = "
          f"{trading_capital * capital_utilization_percent / 100:,.0f} usable. Margin model: "
          f"SPAN = width x lot x {CREDITSPREAD.margin['span_factor']}, exposure = "
          f"{_rate_on(CREDITSPREAD.margin['exposure_pct'], None) * 100:.2f}% x spot x lot. "
          f"Example @ spot 24,000, lot {_lot_now}, width {_w0}: {_mc['span']:,.0f} + {_mc['exposure']:,.0f} = "
          f"{_mc['total']:,.0f}/lot -> {int(trading_capital * capital_utilization_percent / 100 // _mc['total'])} lots"
          f"{' (compounding ON)' if CREDITSPREAD.compound_capital else ''}")

    # Session-time overrides — unchanged class defaults when not supplied.
    CREDITSPREAD.no_entry_after = _coerce_time(no_entry_after, dtime(15, 15))
    CREDITSPREAD.end_of_day_candle = _coerce_time(end_of_day_candle, dtime(15, 15))
    CREDITSPREAD.exit_time = _coerce_time(exit_time, dtime(15, 20))
    CREDITSPREAD.intraday_exit_time = _coerce_time(intraday_exit_time, CREDITSPREAD.exit_time)
    _hm = str(session_halt_mode or "off").strip().lower()
    if _hm not in ("off", "halt", "close"):
        raise ValueError(f"session_halt_mode must be 'off', 'halt' or 'close', got {session_halt_mode!r}")
    CREDITSPREAD.session_halt_mode = _hm
    CREDITSPREAD.session_halt_start = _coerce_time(session_halt_start, dtime(15, 15))
    CREDITSPREAD.session_halt_end = _coerce_time(session_halt_end, dtime(9, 20))
    if _hm != "off":
        if CREDITSPREAD.session_halt_start == CREDITSPREAD.session_halt_end:
            raise ValueError("session_halt_start and session_halt_end must differ")
        print(f"Session halt: mode={_hm} window {CREDITSPREAD.session_halt_start}"
              f" -> {CREDITSPREAD.session_halt_end}"
              f"{' (next day)' if CREDITSPREAD.session_halt_start > CREDITSPREAD.session_halt_end else ''}"
              " | SL / flip / targets / entries frozen inside the window; scheduled exits still run")
        # Entries must stop before the halt starts, otherwise a position opened
        # after no_entry_after could be scheduled to close before it was opened.
        if CREDITSPREAD.session_halt_start <= CREDITSPREAD.no_entry_after:
            raise ValueError(f"session_halt_start {CREDITSPREAD.session_halt_start} must be later than "
                             f"no_entry_after {CREDITSPREAD.no_entry_after}")
        if CREDITSPREAD.exit_time >= CREDITSPREAD.session_halt_start:
            _m = CREDITSPREAD.session_halt_start.hour * 60 + CREDITSPREAD.session_halt_start.minute - 1
            print(f"Expiry-day exit_time {CREDITSPREAD.exit_time} is inside the halt window -> expiry / intraday "
                  f"exits will be scheduled at {dtime(_m // 60, _m % 60)} (one minute before the halt)")
    if CREDITSPREAD.position_mode == "intraday":
        if CREDITSPREAD.intraday_exit_time < CREDITSPREAD.no_entry_after:
            raise ValueError(f"intraday_exit_time {CREDITSPREAD.intraday_exit_time} must not be earlier than "
                             f"no_entry_after {CREDITSPREAD.no_entry_after}, otherwise entries would be closed at once")
        if CREDITSPREAD.intraday_exit_time >= dtime(15, 30):
            raise ValueError("intraday_exit_time must be before 15:30")
    CREDITSPREAD.hedge_entry_time = _coerce_time(hedge_entry_time, dtime(15, 25))
    CREDITSPREAD.hedge_exit_time = _coerce_time(hedge_exit_time, dtime(9, 20))

    # Reset class-level logs. All of these must be cleared so a second main()
    # call in the same process (optimizer resume, notebook re-run) starts clean.
    CREDITSPREAD.signals = []
    CREDITSPREAD.hedges_log = []
    CREDITSPREAD.skipped_log = []
    CREDITSPREAD.charges_breakdown = {name: 0.0 for name in CHARGE_COMPONENTS}
    CREDITSPREAD.hedges_opened_count = 0

    output_filename = output_path or f"BACKTEST_CREDIT_SPREAD_{symbol}.xlsx"

    if not Spot_data_path or not os.path.exists(Spot_data_path):
        print(f"Spot data file not found: {Spot_data_path}")
        return

    print(f"Loading spot data from: {Spot_data_path}")
    data = pd.read_parquet(Spot_data_path)
    data.columns = ["date", "time", "open", "high", "low", "close", "volume", "io"]
    
    data["timestamp"] = data.apply(
        lambda row: datetime.combine(
            datetime.strptime(str(row["date"]), "%Y%m%d").date(), 
            datetime.strptime(str(row["time"]), "%H:%M").time()
        ),
        axis=1
    )

    # Keep only required columns + timestamp for resampling
    data = data[["timestamp", "open", "high", "low", "close", "volume"]]
    data = data.sort_values("timestamp").reset_index(drop=True)
    
    # Convert OHLC to float
    cols_to_numeric = ["open", "high", "low", "close", "volume"]
    data[cols_to_numeric] = data[cols_to_numeric].astype(float)

    # Filter by backtest period.
    # Accepts the nested Backtest_period dict and/or the flat start_date /
    # end_date arguments, so a walk-forward step can override the window in
    # either style. Missing bounds simply leave that side unfiltered.
    period = _resolve_backtest_period(Backtest_period, start_date, end_date)
    period_start = period.get("start_date")
    period_end = period.get("end_date")

    if period_start is not None:
        data = data[data["timestamp"] >= pd.Timestamp(period_start)]
    if period_end is not None:
        end_ts = pd.Timestamp(period_end)
        if inclusive_end_date and end_ts == end_ts.normalize():
            # Opt-in: stretch a bare date to 23:59:59.999999 so the final
            # session is traded. Off by default to keep legacy runs identical.
            end_ts = end_ts + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        data = data[data["timestamp"] <= end_ts]

    if data.empty:
        print(f"No spot data in range {period_start} .. {period_end}")
        _write_empty_report(output_filename, trading_capital)
        return {"status": "no_data", "total_trades": 0, "net_pnl": 0.0,
                "report_path": output_filename}

    # FIX (Bug 1): hand the strategy the 1-min spot series so intrabar
    # trigger minutes can be located and fills priced at/after the trigger.
    m1 = data.set_index("timestamp").sort_index()
    m1 = m1[(m1.index.time >= dtime(9, 15)) & (m1.index.time < dtime(15, 30))]
    CREDITSPREAD.spot_m1_ts = m1.index.values.astype("datetime64[ns]").astype(np.int64)
    CREDITSPREAD.spot_m1_high = m1["high"].values.astype(float)
    CREDITSPREAD.spot_m1_low = m1["low"].values.astype(float)
    # REPORT ADD-ON (intrinsic audit) — same bars, extra columns, no logic use.
    CREDITSPREAD.spot_m1_open = m1["open"].values.astype(float)
    CREDITSPREAD.spot_m1_close = m1["close"].values.astype(float)
    # Session-halt mask over the same bars (None when off -> zero behaviour change)
    if CREDITSPREAD.session_halt_mode != "off":
        _tt = m1.index.time                       # timestamp is the index here, not a column
        _a, _b = CREDITSPREAD.session_halt_start, CREDITSPREAD.session_halt_end
        _in = ((_tt >= _a) & (_tt < _b)) if _a <= _b else ((_tt >= _a) | (_tt < _b))
        _in = np.asarray(_in, dtype=bool)
        CREDITSPREAD.spot_m1_active = ~_in
        print(f"Session halt mask: {int(_in.sum())} of {len(_in)} spot minutes frozen")
    else:
        CREDITSPREAD.spot_m1_active = None
    CREDITSPREAD.tf_minutes = int(pd.Timedelta(Timeframe).total_seconds() // 60)

    # Resample to required timeframe
    print(f"Consolidating to {Timeframe} timeframe...")
    data = ohlc_consolidate(data, Timeframe, Isvolume=False)
    
    print(f"Computing SuperTrend (length={ATR_len}, multiplier={ATR_mult})...")
    data = compute_supertrend(data, ATR_len, ATR_mult)
    
    print("Merging expiry dates...")
    data = merge_expires(data, symbol)
    
    data.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close"
    }, inplace=True)
    
    data = signal_generator(data)
    
    # Save intermediate data for debugging (skippable during large optimizer
    # sweeps, where one ~1 MB CSV per batch adds up fast).
    if save_debug_data:
        data.to_csv("data_debug.csv")
    print(f"Data shape: {data.shape}, Date range: {data.index.min()} to {data.index.max()}")

    # --- Run Backtest ---
    print("Starting Backtest...")
    CREDITSPREAD.last_bar_ts = pd.Timestamp(data.index[-1])
    bt = Backtest(
        data,
        CREDITSPREAD,
        cash=10_000_000,
        commission=0.0002,
        trade_on_close=False
    )
    stats = bt.run()
    
    # --- Process Trade Log ---
    print("\n--- Backtest Complete ---")
    signals_list = CREDITSPREAD.signals
    
    if len(signals_list) == 0:
        print("\nNo trades executed during this period.")
        print("Check if:")
        print("  1. Option data files exist in the OPTIONS_PATH directory")
        print("  2. File naming convention matches (e.g., NIFTY02JUN2622900CE.csv)")
        print("     — switch `option_symbol_format` if your files look like NIFTY2003059650CE.csv")
        print("  3. SuperTrend signals are being generated (check data_debug.csv)")
        _write_empty_report(output_filename, trading_capital)
        return {"status": "no_trades", "total_trades": 0, "net_pnl": 0.0,
                "report_path": output_filename}

    trades_df = pd.DataFrame(signals_list)
    trades_df.to_csv(f"TRADES_RAW_{symbol}.csv")
    print(f"\nTotal exit events executed: {len(trades_df)}")
    
    # Aggregate trades_df by position_id to recreate single rows per trade for metrics
    agg_funcs = {
        'signal_timestamp': 'first',
        'entry_time': 'first',
        'signal_type': 'first',
        'entry_breakout_high': 'first',
        'entry_breakout_low': 'first',
        'entry_type': 'first',
        'spot_price_at_entry': 'first',
        'exit_timestamp': 'max',
        'spot_price_at_exit': 'last',
        'short_strike': 'first',
        'long_strike': 'first',
        'profit_in_inr': 'sum',
        'charges_inr': 'sum',
        'net_profit_in_inr': 'sum',
        'total_qty': 'first',
        'lots': 'first',
        'lot_size': 'first',
        'margin_per_lot': 'first',
        'span_margin': 'first',
        'exposure_margin': 'first',
        'premium_received': 'first',
        'capital_used': 'first',
        'trading_capital_at_entry': 'first',
        'reason_for_exit': 'last',
        'rolled_from_0dte': 'first',
        'synthetic_legs': 'first',
        'structural_max_loss_inr': 'first',
        'sizing_mode': 'first',
        'spread_type': 'first',
        'net_premium': 'first',
    }
    grouped_df = trades_df.groupby('position_id').agg(agg_funcs).reset_index()
    # Re-calculate profit_points for the aggregated trade (average points per unit)
    grouped_df['profit_points'] = grouped_df['profit_in_inr'] / grouped_df['total_qty']
    print(f"Total unique trades executed: {len(grouped_df)}")
    
    # --- Combine Hedge PNL ---
    hedge_pnl_by_pos = {}
    if len(CREDITSPREAD.hedges_log) > 0:
        hedges_df = pd.DataFrame(CREDITSPREAD.hedges_log)
        hedges_df["pnl_inr"] = (hedges_df["points_captured"] * hedges_df["qty"]) - hedges_df.get("charges_inr", 0)
        hedge_pnl_by_pos = hedges_df.groupby("position_id")["pnl_inr"].sum().to_dict()
    
    grouped_df["hedge_pnl_inr"] = grouped_df["position_id"].map(hedge_pnl_by_pos).fillna(0)
    # profit_with_hedges_inr is now NET OF ALL CHARGES on both spread and hedges
    grouped_df["profit_with_hedges_inr"] = grouped_df["net_profit_in_inr"] + grouped_df["hedge_pnl_inr"]
    grouped_df["hedge_profit_points"] = grouped_df["hedge_pnl_inr"] / grouped_df["total_qty"]
    grouped_df["profit_with_hedges_points"] = grouped_df["profit_points"] + grouped_df["hedge_profit_points"]
    
    # --- Drawdown Analysis (shared implementation) ---
    # The P&L series that drives the equity curve is the same one the report's
    # Equity Curve tab uses: realised spread P&L net of all statutory charges,
    # plus overnight-hedge P&L, in rupees or in index points depending on
    # show_pnl_in_rupees. Position exits are the equity events, so
    # exit_timestamp is the time axis.
    pnl_column = "profit_with_hedges_inr" if show_pnl_in_rupees else "profit_with_hedges_points"
    dd_episodes, dd_equity = calculate_drawdown_episodes(
        grouped_df, trading_capital, pnl_col=pnl_column, time_col="exit_timestamp"
    )
    dd_summary = summarize_drawdown(dd_episodes, dd_equity)

    # Extra statistics rows for the optimizer, on top of the drawdown summary.
    total_charges = float(grouped_df["charges_inr"].sum())
    gross_before_costs = float(grouped_df["profit_in_inr"].sum())
    net_after_costs = float(grouped_df[pnl_column].sum())

    extra_metrics = dict(dd_summary)
    extra_metrics["Total Exit Events"] = len(trades_df)
    extra_metrics["Hedge Net PnL"] = float(grouped_df["hedge_pnl_inr"].sum())
    # ---- Issue 1 metrics: sizing on structural loss + drawdown that qty cannot flatter ----
    extra_metrics["Per-Position Max Loss % Setting (0=off)"] = float(CREDITSPREAD.max_loss_per_position_pct)
    extra_metrics["Positions Sized By Max-Loss Cap"] = int((grouped_df["sizing_mode"] == "max_loss_pct").sum())
    _sml = grouped_df["structural_max_loss_inr"].astype(float)
    _cap_at_entry = grouped_df["trading_capital_at_entry"].replace(0, np.nan).astype(float)
    extra_metrics["Avg Structural Max Loss Per Position (INR)"] = float(_sml.mean())
    extra_metrics["Max Structural Max Loss Per Position (INR)"] = float(_sml.max())
    extra_metrics["Avg Structural Max Loss % of Capital"] = float((_sml / _cap_at_entry * 100).mean())
    extra_metrics["Max Structural Max Loss % of Capital"] = float((_sml / _cap_at_entry * 100).max())
    # Drawdown in INR against the FIXED initial capital (the number that sizes the trades)
    _dd_inr = float(dd_summary.get("Max Drawdown $", 0.0) or 0.0)
    extra_metrics["Max Drawdown % vs Initial Capital"] = (_dd_inr / trading_capital * 100.0) if trading_capital else 0.0
    # Basis that agrees with the compounding switch: compounding ON -> vs peak equity
    # (capital really grew); OFF -> vs initial capital (that is all you ever had).
    _dd_pct_peak = dd_summary.get("Max Drawdown %", 0.0)
    try:
        _dd_pct_peak = float(str(_dd_pct_peak).replace("%", ""))
    except Exception:
        _dd_pct_peak = 0.0
    extra_metrics["Max Drawdown % Basis (0=vs initial capital, 1=vs peak equity)"] = 1.0 if CREDITSPREAD.compound_capital else 0.0
    extra_metrics["Max Drawdown % (basis per compounding setting)"] = (
        _dd_pct_peak if CREDITSPREAD.compound_capital else extra_metrics["Max Drawdown % vs Initial Capital"])
    # Points per unit: independent of lots / qty, so sizing cannot flatter it
    _pts = grouped_df.sort_values("exit_timestamp")["profit_with_hedges_points"].astype(float)
    _cum = _pts.cumsum()
    _ddp = (_cum - _cum.cummax()).min() if len(_cum) else 0.0
    extra_metrics["Net Points Per Unit (after charges + hedges)"] = float(_pts.sum())
    extra_metrics["Max Drawdown Points Per Unit"] = float(_ddp)
    _w = float(CREDITSPREAD.long_distance - CREDITSPREAD.short_distance)
    extra_metrics["Max Drawdown Points Per Unit / Spread Width"] = (float(_ddp) / _w) if _w else 0.0
    extra_metrics["Hedge Distance Setting (pts, 0=long leg)"] = float(CREDITSPREAD.hedge_distance)
    extra_metrics["Spread Type (0=credit, 1=debit)"] = 1.0 if CREDITSPREAD.spread_type == "debit" else 0.0
    extra_metrics["Position Mode (0=positional, 1=intraday)"] = 1.0 if CREDITSPREAD.position_mode == "intraday" else 0.0
    extra_metrics["Targets Enabled (1=Yes)"] = 1.0 if CREDITSPREAD.use_targets else 0.0
    extra_metrics["Intraday Time Exits"] = int((grouped_df["reason_for_exit"] == "Intraday Time Exit").sum())
    extra_metrics["Fill Gap Bound (min, 0=off)"] = float(CREDITSPREAD.max_fill_gap_minutes)
    extra_metrics["Flip Exit Mode (0=breakout, 1=immediate)"] = 1.0 if CREDITSPREAD.flip_exit_mode == "immediate" else 0.0
    extra_metrics["Exit Sanity Gate (0=off, 1=intrinsic)"] = 1.0 if CREDITSPREAD.exit_sanity_gate == "intrinsic" else 0.0
    extra_metrics["Exit Gate: Fills Adjusted"] = int(getattr(CREDITSPREAD, "exit_gate_adjustments", 0))
    extra_metrics["Exit Gate: P&L Removed (INR, + = raw was overstated)"] = float(getattr(CREDITSPREAD, "exit_gate_inr", 0.0))
    extra_metrics["Session Halt Mode (0=off, 1=halt, 2=close)"] = {"off": 0.0, "halt": 1.0, "close": 2.0}[CREDITSPREAD.session_halt_mode]
    extra_metrics["Session Halt Start (HHMM)"] = float(CREDITSPREAD.session_halt_start.hour * 100 + CREDITSPREAD.session_halt_start.minute)
    extra_metrics["Session Halt End (HHMM)"] = float(CREDITSPREAD.session_halt_end.hour * 100 + CREDITSPREAD.session_halt_end.minute)
    extra_metrics["Session Close Exits"] = int((grouped_df["reason_for_exit"] == "Session Close Exit").sum())
    _sk = pd.DataFrame(CREDITSPREAD.skipped_log) if CREDITSPREAD.skipped_log else pd.DataFrame(columns=["reason"])
    extra_metrics["Entries Skipped: no mark within fill bound"] = int((_sk["reason"] == "no_option_marks_at_trigger").sum()) if len(_sk) else 0
    extra_metrics["Exits Filled At Stale (pre-trigger) Mark"] = int((_sk["reason"] == "stale_exit_mark").sum()) if len(_sk) else 0
    extra_metrics["Partials Filled At Detection Mark (no next mark in bound)"] = int((_sk["reason"] == "partial_fill_no_next_mark").sum()) if len(_sk) else 0
    if CREDITSPREAD.spread_type == "debit":
        extra_metrics["Avg Net Debit Paid (per unit)"] = float((-grouped_df["net_premium"].astype(float)).mean())
    extra_metrics["Target 1 Qty %"] = float(CREDITSPREAD.targets_qty_credit_spread.get("t1", 40))
    extra_metrics["Target 2 Qty %"] = float(CREDITSPREAD.targets_qty_credit_spread.get("t2", 40))
    extra_metrics["Target 3 Qty % (remainder)"] = float(CREDITSPREAD.targets_qty_credit_spread.get("t3", 20))
    extra_metrics["Partial Exits Adjusted By Lot Rounding"] = int(getattr(CREDITSPREAD, "partial_qty_adjustments", 0))
    if len(CREDITSPREAD.hedges_log):
        _hs = pd.DataFrame(CREDITSPREAD.hedges_log)
        if "hedge_source" in _hs:
            extra_metrics["Hedges On hedge_distance Strike"] = int((_hs["hedge_source"] == "hedge_distance").sum())
            extra_metrics["Hedges Fallen Back To Long Leg"] = int((_hs["hedge_source"] == "long_leg").sum()) if CREDITSPREAD.hedge_distance > 0 else 0
    extra_metrics["Trading Capital"] = trading_capital
    extra_metrics["Capital Utilization Setting %"] = float(capital_utilization_percent)
    extra_metrics["Margin SPAN Factor"] = float(CREDITSPREAD.margin["span_factor"])
    extra_metrics["Margin Exposure % (latest)"] = float(_rate_on(CREDITSPREAD.margin["exposure_pct"], None) * 100)
    extra_metrics["Compounding (1=Yes)"] = 1.0 if CREDITSPREAD.compound_capital else 0.0
    extra_metrics["Final Trading Capital"] = float(CREDITSPREAD.trading_capital)
    # Capital actually deployed per position = lots x margin/lot, vs capital at entry
    _util = grouped_df["capital_used"] / grouped_df["trading_capital_at_entry"].replace(0, np.nan) * 100.0
    extra_metrics["Avg Lots"] = float(grouped_df["lots"].mean())
    extra_metrics["Min Lots"] = float(grouped_df["lots"].min())
    extra_metrics["Max Lots"] = float(grouped_df["lots"].max())
    extra_metrics["Avg Margin Per Lot"] = float(grouped_df["margin_per_lot"].mean())
    extra_metrics["Avg SPAN Per Lot"] = float((grouped_df["span_margin"] / grouped_df["lots"]).mean())
    extra_metrics["Avg Exposure Per Lot"] = float((grouped_df["exposure_margin"] / grouped_df["lots"]).mean())
    extra_metrics["Avg Premium Received"] = float(grouped_df["premium_received"].mean())
    extra_metrics["Avg Net Cash Blocked (margin - premium)"] = float((grouped_df["capital_used"] - grouped_df["premium_received"]).mean())
    extra_metrics["Avg Capital Used"] = float(grouped_df["capital_used"].mean())
    extra_metrics["Max Capital Used"] = float(grouped_df["capital_used"].max())
    extra_metrics["Avg Capital Utilization %"] = float(_util.mean())
    extra_metrics["Max Capital Utilization %"] = float(_util.max())
    for _lot, _g in grouped_df.groupby("lot_size"):
        extra_metrics[f"Avg Lots @ lot {int(_lot)}"] = float(_g["lots"].mean())
    # Cost-model metrics. "Net PnL After Costs" and "Brokerage Ratio %" are the
    # exact names optimizer_engine.priority_score() reads, so this variant —
    # which actually models charges — can be ranked on cost efficiency.
    extra_metrics["Gross PnL Before Costs"] = gross_before_costs
    extra_metrics["Total Charges"] = total_charges
    extra_metrics["Net PnL After Costs"] = net_after_costs
    extra_metrics["Brokerage Ratio %"] = (
        (total_charges / abs(gross_before_costs) * 100.0) if gross_before_costs else 0.0
    )

    # ---- Itemised cost model -------------------------------------------------
    # "Total Charges" above is the spread legs only (it feeds Brokerage Ratio %
    # and the optimizer's ranking). The breakdown below covers every leg the run
    # actually paid for, hedges included, so the rows reconcile as:
    #     sum(line items) == Total Charges + Charges - Hedge Legs
    charges_enabled = bool(CREDITSPREAD.costs.get("apply_charges", True))
    hedge_charges = float(sum(h.get("charges_inr", 0.0) for h in CREDITSPREAD.hedges_log))
    breakdown = CREDITSPREAD.charges_breakdown
    extra_metrics["Charges Enabled (1=Yes)"] = 1.0 if charges_enabled else 0.0
    for component in CHARGE_COMPONENTS:
        extra_metrics[CHARGE_LABELS[component]] = float(breakdown.get(component, 0.0))
    extra_metrics["Charges - Hedge Legs"] = hedge_charges
    extra_metrics["Charges - All Legs Total"] = float(sum(breakdown.values()))
    extra_metrics["Charges - Per Trade Avg"] = (
        float(sum(breakdown.values())) / len(grouped_df) if len(grouped_df) else 0.0
    )
    # Slippage never appears as a charge line — it is baked into fill prices —
    # so publish the rate in force, otherwise a zero-charge run looks free when
    # it is still paying spread on every leg.
    extra_metrics["Slippage Per Leg (Rs)"] = float(CREDITSPREAD.costs["slippage_per_leg"])
    _so, _lo = CREDITSPREAD._leg_offsets(CREDITSPREAD)
    extra_metrics["Short Leg Offset (pts)"] = _so
    extra_metrics["Long Leg Offset (pts)"] = _lo
    extra_metrics["Spread Width (pts)"] = _lo - _so
    _rq = getattr(CREDITSPREAD, "requested_legs", (0, 0))
    extra_metrics["Requested Short Distance"] = _rq[0]
    extra_metrics["Requested Long Distance"] = _rq[1]
    extra_metrics["Legs Adjusted (1=Yes)"] = 1.0 if any("WARNING" in n for n in getattr(CREDITSPREAD, "leg_notes", [])) else 0.0
    extra_metrics["Expiry Selection"] = {"near": 0.0, "next": 1.0, "far": 2.0}[CREDITSPREAD.expiry_selection]  # 0=near 1=next 2=far
    extra_metrics["Near Skip 0DTE (1=Yes)"] = 1.0 if CREDITSPREAD.near_skip_0dte else 0.0
    extra_metrics["End Of Data Forced Exits"] = int((grouped_df["reason_for_exit"] == "End of Data (forced)").sum())
    extra_metrics["Entries Rolled From 0DTE"] = int(grouped_df["rolled_from_0dte"].fillna(False).astype(bool).sum())
    extra_metrics["Missing Option Pricing (0=skip, 1=intrinsic)"] = 1.0 if CREDITSPREAD.missing_option_pricing == "intrinsic" else 0.0
    _syn = grouped_df["synthetic_legs"].fillna("").astype(str) != ""
    extra_metrics["Synthetic-Leg Positions"] = int(_syn.sum())
    extra_metrics["Synthetic-Leg Net PnL (INR)"] = float(grouped_df.loc[_syn, "profit_with_hedges_inr"].sum()) if _syn.any() else 0.0
    extra_metrics["Synthetic-Leg Net Points Per Unit"] = float(grouped_df.loc[_syn, "profit_with_hedges_points"].sum()) if _syn.any() else 0.0
    extra_metrics["Real-Leg Net Points Per Unit"] = float(grouped_df.loc[~_syn, "profit_with_hedges_points"].sum())
    extra_metrics["Skipped Entries"] = len(CREDITSPREAD.skipped_log)
    # Hedge audit: overnight carries vs hedges actually opened. Previous runs
    # showed 800+ overnight positions and ZERO hedges — that is a broken run,
    # not a result. Publish both so it can never be missed again.
    _ent = pd.to_datetime(grouped_df["entry_time"]).dt.date
    _ext = pd.to_datetime(grouped_df["exit_timestamp"]).dt.date
    overnight_positions = int((_ent != _ext).sum())
    extra_metrics["Overnight Positions"] = overnight_positions
    extra_metrics["Hedges Allowed (1=Yes)"] = 1.0 if CREDITSPREAD.hedges_allowed else 0.0
    extra_metrics["Hedges Opened"] = int(getattr(CREDITSPREAD, "hedges_opened_count", 0))
    extra_metrics["Hedges Closed"] = len(CREDITSPREAD.hedges_log)
    if CREDITSPREAD.hedges_allowed and overnight_positions > 0 and len(CREDITSPREAD.hedges_log) == 0:
        print("!! HEDGE AUDIT: {} positions carried overnight but NO hedges were opened/closed. "
              "Check end_of_day_candle / hedge_entry_time / hedges_qty_percent — "
              "the overnight-hedge framework did NOT run.".format(overnight_positions))

    # PNL Scaling Logic (Points vs Rupees)
    # the metrics calculation should use rupees if show_pnl_in_rupees is true
    if show_pnl_in_rupees:
        metrics_df = grouped_df.copy()
        metrics_df["profit_points"] = metrics_df["profit_with_hedges_inr"]
        final_df = calculate_metrics(metrics_df, capital=trading_capital, extra_metrics=extra_metrics)
    else:
        metrics_df = grouped_df.copy()
        metrics_df["profit_points"] = metrics_df["profit_with_hedges_points"]
        final_df = calculate_metrics(metrics_df, capital=trading_capital, extra_metrics=extra_metrics)

    print(f"Winning trades: {len(grouped_df[grouped_df['profit_with_hedges_inr' if show_pnl_in_rupees else 'profit_with_hedges_points'] > 0])}")
    print(f"Losing trades: {len(grouped_df[grouped_df['profit_with_hedges_inr' if show_pnl_in_rupees else 'profit_with_hedges_points'] <= 0])}")
    print(f"Net PNL ({'Rs' if show_pnl_in_rupees else 'points'}): {grouped_df['profit_with_hedges_inr' if show_pnl_in_rupees else 'profit_with_hedges_points'].sum():.2f}")

    # Generate Multi-Tab Excel Output
    with pd.ExcelWriter(output_filename, engine='openpyxl') as writer:
        
        # TAB 0: Skipped Entries (Bug 4/5 fix — nothing is silent anymore)
        skipped_df = pd.DataFrame(CREDITSPREAD.skipped_log)
        if skipped_df.empty:
            skipped_df = pd.DataFrame(columns=["timestamp", "reason", "detail"])
        skipped_df.to_excel(writer, sheet_name="Skipped Entries", index=False)
        print(f"Skipped entries logged: {len(skipped_df)}")
        
        # TAB 1: Trades based on index
        index_df = grouped_df.copy()
        index_df["index_points_captured"] = index_df.apply(
            lambda row: (row["spot_price_at_exit"] - row["spot_price_at_entry"]) 
            if row["signal_type"] == "long" else (row["spot_price_at_entry"] - row["spot_price_at_exit"]),
            axis=1
        )
            
        index_export = index_df[[
            "signal_timestamp", "entry_time", "signal_type", 
            "entry_breakout_high", "entry_breakout_low",
            "spot_price_at_entry", "exit_timestamp", "spot_price_at_exit", "index_points_captured", "reason_for_exit"
        ]]
        index_export.to_excel(writer, sheet_name="Index Trades", index=False)
        
        # TAB 2: Detailed tab with options (All raw data, flattened to exit events)
        detailed_cols = [
            "position_id", "signal_timestamp", "signal_type", "supertrend_value", "supertrend_direction", 
            "spot_price_at_entry", "spot_price_at_exit", "symbol", "current_expiry", "trade_expiry", "rolled_from_0dte", 
            "reference_strike", "option_type", "entry_type", "short_strike", "long_strike", "entry_time", 
            "credit_spread_entry", "short_entry_price", "long_entry_price", "exit_timestamp", "credit_spread_exit", 
            "credit_points_captured", "reason_for_exit", "trade_number_today", "total_qty",
            "lots", "lot_size", "margin_per_lot", "span_margin", "exposure_margin", "premium_received",
            "capital_used", "trading_capital_at_entry", 
            "exit_time", "exit_qty", "short_exit_price", "long_exit_price", "short_points", "long_points", "profit_in_inr",
            "structural_max_loss_inr", "sizing_mode", "spread_type", "net_premium", "synthetic_legs"
        ]
        trades_df[detailed_cols].to_excel(writer, sheet_name="Detailed Options", index=False)
        
        # TAB 3: Options Summary tab (Grouped per trade)
        summary_export = grouped_df[[
            "position_id", "signal_type", "entry_type", "entry_time", "exit_timestamp",
            "short_strike", "long_strike", "profit_points", "profit_in_inr", "profit_with_hedges_inr",
            "total_qty", "lots", "lot_size", "margin_per_lot", "span_margin", "exposure_margin",
            "premium_received", "capital_used", "trading_capital_at_entry",
            "structural_max_loss_inr", "sizing_mode"
        ]]
        summary_export.to_excel(writer, sheet_name="Options Summary", index=False)
        
        # NEW TAB: Hedges Tab
        hedges_list = CREDITSPREAD.hedges_log
        if len(hedges_list) > 0:
            hedges_df = pd.DataFrame(hedges_list)
            if show_pnl_in_rupees:
                # If scaling to rupees, scale points captured by qty
                hedges_df["pnl_inr"] = (hedges_df["points_captured"] * hedges_df["qty"]) - hedges_df.get("charges_inr", 0)
            hedges_df.to_excel(writer, sheet_name="Hedges", index=False)
        
        # TAB 4: Technical statistics & Tables
        if final_df is not None:
            # Main Stats
            stats_df = final_df[final_df["entry_time"].isna()][["entry_type", "profit_points"]]
            stats_df.rename(columns={"entry_type": "Metric", "profit_points": "Value"}, inplace=True)
            
            # Remove the monthly/yearly rows from the main summary list to create dedicated tables
            main_stats = stats_df[~stats_df['Metric'].str.contains(r'Monthly PnL|Year PnL|Trades in|Max Drawdown \(')]
            main_stats.to_excel(writer, sheet_name="Technical Statistics", index=False, startrow=0, startcol=0)
            
            # Monthly/Yearly Returns Table
        returns_df = grouped_df.copy()
        returns_df['month_year'] = pd.to_datetime(returns_df['exit_timestamp']).dt.to_period('M')
        returns_df['year'] = pd.to_datetime(returns_df['exit_timestamp']).dt.year
        
        monthly_ret = returns_df.groupby('month_year')['profit_with_hedges_inr'].sum().reset_index() if show_pnl_in_rupees else returns_df.groupby('month_year')['profit_with_hedges_points'].sum().reset_index()
        yearly_ret = returns_df.groupby('year')['profit_with_hedges_inr'].sum().reset_index() if show_pnl_in_rupees else returns_df.groupby('year')['profit_with_hedges_points'].sum().reset_index()
        
        monthly_ret.to_excel(writer, sheet_name="Technical Statistics", index=False, startrow=0, startcol=4)
        yearly_ret.to_excel(writer, sheet_name="Technical Statistics", index=False, startrow=0, startcol=7)
        
        # Trade Distribution Table (Buckets)
        pnl_series = grouped_df['profit_with_hedges_inr'] if show_pnl_in_rupees else grouped_df['profit_with_hedges_points']
        bins = [-float('inf'), -10000, -5000, 0, 5000, 10000, float('inf')] if show_pnl_in_rupees else [-float('inf'), -20, -10, 0, 10, 20, float('inf')]
        labels = ['Large Loss', 'Medium Loss', 'Small Loss', 'Small Win', 'Medium Win', 'Large Win']
        grouped_df['pnl_bucket'] = pd.cut(pnl_series, bins=bins, labels=labels)
        dist_df = grouped_df['pnl_bucket'].value_counts().sort_index().reset_index()
        dist_df.columns = ['Trade Type', 'Count']
        dist_df.to_excel(writer, sheet_name="Technical Statistics", index=False, startrow=0, startcol=10)

        # TAB 5: Equity Curve (Graph + Data)
        equity_df = grouped_df[['exit_timestamp', 'profit_with_hedges_inr' if show_pnl_in_rupees else 'profit_with_hedges_points']].copy()
        equity_df = equity_df.sort_values('exit_timestamp')
        equity_df['cumulative_pnl'] = equity_df['profit_with_hedges_inr' if show_pnl_in_rupees else 'profit_with_hedges_points'].cumsum()
        equity_df['account_balance'] = trading_capital + equity_df['cumulative_pnl']
        
        # Start table at row 20 (index 20 means row 21 in Excel), leaving space for the chart above
        start_row = 20
        equity_df.to_excel(writer, sheet_name="Equity Curve", index=False, startrow=start_row)

        wb = writer.book
        data_sheet = wb["Equity Curve"]
        
        chart = LineChart()
        chart.title = "Equity Curve"
        chart.style = 13
        chart.y_axis.title = "Cumulative PNL"
        chart.x_axis.title = "Trade Datetime"
        
        # Calculate rows for reference: pandas startrow=20 means openpyxl row 21 (header) and data starts at 22
        min_row = start_row + 1
        max_row = min_row + len(equity_df)
        
        # Column 3 is cumulative_pnl, Column 1 is exit_timestamp
        data_ref = Reference(data_sheet, min_col=3, min_row=min_row, max_row=max_row)
        cats_ref = Reference(data_sheet, min_col=1, min_row=min_row+1, max_row=max_row)
        
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        
        chart.width = 30
        chart.height = 10
        
        # Add chart to the same sheet at A1
        data_sheet.add_chart(chart, "A1")

        # REPORT ADD-ON: Intrinsic Fills + Intrinsic PnL tabs.
        # Post-processing only; wrapped so a failure here can never break the
        # rest of the report or change any number in it.
        try:
            _fills_df, _ipnl_df, _isum_df = build_intrinsic_audit(
                CREDITSPREAD.signals, CREDITSPREAD.hedges_log,
                CREDITSPREAD.spot_m1_ts, CREDITSPREAD.spot_m1_open, CREDITSPREAD.spot_m1_high,
                CREDITSPREAD.spot_m1_low, CREDITSPREAD.spot_m1_close)
            if _fills_df.empty:
                _fills_df = pd.DataFrame(columns=["position_id", "event", "leg", "side", "verdict"])
            _fills_df.to_excel(writer, sheet_name="Intrinsic Fills", index=False)
            _isum_df.to_excel(writer, sheet_name="Intrinsic PnL", index=False, startrow=0, startcol=0)
            if not _ipnl_df.empty:
                _ipnl_df.to_excel(writer, sheet_name="Intrinsic PnL", index=False, startrow=0, startcol=4)
            _n_bad = int(_fills_df["below_intrinsic_min"].sum()) if "below_intrinsic_min" in _fills_df else 0
            _dist = float(_fills_df["pnl_distortion_inr"].sum()) if "pnl_distortion_inr" in _fills_df else 0.0
            print(f"Intrinsic audit: {len(_fills_df)} fills checked, {_n_bad} below intrinsic, "
                  f"net P&L distortion {_dist:,.0f} INR (+ = booked P&L overstated)")
        except Exception as _exc:
            print(f"Warning: intrinsic audit tabs skipped: {_exc}")

        # TAB 6 & 7: Drawdown Analysis + Drawdown Episodes
        # Same layout and statistics as the XAUUSD Supertrend report, produced
        # by the shared backtest_analytics implementation driven by this
        # strategy's own (cost-adjusted) equity series.
        write_drawdown_sheets(
            writer,
            grouped_df,
            initial_capital=trading_capital,
            pnl_col=pnl_column,
            time_col="exit_timestamp",
            currency_symbol="Rs" if show_pnl_in_rupees else "points",
            x_axis_title="Trade Exit Time",
        )

    print(f"\nMulti-tab Excel saved to {output_filename}")

    return {
        "status": "ok",
        "symbol": symbol,
        "requested_short_distance": getattr(CREDITSPREAD, "requested_legs", (0, 0))[0],
        "requested_long_distance": getattr(CREDITSPREAD, "requested_legs", (0, 0))[1],
        "effective_short_distance": int(CREDITSPREAD.short_distance),
        "effective_long_distance": int(CREDITSPREAD.long_distance),
        "legs_adjusted": bool(any("WARNING" in n for n in getattr(CREDITSPREAD, "leg_notes", []))),
        "leg_notes": list(getattr(CREDITSPREAD, "leg_notes", [])),
        "total_trades": int(len(grouped_df)),
        "total_exit_events": int(len(trades_df)),
        "net_pnl": net_after_costs,
        "gross_pnl_before_costs": gross_before_costs,
        "total_charges": total_charges,
        "charges_enabled": charges_enabled,
        "charges_breakdown": {k: float(v) for k, v in breakdown.items()},
        "hedge_charges": hedge_charges,
        "pnl_unit": "INR" if show_pnl_in_rupees else "points",
        "max_drawdown": dd_summary.get("Max Drawdown $", 0.0),
        "max_drawdown_pct": dd_summary.get("Max Drawdown %", 0.0),
        "drawdown_episodes": int(dd_summary.get("Total Drawdown Count", 0)),
        "skipped_entries": len(CREDITSPREAD.skipped_log),
        "report_path": os.path.abspath(output_filename),
    }


if __name__ == "__main__":
    CONFIG = dict(
        symbol="NIFTY",
        step_size={"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25},
        
        Spot_data_path=r"C:\Users\ADMIN\Desktop\Datasets\Niftyoptions_data_parquet\NIFTY Spot\NIFTY.parquet",
        Options_dir_Path=r"C:\Users\ADMIN\Desktop\Datasets\Niftyoptions_data_parquet\NIFTY Options",

        # Option CSV filename convention — switch this to match your data folder:
        #   "ddMMMyy" -> NIFTY02JUN2622900CE  (day + MMM + yy + strike + CE/PE)
        #   "yymmdd"  -> NIFTY2003059650CE    (yy + mm + dd + strike + CE/PE)
        option_symbol_format="yymmdd",

        Backtest_period={"start_date": ddate(2020, 3, 1), "end_date": ddate(2026, 8, 31)},
        Timeframe="15min",
        ATR_len=16,              # SuperTrend ATR Length (per strategy PDF)
        ATR_mult=1.72,           # SuperTrend Multiplier (per strategy PDF)
        buffer_point=10,        # Buffer points on signal candle high/low
        supertrend_sl_buffer=10, # SuperTrend Hard SL buffer
        spread_distance=150,    # legacy: short at 150, long at 300 (used only when the two below are None)
        short_distance=0,       # sell leg offset from ref. 0 = derive (see resolve_leg_distances)
        long_distance=0,        # buy  leg offset from ref. 0 = short + spread_distance
        expiry_selection="next",  # "near" = current expiry, "next" = one after (legacy), "far" = two out
        near_skip_0dte=True,      # near: on expiry day use the NEXT expiry instead of the 0-DTE contract
        # Lot size by CONTRACT EXPIRY date range (inclusive). Edit here when NSE changes it.
        # A plain int (e.g. lot_size=75) is also accepted and applies to all dates.
        lot_size={
            "2000-01-01:2024-04-25": 50,
            "2024-04-26:2024-11-19": 25,
            "2024-11-20:2025-12-31": 75,
            "2026-01-01:2099-12-31": 65,
        },
        
        # Capital Management Config
        capital_utilization_percent=50,   # % of capital deployable per position
        trading_capital=500000,           # Demat account capital
        # Broker margin model (see margin_components): total = SPAN + exposure.
        # SPAN ~= width x lot x span_factor; exposure = pct x spot x lot on the short leg.
        margin=dict(span_factor=1.0, exposure_pct=[("2000-01-01", 0.02)]),
        compound_capital=False,           # True: net P&L rolls into trading_capital
        
        # Display Config
        show_pnl_in_rupees=True,          # Multiply points by qty to show actual Rs
        
        hedges_allowed=True,    # False -> no overnight hedges at all
        hedges_qty_percent=23,  # Overnight hedge: 23% of remaining qty (whole lots)
        hedge_distance=0,       # 0 = hedge with the position's long leg (as before);
                                # >0 = hedge strike at reference -/+ this many pts (e.g. 200 sits between short 50 / long 400)
        max_loss_per_position_pct=0,  # 0 = margin sizing (as before); 3 or 5 = worst-case loss per position capped at 3%/5% of capital

        # ---- mode toggles (defaults = exactly the previous behaviour) ----
        spread_type="credit",         # "credit" | "debit"  (debit: buy near leg, sell far leg; set max_loss_per_position_pct!)
        position_mode="positional",   # "positional" | "intraday" (intraday: force-close same day at intraday_exit_time)
        intraday_exit_time=None,      # e.g. dtime(15, 20); None -> exit_time
        use_targets=True,             # True | False (False: no T1/T2/T3, book all only on SuperTrend flip / SL / time exit)
        # ---- session halt (closing-auction spike protection) ----
        session_halt_mode="off",      # "off" | "halt" (freeze SL/flip/targets/entries, keep position) | "close" (square off at start)
        session_halt_start=dtime(15, 15),   # window start (today)
        session_halt_end=dtime(9, 20),      # window end (next day when start > end).
                                            # Expiry-day / intraday exits that fall inside the window are pulled to 1 min before it.
        flip_exit_mode="breakout",    # "breakout" (as before) | "immediate": exit 1 min after the flip, opposite entry only on breakout
        exit_sanity_gate="off",       # "off" (as before) | "intrinsic": exit legs floored at intrinsic value, spread clipped to [0, width]
        missing_option_pricing="skip",  # "skip" (as before) | "intrinsic": price missing legs from spot intrinsic — diagnostic, tagged in report
        max_fill_gap_minutes=5,       # LOOKAHEAD BOUND: at/after fills (entry, SL, flip, targets) must print within 5 min same day;
                                      # else entry skipped / exit at last pre-trigger mark / partial at detection mark. 0 = old unbounded.
        targets_credit_spread={
            "t1": 31,           # target 1 fires when spread has decayed 31% from entry credit
            "t2": 69,           # target 2 at 69% decay
            "t3": 98            # target 3 at 98% decay
        },

        # Flat aliases for the OMS scaling targets above. The optimizer can only
        # sweep top-level scalars, so these are what you mark "optimizable" in
        # the dashboard; each one overrides its t1/t2/t3 counterpart when set.
        # They currently mirror targets_credit_spread, so results are unchanged.
        target_1_percent=31,
        target_2_percent=69,
        target_3_percent=98,

        # Qty booked at each target, % of total position qty. Same nested +
        # flat pattern as the profit targets. T3 always takes the remainder.
        # 40 / 40 / 20 = the previous hard-coded split, so results are unchanged.
        targets_qty_credit_spread={
            "t1": 40,           # book 40% of qty at target 1
            "t2": 40,           # book 40% of qty at target 2
            "t3": 20            # remainder at target 3 (informational)
        },
        target_1_qty_percent=40,
        target_2_qty_percent=40,
        target_3_qty_percent=20,

        # Execution-cost knobs (flat aliases into the `costs` dict, so the
        # optimizer can stress-test slippage/brokerage assumptions).
        apply_charges=True,         # False -> zero brokerage/STT/exchange/GST/stamp/SEBI
        slippage_per_leg=0.50,      # Rs per leg, applied ADVERSELY at every fill
        brokerage_per_order=20.0,   # flat discount-broker rate, per executed order
        min_entry_credit=2.0,       # reject entries whose net credit is below this

        # Session timings (previously hardcoded on the strategy class).
        no_entry_after=dtime(15, 15),     # No new entry / reversal after this time
        end_of_day_candle=dtime(15, 15),  # Candle that triggers the overnight hedge
        exit_time=dtime(15, 20),          # Forced exit time on expiry day
        hedge_entry_time=dtime(15, 25),   # Overnight hedge buy time
        hedge_exit_time=dtime(9, 20),     # Overnight hedge sell time next morning
    )
    main(**CONFIG)

"""
  "lot_size": {"2000-01-01:2024-04-25": 50, "2024-04-26:2024-11-19": 25, "2024-11-20:2099-12-31": 75},
  "trading_capital": 1000000,
  "capital_utilization_percent": 50,
  "margin": {"span_factor": 1.0, "exposure_pct": [["2000-01-01", 0.02]]},
  "compound_capital": false,
  "show_pnl_in_rupees": true,
  "Timeframe": "15min",
  "ATR_len": 16,
  "ATR_mult": 1.72,
  "buffer_point": 24,
  "spread_distance": 150,
  "hedges_qty_percent": 23,
  "targets_credit_spread": {
    "t1": 31,
    "t2": 69,
    "t3": 98
  }
    
    """

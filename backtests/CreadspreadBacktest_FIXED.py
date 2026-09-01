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
   - Bull Put Credit Spread: Sell PE 150 below ref, Buy PE 300 below ref

3. Short Entry:
   - SuperTrend flips from Long → Short (signal candle)
   - Wait for buffer candle, confirm break below: Signal Candle Low − 24 pts (rounded)
   - Reference strike = round DOWN trigger level to nearest 50-pt strike
   - Bear Call Credit Spread: Sell CE 150 above ref, Buy CE 300 above ref

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
        "expiry_date": {"current_expiry": None, "next_expiry": None},
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
        exp_df[["expiry_date", "expiry", "next_expiry"]].sort_values("expiry_date"),
        left_on="trade_date",
        right_on="expiry_date",
        direction="forward"
    )

    # 4) Clean up
    final_df = final_df.drop(columns=["trade_date", "expiry_date"])
    final_df.set_index(keys="datetime", inplace=True)
    final_df["expiry"] = pd.to_datetime(final_df["expiry"]).dt.date
    final_df["next_expiry"] = pd.to_datetime(final_df["next_expiry"]).dt.date
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

def find_spot_cross_minute(spot_ts, spot_hi, spot_lo, start_ts, end_ts, level, direction):
    """First 1-min spot candle in [start_ts, end_ts) crossing `level`.
    direction 'up': high >= level ; 'down': low <= level.
    spot_ts must be int64 ns, sorted. Returns pd.Timestamp or None."""
    lo_i = np.searchsorted(spot_ts, np.int64(pd.Timestamp(start_ts).value), side="left")
    hi_i = np.searchsorted(spot_ts, np.int64(pd.Timestamp(end_ts).value), side="left")
    if lo_i >= hi_i:
        return None
    if direction == "up":
        hits = np.nonzero(spot_hi[lo_i:hi_i] >= level)[0]
    else:
        hits = np.nonzero(spot_lo[lo_i:hi_i] <= level)[0]
    if len(hits) == 0:
        return None
    return pd.Timestamp(spot_ts[lo_i + hits[0]])


def option_price_at_or_after(df, ts):
    """Fill price for an option leg: first 1-min Close AT OR AFTER `ts`
    (the honest fill — never a mark from before the trigger). Falls back to
    the last available mark if the series ends. Returns (price, ts_used) or
    (None, None) if df unusable."""
    if df is None or df.empty:
        return None, None
    ts = pd.Timestamp(ts)
    tvals = df["timestamp"].values
    i = np.searchsorted(tvals, np.datetime64(ts), side="left")
    if i < len(df):
        row = df.iloc[i]
        return float(row["Close"]), pd.Timestamp(row["timestamp"])
    row = df.iloc[-1]
    return float(row["Close"]), pd.Timestamp(row["timestamp"])


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


def nifty_lot_size(d, override=None):
    """Date-dependent NIFTY lot size.
    !! VERIFY these changeover dates against NSE circulars before trusting
    INR output — they are best-effort and the historical dates matter. !!
      - 50 until the Apr-2024 reduction
      - 25 from Apr-2024 until the Nov-2024 SEBI contract-size hike
      - 75 for expiries from late Nov-2024 onward
    """
    if override is not None:
        return int(override)
    d = pd.Timestamp(d).date()
    if d < ddate(2024, 4, 26):
        return 50
    if d < ddate(2024, 11, 20):
        return 25
    return 75


DEFAULT_COSTS = dict(
    brokerage_per_order=20.0,     # flat discount-broker rate, per executed order
    exchange_txn_pct=0.00035,     # NSE txn charge on premium turnover (both sides)
    stt_sell_pct=0.001,           # STT on SELL-side premium (0.1% post Oct-2024; set 0.000625 for earlier regime)
    gst_pct=0.18,                 # GST on (brokerage + exchange charges)
    stamp_buy_pct=0.00003,        # stamp duty on buy-side premium
    sebi_pct=0.000001,            # SEBI fees on turnover
    slippage_per_leg=0.50,        # Rs per leg, applied ADVERSELY at every fill
)


def estimate_charges(sell_turnover, buy_turnover, n_orders, costs):
    """Statutory + brokerage charges in INR (slippage is applied separately,
    directly at fill prices)."""
    brokerage = n_orders * costs["brokerage_per_order"]
    exch = costs["exchange_txn_pct"] * (sell_turnover + buy_turnover)
    stt = costs["stt_sell_pct"] * sell_turnover
    gst = costs["gst_pct"] * (brokerage + exch)
    stamp = costs["stamp_buy_pct"] * buy_turnover
    sebi = costs["sebi_pct"] * (sell_turnover + buy_turnover)
    return brokerage + exch + stt + gst + stamp + sebi


class CREDITSPREAD(Strategy):
    # --- Class Variables (set before bt.run()) ---
    symbol = "NIFTY"
    step_size = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25}
    OPTIONS_PATH = r""
    option_symbol_format = "ddMMMyy"  # Option CSV naming; see OPTION_SYMBOL_FORMATS
    buffer_point = 10
    supertrend_sl_buffer = 10
    spread_distance = 150
    lot_size = 65                     # fallback only; nifty_lot_size() by date is used
    lot_size_override = None          # set to force a fixed lot size
    trading_lots = 10
    hedges_qty_percent = 20

    dynamic_qty_allocation = False
    trading_capital = 1_000_000
    capital_utilization_percent = 50
    margin_per_lot = 40_000

    no_entry_after = dtime(15, 15)
    end_of_day_candle = dtime(15, 15)
    exit_time = dtime(15, 20)
    hedge_entry_time = dtime(15, 25)
    hedge_exit_time = dtime(9, 20)
    targets_credit_spread = {"t1": 40, "t2": 80, "t3": 98}

    # FIX (Bug 1): 1-min spot arrays for intrabar trigger detection
    spot_m1_ts = None                 # int64 ns array
    spot_m1_high = None
    spot_m1_low = None
    tf_minutes = 15

    # FIX (Bug 3): costs; FIX (Bug 4/5): loud skip log
    costs = dict(DEFAULT_COSTS)
    min_entry_credit = 2.0            # reject entries whose net credit is below this

    signals = []
    hedges_log = []
    skipped_log = []

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
                    else option_price_at_or_after(df, ts))
        if px is None:
            return None, None
        return max(px - self.costs["slippage_per_leg"], 0.05), used

    def _fill_buy(self, df, ts, scheduled=False):
        px, used = (option_price_at_or_before(df, ts) if scheduled
                    else option_price_at_or_after(df, ts))
        if px is None:
            return None, None
        return px + self.costs["slippage_per_leg"], used

    # ------------------------------------------------------------------
    def _force_close_active_hedge(self, exit_timestamp, scheduled=False):
        if self.current_trade.get("active_hedge") is None:
            return
        active_hedge = self.current_trade["active_hedge"]
        long_df = self.current_trade["long_option"].get("options_data")
        exit_price, _ = self._fill_sell(long_df, exit_timestamp, scheduled=scheduled)
        if exit_price is None:
            exit_price = active_hedge["entry_price"]
        qty = active_hedge["qty"]
        charges = estimate_charges(
            sell_turnover=exit_price * qty,
            buy_turnover=active_hedge["entry_price"] * qty,
            n_orders=2, costs=self.costs)
        CREDITSPREAD.hedges_log.append({
            "position_id": self.current_trade.get("position_id"),
            "hedge_entry_time": datetime.combine(active_hedge["entry_date"], active_hedge["entry_time"]),
            "hedge_exit_time": exit_timestamp,
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
            charges = estimate_charges(sell_turn, buy_turn, n_orders, self.costs)
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
                "trade_expiry": self.current_trade["expiry_date"]["next_expiry"],
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
                "capital_used": self.current_trade["capital_used"],
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
            })

        if self.dynamic_qty_allocation:
            pnl_inr = self.current_trade["profit_in_inr"]
            if pnl_inr is not None:
                CREDITSPREAD.trading_capital += pnl_inr

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
            short_px = self.current_trade["short_option"]["entry_price"]
            long_px = self.current_trade["long_option"]["entry_price"]
            used_ts = trigger_ts
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

    def _partial_exit(self, which, row_ts, spread_row):
        """Book a partial target exit at the spread row that actually crossed
        the threshold (fill == detection mark; slippage applied)."""
        cs = self.current_trade["credit_spread"]
        total_qty = self.current_trade["total_qty"]          # Bug 7 fix
        slip = self.costs["slippage_per_leg"]
        short_px = float(spread_row["Close_short"]) + slip    # buy back short
        long_px = max(float(spread_row["Close_long"]) - slip, 0.05)  # sell long
        spread_val = short_px - long_px
        if which == "exit3":
            qty = self.current_trade["remaining_qty"]
        else:
            qty = int(total_qty * 0.4)
            qty = min(qty, self.current_trade["remaining_qty"])
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

    def _maybe_open_evening_hedge(self, current_date, current_time):
        if (self.current_trade["entry_price"] is not None and
                self.current_trade.get("remaining_qty", 0) > 0 and
                self.current_trade.get("active_hedge") is None and
                current_time >= self.end_of_day_candle):
            eff_lot = self.current_trade.get("eff_lot_size") or self.lot_size
            hedge_lots = max(1, int((self.current_trade["remaining_qty"] / eff_lot) * self.hedges_qty_percent / 100))
            hedge_qty = hedge_lots * eff_lot
            long_df = self.current_trade["long_option"].get("options_data")
            target_entry_time = datetime.combine(current_date, self.hedge_entry_time)
            entry_price, _ = self._fill_buy(long_df, target_entry_time, scheduled=True)
            if entry_price is None:
                entry_price = self.current_trade["long_option"]["entry_price"]
            self.current_trade["active_hedge"] = {
                "entry_date": current_date,
                "entry_time": self.hedge_entry_time,
                "entry_price": entry_price,
                "qty": hedge_qty,
            }

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
        trade_expiry = self.current_trade["expiry_date"]["next_expiry"]
        cursor = pd.Timestamp(trigger_ts)

        while self.current_trade["entry_price"] is not None:
            events = []
            if trade_expiry is not None and current_date >= trade_expiry:
                sched = pd.Timestamp(datetime.combine(current_date, self.exit_time))
                if sched < bar_end:
                    events.append((max(sched, cursor), 0, "TimeExit", None))
            if st_val is not None and trend_dir == self.current_trade["signal_type"]:
                if self.current_trade["signal_type"] == "long":
                    lvl = st_val - self.supertrend_sl_buffer
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               cursor, bar_end, lvl, "down")
                else:
                    lvl = st_val + self.supertrend_sl_buffer
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               cursor, bar_end, lvl, "up")
                if m is not None:
                    events.append((m, 1, "SL", lvl))
            if spread_df is not None and not spread_df.empty:
                entry_credit = self.current_trade["entry_price"]
                win = spread_df[(spread_df["timestamp"] >= cursor) & (spread_df["timestamp"] < bar_end)]
                if not win.empty and entry_credit and entry_credit > 0:
                    for i, (tkey, which) in enumerate([("t1", "exit1"), ("t2", "exit2"), ("t3", "exit3")]):
                        if self.current_trade["credit_spread"].get(which) is not None:
                            continue
                        thr = entry_credit * (1.0 - self.targets_credit_spread[tkey] / 100.0)
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
        self._maybe_open_evening_hedge(current_date, current_time)

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
            trade_expiry = self.current_trade["expiry_date"]["next_expiry"]

            cursor = bar_start
            while self.current_trade["entry_price"] is not None:
                events = []   # (ts, priority, kind, payload)

                # -- Expiry-day time exit (Bug 2 fix: compare against bar END) --
                if trade_expiry is not None and current_date >= trade_expiry:
                    if current_date > trade_expiry:
                        # overdue guard — should never happen, close immediately
                        events.append((cursor, 0, "TimeExit", None))
                    else:
                        sched = pd.Timestamp(datetime.combine(current_date, self.exit_time))
                        if sched < bar_end:
                            events.append((max(sched, cursor), 0, "TimeExit", None))

                # -- SuperTrend hard SL: trigger minute on 1-min spot --
                if st_val is not None and trend_dir == self.current_trade["signal_type"]:
                    if self.current_trade["signal_type"] == "long":
                        lvl = st_val - self.supertrend_sl_buffer
                        m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                                   cursor, bar_end, lvl, "down")
                    else:
                        lvl = st_val + self.supertrend_sl_buffer
                        m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                                   cursor, bar_end, lvl, "up")
                    if m is not None:
                        events.append((m, 1, "SL", lvl))

                # -- Flip-exit breakout: trigger minute on 1-min spot --
                if self.current_trade["exit_signal"] == "long" and self.current_trade["exit_buffer_candle"]["high"] is not None:
                    lvl = self.current_trade["exit_buffer_candle"]["high"]
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               cursor, bar_end, lvl, "up")
                    if m is not None:
                        events.append((m, 2, "Flip", ("long", lvl)))
                elif self.current_trade["exit_signal"] == "short" and self.current_trade["exit_buffer_candle"]["low"] is not None:
                    lvl = self.current_trade["exit_buffer_candle"]["low"]
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               cursor, bar_end, lvl, "down")
                    if m is not None:
                        events.append((m, 2, "Flip", ("short", lvl)))

                # -- Targets: first spread row in window crossing threshold --
                if spread_df is not None and not spread_df.empty:
                    entry_credit = self.current_trade["entry_price"]
                    win = spread_df[(spread_df["timestamp"] >= cursor) & (spread_df["timestamp"] < bar_end)]
                    if not win.empty and entry_credit and entry_credit > 0:
                        for i, (tkey, which) in enumerate([("t1", "exit1"), ("t2", "exit2"), ("t3", "exit3")]):
                            if self.current_trade["credit_spread"].get(which) is not None:
                                continue
                            thr = entry_credit * (1.0 - self.targets_credit_spread[tkey] / 100.0)
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
            self._maybe_open_evening_hedge(current_date, current_time)
            return  # still in trade, no entry logic

        # =====================================================================
        # ENTRY LOGIC (only if NOT in a trade)
        # =====================================================================
        if current_time >= self.no_entry_after:
            self.pending_sl_reentry = None
            return

        # --- False SL Re-Entry ---
        if self.pending_sl_reentry is not None:
            current_trend = "long" if self.data["SUPERTd"][-1] == 1 else "short"
            if self.pending_sl_reentry["signal_type"] != current_trend:
                self.pending_sl_reentry = None
            else:
                if self.pending_sl_reentry["signal_type"] == "long":
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               bar_start, bar_end, self.pending_sl_reentry["buffer_high"], "up")
                else:
                    m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                               bar_start, bar_end, self.pending_sl_reentry["buffer_low"], "down")
                if m is not None:
                    sig_type = self.pending_sl_reentry["signal_type"]
                    self.current_trade["signal_type"] = sig_type
                    self.current_trade["signal_timestamp"] = self.data.index[-1]
                    self.current_trade["supertrend_value"] = self.data["SUPERT"][-1]
                    self.current_trade["supertrend_direction"] = "BULLISH" if self.data["SUPERTd"][-1] == 1 else "BEARISH"
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

        current_trend = "long" if self.data["SUPERTd"][-1] == 1 else "short"
        if self.current_trade["signal_type"] != current_trend:
            self.current_trade["signal_type"] = None
            self.current_trade["buffer_candle"] = {"high": None, "low": None}
            return

        # --- Breakout: find the trigger MINUTE inside this bar (Bug 1 fix) ---
        if self.current_trade["signal_type"] == "long":
            m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                       bar_start, bar_end, self.current_trade["buffer_candle"]["high"], "up")
            if m is not None:
                self.execute_entry(True, False, self.current_trade["buffer_candle"]["high"], trigger_ts=m)
                self._post_entry_same_bar_exits(m, bar_end, current_time, current_date)
        else:
            m = find_spot_cross_minute(self.spot_m1_ts, self.spot_m1_high, self.spot_m1_low,
                                       bar_start, bar_end, self.current_trade["buffer_candle"]["low"], "down")
            if m is not None:
                self.execute_entry(False, True, self.current_trade["buffer_candle"]["low"], trigger_ts=m)
                self._post_entry_same_bar_exits(m, bar_end, current_time, current_date)

    # ------------------------------------------------------------------
    def execute_entry(self, breaks_above, breaks_below, close, trigger_ts=None):
        if self.data.index[-1].time() >= self.no_entry_after:
            self.current_trade = default_records()
            return
        if trigger_ts is None:
            trigger_ts = pd.Timestamp(self.data.index[-1])
        step = self.step_size.get(self.symbol, 50)

        if breaks_above:
            self.current_trade["option_type"] = "PE"
            self.current_trade["entry_type"] = "Bull Put Credit Spread"
            trigger_level = self.current_trade["buffer_candle"]["high"]
            ref_strike = round_up_to_strike(trigger_level, step)
            short_strike = ref_strike - self.spread_distance
            long_strike = short_strike - self.spread_distance
        elif breaks_below:
            self.current_trade["option_type"] = "CE"
            self.current_trade["entry_type"] = "Bear Call Credit Spread"
            trigger_level = self.current_trade["buffer_candle"]["low"]
            ref_strike = round_down_to_strike(trigger_level, step)
            short_strike = ref_strike + self.spread_distance
            long_strike = short_strike + self.spread_distance
        else:
            return

        self.current_trade["strike"] = ref_strike
        self.current_trade["short_option"]["strike_price"] = short_strike
        self.current_trade["short_option"]["option_type"] = self.current_trade["option_type"]
        self.current_trade["long_option"]["strike_price"] = long_strike
        self.current_trade["long_option"]["option_type"] = self.current_trade["option_type"]

        self.current_trade["expiry_date"]["current_expiry"] = self.data["expiry"][-1] if "expiry" in self.data.df.columns else None
        self.current_trade["expiry_date"]["next_expiry"] = self.data["next_expiry"][-1] if "next_expiry" in self.data.df.columns else None

        trade_expiry = self.current_trade["expiry_date"]["next_expiry"]
        if trade_expiry is None:
            self._log_skip(trigger_ts, "no_next_expiry", "expiry calendar has no next expiry here")
            self.current_trade = default_records()
            return

        # Bug 5 fix: warn loudly when the calendar hands us a far-dated "weekly"
        dte = (pd.Timestamp(trade_expiry).date() - trigger_ts.date()).days
        if dte > 12:
            self._log_skip(trigger_ts, "stale_expiry_calendar",
                           f"next expiry {trade_expiry} is {dte} days out — calendar gap; entry taken anyway, verify calendar")

        SHORT_KEY = self.generate_symbol(self.current_trade["option_type"], trade_expiry, short_strike)
        LONG_KEY = self.generate_symbol(self.current_trade["option_type"], trade_expiry, long_strike)
        SHORT_FILE = os.path.join(self.OPTIONS_PATH, SHORT_KEY + ".parquet")
        LONG_FILE = os.path.join(self.OPTIONS_PATH, LONG_KEY + ".parquet")

        if not os.path.exists(SHORT_FILE) or not os.path.exists(LONG_FILE):
            self._log_skip(trigger_ts, "option_file_missing", f"{SHORT_KEY} / {LONG_KEY}")
            self.current_trade = default_records()
            return

        try:
            SHORT_DF = pd.read_parquet(SHORT_FILE)
            SHORT_DF.columns = ["Date", "Time", "Open", "High", "Low", "Close", "Volume", "IO"]
            LONG_DF = pd.read_parquet(LONG_FILE)
            LONG_DF.columns = ["Date", "Time", "Open", "High", "Low", "Close", "Volume", "IO"]
            for D in (SHORT_DF, LONG_DF):
                D["timestamp"] = pd.to_datetime(D["Date"].astype(str) + " " + D["Time"].astype(str),
                                                format="%Y%m%d %H:%M")
                D.sort_values(by="timestamp", inplace=True)
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

        # ---- Bug 4 fix: reject impossible / worthless marks ----
        if initial_credit < self.min_entry_credit:
            self._log_skip(trigger_ts, "credit_too_small_or_inverted",
                           f"credit={initial_credit:.2f} (short={short_opt_entry_price}, long={long_opt_entry_price})")
            self.current_trade = default_records()
            return
        if initial_credit >= self.spread_distance:
            self._log_skip(trigger_ts, "credit_exceeds_width",
                           f"credit={initial_credit:.2f} >= width={self.spread_distance} — bad marks")
            self.current_trade = default_records()
            return

        # ---- Bug 6 fix: date-dependent lot size ----
        eff_lot = nifty_lot_size(self.data.index[-1], self.lot_size_override) \
            if self.symbol == "NIFTY" else self.lot_size

        self.trades_today += 1
        self.trade_counter += 1
        self.current_trade["position_id"] = self.trade_counter

        if self.dynamic_qty_allocation:
            allocated_capital = self.trading_capital * (self.capital_utilization_percent / 100)
            lots_to_trade = int(allocated_capital / self.margin_per_lot)
        else:
            lots_to_trade = self.trading_lots

        total_qty_trade = lots_to_trade * eff_lot
        capital_used_trade = lots_to_trade * self.margin_per_lot

        self.current_trade["eff_lot_size"] = eff_lot
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


def signal_generator(data: pd.DataFrame) -> pd.DataFrame:
    """Generate SuperTrend flip signals."""
    data.loc[(data["SUPERTd"] == 1) & (data["SUPERTd"].shift(1) != 1), "signal"] = "long"
    data.loc[(data["SUPERTd"] == -1) & (data["SUPERTd"].shift(1) != -1), "signal"] = "short"
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
         lot_size=65, trading_lots=5, hedges_qty_percent=23, targets_credit_spread=None,
         dynamic_qty_allocation=False, trading_capital=10_000_000,
         capital_utilization_percent=50, margin_per_lot=40000, show_pnl_in_rupees=True,
         supertrend_sl_buffer=10, costs=None, lot_size_override=None,
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
         no_entry_after=None,          # Latest time a new position may be opened
         exit_time=None,               # Forced exit time on expiry day
         end_of_day_candle=None,       # Candle that triggers the overnight hedge
         hedge_entry_time=None,        # Time the overnight hedge is bought
         hedge_exit_time=None,         # Time the overnight hedge is sold next morning
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

    # --- Configure Strategy Class Parameters ---
    CREDITSPREAD.symbol = symbol
    CREDITSPREAD.step_size = step_size
    CREDITSPREAD.OPTIONS_PATH = Options_dir_Path
    CREDITSPREAD.option_symbol_format = option_symbol_format
    CREDITSPREAD.buffer_point = buffer_point
    CREDITSPREAD.supertrend_sl_buffer = supertrend_sl_buffer
    CREDITSPREAD.spread_distance = spread_distance
    CREDITSPREAD.lot_size = lot_size
    CREDITSPREAD.trading_lots = trading_lots
    CREDITSPREAD.hedges_qty_percent = hedges_qty_percent
    CREDITSPREAD.targets_credit_spread = targets

    CREDITSPREAD.costs = dict(DEFAULT_COSTS)
    if costs:
        CREDITSPREAD.costs.update(costs)
    # Flat cost overrides — the optimizer cannot sweep values nested in the
    # `costs` dict, so expose the two that matter most as top-level scalars.
    if slippage_per_leg is not None:
        CREDITSPREAD.costs["slippage_per_leg"] = float(slippage_per_leg)
    if brokerage_per_order is not None:
        CREDITSPREAD.costs["brokerage_per_order"] = float(brokerage_per_order)
    CREDITSPREAD.lot_size_override = lot_size_override
    CREDITSPREAD.min_entry_credit = min_entry_credit
    CREDITSPREAD.dynamic_qty_allocation = dynamic_qty_allocation
    CREDITSPREAD.trading_capital = trading_capital
    CREDITSPREAD.capital_utilization_percent = capital_utilization_percent
    CREDITSPREAD.margin_per_lot = margin_per_lot

    # Session-time overrides — unchanged class defaults when not supplied.
    CREDITSPREAD.no_entry_after = _coerce_time(no_entry_after, dtime(15, 15))
    CREDITSPREAD.end_of_day_candle = _coerce_time(end_of_day_candle, dtime(15, 15))
    CREDITSPREAD.exit_time = _coerce_time(exit_time, dtime(15, 20))
    CREDITSPREAD.hedge_entry_time = _coerce_time(hedge_entry_time, dtime(15, 25))
    CREDITSPREAD.hedge_exit_time = _coerce_time(hedge_exit_time, dtime(9, 20))

    # Reset class-level logs. All three must be cleared so a second main() call
    # in the same process (optimizer resume, notebook re-run) starts clean.
    CREDITSPREAD.signals = []
    CREDITSPREAD.hedges_log = []
    CREDITSPREAD.skipped_log = []

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
        'capital_used': 'first',
        'reason_for_exit': 'last',
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
    extra_metrics["Trading Capital"] = trading_capital
    # Cost-model metrics. "Net PnL After Costs" and "Brokerage Ratio %" are the
    # exact names optimizer_engine.priority_score() reads, so this variant —
    # which actually models charges — can be ranked on cost efficiency.
    extra_metrics["Gross PnL Before Costs"] = gross_before_costs
    extra_metrics["Total Charges"] = total_charges
    extra_metrics["Net PnL After Costs"] = net_after_costs
    extra_metrics["Brokerage Ratio %"] = (
        (total_charges / abs(gross_before_costs) * 100.0) if gross_before_costs else 0.0
    )
    extra_metrics["Skipped Entries"] = len(CREDITSPREAD.skipped_log)

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
            "spot_price_at_entry", "spot_price_at_exit", "symbol", "current_expiry", "trade_expiry", 
            "reference_strike", "option_type", "entry_type", "short_strike", "long_strike", "entry_time", 
            "credit_spread_entry", "short_entry_price", "long_entry_price", "exit_timestamp", "credit_spread_exit", 
            "credit_points_captured", "reason_for_exit", "trade_number_today", "total_qty", "capital_used", 
            "exit_time", "exit_qty", "short_exit_price", "long_exit_price", "short_points", "long_points", "profit_in_inr"
        ]
        trades_df[detailed_cols].to_excel(writer, sheet_name="Detailed Options", index=False)
        
        # TAB 3: Options Summary tab (Grouped per trade)
        summary_export = grouped_df[[
            "position_id", "signal_type", "entry_type", "entry_time", "exit_timestamp",
            "short_strike", "long_strike", "profit_points", "profit_in_inr", "profit_with_hedges_inr", "capital_used"
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
        "total_trades": int(len(grouped_df)),
        "total_exit_events": int(len(trades_df)),
        "net_pnl": net_after_costs,
        "gross_pnl_before_costs": gross_before_costs,
        "total_charges": total_charges,
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
        
        Spot_data_path=r"C:\Users\ADMIN\Desktop\Niftyoptions_data_parquet\NIFTY Spot\NIFTY.parquet",
        Options_dir_Path=r"C:\Users\ADMIN\Desktop\Niftyoptions_data_parquet\NIFTY Options",

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
        spread_distance=150,    # 150 pts between reference strike and sold/bought strikes
        lot_size=65,            # NIFTY lot size
        
        # Capital Management Config
        dynamic_qty_allocation=False,     # True to use capital calc, False to use fixed lots
        capital_utilization_percent=50,   # Percentage of capital to use
        trading_capital=500000,          # Demat account capital
        margin_per_lot=50000,             # Margin required per lot
        trading_lots=5,                  # Fallback fixed lots if dynamic is False
        
        # Display Config
        show_pnl_in_rupees=True,          # Multiply points by qty to show actual Rs
        
        hedges_qty_percent=23,  # Overnight hedge: 20% of original qty
        targets_credit_spread={
            "t1": 31,           # Exit 40% qty at 40% profit
            "t2": 69,           # Exit 40% qty at 80% profit
            "t3": 98            # Exit 20% qty at 98% profit
        },

        # Flat aliases for the OMS scaling targets above. The optimizer can only
        # sweep top-level scalars, so these are what you mark "optimizable" in
        # the dashboard; each one overrides its t1/t2/t3 counterpart when set.
        # They currently mirror targets_credit_spread, so results are unchanged.
        target_1_percent=31,
        target_2_percent=69,
        target_3_percent=98,

        # Execution-cost knobs (flat aliases into the `costs` dict, so the
        # optimizer can stress-test slippage/brokerage assumptions).
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
       "lot_size": 65,
  "dynamic_qty_allocation": false,
  "trading_capital": 1000000,
  "capital_utilization_percent": 50,
  "margin_per_lot": 40000,
  "show_pnl_in_rupees": true,
  "Timeframe": "15min",
  "ATR_len": 16,
  "ATR_mult": 1.72,
  "buffer_point": 24,
  "spread_distance": 150,
  "trading_lots": 19,
  "hedges_qty_percent": 23,
  "targets_credit_spread": {
    "t1": 31,
    "t2": 69,
    "t3": 98
  }
    
    """
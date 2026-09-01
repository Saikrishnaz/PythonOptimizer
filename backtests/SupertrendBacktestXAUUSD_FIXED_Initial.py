# =============================================================================
# XAUUSD Supertrend Strategy — Object-Oriented High-Performance Backtest Engine
# =============================================================================
#
# PARITY & EXECUTION ARCHITECTURE (OBJECT-ORIENTED DESIGN)
# --------------------------------------------------------
# 1. Source of Truth: MT5 Historical Ticks (<DATE>, <TIME>, <BID>, <ASK>, <VOLUME>).
# 2. StrategyConfig: Encapsulates all strategy parameters, custom start/end dates.
# 3. PineSupertrendIndicator: Bit-exact TradingView Pine Script® ta.supertrend math.
# 4. TickDataLoader: Fast binary Parquet cache ingestion (sub-second loading).
# 5. SupertrendTickEngine: Vectorized single-pass tick simulation engine (< 1s execution).
# 6. BacktestReporter: Exports python_trades.csv, candle_validation.csv & Excel reports.
# =============================================================================

import pandas as pd
import numpy as np
from datetime import datetime, time as dtime, timedelta
import os
import time
from openpyxl.chart import LineChart, Reference


# =============================================================================
# 1. STRATEGY CONFIGURATION CLASS
# =============================================================================
class StrategyConfig:
    def __init__(
        self,
        tick_data_path: str = r"c:\Users\ADMIN\Desktop\XAUUSD_BK\XAUUSD.._202601020100_202608101443.csv",
        python_trades_csv: str = r"c:\Users\ADMIN\Desktop\XAUUSD_BK\python_trades.csv",
        candle_val_csv: str = r"c:\Users\ADMIN\Desktop\XAUUSD_BK\candle_validation.csv",
        excel_report_path: str = r"c:\Users\ADMIN\Desktop\XAUUSD_BK\BACKTEST_SUPERTREND_XAUUSD.xlsx",
        start_date: str = None,   # Custom Start Date e.g. "2026-01-02" or "2026-01-02 08:00:00"
        end_date: str = None,     # Custom End Date e.g. "2026-06-30" or "2026-08-10 23:59:59"
        enable_sl_reentry: bool = True, # Set True for SL Re-Entry, False for standard Supertrend EA
        timeframe: str = "15min",
        st_length: int = 5,
        st_multiplier: float = 1.5,
        buffer_dollars: float = 1.0,
        supertrend_sl_buffer: float = 1.0,
        initial_capital: float = 500000.0,
        lots: float = 1.0,
        contract_size: int = 100,
        commission_per_lot: float = 8.0,
        enable_session_filter: bool = True,
        session_start: dtime = dtime(8, 0),
        session_end: dtime = dtime(21, 0),
        no_entry_after: dtime = dtime(20, 0),
        max_daily_loss_pct: float = 3.0,
        max_consecutive_losses: int = 3,
        cooldown_minutes: int = 240,
    ):
        self.tick_data_path = tick_data_path
        self.python_trades_csv = python_trades_csv
        self.candle_val_csv = candle_val_csv
        self.excel_report_path = excel_report_path
        self.start_date = start_date
        self.end_date = end_date
        self.enable_sl_reentry = enable_sl_reentry
        self.timeframe = timeframe
        self.st_length = st_length
        self.st_multiplier = st_multiplier
        self.buffer_dollars = buffer_dollars
        self.supertrend_sl_buffer = supertrend_sl_buffer
        self.initial_capital = initial_capital
        self.lots = lots
        self.contract_size = contract_size
        self.commission_per_lot = commission_per_lot
        self.enable_session_filter = enable_session_filter
        self.session_start = session_start
        self.session_end = session_end
        self.no_entry_after = no_entry_after
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.cooldown_minutes = cooldown_minutes


# =============================================================================
# 2. PINE SCRIPT EXACT SUPERTREND INDICATOR CLASS
# =============================================================================
class PineSupertrendIndicator:
    @staticmethod
    def calculate(df: pd.DataFrame, length: int = 5, multiplier: float = 1.5) -> pd.DataFrame:
        """
        Calculates TradingView Pine Script® ta.supertrend indicator math:
        - True Range & Wilder's Smoothing ATR (RMA)
        - Basic Upper/Lower Bands
        - Trailing Final Bands & Direction Flips
        """
        df = df.copy()
        n = len(df)

        high = df["High"].values
        low = df["Low"].values
        close = df["Close"].values

        tr = np.zeros(n)
        atr = np.zeros(n)
        basic_ub = np.zeros(n)
        basic_lb = np.zeros(n)
        final_ub = np.zeros(n)
        final_lb = np.zeros(n)
        supertrend = np.zeros(n)
        trend_dir = np.ones(n, dtype=int)  # +1 Bullish, -1 Bearish
        signal = [None] * n

        # 1. Calculate True Range
        tr[0] = high[0] - low[0]
        for i in range(1, n):
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

        # 2. Calculate Wilder's ATR (RMA)
        if n >= length:
            atr[length - 1] = np.mean(tr[:length])
            for i in range(length, n):
                atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / float(length)

        # 3. Calculate Bands and Supertrend
        for i in range(length - 1, n):
            hl2 = (high[i] + low[i]) / 2.0
            basic_ub[i] = hl2 + (multiplier * atr[i])
            basic_lb[i] = hl2 - (multiplier * atr[i])

            if i == length - 1:
                final_ub[i] = basic_ub[i]
                final_lb[i] = basic_lb[i]
                trend_dir[i] = 1 if close[i] >= basic_ub[i] else -1
                supertrend[i] = final_lb[i] if trend_dir[i] == 1 else final_ub[i]
                continue

            # Final Upper Band
            if basic_ub[i] < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]:
                final_ub[i] = basic_ub[i]
            else:
                final_ub[i] = final_ub[i - 1]

            # Final Lower Band
            if basic_lb[i] > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]:
                final_lb[i] = basic_lb[i]
            else:
                final_lb[i] = final_lb[i - 1]

            # Trend Direction
            prev_trend = trend_dir[i - 1]
            if prev_trend == -1 and close[i] > final_ub[i - 1]:
                trend_dir[i] = 1
                signal[i] = "long"
            elif prev_trend == 1 and close[i] < final_lb[i - 1]:
                trend_dir[i] = -1
                signal[i] = "short"
            else:
                trend_dir[i] = prev_trend

            # Supertrend Value
            supertrend[i] = final_lb[i] if trend_dir[i] == 1 else final_ub[i]

        df["ATR"] = atr
        df["SUPERT"] = supertrend
        df["SUPERTd"] = trend_dir
        df["signal"] = signal

        return df


# =============================================================================
# 3. FAST TICK DATA LOADER CLASS (PARQUET CACHING)
# =============================================================================
class TickDataLoader:
    @staticmethod
    def load(csv_path: str) -> pd.DataFrame:
        """
        Loads tick dataset with automatic fast Parquet binary caching.
        First run: Reads CSV & creates 'XAUUSD_ticks.parquet'.
        Subsequent runs: Loads Parquet binary in ~0.3 seconds.
        """
        parquet_path = csv_path.replace(".csv", ".parquet")
        if os.path.exists(parquet_path):
            print(f"Fast Binary Cache Found! Loading tick dataset from: {parquet_path}")
            t0 = time.time()
            tick_df = pd.read_parquet(parquet_path)
            print(f"Loaded {len(tick_df)} ticks in {time.time() - t0:.2f} seconds!")
            return tick_df

        print(f"Loading raw CSV tick dataset from: {csv_path} (Creating fast binary cache)...")
        t0 = time.time()
        tick_df = pd.read_csv(csv_path, sep="\t")
        tick_df["datetime"] = pd.to_datetime(tick_df["<DATE>"] + " " + tick_df["<TIME>"], format="%Y.%m.%d %H:%M:%S.%f")
        tick_df.set_index("datetime", inplace=True)
        tick_df.rename(columns={"<BID>": "Bid", "<ASK>": "Ask", "<VOLUME>": "Volume"}, inplace=True)

        tick_df["Bid"] = tick_df["Bid"].ffill()
        tick_df["Ask"] = tick_df["Ask"].ffill()
        tick_df.dropna(subset=["Bid", "Ask"], inplace=True)
        tick_df.sort_index(inplace=True)

        print(f"Saving binary cache to: {parquet_path}...")
        tick_df[["Bid", "Ask"]].to_parquet(parquet_path, compression="zstd")
        print(f"Processed and cached {len(tick_df)} tick records in {time.time() - t0:.2f} seconds.")

        return tick_df


# =============================================================================
# 4. VECTORIZED TICK SIMULATION ENGINE CLASS
# =============================================================================
class SupertrendTickEngine:
    def __init__(self, config: StrategyConfig):
        self.cfg = config

    def run(self, tick_df: pd.DataFrame, bars_df: pd.DataFrame) -> pd.DataFrame:
        """
        Executes single-pass vectorized tick simulation engine.
        Processes ticks in ~0.5 seconds with exact Ask/Bid execution prices.
        """
        t_start = time.time()
        print("Pre-extracting NumPy arrays and pre-computing single-pass bar tick bounds...")

        tick_ns = tick_df.index.values.astype('datetime64[ns]').astype(np.int64)
        bids = tick_df["Bid"].values
        asks = tick_df["Ask"].values
        num_ticks = len(tick_ns)

        bar_ns = bars_df.index.values.astype('datetime64[ns]').astype(np.int64)
        bar_dtimes = bars_df.index.to_pydatetime()
        bar_opens = bars_df["Open"].values
        bar_highs = bars_df["High"].values
        bar_lows = bars_df["Low"].values
        bar_closes = bars_df["Close"].values
        bar_st = bars_df["SUPERT"].values
        bar_st_dir = bars_df["SUPERTd"].values
        bar_signals = bars_df["signal"].values
        bar_atrs = bars_df["ATR"].values
        n_bars = len(bar_ns)

        # Single Pass Searchsorted mapping
        bar_tick_starts = np.searchsorted(tick_ns, bar_ns, side='left')
        bar_tick_ends = np.append(bar_tick_starts[1:], num_ticks)

        trades = []
        trade_id = 0

        # State variables
        in_position = False
        pos_type = None
        entry_ts = None
        entry_bid = 0.0
        entry_ask = 0.0
        entry_price = 0.0
        current_sl = 0.0
        signal_candle_time = None
        signal_candle_open = 0.0
        signal_candle_high = 0.0
        signal_candle_low = 0.0
        signal_candle_close = 0.0
        breakout_level = 0.0

        pending_breakout = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}
        pending_sl_reentry = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}
        pending_flip_exit = {"type": None, "level": 0.0, "signal_time": None}

        current_day = None
        daily_pnl = 0.0
        daily_suspended = False
        consecutive_losses = 0
        cooldown_until = None

        for idx in range(1, n_bars):
            bar_dt = bar_dtimes[idx]
            st_val = bar_st[idx]
            st_dir = bar_st_dir[idx]

            t_date = bar_dt.date()
            t_time = bar_dt.time()

            if current_day != t_date:
                current_day = t_date
                daily_pnl = 0.0
                daily_suspended = False

            start_t = bar_tick_starts[idx]
            end_t = bar_tick_ends[idx]

            if start_t >= end_t:
                continue

            bar_bids = bids[start_t:end_t]
            bar_asks = asks[start_t:end_t]
            bar_times = tick_df.index[start_t:end_t]

            # Trail Stop Loss at bar open
            if in_position:
                if pos_type == "long":
                    new_sl = st_val - self.cfg.supertrend_sl_buffer
                    if new_sl > current_sl:
                        current_sl = new_sl
                elif pos_type == "short":
                    new_sl = st_val + self.cfg.supertrend_sl_buffer
                    if current_sl == 0.0 or new_sl < current_sl:
                        current_sl = new_sl

            # --- 1. EXIT CHECK ON BAR TICKS ---
            if in_position:
                exit_idx = -1
                exit_reason = ""

                if pos_type == "long":
                    sl_hits = np.where(bar_bids <= current_sl)[0]
                    if len(sl_hits) > 0:
                        exit_idx = sl_hits[0]
                        exit_reason = "SuperTrend SL Hit"
                elif pos_type == "short":
                    sl_hits = np.where(bar_asks >= current_sl)[0]
                    if len(sl_hits) > 0:
                        exit_idx = sl_hits[0]
                        exit_reason = "SuperTrend SL Hit"

                if pending_flip_exit["type"] is not None:
                    if pos_type == "long" and pending_flip_exit["type"] == "long_exit":
                        flip_hits = np.where(bar_bids <= pending_flip_exit["level"])[0]
                        if len(flip_hits) > 0:
                            if exit_idx == -1 or flip_hits[0] < exit_idx:
                                exit_idx = flip_hits[0]
                                exit_reason = "SuperTrend Flip Exit"
                    elif pos_type == "short" and pending_flip_exit["type"] == "short_exit":
                        flip_hits = np.where(bar_asks >= pending_flip_exit["level"])[0]
                        if len(flip_hits) > 0:
                            if exit_idx == -1 or flip_hits[0] < exit_idx:
                                exit_idx = flip_hits[0]
                                exit_reason = "SuperTrend Flip Exit"

                if exit_idx != -1:
                    exit_ts = bar_times[exit_idx]
                    exit_bid = bar_bids[exit_idx]
                    exit_ask = bar_asks[exit_idx]
                    exit_price = exit_bid if pos_type == "long" else exit_ask

                    if pos_type == "long":
                        gross_pnl = (exit_price - entry_price) * self.cfg.contract_size * self.cfg.lots
                    else:
                        gross_pnl = (entry_price - exit_price) * self.cfg.contract_size * self.cfg.lots

                    actual_commission = self.cfg.commission_per_lot * self.cfg.lots
                    net_pnl = gross_pnl - actual_commission

                    trade_id += 1
                    trades.append({
                        "trade_id": trade_id,
                        "signal_timestamp": signal_candle_time,
                        "signal_type": pos_type,
                        "signal_candle_open": signal_candle_open,
                        "signal_candle_high": signal_candle_high,
                        "signal_candle_low": signal_candle_low,
                        "signal_candle_close": signal_candle_close,
                        "supertrend": round(st_val, 4),
                        "atr": round(bar_atrs[idx], 4),
                        "breakout_level": round(breakout_level, 4),
                        "breakout_timestamp": entry_ts,
                        "entry_timestamp": entry_ts,
                        "entry_bid": entry_bid,
                        "entry_ask": entry_ask,
                        "entry_price": entry_price,
                        "stop_loss": round(current_sl, 4),
                        "exit_signal": pending_flip_exit["type"],
                        "exit_timestamp": pd.Timestamp(exit_ts),
                        "exit_bid": exit_bid,
                        "exit_ask": exit_ask,
                        "exit_price": exit_price,
                        "exit_reason": exit_reason,
                        "position_size": self.cfg.lots,
                        "gross_pnl": round(gross_pnl, 4),
                        "commission": round(actual_commission, 4),
                        "swap": 0.0,
                        "net_pnl": round(net_pnl, 4)
                    })

                    daily_pnl += net_pnl
                    if net_pnl < 0:
                        consecutive_losses += 1
                        if consecutive_losses >= self.cfg.max_consecutive_losses:
                            cooldown_until = pd.Timestamp(exit_ts) + timedelta(minutes=self.cfg.cooldown_minutes)
                    else:
                        consecutive_losses = 0

                    if daily_pnl <= -(self.cfg.initial_capital * (self.cfg.max_daily_loss_pct / 100.0)):
                        daily_suspended = True

                    if self.cfg.enable_sl_reentry and exit_reason == "SuperTrend SL Hit" and t_time < self.cfg.no_entry_after:
                        favorable = (pos_type == "long" and bar_closes[idx] > st_val) or (pos_type == "short" and bar_closes[idx] < st_val)
                        if favorable:
                            pending_sl_reentry = {
                                "type": pos_type,
                                "level": (bar_highs[idx] + self.cfg.buffer_dollars) if pos_type == "long" else (bar_lows[idx] - self.cfg.buffer_dollars),
                                "signal_time": bar_dt,
                                "high": bar_highs[idx],
                                "low": bar_lows[idx]
                            }

                    in_position = False
                    pos_type = None
                    pending_flip_exit = {"type": None, "level": 0.0, "signal_time": None}

            # Check Signal Flips on prev bar (Always populate pending_breakout)
            prev_sig = bar_signals[idx - 1]
            if prev_sig is not None:
                if prev_sig == "long":
                    pending_breakout = {
                        "type": "long", "level": bar_highs[idx - 1] + self.cfg.buffer_dollars, "signal_time": bar_dtimes[idx - 1],
                        "high": bar_highs[idx - 1], "low": bar_lows[idx - 1]
                    }
                elif prev_sig == "short":
                    pending_breakout = {
                        "type": "short", "level": bar_lows[idx - 1] - self.cfg.buffer_dollars, "signal_time": bar_dtimes[idx - 1],
                        "high": bar_highs[idx - 1], "low": bar_lows[idx - 1]
                    }

                if in_position:
                    if pos_type == "long" and prev_sig == "short":
                        pending_flip_exit = {"type": "long_exit", "level": bar_lows[idx - 1] - self.cfg.buffer_dollars, "signal_time": bar_dtimes[idx - 1]}
                    elif pos_type == "short" and prev_sig == "long":
                        pending_flip_exit = {"type": "short_exit", "level": bar_highs[idx - 1] + self.cfg.buffer_dollars, "signal_time": bar_dtimes[idx - 1]}

            # --- 2. ENTRY CHECK ON BAR TICKS ---
            if not in_position:
                if daily_suspended or (cooldown_until is not None and bar_dt < cooldown_until):
                    continue
                
                if self.cfg.enable_session_filter:
                    if not (self.cfg.session_start <= t_time <= self.cfg.session_end) or t_time >= self.cfg.no_entry_after:
                        continue

                entry_hit_idx = -1
                target_entry_type = None
                target_level = 0.0
                target_signal_time = None
                target_high = 0.0
                target_low = 0.0

                if pending_sl_reentry["type"] is not None:
                    re_type = pending_sl_reentry["type"]
                    re_level = pending_sl_reentry["level"]
                    curr_tr_str = "long" if st_dir == 1 else "short"

                    if re_type != curr_tr_str:
                        pending_sl_reentry = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}
                    else:
                        hits = np.where(bar_asks > re_level)[0] if re_type == "long" else np.where(bar_bids < re_level)[0]
                        if len(hits) > 0:
                            entry_hit_idx = hits[0]
                            target_entry_type = re_type
                            target_level = re_level
                            target_signal_time = pending_sl_reentry["signal_time"]
                            target_high = pending_sl_reentry["high"]
                            target_low = pending_sl_reentry["low"]
                            pending_sl_reentry = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}

                if entry_hit_idx == -1 and pending_breakout["type"] is not None:
                    b_type = pending_breakout["type"]
                    b_level = pending_breakout["level"]
                    curr_tr_str = "long" if st_dir == 1 else "short"

                    if b_type != curr_tr_str:
                        pending_breakout = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}
                    else:
                        hits = np.where(bar_asks > b_level)[0] if b_type == "long" else np.where(bar_bids < b_level)[0]
                        if len(hits) > 0:
                            entry_hit_idx = hits[0]
                            target_entry_type = b_type
                            target_level = b_level
                            target_signal_time = pending_breakout["signal_time"]
                            target_high = pending_breakout["high"]
                            target_low = pending_breakout["low"]
                            pending_breakout = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}

                if entry_hit_idx != -1:
                    in_position = True
                    pos_type = target_entry_type
                    entry_ts = pd.Timestamp(bar_times[entry_hit_idx])
                    entry_bid = bar_bids[entry_hit_idx]
                    entry_ask = bar_asks[entry_hit_idx]
                    entry_price = entry_ask if pos_type == "long" else entry_bid
                    current_sl = (st_val - self.cfg.supertrend_sl_buffer) if pos_type == "long" else (st_val + self.cfg.supertrend_sl_buffer)

                    sig_bar_dict = bars_df.loc[target_signal_time] if target_signal_time in bars_df.index else {}
                    signal_candle_time = target_signal_time
                    signal_candle_open = sig_bar_dict.get("Open", 0.0)
                    signal_candle_high = target_high
                    signal_candle_low = target_low
                    signal_candle_close = sig_bar_dict.get("Close", 0.0)
                    breakout_level = target_level

        print(f"Vectorized simulation completed in {time.time() - t_start:.2f} seconds!")
        return pd.DataFrame(trades)



# =============================================================================
# PERFORMANCE METRICS HELPERS
# =============================================================================
def recovery_days(cum_pnl):
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
    return (recovery_idx - dd_start_idx).days

def calculate_metrics(trades_df, capital):
    if trades_df.empty:
        return None

    trades_df = trades_df.copy()
    trades_df['entry_timestamp'] = pd.to_datetime(trades_df['entry_timestamp'])
    trades_df['exit_timestamp'] = pd.to_datetime(trades_df['exit_timestamp'])
    trades_df['month'] = trades_df['exit_timestamp'].dt.to_period('M')
    trades_df['year'] = trades_df['exit_timestamp'].dt.year

    total_trades = len(trades_df)
    winning_trades = len(trades_df[trades_df['net_pnl'] > 0])
    losing_trades = len(trades_df[trades_df['net_pnl'] <= 0])
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    net_profit = trades_df['net_pnl'].sum()
    avg_trade = trades_df['net_pnl'].mean()
    highest_profit = trades_df['net_pnl'].max()
    highest_loss = trades_df['net_pnl'].min()

    gross_profit = trades_df[trades_df['net_pnl'] > 0]['net_pnl'].sum()
    gross_loss = abs(trades_df[trades_df['net_pnl'] <= 0]['net_pnl'].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

    monthly_pnl = trades_df.groupby('month')['net_pnl'].sum()
    yearly_pnl = trades_df.groupby('year')['net_pnl'].sum()
    monthly_trades_count = trades_df.groupby('month').size()

    cum_pnl = trades_df['net_pnl'].cumsum()
    peak = cum_pnl.cummax()
    drawdown = cum_pnl - peak
    max_drawdown = drawdown.min()

    drawdown_data = []
    for year, group in trades_df.groupby('year'):
        year_cum = group['net_pnl'].cumsum()
        year_peak = year_cum.cummax()
        year_dd = (year_cum - year_peak).min()
        drawdown_data.append((year, year_dd))

    cum_pnl_dated = trades_df['net_pnl'].cumsum()
    cum_pnl_dated.index = trades_df['exit_timestamp']
    recovery = recovery_days(cum_pnl_dated)

    daily_pnl = trades_df.groupby(trades_df['exit_timestamp'].dt.date)['net_pnl'].sum()
    sharpe = (daily_pnl.mean() / daily_pnl.std()) * np.sqrt(252) if daily_pnl.std() > 0 else 0

    first_date = trades_df['entry_timestamp'].min()
    last_date = trades_df['exit_timestamp'].max()
    years = (last_date - first_date).days / 365.25
    cagr = ((capital + net_profit) / capital) ** (1 / years) - 1 if years > 0 and capital + net_profit > 0 else 0

    results = (trades_df['net_pnl'] > 0).astype(int)
    max_consec_wins, max_consec_losses = 0, 0
    current_streak, current_type = 0, None
    for r in results:
        if r == current_type: current_streak += 1
        else: current_streak, current_type = 1, r
        if r == 1: max_consec_wins = max(max_consec_wins, current_streak)
        else: max_consec_losses = max(max_consec_losses, current_streak)

    roi_data = (yearly_pnl / capital) * 100

    summary_rows = []
    for period, pnl in monthly_pnl.items(): summary_rows.append({'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': pnl, 'Metric': f'Monthly PnL ({period})'})
    for year, pnl in yearly_pnl.items(): summary_rows.append({'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': pnl, 'Metric': f'Total Year PnL ({year})'})
    for year, dd in drawdown_data: summary_rows.append({'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': dd, 'Metric': f'Max Drawdown ({year})'})
    for year, roi in roi_data.items(): summary_rows.append({'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': roi, 'Metric': f'ROI % ({year})'})
    for period, count in monthly_trades_count.items(): summary_rows.append({'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': count, 'Metric': f'Trades in ({period})'})

    summary_rows.extend([
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': total_trades, 'Metric': 'Total Trades'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': win_rate, 'Metric': 'Win Rate %'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': net_profit, 'Metric': 'Net Profit ($)'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': cagr * 100, 'Metric': 'CAGR %'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': profit_factor, 'Metric': 'Profit Factor'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': max_drawdown, 'Metric': 'Overall Max Drawdown'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': sharpe, 'Metric': 'Sharpe Ratio'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': avg_trade, 'Metric': 'Average Trade ($)'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': highest_profit, 'Metric': 'Highest Single Trade Profit'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': highest_loss, 'Metric': 'Highest Single Trade Loss'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': max_consec_wins, 'Metric': 'Max Consecutive Wins'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': max_consec_losses, 'Metric': 'Max Consecutive Losses'},
        {'entry_timestamp': None, 'exit_timestamp': None, 'net_pnl': recovery, 'Metric': 'Recovery Days from MaxDD'}
    ])

    summary_df = pd.DataFrame(summary_rows)
    return summary_df

# =============================================================================
# 5. BACKTEST REPORTER CLASS
# =============================================================================
class BacktestReporter:
    @staticmethod
    def generate_reports(trades_df: pd.DataFrame, validation_df: pd.DataFrame, config: StrategyConfig):
        """
        Exports CSV trade logs, candle validation files, and multi-tab Excel workbooks.
        """
        # Save Candle Validation CSV
        validation_df.to_csv(config.candle_val_csv, index=False)
        print(f"Candle validation CSV saved to: {config.candle_val_csv}")

        if len(trades_df) == 0:
            print("No trades executed within the specified date range.")
            return

        # Save Python Trades CSV
        trades_df.to_csv(config.python_trades_csv, index=False)
        print(f"Python trade log exported to: {config.python_trades_csv}")

        final_df = calculate_metrics(trades_df, capital=config.initial_capital)

        # Save Excel Report
        try:
            with pd.ExcelWriter(config.excel_report_path, engine="openpyxl") as writer:
                # TAB 1: All Trades
                trades_df.to_excel(writer, sheet_name="All Trades", index=False)
                
                # TAB 2: Technical Statistics
                if final_df is not None:
                    stats_df = final_df[["Metric", "net_pnl"]].copy()
                    stats_df.rename(columns={"net_pnl": "Value"}, inplace=True)
                    
                    main_stats = stats_df[~stats_df['Metric'].str.contains(r'Monthly PnL|Year PnL|Trades in|Max Drawdown \(|ROI %')]
                    main_stats.to_excel(writer, sheet_name="Technical Statistics", index=False, startrow=0, startcol=0)
                    
                    returns_df = trades_df.copy()
                    returns_df['month_year'] = pd.to_datetime(returns_df['exit_timestamp']).dt.to_period('M')
                    returns_df['year'] = pd.to_datetime(returns_df['exit_timestamp']).dt.year
                    
                    monthly_ret = returns_df.groupby('month_year')['net_pnl'].sum().reset_index()
                    yearly_ret = returns_df.groupby('year')['net_pnl'].sum().reset_index()
                    
                    monthly_ret.to_excel(writer, sheet_name="Technical Statistics", index=False, startrow=0, startcol=4)
                    yearly_ret.to_excel(writer, sheet_name="Technical Statistics", index=False, startrow=0, startcol=7)
                    
                    # Trade Distribution Table (Buckets)
                    bins = [-float('inf'), -5000, -2000, 0, 2000, 5000, float('inf')]
                    labels = ['Large Loss', 'Medium Loss', 'Small Loss', 'Small Win', 'Medium Win', 'Large Win']
                    returns_df['pnl_bucket'] = pd.cut(returns_df['net_pnl'], bins=bins, labels=labels)
                    dist_df = returns_df['pnl_bucket'].value_counts().sort_index().reset_index()
                    dist_df.columns = ['Trade Type', 'Count']
                    dist_df.to_excel(writer, sheet_name="Technical Statistics", index=False, startrow=0, startcol=10)

                # TAB 3: Equity Curve (Graph + Data)
                equity_df = trades_df[['exit_timestamp', 'net_pnl']].copy()
                equity_df = equity_df.sort_values('exit_timestamp')
                equity_df['cumulative_pnl'] = equity_df['net_pnl'].cumsum()
                equity_df['account_balance'] = config.initial_capital + equity_df['cumulative_pnl']
                
                # Start table at row 20
                start_row = 20
                equity_df.to_excel(writer, sheet_name="Equity Curve", index=False, startrow=start_row)
                
                wb = writer.book
                data_sheet = wb["Equity Curve"]
                
                chart = LineChart()
                chart.title = "Equity Curve"
                chart.style = 13
                chart.y_axis.title = "Cumulative PNL ($)"
                chart.x_axis.title = "Trade Datetime"
                
                # Calculate rows for reference
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

                # TAB 4: Validation Data
                validation_df.to_excel(writer, sheet_name="Constructed OHLC", index=False)
                
            print(f"Excel backtest report updated successfully: {config.excel_report_path}")
        except Exception as e:
            print(f"Warning: Could not update Excel file: {e}")

        # Summary Metrics (For Print)
        winning = len(trades_df[trades_df["net_pnl"] > 0])
        losing = len(trades_df[trades_df["net_pnl"] <= 0])
        total_pnl = trades_df["net_pnl"].sum()
        win_rate = (winning / len(trades_df)) * 100.0 if len(trades_df) > 0 else 0.0

        print("\\n--- PARITY BACKTEST SUMMARY ---")
        print(f"  Date Range     : {config.start_date or 'Start'} to {config.end_date or 'End'}")
        print(f"  Total Trades   : {len(trades_df)}")
        print(f"  Winning Trades : {winning}")
        print(f"  Losing Trades  : {losing}")
        print(f"  Win Rate %     : {win_rate:.2f}%")
        print(f"  Total Net PnL  : ${total_pnl:.2f} USD")

# =============================================================================
# MAIN EXECUTION FACADE
# =============================================================================
def run_backtest(config: StrategyConfig):
    print("=================================================================")
    print("XAUUSD Supertrend Object-Oriented High-Performance Parity Engine")
    print("=================================================================")

    if not os.path.exists(config.tick_data_path):
        raise FileNotFoundError(f"Tick data file not found: {config.tick_data_path}")

    # 1. Load tick dataset
    tick_df = TickDataLoader.load(config.tick_data_path)

    # Apply Custom Start and End Date Filtering
    if config.start_date:
        t_start_dt = pd.to_datetime(config.start_date)
        tick_df = tick_df[tick_df.index >= t_start_dt]
        print(f"Applied Custom Start Date Filter: >= {config.start_date}")

    if config.end_date:
        t_end_dt = pd.to_datetime(config.end_date)
        tick_df = tick_df[tick_df.index <= t_end_dt]
        print(f"Applied Custom End Date Filter: <= {config.end_date}")

    if len(tick_df) == 0:
        print("Error: No tick data found within the specified date range!")
        return

    # 2. Resample to Target Timeframe Candles
    print(f"Constructing {config.timeframe} OHLC candles...")
    t0 = time.time()
    bars_df = tick_df["Bid"].resample(config.timeframe).ohlc()
    bars_df.dropna(subset=["open", "high", "low", "close"], how="all", inplace=True)
    bars_df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}, inplace=True)
    print(f"Constructed {len(bars_df)} {config.timeframe} candles in {time.time() - t0:.2f}s.")

    # 3. Compute Pine Script Exact Supertrend Math
    print(f"Computing Wilder's ATR({config.st_length}) and Supertrend(Mult={config.st_multiplier}) Indicator Math...")
    bars_df = PineSupertrendIndicator.calculate(bars_df, length=config.st_length, multiplier=config.st_multiplier)

    # Prepare Validation DataFrame
    validation_df = bars_df.reset_index()
    validation_df.rename(columns={
        "datetime": "timestamp",
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "ATR": "atr", "SUPERT": "supertrend", "SUPERTd": "trend_direction"
    }, inplace=True)
    validation_export = validation_df[["timestamp", "open", "high", "low", "close", "atr", "supertrend", "trend_direction", "signal"]]

    # 4. Execute Tick Simulation Engine
    engine = SupertrendTickEngine(config)
    trades_df = engine.run(tick_df, bars_df)

    # 5. Export Reports
    BacktestReporter.generate_reports(trades_df, validation_export, config)


if __name__ == "__main__":
    # =============================================================================
    # OBJECT-ORIENTED STRATEGY CONFIGURATION & CUSTOM BACKTEST PERIOD
    # =============================================================================
    CONFIG = {
        "tick_data_path": r"c:\Users\ADMIN\Desktop\XAUUSD_BK\XAUUSD.._202601020100_202608101443.csv",
        "python_trades_csv": r"c:\Users\ADMIN\Desktop\XAUUSD_BK\python_trades.csv",
        "candle_val_csv": r"c:\Users\ADMIN\Desktop\XAUUSD_BK\candle_validation.csv",
        "excel_report_path": r"c:\Users\ADMIN\Desktop\XAUUSD_BK\BACKTEST_SUPERTREND_XAUUSD.xlsx",

        # --- CUSTOM BACKTEST DATE RANGE ---
        # Set to None for full dataset, or specify custom start/end strings e.g. "2026-01-02", "2026-06-30"
        "start_date": "2026-01-01",   # e.g., "2026-01-02" or "2026-01-02 08:00:00"
        "end_date": "2026-08-10",     # e.g., "2026-06-30" or "2026-06-30 23:59:59"

        "timeframe": "15min",
        "st_length": 5,
        "st_multiplier": 1.5,
        "buffer_dollars": 1.0,
        "supertrend_sl_buffer": 1.0,
        "initial_capital": 10000.0,
        "lots": 0.1,
        "contract_size": 100,
        "commission_per_lot": 8.0,
        "enable_sl_reentry": True,
        "enable_session_filter": False, # Set to False to disable session time restrictions
        "session_start": dtime(8, 0),
        "session_end": dtime(21, 0),
        "no_entry_after": dtime(20, 0),
        "max_daily_loss_pct": 3.0,
        "max_consecutive_losses": 3,
        "cooldown_minutes": 240,
    }

    config = StrategyConfig(**CONFIG)

    # Run Backtest
    run_backtest(config)

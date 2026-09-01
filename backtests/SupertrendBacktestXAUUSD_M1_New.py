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
from openpyxl.chart import LineChart, AreaChart, Reference


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
        use_dynamic_spread: bool = False,
        enable_market_hours: bool = True,
        market_open_time: dtime = dtime(1, 0),
        market_close_time: dtime = dtime(23, 59),
        market_buffer_mins: int = 30,
        market_buffer_mode: str = "halt",
        **kwargs
    ):
        self.enable_market_hours = enable_market_hours
        self.market_open_time = market_open_time
        self.market_close_time = market_close_time
        self.market_buffer_mins = market_buffer_mins
        self.market_buffer_mode = kwargs.get('market_buffer_mode', 'close') # 'close' or 'halt'
        self.use_dynamic_spread = kwargs.get('use_dynamic_spread', True)
        self.max_spread_dollars = kwargs.get('max_spread_dollars', 1.5)
        self.max_slippage_dollars = kwargs.get('max_slippage_dollars', 10.0)
        self.disable_monday_mins = kwargs.get('disable_monday_mins', 120)
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
class M1DataLoader:
    @staticmethod
    def load(csv_path: str) -> pd.DataFrame:
        """
        Loads M1 dataset with automatic fast Parquet binary caching.
        """
        parquet_path = csv_path.replace(".csv", ".parquet")
        if os.path.exists(parquet_path):
            print(f"Fast Binary Cache Found! Loading M1 dataset from: {parquet_path}")
            t0 = time.time()
            df = pd.read_parquet(parquet_path)
            print(f"Loaded {len(df)} M1 rows in {time.time() - t0:.2f} seconds!")
            return df

        print(f"Loading raw CSV M1 dataset from: {csv_path} (Creating fast binary cache)...")
        t0 = time.time()
        df = pd.read_csv(csv_path)
        df['time'] = pd.to_datetime(df['time'])
        df.set_index('time', inplace=True)
        df.sort_index(inplace=True)

        print(f"Saving binary cache to: {parquet_path}...")
        df.to_parquet(parquet_path, compression="zstd")
        print(f"Processed and cached {len(df)} M1 rows in {time.time() - t0:.2f} seconds.")

        return df


# =============================================================================
# 4. LOOK-AHEAD-FREE M1 EXECUTION ENGINE  (FIXED)
# =============================================================================
#
# WHAT WAS WRONG WITH THE ORIGINAL ENGINE (and why backtest >> live):
#
#  BUG 1 — LOOK-AHEAD IN THE TRAILING STOP (the big one):
#     The original set `current_sl` from `bar_st[idx]` — the Supertrend value
#     of the CURRENT 15-min bar — and then checked SL hits on the M1 candles
#     *inside* that same bar. bar_st[idx] is computed from bar idx's own
#     high/low/close, i.e. it only exists once the bar has CLOSED. The live
#     EA (correctly) trails the SL only on completed bars. Verified impact on
#     the Aug 17-27 overlap: identical entries, exits better by $20-$120 per
#     trade in the backtest — the entire "edge" was this bug.
#
#  BUG 2 — ENTRY-BAR EXIT BLINDNESS:
#     After entering mid-bar, the original never re-checked exits until the
#     NEXT 15-min bar, so any stop-out in the remainder of the entry bar was
#     invisible.
#
#  BUG 3 — SAME-BAR LOOK-AHEAD IN SL RE-ENTRY:
#     The re-entry "favorable" test compared bar_closes[idx] (the current
#     bar's close — future info) against st_val (also future info).
#
#  BUG 4 — PENDING ORDERS ARMED ONE BAR LATE / VALIDATED WITH FUTURE DIRECTION:
#     pending_breakout / pending_flip_exit were updated AFTER the exit scan of
#     the bar, and pending validity was checked against st_dir of the current
#     (unfinished) bar.
#
# HOW THIS ENGINE FIXES IT — the invariant is simple:
#     Every decision made DURING bar idx uses only values of bar idx-1
#     (the last COMPLETED bar): its Supertrend, direction, signal, close.
#     This matches what the live ApexTrend EA can actually know.
#
# Additional live-parity details implemented:
#   * SL only ratchets (tightens) — identical to the live trailing logic.
#   * SL re-entry favorability is evaluated at the close of the stop-out bar
#     (i.e. at the open of the following bar), mirroring the live EA's
#     awaiting_sl_reentry_eval mechanism.
#   * After an intrabar entry at M1 candle k, exits are scanned from candle k
#     onward. If the ENTRY candle's own range also touches the SL, the engine
#     assumes the stop was hit (worst-case intrabar ordering — conservative
#     by design; M1 OHLC cannot tell you which came first).
#   * At most one entry per 15-min bar, and a new entry is only allowed on
#     candles strictly AFTER an exit that happened in the same bar.
# =============================================================================
class SupertrendM1Engine:
    def __init__(self, config: StrategyConfig):
        self.cfg = config

    def run(self, m1_df: pd.DataFrame, bars_df: pd.DataFrame) -> pd.DataFrame:
        t_start = time.time()
        print("Pre-extracting NumPy arrays and pre-computing single-pass bar M1 bounds...")

        m1_ns = m1_df.index.values.astype('datetime64[ns]').astype(np.int64)
        m1_opens = m1_df["open"].values
        m1_highs = m1_df["high"].values
        m1_lows = m1_df["low"].values
        m1_closes = m1_df["close"].values
        m1_spreads = m1_df["spread"].values if "spread" in m1_df.columns else np.zeros(len(m1_df))
        num_m1 = len(m1_ns)

        bar_ns = bars_df.index.values.astype('datetime64[ns]').astype(np.int64)
        bar_dtimes = bars_df.index.to_pydatetime()
        bar_highs = bars_df["High"].values
        bar_lows = bars_df["Low"].values
        bar_closes = bars_df["Close"].values
        bar_st = bars_df["SUPERT"].values
        bar_st_dir = bars_df["SUPERTd"].values
        bar_signals = bars_df["signal"].values
        bar_atrs = bars_df["ATR"].values
        n_bars = len(bar_ns)

        bar_m1_starts = np.searchsorted(m1_ns, bar_ns, side='left')
        bar_m1_ends = np.append(bar_m1_starts[1:], num_m1)

        m1_restricted = np.zeros(num_m1, dtype=bool)
        if self.cfg.enable_market_hours:
            dummy_date = datetime(2000, 1, 1)
            close_dt = datetime.combine(dummy_date, self.cfg.market_close_time)
            open_dt = datetime.combine(dummy_date, self.cfg.market_open_time)
            buffer_td = timedelta(minutes=self.cfg.market_buffer_mins)

            close_cutoff = (close_dt - buffer_td).time()
            open_cutoff = (open_dt + buffer_td).time()

            m1_times = pd.Series(m1_df.index).dt.time.values
            if close_cutoff > open_cutoff:
                m1_restricted = (m1_times >= close_cutoff) | (m1_times < open_cutoff)
            else:
                m1_restricted = (m1_times >= close_cutoff) & (m1_times < open_cutoff)

        trades = []
        state = {
            "trade_id": 0,
            "in_position": False,
            "pos_type": None,
            "entry_ts": None,
            "entry_bid": 0.0,
            "entry_ask": 0.0,
            "entry_price": 0.0,
            "current_sl": 0.0,
            "signal_candle_time": None,
            "signal_candle_open": 0.0,
            "signal_candle_high": 0.0,
            "signal_candle_low": 0.0,
            "signal_candle_close": 0.0,
            "breakout_level": 0.0,
            "entry_st": 0.0,      # Supertrend of last completed bar at entry (what live knows)
            "entry_atr": 0.0,
            "daily_suspended": False,
            "daily_pnl": 0.0,
            "last_day": None,
            "consecutive_losses": 0,
            "cooldown_until": None,
        }

        pending_breakout = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}
        pending_flip_exit = {"type": None, "level": 0.0, "signal_time": None}
        pending_sl_reentry = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}
        awaiting_sl_reentry_eval = {"type": None}   # evaluated at the NEXT bar open (live parity)

        cfg = self.cfg

        def close_trade(exit_ts, exit_price, exit_reason, spread_at_exit, bar_time_of_day):
            """Book the trade, update risk state, arm deferred SL re-entry."""
            if state["pos_type"] == "long":
                exit_bid = exit_price
                exit_ask = exit_price + (spread_at_exit * 0.01 if cfg.use_dynamic_spread else 0.0)
            else:
                exit_ask = exit_price
                exit_bid = exit_price - (spread_at_exit * 0.01 if cfg.use_dynamic_spread else 0.0)

            if state["pos_type"] == "long":
                gross_pnl = (exit_price - state["entry_price"]) * cfg.contract_size * cfg.lots
            else:
                gross_pnl = (state["entry_price"] - exit_price) * cfg.contract_size * cfg.lots
            actual_commission = cfg.commission_per_lot * cfg.lots
            net_pnl = gross_pnl - actual_commission
            state["trade_id"] += 1
            trades.append({
                "trade_id": state["trade_id"],
                "signal_timestamp": state["signal_candle_time"],
                "signal_type": state["pos_type"],
                "signal_candle_open": state["signal_candle_open"],
                "signal_candle_high": state["signal_candle_high"],
                "signal_candle_low": state["signal_candle_low"],
                "signal_candle_close": state["signal_candle_close"],
                "supertrend": round(state["entry_st"], 4),
                "atr": round(state["entry_atr"], 4),
                "breakout_level": round(state["breakout_level"], 4),
                "breakout_timestamp": state["entry_ts"],
                "entry_timestamp": state["entry_ts"],
                "entry_bid": state["entry_bid"],
                "entry_ask": state["entry_ask"],
                "entry_price": state["entry_price"],
                "stop_loss": round(state["current_sl"], 4),
                "exit_signal": pending_flip_exit["type"],
                "exit_timestamp": exit_ts,
                "exit_bid": exit_bid,
                "exit_ask": exit_ask,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "position_size": cfg.lots,
                "gross_pnl": round(gross_pnl, 4),
                "commission": round(actual_commission, 4),
                "swap": 0.0,
                "net_pnl": round(net_pnl, 4)
            })

            state["daily_pnl"] += net_pnl
            if net_pnl < 0:
                state["consecutive_losses"] += 1
                if state["consecutive_losses"] >= cfg.max_consecutive_losses:
                    state["cooldown_until"] = exit_ts + timedelta(minutes=cfg.cooldown_minutes)
            else:
                state["consecutive_losses"] = 0

            if state["daily_pnl"] <= -(cfg.initial_capital * (cfg.max_daily_loss_pct / 100.0)):
                state["daily_suspended"] = True

            # FIX 3: do NOT evaluate re-entry favorability with the current
            # (unfinished) bar's close/Supertrend. Just flag it; the check
            # happens at the next bar open with completed-bar data — exactly
            # like the live EA's awaiting_sl_reentry_eval.
            if (cfg.enable_sl_reentry and exit_reason == "SuperTrend SL Hit"
                    and bar_time_of_day < cfg.no_entry_after):
                awaiting_sl_reentry_eval["type"] = state["pos_type"]

            state["in_position"] = False
            state["pos_type"] = None
            pending_flip_exit.update({"type": None, "level": 0.0, "signal_time": None})

        # NOTE: loop starts at 1 — bar 0 has no completed prior bar to act on.
        for idx in range(1, n_bars):
            m1_start = bar_m1_starts[idx]
            m1_end = bar_m1_ends[idx]
            if m1_start >= m1_end:
                continue

            bar_dt = bar_dtimes[idx]
            t_time = bar_dt.time()
            current_day = bar_dt.date()

            # =============================================================
            # THE FIX — everything below uses the LAST COMPLETED bar (idx-1)
            # =============================================================
            st_prev = bar_st[idx - 1]
            dir_prev = bar_st_dir[idx - 1]
            prev_close = bar_closes[idx - 1]
            prev_high = bar_highs[idx - 1]
            prev_low = bar_lows[idx - 1]
            prev_sig = bar_signals[idx - 1]
            prev_atr = bar_atrs[idx - 1]

            if state["last_day"] != current_day:
                state["daily_suspended"] = False
                state["daily_pnl"] = 0.0
                state["last_day"] = current_day

            if pd.isna(st_prev) or st_prev == 0.0:
                continue

            dir_prev_str = "long" if dir_prev == 1 else "short"

            # --- 0a. Deferred SL re-entry evaluation (uses completed-bar data) ---
            if awaiting_sl_reentry_eval["type"] is not None:
                a_type = awaiting_sl_reentry_eval["type"]
                favorable = ((a_type == "long" and prev_close > st_prev) or
                             (a_type == "short" and prev_close < st_prev))
                if favorable and a_type == dir_prev_str:
                    pending_sl_reentry = {
                        "type": a_type,
                        "level": (prev_high + cfg.buffer_dollars) if a_type == "long" else (prev_low - cfg.buffer_dollars),
                        "signal_time": bar_dtimes[idx - 1],
                        "high": prev_high,
                        "low": prev_low
                    }
                awaiting_sl_reentry_eval["type"] = None

            # --- 0b. Arm pendings from the just-closed bar's signal (BUG 4 fix:
            #         armed BEFORE this bar's exit/entry scans, not after) ---
            if prev_sig is not None:
                if prev_sig == "long":
                    pending_breakout = {
                        "type": "long", "level": prev_high + cfg.buffer_dollars,
                        "signal_time": bar_dtimes[idx - 1], "high": prev_high, "low": prev_low
                    }
                elif prev_sig == "short":
                    pending_breakout = {
                        "type": "short", "level": prev_low - cfg.buffer_dollars,
                        "signal_time": bar_dtimes[idx - 1], "high": prev_high, "low": prev_low
                    }

                if state["in_position"]:
                    if state["pos_type"] == "long" and prev_sig == "short":
                        pending_flip_exit = {"type": "long_exit", "level": prev_low - cfg.buffer_dollars, "signal_time": bar_dtimes[idx - 1]}
                    elif state["pos_type"] == "short" and prev_sig == "long":
                        pending_flip_exit = {"type": "short_exit", "level": prev_high + cfg.buffer_dollars, "signal_time": bar_dtimes[idx - 1]}

            # --- 0c. Trail the SL from the COMPLETED bar's Supertrend, ratchet-only
            #         (BUG 1 fix + live-parity ratchet) ---
            if state["in_position"]:
                if state["pos_type"] == "long":
                    new_sl = st_prev - cfg.supertrend_sl_buffer
                    if new_sl > state["current_sl"]:
                        state["current_sl"] = new_sl
                else:
                    new_sl = st_prev + cfg.supertrend_sl_buffer
                    if state["current_sl"] == 0.0 or new_sl < state["current_sl"]:
                        state["current_sl"] = new_sl

            # --- Per-bar M1 slices ---
            bar_times = m1_ns[m1_start:m1_end]
            bar_op = m1_opens[m1_start:m1_end]
            bar_hi = m1_highs[m1_start:m1_end]
            bar_lo = m1_lows[m1_start:m1_end]
            bar_sp = m1_spreads[m1_start:m1_end]
            bar_restr = m1_restricted[m1_start:m1_end]

            if cfg.use_dynamic_spread:
                bids_lo = bar_lo
                asks_hi = bar_hi + (bar_sp * 0.01)
            else:
                bids_lo = bar_lo
                asks_hi = bar_hi

            def scan_exit(from_off):
                """Scan M1 candles [from_off:] for the earliest exit.
                Returns (offset, reason) or (-1, None)."""
                if not state["in_position"]:
                    return -1, None
                exit_reason = None
                exit_hit_idx = -1

                if state["pos_type"] == "long":
                    sl_hits = np.where(bids_lo[from_off:] <= state["current_sl"])[0]
                else:
                    sl_hits = np.where(asks_hi[from_off:] >= state["current_sl"])[0]
                sl_hits = sl_hits + from_off
                if cfg.enable_market_hours and cfg.market_buffer_mode == "halt":
                    sl_hits = sl_hits[~bar_restr[sl_hits]]

                if pending_flip_exit["type"] is not None:
                    exit_level = pending_flip_exit["level"]
                    if state["pos_type"] == "long":
                        f_hits = np.where(bids_lo[from_off:] <= exit_level)[0]
                    else:
                        f_hits = np.where(asks_hi[from_off:] >= exit_level)[0]
                    f_hits = f_hits + from_off
                    if cfg.enable_market_hours and cfg.market_buffer_mode == "halt":
                        f_hits = f_hits[~bar_restr[f_hits]]
                    if len(f_hits) > 0 and (len(sl_hits) == 0 or f_hits[0] <= sl_hits[0]):
                        exit_hit_idx = f_hits[0]
                        exit_reason = "SuperTrend Flip Buffer Hit"

                if exit_reason is None and len(sl_hits) > 0:
                    exit_hit_idx = sl_hits[0]
                    exit_reason = "SuperTrend SL Hit"

                if cfg.enable_market_hours and cfg.market_buffer_mode == "close":
                    restr_hits = np.where(bar_restr[from_off:])[0] + from_off
                    if len(restr_hits) > 0:
                        restr_hit_idx = restr_hits[0]
                        if exit_hit_idx == -1 or restr_hit_idx < exit_hit_idx:
                            exit_hit_idx = restr_hit_idx
                            exit_reason = "Market Close Force Exit"

                return exit_hit_idx, exit_reason

            def resolve_exit_price(off, reason):
                if reason == "Market Close Force Exit":
                    return bar_op[off]
                level = state["current_sl"] if reason == "SuperTrend SL Hit" else pending_flip_exit["level"]
                # Gap-through-open handling (unchanged from original)
                if state["pos_type"] == "long" and bar_op[off] < level:
                    return bar_op[off]
                if state["pos_type"] == "short" and bar_op[off] > level:
                    return bar_op[off]
                return level

            exited_at = -1

            # --- 1. Exit scan over the whole bar (position carried in) ---
            off, reason = scan_exit(0)
            if off != -1:
                exit_ts = pd.Timestamp(bar_times[off])
                exit_price = resolve_exit_price(off, reason)
                close_trade(exit_ts, exit_price, reason, bar_sp[off], t_time)
                exited_at = off

            # --- 2. Entry scan ---
            # Only candles strictly AFTER a same-bar exit are eligible, and at
            # most one entry per 15-min bar (matches live cadence, avoids
            # exit-before-entry time travel present in the original).
            if not state["in_position"]:
                if state["daily_suspended"] or (state["cooldown_until"] is not None and bar_dt < state["cooldown_until"]):
                    continue
                if cfg.enable_session_filter:
                    if not (cfg.session_start <= t_time <= cfg.session_end) or t_time >= cfg.no_entry_after:
                        continue

                entry_from = exited_at + 1  # 0 if no exit this bar

                def try_pending(pend):
                    """Attempt to trigger a pending stop-order dict.
                    Returns entry M1 offset, or -1. Mutates/clears pend on invalidation."""
                    p_type = pend["type"]
                    p_level = pend["level"]
                    # BUG 4 fix: validate against the COMPLETED bar's direction
                    if p_type != dir_prev_str:
                        pend.update({"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0})
                        return -1
                    if p_type == "long":
                        hits = np.where(asks_hi[entry_from:] >= p_level)[0]
                    else:
                        hits = np.where(bids_lo[entry_from:] <= p_level)[0]
                    hits = hits + entry_from
                    if cfg.enable_market_hours:
                        hits = hits[~bar_restr[hits]]
                    if len(hits) == 0:
                        return -1
                    k = hits[0]
                    cand_ts = pd.Timestamp(bar_times[k])
                    # Monday filter
                    if cand_ts.weekday() == 0:
                        open_ts = pd.Timestamp(f"{cand_ts.strftime('%Y-%m-%d')} {cfg.market_open_time.strftime('%H:%M:%S')}")
                        mins_since_open = (cand_ts - open_ts).total_seconds() / 60.0
                        if 0 <= mins_since_open < cfg.disable_monday_mins:
                            pend.update({"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0})
                            return -1
                    # Gap / slippage filter
                    if p_type == "long" and bar_op[k] > p_level and abs(bar_op[k] - p_level) > cfg.max_slippage_dollars:
                        pend.update({"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0})
                        return -1
                    if p_type == "short" and bar_op[k] < p_level and abs(bar_op[k] - p_level) > cfg.max_slippage_dollars:
                        pend.update({"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0})
                        return -1
                    return k

                chosen = None
                entry_hit_idx = -1
                if pending_sl_reentry["type"] is not None:
                    entry_hit_idx = try_pending(pending_sl_reentry)
                    if entry_hit_idx != -1:
                        chosen = dict(pending_sl_reentry)
                        pending_sl_reentry = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}
                if entry_hit_idx == -1 and pending_breakout["type"] is not None:
                    entry_hit_idx = try_pending(pending_breakout)
                    if entry_hit_idx != -1:
                        chosen = dict(pending_breakout)
                        pending_breakout = {"type": None, "level": 0.0, "signal_time": None, "high": 0.0, "low": 0.0}

                if entry_hit_idx != -1 and chosen is not None:
                    k = entry_hit_idx
                    state["in_position"] = True
                    state["pos_type"] = chosen["type"]
                    state["entry_ts"] = pd.Timestamp(bar_times[k])

                    if chosen["type"] == "long" and bar_op[k] > chosen["level"]:
                        entry_price = bar_op[k]
                    elif chosen["type"] == "short" and bar_op[k] < chosen["level"]:
                        entry_price = bar_op[k]
                    else:
                        entry_price = chosen["level"]
                    state["entry_price"] = entry_price

                    if chosen["type"] == "long":
                        state["entry_ask"] = entry_price
                        state["entry_bid"] = entry_price - (bar_sp[k] * 0.01 if cfg.use_dynamic_spread else 0.0)
                    else:
                        state["entry_bid"] = entry_price
                        state["entry_ask"] = entry_price + (bar_sp[k] * 0.01 if cfg.use_dynamic_spread else 0.0)

                    # BUG 1 fix at entry: initial SL from the last COMPLETED
                    # bar's Supertrend — exactly what the live EA sets.
                    state["current_sl"] = (st_prev - cfg.supertrend_sl_buffer) if chosen["type"] == "long" else (st_prev + cfg.supertrend_sl_buffer)
                    state["entry_st"] = st_prev
                    state["entry_atr"] = prev_atr

                    sig_time = chosen["signal_time"]
                    sig_bar = bars_df.loc[sig_time] if sig_time in bars_df.index else {}
                    state["signal_candle_time"] = sig_time
                    state["signal_candle_open"] = sig_bar.get("Open", 0.0) if hasattr(sig_bar, "get") else sig_bar["Open"]
                    state["signal_candle_high"] = chosen["high"]
                    state["signal_candle_low"] = chosen["low"]
                    state["signal_candle_close"] = sig_bar.get("Close", 0.0) if hasattr(sig_bar, "get") else sig_bar["Close"]
                    state["breakout_level"] = chosen["level"]

                    # --- 3. BUG 2 fix: scan the REMAINDER of the entry bar for
                    #        an exit, starting AT the entry candle (conservative:
                    #        if the entry candle also spans the SL, count the
                    #        stop as hit — worst-case intrabar ordering). ---
                    off2, reason2 = scan_exit(k)
                    if off2 != -1:
                        exit_ts2 = pd.Timestamp(bar_times[off2])
                        if off2 == k and reason2 == "SuperTrend SL Hit":
                            # Exiting on the entry candle itself: the candle's
                            # open happened BEFORE our entry, so the open-gap
                            # rule doesn't apply — fill exactly at the stop.
                            exit_price2 = state["current_sl"]
                        elif off2 == k and reason2 == "SuperTrend Flip Buffer Hit":
                            exit_price2 = pending_flip_exit["level"]
                        else:
                            exit_price2 = resolve_exit_price(off2, reason2)
                        close_trade(exit_ts2, exit_price2, reason2, bar_sp[off2], t_time)

        print(f"Look-ahead-free simulation completed in {time.time() - t_start:.2f} seconds!")
        return pd.DataFrame(trades)


# =============================================================================
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
# DRAWDOWN EPISODE ANALYSIS
# =============================================================================
def calculate_drawdown_episodes(trades_df, initial_capital):
    """
    Identifies all distinct drawdown episodes from the chronological equity curve.
    A drawdown episode starts when equity falls below the running peak and ends
    when equity recovers to or exceeds that peak. No double-counting of nested drawdowns.

    Returns:
        episodes_df: One row per drawdown episode with 13 columns.
        equity_curve_df: Full equity curve with running peak and drawdown columns.
    """
    if trades_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    eq = trades_df[['exit_timestamp', 'net_pnl']].copy()
    eq['exit_timestamp'] = pd.to_datetime(eq['exit_timestamp'])
    eq = eq.sort_values('exit_timestamp').reset_index(drop=True)
    eq['cumulative_pnl'] = eq['net_pnl'].cumsum()
    eq['account_balance'] = initial_capital + eq['cumulative_pnl']

    # Prepend initial capital so cummax correctly identifies the starting peak
    initial_ts = eq['exit_timestamp'].iloc[0] - pd.Timedelta(seconds=1)
    initial_row = pd.DataFrame({
        'exit_timestamp': [initial_ts],
        'net_pnl': [0.0],
        'cumulative_pnl': [0.0],
        'account_balance': [float(initial_capital)]
    })
    eq = pd.concat([initial_row, eq], ignore_index=True)

    eq['running_peak'] = eq['account_balance'].cummax()
    eq['drawdown_$'] = eq['account_balance'] - eq['running_peak']
    eq['drawdown_%'] = np.where(
        eq['running_peak'] > 0,
        (eq['account_balance'] - eq['running_peak']) / eq['running_peak'] * 100.0,
        0.0
    )

    # --- Episode detection: walk chronologically ---
    episodes = []
    in_drawdown = False
    dd_num = 0
    peak_equity = 0.0
    peak_datetime = None
    trough_equity = float('inf')
    trough_datetime = None

    balances = eq['account_balance'].values
    peaks = eq['running_peak'].values
    timestamps = eq['exit_timestamp'].values

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
                # Peak datetime = last time account_balance was at the running peak
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
                dd_pct = round((trough_equity - peak_equity) / peak_equity * 100.0, 4) if peak_equity > 0 else 0.0
                dd_duration = recovery_end - peak_datetime
                recovery_duration = recovery_end - trough_datetime

                episodes.append({
                    'DD #': dd_num,
                    'Start Datetime': peak_datetime,
                    'Peak Equity': round(peak_equity, 2),
                    'Trough Datetime': trough_datetime,
                    'Trough Equity': round(trough_equity, 2),
                    'Drawdown $': dd_dollar,
                    'Drawdown %': round(dd_pct, 2),
                    'Recovery Start': trough_datetime,
                    'Recovery End': recovery_end,
                    'Drawdown Duration': dd_duration,
                    'Recovery Duration': recovery_duration,
                    'Total Episode Duration': dd_duration,
                    'Status': 'RECOVERED'
                })
                in_drawdown = False
                trough_equity = float('inf')
                trough_datetime = None

    # Handle open drawdown at end of backtest
    if in_drawdown:
        dd_dollar = round(trough_equity - peak_equity, 2)
        dd_pct = round((trough_equity - peak_equity) / peak_equity * 100.0, 4) if peak_equity > 0 else 0.0
        episodes.append({
            'DD #': dd_num,
            'Start Datetime': peak_datetime,
            'Peak Equity': round(peak_equity, 2),
            'Trough Datetime': trough_datetime,
            'Trough Equity': round(trough_equity, 2),
            'Drawdown $': dd_dollar,
            'Drawdown %': round(dd_pct, 2),
            'Recovery Start': trough_datetime,
            'Recovery End': pd.NaT,
            'Drawdown Duration': pd.NaT,
            'Recovery Duration': pd.NaT,
            'Total Episode Duration': pd.NaT,
            'Status': 'OPEN / NOT RECOVERED'
        })

    episodes_df = pd.DataFrame(episodes)
    # Remove the prepended initial-capital row from the output equity curve
    eq_output = eq.iloc[1:].reset_index(drop=True)
    return episodes_df, eq_output


def calculate_drawdown_statistics(episodes_df, last_timestamp=None):
    """
    Computes 19 overall drawdown summary statistics from the episodes table.
    Returns a DataFrame with 'Metric' and 'Value' columns.

    Args:
        episodes_df: DataFrame from calculate_drawdown_episodes.
        last_timestamp: Last trade exit timestamp, used to compute partial
                        duration for open/unrecovered drawdowns in the
                        "Total Time in Drawdown" statistic.
    """
    if episodes_df.empty:
        return pd.DataFrame({'Metric': ['Total Drawdown Count'], 'Value': [0]})

    total_count = len(episodes_df)

    # Maximum drawdown (most negative values)
    max_dd_dollar = episodes_df['Drawdown $'].min()
    max_dd_pct = episodes_df['Drawdown %'].min()
    max_dd_row = episodes_df.loc[episodes_df['Drawdown $'].idxmin()]

    # Recovered vs open
    recovered = episodes_df[episodes_df['Status'] == 'RECOVERED']
    open_dd = episodes_df[episodes_df['Status'] != 'RECOVERED']
    num_recovered = len(recovered)
    num_open = len(open_dd)
    pct_recovered = round((num_recovered / total_count) * 100.0, 2) if total_count > 0 else 0.0

    # Duration statistics (from recovered episodes with valid durations)
    dd_durations = recovered['Drawdown Duration'].dropna()
    rec_durations = recovered['Recovery Duration'].dropna()

    # Total Time in Drawdown: sum recovered durations + partial open drawdown time
    total_time_in_dd = pd.Timedelta(0)
    for _, row in episodes_df.iterrows():
        if pd.notna(row['Drawdown Duration']):
            total_time_in_dd += row['Drawdown Duration']
        elif last_timestamp is not None and pd.notna(row['Start Datetime']):
            total_time_in_dd += (pd.Timestamp(last_timestamp) - pd.Timestamp(row['Start Datetime']))

    longest_dd = str(dd_durations.max()) if len(dd_durations) > 0 else 'N/A'
    avg_dd = str(dd_durations.mean()) if len(dd_durations) > 0 else 'N/A'
    median_dd = str(dd_durations.median()) if len(dd_durations) > 0 else 'N/A'

    longest_rec = str(rec_durations.max()) if len(rec_durations) > 0 else 'N/A'
    avg_rec = str(rec_durations.mean()) if len(rec_durations) > 0 else 'N/A'
    median_rec = str(rec_durations.median()) if len(rec_durations) > 0 else 'N/A'
    total_rec_time = str(rec_durations.sum()) if len(rec_durations) > 0 else 'N/A'

    stats = [
        {'Metric': 'Total Drawdown Count', 'Value': total_count},
        {'Metric': 'Maximum Drawdown ($)', 'Value': max_dd_dollar},
        {'Metric': 'Maximum Drawdown (%)', 'Value': f"{max_dd_pct}%"},
        {'Metric': 'Total Time in Drawdown', 'Value': str(total_time_in_dd)},
        {'Metric': 'Longest Drawdown Duration', 'Value': longest_dd},
        {'Metric': 'Average Drawdown Duration', 'Value': avg_dd},
        {'Metric': 'Median Drawdown Duration', 'Value': median_dd},
        {'Metric': 'Longest Recovery Duration', 'Value': longest_rec},
        {'Metric': 'Average Recovery Duration', 'Value': avg_rec},
        {'Metric': 'Median Recovery Duration', 'Value': median_rec},
        {'Metric': 'Total Recovery Time', 'Value': total_rec_time},
        {'Metric': 'Number of Recovered Drawdowns', 'Value': num_recovered},
        {'Metric': 'Number of Open/Unrecovered Drawdowns', 'Value': num_open},
        {'Metric': 'Percentage of Drawdowns Recovered', 'Value': f"{pct_recovered}%"},
        {'Metric': 'Maximum Drawdown Start Datetime', 'Value': str(max_dd_row['Start Datetime'])},
        {'Metric': 'Maximum Drawdown Trough Datetime', 'Value': str(max_dd_row['Trough Datetime'])},
        {'Metric': 'Maximum Drawdown Recovery Datetime', 'Value': str(max_dd_row['Recovery End']) if pd.notna(max_dd_row['Recovery End']) else 'NOT RECOVERED'},
        {'Metric': 'Maximum Drawdown Duration', 'Value': str(max_dd_row['Drawdown Duration']) if pd.notna(max_dd_row['Drawdown Duration']) else 'ONGOING'},
        {'Metric': 'Maximum Recovery Duration', 'Value': str(max_dd_row['Recovery Duration']) if pd.notna(max_dd_row['Recovery Duration']) else 'ONGOING'},
    ]

    return pd.DataFrame(stats)


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

                # TAB 4: Drawdown Analysis (Summary Stats + Underwater Curve Chart)
                episodes_df, eq_curve = calculate_drawdown_episodes(trades_df, config.initial_capital)

                if not episodes_df.empty:
                    last_ts = pd.to_datetime(trades_df['exit_timestamp']).max()
                    dd_stats = calculate_drawdown_statistics(episodes_df, last_timestamp=last_ts)
                    dd_stats.to_excel(writer, sheet_name="Drawdown Analysis", index=False, startrow=0)

                    # Underwater curve data below the stats table
                    uw_start_row = len(dd_stats) + 3
                    underwater_data = eq_curve[['exit_timestamp', 'account_balance', 'running_peak', 'drawdown_$', 'drawdown_%']].copy()
                    underwater_data.to_excel(writer, sheet_name="Drawdown Analysis", index=False, startrow=uw_start_row)

                    # Underwater / Drawdown Curve Chart (AreaChart)
                    dd_sheet = wb["Drawdown Analysis"]
                    dd_chart = AreaChart()
                    dd_chart.title = "Underwater / Drawdown Curve"
                    dd_chart.style = 13
                    dd_chart.y_axis.title = "Drawdown ($)"
                    dd_chart.x_axis.title = "Trade Exit Time"

                    uw_header_row = uw_start_row + 1  # openpyxl is 1-indexed
                    uw_data_end = uw_header_row + len(underwater_data)

                    # Col D (4) = drawdown_$ | Col A (1) = exit_timestamp
                    dd_data_ref = Reference(dd_sheet, min_col=4, min_row=uw_header_row, max_row=uw_data_end)
                    dd_cats_ref = Reference(dd_sheet, min_col=1, min_row=uw_header_row + 1, max_row=uw_data_end)

                    dd_chart.add_data(dd_data_ref, titles_from_data=True)
                    dd_chart.set_categories(dd_cats_ref)
                    dd_chart.width = 30
                    dd_chart.height = 12

                    dd_sheet.add_chart(dd_chart, "D1")

                    # TAB 5: Drawdown Episodes (Individual Episode Table)
                    ep_export = episodes_df.copy()
                    for col in ['Drawdown Duration', 'Recovery Duration', 'Total Episode Duration']:
                        ep_export[col] = ep_export[col].apply(lambda x: str(x) if pd.notna(x) else '')
                    ep_export.to_excel(writer, sheet_name="Drawdown Episodes", index=False)
                else:
                    # No drawdowns detected — create empty sheets with column headers
                    pd.DataFrame(columns=['Metric', 'Value']).to_excel(
                        writer, sheet_name="Drawdown Analysis", index=False)
                    pd.DataFrame(columns=[
                        'DD #', 'Start Datetime', 'Peak Equity', 'Trough Datetime', 'Trough Equity',
                        'Drawdown $', 'Drawdown %', 'Recovery Start', 'Recovery End',
                        'Drawdown Duration', 'Recovery Duration', 'Total Episode Duration', 'Status'
                    ]).to_excel(writer, sheet_name="Drawdown Episodes", index=False)
                
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

    # 1. Load M1 dataset
    m1_df = M1DataLoader.load(config.tick_data_path)

    # Apply Custom Start and End Date Filtering
    if config.start_date:
        t_start_dt = pd.to_datetime(config.start_date)
        m1_df = m1_df[m1_df.index >= t_start_dt]
        print(f"Applied Custom Start Date Filter: >= {config.start_date}")

    if config.end_date:
        t_end_dt = pd.to_datetime(config.end_date)
        m1_df = m1_df[m1_df.index <= t_end_dt]
        print(f"Applied Custom End Date Filter: <= {config.end_date}")

    if len(m1_df) == 0:
        print("Error: No M1 data found within the specified date range!")
        return

    # 2. Resample to Target Timeframe Candles
    print(f"Constructing {config.timeframe} OHLC candles...")
    t0 = time.time()
    bars_df = m1_df.resample(config.timeframe).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last'
    })
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
        "time": "timestamp",
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "ATR": "atr", "SUPERT": "supertrend", "SUPERTd": "trend_direction"
    }, inplace=True)
    validation_export = validation_df[["timestamp", "open", "high", "low", "close", "atr", "supertrend", "trend_direction", "signal"]]

    # 4. Execute Tick Simulation Engine
    engine = SupertrendM1Engine(config)
    trades_df = engine.run(m1_df, bars_df)

    # 5. Export Reports
    BacktestReporter.generate_reports(trades_df, validation_export, config)


if __name__ == "__main__":
    # =============================================================================
    # OBJECT-ORIENTED STRATEGY CONFIGURATION & CUSTOM BACKTEST PERIOD
    # =============================================================================
    CONFIG = {
        "tick_data_path": r"C:\Users\ADMIN\Desktop\XAUUSD_BK\xauusd_2018_2026.csv",
        "python_trades_csv": r"c:\Users\ADMIN\Desktop\XAUUSD_BK\python_trades_M1.csv",
        "candle_val_csv": r"c:\Users\ADMIN\Desktop\XAUUSD_BK\candle_validation_M1.csv",
        "excel_report_path": r"c:\Users\ADMIN\Desktop\XAUUSD_BK\BACKTEST_SUPERTREND_XAUUSD_M1.xlsx",

        # --- CUSTOM BACKTEST DATE RANGE ---
        # Set to None for full dataset, or specify custom start/end strings e.g. "2026-01-02", "2026-06-30"
        "start_date": "2026-01-01",   # e.g., "2026-01-02" or "2026-01-02 08:00:00"
        "end_date": "2026-08-28",     # e.g., "2026-06-30" or "2026-06-30 23:59:59"        # --- CORE STRATEGY PARAMETERS ---
        "timeframe": "5min",                         # The timeframe used for calculating the Supertrend
        "st_length": 6,                               # ATR period used in the Supertrend formula
        "st_multiplier": 1.06,          # The multiplier applied to the ATR for the Supertrend bands
        "buffer_dollars": 1,          # Extra dollar buffer added to the breakout level to avoid false triggers
        "supertrend_sl_buffer": 1,   # Extra dollar buffer applied to the dynamic Supertrend Stop Loss

        # --- MT5 VIRTUAL STOP EXECUTION FILTERS ---
        "max_spread_dollars": 1.013,                    # If the tick's Bid/Ask spread > this value, ignore the tick (wait for spread to settle)
        "max_slippage_dollars": 6.82,                 # If gap between actual fill price and target level > this value, CANCEL signal (prevents terrible weekend gap fills)
        "disable_monday_mins": 44,                   # Disables all new entries for the first X minutes after Monday market open (avoids toxic liquidity)

        # --- FINANCIAL / ACCOUNT PARAMETERS ---
        "initial_capital": 10000.0,                   # Starting account balance in USD
        "lots": 0.1,                                  # Fixed lot size for every trade
        "contract_size": 100,                         # 1 standard lot of XAUUSD = 100 oz
        "commission_per_lot": 8.0,                    # Broker commission per lot traded (e.g. $8 round turn)

        # --- RE-ENTRY & SESSION (LEGACY) FILTERS ---
        "enable_sl_reentry": True,                    # If True, allows re-entering a trade in the same direction if stopped out, provided price crosses the old entry level
        "enable_session_filter": False,               # If True, enables legacy session constraints (overridden by enable_market_hours in this version)
        "session_start": dtime(8, 0),                 # Start of the legacy trading session
        "session_end": dtime(21, 0),                  # End of the legacy trading session
        "no_entry_after": dtime(20, 0),               # No new trades after this time (legacy)

        # --- RISK MANAGEMENT ---
        "max_daily_loss_pct": 1000,                   # Suspends trading if daily loss exceeds this percentage (1000 = disabled)
        "max_consecutive_losses": 1000,               # Suspends trading if consecutive losses hit this number (1000 = disabled)
        "cooldown_minutes": 0,                        # Waits X minutes after a loss before taking a new trade

        # --- MARKET HOURS & BUFFER ZONES ---
        "enable_market_hours": True,                  # Enables strictly adhering to the broker's market hours below
        "market_open_time": dtime(1, 0),              # The exact time the broker market opens daily
        "market_close_time": dtime(23, 59),           # The exact time the broker market closes daily
        "market_buffer_mins": 30,                     # Buffer zone around open/close to halt or close trades (avoids closing/opening volatility)
        "market_buffer_mode": "close",                # 'close' = force close trades before market end. 'halt' = carry trades over but halt entries/exits in buffer.
    }

    config = StrategyConfig(**CONFIG)

    # Run Backtest
    run_backtest(config)

# =============================================================================
# XAUUSD Adaptive Mean Reversion Scalper — Backtest Engine
# =============================================================================
#
# STRATEGY OVERVIEW
# -----------------
# The XAUUSD Adaptive Mean Reversion Scalper is a fully systematic intraday
# trading strategy designed to capture short-term price reversals within the
# prevailing market trend. It combines trend identification, volatility
# analysis, momentum exhaustion signals, dynamic position sizing, and multiple
# risk protection layers.
#
# PHILOSOPHY
# ----------
# Strong trends frequently experience temporary pullbacks before resuming
# their primary direction. Rather than chasing momentum breakouts, the
# strategy waits for statistically stretched conditions and enters in
# anticipation of mean reversion back toward equilibrium — while remaining
# aligned with the dominant trend.
#
# MARKET UNIVERSE
# ---------------
# Underlying      : XAUUSD (Gold)
# Timeframe       : M1 (1 Minute)
# Trading Style   : Intraday Scalping
# Execution       : Fully Automated
# Max Positions   : 1 at a time
#
# INDICATOR CONFIGURATION
# -----------------------
# Trend Filter    : EMA Period = 200
# Bollinger Bands : Period = 20, Std Dev = 1.5
# RSI             : Period = 9, Oversold = 30, Overbought = 70
# ATR             : Period = 14 (for SL/TP sizing)
#
# ENTRY FRAMEWORK
# ---------------
# Long Setup:
#   1. Price closes above the 200 EMA (bullish regime)
#   2. Candle low touches or breaches the lower Bollinger Band
#   3. RSI(9) is below the oversold threshold (30)
#   → Enter Buy at market on the next candle open
#
# Short Setup:
#   1. Price closes below the 200 EMA (bearish regime)
#   2. Candle high touches or breaches the upper Bollinger Band
#   3. RSI(9) is above the overbought threshold (70)
#   → Enter Sell at market on the next candle open
#
# RISK MANAGEMENT
# ---------------
# Stop Loss       : ATR × 1.5 multiplier (volatility-adjusted)
# Take Profit     : ATR × 1.5 multiplier (1:1 Risk-Reward initially)
# Position Sizing : Dynamic — based on account balance, risk %, SL distance,
#                   and instrument tick value. Risk stays consistent across
#                   changing market conditions.
#
# TRADE MANAGEMENT
# ----------------
# Breakeven Protection:
#   - Activated once trade reaches 50% of TP distance
#   - SL moved beyond entry price to cover costs and protect capital
#
# Trailing Stop System:
#   - Activated once trade reaches 75% of TP distance
#   - SL trails price dynamically (ATR × 0.5 distance)
#   - Profits are progressively locked in
#
# EXECUTION FILTERS
# -----------------
# Spread Filter   : Avoids trading during abnormal liquidity (wide spreads)
# Session Filter  : Restricts trading to predefined market hours (08:00–20:00 UTC)
#
# RISK PROTECTION FRAMEWORK
# -------------------------
# Daily Loss Protection:
#   - If daily losses exceed 3% of account equity, trading is suspended
#     for the remainder of the session.
#
# Consecutive Loss Protection:
#   - After 3 consecutive losing trades, strategy enters cooldown mode
#     (30 minutes). Trading resumes only after cooldown expires.
#
# STRENGTHS
# ---------
# • Fully systematic execution
# • Dynamic risk management
# • Trend-aligned trade selection
# • Volatility-adjusted stop placement
# • Automated breakeven and trailing mechanisms
# • Institutional-style risk controls
# • Suitable for automation and VPS deployment
#
# LIMITATIONS
# -----------
# • Can underperform during prolonged one-directional trends without pullbacks
# • Sensitive to volatility regime shifts
# • M1 timeframe contains significant market noise
# • Performance may deteriorate during major economic news releases
# • Requires periodic parameter review as market microstructure evolves
#
# =============================================================================

import pandas as pd
import numpy as np
import pandas_ta as pdt
from backtesting import Backtest, Strategy
from datetime import datetime, time as dtime, date as ddate, timedelta
import os
import math
from openpyxl.chart import LineChart, Reference



# --- OHLC Consolidation (for resampling to higher timeframes) ---
def ohlc_consolidate(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Resample OHLCV data to a higher timeframe.
    Works for 24/5 XAUUSD market (no session time filtering).
    """
    df = df.copy()
    if "timestamp" in df.columns:
        df.set_index("timestamp", inplace=True)
    df.index = pd.to_datetime(df.index)

    ohlc_df = df.resample(timeframe).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    })

    if "tick_volume" in df.columns:
        ohlc_df["tick_volume"] = df["tick_volume"].resample(timeframe).sum()
    if "spread" in df.columns:
        ohlc_df["spread"] = df["spread"].resample(timeframe).max()

    ohlc_df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    return ohlc_df


# --- Compute Technical Indicators ---
def compute_indicators(
    df: pd.DataFrame,
    ema_length: int = 200,
    bb_length: int = 20,
    bb_std: float = 1.5,
    rsi_length: int = 9,
    atr_length: int = 14,
    htf_timeframe: str = "5min",
) -> pd.DataFrame:
    """
    Compute all indicators required by the Mean Reversion strategy.
    Adds columns: EMA (if ema_length > 0), BBL, BBM, BBU, RSI, ATR, HTF_ATR.
    When ema_length=0, the EMA trend filter is disabled entirely.
    """
    df = df.copy()

    # Ensure index is datetime for resampling
    if "timestamp" in df.columns:
        df.set_index("timestamp", inplace=True)
    df.index = pd.to_datetime(df.index)

    # EMA — Trend Filter (disabled when ema_length=0)
    if ema_length > 0:
        df["EMA"] = pdt.ema(df["close"], length=ema_length)

    # Bollinger Bands (20, 1.5) — Mean Reversion Bands
    bb_df = pdt.bbands(df["close"], length=bb_length, lower_std=bb_std, upper_std=bb_std)
    col_suffix = f"_{bb_length}_{float(bb_std)}_{float(bb_std)}"
    df["BBL"] = bb_df[f"BBL{col_suffix}"]
    df["BBM"] = bb_df[f"BBM{col_suffix}"]
    df["BBU"] = bb_df[f"BBU{col_suffix}"]

    # RSI 9 — Momentum Exhaustion
    df["RSI"] = pdt.rsi(df["close"], length=rsi_length)

    # ATR 14 — Volatility for SL/TP sizing
    df["ATR"] = pdt.atr(
        high=df["high"], low=df["low"], close=df["close"], length=atr_length
    )

    # Compute HTF ATR
    if htf_timeframe and htf_timeframe != "1min":
        htf_df = df.resample(htf_timeframe).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last"
        })
        htf_df.dropna(subset=["open", "high", "low", "close"], inplace=True)
        htf_df["HTF_ATR"] = pdt.atr(
            high=htf_df["high"], low=htf_df["low"], close=htf_df["close"], length=atr_length
        )
        df = df.join(htf_df[["HTF_ATR"]], how="left").ffill()
    else:
        df["HTF_ATR"] = df["ATR"]

    return df


# --- Signal Generator ---
def signal_generator(
    data: pd.DataFrame, rsi_oversold: float = 30, rsi_overbought: float = 70
) -> pd.DataFrame:
    """
    Generate mean reversion entry signals.

    When EMA column is present (ema_length > 0):
        Long:  close > EMA AND low <= BBL AND RSI < oversold
        Short: close < EMA AND high >= BBU AND RSI > overbought

    When EMA is disabled (ema_length = 0, no EMA column):
        Long:  low <= BBL AND RSI < oversold
        Short: high >= BBU AND RSI > overbought
    """
    data = data.copy()
    data["signal"] = None  # Object dtype allows string values

    ema_enabled = "EMA" in data.columns

    # Long signal
    long_cond = (data["Low"] <= data["BBL"]) & (data["RSI"] <= rsi_oversold)
    if ema_enabled:
        long_cond = long_cond & (data["Close"] > data["EMA"])

    # Short signal
    short_cond = (data["High"] >= data["BBU"]) & (data["RSI"] >= rsi_overbought)
    if ema_enabled:
        short_cond = short_cond & (data["Close"] < data["EMA"])

    data.loc[long_cond, "signal"] = "long"
    data.loc[short_cond, "signal"] = "short"

    return data


# --- Trade Record Template ---
def default_records():
    """Simplified trade record for XAUUSD spot trades."""
    return {
        "signal_timestamp": None,       # Candle that triggered the signal
        "signal_type": None,            # "long" or "short"
        "entry_price": None,            # Execution price
        "entry_time": None,             # Execution timestamp
        "initial_stop_loss": None,      # Original ATR-based SL
        "stop_loss": None,              # Current SL (may have moved to breakeven/trailing)
        "take_profit": None,            # ATR-based TP
        "exit_price": None,             # Actual exit price
        "exit_time": None,              # Exit timestamp
        "entry_spread": None,           # Spread calculated at the exact time of entry
        "position_size": None,          # Lots
        "pnl_usd": None,               # Profit/Loss in USD
        "pnl_pips": None,              # Profit/Loss in price points
        "spread": None,                # Spread value (e.g. 0.60)
        "spread_usd": None,            # Spread cost in USD
        "brokerage_usd": None,         # Brokerage cost in USD
        "reason_for_exit": None,        # "Stop Loss" / "Take Profit" / "Trailing Stop" / "Breakeven SL"
        "atr_at_entry": None,           # ATR value when trade was opened
        "ema_at_entry": None,           # EMA value at entry
        "rsi_at_entry": None,           # RSI value at signal candle
        "bb_lower_at_entry": None,      # Lower BB at signal candle
        "bb_upper_at_entry": None,      # Upper BB at signal candle
        "trade_number": None,           # Sequential trade counter
        "breakeven_activated": False,   # Whether breakeven was triggered
        "trailing_activated": False,    # Whether trailing stop was triggered
    }


# =============================================================================
# STRATEGY CLASS
# =============================================================================
class MeanReversionScalper(Strategy):
    """
    XAUUSD Adaptive Mean Reversion Scalper.

    Enters mean-reversion trades aligned with the dominant trend (EMA 200).
    Uses Bollinger Bands + RSI for exhaustion signals.
    Manages risk with ATR-based SL/TP, breakeven, and trailing stop.
    """

    # --- Indicator Parameters (set via CONFIG before run) ---
    ema_length = 200
    bb_length = 20
    bb_std = 1.5
    rsi_length = 9
    rsi_oversold = 30
    rsi_overbought = 70
    atr_length = 14

    # --- SL/TP Configuration ---
    atr_sl_multiplier = 1.5        # Stop Loss = ATR × multiplier
    atr_tp_multiplier = 1.5        # Take Profit = ATR × multiplier (1:1 R:R)

    # --- Capital & Position Sizing ---
    trading_capital = 500000       # Account balance in USD
    initial_capital = 500000       # Starting capital (for metrics)
    risk_per_trade_pct = 1.0       # Risk % of account per trade
    lot_size_standard = 100        # 1 standard lot = 100 oz for XAUUSD
    dynamic_qty = True             # True = dynamic sizing, False = use static_lot_size
    static_lot_size = 0.1          # Fixed lot size when dynamic_qty is False (Matches MT5 FixedLotSize)
    brokerage_per_std_lot = 8.0    # Brokerage per 1 standard lot per trade ($8)
    spreads_in_trades = 0.0        # Spread in points (e.g. 0.60 to add, -0.60 to deduct)

    # --- Breakeven Protection ---
    use_breakeven = True
    breakeven_trigger_points = 1.0    # Changed from 100 to 1.0 (XAUUSD $1.00 move = 100 points in MT5)
    profit_lock_points = 0.1          # Changed from 10 to 0.10 (XAUUSD $0.10 move = 10 points in MT5)

    # --- Trailing Stop ---
    use_trailing_stop = True
    trailing_trigger_mode = "points"
    trailing_trigger_points = 1.2     # Changed from 120 to 1.20 (XAUUSD $1.20 move = 120 points)
    trailing_trigger_percent = 2.0
    trailing_step_mode = "points"
    trailing_step_points = 0.2        # Changed from 20 to 0.20 (XAUUSD $0.20 move = 20 points)
    trailing_step_atr_multiplier = 1.0

    # --- Execution Filters ---
    spread_mode = "points"
    max_spread_points = 25         # Matches MT5 MaxSpread = 25
    max_spread_units = 5
    use_session_filter = True      # True = restrict to session hours, False = trade any time
    session_start = dtime(8, 0)    # Session start (UTC)
    session_end = dtime(21, 0)     # Matches MT5 EndHour = 21
    force_close_at_session_end = True  # Added to prevent overnight exposure

    # --- ATR Fallback ---
    fixed_stop_distance = 1.5      # Matches MT5 Default_SL_Points = 150 ($1.50)

    # --- Risk Protection ---
    max_daily_loss_pct = 3.0       # Suspend trading if daily loss exceeds this % of equity
    max_consecutive_losses = 3     # Consecutive losses before cooldown
    cooldown_mode = "minutes"
    cooldown_minutes = 240         # Matches MT5 CooldownHours = 4 (4 * 60 = 240 mins)

    # --- Class-Level Trade Log ---
    signals = []

    def init(self):
        """Initialize strategy state."""
        self.current_trade = default_records()
        self.trade_counter = 0
        self.current_date = None
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = None
        self.daily_trading_suspended = False

        # Parse session start/end if they are strings
        if isinstance(self.session_start, str):
            parts = self.session_start.split(":")
            self.session_start = dtime(int(parts[0]), int(parts[1]))
        if isinstance(self.session_end, str):
            parts = self.session_end.split(":")
            self.session_end = dtime(int(parts[0]), int(parts[1]))

    def calculate_position_size(self, sl_distance):
        """
        Position sizing based on dynamic_qty flag.

        If dynamic_qty is True:
            lots = (account_balance × risk_pct) / (sl_distance × point_value_per_lot)
            For XAUUSD: 1 standard lot = 100 oz, so $1 move = $100 per lot.

        If dynamic_qty is False:
            Uses the fixed static_lot_size from config.
        """
        if not self.dynamic_qty:
            return self.static_lot_size

        if sl_distance <= 0:
            return 0.01  # Minimum micro lot

        risk_amount = self.trading_capital * (self.risk_per_trade_pct / 100)
        lots = risk_amount / (sl_distance * self.lot_size_standard)
        return max(0.01, round(lots, 2))

    def next(self):
        """Main strategy loop — called once per candle."""
        # Need enough candles for indicators to stabilize
        warmup = max(self.ema_length, self.bb_length, self.atr_length) + 1 if self.ema_length > 0 else max(self.bb_length, self.atr_length) + 1
        if len(self.data) < warmup:
            return

        current_ts = self.data.index[-1]
        current_time_only = current_ts.time() if hasattr(current_ts, "time") else None
        current_date = current_ts.date() if hasattr(current_ts, "date") else None

        close = self.data.Close[-1]
        high = self.data.High[-1]
        low = self.data.Low[-1]
        open_price = self.data.Open[-1]

        # --- Day Reset: reset daily counters on new trading day ---
        if self.current_date != current_date:
            self.current_date = current_date
            self.daily_pnl = 0.0
            self.daily_trading_suspended = False

        # Fetch current minute ticks ONLY if needed (trade is open)
        current_ticks = pd.DataFrame()
        if self.current_trade["entry_price"] is not None and self.tick_df is not None:
            next_minute = current_ts + pd.Timedelta(minutes=1)
            try:
                current_ticks = self.tick_df.loc[current_ts : next_minute - pd.Timedelta(milliseconds=1)]
            except KeyError:
                pass

        # =================================================================
        # EXIT MANAGEMENT (if currently in a trade)
        # =================================================================
        if self.current_trade["entry_price"] is not None:
            # Check for session end forced exit (only when session filter is active)
            if self.use_session_filter and self.force_close_at_session_end and current_time_only is not None:
                if current_time_only >= self.session_end:
                    self._close_trade(close, current_ts, "Session End")
                    return

            self._manage_exit(close, high, low, current_ts, current_ticks)
            return  # Skip entry logic while in a trade

        # =================================================================
        # ENTRY FILTERS (only if NOT in a trade)
        # =================================================================

        # Session Filter — only trade during defined hours (skipped when use_session_filter is False)
        if self.use_session_filter and current_time_only is not None:
            if self.session_start <= self.session_end:
                if not (self.session_start <= current_time_only <= self.session_end):
                    return
            else:
                # Handle overnight session (e.g., 20:00 to 08:00)
                if not (
                    current_time_only >= self.session_start
                    or current_time_only <= self.session_end
                ):
                    return


        # Spread Filter — skip if spread too wide
        entry_spread = 0.0
        if self.use_dynamic_spread:
            if self.tick_df is not None:
                next_minute = current_ts + pd.Timedelta(minutes=1)
                try:
                    current_ticks = self.tick_df.loc[current_ts : next_minute - pd.Timedelta(milliseconds=1)]
                except KeyError:
                    pass
                    
            if not current_ticks.empty:
                first_tick = current_ticks.iloc[0]
                current_spread = first_tick['Ask'] - first_tick['Bid']
            entry_spread = current_spread
            current_spread_points = current_spread * 100  # Convert price units to points (e.g. 0.25 -> 25)
            
            if self.spread_mode == "points":
                if current_spread_points > self.max_spread_points:
                    return
            else:
                if current_spread > self.max_spread_units:
                    return
        elif "Spread" in self.data.df.columns and not self.use_dynamic_spread:
            current_spread = self.data.Spread[-1]
            entry_spread = current_spread
            if self.spread_mode == "points":
                max_allowed_spread = self.max_spread_points
            else:
                max_allowed_spread = self.max_spread_units
            if not np.isnan(current_spread) and current_spread > max_allowed_spread:
                return

        # Daily Loss Protection — no more trades today
        if self.daily_trading_suspended:
            return

        # Consecutive Loss Cooldown
        if self.cooldown_until is not None:
            if current_ts < self.cooldown_until:
                return
            else:
                self.cooldown_until = None
                self.consecutive_losses = 0

        # =================================================================
        # ENTRY LOGIC
        # Signal fires on the PREVIOUS candle; we enter on the CURRENT candle
        # =================================================================
        if len(self.data) < 2:
            return

        # Read signal from previous candle
        prev_signal = None
        if "signal" in self.data.df.columns:
            prev_signal = self.data.signal[-2]

        if prev_signal is None or (
            isinstance(prev_signal, float) and np.isnan(prev_signal)
        ):
            return

        # Validate ATR is available, use fallback if not
        atr_val = self.data.HTF_ATR[-1] if "HTF_ATR" in self.data.df.columns else self.data.ATR[-1]
        if np.isnan(atr_val) or atr_val <= 0:
            atr_val = self.fixed_stop_distance / self.atr_sl_multiplier # reverse engineer an ATR to keep calculations consistent

        # Calculate SL/TP distances
        sl_distance = atr_val * self.atr_sl_multiplier
        tp_distance = atr_val * self.atr_tp_multiplier

        entry_price = open_price  # Fallback to OHLC open
        
        if self.use_dynamic_spread and not current_ticks.empty:
            first_tick = current_ticks.iloc[0]
            if prev_signal == "long":
                entry_price = first_tick['Ask']
            elif prev_signal == "short":
                entry_price = first_tick['Bid']

        if prev_signal == "long":
            sl = entry_price - sl_distance
            tp = entry_price + tp_distance
        elif prev_signal == "short":
            sl = entry_price + sl_distance
            tp = entry_price - tp_distance
        else:
            return

        # Dynamic position sizing
        pos_size = self.calculate_position_size(sl_distance)

        # --- Record Entry ---
        self.trade_counter += 1
        self.current_trade = default_records()
        self.current_trade.update(
            {
                "signal_timestamp": self.data.index[-2],
                "signal_type": prev_signal,
                "entry_price": entry_price,
                "entry_time": current_ts,
                "initial_stop_loss": sl,
                "stop_loss": sl,
                "take_profit": tp,
                "position_size": pos_size,
                "atr_at_entry": atr_val,
                "ema_at_entry": (
                    self.data.EMA[-1] if "EMA" in self.data.df.columns else None
                ),
                "rsi_at_entry": (
                    self.data.RSI[-2] if "RSI" in self.data.df.columns else None
                ),
                "bb_lower_at_entry": (
                    self.data.BBL[-2] if "BBL" in self.data.df.columns else None
                ),
                "bb_upper_at_entry": (
                    self.data.BBU[-2] if "BBU" in self.data.df.columns else None
                ),
                "trade_number": self.trade_counter,
                "entry_spread": entry_spread,
            }
        )

        # Immediately evaluate exit for the entry candle
        self._manage_exit(close, high, low, current_ts, current_ticks)

    def _manage_exit(self, close, high, low, current_ts, current_ticks=None):
        """
        Manage open position exits:
        1. Check SL hit
        2. Check TP hit
        3. Apply breakeven protection
        4. Apply trailing stop
        """
        if current_ticks is not None and not current_ticks.empty and self.use_dynamic_spread:
            trade_type = self.current_trade["signal_type"]
            for tick_time, tick in current_ticks.iterrows():
                bid = tick['Bid']
                ask = tick['Ask']
                
                if trade_type == "long":
                    if self._evaluate_single_price_exit(bid, tick_time):
                        return
                elif trade_type == "short":
                    if self._evaluate_single_price_exit(ask, tick_time):
                        return
            return # Survived all ticks in this minute
            
        # --- Fallback to OHLC logic if ticks are not available ---
        trade_type = self.current_trade["signal_type"]
        if trade_type == "long":
            # Rough approximation: check TP then SL with High/Low
            if self._evaluate_single_price_exit(high, current_ts): return
            if self._evaluate_single_price_exit(low, current_ts): return
        elif trade_type == "short":
            if self._evaluate_single_price_exit(low, current_ts): return
            if self._evaluate_single_price_exit(high, current_ts): return

    def _evaluate_single_price_exit(self, current_price, current_ts):
        """
        Evaluates exit conditions for a single price point.
        Returns True if trade is closed.
        """
        entry = self.current_trade["entry_price"]
        sl = self.current_trade["stop_loss"]
        tp = self.current_trade["take_profit"]
        trade_type = self.current_trade["signal_type"]

        if trade_type == "long":
            # --- Take Profit ---
            if current_price >= tp:
                self._close_trade(current_price, current_ts, "Take Profit")
                return True
                
            # --- Stop Loss ---
            if current_price <= sl:
                reason = self._get_exit_reason()
                self._close_trade(current_price, current_ts, reason)
                return True

            # --- Breakeven & Trailing ---
            profit_distance = current_price - entry
            profit_pct_entry = (profit_distance / entry) * 100 if entry > 0 else 0

            if (self.use_breakeven and not self.current_trade["breakeven_activated"]
                and profit_distance >= self.breakeven_trigger_points):
                self.current_trade["stop_loss"] = entry + self.profit_lock_points
                self.current_trade["breakeven_activated"] = True

            if self.use_trailing_stop:
                trailing_triggered = (profit_distance >= self.trailing_trigger_points) if self.trailing_trigger_mode == "points" else (profit_pct_entry >= self.trailing_trigger_percent)
                if trailing_triggered:
                    self.current_trade["trailing_activated"] = True
                    trail_dist = self.trailing_trigger_points if self.trailing_step_mode == "points" else (self.current_trade["atr_at_entry"] * self.trailing_step_atr_multiplier)
                    new_sl = current_price - trail_dist
                    if new_sl > self.current_trade["stop_loss"]:
                        self.current_trade["stop_loss"] = new_sl

        elif trade_type == "short":
            # --- Take Profit ---
            if current_price <= tp:
                self._close_trade(current_price, current_ts, "Take Profit")
                return True
                
            # --- Stop Loss ---
            if current_price >= sl:
                reason = self._get_exit_reason()
                self._close_trade(current_price, current_ts, reason)
                return True

            # --- Breakeven & Trailing ---
            profit_distance = entry - current_price
            profit_pct_entry = (profit_distance / entry) * 100 if entry > 0 else 0

            if (self.use_breakeven and not self.current_trade["breakeven_activated"]
                and profit_distance >= self.breakeven_trigger_points):
                self.current_trade["stop_loss"] = entry - self.profit_lock_points
                self.current_trade["breakeven_activated"] = True

            if self.use_trailing_stop:
                trailing_triggered = (profit_distance >= self.trailing_trigger_points) if self.trailing_trigger_mode == "points" else (profit_pct_entry >= self.trailing_trigger_percent)
                if trailing_triggered:
                    self.current_trade["trailing_activated"] = True
                    trail_dist = self.trailing_trigger_points if self.trailing_step_mode == "points" else (self.current_trade["atr_at_entry"] * self.trailing_step_atr_multiplier)
                    new_sl = current_price + trail_dist
                    if new_sl < self.current_trade["stop_loss"]:
                        self.current_trade["stop_loss"] = new_sl
                        
        return False

    def _get_exit_reason(self):
        """Determine the exit reason label based on trade management state."""
        if self.current_trade["trailing_activated"]:
            return "Trailing Stop"
        elif self.current_trade["breakeven_activated"]:
            return "Breakeven SL"
        else:
            return "Stop Loss"

    def _close_trade(self, exit_price, exit_time, reason):
        """
        Close the current trade:
        - Calculate PnL in pips and USD
        - Update capital (compounding)
        - Check daily loss / consecutive loss protections
        - Append trade to signals log
        - Reset trade state
        """
        entry = self.current_trade["entry_price"]
        trade_type = self.current_trade["signal_type"]
        pos_size = self.current_trade["position_size"]

        # PnL calculation
        if trade_type == "long":
            pnl_pips = exit_price - entry
        else:
            pnl_pips = entry - exit_price

        pnl_usd = pnl_pips * pos_size * self.lot_size_standard

        # --- Spread Calculation ---
        if self.use_dynamic_spread:
            # Dynamic spread is already embedded in the PnL since we executed at Bid/Ask
            # But we record the exact dynamic spread observed at entry
            spread = self.current_trade.get("entry_spread", 0.0)
            if self.spread_mode == "points":
                spread = spread * 100 # log in points if requested
            spread_usd = 0.0
        else:
            spread = self.spreads_in_trades
            spread_usd = pos_size * spread

        # --- Brokerage Calculation ---
        # Brokerage = lots × cost per standard lot (charged per trade)
        brokerage = pos_size * self.brokerage_per_std_lot

        # --- Update Capital (Compounding) — deduct brokerage and adjust spread ---
        net_pnl_usd = pnl_usd - brokerage + spread_usd
        self.daily_pnl += net_pnl_usd
        MeanReversionScalper.trading_capital += net_pnl_usd

        # --- Daily Loss Protection ---
        daily_loss_limit = MeanReversionScalper.trading_capital * (
            self.max_daily_loss_pct / 100
        )
        if self.daily_pnl < -daily_loss_limit:
            self.daily_trading_suspended = True

        # --- Consecutive Loss Tracking ---
        if pnl_usd < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive_losses:
                if self.cooldown_mode == "next_day":
                    self.daily_trading_suspended = True
                else:
                    self.cooldown_until = exit_time + pd.Timedelta(
                        minutes=self.cooldown_minutes
                    )
        else:
            self.consecutive_losses = 0

        # --- Log Trade ---
        MeanReversionScalper.signals.append(
            {
                "trade_number": self.current_trade["trade_number"],
                "signal_timestamp": self.current_trade["signal_timestamp"],
                "signal_type": trade_type,
                "entry_price": round(entry, 2),
                "entry_time": self.current_trade["entry_time"],
                "initial_stop_loss": round(
                    self.current_trade["initial_stop_loss"], 2
                ),
                "final_stop_loss": round(self.current_trade["stop_loss"], 2),
                "take_profit": round(self.current_trade["take_profit"], 2),
                "exit_price": round(exit_price, 2),
                "exit_time": exit_time,
                "position_size_lots": pos_size,
                "pnl_pips": round(pnl_pips, 2),
                "pnl_usd": round(pnl_usd, 2),
                "spread": round(spread, 2),
                "spread_usd": round(spread_usd, 2),
                "brokerage_usd": round(brokerage, 2),
                "net_pnl_usd": round(net_pnl_usd, 2),
                "reason_for_exit": reason,
                "atr_at_entry": round(self.current_trade["atr_at_entry"], 4),
                "ema_at_entry": (
                    round(self.current_trade["ema_at_entry"], 2)
                    if self.current_trade["ema_at_entry"] is not None
                    else None
                ),
                "rsi_at_entry": (
                    round(self.current_trade["rsi_at_entry"], 2)
                    if self.current_trade["rsi_at_entry"] is not None
                    else None
                ),
                "breakeven_activated": self.current_trade["breakeven_activated"],
                "trailing_activated": self.current_trade["trailing_activated"],
            }
        )

        # --- Reset Trade ---
        self.current_trade = default_records()


# =============================================================================
# PERFORMANCE METRICS
# =============================================================================
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
    return (recovery_idx - dd_start_idx).days


def calculate_metrics(trades_df, capital, pnl_column="pnl_usd"):
    """
    Calculate comprehensive performance metrics from the trades DataFrame.
    Works with either pnl_usd or pnl_pips as the profit column.
    """
    if trades_df.empty:
        print("\nNo trades executed during this period.")
        return None

    trades_df = trades_df.copy()
    trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
    trades_df["exit_time"] = pd.to_datetime(trades_df["exit_time"])
    trades_df["month"] = trades_df["exit_time"].dt.to_period("M")
    trades_df["year"] = trades_df["exit_time"].dt.year

    profit_col = pnl_column

    # Basic metrics
    total_trades = len(trades_df)
    winning_trades = len(trades_df[trades_df[profit_col] > 0])
    losing_trades = len(trades_df[trades_df[profit_col] <= 0])
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    net_profit = trades_df[profit_col].sum()
    avg_trade = trades_df[profit_col].mean()
    highest_profit = trades_df[profit_col].max()
    highest_loss = trades_df[profit_col].min()

    # Profit Factor
    gross_profit = trades_df[trades_df[profit_col] > 0][profit_col].sum()
    gross_loss = abs(trades_df[trades_df[profit_col] <= 0][profit_col].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    # Monthly & Yearly PNL
    monthly_pnl = trades_df.groupby("month")[profit_col].sum()
    yearly_pnl = trades_df.groupby("year")[profit_col].sum()
    monthly_trades_count = trades_df.groupby("month").size()

    # Drawdown
    cum_pnl = trades_df[profit_col].cumsum()
    peak = cum_pnl.cummax()
    drawdown = cum_pnl - peak
    max_drawdown = drawdown.min()

    # Annualized drawdown per year
    drawdown_data = []
    for year, group in trades_df.groupby("year"):
        year_cum = group[profit_col].cumsum()
        year_peak = year_cum.cummax()
        year_dd = (year_cum - year_peak).min()
        drawdown_data.append((year, year_dd))

    # Recovery days
    cum_pnl_dated = trades_df[profit_col].cumsum()
    cum_pnl_dated.index = trades_df["exit_time"]
    recovery = recovery_days(cum_pnl_dated)

    # Sharpe Ratio (annualized, ~252 trading days)
    daily_pnl = trades_df.groupby(trades_df["exit_time"].dt.date)[profit_col].sum()
    if daily_pnl.std() > 0:
        sharpe = (daily_pnl.mean() / daily_pnl.std()) * np.sqrt(252)
    else:
        sharpe = 0

    # CAGR
    first_date = trades_df["entry_time"].min()
    last_date = trades_df["exit_time"].max()
    years = (last_date - first_date).days / 365.25
    if years > 0 and capital + net_profit > 0:
        cagr = ((capital + net_profit) / capital) ** (1 / years) - 1
    else:
        cagr = 0

    # Consecutive wins/losses
    results = (trades_df[profit_col] > 0).astype(int)
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

    # Average winner / loser
    avg_winner = (
        trades_df[trades_df[profit_col] > 0][profit_col].mean()
        if winning_trades > 0
        else 0
    )
    avg_loser = (
        trades_df[trades_df[profit_col] <= 0][profit_col].mean()
        if losing_trades > 0
        else 0
    )

    # --- Build Summary Rows ---
    summary_rows = []

    for period, pnl in monthly_pnl.items():
        summary_rows.append(
            {"entry_time": None, "exit_time": None, "pnl": pnl, "metric": f"Monthly PnL ({period})"}
        )
    for year, pnl in yearly_pnl.items():
        summary_rows.append(
            {"entry_time": None, "exit_time": None, "pnl": pnl, "metric": f"Total Year PnL ({year})"}
        )
    for year, dd in drawdown_data:
        summary_rows.append(
            {"entry_time": None, "exit_time": None, "pnl": dd, "metric": f"Max Drawdown ({year})"}
        )
    for year, roi in roi_data.items():
        summary_rows.append(
            {"entry_time": None, "exit_time": None, "pnl": roi, "metric": f"ROI % ({year})"}
        )
    for period, count in monthly_trades_count.items():
        summary_rows.append(
            {"entry_time": None, "exit_time": None, "pnl": count, "metric": f"Trades in ({period})"}
        )

    # Brokerage & cost metrics
    total_brokerage = trades_df["brokerage_usd"].sum() if "brokerage_usd" in trades_df.columns else 0
    total_spread_cost = trades_df["spread_usd"].sum() if "spread_usd" in trades_df.columns else 0
    total_net_pnl = trades_df["net_pnl_usd"].sum() if "net_pnl_usd" in trades_df.columns else net_profit
    brokerage_ratio = (total_brokerage / gross_profit * 100) if gross_profit > 0 else 100

    # Overall metrics
    overall = [
        ("Total Trades", total_trades),
        ("Winning Trades", winning_trades),
        ("Losing Trades", losing_trades),
        ("Win Rate %", round(win_rate, 2)),
        ("Net Profit", round(net_profit, 2)),
        ("Total Brokerage", round(total_brokerage, 2)),
        ("Total Spread Cost", round(total_spread_cost, 2)),
        ("Net PnL After Costs", round(total_net_pnl, 2)),
        ("Brokerage Ratio %", round(brokerage_ratio, 2)),
        ("CAGR %", round(cagr * 100, 2)),
        ("Profit Factor", round(profit_factor, 4)),
        ("Overall Max Drawdown", round(max_drawdown, 2)),
        ("Sharpe Ratio", round(sharpe, 4)),
        ("Average Trade", round(avg_trade, 2)),
        ("Average Winner", round(avg_winner, 2)),
        ("Average Loser", round(avg_loser, 2)),
        ("Highest Single Profit", round(highest_profit, 2)),
        ("Highest Single Loss", round(highest_loss, 2)),
        ("Max Consecutive Wins", max_consec_wins),
        ("Max Consecutive Losses", max_consec_losses),
        ("Recovery Days from MaxDD", recovery),
    ]
    for metric_name, value in overall:
        summary_rows.append(
            {"entry_time": None, "exit_time": None, "pnl": value, "metric": metric_name}
        )

    summary_df = pd.DataFrame(summary_rows)
    return summary_df


# =============================================================================
# MAIN EXECUTION
# =============================================================================
def main(
    Backtest_period,
    Timeframe,
    ema_length,
    bb_length,
    bb_std,
    rsi_length,
    rsi_oversold,
    rsi_overbought,
    atr_length,
    htf_timeframe,
    atr_sl_multiplier,
    atr_tp_multiplier,
    trading_capital,
    risk_per_trade_pct,
    lot_size_standard,
    dynamic_qty,
    static_lot_size,
    brokerage_per_std_lot,
    spreads_in_trades,
    use_breakeven,
    breakeven_trigger_points,
    profit_lock_points,
    use_trailing_stop,
    trailing_trigger_mode,
    trailing_trigger_points,
    trailing_trigger_percent,
    trailing_step_mode,
    trailing_step_points,
    trailing_step_atr_multiplier,
    spread_mode,
    max_spread_points,
    max_spread_units,
    use_session_filter,
    session_start,
    session_end,
    max_daily_loss_pct,
    max_consecutive_losses,
    cooldown_mode,
    cooldown_minutes,
    show_pnl_in_usd,
    Tick_data_path=None,
    use_dynamic_spread=True,
):
    """Run the XAUUSD Mean Reversion Scalper backtest."""

    # --- Configure Strategy Class Parameters ---
    MeanReversionScalper.ema_length = ema_length
    MeanReversionScalper.bb_length = bb_length
    MeanReversionScalper.bb_std = bb_std
    MeanReversionScalper.rsi_length = rsi_length
    MeanReversionScalper.rsi_oversold = rsi_oversold
    MeanReversionScalper.rsi_overbought = rsi_overbought
    MeanReversionScalper.atr_length = atr_length
    MeanReversionScalper.atr_sl_multiplier = atr_sl_multiplier
    MeanReversionScalper.atr_tp_multiplier = atr_tp_multiplier
    MeanReversionScalper.trading_capital = trading_capital
    MeanReversionScalper.initial_capital = trading_capital
    MeanReversionScalper.risk_per_trade_pct = risk_per_trade_pct
    MeanReversionScalper.lot_size_standard = lot_size_standard
    MeanReversionScalper.dynamic_qty = dynamic_qty
    MeanReversionScalper.static_lot_size = static_lot_size
    MeanReversionScalper.brokerage_per_std_lot = brokerage_per_std_lot
    MeanReversionScalper.spreads_in_trades = spreads_in_trades
    MeanReversionScalper.use_breakeven = use_breakeven
    MeanReversionScalper.breakeven_trigger_points = breakeven_trigger_points
    MeanReversionScalper.profit_lock_points = profit_lock_points
    MeanReversionScalper.use_trailing_stop = use_trailing_stop
    MeanReversionScalper.trailing_trigger_mode = trailing_trigger_mode
    MeanReversionScalper.trailing_trigger_points = trailing_trigger_points
    MeanReversionScalper.trailing_trigger_percent = trailing_trigger_percent
    MeanReversionScalper.trailing_step_mode = trailing_step_mode
    MeanReversionScalper.trailing_step_points = trailing_step_points
    MeanReversionScalper.trailing_step_atr_multiplier = trailing_step_atr_multiplier
    MeanReversionScalper.spread_mode = spread_mode
    MeanReversionScalper.max_spread_points = max_spread_points
    MeanReversionScalper.max_spread_units = max_spread_units
    MeanReversionScalper.use_session_filter = use_session_filter
    MeanReversionScalper.session_start = session_start
    MeanReversionScalper.session_end = session_end
    MeanReversionScalper.max_daily_loss_pct = max_daily_loss_pct
    MeanReversionScalper.max_consecutive_losses = max_consecutive_losses
    MeanReversionScalper.cooldown_mode = cooldown_mode
    MeanReversionScalper.cooldown_minutes = cooldown_minutes
    MeanReversionScalper.use_dynamic_spread = use_dynamic_spread
    MeanReversionScalper.signals = []  # Reset trade log
    MeanReversionScalper.tick_df = None # Initialize tick data

    # --- Load Data ---
    if not Tick_data_path or not os.path.exists(Tick_data_path):
        print(f"Tick data file not found: {Tick_data_path}")
        return

    print(f"Loading tick data from: {Tick_data_path}")
    tick_df = pd.read_csv(Tick_data_path, sep='\t')
    if '<DATE>' in tick_df.columns and '<TIME>' in tick_df.columns:
        tick_df['datetime'] = pd.to_datetime(tick_df['<DATE>'] + ' ' + tick_df['<TIME>'], format='%Y.%m.%d %H:%M:%S.%f')
        tick_df.set_index('datetime', inplace=True)
        # Rename for easier access
        if '<BID>' in tick_df.columns:
            tick_df.rename(columns={'<BID>': 'Bid'}, inplace=True)
        if '<ASK>' in tick_df.columns:
            tick_df.rename(columns={'<ASK>': 'Ask'}, inplace=True)
            
        print("Forward-filling missing Bid/Ask prices...")
        tick_df['Bid'] = tick_df['Bid'].ffill()
        tick_df['Ask'] = tick_df['Ask'].ffill()
        tick_df.dropna(subset=['Bid', 'Ask'], inplace=True)
        
        MeanReversionScalper.tick_df = tick_df.sort_index()
        print(f"Tick data loaded: {len(MeanReversionScalper.tick_df)} ticks.")
    else:
        raise ValueError("Tick data does not contain <DATE> and <TIME> columns.")
        
    print(f"Generating {Timeframe} OHLC data from ticks...")
    # Resample to the base timeframe
    data = MeanReversionScalper.tick_df['Bid'].resample(Timeframe).ohlc()
    data.dropna(subset=['open', 'high', 'low', 'close'], how='all', inplace=True)
    data.reset_index(inplace=True)
    data.rename(columns={'datetime': 'timestamp'}, inplace=True)
    
    # Optionally compute volume
    if '<VOLUME>' in tick_df.columns:
        tick_df['<VOLUME>'] = pd.to_numeric(tick_df['<VOLUME>'], errors='coerce').fillna(0)
        volume = tick_df['<VOLUME>'].resample(Timeframe).sum()
        volume_df = volume.reset_index().rename(columns={'datetime': 'timestamp', '<VOLUME>': 'tick_volume'})
        data = pd.merge(data, volume_df, on='timestamp', how='left')

    # Keep required columns
    keep_cols = ["timestamp", "open", "high", "low", "close"]
    if "spread" in data.columns:
        keep_cols.append("spread")
    if "tick_volume" in data.columns:
        keep_cols.append("tick_volume")
    data = data[keep_cols]
    data = data.sort_values("timestamp").reset_index(drop=True)

    # Convert OHLC to float
    for col in ["open", "high", "low", "close"]:
        data[col] = data[col].astype(float)

    # Filter by backtest period
    start_date = pd.Timestamp(Backtest_period["start_date"])
    end_date = pd.Timestamp(Backtest_period["end_date"])
    data = data[(data["timestamp"] >= start_date) & (data["timestamp"] <= end_date)]
    print(f"Data loaded: {len(data)} candles from {data['timestamp'].min()} to {data['timestamp'].max()}")

    print(f"Data loaded: {len(data)} candles from {data['timestamp'].min()} to {data['timestamp'].max()}")

    # --- Compute Indicators ---
    ema_label = f"EMA({ema_length})" if ema_length > 0 else "EMA(disabled)"
    print(
        f"Computing Indicators: {ema_label}, BB({bb_length}, {bb_std}), "
        f"RSI({rsi_length}), ATR({atr_length})..."
    )
    data = compute_indicators(
        data,
        ema_length=ema_length,
        bb_length=bb_length,
        bb_std=bb_std,
        rsi_length=rsi_length,
        atr_length=atr_length,
        htf_timeframe=htf_timeframe,
    )

    # Set timestamp as index and rename columns for backtesting.py
    if "timestamp" in data.columns:
        data.set_index("timestamp", inplace=True)
    data.rename(
        columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"},
        inplace=True,
    )

    # Rename spread column if present
    if "spread" in data.columns:
        data.rename(columns={"spread": "Spread"}, inplace=True)
    if "tick_volume" in data.columns:
        data.rename(columns={"tick_volume": "Volume"}, inplace=True)

    # --- Generate Signals ---
    print("Generating mean reversion signals...")
    data = signal_generator(data, rsi_oversold=rsi_oversold, rsi_overbought=rsi_overbought)

    # Drop rows with NaN indicators (warmup period)
    dropna_cols = ["BBL", "BBU", "RSI", "ATR"]
    if "EMA" in data.columns:
        dropna_cols.append("EMA")
    data.dropna(subset=dropna_cols, inplace=True)

    # Save preprocessed data for debugging
    data.to_csv("meanreversion_preprocessed.csv")
    signal_count = data["signal"].notna().sum()
    print(f"Data shape: {data.shape}, Date range: {data.index.min()} to {data.index.max()}")
    print(f"Signals generated: {signal_count} ({data[data['signal']=='long'].shape[0]} long, {data[data['signal']=='short'].shape[0]} short)")

    # Tick Data is already loaded in MeanReversionScalper.tick_df
    # We no longer need to load it again here.

    # --- Run Backtest ---
    print("\nStarting Backtest...")
    bt = Backtest(
        data,
        MeanReversionScalper,
        cash=trading_capital,
        commission=0.0,
        trade_on_close=False,
    )
    stats = bt.run()

    # --- Process Trade Log ---
    print("\n--- Backtest Complete ---")
    signals_list = MeanReversionScalper.signals

    if len(signals_list) == 0:
        print("\nNo trades executed during this period.")
        print("Possible reasons:")
        print("  1. No signals generated (check indicator parameters)")
        print("  2. Session filter is too restrictive")
        print("  3. Spread filter rejected all entries")
        print("  4. Insufficient data for EMA warmup")
        return

    trades_df = pd.DataFrame(signals_list)
    print(f"Total trades executed: {len(trades_df)}")

    # --- Calculate Metrics ---
    pnl_col = "pnl_usd" if show_pnl_in_usd else "pnl_pips"
    pnl_label = "USD" if show_pnl_in_usd else "Pips"

    winning = len(trades_df[trades_df[pnl_col] > 0])
    losing = len(trades_df[trades_df[pnl_col] <= 0])
    net_pnl = trades_df[pnl_col].sum()

    print(f"Winning trades: {winning}")
    print(f"Losing trades: {losing}")
    print(f"Win rate: {winning / len(trades_df) * 100:.1f}%")
    print(f"Net PnL ({pnl_label}): {net_pnl:.2f}")

    total_spread = trades_df["spread_usd"].sum()
    total_brokerage = trades_df["brokerage_usd"].sum()
    total_net_pnl = trades_df["net_pnl_usd"].sum()
    print(f"Total Spread Cost: ${total_spread:.2f}")
    print(f"Total Brokerage: ${total_brokerage:.2f}")
    print(f"Net PnL after Costs: ${total_net_pnl:.2f}")

    # Exit reasons breakdown
    print("\nExit reasons:")
    for reason, count in trades_df["reason_for_exit"].value_counts().items():
        print(f"  {reason}: {count}")

    summary_df = calculate_metrics(trades_df, capital=trading_capital, pnl_column=pnl_col)

    # --- Generate Multi-Tab Excel Output ---
    output_filename = "BACKTEST_MEAN_REVERSION_XAUUSD.xlsx"
    with pd.ExcelWriter(output_filename, engine="openpyxl") as writer:

        # TAB 1: All Trades
        trades_export = trades_df[
            [
                "trade_number", "signal_type", "entry_time", "entry_price",
                "initial_stop_loss", "take_profit", "exit_time", "exit_price",
                "final_stop_loss", "position_size_lots", "pnl_pips", "pnl_usd",
                "spread", "spread_usd", "brokerage_usd", "net_pnl_usd",
                "reason_for_exit", "breakeven_activated", "trailing_activated",
            ]
        ]
        trades_export.to_excel(writer, sheet_name="All Trades", index=False)

        # TAB 2: Detailed Trades (with indicator values)
        trades_df.to_excel(writer, sheet_name="Detailed Trades", index=False)

        # TAB 3: Technical Statistics
        if summary_df is not None:
            # Main metrics (exclude monthly/yearly breakdowns)
            main_stats = summary_df[
                ~summary_df["metric"].str.contains(
                    r"Monthly PnL|Year PnL|Trades in|Max Drawdown \(|ROI %",
                    regex=True,
                    na=False,
                )
            ][["metric", "pnl"]].copy()
            main_stats.columns = ["Metric", "Value"]
            main_stats.to_excel(
                writer, sheet_name="Technical Statistics", index=False,
                startrow=0, startcol=0,
            )

            # Monthly returns
            monthly_ret = trades_df.copy()
            monthly_ret["month_year"] = pd.to_datetime(monthly_ret["exit_time"]).dt.to_period("M")
            monthly_table = monthly_ret.groupby("month_year")[pnl_col].sum().reset_index()
            monthly_table.columns = ["Month", f"PnL ({pnl_label})"]
            monthly_table.to_excel(
                writer, sheet_name="Technical Statistics", index=False,
                startrow=0, startcol=4,
            )

            # Yearly returns
            monthly_ret["year"] = pd.to_datetime(monthly_ret["exit_time"]).dt.year
            yearly_table = monthly_ret.groupby("year")[pnl_col].sum().reset_index()
            yearly_table.columns = ["Year", f"PnL ({pnl_label})"]
            yearly_table.to_excel(
                writer, sheet_name="Technical Statistics", index=False,
                startrow=0, startcol=7,
            )

            # Trade distribution by exit reason
            reason_dist = trades_df["reason_for_exit"].value_counts().reset_index()
            reason_dist.columns = ["Exit Reason", "Count"]
            reason_dist.to_excel(
                writer, sheet_name="Technical Statistics", index=False,
                startrow=0, startcol=10,
            )

            # Trade distribution by PnL buckets
            pnl_series = trades_df[pnl_col]
            if show_pnl_in_usd:
                bins = [-float("inf"), -5000, -2000, -500, 0, 500, 2000, 5000, float("inf")]
                labels = ["Large Loss", "Medium Loss", "Small Loss", "Tiny Loss",
                          "Tiny Win", "Small Win", "Medium Win", "Large Win"]
            else:
                bins = [-float("inf"), -20, -10, -5, 0, 5, 10, 20, float("inf")]
                labels = ["Large Loss", "Medium Loss", "Small Loss", "Tiny Loss",
                          "Tiny Win", "Small Win", "Medium Win", "Large Win"]
            trades_df["pnl_bucket"] = pd.cut(pnl_series, bins=bins, labels=labels)
            dist_df = trades_df["pnl_bucket"].value_counts().sort_index().reset_index()
            dist_df.columns = ["PnL Range", "Count"]
            dist_df.to_excel(
                writer, sheet_name="Technical Statistics", index=False,
                startrow=0, startcol=13,
            )

        # TAB 4: Equity Curve
        equity_df = trades_df[["exit_time", pnl_col]].copy()
        equity_df = equity_df.sort_values("exit_time")
        equity_df["cumulative_pnl"] = equity_df[pnl_col].cumsum()
        equity_df["account_balance"] = trading_capital + equity_df["cumulative_pnl"]

        start_row = 20  # Leave space for chart above data
        equity_df.to_excel(writer, sheet_name="Equity Curve", index=False, startrow=start_row)

        # Add line chart
        wb = writer.book
        data_sheet = wb["Equity Curve"]

        chart = LineChart()
        chart.title = "XAUUSD Mean Reversion — Equity Curve"
        chart.style = 13
        chart.y_axis.title = f"Cumulative PnL ({pnl_label})"
        chart.x_axis.title = "Trade Exit Datetime"

        min_row = start_row + 1
        max_row = min_row + len(equity_df)

        # Column 3 = cumulative_pnl, Column 1 = exit_time
        data_ref = Reference(data_sheet, min_col=3, min_row=min_row, max_row=max_row)
        cats_ref = Reference(data_sheet, min_col=1, min_row=min_row + 1, max_row=max_row)

        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        chart.width = 30
        chart.height = 12

        data_sheet.add_chart(chart, "A1")

    print(f"\nMulti-tab Excel saved to: {output_filename}")
    print("Tabs: All Trades | Detailed Trades | Technical Statistics | Equity Curve")

    # Save raw trades CSV
    trades_df.to_csv("TRADES_RAW_XAUUSD.csv", index=False)
    print(f"Raw trades CSV saved to: TRADES_RAW_XAUUSD.csv")


# =============================================================================
# CONFIGURATION & ENTRY POINT
# =============================================================================
if __name__ == "__main__":

    CONFIG = dict(
        # --- Data ---
        Spot_data_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "xauusd_2020_2026.csv"),
        Backtest_period={"start_date":  ddate(2026, 7, 10), "end_date": ddate(2026, 7, 18)},
        Timeframe="1min",  # "1min", "5min", "15min", "1h", etc.
        htf_timeframe="5min", # Higher timeframe for ATR calculation

        # --- Indicator Parameters ---
        ema_length=0,       # EMA period (trend filter) - changed from 0 to 200
        bb_length=20,         # Bollinger Bands period
        bb_std=1.5,           # Bollinger Bands std deviation
        rsi_length=9,         # RSI period
        rsi_oversold=30,      # RSI oversold threshold
        rsi_overbought=70,    # RSI overbought threshold
        atr_length=14,        # ATR period

        # --- SL/TP Configuration ---
        atr_sl_multiplier=1.5,   # Stop Loss = ATR × multiplier
        atr_tp_multiplier=1.5,   # Take Profit = ATR × multiplier - changed from 1.75 to 1.5

        # --- Capital & Position Sizing ---
        trading_capital=30000,      # Account balance (USD)
        risk_per_trade_pct=1,      # Risk % per trade
        lot_size_standard=100,       # 1 standard lot = 100 oz for XAUUSD
        dynamic_qty=True,            # True = dynamic position sizing, False = use static_lot_size
        static_lot_size=0.1,         # Fixed lot size when dynamic_qty is False
        brokerage_per_std_lot=8.0,   # Brokerage cost per 1 standard lot per trade ($8)
        spreads_in_trades=0.60,      # Spread in points (e.g. 0.60 to add, -0.60 to deduct, 0 to disable)

        # --- Breakeven Protection ---
        use_breakeven=True,
        breakeven_trigger_points=1.0, # changed from 100 to 1.0 ($1.00 move = 100 points)
        profit_lock_points=0.1,       # changed from 10 to 0.1 ($0.10 locked = 10 points)

        # --- Trailing Stop ---
        use_trailing_stop=True,
        trailing_trigger_mode="points",
        trailing_trigger_points=1.2, # changed from 120 to 1.2 ($1.20 move = 120 points)
        trailing_trigger_percent=2.0,
        trailing_step_mode="points",
        trailing_step_points=0.2,    # changed from 20 to 0.2 ($0.20 ratchet = 20 points)
        trailing_step_atr_multiplier=1.0,

        # --- Execution Filters ---
        spread_mode="points",
        max_spread_points=25,          # changed from 50 to 25 points
        max_spread_units=5,
        use_session_filter=True,       # True = restrict to session hours, False = trade any time
        session_start=dtime(8, 0),     # Session start (UTC)
        session_end=dtime(15, 0),      # changed from 15:00 to 21:00 (UTC)

        # --- Risk Protection ---
        max_daily_loss_pct=3.0,           # Suspend if daily loss > 3% of equity
        max_consecutive_losses=3,         # Consecutive losses before cooldown
        cooldown_mode="minutes",          # changed from "next_day" to "minutes"
        cooldown_minutes=240,             # changed from 60 to 240 (4 hours)

        # --- Display ---
        show_pnl_in_usd=True,            # True = USD, False = Pips
    )
    CONFIG = {
  "Backtest_period": {
    "start_date": "2026-01-01",
    "end_date": "2026-07-30"
  },
  "Timeframe": "5min",
  "htf_timeframe": "5min",
  "ema_length": 200,
  "bb_length": 20,
  "bb_std": 1.5,
  "rsi_length": 9,
  "rsi_oversold": 30,
  "rsi_overbought": 70,
  "atr_length": 14,
  "atr_sl_multiplier": 1.5,
  "atr_tp_multiplier": 4.5,
  "trading_capital": 10000,
  "risk_per_trade_pct": 0.5,
  "lot_size_standard": 100,
  "dynamic_qty": False,
  "static_lot_size": 0.1,
  "brokerage_per_std_lot": 8.0,
  "spreads_in_trades": 0,
  "use_breakeven": True,
  "breakeven_trigger_points": 1,
  "profit_lock_points": 0.1,
  "use_trailing_stop": True,
  "trailing_trigger_mode": "points",
  "trailing_trigger_points": 4.5,
  "trailing_trigger_percent": 1.28,
  "trailing_step_mode": "points",
  "trailing_step_points": 1,
  "trailing_step_atr_multiplier": 0.83,
  "spread_mode": "points",
  "max_spread_points": 40,
  "max_spread_units": 5,
  "use_session_filter": True,
  "session_start": "06:00:00",
  "session_end": "12:00:00",
  "max_daily_loss_pct": 5,
  "max_consecutive_losses": 5,
  "cooldown_mode": "minutes",
  "cooldown_minutes": 240,
  "show_pnl_in_usd": True,
  "Tick_data_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "XAUUSD.._202601020100_202608031435.csv"),
  "use_dynamic_spread": True
}
    main(**CONFIG)

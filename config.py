"""
config.py
---------
Two things live here:

1. BASE_CONFIG  -> everything that stays FIXED across every batch
                   (paths, symbol, capital, etc). EDIT THE PATHS BELOW
                   to match your machine before running.

2. sample_params() -> draws ONE random combination of the parameters
                   you want to sweep. Edit the ranges/choices to widen
                   or narrow your search.
"""
import random
import datetime
from datetime import date as ddate,time as dtime
import os

# Project root = directory where this config.py lives
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------
# FIXED CONFIG - same for every batch. Paths are relative to PROJECT_ROOT.
# ---------------------------------------------------------------

BASE_CONFIG = dict(
        # --- Data ---
        Spot_data_path=os.path.join(PROJECT_ROOT, "xauusd_2020_2026.csv"),
        Backtest_period={"start_date": ddate(2020, 7, 14), "end_date": ddate(2026, 7, 18)},
        Timeframe="1min",  # "1min", "5min", "15min", "1h", etc.

        # --- Indicator Parameters ---
        ema_length=200,       # EMA period (trend filter)
        bb_length=20,         # Bollinger Bands period
        bb_std=1.5,           # Bollinger Bands std deviation
        rsi_length=9,         # RSI period
        rsi_oversold=30,      # RSI oversold threshold
        rsi_overbought=70,    # RSI overbought threshold
        atr_length=14,        # ATR period

        # --- SL/TP Configuration ---
        atr_sl_multiplier=1.5,   # Stop Loss = ATR × multiplier
        atr_tp_multiplier=1.75,   # Take Profit = ATR × multiplier

        # --- Capital & Position Sizing ---
        trading_capital=10000,      # Account balance (USD)
        risk_per_trade_pct=0.5,      # Risk % per trade
        lot_size_standard=100,       # 1 standard lot = 100 oz for XAUUSD
        dynamic_qty=True,            # True = dynamic position sizing, False = use static_lot_size
        static_lot_size=0.1,         # Fixed lot size when dynamic_qty is False
        brokerage_per_std_lot=8.0,   # Brokerage cost per 1 standard lot per trade ($8)
        spreads_in_trades=0.60,      # Spread in points (e.g. 0.60 to add, -0.60 to deduct, 0 to disable)

        # --- Breakeven Protection ---
        use_breakeven=True,
        breakeven_trigger_points=100,
        profit_lock_points=10,

        # --- Trailing Stop ---
        use_trailing_stop=True,
        trailing_trigger_mode="points",
        trailing_trigger_points=120,
        trailing_trigger_percent=2.0,
        trailing_step_mode="points",
        trailing_step_points=20,
        trailing_step_atr_multiplier=1.0,

        # --- Execution Filters ---
        spread_mode="points",
        max_spread_points=50,
        max_spread_units=5,
        use_session_filter=True,       # True = restrict to session hours, False = trade any time
        session_start=dtime(8, 0),    # 10:30 AM IST     # Session start (UTC)
        session_end=dtime(15, 0),      # 5:30 PM IST   # Session end (UTC)

        # --- Risk Protection ---
        max_daily_loss_pct=3.0,           # Suspend if daily loss > 3% of equity
        max_consecutive_losses=3,         # Consecutive losses before cooldown
        cooldown_mode="next_day",
        cooldown_minutes=60,              # Cooldown duration (minutes) - only used if cooldown_mode == "minutes"

        # --- Display ---
        show_pnl_in_usd=True,            # True = USD, False = Pips
    )



# ---------------------------------------------------------------
# SWEEP SPACE - edit ranges/choices here to control what's tested
# ---------------------------------------------------------------
# NOTE: The backtest engine (MeanReversionBacktestXAUUSD.py) uses
# XAUUSD DOLLAR values for breakeven/trailing — NOT MT5 "points".
# e.g. breakeven_trigger_points=1.0 means a $1.00 price move,
#      which equals 100 MT5 points.
# All ranges below are calibrated to produce 10,000+ trades
# on 1-min data across 6 years.
# ---------------------------------------------------------------
TIMEFRAME_CHOICES = ["1min"]

def sample_params(rng: random.Random) -> dict:
    """Draw one random parameter combination. Called once per batch."""
    
    # --- Indicator Parameters ---
    # EMA: 0 = disabled (no trend filter → more signals), otherwise 50-200
    # 40% chance of disabling EMA to get more trades in many combos
    ema_length = 0 if rng.random() < 0.4 else rng.randint(50, 200)
    
    # --- Position Sizing ---
    dynamic_qty = rng.choice([False])
    risk_per_trade_pct = round(rng.uniform(0.5, 2.0), 2)
    static_lot_size = round(rng.uniform(0.05, 0.5), 2)

    # --- Breakeven ---
    # ~40% chance of disabling breakeven entirely to not choke trades
    use_breakeven = rng.choice([True, True, True, False, False])
    
    # --- Trailing Stop ---
    use_trailing_stop = rng.choice([True, True, False])
    
    # Trailing trigger/step: these are XAUUSD dollar values (NOT MT5 points!)
    # Typical XAUUSD ATR on 1-min is ~$0.50-$2.00
    trailing_trigger_mode = rng.choice(["points", "percent"])
    trailing_trigger_points = round(rng.uniform(0.5, 3.0), 2)   # $0.50–$3.00 move triggers trailing
    trailing_trigger_percent = round(rng.uniform(0.5, 3.0), 2)
    
    trailing_step_mode = rng.choice(["points", "atr"])
    trailing_step_points = round(rng.uniform(0.1, 1.0), 2)      # $0.10–$1.00 trail distance
    trailing_step_atr_multiplier = round(rng.uniform(0.3, 1.5), 2)

    # --- Execution Filters ---
    # Spread filter — "points" mode is most common for XAUUSD
    spread_mode = "points"
    max_spread_points = rng.randint(20, 80)   # Wider range to not reject too many candles
    max_spread_units = rng.randint(3, 10)
    
    # Session filter — 30% chance of disabling to test 24-hour trading
    use_session_filter = rng.random() >= 0.3  # True 70%, False 30%
    
    # --- Risk Protection ---
    max_daily_loss_pct = round(rng.uniform(0.5, 4.0), 2)        # 2–8% daily loss limit (less restrictive)
    max_consecutive_losses = rng.randint(3, 15)                   # 5–15 consecutive losses before cooldown
    cooldown_mode = rng.choice(["minutes", "minutes", "next_day"])  # Favor minutes over next_day
    cooldown_minutes = rng.choice([30, 60, 90, 120, 180, 240])

    # --- Session Times (UTC) ---
    # Known trading sessions with real-world liquidity windows.
    # The optimizer randomly picks one (or a combination) to test.
    trading_sessions_utc = [
        ("Sydney",    dtime(22, 0), dtime(7, 0)),
        ("Tokyo",     dtime(0, 0),  dtime(9, 0)),
        ("Hong Kong", dtime(1, 0),  dtime(10, 0)),
        ("Singapore", dtime(1, 0),  dtime(10, 0)),
        ("Shanghai",  dtime(1, 30), dtime(7, 0)),
        ("Frankfurt", dtime(7, 0),  dtime(16, 0)),
        ("Zurich",    dtime(7, 0),  dtime(16, 0)),
        ("London",    dtime(8, 0),  dtime(17, 0)),
        ("New York",  dtime(13, 0), dtime(22, 0)),
        ("custom 1",  dtime(8, 0), dtime(15, 0)),
        ("custom 2",  dtime(10, 0), dtime(17, 0)),
        ("custom 3",  dtime(12, 0), dtime(22, 0)),
        ("custom 4",  dtime(8, 0), dtime(20, 0)),
        ("custom 5",  dtime(13, 0), dtime(23, 0)),
        ("custom 6",  dtime(0, 0), dtime(10, 0)),
        ("custom 7",  dtime(7, 0), dtime(23, 0)),
        ("custom 8",  dtime(12, 0), dtime(23, 0)),
        ("custom 9",  dtime(13, 0), dtime(23, 59)),
        ("custom 10", dtime(14, 0), dtime(23, 59)),
    ]
    session_name, session_start, session_end = rng.choice(trading_sessions_utc)

    return dict(
        Timeframe=rng.choice(TIMEFRAME_CHOICES),
        ema_length=ema_length,
        bb_length=rng.randint(10, 30),                           # Shorter BB → bands tighter → more touches
        bb_std=round(rng.uniform(1.0, 2.2), 2),                  # 1.0–2.2 std (was up to 3.0 — too wide)
        rsi_length=rng.randint(5, 14),                            # Shorter RSI → more extremes on 1-min
        rsi_oversold=rng.randint(25, 40),                         # 25–40 (not as restrictive as 20)
        rsi_overbought=rng.randint(60, 75),                       # 60–75 (not as restrictive as 80)
        atr_length=rng.randint(7, 21),
        
        atr_sl_multiplier=round(rng.uniform(0.8, 2.5), 2),       # Tighter range for SL
        atr_tp_multiplier=round(rng.uniform(0.8, 3.0), 2),       # Tighter range for TP
        
        dynamic_qty=dynamic_qty,
        risk_per_trade_pct=risk_per_trade_pct,
        static_lot_size=static_lot_size,

        use_breakeven=use_breakeven,
        breakeven_trigger_points=round(rng.uniform(0.3, 2.0), 2),  # $0.30–$2.00 (was 50–200 MT5 points!)
        profit_lock_points=round(rng.uniform(0.05, 0.5), 2),       # $0.05–$0.50 locked (was 5–50 MT5 points!)
        
        use_trailing_stop=use_trailing_stop,
        trailing_trigger_mode=trailing_trigger_mode,
        trailing_trigger_points=trailing_trigger_points,
        trailing_trigger_percent=trailing_trigger_percent,
        trailing_step_mode=trailing_step_mode,
        trailing_step_points=trailing_step_points,
        trailing_step_atr_multiplier=trailing_step_atr_multiplier,
        
        spread_mode=spread_mode,
        max_spread_points=max_spread_points,
        max_spread_units=max_spread_units,
        use_session_filter=use_session_filter,
        
        session_start=session_start,
        session_end=session_end,
        
        max_daily_loss_pct=max_daily_loss_pct,
        max_consecutive_losses=max_consecutive_losses,
        cooldown_mode=cooldown_mode,
        cooldown_minutes=cooldown_minutes,
    )


def build_full_config(sampled: dict) -> dict:
    """Merge sampled sweep params on top of fixed BASE_CONFIG."""
    cfg = dict(BASE_CONFIG)
    cfg.update(sampled)
    return cfg

import numpy as np
import pandas as pd

class IndicatorRegistry:
    """
    Registry for technical indicators computed on the backend.
    """
    _registry = {}

    @classmethod
    def register(cls, name: str, display_name: str, category: str, overlay: bool, params_schema: list):
        def decorator(func):
            cls._registry[name] = {
                "name": name,
                "display_name": display_name,
                "category": category,
                "overlay": overlay,
                "params": params_schema,
                "calc_func": func
            }
            return func
        return decorator

    @classmethod
    def get_all(cls):
        result = []
        for name, meta in cls._registry.items():
            result.append({
                "name": meta["name"],
                "display_name": meta["display_name"],
                "category": meta["category"],
                "overlay": meta["overlay"],
                "params": meta["params"]
            })
        return result

    @classmethod
    def calculate(cls, name: str, df: pd.DataFrame, params: dict):
        if name not in cls._registry:
            raise ValueError(f"Indicator '{name}' not found in registry.")
        return cls._registry[name]["calc_func"](df, **params)


# =============================================================================
# 1. PINE SCRIPT EXACT SUPERTREND INDICATOR (Reference: SupertrendBacktestXAUUSD_FIXED.py)
# =============================================================================
@IndicatorRegistry.register(
    name="supertrend",
    display_name="Supertrend (Pine Script Math)",
    category="Overlay",
    overlay=True,
    params_schema=[
        {"name": "length", "label": "ATR Length", "type": "int", "default": 5},
        {"name": "multiplier", "label": "Multiplier", "type": "float", "default": 1.5}
    ]
)
def calculate_supertrend(df: pd.DataFrame, length: int = 5, multiplier: float = 1.5) -> dict:
    n = len(df)
    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    timestamps = df.index.astype('datetime64[s]').astype('int64').tolist()

    tr = np.zeros(n)
    atr = np.zeros(n)
    basic_ub = np.zeros(n)
    basic_lb = np.zeros(n)
    final_ub = np.zeros(n)
    final_lb = np.zeros(n)
    supertrend = np.zeros(n)
    trend_dir = np.ones(n, dtype=int)
    signals = [None] * n

    # 1. True Range
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

    # 2. Wilder's RMA ATR
    if n >= length:
        atr[length - 1] = np.mean(tr[:length])
        for i in range(length, n):
            atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / float(length)

    # 3. Bands and Supertrend
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

        if basic_ub[i] < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]:
            final_ub[i] = basic_ub[i]
        else:
            final_ub[i] = final_ub[i - 1]

        if basic_lb[i] > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]:
            final_lb[i] = basic_lb[i]
        else:
            final_lb[i] = final_lb[i - 1]

        prev_trend = trend_dir[i - 1]
        if prev_trend == -1 and close[i] > final_ub[i - 1]:
            trend_dir[i] = 1
            signals[i] = "BUY"
        elif prev_trend == 1 and close[i] < final_lb[i - 1]:
            trend_dir[i] = -1
            signals[i] = "SELL"
        else:
            trend_dir[i] = prev_trend

        supertrend[i] = final_lb[i] if trend_dir[i] == 1 else final_ub[i]

    # Format Series Output for Lightweight Charts
    st_line = []
    bull_band = []
    bear_band = []
    markers = []

    for i in range(n):
        t = int(timestamps[i])
        if i < length - 1:
            continue
        
        val = round(float(supertrend[i]), 3)
        st_line.append({"time": t, "value": val, "direction": int(trend_dir[i])})

        # Shaded Bands
        if trend_dir[i] == 1:
            bull_band.append({"time": t, "top": round(float(high[i]), 3), "bottom": val})
        else:
            bear_band.append({"time": t, "top": val, "bottom": round(float(low[i]), 3)})

        # Signal Markers
        if signals[i] == "BUY":
            markers.append({
                "time": t,
                "position": "belowBar",
                "color": "#089981",
                "shape": "arrowUp",
                "text": f"BUY @ {round(float(close[i]), 2)}"
            })
        elif signals[i] == "SELL":
            markers.append({
                "time": t,
                "position": "aboveBar",
                "color": "#f23645",
                "shape": "arrowDown",
                "text": f"SELL @ {round(float(close[i]), 2)}"
            })

    return {
        "type": "supertrend",
        "title": f"Supertrend ({length}, {multiplier})",
        "overlay": True,
        "series": {
            "st_line": st_line,
            "bull_band": bull_band,
            "bear_band": bear_band,
            "markers": markers
        }
    }


# =============================================================================
# 2. SIMPLE MOVING AVERAGE (SMA)
# =============================================================================
@IndicatorRegistry.register(
    name="sma",
    display_name="Simple Moving Average (SMA)",
    category="Overlay",
    overlay=True,
    params_schema=[
        {"name": "period", "label": "Period", "type": "int", "default": 20},
        {"name": "color", "label": "Line Color", "type": "color", "default": "#2962FF"}
    ]
)
def calculate_sma(df: pd.DataFrame, period: int = 20, color: str = "#2962FF") -> dict:
    timestamps = df.index.astype('datetime64[s]').astype('int64').tolist()
    sma_vals = df["Close"].rolling(window=period).mean().values

    line_data = []
    for i in range(len(df)):
        if not np.isnan(sma_vals[i]):
            line_data.append({
                "time": int(timestamps[i]),
                "value": round(float(sma_vals[i]), 3)
            })

    return {
        "type": "line",
        "title": f"SMA ({period})",
        "overlay": True,
        "color": color,
        "series": line_data
    }


# =============================================================================
# 3. EXPONENTIAL MOVING AVERAGE (EMA)
# =============================================================================
@IndicatorRegistry.register(
    name="ema",
    display_name="Exponential Moving Average (EMA)",
    category="Overlay",
    overlay=True,
    params_schema=[
        {"name": "period", "label": "Period", "type": "int", "default": 50},
        {"name": "color", "label": "Line Color", "type": "color", "default": "#FF6D00"}
    ]
)
def calculate_ema(df: pd.DataFrame, period: int = 50, color: str = "#FF6D00") -> dict:
    timestamps = df.index.astype('datetime64[s]').astype('int64').tolist()
    ema_vals = df["Close"].ewm(span=period, adjust=False).mean().values

    line_data = []
    for i in range(len(df)):
        if not np.isnan(ema_vals[i]):
            line_data.append({
                "time": int(timestamps[i]),
                "value": round(float(ema_vals[i]), 3)
            })

    return {
        "type": "line",
        "title": f"EMA ({period})",
        "overlay": True,
        "color": color,
        "series": line_data
    }


# =============================================================================
# 4. RELATIVE STRENGTH INDEX (RSI)
# =============================================================================
@IndicatorRegistry.register(
    name="rsi",
    display_name="Relative Strength Index (RSI)",
    category="Oscillator",
    overlay=False,
    params_schema=[
        {"name": "period", "label": "RSI Length", "type": "int", "default": 14},
        {"name": "color", "label": "RSI Color", "type": "color", "default": "#7E57C2"}
    ]
)
def calculate_rsi(df: pd.DataFrame, period: int = 14, color: str = "#7E57C2") -> dict:
    timestamps = df.index.astype('datetime64[s]').astype('int64').tolist()
    delta = df["Close"].diff()

    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    rsi_vals = rsi.values

    series_data = []
    for i in range(len(df)):
        if not np.isnan(rsi_vals[i]):
            series_data.append({
                "time": int(timestamps[i]),
                "value": round(float(rsi_vals[i]), 2)
            })

    return {
        "type": "oscillator",
        "title": f"RSI ({period})",
        "overlay": False,
        "color": color,
        "series": series_data,
        "levels": {"upper": 70, "lower": 30, "middle": 50}
    }


# =============================================================================
# 5. MACD (MOVING AVERAGE CONVERGENCE DIVERGENCE)
# =============================================================================
@IndicatorRegistry.register(
    name="macd",
    display_name="MACD",
    category="Oscillator",
    overlay=False,
    params_schema=[
        {"name": "fast", "label": "Fast Period", "type": "int", "default": 12},
        {"name": "slow", "label": "Slow Period", "type": "int", "default": 26},
        {"name": "signal", "label": "Signal Period", "type": "int", "default": 9}
    ]
)
def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    timestamps = df.index.astype('datetime64[s]').astype('int64').tolist()
    ema_fast = df["Close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["Close"].ewm(span=slow, adjust=False).mean()

    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    macd_vals = macd_line.values
    signal_vals = signal_line.values
    hist_vals = histogram.values

    macd_series = []
    signal_series = []
    hist_series = []

    for i in range(len(df)):
        t = int(timestamps[i])
        if not np.isnan(macd_vals[i]):
            macd_series.append({"time": t, "value": round(float(macd_vals[i]), 3)})
            signal_series.append({"time": t, "value": round(float(signal_vals[i]), 3)})
            
            h_val = round(float(hist_vals[i]), 3)
            h_color = "#26a69a" if h_val >= 0 else "#ef5350"
            hist_series.append({"time": t, "value": h_val, "color": h_color})

    return {
        "type": "macd",
        "title": f"MACD ({fast}, {slow}, {signal})",
        "overlay": False,
        "series": {
            "macd": macd_series,
            "signal": signal_series,
            "histogram": hist_series
        }
    }


# =============================================================================
# 6. BOLLINGER BANDS
# =============================================================================
@IndicatorRegistry.register(
    name="bollinger",
    display_name="Bollinger Bands",
    category="Overlay",
    overlay=True,
    params_schema=[
        {"name": "period", "label": "Length", "type": "int", "default": 20},
        {"name": "std_dev", "label": "StdDev", "type": "float", "default": 2.0}
    ]
)
def calculate_bollinger(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> dict:
    timestamps = df.index.astype('datetime64[s]').astype('int64').tolist()
    sma = df["Close"].rolling(window=period).mean()
    std = df["Close"].rolling(window=period).std()

    upper = sma + (std_dev * std)
    lower = sma - (std_dev * std)

    upper_series = []
    middle_series = []
    lower_series = []

    for i in range(len(df)):
        t = int(timestamps[i])
        if not np.isnan(sma.values[i]):
            upper_series.append({"time": t, "value": round(float(upper.values[i]), 3)})
            middle_series.append({"time": t, "value": round(float(sma.values[i]), 3)})
            lower_series.append({"time": t, "value": round(float(lower.values[i]), 3)})

    return {
        "type": "bollinger",
        "title": f"Bollinger Bands ({period}, {std_dev})",
        "overlay": True,
        "series": {
            "upper": upper_series,
            "middle": middle_series,
            "lower": lower_series
        }
    }

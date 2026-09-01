import os
import time
import pandas as pd
import numpy as np

class DataEngine:
    """
    DataEngine handles loading tick Parquet datasets, resampling ticks into OHLC bars,
    and caching resampled timeframes (1m, 5m, 15m, 1h, 4h, 1D) for fast server responses.
    """
    def __init__(self, parquet_path: str):
        self.parquet_path = parquet_path
        self.tick_df = None
        self.base_1m_df = None
        self.timeframe_cache = {}
        self.broker = "MULTIBANK"
        self.symbol = "XAUUSD"
        self.symbol_display = "Gold Spot / U.S. Dollar"

    def load_data(self):
        if not os.path.exists(self.parquet_path):
            raise FileNotFoundError(f"Parquet file not found: {self.parquet_path}")

        print(f"[DataEngine] Loading Parquet dataset: {self.parquet_path}...")
        t0 = time.time()
        self.tick_df = pd.read_parquet(self.parquet_path)
        print(f"[DataEngine] Loaded {len(self.tick_df):,} tick records in {time.time() - t0:.2f}s.")

        # Ensure index is DatetimeIndex
        if not isinstance(self.tick_df.index, pd.DatetimeIndex):
            if "datetime" in self.tick_df.columns:
                self.tick_df.set_index("datetime", inplace=True)
            else:
                self.tick_df.index = pd.to_datetime(self.tick_df.index)

        # Build Base 1-Minute OHLC DataFrame
        print("[DataEngine] Resampling raw ticks into Base 1-Minute OHLC candles...")
        t1 = time.time()
        
        # Resample Bid price for OHLC, and count ticks for volume
        resampled = self.tick_df["Bid"].resample("1min").ohlc()
        resampled["volume"] = self.tick_df["Bid"].resample("1min").count()
        resampled.dropna(subset=["open", "high", "low", "close"], how="all", inplace=True)
        resampled.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}, inplace=True)
        
        self.base_1m_df = resampled
        self.timeframe_cache["1m"] = self.base_1m_df
        print(f"[DataEngine] Base 1m candles constructed ({len(self.base_1m_df):,} bars) in {time.time() - t1:.2f}s.")

    def get_ohlc(self, timeframe: str = "15m", start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        Returns OHLC DataFrame for the specified timeframe (1m, 5m, 15m, 1h, 4h, 1D).
        Uses cached data if available.
        """
        if self.base_1m_df is None:
            self.load_data()

        tf_map = {
            "1m": "1min",
            "2m": "2min",
            "3m": "3min",
            "5m": "5min",
            "10m": "10min",
            "15m": "15min",
            "20m": "20min",
            "30m": "30min",
            "1h": "1h",
            "4h": "4h",
            "1D": "1d",
            "1d": "1d"
        }

        target_tf = tf_map.get(timeframe.lower(), "15min")

        if timeframe in self.timeframe_cache:
            df = self.timeframe_cache[timeframe]
        else:
            print(f"[DataEngine] Aggregating OHLC for timeframe: {timeframe} ({target_tf})...")
            if target_tf == "1min":
                df = self.base_1m_df
            else:
                ohlc_dict = {
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum"
                }
                df = self.base_1m_df.resample(target_tf).agg(ohlc_dict)
                df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)
            
            self.timeframe_cache[timeframe] = df

        # Filter by start_date and end_date if supplied
        if start_date or end_date:
            df_filtered = df.copy()
            if start_date:
                df_filtered = df_filtered[df_filtered.index >= pd.to_datetime(start_date)]
            if end_date:
                df_filtered = df_filtered[df_filtered.index <= pd.to_datetime(end_date)]
            return df_filtered

        return df

    def get_formatted_ohlc(self, timeframe: str = "15m", start_date: str = None, end_date: str = None) -> list:
        """
        Formats OHLC bars into TradingView Lightweight Charts format:
        [ { "time": 1767315600, "open": 4329.7, "high": 4333.2, "low": 4329.0, "close": 4331.5, "volume": 120 }, ... ]
        """
        df = self.get_ohlc(timeframe, start_date, end_date)
        records = []
        
        # Convert DatetimeIndex cleanly to Unix Epoch Seconds
        timestamps = df.index.astype('datetime64[s]').astype('int64').tolist()
        opens = df["Open"].values.tolist()
        highs = df["High"].values.tolist()
        lows = df["Low"].values.tolist()
        closes = df["Close"].values.tolist()
        volumes = df["Volume"].values.tolist()

        for t, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes):
            records.append({
                "time": int(t),
                "open": round(float(o), 3),
                "high": round(float(h), 3),
                "low": round(float(l), 3),
                "close": round(float(c), 3),
                "volume": int(v) if not np.isnan(v) else 0
            })
        return records

    def get_summary(self) -> dict:
        if self.base_1m_df is None:
            self.load_data()

        start_time = str(self.base_1m_df.index[0])
        end_time = str(self.base_1m_df.index[-1])
        last_bar = self.base_1m_df.iloc[-1]
        prev_bar = self.base_1m_df.iloc[-2] if len(self.base_1m_df) > 1 else last_bar
        change = round(float(last_bar["Close"] - prev_bar["Close"]), 3)
        change_pct = round((change / float(prev_bar["Close"])) * 100.0, 2) if prev_bar["Close"] != 0 else 0.0

        return {
            "symbol": self.symbol,
            "symbol_display": self.symbol_display,
            "broker": self.broker,
            "total_ticks": len(self.tick_df) if self.tick_df is not None else 0,
            "total_1m_bars": len(self.base_1m_df),
            "start_time": start_time,
            "end_time": end_time,
            "last_price": round(float(last_bar["Close"]), 3),
            "open": round(float(last_bar["Open"]), 3),
            "high": round(float(last_bar["High"]), 3),
            "low": round(float(last_bar["Low"]), 3),
            "close": round(float(last_bar["Close"]), 3),
            "change": change,
            "change_pct": change_pct
        }

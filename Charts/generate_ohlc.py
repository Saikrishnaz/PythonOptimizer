import os
import time
import pandas as pd

def generate_ohlc_csvs():
    parquet_file = 'XAUUSD.._202601020100_202608101443.parquet'
    output_dir = 'generated_ohlc'
    
    if not os.path.exists(parquet_file):
        print(f"Error: Could not find {parquet_file}")
        return

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    print(f"Loading Parquet dataset: {parquet_file}...")
    t0 = time.time()
    tick_df = pd.read_parquet(parquet_file)
    print(f"Loaded {len(tick_df):,} tick records in {time.time() - t0:.2f}s.")

    # Ensure index is DatetimeIndex
    if not isinstance(tick_df.index, pd.DatetimeIndex):
        if "datetime" in tick_df.columns:
            tick_df.set_index("datetime", inplace=True)
        else:
            tick_df.index = pd.to_datetime(tick_df.index)

    print("Resampling raw ticks into Base 1-Minute OHLC candles...")
    t1 = time.time()
    
    # Resample Bid price for OHLC, and count ticks for volume
    base_1m_df = tick_df["Bid"].resample("1min").ohlc()
    base_1m_df["volume"] = tick_df["Bid"].resample("1min").count()
    base_1m_df.dropna(subset=["open", "high", "low", "close"], how="all", inplace=True)
    base_1m_df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}, inplace=True)
    
    print(f"Base 1m candles constructed ({len(base_1m_df):,} bars) in {time.time() - t1:.2f}s.")

    # Timeframes to generate
    timeframes = {
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
        "1D": "1d"
    }

    ohlc_dict = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum"
    }

    for tf_label, tf_pandas in timeframes.items():
        print(f"Generating {tf_label} CSV...")
        
        if tf_pandas == "1min":
            df_resampled = base_1m_df.copy()
        else:
            df_resampled = base_1m_df.resample(tf_pandas).agg(ohlc_dict)
            df_resampled.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)

        # Format DataFrame to match the target CSV structure
        # <DATE>    <TIME>  <OPEN>  <HIGH>  <LOW>   <CLOSE> <TICKVOL>   <VOL>   <SPREAD>
        # Dates should be YYYY.MM.DD
        # Times should be HH:MM:SS
        
        df_out = pd.DataFrame(index=df_resampled.index)
        df_out["<DATE>"] = df_resampled.index.strftime('%Y.%m.%d')
        df_out["<TIME>"] = df_resampled.index.strftime('%H:%M:%S')
        df_out["<OPEN>"] = df_resampled["Open"].apply(lambda x: f"{x:.2f}")
        df_out["<HIGH>"] = df_resampled["High"].apply(lambda x: f"{x:.2f}")
        df_out["<LOW>"] = df_resampled["Low"].apply(lambda x: f"{x:.2f}")
        df_out["<CLOSE>"] = df_resampled["Close"].apply(lambda x: f"{x:.2f}")
        df_out["<TICKVOL>"] = df_resampled["Volume"].astype(int)
        df_out["<VOL>"] = 0
        df_out["<SPREAD>"] = 40  # default constant matching the source CSVs
        
        out_file = os.path.join(output_dir, f"XAUUSD_generated_{tf_label}.csv")
        df_out.to_csv(out_file, sep='\t', index=False)
        print(f"  -> Saved {len(df_out):,} rows to {out_file}")

    print("Done!")

if __name__ == "__main__":
    generate_ohlc_csvs()

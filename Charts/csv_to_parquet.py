"""
=============================================================================
High-Performance CSV to Parquet Converter Utility
=============================================================================
Converts MT5 Tick CSV / Standard OHLC CSV files to compressed Parquet format.
Parquet files load ~100x faster than CSV and use 70% less disk space.

Usage:
  python csv_to_parquet.py <input_csv_path> [output_parquet_path]
=============================================================================
"""

import sys
import os
import time
import pandas as pd

def convert_csv_to_parquet(csv_path: str, parquet_path: str = None, sep: str = None):
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Input CSV file not found: {csv_path}")

    if parquet_path is None:
        parquet_path = os.path.splitext(csv_path)[0] + ".parquet"

    print(f"[1/4] Inspecting CSV file: {csv_path}...")
    t0 = time.time()

    # Detect delimiter if not provided
    if sep is None:
        with open(csv_path, 'r', encoding='utf-8', errors='ignore') as f:
            first_line = f.readline()
            if '\t' in first_line:
                sep = '\t'
            elif ';' in first_line:
                sep = ';'
            else:
                sep = ','
        print(f"      Detected Delimiter: {repr(sep)}")

    # Read CSV
    print(f"[2/4] Ingesting CSV dataset into pandas DataFrame...")
    df = pd.read_csv(csv_path, sep=sep, low_memory=False)
    print(f"      Loaded {len(df):,} rows x {len(df.columns)} columns in {time.time() - t0:.2f}s.")

    # Process Timestamp / Datetime Index
    print(f"[3/4] Parsing and indexing datetime timestamps...")
    t1 = time.time()
    
    # MetaTrader 5 Tick format: <DATE> + <TIME>
    if "<DATE>" in df.columns and "<TIME>" in df.columns:
        df["datetime"] = pd.to_datetime(df["<DATE>"].astype(str) + " " + df["<TIME>"].astype(str), errors='coerce')
        df.drop(columns=["<DATE>", "<TIME>"], inplace=True, errors='ignore')
        df.set_index("datetime", inplace=True)
        print("      Parsed MT5 <DATE> <TIME> into DatetimeIndex.")
    elif "DATE" in df.columns and "TIME" in df.columns:
        df["datetime"] = pd.to_datetime(df["DATE"].astype(str) + " " + df["TIME"].astype(str), errors='coerce')
        df.drop(columns=["DATE", "TIME"], inplace=True, errors='ignore')
        df.set_index("datetime", inplace=True)
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors='coerce')
        df.set_index("datetime", inplace=True)
    elif "Date" in df.columns and "Time" in df.columns:
        df["datetime"] = pd.to_datetime(df["Date"].astype(str) + " " + df["Time"].astype(str), errors='coerce')
        df.set_index("datetime", inplace=True)
    elif "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors='coerce')
        df.set_index("Date", inplace=True)

    # Rename MT5 columns <BID>, <ASK>, <LAST>, <VOLUME> if present
    rename_map = {
        "<BID>": "Bid", "<ASK>": "Ask", "<LAST>": "Last", "<VOLUME>": "Volume",
        "<FLAGS>": "Flags", "<OPEN>": "Open", "<HIGH>": "High", "<LOW>": "Low", "<CLOSE>": "Close"
    }
    df.rename(columns=rename_map, inplace=True)

    # Sort index if datetime
    if isinstance(df.index, pd.DatetimeIndex):
        df.sort_index(inplace=True)

    # Write to Parquet binary
    print(f"[4/4] Writing compressed Parquet binary (zstd) to: {parquet_path}...")
    t2 = time.time()
    df.to_parquet(parquet_path, compression="zstd", index=True)

    csv_size_mb = os.path.getsize(csv_path) / (1024 * 1024)
    parquet_size_mb = os.path.getsize(parquet_path) / (1024 * 1024)
    compression_ratio = (1 - (parquet_size_mb / csv_size_mb)) * 100

    print("=================================================================")
    print("CONVERSION SUCCESSFUL!")
    print(f"  Rows Converted   : {len(df):,}")
    print(f"  CSV File Size    : {csv_size_mb:.2f} MB")
    print(f"  Parquet Size     : {parquet_size_mb:.2f} MB ({compression_ratio:.1f}% space saved!)")
    print(f"  Total Duration   : {time.time() - t0:.2f} seconds")
    print("=================================================================")
    return parquet_path

if __name__ == "__main__":
    csv_file = r"C:\Users\ADMIN\Desktop\Charts\XAUUSD.._202601020100_202608101443.csv"
    out_file = r"C:\Users\ADMIN\Desktop\Charts\XAUUSD_MULTIBANK_2020_2026.parquet"
    convert_csv_to_parquet(csv_file, out_file)

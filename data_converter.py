"""
data_converter.py
-----------------
High-Performance CSV to Parquet Converter.
Reused from Charts project with enhancements for the optimizer.

Converts MT5 tick CSVs, standard OHLC CSVs, and Indian market CSVs
to compressed Parquet for 5-10x faster backtest execution.
"""

import os
import time
import pandas as pd


def convert_csv_to_parquet(csv_path: str, parquet_path: str = None, sep: str = None) -> dict:
    """
    Convert a CSV file to Parquet format.
    
    Returns:
        dict with conversion stats
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Input CSV file not found: {csv_path}")

    if parquet_path is None:
        parquet_path = os.path.splitext(csv_path)[0] + ".parquet"

    # Check if parquet already exists and is newer than CSV
    if os.path.exists(parquet_path):
        csv_mtime = os.path.getmtime(csv_path)
        pq_mtime = os.path.getmtime(parquet_path)
        if pq_mtime > csv_mtime:
            csv_size = os.path.getsize(csv_path) / (1024 * 1024)
            pq_size = os.path.getsize(parquet_path) / (1024 * 1024)
            return {
                "status": "already_converted",
                "csv_path": csv_path,
                "parquet_path": parquet_path,
                "csv_size_mb": round(csv_size, 2),
                "parquet_size_mb": round(pq_size, 2),
                "message": "Parquet file already up to date"
            }

    t0 = time.time()

    # Detect delimiter
    if sep is None:
        with open(csv_path, 'r', encoding='utf-8', errors='ignore') as f:
            first_line = f.readline()
            if '\t' in first_line:
                sep = '\t'
            elif ';' in first_line:
                sep = ';'
            else:
                sep = ','

    # Read CSV
    df = pd.read_csv(csv_path, sep=sep, low_memory=False)

    # Process Timestamp / Datetime Index
    # MetaTrader 5 Tick format: <DATE> + <TIME>
    if "<DATE>" in df.columns and "<TIME>" in df.columns:
        df["datetime"] = pd.to_datetime(df["<DATE>"].astype(str) + " " + df["<TIME>"].astype(str), errors='coerce')
        df.drop(columns=["<DATE>", "<TIME>"], inplace=True, errors='ignore')
        df.set_index("datetime", inplace=True)
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

    # Rename MT5 columns
    rename_map = {
        "<BID>": "Bid", "<ASK>": "Ask", "<LAST>": "Last", "<VOLUME>": "Volume",
        "<FLAGS>": "Flags", "<OPEN>": "Open", "<HIGH>": "High", "<LOW>": "Low", "<CLOSE>": "Close"
    }
    df.rename(columns=rename_map, inplace=True)

    # Sort index
    if isinstance(df.index, pd.DatetimeIndex):
        df.sort_index(inplace=True)

    # Write Parquet
    df.to_parquet(parquet_path, compression="zstd", index=True)

    csv_size_mb = os.path.getsize(csv_path) / (1024 * 1024)
    parquet_size_mb = os.path.getsize(parquet_path) / (1024 * 1024)
    compression_ratio = (1 - (parquet_size_mb / csv_size_mb)) * 100
    elapsed = time.time() - t0

    return {
        "status": "converted",
        "csv_path": csv_path,
        "parquet_path": parquet_path,
        "rows": len(df),
        "csv_size_mb": round(csv_size_mb, 2),
        "parquet_size_mb": round(parquet_size_mb, 2),
        "compression_pct": round(compression_ratio, 1),
        "elapsed_seconds": round(elapsed, 2),
        "message": f"Converted {len(df):,} rows — {compression_ratio:.1f}% space saved"
    }


def find_data_files_in_params(params: dict) -> list:
    """
    Scan params dict for file paths that could be converted to parquet.
    Returns list of CSV paths found.
    """
    csv_files = []
    for key, value in params.items():
        if isinstance(value, str) and os.path.isfile(value):
            if value.lower().endswith('.csv'):
                csv_files.append({"param_name": key, "csv_path": value})
    return csv_files


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python data_converter.py <input_csv_path> [output_parquet_path]")
        sys.exit(1)
    
    csv_file = sys.argv[1]
    out_file = sys.argv[2] if len(sys.argv) > 2 else None
    result = convert_csv_to_parquet(csv_file, out_file)
    print(result)

"""
walk_forward/window_generator.py
---------------------------------
Window generation and validation for Walk-Forward Testing.

Supports:
- Rolling windows (IS window slides forward)
- Expanding/anchored windows (IS start stays fixed)
- Auto-calculating number of steps
- Dataset date range detection
- Comprehensive validation
"""
import os
import re
from datetime import datetime
from dateutil.relativedelta import relativedelta
from io import StringIO
from typing import List, Tuple, Optional

import pandas as pd

from .models import WFOWindow, WFOConfig


# =============================================================================
# DATE RANGE DETECTION
# =============================================================================

def detect_data_range(data_path: str) -> Tuple[str, str]:
    """
    Detect the available date range from a dataset file.
    
    Supports CSV and Parquet files. Reads only the first and last
    few rows for efficiency — does NOT load the entire file.
    
    Returns:
        (start_date_iso, end_date_iso) as strings
    """
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"Data file not found: {data_path}")

    # Try Parquet first (fast)
    parquet_path = os.path.splitext(data_path)[0] + ".parquet"
    if os.path.exists(parquet_path):
        return _detect_range_parquet(parquet_path)
    if data_path.endswith(".parquet"):
        return _detect_range_parquet(data_path)

    # Fall back to CSV
    return _detect_range_csv(data_path)


def _detect_range_parquet(path: str) -> Tuple[str, str]:
    """Detect date range from a Parquet file."""
    df = pd.read_parquet(path)
    return _extract_range_from_df(df)


def _looks_like_header(line: str) -> bool:
    """
    Decide whether a CSV's first line is a header row.

    Headerless exports are common for Indian options/spot data
    (e.g. ``20220103,09:15,17393.2,...``). Treating that first row as column
    names silently breaks date detection, so sniff it: a header has at least one
    field that is neither numeric nor a bare time.
    """
    fields = [f.strip().strip('"') for f in line.replace('\t', ',').replace(';', ',').split(',')]
    if not fields:
        return True
    for field in fields:
        if not field:
            continue
        if re.fullmatch(r'\d{1,2}:\d{2}(:\d{2})?', field):
            continue  # bare time, e.g. 09:15
        try:
            float(field)
        except ValueError:
            return True  # non-numeric, non-time -> it is a label
    return False


def _read_tail_lines(path: str, num_lines: int = 100) -> str:
    """Read the last ``num_lines`` lines by seeking from the end of the file."""
    block = 65536
    with open(path, 'rb') as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        data = b''
        while size > 0 and data.count(b'\n') <= num_lines:
            step = min(block, size)
            size -= step
            fh.seek(size)
            data = fh.read(step) + data
    text = data.decode('utf-8', errors='ignore')
    return '\n'.join(text.splitlines()[-num_lines:])


def _detect_range_csv(path: str) -> Tuple[str, str]:
    """Detect date range from a CSV file — reads head and tail only."""
    with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
        first_line = fh.readline()
    header = 0 if _looks_like_header(first_line) else None

    # Read first 100 rows to detect format and get start.
    # NOTE: no low_memory here — pandas rejects it for the python engine
    # (and it is a no-op for that engine anyway).
    head_df = pd.read_csv(path, sep=None, engine='python', nrows=100, header=header,
                          encoding='utf-8', on_bad_lines='skip')

    # Read the last 100 rows by seeking from the end, so a multi-GB tick file
    # costs the same as a small one.
    try:
        tail_text = _read_tail_lines(path, 100)
        tail_df = pd.read_csv(StringIO(tail_text), sep=None, engine='python',
                              header=None, names=head_df.columns,
                              on_bad_lines='skip')
    except Exception:
        tail_df = head_df

    # Combine to find range
    combined = pd.concat([head_df.head(10), tail_df.tail(10)])
    return _extract_range_from_df(combined)


def _extract_range_from_df(df: pd.DataFrame) -> Tuple[str, str]:
    """Extract min/max timestamps from a DataFrame."""
    timestamps = None

    # Check if index is already datetime
    if isinstance(df.index, pd.DatetimeIndex):
        timestamps = df.index

    # Try common datetime column patterns
    if timestamps is None:
        for col in ['datetime', 'Date', 'Timestamp', 'time', 'timestamp']:
            if col in df.columns:
                try:
                    timestamps = pd.to_datetime(df[col], errors='coerce').dropna()
                    break
                except Exception:
                    continue

    # MT5 format: <DATE> + <TIME>
    if timestamps is None and '<DATE>' in df.columns and '<TIME>' in df.columns:
        try:
            dt_str = df['<DATE>'].astype(str) + ' ' + df['<TIME>'].astype(str)
            timestamps = pd.to_datetime(dt_str, errors='coerce').dropna()
        except Exception:
            pass

    # Separate date + time columns under any casing/decoration, e.g. MT5's
    # "DATE"/"TIME" or "<DATE>"/"<TIME>".
    if timestamps is None or len(timestamps) == 0:
        lookup = {str(c).strip().strip('<>').upper(): c for c in df.columns}
        if 'DATE' in lookup and 'TIME' in lookup:
            try:
                dt_str = (df[lookup['DATE']].astype(str) + ' '
                          + df[lookup['TIME']].astype(str))
                parsed = pd.to_datetime(dt_str, errors='coerce').dropna()
                if len(parsed) > 0:
                    timestamps = parsed
            except Exception:
                pass

    # Headerless YYYYMMDD[,HH:MM] layout used by the Indian spot/options
    # exports (e.g. "20220103,09:15,17393.2,..."). Columns are positional, so
    # look at the first column's shape rather than its name.
    if (timestamps is None or len(timestamps) == 0) and len(df.columns) >= 1:
        try:
            first = df[df.columns[0]].astype(str).str.strip()
            if first.str.fullmatch(r'\d{8}').all():
                if len(df.columns) >= 2:
                    second = df[df.columns[1]].astype(str).str.strip()
                    if second.str.fullmatch(r'\d{1,2}:\d{2}(:\d{2})?').all():
                        parsed = pd.to_datetime(first + ' ' + second, errors='coerce')
                    else:
                        parsed = pd.to_datetime(first, format='%Y%m%d', errors='coerce')
                else:
                    parsed = pd.to_datetime(first, format='%Y%m%d', errors='coerce')
                parsed = parsed.dropna()
                if len(parsed) > 0:
                    timestamps = parsed
        except Exception:
            pass

    if timestamps is None or len(timestamps) == 0:
        raise ValueError("Could not detect date range — no datetime column found")

    start = timestamps.min()
    end = timestamps.max()
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# =============================================================================
# WINDOW GENERATION
# =============================================================================

def generate_windows(config: WFOConfig) -> List[WFOWindow]:
    """
    Generate walk-forward windows based on configuration.
    
    Rolling mode:
        IS window slides forward by step_duration each iteration.
        IS length stays fixed at is_duration_months.
    
    Expanding mode:
        IS start is anchored at wfo_start.
        IS end extends by step_duration each iteration.
    
    Returns:
        List of WFOWindow objects
    """
    wfo_start = pd.Timestamp(config.wfo_start)
    wfo_end = pd.Timestamp(config.wfo_end)
    is_months = config.is_duration_months
    oos_months = config.oos_duration_months
    step_months = config.step_duration_months
    mode = config.window_mode

    windows = []
    step = 1

    if mode == "expanding":
        # Expanding/anchored: IS always starts at wfo_start
        is_start = wfo_start
        is_end = is_start + relativedelta(months=is_months) - relativedelta(days=1)
        oos_start = is_end + relativedelta(days=1)
        oos_end = oos_start + relativedelta(months=oos_months) - relativedelta(days=1)

        while oos_end <= wfo_end:
            if config.num_steps and step > config.num_steps:
                break

            windows.append(WFOWindow(
                step=step,
                is_start=is_start.strftime("%Y-%m-%d"),
                is_end=is_end.strftime("%Y-%m-%d"),
                oos_start=oos_start.strftime("%Y-%m-%d"),
                oos_end=oos_end.strftime("%Y-%m-%d"),
            ))

            step += 1
            # IS expands: start stays, end moves forward
            is_end = is_end + relativedelta(months=step_months)
            oos_start = is_end + relativedelta(days=1)
            oos_end = oos_start + relativedelta(months=oos_months) - relativedelta(days=1)

    else:
        # Rolling: IS window slides forward
        is_start = wfo_start
        is_end = is_start + relativedelta(months=is_months) - relativedelta(days=1)
        oos_start = is_end + relativedelta(days=1)
        oos_end = oos_start + relativedelta(months=oos_months) - relativedelta(days=1)

        while oos_end <= wfo_end:
            if config.num_steps and step > config.num_steps:
                break

            windows.append(WFOWindow(
                step=step,
                is_start=is_start.strftime("%Y-%m-%d"),
                is_end=is_end.strftime("%Y-%m-%d"),
                oos_start=oos_start.strftime("%Y-%m-%d"),
                oos_end=oos_end.strftime("%Y-%m-%d"),
            ))

            step += 1
            # Both IS start and IS end slide forward
            is_start = is_start + relativedelta(months=step_months)
            is_end = is_start + relativedelta(months=is_months) - relativedelta(days=1)
            oos_start = is_end + relativedelta(days=1)
            oos_end = oos_start + relativedelta(months=oos_months) - relativedelta(days=1)

    return windows


def calculate_max_steps(config: WFOConfig) -> int:
    """Calculate the maximum number of steps that fit in the dataset."""
    temp_config = WFOConfig(
        wfo_start=config.wfo_start,
        wfo_end=config.wfo_end,
        window_mode=config.window_mode,
        is_duration_months=config.is_duration_months,
        oos_duration_months=config.oos_duration_months,
        step_duration_months=config.step_duration_months,
        num_steps=None,  # No limit
    )
    windows = generate_windows(temp_config)
    return len(windows)


# =============================================================================
# VALIDATION
# =============================================================================

def validate_windows(
    windows: List[WFOWindow],
    dataset_start: str,
    dataset_end: str,
) -> List[str]:
    """
    Validate generated windows against the dataset.
    
    Returns:
        List of error/warning messages. Empty list = all valid.
    """
    errors = []
    ds_start = pd.Timestamp(dataset_start)
    ds_end = pd.Timestamp(dataset_end)

    if not windows:
        errors.append("No walk-forward windows generated. Check date range and durations.")
        return errors

    for w in windows:
        is_s = pd.Timestamp(w.is_start)
        is_e = pd.Timestamp(w.is_end)
        oos_s = pd.Timestamp(w.oos_start)
        oos_e = pd.Timestamp(w.oos_end)

        # IS must come before OOS
        if is_e >= oos_s:
            errors.append(
                f"Step {w.step}: IS end ({w.is_end}) overlaps with OOS start ({w.oos_start})."
            )

        # OOS must be strictly after IS
        if oos_s <= is_e:
            errors.append(
                f"Step {w.step}: OOS start ({w.oos_start}) is not strictly after IS end ({w.is_end})."
            )

        # IS must be within dataset
        if is_s < ds_start:
            errors.append(
                f"Step {w.step}: IS start ({w.is_start}) is before dataset start ({dataset_start})."
            )

        # OOS must be within dataset
        if oos_e > ds_end:
            errors.append(
                f"Step {w.step}: OOS end ({w.oos_end}) extends beyond dataset end ({dataset_end})."
            )

        # Chronological order
        if is_s > is_e:
            errors.append(f"Step {w.step}: IS start after IS end.")
        if oos_s > oos_e:
            errors.append(f"Step {w.step}: OOS start after OOS end.")

    # Check for OOS overlaps between steps
    for i in range(len(windows) - 1):
        curr_oos_e = pd.Timestamp(windows[i].oos_end)
        next_oos_s = pd.Timestamp(windows[i + 1].oos_start)
        # OOS periods can be adjacent but should not overlap
        if curr_oos_e >= next_oos_s:
            # This is a warning, not necessarily an error — adjacent OOS is fine
            pass

    return errors

"""
worker.py
---------
Universal script-agnostic backtest worker.

Runs ONE backtest batch in its own directory:
  1. Loads params.json
  2. cd's into the batch folder (so all output files land there)
  3. Auto-detects entry point style (main(**params), run_backtest(Config(**params)), etc.)
  4. Reads the "Technical Statistics" tab from any generated xlsx
  5. Writes metrics.json with parsed metrics + elapsed time

Invoked automatically by optimizer_engine.py as a subprocess per batch.
"""
import sys
import os
import json
import importlib.util
import traceback
import time
import glob
import inspect
from datetime import datetime, time as dtime, date as ddate

import pandas as pd

STRATEGY_MODULE_PATH = os.environ.get("STRATEGY_MODULE_PATH")
ENTRY_STYLE = os.environ.get("ENTRY_STYLE", "function_kwargs")
CONFIG_CLASS_NAME = os.environ.get("CONFIG_CLASS_NAME", "")


def load_strategy_module(path):
    """Dynamically import a strategy module from file path."""
    spec = importlib.util.spec_from_file_location("strategy_module", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def deserialize_params(params: dict) -> dict:
    """
    Convert JSON-serialized params back to proper Python types.
    Handles: datetime.time, datetime.date, nested dicts, etc.
    """
    result = {}
    for key, value in params.items():
        if isinstance(value, str):
            # Try datetime.time pattern: "HH:MM:SS"
            import re
            time_match = re.match(r'^(\d{1,2}):(\d{2}):(\d{2})$', value)
            if time_match:
                h, m, s = int(time_match.group(1)), int(time_match.group(2)), int(time_match.group(3))
                result[key] = dtime(h, m, s)
                continue
            
            # Try datetime.date pattern: "YYYY-MM-DD" (but not datetime strings with time)
            date_match = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', value)
            if date_match and key.lower() in ('start_date', 'end_date'):
                # Keep as string for scripts that expect string dates
                result[key] = value
        # Heuristically detect output paths and rewrite them to local basenames
        # so parallel workers don't overwrite the same global files
        if isinstance(value, str) and value.endswith(('.csv', '.xlsx', '.json', '.html')):
            key_lower = key.lower()
            if any(x in key_lower for x in ['trade', 'report', 'val_', 'out']):
                value = os.path.basename(value)
        
        if isinstance(value, dict):
            if "type" in value and value["type"] == "dtime":
                result[key] = datetime.fromisoformat(value["val"]).time()
            elif "type" in value and value["type"] == "dt":
                result[key] = datetime.fromisoformat(value["val"])
            else:
                deserialized_dict = {}
                for dk, dv in value.items():
                    if isinstance(dv, dict) and "type" in dv and dv["type"] == "dtime":
                        deserialized_dict[dk] = datetime.fromisoformat(dv["val"]).time()
                    elif isinstance(dv, dict) and "type" in dv and dv["type"] == "dt":
                        deserialized_dict[dk] = datetime.fromisoformat(dv["val"])
                    else:
                        deserialized_dict[dk] = dv
                result[key] = deserialized_dict
        else:
            result[key] = value
    return result


def filter_kwargs_for(func, params: dict) -> dict:
    """
    Drop params the entry function cannot accept.

    Mirrors what the config_class path already does with StrategyConfig's
    signature. It matters for the function-kwargs path because a walk-forward
    step can carry extra columns (metric names, flattened nested params)
    alongside the real parameters, and those would otherwise raise TypeError.

    A function declaring **kwargs takes everything — it has opted in to
    handling unknown keys itself.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return params

    accepts_var_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_var_kwargs:
        return params

    valid = {
        name for name, p in sig.parameters.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                      inspect.Parameter.KEYWORD_ONLY)
    }
    filtered = {k: v for k, v in params.items() if k in valid}

    dropped = sorted(set(params) - set(filtered))
    if dropped:
        print(f"[worker] Dropped params not accepted by {func.__name__}(): {dropped}")
    return filtered


def parse_technical_stats(xlsx_path: str) -> dict:
    """Reads the Metric/Value columns (col A/B) of the Technical Statistics tab."""
    try:
        df = pd.read_excel(xlsx_path, sheet_name="Technical Statistics", header=0, usecols=[0, 1])
        cols = df.columns.tolist()
        metric_col = cols[0]
        value_col = cols[1]
        df = df.dropna(subset=[metric_col])
        
        # Canonical names the optimizer's ranking functions read.
        #
        # Aliases are only ever added for labels no strategy in this project
        # emits as its own column, so nothing existing is renamed. A strategy
        # whose statistics use its own wording (e.g. the credit spread's
        # "Net Profit (points)") is expected to ALSO publish an explicit
        # canonical row — that keeps both columns available in the results
        # table instead of collapsing one into the other.
        key_map = {
            'total trades': 'Total Trades',
            'net profit ($)': 'Net Profit',
            'net profit': 'Net Profit',
            'net profit (inr)': 'Net Profit',
            'net profit (rs)': 'Net Profit',
            'net pnl': 'Net Profit',
            'win rate %': 'Win Rate %',
            'win rate': 'Win Rate %',
            'profit factor': 'Profit Factor',
            'sharpe ratio': 'Sharpe Ratio',
            'overall max drawdown': 'Overall Max Drawdown',
            'max drawdown': 'Overall Max Drawdown'
        }

        metrics = {}
        for _, row in df.iterrows():
            raw_key = str(row[metric_col]).strip()
            norm_key = raw_key.lower()
            mapped_key = key_map.get(norm_key, raw_key)
            # If two rows normalise onto the same canonical key, the row that
            # already used the canonical name is authoritative.
            if mapped_key in metrics and norm_key != mapped_key.lower():
                continue
            metrics[mapped_key] = row[value_col]
        return metrics
    except Exception as e:
        return {"_parse_error": str(e)}


def compute_basic_metrics_from_trades(trades_csv: str) -> dict:
    """Fallback: compute basic metrics from a trades CSV if no xlsx exists."""
    try:
        df = pd.read_csv(trades_csv)
        # Try common PnL column names. Existing names stay first so already
        # supported strategies resolve exactly as before; the trailing entries
        # cover options/credit-spread trade logs.
        pnl_col = None
        for col in ['net_pnl', 'pnl', 'profit', 'Net PnL', 'P&L', 'realized_pnl',
                    'profit_with_hedges_inr', 'profit_with_hedges_points',
                    'profit_in_inr', 'profit_points']:
            if col in df.columns:
                pnl_col = col
                break
        
        if pnl_col is None:
            return {"Total Trades": len(df)}
        
        pnl = df[pnl_col].astype(float)
        total_trades = len(pnl)
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        net_profit = float(pnl.sum())
        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0
        avg_win = float(wins.mean()) if len(wins) > 0 else 0
        avg_loss = float(losses.mean()) if len(losses) > 0 else 0
        profit_factor = abs(float(wins.sum()) / float(losses.sum())) if len(losses) > 0 and losses.sum() != 0 else 0
        
        # Sharpe ratio (simplified)
        if pnl.std() > 0:
            sharpe = float(pnl.mean() / pnl.std() * (252 ** 0.5))
        else:
            sharpe = 0
        
        # Max drawdown
        cumsum = pnl.cumsum()
        running_max = cumsum.cummax()
        drawdown = cumsum - running_max
        max_dd = float(drawdown.min())
        
        return {
            "Total Trades": total_trades,
            "Win Rate %": round(win_rate, 2),
            "Net Profit": round(net_profit, 2),
            "Profit Factor": round(profit_factor, 2),
            "Sharpe Ratio": round(sharpe, 2),
            "Overall Max Drawdown": round(max_dd, 2),
            "Average Win": round(avg_win, 2),
            "Average Loss": round(avg_loss, 2),
        }
    except Exception as e:
        return {"_parse_error": str(e)}


def main():
    batch_dir = sys.argv[1]
    params_path = os.path.join(batch_dir, "params.json")
    with open(params_path) as f:
        params = json.load(f)

    os.chdir(batch_dir)  # all outputs (xlsx, csv, debug files) land here

    status = {"ok": False, "error": None}
    metrics = {}
    t0 = time.time()
    
    try:
        if not STRATEGY_MODULE_PATH:
            raise RuntimeError("STRATEGY_MODULE_PATH env var not set")
        
        mod = load_strategy_module(STRATEGY_MODULE_PATH)
        
        # Deserialize params (JSON → Python types)
        deserialized = deserialize_params(params)
        
        if ENTRY_STYLE == "config_class" and CONFIG_CLASS_NAME:
            # Class-based: run_backtest(StrategyConfig(**params))
            config_cls = getattr(mod, CONFIG_CLASS_NAME)

            sig = inspect.signature(config_cls.__init__)
            valid_keys = set(sig.parameters.keys())

            # Filter deserialized params
            filtered_params = {k: v for k, v in deserialized.items() if k in valid_keys or k == 'kwargs'}

            config_obj = config_cls(**filtered_params)

            # Find the entry point function
            entry_fn = getattr(mod, 'run_backtest', None) or getattr(mod, 'main', None)
            if entry_fn is None:
                raise RuntimeError(f"No run_backtest() or main() found in {STRATEGY_MODULE_PATH}")
            entry_fn(config_obj)
        else:
            # Function kwargs: main(**params)
            entry_fn = getattr(mod, 'main', None)
            if entry_fn is None:
                raise RuntimeError(f"No main() function found in {STRATEGY_MODULE_PATH}")
            entry_fn(**filter_kwargs_for(entry_fn, deserialized))
        
        # Auto-discover xlsx output
        xlsx_files = glob.glob("*.xlsx")
        if xlsx_files:
            # Prefer the largest xlsx (most likely the full report)
            xlsx_path = max(xlsx_files, key=os.path.getsize)
            metrics = parse_technical_stats(xlsx_path)
        else:
            # Fallback: look for trade CSVs
            csv_files = glob.glob("*trade*.csv") + glob.glob("*python_trade*.csv")
            if csv_files:
                metrics = compute_basic_metrics_from_trades(csv_files[0])
            else:
                # No output at all — restrictive params, 0 trades
                metrics = {
                    "Total Trades": 0,
                    "Net Profit": 0.0,
                    "Sharpe Ratio": 0.0,
                    "Profit Factor": 0.0,
                    "Overall Max Drawdown": 0.0,
                }
        
        status["ok"] = True
    except Exception as e:
        status["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

    elapsed = time.time() - t0
    
    with open("metrics.json", "w") as f:
        json.dump({
            "status": status,
            "metrics": metrics,
            "params": params,
            "elapsed_seconds": round(elapsed, 2)
        }, f, indent=2, default=str)


if __name__ == "__main__":
    main()

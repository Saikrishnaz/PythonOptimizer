"""
monte_carlo/sources.py
----------------------
Where a Monte Carlo run gets its trades from.

Four kinds of source, all reduced to the same thing — an ordered array of
realised trade P&Ls:

    batch      one batch of an optimization run
    wfo        every out-of-sample trade of a walk-forward run, combined
    wfo_step   one walk-forward step's out-of-sample trades
    csv        any trade log on disk

Column detection is shared with the walk-forward report rather than
duplicated, so a trade log that one of them can read is readable by both.
"""
import glob
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from walk_forward import persistence as wfo_persistence
from walk_forward.report import (
    find_pnl_column, load_combined_oos_trades, load_step_trades,
)
from walk_forward.window_generator import generate_windows


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPTIMIZATIONS_DIR = os.path.join(SCRIPT_DIR, "optimizations")


class SourceError(Exception):
    """A source that cannot be turned into a trade series."""


# =============================================================================
# READING A TRADE FILE
# =============================================================================

def read_trade_file(path: str) -> np.ndarray:
    """Pull the P&L column out of a trade log."""
    if not os.path.exists(path):
        raise SourceError(f"Trade file not found: {path}")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise SourceError(f"Could not read {os.path.basename(path)}: {e}")

    column = find_pnl_column(df)
    if column is None:
        raise SourceError(
            f"{os.path.basename(path)} has no recognisable profit column. "
            f"Columns found: {', '.join(map(str, df.columns[:12]))}")

    values = pd.to_numeric(df[column], errors="coerce").to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise SourceError(f"{os.path.basename(path)} contains no numeric P&L values.")
    return values


def find_batch_trade_file(opt_id: str, batch_id: str) -> Optional[str]:
    """The trade CSV a batch produced, if it kept one."""
    batch_dir = os.path.join(OPTIMIZATIONS_DIR, opt_id, "runs", batch_id)
    if not os.path.isdir(batch_dir):
        return None
    matches = (glob.glob(os.path.join(batch_dir, "*trade*.csv")) +
               glob.glob(os.path.join(batch_dir, "*python_trade*.csv")))
    if not matches:
        return None
    # Largest file: strategies also emit small companion CSVs next to the log.
    return max(matches, key=os.path.getsize)


# =============================================================================
# SOURCE RESOLUTION
# =============================================================================

def resolve_source(spec: Dict[str, Any]) -> Tuple[np.ndarray, str, Dict[str, Any]]:
    """
    Turn a source spec into (pnl array, human label, metadata).

    Raises SourceError with a message worth showing to the user — a Monte
    Carlo run that cannot find its trades is the most common failure here, and
    "not found" alone never explains which part was missing.
    """
    kind = (spec or {}).get("type", "")

    if kind == "batch":
        opt_id = spec.get("optimization_id", "")
        batch_id = spec.get("batch_id", "")
        if not opt_id or not batch_id:
            raise SourceError("Select an optimization run and a batch.")
        path = find_batch_trade_file(opt_id, batch_id)
        if path is None:
            raise SourceError(
                f"{batch_id} kept no trade log. Only batches whose strategy "
                "wrote a *trades*.csv can be simulated.")
        pnl = read_trade_file(path)
        return pnl, f"{opt_id} · {batch_id}", {
            "type": "batch", "optimization_id": opt_id, "batch_id": batch_id,
            "file": os.path.basename(path),
        }

    if kind == "wfo":
        run_id = spec.get("run_id", "")
        if not run_id:
            raise SourceError("Select a walk-forward run.")
        config = wfo_persistence.load_wfo_config(run_id)
        if config is None:
            raise SourceError(f"Walk-forward run not found: {run_id}")

        aggregate = wfo_persistence.load_aggregate_results(run_id) or {}
        steps = [int(s["step"]) for s in aggregate.get("step_results", [])
                 if str(s.get("state", "")) == "completed"]
        if not steps:
            steps = [w.step for w in generate_windows(config)
                     if wfo_persistence.load_step_state(run_id, w.step) == "completed"]
        if not steps:
            raise SourceError(f"{run_id} has no completed steps to draw trades from.")

        trades = load_combined_oos_trades(run_id, steps)
        if trades.empty:
            raise SourceError(
                f"{run_id} completed, but none of its OOS trade logs could be read.")
        pnl = trades["pnl"].to_numpy(dtype=float)
        return pnl, f"{run_id} · combined OOS", {
            "type": "wfo", "run_id": run_id, "steps": steps,
        }

    if kind == "wfo_step":
        run_id = spec.get("run_id", "")
        step = int(spec.get("step", 0) or 0)
        if not run_id or step <= 0:
            raise SourceError("Select a walk-forward run and a step.")
        trades = load_step_trades(run_id, step)
        if trades is None or trades.empty:
            raise SourceError(f"Step {step} of {run_id} has no readable trade log.")
        return trades["pnl"].to_numpy(dtype=float), f"{run_id} · step {step}", {
            "type": "wfo_step", "run_id": run_id, "step": step,
        }

    if kind == "csv":
        path = spec.get("path", "")
        if not path:
            raise SourceError("Enter the path to a trade CSV.")
        pnl = read_trade_file(path)
        return pnl, os.path.basename(path), {"type": "csv", "path": path}

    raise SourceError(f"Unknown source type: {kind or '(none)'}")


# =============================================================================
# SOURCE DISCOVERY (for the picker)
# =============================================================================

def list_sources() -> Dict[str, List[Dict[str, Any]]]:
    """Optimization runs and walk-forward runs that could supply trades."""
    optimizations = []
    if os.path.isdir(OPTIMIZATIONS_DIR):
        for opt_id in sorted(os.listdir(OPTIMIZATIONS_DIR), reverse=True):
            opt_dir = os.path.join(OPTIMIZATIONS_DIR, opt_id)
            if not os.path.isdir(opt_dir):
                continue
            runs_dir = os.path.join(opt_dir, "runs")
            if not os.path.isdir(runs_dir):
                continue
            script_name = ""
            try:
                import json
                with open(os.path.join(opt_dir, "optimization_config.json")) as f:
                    script_name = json.load(f).get("script_name", "")
            except Exception:
                pass
            optimizations.append({
                "id": opt_id,
                "script_name": script_name,
                # Cheap: one directory listing, no per-batch stat.
                "batches": sum(1 for _ in os.scandir(runs_dir)),
            })

    wfo_runs = []
    for run in wfo_persistence.list_wfo_runs():
        wfo_runs.append({
            "run_id": run.get("run_id"),
            "strategy_name": run.get("strategy_name", ""),
            "status": run.get("status", ""),
            "total_steps": run.get("total_steps", 0),
        })

    return {"optimizations": optimizations, "wfo_runs": wfo_runs}


def list_batches(opt_id: str, top: int = 60) -> List[Dict[str, Any]]:
    """
    Batches of one optimization, best first, limited to those with a trade log.

    Ranked off the results table so the batches worth simulating are the ones
    offered — a 1,000-batch sweep would otherwise present an unusable list.
    """
    opt_dir = os.path.join(OPTIMIZATIONS_DIR, opt_id)
    if not os.path.isdir(opt_dir):
        raise SourceError(f"Optimization not found: {opt_id}")

    parquet = os.path.join(opt_dir, "optimization_results.parquet")
    csv = os.path.join(opt_dir, "optimization_results.csv")
    rows: List[Dict[str, Any]] = []

    if os.path.exists(parquet) or os.path.exists(csv):
        df = pd.read_parquet(parquet) if os.path.exists(parquet) else pd.read_csv(csv)
        if "status" in df.columns:
            df = df[df["status"] == "OK"]
        if "composite_score" in df.columns:
            df = df.assign(
                composite_score=pd.to_numeric(df["composite_score"], errors="coerce")
            ).sort_values("composite_score", ascending=False)
        for _, row in df.head(top * 2).iterrows():
            rows.append({
                "batch": str(row.get("batch", "")),
                "net_profit": _safe(row.get("Net Profit")),
                "total_trades": _safe(row.get("Total Trades")),
                "profit_factor": _safe(row.get("Profit Factor")),
                "score": _safe(row.get("composite_score")),
            })
    else:
        runs_dir = os.path.join(opt_dir, "runs")
        if os.path.isdir(runs_dir):
            for name in sorted(os.listdir(runs_dir))[: top * 2]:
                rows.append({"batch": name})

    # Only offer batches that actually kept a trade log.
    out = []
    for row in rows:
        if not row["batch"]:
            continue
        if find_batch_trade_file(opt_id, row["batch"]) is None:
            continue
        out.append(row)
        if len(out) >= top:
            break
    return out


def _safe(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(out):
        return None
    return round(out, 4)

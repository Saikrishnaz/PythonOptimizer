"""
walk_forward/selector.py
-------------------------
Deterministic parameter selection for Walk-Forward Testing.

Applies user-defined selection rules and constraints to optimization
results to select the best parameter set without subjective bias.

Supports flexible rules:
- Any metric as primary ranking
- Any metric as constraint with threshold
- Direction control (maximize/minimize) per rule
- Multiple simultaneous constraints
"""
import json
import sys
import os
import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from optimizer_engine import composite_score, priority_score, flatten_params

from .models import SelectionConfig, SelectionRule


# Columns that are never strategy parameters. Used as the fallback split when
# the run's optimization_config.json is unavailable.
NON_PARAM_COLUMNS = {
    'batch', 'status', 'composite_score', 'priority_score', 'elapsed_seconds',
    'error',
    'Total Trades', 'Net Profit', 'Win Rate %', 'Profit Factor',
    'Sharpe Ratio', 'Overall Max Drawdown', 'Average Win', 'Average Loss',
    'Net PnL After Costs', 'Brokerage Ratio %', 'Max Consecutive Losses',
    'Max Consecutive Wins', 'Largest Win', 'Largest Loss', 'Expectancy',
    'Recovery Factor', 'CAGR %',
}


def _load_declared_param_names(results_path: str) -> Optional[set]:
    """
    Read the parameter names this optimization actually declared.

    optimization_config.json sits next to the results file and records both the
    swept (optimize_params) and constant (fixed_params) parameters, so it is the
    authoritative answer to "which result columns are parameters?". Everything
    else in a results row is a metric the strategy reported, and passing those
    back into the strategy as keyword arguments would be wrong.

    Returns None when the file is missing or declares nothing usable, so the
    caller can fall back to the legacy name-based heuristic.
    """
    config_path = os.path.join(os.path.dirname(results_path), "optimization_config.json")
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except Exception:
        return None

    names = set(cfg.get("optimize_params") or {})
    # Fixed params are included so a resumed/edited run that moved a parameter
    # between the two buckets still recognises it as a parameter.
    names |= set(flatten_params(cfg.get("fixed_params") or {}))
    return names or None


def select_best_params(
    results_path: str,
    selection_config: SelectionConfig,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Apply deterministic selection rules to optimization results.
    
    Args:
        results_path: Path to optimization_results.parquet or .csv
        selection_config: User-defined selection configuration
    
    Returns:
        (selected_params, selection_info) where:
        - selected_params: dict of parameter values, or None if no candidate passes
        - selection_info: dict with rank, score, metrics, filtering details
    """
    # Load results
    if results_path.endswith(".parquet") and os.path.exists(results_path):
        df = pd.read_parquet(results_path)
    elif os.path.exists(results_path.replace(".parquet", ".csv")):
        df = pd.read_csv(results_path.replace(".parquet", ".csv"))
    else:
        return None, {"error": f"Results file not found: {results_path}"}

    # Filter to successful runs only
    if "status" in df.columns:
        df = df[df["status"] == "OK"].copy()

    if df.empty:
        return None, {"error": "No successful optimization results found"}

    total_before_filter = len(df)

    # Apply constraint rules (filters)
    for rule in selection_config.rules:
        if not rule.enabled or rule.threshold is None:
            continue

        metric_col = rule.metric
        if metric_col not in df.columns:
            continue

        df[metric_col] = pd.to_numeric(df[metric_col], errors='coerce')

        if rule.direction == "max":
            # For "max" direction with threshold: keep rows >= threshold
            df = df[df[metric_col] >= rule.threshold]
        elif rule.direction == "min":
            # For "min" direction with threshold: keep rows <= threshold  
            # (e.g., max drawdown must be <= -5000, or abs DD must be <= 20%)
            df = df[df[metric_col] <= rule.threshold]

    total_after_filter = len(df)

    if df.empty:
        return None, {
            "error": "No candidates passed selection constraints",
            "total_before_filter": total_before_filter,
            "total_after_filter": 0,
            "rules_applied": [r.to_dict() for r in selection_config.rules if r.enabled],
        }

    # Sort by primary metric
    primary = selection_config.primary_metric
    ascending = selection_config.primary_direction == "min"

    if primary == "composite_score" and primary not in df.columns:
        # Recompute composite score if not in results
        df["composite_score"] = df.apply(
            lambda row: composite_score(row.to_dict()), axis=1
        )
    elif primary == "priority_score" and primary not in df.columns:
        df["priority_score"] = df.apply(
            lambda row: priority_score(row.to_dict()), axis=1
        )

    if primary in df.columns:
        df[primary] = pd.to_numeric(df[primary], errors='coerce')
        df = df.sort_values(primary, ascending=ascending, na_position='last')
    else:
        # Fall back to composite_score
        if "composite_score" in df.columns:
            df = df.sort_values("composite_score", ascending=False, na_position='last')

    # Select the top row
    best = df.iloc[0]
    rank = 1

    # Split the winning row into strategy parameters and reported metrics.
    #
    # Prefer the run's declared parameter names — a strategy's statistics tab is
    # open-ended (a credit spread reports "ROI % (2024)", "Hedge Net PnL", ...),
    # and treating an unrecognised metric as a parameter means feeding it back
    # into the strategy on the OOS run. Fall back to the legacy name-based split
    # when the declaration is unavailable.
    declared_params = _load_declared_param_names(results_path)

    params = {}
    metrics = {}
    for col in best.index:
        val = best[col]
        # Convert numpy types to Python types
        if isinstance(val, (np.integer,)):
            val = int(val)
        elif isinstance(val, (np.floating,)):
            val = float(val)
            if np.isnan(val) or np.isinf(val):
                val = 0.0
        elif isinstance(val, np.bool_):
            val = bool(val)

        if declared_params is not None:
            # A dotted column is a flattened nested parameter (e.g.
            # "Backtest_period.start_date"). It cannot be passed as a keyword
            # argument, and the runner re-supplies the nested dict anyway.
            is_param = col in declared_params and "." not in col
        else:
            is_param = not (col in NON_PARAM_COLUMNS or col.startswith("_"))

        if is_param:
            params[col] = val
        else:
            metrics[col] = val

    score = float(best.get(primary, best.get("composite_score", 0)) or 0)

    selection_info = {
        "rank": rank,
        "score": score,
        "primary_metric": primary,
        "batch": str(best.get("batch", "")),
        "total_before_filter": total_before_filter,
        "total_after_filter": total_after_filter,
        "metrics": metrics,
        "rules_applied": [r.to_dict() for r in selection_config.rules if r.enabled],
    }

    return params, selection_info

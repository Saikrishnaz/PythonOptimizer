"""
walk_forward/persistence.py
-----------------------------
File I/O, state management, and resume support for Walk-Forward Testing.

Directory structure:
    wfo_runs/
        wfo_<run_id>/
            config.json
            wfo_progress.json
            step_001/
                state.json
                selected_parameters.json
                is_metrics.json
                oos_metrics.json
                oos_trades.csv
                oos_equity.csv
            step_002/
                ...
            aggregate/
                combined_oos_trades.csv
                combined_oos_equity.csv
                step_results.csv
                parameter_stability.csv
                robustness_score.json
                candidates.json
                final_summary.json
"""
import json
import os
import shutil
from typing import List, Dict, Any, Optional

import pandas as pd
import numpy as np

from .models import (
    WFOConfig, WFOWindow, WFOStepResult, StepState
)


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WFO_RUNS_DIR = os.path.join(SCRIPT_DIR, "wfo_runs")


def _json_default(obj):
    """JSON serializer for numpy types."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        val = float(obj)
        if np.isnan(val) or np.isinf(val):
            return None
        return val
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.bool_):
        return bool(obj)
    return str(obj)


# =============================================================================
# DIRECTORY MANAGEMENT
# =============================================================================

def get_wfo_dir(run_id: str) -> str:
    """Get the directory for a WFO run."""
    return os.path.join(WFO_RUNS_DIR, f"wfo_{run_id}")


def get_step_dir(run_id: str, step: int) -> str:
    """Get the directory for a specific step."""
    return os.path.join(get_wfo_dir(run_id), f"step_{step:03d}")


def get_aggregate_dir(run_id: str) -> str:
    """Get the aggregate results directory."""
    return os.path.join(get_wfo_dir(run_id), "aggregate")


def ensure_dirs(run_id: str, num_steps: int):
    """Create all necessary directories for a WFO run."""
    wfo_dir = get_wfo_dir(run_id)
    os.makedirs(wfo_dir, exist_ok=True)
    for i in range(1, num_steps + 1):
        os.makedirs(get_step_dir(run_id, i), exist_ok=True)
    os.makedirs(get_aggregate_dir(run_id), exist_ok=True)


# =============================================================================
# CONFIG PERSISTENCE
# =============================================================================

def save_wfo_config(run_id: str, config: WFOConfig):
    """Save WFO configuration to disk."""
    wfo_dir = get_wfo_dir(run_id)
    os.makedirs(wfo_dir, exist_ok=True)
    path = os.path.join(wfo_dir, "config.json")
    with open(path, "w") as f:
        json.dump(config.to_dict(), f, indent=2, default=_json_default)


def load_wfo_config(run_id: str) -> Optional[WFOConfig]:
    """Load WFO configuration from disk."""
    path = os.path.join(get_wfo_dir(run_id), "config.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return WFOConfig.from_dict(data)


# =============================================================================
# STEP STATE PERSISTENCE
# =============================================================================

def save_step_state(run_id: str, step: int, state: str, error: str = None):
    """Save step state to disk."""
    step_dir = get_step_dir(run_id, step)
    os.makedirs(step_dir, exist_ok=True)
    data = {"state": state, "step": step}
    if error:
        data["error"] = error
    path = os.path.join(step_dir, "state.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_step_state(run_id: str, step: int) -> str:
    """Load step state from disk."""
    path = os.path.join(get_step_dir(run_id, step), "state.json")
    if not os.path.exists(path):
        return StepState.PENDING.value
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("state", StepState.PENDING.value)
    except Exception:
        return StepState.PENDING.value


def is_step_completed(run_id: str, step: int) -> bool:
    """Check if a step is fully completed."""
    return load_step_state(run_id, step) == StepState.COMPLETED.value


def get_completed_steps(run_id: str, num_steps: int) -> List[int]:
    """Get list of completed step numbers."""
    completed = []
    for i in range(1, num_steps + 1):
        if is_step_completed(run_id, i):
            completed.append(i)
    return completed


# =============================================================================
# STEP RESULT PERSISTENCE
# =============================================================================

def save_selected_params(run_id: str, step: int, params: Dict[str, Any]):
    """Save selected parameters for a step."""
    path = os.path.join(get_step_dir(run_id, step), "selected_parameters.json")
    with open(path, "w") as f:
        json.dump(params, f, indent=2, default=_json_default)


def load_selected_params(run_id: str, step: int) -> Optional[Dict[str, Any]]:
    """Load selected parameters for a step."""
    path = os.path.join(get_step_dir(run_id, step), "selected_parameters.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_is_metrics(run_id: str, step: int, metrics: Dict[str, Any]):
    """Save IS metrics for a step."""
    path = os.path.join(get_step_dir(run_id, step), "is_metrics.json")
    with open(path, "w") as f:
        json.dump(metrics, f, indent=2, default=_json_default)


def save_oos_metrics(run_id: str, step: int, metrics: Dict[str, Any]):
    """Save OOS metrics for a step."""
    path = os.path.join(get_step_dir(run_id, step), "oos_metrics.json")
    with open(path, "w") as f:
        json.dump(metrics, f, indent=2, default=_json_default)


def load_oos_metrics(run_id: str, step: int) -> Optional[Dict[str, Any]]:
    """Load OOS metrics for a step."""
    path = os.path.join(get_step_dir(run_id, step), "oos_metrics.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_oos_trades(run_id: str, step: int, trades_df: pd.DataFrame):
    """Save OOS trades CSV."""
    path = os.path.join(get_step_dir(run_id, step), "oos_trades.csv")
    trades_df.to_csv(path, index=False)


def save_oos_equity(run_id: str, step: int, equity_data: list):
    """Save OOS equity curve."""
    path = os.path.join(get_step_dir(run_id, step), "oos_equity.csv")
    df = pd.DataFrame(equity_data)
    df.to_csv(path, index=False)


# =============================================================================
# PROGRESS PERSISTENCE (FOR SSE)
# =============================================================================

def save_wfo_progress(run_id: str, progress: Dict[str, Any]):
    """Save WFO progress for SSE streaming."""
    wfo_dir = get_wfo_dir(run_id)
    os.makedirs(wfo_dir, exist_ok=True)
    path = os.path.join(wfo_dir, "wfo_progress.json")
    with open(path, "w") as f:
        json.dump(progress, f, indent=2, default=_json_default)


def load_wfo_progress(run_id: str) -> Optional[Dict[str, Any]]:
    """Load WFO progress."""
    path = os.path.join(get_wfo_dir(run_id), "wfo_progress.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


# =============================================================================
# AGGREGATE PERSISTENCE
# =============================================================================

def save_aggregate_results(
    run_id: str,
    step_results: List[Dict],
    oos_aggregate: Dict,
    stability: Dict,
    robustness: Dict,
    candidates: List[Dict],
):
    """Save all aggregate results."""
    agg_dir = get_aggregate_dir(run_id)
    os.makedirs(agg_dir, exist_ok=True)

    # Step results table
    if step_results:
        df = pd.DataFrame(step_results)
        df.to_csv(os.path.join(agg_dir, "step_results.csv"), index=False)

    # Parameter stability
    stability_params = stability.get("parameters", [])
    if stability_params:
        df = pd.DataFrame(stability_params)
        df.to_csv(os.path.join(agg_dir, "parameter_stability.csv"), index=False)

    # Robustness score
    with open(os.path.join(agg_dir, "robustness_score.json"), "w") as f:
        json.dump(robustness, f, indent=2, default=_json_default)

    # Candidates
    with open(os.path.join(agg_dir, "candidates.json"), "w") as f:
        json.dump(candidates, f, indent=2, default=_json_default)

    # Final summary
    summary = {
        "oos_aggregate": oos_aggregate,
        "robustness": robustness,
        "stability_score": stability.get("overall_stability_score", 0),
        "num_candidates": len(candidates),
    }
    with open(os.path.join(agg_dir, "final_summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=_json_default)


def load_aggregate_results(run_id: str) -> Optional[Dict[str, Any]]:
    """Load final summary and all aggregate data."""
    agg_dir = get_aggregate_dir(run_id)
    summary_path = os.path.join(agg_dir, "final_summary.json")
    if not os.path.exists(summary_path):
        return None

    result = {}
    with open(summary_path) as f:
        result["summary"] = json.load(f)

    # Load robustness
    robustness_path = os.path.join(agg_dir, "robustness_score.json")
    if os.path.exists(robustness_path):
        with open(robustness_path) as f:
            result["robustness"] = json.load(f)

    # Load candidates
    candidates_path = os.path.join(agg_dir, "candidates.json")
    if os.path.exists(candidates_path):
        with open(candidates_path) as f:
            result["candidates"] = json.load(f)

    # Load stability
    stability_path = os.path.join(agg_dir, "parameter_stability.csv")
    if os.path.exists(stability_path):
        df = pd.read_csv(stability_path)
        result["stability"] = df.to_dict(orient="records")

    # Load step results
    steps_path = os.path.join(agg_dir, "step_results.csv")
    if os.path.exists(steps_path):
        df = pd.read_csv(steps_path)
        result["step_results"] = df.replace({np.nan: None}).to_dict(orient="records")

    return result


# =============================================================================
# WFO RUN LISTING
# =============================================================================

def list_wfo_runs() -> List[Dict[str, Any]]:
    """List all WFO runs with basic info."""
    if not os.path.exists(WFO_RUNS_DIR):
        return []

    runs = []
    for name in sorted(os.listdir(WFO_RUNS_DIR), reverse=True):
        if not name.startswith("wfo_"):
            continue
        run_dir = os.path.join(WFO_RUNS_DIR, name)
        if not os.path.isdir(run_dir):
            continue

        run_id = name[4:]  # Remove 'wfo_' prefix
        info = {"run_id": run_id, "dir_name": name}

        # Load config
        config_path = os.path.join(run_dir, "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    cfg = json.load(f)
                info["strategy_name"] = cfg.get("strategy_name", "")
                info["window_mode"] = cfg.get("window_mode", "")
                info["created_at"] = cfg.get("created_at", "")
                info["wfo_start"] = cfg.get("wfo_start", "")
                info["wfo_end"] = cfg.get("wfo_end", "")
            except Exception:
                pass

        # Load progress
        progress = load_wfo_progress(run_id)
        if progress:
            info["status"] = progress.get("status", "unknown")
            info["current_step"] = progress.get("current_step", 0)
            info["total_steps"] = progress.get("total_steps", 0)
        else:
            info["status"] = "unknown"

        # Directory size
        total_size = 0
        for dirpath, _, filenames in os.walk(run_dir):
            for f in filenames:
                total_size += os.path.getsize(os.path.join(dirpath, f))
        info["size_mb"] = round(total_size / (1024 * 1024), 2)

        runs.append(info)

    return runs


def delete_wfo_run(run_id: str) -> bool:
    """Delete a WFO run and all its data."""
    wfo_dir = get_wfo_dir(run_id)
    if os.path.exists(wfo_dir):
        shutil.rmtree(wfo_dir)
        return True
    return False

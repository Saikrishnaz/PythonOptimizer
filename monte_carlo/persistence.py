"""
monte_carlo/persistence.py
--------------------------
Storage for Monte Carlo runs.

    monte_carlo_runs/
        mc_<run_id>/
            result.json     the full result document

A run is small — a few hundred kilobytes of summarised distributions, never
the simulated paths themselves — so one JSON file per run is enough and the
listing can read them directly.
"""
import json
import os
import shutil
from typing import Any, Dict, List, Optional

import numpy as np


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MC_RUNS_DIR = os.path.join(SCRIPT_DIR, "monte_carlo_runs")


def _json_default(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        value = float(obj)
        return None if (np.isnan(value) or np.isinf(value)) else value
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.bool_):
        return bool(obj)
    return str(obj)


def get_run_dir(run_id: str) -> str:
    return os.path.join(MC_RUNS_DIR, f"mc_{run_id}")


def save_run(run_id: str, result: Dict[str, Any]) -> str:
    run_dir = get_run_dir(run_id)
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "result.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=_json_default)
    return path


def load_run(run_id: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(get_run_dir(run_id), "result.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_runs() -> List[Dict[str, Any]]:
    """Newest first, with just enough detail for a history table."""
    if not os.path.isdir(MC_RUNS_DIR):
        return []

    runs = []
    for name in sorted(os.listdir(MC_RUNS_DIR), reverse=True):
        if not name.startswith("mc_"):
            continue
        run_id = name[3:]
        data = load_run(run_id)
        if not data:
            continue
        config = data.get("config", {})
        first_method = next(iter(data.get("results", {}).values()), {})
        runs.append({
            "run_id": run_id,
            "created_at": data.get("created_at", ""),
            "source_label": data.get("source_label", ""),
            "source": data.get("source", {}),
            "methods": config.get("methods", []),
            "simulations": config.get("simulations", 0),
            "total_trades": data.get("input", {}).get("total_trades", 0),
            "prob_profit": (first_method.get("probabilities", {}) or {}).get("profit"),
            "elapsed_seconds": data.get("elapsed_seconds"),
        })
    return runs


def delete_run(run_id: str) -> bool:
    run_dir = get_run_dir(run_id)
    if os.path.isdir(run_dir):
        shutil.rmtree(run_dir)
        return True
    return False

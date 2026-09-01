"""
optimizer_engine.py
-------------------
Core optimization engine supporting Grid, Random, and Bayesian search modes.

Features:
- Generates parameter combinations based on user-defined ranges
- Runs batches in parallel via subprocess workers
- Tracks live progress (progress.json), counted from what batches actually
  produced on disk so the dashboard's bar can't run ahead of the real work
- Resume support (skips completed batches, re-queues failed/unfinished ones)
- Stores results in Parquet for efficient storage
- Supports multiple scoring/ranking metrics
"""
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
import itertools
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import numpy as np


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Hard ceiling on a single batch. Also the basis for the worst-case runtime
# estimate reported in progress.json.
BATCH_TIMEOUT_SECONDS = 3600


def json_default(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)


RANK_METRIC_MAP = {
    "net_profit": "Net Profit",
    "sharpe": "Sharpe Ratio",
    "profit_factor": "Profit Factor",
    "win_rate": "Win Rate %",
    "total_trades": "Total Trades",
}


def composite_score(metrics: dict) -> float:
    """Blend risk-adjusted return, consistency, and drawdown-adjusted profit."""
    try:
        sharpe = float(metrics.get("Sharpe Ratio", 0) or 0)
        pf = float(metrics.get("Profit Factor", 0) or 0)
        net_profit = float(metrics.get("Net Profit", 0) or 0)
        max_dd = abs(float(metrics.get("Overall Max Drawdown", 1) or 1)) or 1
        dd_adjusted = net_profit / max_dd
        return sharpe * 2 + pf * 1.5 + dd_adjusted
    except Exception:
        return float("-inf")


def priority_score(metrics: dict) -> float:
    """
    Ranks based on: Max Trades × Profit × Brokerage Efficiency × Profit Factor.
    """
    try:
        trades = float(metrics.get("Total Trades", 0) or 0)
        net_profit = float(metrics.get("Net PnL After Costs", 0) or 0)
        if net_profit == 0:
            net_profit = float(metrics.get("Net Profit", 0) or 0)
        brokerage_ratio = float(metrics.get("Brokerage Ratio %", 50) or 50)
        profit_factor = float(metrics.get("Profit Factor", 0) or 0)

        if trades < 10:
            return float("-inf")
        if net_profit <= 0:
            return net_profit

        trade_bonus = math.log2(max(trades, 1))
        brokerage_eff = max(0.01, (100 - min(brokerage_ratio, 99)) / 100)
        pf_bonus = min(profit_factor, 5.0)

        return net_profit * trade_bonus * brokerage_eff * pf_bonus
    except Exception:
        return float("-inf")

# =============================================================================
# DRAWDOWN OPTIMIZATION SCORING
# =============================================================================

def compute_trading_days(fixed_params: dict) -> int:
    """Estimate trading days from start_date/end_date in fixed_params."""
    try:
        start = end = None
        for key, val in fixed_params.items():
            k = key.lower()
            if isinstance(val, dict):
                # Nested date params (e.g., Backtest_period.start_date)
                for sk, sv in val.items():
                    if 'start' in sk.lower() and 'date' in sk.lower():
                        start = str(sv)
                    elif 'end' in sk.lower() and 'date' in sk.lower():
                        end = str(sv)
            elif 'start' in k and 'date' in k:
                start = str(val)
            elif 'end' in k and 'date' in k:
                end = str(val)
        
        if start and end:
            from datetime import datetime as dt
            s = dt.strptime(start[:10], "%Y-%m-%d")
            e = dt.strptime(end[:10], "%Y-%m-%d")
            cal_days = (e - s).days
            return max(cal_days, 1)
    except Exception:
        pass
    return 365  # Safe fallback: assume 1 year


def drawdown_score(
    metrics: dict,
    trading_days: int,
    min_tpd: float,
    target_tpd: float,
    existing_score: float,
) -> float:
    """
    Score that prioritizes lowest Max Drawdown while enforcing
    minimum trading activity requirements.
    
    Returns:
        Combined score (higher = better, lower drawdown = higher score)
    """
    try:
        total_trades = float(metrics.get("Total Trades", 0) or 0)
        max_dd = abs(float(metrics.get("Overall Max Drawdown", 0) or 0))
        net_profit = float(metrics.get("Net Profit", 0) or 0)
        
        avg_tpd = total_trades / max(trading_days, 1)
        
        # Gate: below minimum trades/day -> rejected
        if avg_tpd < min_tpd:
            return float("-inf")
        
        # Reject losing strategies entirely
        if net_profit <= 0:
            return net_profit - max_dd * 100
        
        # Trade activity penalty ramp: min_tpd->target_tpd maps to 0.3->1.0
        if target_tpd > min_tpd and avg_tpd < target_tpd:
            trade_factor = 0.3 + 0.7 * (avg_tpd - min_tpd) / (target_tpd - min_tpd)
        else:
            trade_factor = 1.0
        
        # Primary: lower drawdown = higher score (inverted)
        dd_score = (1.0 / (1.0 + max_dd)) * trade_factor * 10000
        
        # Tie-breaker: existing ranking metric (tiny weight)
        tiebreak = max(existing_score, 0) * 0.001
        
        return dd_score + tiebreak
    except Exception:
        return float("-inf")


def auto_detect_trade_thresholds(rows: list, trading_days: int) -> tuple:
    """
    Auto-compute min/target trades-per-day from actual optimization results.
    Uses percentile-based thresholds for intelligent filtering.
    
    Returns:
        (min_tpd, target_tpd)
    """
    try:
        tpd_values = []
        for r in rows:
            trades = float(r.get("Total Trades", 0) or 0)
            if trades > 0:
                tpd_values.append(trades / max(trading_days, 1))
        
        if len(tpd_values) < 3:
            return 1.0, 5.0  # Safe fallback
        
        arr = np.array(tpd_values)
        p25 = float(np.percentile(arr, 25))
        p75 = float(np.percentile(arr, 75))
        
        # Minimum = 25th percentile (reject bottom quartile)
        min_tpd = max(0.5, round(p25, 1))
        # Target = 75th percentile (prefer active strategies)
        target_tpd = max(min_tpd + 0.5, round(p75, 1))
        
        return min_tpd, target_tpd
    except Exception:
        return 1.0, 5.0


# =============================================================================
# PARAMETER GENERATION
# =============================================================================

def grid_axes(optimize_params: dict):
    """Parameter names and the discrete value list for each, in declared order."""
    param_names = []
    param_values = []

    for name, spec in optimize_params.items():
        param_names.append(name)
        if "choices" in spec:
            param_values.append(list(spec["choices"]))
        elif "min" in spec and "max" in spec:
            # A zero/None step would spin forever; treat it as the default 1.
            step = spec.get("step", 1) or 1
            ptype = spec.get("type", "float")
            vals = []
            v = spec["min"]
            while v <= spec["max"] + 1e-9:
                if ptype == "int":
                    vals.append(int(round(v)))
                else:
                    vals.append(round(v, 6))
                v += step
            param_values.append(vals)
        else:
            param_values.append([spec.get("value", spec.get("min", 0))])

    return param_names, param_values


def grid_size(param_values: list) -> int:
    """How many combinations the full cartesian product would contain."""
    return math.prod(len(v) for v in param_values)


def decode_grid_index(index: int, param_names: list, param_values: list) -> dict:
    """
    Map a flat index onto one grid combination.

    Mixed-radix decode with the last axis varying fastest, which is exactly
    itertools.product's ordering — so index i always yields the same
    combination the full product would have produced at position i.
    """
    combo = {}
    for name, values in zip(reversed(param_names), reversed(param_values)):
        index, pos = divmod(index, len(values))
        combo[name] = values[pos]
    return {name: combo[name] for name in param_names}


def generate_grid_params(optimize_params: dict, limit: int = None, seed: int = 42) -> list:
    """
    Generate grid combinations, at most `limit` of them.

    With a limit, combinations are drawn by sampling *indices* into the grid
    and decoding them, so asking for 1000 points costs 1000 dicts no matter
    how large the grid is. Building the whole product first — a million dicts
    and several hundred MB — only to throw all but 1000 away was the single
    biggest memory cost in a grid run, and it also made large search spaces
    fail outright instead of being sampled.

    Called without a limit the behaviour is unchanged: the full product, in
    itertools order, with the million-combination guard.
    """
    param_names, param_values = grid_axes(optimize_params)
    total_combinations = grid_size(param_values)

    if limit is not None and 0 < limit < total_combinations:
        rng = random.Random(seed)
        picked = sorted(rng.sample(range(total_combinations), limit))
        return [decode_grid_index(i, param_names, param_values) for i in picked]

    if total_combinations > 1_000_000:
        raise ValueError(
            f"Grid search space is too large ({total_combinations:,} combinations). "
            f"This will cause a MemoryError. Please use 'Random' or 'Bayesian' "
            f"optimization mode, or reduce your parameter ranges."
        )

    combos = []
    for combo in itertools.product(*param_values):
        combos.append(dict(zip(param_names, combo)))

    return combos


def generate_random_params(optimize_params: dict, n: int, seed: int = 42) -> list:
    """Generate n random parameter combinations."""
    rng = random.Random(seed)
    combos = []
    seen = set()
    attempts = 0
    max_attempts = n * 20
    
    while len(combos) < n and attempts < max_attempts:
        attempts += 1
        combo = {}
        for name, spec in optimize_params.items():
            if "choices" in spec:
                combo[name] = rng.choice(spec["choices"])
            elif "min" in spec and "max" in spec:
                ptype = spec.get("type", "float")
                if ptype == "int":
                    combo[name] = rng.randint(int(spec["min"]), int(spec["max"]))
                else:
                    step = spec.get("step", None)
                    if step and step > 0:
                        # Snap to step grid
                        steps_count = int((spec["max"] - spec["min"]) / step)
                        chosen_step = rng.randint(0, steps_count)
                        combo[name] = round(spec["min"] + chosen_step * step, 6)
                    else:
                        combo[name] = round(rng.uniform(spec["min"], spec["max"]), 6)
            else:
                combo[name] = spec.get("value", 0)
        
        key = json.dumps(combo, sort_keys=True, default=json_default)
        if key not in seen:
            seen.add(key)
            combos.append(combo)
    
    return combos


# How many points are drawn quasi-randomly before the surrogate model takes
# over. Below roughly this many observations a GP has nothing useful to say.
BAYESIAN_INITIAL_POINTS = 10


def build_bayesian_optimizer(optimize_params: dict, seed: int = 42,
                             n_initial_points: int = BAYESIAN_INITIAL_POINTS):
    """
    Build a scikit-optimize Optimizer over the parameter space.

    Returns (optimizer, param_names). The optimizer is None when
    scikit-optimize isn't installed, which is the caller's cue to fall back
    to random search.
    """
    param_names = list(optimize_params)
    try:
        from skopt.space import Real, Integer, Categorical
        from skopt import Optimizer as SkoptOptimizer
    except ImportError:
        print("WARNING: scikit-optimize not installed. Falling back to random search.")
        return None, param_names

    dimensions = []
    for name, spec in optimize_params.items():
        if "choices" in spec:
            dimensions.append(Categorical(spec["choices"], name=name))
        elif "min" in spec and "max" in spec:
            ptype = spec.get("type", "float")
            if ptype == "int":
                dimensions.append(Integer(int(spec["min"]), int(spec["max"]), name=name))
            else:
                dimensions.append(Real(float(spec["min"]), float(spec["max"]), name=name))
        else:
            dimensions.append(Categorical([spec.get("value", 0)], name=name))

    opt = SkoptOptimizer(
        dimensions,
        random_state=seed,
        n_initial_points=max(1, min(n_initial_points, 1000)),
    )
    return opt, param_names


def ask_bayesian_points(opt, param_names: list, count: int) -> list:
    """
    Ask the optimizer for `count` points, returned as parameter dicts.

    Once the search converges skopt warns every time it proposes a point it
    has already evaluated and substitutes a random one. That is useful to
    know but it fires per point, so a long run would bury the log in
    thousands of identical warnings; they are collapsed into one line.
    """
    if count <= 0:
        return []

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        points = opt.ask(n_points=count)

    repeats = 0
    for w in caught:
        if "evaluated at point" in str(w.message):
            repeats += 1
        else:
            print(f"[optimizer] skopt: {w.message}")
    if repeats:
        print(f"[optimizer] Bayesian search has converged — {repeats}/{count} points "
              f"in this wave fell back to random exploration")

    # ask(n_points=k) returns a list of points; guard against a bare point.
    if points and not isinstance(points[0], (list, tuple)):
        points = [points]
    return [dict(zip(param_names, point)) for point in points]


def generate_bayesian_params(optimize_params: dict, n: int, seed: int = 42) -> list:
    """
    Draw n points from a Bayesian optimizer without any result feedback.

    This is only the cold-start sampler. Real Bayesian search needs the
    ask -> evaluate -> tell loop, which run_optimization() drives; a caller
    that just wants n points up front gets quasi-random coverage of the space.

    (This function used to hard-cap at 10 points regardless of n, which is why
    a 200-iteration Bayesian run produced exactly 10 batches.)
    """
    opt, param_names = build_bayesian_optimizer(optimize_params, seed, n_initial_points=n)
    if opt is None:
        return generate_random_params(optimize_params, n, seed)
    return ask_bayesian_points(opt, param_names, n)


def score_metrics(metrics: dict, ranking_metric: str) -> float:
    """The ranking score for one batch's metrics, per the selected metric."""
    if ranking_metric == "priority":
        return priority_score(metrics)
    if ranking_metric == "composite":
        return composite_score(metrics)
    if ranking_metric in RANK_METRIC_MAP:
        try:
            return float(metrics.get(RANK_METRIC_MAP[ranking_metric], 0) or 0)
        except (TypeError, ValueError):
            return float("-inf")
    return composite_score(metrics)


def read_batch_score(batch_dir: str, ranking_metric: str):
    """
    The ranking score a finished batch earned, or None if it has no result.

    Returns -inf for a batch that ran but failed, so the caller can treat it
    as the worst possible outcome rather than pretending it never happened.
    """
    try:
        with open(os.path.join(batch_dir, "metrics.json")) as f:
            data = json.load(f)
    except Exception:
        return None
    if not data.get("status", {}).get("ok"):
        return float("-inf")
    return score_metrics(data.get("metrics") or {}, ranking_metric)


# =============================================================================
# BATCH MANAGEMENT
# =============================================================================

def write_batch_input(runs_dir: str, batch_id: int, cfg: dict) -> str:
    """Write params.json for a single batch."""
    batch_dir = os.path.join(runs_dir, f"batch_{batch_id:04d}")
    os.makedirs(batch_dir, exist_ok=True)
    with open(os.path.join(batch_dir, "params.json"), "w") as f:
        json.dump(cfg, f, indent=2, default=json_default)
    return batch_dir


def run_one(batch_dir: str, strategy_path: str, entry_style: str,
            config_class_name: str, python_exe: str, capture_stdout: bool = True):
    """
    Run a single batch in a subprocess.

    capture_stdout=False discards the child's stdout at the pipe instead of
    buffering it. Nothing reads a batch's stdout, and holding it costs real
    memory across a large sweep.
    """
    env = os.environ.copy()
    env["STRATEGY_MODULE_PATH"] = os.path.abspath(strategy_path)
    env["ENTRY_STYLE"] = entry_style
    env["CONFIG_CLASS_NAME"] = config_class_name or ""

    worker_path = os.path.join(SCRIPT_DIR, "worker.py")

    # Decode the child's output as UTF-8 with replacement rather than the
    # Windows locale codec. Strategies that print progress bars or non-ASCII
    # symbols would otherwise kill subprocess's reader thread with a
    # UnicodeDecodeError, losing the batch's output for no good reason.
    result = subprocess.run(
        [python_exe, worker_path, os.path.abspath(batch_dir)],
        env=env, text=True,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace",
        timeout=BATCH_TIMEOUT_SECONDS,
    )
    return batch_dir, result


def run_one_slim(batch_dir: str, strategy_path: str, entry_style: str,
                 config_class_name: str, python_exe: str):
    """
    Pool-facing wrapper around run_one(), returning only what the engine uses:
    (batch_dir, returncode, stderr tail, wall seconds).

    The engine holds one future per batch for the whole run, and a future
    keeps its result alive. Returning the full CompletedProcess meant every
    worker's captured stdout and stderr stayed resident simultaneously — for a
    1000-batch sweep of a strategy that prints progress, hundreds of MB of
    text nobody ever reads. This caps it at 2 KB per batch.

    A timeout is reported as an ordinary failure rather than raised, so it
    travels back as data instead of an exception through the pool.
    """
    started = time.time()
    try:
        _, result = run_one(batch_dir, strategy_path, entry_style,
                            config_class_name, python_exe, capture_stdout=False)
    except subprocess.TimeoutExpired:
        return (batch_dir, -1,
                f"Batch exceeded the {BATCH_TIMEOUT_SECONDS}s per-batch limit",
                time.time() - started)

    return (batch_dir, result.returncode, (result.stderr or "")[-2000:],
            time.time() - started)


def flatten_params(params: dict) -> dict:
    """Flatten nested dicts for CSV/DataFrame storage."""
    flat = {}
    for k, v in params.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                flat[f"{k}.{kk}"] = vv
        else:
            flat[k] = v
    return flat


# =============================================================================
# PROGRESS / RESUME HELPERS
# =============================================================================

def atomic_write_json(path: str, payload: dict):
    """
    Write JSON through a temp file + os.replace.

    progress.json is read once a second by the dashboard's SSE stream while
    it is being rewritten after every batch; a plain open(..., "w") lets the
    reader catch the file mid-truncation and drop that update.

    On Windows os.replace fails while another process holds the destination
    open, so the swap is retried briefly and then falls back to a direct
    write. Losing atomicity on a progress file is a far better outcome than
    letting the optimization thread die over it.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=json_default)

    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))

    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=json_default)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def read_batch_state(batch_dir: str) -> str:
    """
    Ground truth for one batch, read from disk: 'ok' | 'failed' | 'pending'.

    'pending' covers both never-started and started-but-produced-nothing,
    which is exactly the set a resume needs to re-run. A corrupt/partial
    metrics.json counts as pending so it gets retried rather than trusted.
    """
    metrics_file = os.path.join(batch_dir, "metrics.json")
    if not os.path.isfile(metrics_file):
        return "pending"
    try:
        with open(metrics_file) as f:
            data = json.load(f)
    except Exception:
        return "pending"
    return "ok" if data.get("status", {}).get("ok") else "failed"


def scan_batch_states(runs_dir: str, total_batches: int) -> dict:
    """Tally every batch's state by reading its metrics.json."""
    counts = {"ok": 0, "failed": 0, "pending": 0}
    for i in range(total_batches):
        counts[read_batch_state(os.path.join(runs_dir, f"batch_{i + 1:04d}"))] += 1
    return counts


def count_finished_batches(runs_dir: str, total_batches: int) -> int:
    """
    Stat-only tally of batches that left a metrics.json behind.

    Cheap enough to call for every run in the history listing, where the
    ok/failed split isn't needed — only how far each run actually got.
    """
    if not os.path.isdir(runs_dir):
        return 0
    return sum(
        1 for i in range(total_batches)
        if os.path.isfile(os.path.join(runs_dir, f"batch_{i + 1:04d}", "metrics.json"))
    )


def write_crash_metrics(batch_dir: str, error: str):
    """
    Record a failure for a batch whose worker died without writing metrics.json
    (subprocess timeout, hard crash, killed pool worker).

    Without this the batch is invisible everywhere downstream: it never
    reaches optimization_results.csv and nothing on disk explains why the
    batch count and the result count disagree.
    """
    metrics_path = os.path.join(batch_dir, "metrics.json")
    if os.path.isfile(metrics_path):
        return
    params = {}
    try:
        with open(os.path.join(batch_dir, "params.json")) as f:
            params = json.load(f)
    except Exception:
        pass
    try:
        atomic_write_json(metrics_path, {
            "status": {"ok": False, "error": error},
            "metrics": {},
            "params": params,
            "elapsed_seconds": 0,
            "crashed": True,
        })
    except Exception:
        pass


def make_progress_fields(total: int, ok: int, failed: int) -> dict:
    """The derived counters the dashboard's progress bar reads."""
    completed = ok + failed
    remaining = max(total - completed, 0)
    return {
        "completed": completed,
        "ok": ok,
        "failed": failed,
        "pending": remaining,
        "remaining": remaining,
        "percent": round(completed / total * 100, 1) if total else 0.0,
        "resumable": remaining > 0,
    }


def save_sweep_plan(path: str, combos: list):
    """
    Record the exact parameter plan a run is executing.

    Grid and random plans can be regenerated from the seed, but Bayesian
    points are chosen from earlier results and can never be reproduced. Since
    resume matches results to batch numbers by position, the plan has to be
    on disk or a resumed Bayesian run would silently pair batch folders with
    different parameters.
    """
    try:
        atomic_write_json(path, {"count": len(combos), "combos": combos})
    except Exception as e:
        print(f"[optimizer] Could not write sweep_plan.json: {e}")


def load_sweep_plan(path: str):
    """The recorded plan for a run, or None if it predates plan recording."""
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    combos = data.get("combos") if isinstance(data, dict) else data
    return combos if isinstance(combos, list) and combos else None


def build_progress_snapshot(opt_dir: str) -> dict:
    """
    Rebuild a run's progress from what is on disk.

    Used after a server restart, when the counters held by the thread that
    was driving the optimization are gone but every batch folder is still
    there. Also repairs runs recorded by the old counter, which incremented
    'completed' even for batches that produced no result at all.
    """
    runs_dir = os.path.join(opt_dir, "runs")
    progress_path = os.path.join(opt_dir, "progress.json")

    progress = {}
    if os.path.isfile(progress_path):
        try:
            with open(progress_path) as f:
                progress = json.load(f)
        except Exception:
            progress = {}
    if not isinstance(progress, dict):
        progress = {}

    total = int(progress.get("total") or 0)
    if not total:
        config_path = os.path.join(opt_dir, "optimization_config.json")
        try:
            with open(config_path) as f:
                total = int(json.load(f).get("num_iterations") or 0)
        except Exception:
            total = 0
    if not total and os.path.isdir(runs_dir):
        total = len([d for d in os.listdir(runs_dir) if d.startswith("batch_")])

    counts = scan_batch_states(runs_dir, total) if total else {"ok": 0, "failed": 0, "pending": 0}
    progress["total"] = total
    progress.update(make_progress_fields(total, counts["ok"], counts["failed"]))
    # A resume retries failures too, so they are part of what is left to run.
    progress["remaining"] = counts["pending"] + counts["failed"]
    progress["resumable"] = progress["remaining"] > 0
    return progress


# =============================================================================
# OPTIMIZATION RUNNER
# =============================================================================

def run_optimization(
    script_path: str,
    entry_style: str,
    config_class_name: str,
    fixed_params: dict,
    optimize_params: dict,
    mode: str = "random",
    num_iterations: int = 100,
    num_workers: int = 2,
    seed: int = 42,
    ranking_metric: str = "priority",
    top_n: int = 10,
    optimization_id: str = None,
    progress_callback=None,
    drawdown_optimization: str = "disabled",
    dd_min_trades_per_day: float = 2.0,
    dd_target_trades_per_day: float = 8.0,
    resume: bool = False,
    stop_flag=None,
):
    """
    Main optimization entry point.
    
    Args:
        script_path: Path to the backtest .py script
        entry_style: "function_kwargs" | "config_class"
        config_class_name: Class name for config_class style (e.g., "StrategyConfig")
        fixed_params: Dict of parameters that stay constant
        optimize_params: Dict of parameters to optimize with ranges
        mode: "grid" | "random" | "bayesian"
        num_iterations: Max number of parameter combinations to try
        num_workers: Parallel worker processes
        seed: Random seed for reproducibility
        ranking_metric: How to rank results
        top_n: How many best results to keep
        optimization_id: Unique ID for this optimization run
        progress_callback: Optional callback(progress_dict) for live updates
        resume: True when picking up an existing run — keeps the original
                started_at and records the restart in the run's config
        stop_flag: Optional threading.Event; when set, no further batches are
                   dispatched and the run ends as "stopped" (and resumable)

    Returns:
        dict with results summary
    """
    if optimization_id is None:
        optimization_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Create runs directory for this optimization
    runs_dir = os.path.join(SCRIPT_DIR, "optimizations", optimization_id, "runs")
    os.makedirs(runs_dir, exist_ok=True)
    
    # Save optimization config
    opt_config = {
        "script_path": script_path,
        "script_name": os.path.basename(script_path),
        "entry_style": entry_style,
        "config_class_name": config_class_name,
        "fixed_params": fixed_params,
        "optimize_params": optimize_params,
        "mode": mode,
        "num_iterations": num_iterations,
        "num_workers": num_workers,
        "seed": seed,
        "ranking_metric": ranking_metric,
        "top_n": top_n,
        "drawdown_optimization": drawdown_optimization,
        "dd_min_trades_per_day": dd_min_trades_per_day,
        "dd_target_trades_per_day": dd_target_trades_per_day,
        "started_at": datetime.now().isoformat(),
        "status": "running",
    }
    opt_dir = os.path.dirname(runs_dir)
    config_path = os.path.join(opt_dir, "optimization_config.json")

    # On a resume, keep the run's original start time so the history card
    # still shows when the work actually began, and leave a trail of restarts.
    if resume and os.path.exists(config_path):
        try:
            with open(config_path) as f:
                prev_config = json.load(f)
            opt_config["started_at"] = prev_config.get("started_at") or opt_config["started_at"]
            opt_config["resume_history"] = list(prev_config.get("resume_history") or []) + [
                datetime.now().isoformat()
            ]
        except Exception:
            pass

    with open(config_path, "w") as f:
        json.dump(opt_config, f, indent=2, default=json_default)

    # ---- Build (or recover) the sweep plan --------------------------------
    plan_path = os.path.join(opt_dir, "sweep_plan.json")
    bayes_opt = None
    param_names = []

    # On resume, replay the recorded plan rather than regenerating it: only
    # then are batch folders guaranteed to still hold the parameters they were
    # created with.
    sweep_combos = load_sweep_plan(plan_path) if resume else None

    is_bayesian = mode == "bayesian" and bool(optimize_params)
    if is_bayesian:
        bayes_opt, param_names = build_bayesian_optimizer(
            optimize_params, seed,
            n_initial_points=min(BAYESIAN_INITIAL_POINTS, max(num_iterations, 1)),
        )
        if bayes_opt is None:
            is_bayesian = False  # scikit-optimize missing -> random search

    if sweep_combos is None:
        if not optimize_params:
            sweep_combos = [{}]
        elif mode == "grid":
            # Bounded up front so a large grid is sampled, never materialised.
            sweep_combos = generate_grid_params(optimize_params, limit=num_iterations, seed=seed)
        elif is_bayesian:
            sweep_combos = []  # asked for in waves, as results come back
        else:
            sweep_combos = generate_random_params(optimize_params, num_iterations, seed)

    # Bayesian keeps planning until it reaches num_iterations; every other mode
    # knows its whole plan up front.
    total_batches = num_iterations if is_bayesian else len(sweep_combos)

    if not is_bayesian:
        save_sweep_plan(plan_path, sweep_combos)

    def batch_dir_for(index: int) -> str:
        """Directory for the 0-based plan position `index`."""
        return os.path.join(runs_dir, f"batch_{index + 1:04d}")

    def prepare_batch(index: int, sweep: dict) -> str:
        """Write params.json for the 0-based plan position `index`."""
        full_cfg = dict(fixed_params)
        full_cfg.update(sweep)
        return write_batch_input(runs_dir, index + 1, full_cfg)

    # Create batch directories for everything already planned.
    batches_to_run = []
    for i, sweep in enumerate(sweep_combos):
        batch_dir = prepare_batch(i, sweep)

        # Resume support: only a batch that genuinely succeeded is skipped.
        # Failed and never-finished batches are re-queued, which is what makes
        # a resumed run pick up exactly where the interrupted one left off.
        if read_batch_state(batch_dir) == "ok":
            continue

        batches_to_run.append(batch_dir)

    already_done = len(sweep_combos) - len(batches_to_run)

    # Progress tracking.
    #
    # ok/failed are the honest tally: a batch counts as done only once it has
    # left a metrics.json behind. The previous counter incremented on every
    # future that returned, so a worker that died without producing anything
    # still pushed the bar forward and the run could read 100% complete while
    # hundreds of batches had produced no result at all.
    progress = {
        "optimization_id": optimization_id,
        "total": total_batches,
        "running": 0,
        "status": "running",
        "current_best": None,
        "eta_seconds": None,
        "eta_max_seconds": None,
        "elapsed_seconds": 0.0,
        "started_at": time.time(),
        "resumed": bool(resume),
        "mode": mode,
        "workers": num_workers,
        # Bayesian plans more points as it learns, so what's left to run is
        # everything up to num_iterations, not just what's planned so far.
        "queued": (total_batches - already_done) if is_bayesian else len(batches_to_run),
        **make_progress_fields(total_batches, already_done, 0),
    }

    def update_progress():
        # Progress reporting must never be able to abort the run itself.
        try:
            atomic_write_json(os.path.join(opt_dir, "progress.json"), progress)
        except Exception as e:
            print(f"[optimizer] Could not write progress.json: {e}")
        if progress_callback:
            progress_callback(progress)

    update_progress()
    
    # Run batches in parallel
    python_exe = sys.executable
    completed_times = []
    
    stopped = False
    slowest_batch_seconds = 0.0

    def record_outcome(batch_dir, returncode=0, stderr_tail="", error=None, duration=None):
        """Fold one finished batch into the progress tally and time estimates."""
        nonlocal slowest_batch_seconds

        if error is not None:
            # Timeout, broken pool, killed process — no result to read.
            write_crash_metrics(batch_dir, error)
        elif returncode != 0:
            # worker.py writes its own metrics.json for strategy-level errors
            # and still exits 0; a non-zero exit means it died before it could
            # record anything.
            write_crash_metrics(
                batch_dir,
                f"Worker exited with code {returncode}\n{stderr_tail}",
            )

        # Outcome comes from what the batch left on disk, never from the exit
        # code, because a caught strategy error still exits 0.
        if read_batch_state(batch_dir) == "ok":
            progress["ok"] += 1
        else:
            progress["failed"] += 1

        if duration and duration > slowest_batch_seconds:
            slowest_batch_seconds = duration

        progress.update(
            make_progress_fields(total_batches, progress["ok"], progress["failed"])
        )

        elapsed = time.time() - progress["started_at"]
        progress["elapsed_seconds"] = round(elapsed, 1)

        done_count = progress["completed"] - already_done
        left = max(progress["queued"] - done_count, 0)
        if done_count > 0:
            # Wall-clock throughput already accounts for parallelism.
            progress["eta_seconds"] = round((elapsed / done_count) * left, 1)
            # Worst case: every remaining batch takes as long as the slowest
            # one seen so far, run in waves of num_workers.
            waves = math.ceil(left / max(num_workers, 1))
            progress["eta_max_seconds"] = round(slowest_batch_seconds * waves, 1)

        update_progress()

    def run_wave(executor, batch_dirs) -> bool:
        """Run a set of batches to completion. Returns True if asked to stop."""
        futures = {
            executor.submit(run_one_slim, bd, script_path, entry_style,
                            config_class_name, python_exe): bd
            for bd in batch_dirs
        }
        interrupted = False
        for fut in as_completed(futures):
            batch_dir = futures[fut]
            try:
                batch_dir, returncode, stderr_tail, duration = fut.result()
                record_outcome(batch_dir, returncode, stderr_tail, duration=duration)
            except Exception as e:
                record_outcome(batch_dir, error=f"{type(e).__name__}: {e}")

            if stop_flag is not None and stop_flag.is_set():
                # Drop everything not yet started; batches already in flight
                # finish on their own so their results aren't thrown away.
                interrupted = True
                for pending_fut in futures:
                    pending_fut.cancel()
                break
        return interrupted

    def execute_bayesian() -> bool:
        """
        Drive the real ask -> evaluate -> tell loop.

        Each wave runs num_workers points in parallel, feeds their scores back
        into the surrogate model, and asks for the next wave informed by
        everything seen so far. The previous implementation asked for ten
        points, never told the optimizer a single result, and stopped there —
        so a 1000-iteration Bayesian run executed 10 batches of plain random
        search.
        """
        told = set()
        # skopt minimises, so objectives are negated scores. Failures get an
        # objective slightly worse than anything real, keeping the surrogate
        # numerically sane instead of feeding it an infinity.
        #
        # The search is guided by the base ranking metric. Drawdown scoring is
        # deliberately left to the final ranking: its "auto" mode derives its
        # thresholds from the whole result set, which doesn't exist yet while
        # the search is still running.
        worst_objective = [None]

        def objective_for(index: int):
            score = read_batch_score(batch_dir_for(index), ranking_metric)
            if score is None:
                return None
            if math.isfinite(score):
                objective = -float(score)
                if worst_objective[0] is None or objective > worst_objective[0]:
                    worst_objective[0] = objective
                return objective
            base = worst_objective[0] if worst_objective[0] is not None else 0.0
            return base + abs(base) * 0.1 + 1.0

        def tell_completed():
            """Report every finished batch the optimizer hasn't seen yet."""
            for index in range(len(sweep_combos)):
                if index in told:
                    continue
                objective = objective_for(index)
                if objective is None:
                    continue
                told.add(index)
                try:
                    point = [sweep_combos[index][name] for name in param_names]
                    bayes_opt.tell(point, objective)
                except Exception as e:
                    print(f"[optimizer] skopt.tell failed for batch {index + 1}: {e}")

        wave_size = max(num_workers, 1)
        queue = [i for i in range(len(sweep_combos))
                 if read_batch_state(batch_dir_for(i)) != "ok"]

        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            while queue or len(sweep_combos) < total_batches:
                if stop_flag is not None and stop_flag.is_set():
                    return True

                if not queue:
                    tell_completed()
                    new_points = ask_bayesian_points(
                        bayes_opt, param_names,
                        min(wave_size, total_batches - len(sweep_combos)),
                    )
                    if not new_points:
                        break
                    for sweep in new_points:
                        queue.append(len(sweep_combos))
                        sweep_combos.append(sweep)
                    save_sweep_plan(plan_path, sweep_combos)

                wave, queue = queue[:wave_size], queue[wave_size:]
                if run_wave(executor, [prepare_batch(i, sweep_combos[i]) for i in wave]):
                    return True
        return False

    if is_bayesian:
        stopped = execute_bayesian()
    elif batches_to_run:
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            stopped = run_wave(executor, batches_to_run)

    # Aggregate results
    progress["status"] = "aggregating"
    update_progress()
    
    rows = []
    for i in range(total_batches):
        batch_dir = os.path.join(runs_dir, f"batch_{i + 1:04d}")
        params_path = os.path.join(batch_dir, "params.json")
        metrics_path = os.path.join(batch_dir, "metrics.json")
        
        if not (os.path.exists(params_path) and os.path.exists(metrics_path)):
            continue
        
        with open(params_path) as f:
            params = json.load(f)
        with open(metrics_path) as f:
            result_data = json.load(f)
        
        if not result_data["status"]["ok"]:
            rows.append({
                "batch": f"batch_{i + 1:04d}",
                "status": "FAILED",
                "error": (result_data["status"]["error"] or "")[:200],
                **flatten_params(params),
            })
            continue
        
        metrics = result_data["metrics"]
        elapsed_s = result_data.get("elapsed_seconds", 0)
        
        # Compute base ranking score (shared with the Bayesian objective, so
        # the search optimises exactly what the results are ranked by)
        base_score = score_metrics(metrics, ranking_metric)

        # Apply drawdown optimization if enabled
        if drawdown_optimization != "disabled":
            _trading_days = compute_trading_days(fixed_params)
            score = drawdown_score(
                metrics, _trading_days,
                dd_min_trades_per_day, dd_target_trades_per_day,
                base_score,
            )
        else:
            score = base_score
        
        row = {
            "batch": f"batch_{i + 1:04d}",
            "status": "OK",
            "composite_score": score,
            "elapsed_seconds": elapsed_s,
            **flatten_params(params),
            **metrics,
        }
        rows.append(row)
    
    # Auto mode: re-score with auto-detected thresholds
    if drawdown_optimization == "auto":
        _trading_days = compute_trading_days(fixed_params)
        ok_rows = [r for r in rows if r.get("status") == "OK"]
        auto_min, auto_target = auto_detect_trade_thresholds(ok_rows, _trading_days)
        
        for row in rows:
            if row.get("status") != "OK":
                continue
            # Re-compute drawdown score with auto thresholds
            row_metrics = {k: v for k, v in row.items() 
                         if k not in ("batch", "status", "composite_score", "elapsed_seconds")}
            base = row.get("composite_score", 0)
            row["composite_score"] = drawdown_score(
                row_metrics, _trading_days, auto_min, auto_target, base
            )
    
    # Save results as both CSV and Parquet
    results_df = pd.DataFrame(rows)
    
    results_csv = os.path.join(opt_dir, "optimization_results.csv")
    results_df.to_csv(results_csv, index=False)
    
    results_parquet = os.path.join(opt_dir, "optimization_results.parquet")
    try:
        results_df.to_parquet(results_parquet, index=False, compression="zstd")
    except Exception:
        pass  # Parquet write might fail on mixed types
    
    # Rank and identify best results.
    # A run stopped before its first batch finished produces no rows at all,
    # and an empty DataFrame has no 'status' column to filter on.
    if "status" in results_df.columns:
        ok_df = results_df[results_df["status"] == "OK"].copy()
    else:
        ok_df = results_df.iloc[0:0]
    best_results = []
    
    if not ok_df.empty:
        rank_col = "composite_score"
        ok_df[rank_col] = pd.to_numeric(ok_df[rank_col], errors="coerce")
        ok_df = ok_df.sort_values(rank_col, ascending=False)
        
        top_rows = ok_df.head(top_n)
        best_results = top_rows.to_dict(orient="records")
        
        # Save top summary
        top_csv = os.path.join(opt_dir, "top_results.csv")
        top_rows.to_csv(top_csv, index=False)
    
    # Update final progress. A stopped run stays resumable, so the dashboard
    # can offer to finish the batches that never ran instead of starting over.
    final_status = "stopped" if stopped else "completed"
    final_states = scan_batch_states(runs_dir, total_batches)
    progress.update(
        make_progress_fields(total_batches, final_states["ok"], final_states["failed"])
    )
    progress["remaining"] = final_states["pending"] + final_states["failed"]
    progress["resumable"] = progress["remaining"] > 0
    progress["status"] = final_status
    progress["completed_at"] = datetime.now().isoformat()
    progress["total_ok"] = int(len(ok_df)) if not ok_df.empty else 0
    progress["total_failed"] = int(progress["failed"])
    update_progress()

    # Update optimization config
    opt_config["status"] = final_status
    opt_config["completed_at"] = datetime.now().isoformat()
    opt_config["total_ok"] = progress["total_ok"]
    opt_config["total_failed"] = progress["total_failed"]
    with open(os.path.join(opt_dir, "optimization_config.json"), "w") as f:
        json.dump(opt_config, f, indent=2, default=json_default)
    
    return {
        "optimization_id": optimization_id,
        "total_batches": total_batches,
        "successful": progress["total_ok"],
        "failed": progress["total_failed"],
        "status": final_status,
        "resumable": progress["resumable"],
        "remaining": progress["remaining"],
        "best_results": best_results[:top_n],
        "results_csv": results_csv,
        "results_parquet": results_parquet,
    }


# =============================================================================
# SINGLE BACKTEST (for Walk-Forward OOS)
# =============================================================================

def run_single_backtest(
    batch_dir: str,
    script_path: str,
    params: dict,
    entry_style: str = "config_class",
    config_class_name: str = None,
) -> dict:
    """
    Run a single backtest with frozen parameters.
    
    Used by the Walk-Forward engine for OOS backtests.
    Reuses the existing run_one() subprocess worker pipeline
    without going through the full optimization engine.
    
    Args:
        batch_dir: Directory where params.json and outputs will be stored
        script_path: Path to the strategy .py file
        params: Complete parameter dict (fixed + frozen optimized)
        entry_style: "function_kwargs" or "config_class"
        config_class_name: Class name for config_class style
    
    Returns:
        dict with metrics, status, and elapsed time
    """
    os.makedirs(batch_dir, exist_ok=True)

    # Write params
    with open(os.path.join(batch_dir, "params.json"), "w") as f:
        json.dump(params, f, indent=2, default=json_default)

    # Run via existing worker subprocess
    python_exe = sys.executable
    _, result = run_one(batch_dir, script_path, entry_style, config_class_name, python_exe)

    # Read metrics
    metrics_path = os.path.join(batch_dir, "metrics.json")
    if os.path.exists(metrics_path):
        with open(metrics_path) as f:
            return json.load(f)

    return {
        "status": {"ok": False, "error": f"No metrics produced. returncode={result.returncode}"},
        "metrics": {},
        "elapsed_seconds": 0,
    }

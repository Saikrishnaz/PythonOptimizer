"""
run_optimizer.py
-----------------
py run_optimizer.py --strategy MeanReversionBacktestXAUUSD.py --n 200 --workers 3
Random-search parameter optimizer for your mean reversion backtest.

USAGE:
    python run_optimizer.py --strategy "C:\\path\\to\\your_strategy_script.py" --n 200 --workers 4

    --strategy   path to YOUR .py file that contains the main() function
                 (the one you pasted, with CREDITSPREAD, main(), etc.)
    --n          how many random parameter combinations to try (default 200)
    --workers    how many to run in parallel (default 2 -- raise carefully,
                 each run reads option data files from disk and is CPU-bound)
    --seed       random seed, for reproducible sweeps (default 42)
    --rank       "composite" (default) | "net_profit" | "sharpe" | "profit_factor"
    --top        how many best batches to copy into runs/best_10 (default 10)

OUTPUT:
    runs/batch_0001/ ... batch_NNNN/   each batch's full output (xlsx, csvs, params.json, metrics.json)
    runs/optimization_results.csv      master table: every batch's params + metrics
    runs/best_10/                      copies of the top N batch folders + top_summary.csv

Safe to re-run: batches that already have a metrics.json are skipped (resume support).
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys

from concurrent.futures import ProcessPoolExecutor, as_completed
import pandas as pd

from config import sample_params, build_full_config

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(SCRIPT_DIR, "runs")

RANK_METRIC_MAP = {
    "net_profit": "Net Profit",
    "sharpe": "Sharpe Ratio",
    "profit_factor": "Profit Factor",
}


def make_batches(n, seed):
    """
    Draws n random combinations, skipping exact duplicates.
    (With a ~4.6 trillion-combo space this almost never triggers,
    but it's a free safety net so no run is ever wasted re-testing
    a combo you've already tried.)
    """
    rng = random.Random(seed)
    batches = []
    seen = set()
    attempts = 0
    max_attempts = n * 20  # generous ceiling in case ranges are ever narrowed a lot

    while len(batches) < n and attempts < max_attempts:
        attempts += 1
        sampled = sample_params(rng)
        key = json.dumps(sampled, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        full_cfg = build_full_config(sampled)
        batches.append((len(batches) + 1, full_cfg))

    if len(batches) < n:
        print(f"Warning: only found {len(batches)} unique combos after {attempts} attempts "
              f"(requested {n}). Consider widening the ranges in config.py.")

    return batches


def write_batch_input(batch_id, cfg):
    batch_dir = os.path.join(RUNS_DIR, f"batch_{batch_id:04d}")
    os.makedirs(batch_dir, exist_ok=True)
    with open(os.path.join(batch_dir, "params.json"), "w") as f:
        json.dump(cfg, f, indent=2, default=str)
    return batch_dir


def run_one(batch_dir, strategy_path, python_exe):
    env = os.environ.copy()
    env["STRATEGY_MODULE_PATH"] = os.path.abspath(strategy_path)
    worker_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "worker.py"))
    result = subprocess.run(
        [python_exe, worker_path, os.path.abspath(batch_dir)],
        env=env, capture_output=True, text=True
    )
    return batch_dir, result


def composite_score(metrics: dict) -> float:
    """
    Default 'best' ranking when --rank composite is used.
    Blends risk-adjusted return (Sharpe), consistency (Profit Factor),
    and drawdown-adjusted profit. Edit freely to match what YOU care about.
    """
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
    Ranks based on three pillars:
      1. Max Trades   — high trade frequency (more opportunities)
      2. Profit       — net PnL after all costs (brokerage + spread)
      3. Brokerage    — cost efficiency (low brokerage ratio = good)

    Formula:
      score = net_pnl_after_costs × trade_frequency_bonus × brokerage_efficiency

    Where:
      - net_pnl_after_costs: Net PnL After Costs (or Net Profit as fallback)
      - trade_frequency_bonus: log2(trades) — rewards more trades with diminishing returns
      - brokerage_efficiency: (100 - brokerage_ratio%) / 100
        e.g. 10% brokerage ratio → 0.90 efficiency (good)
             50% brokerage ratio → 0.50 efficiency (bad)

    Strategies with negative profit or fewer than 50 trades are heavily penalized.
    """
    try:
        trades = float(metrics.get("Total Trades", 0) or 0)
        net_profit = float(metrics.get("Net PnL After Costs", 0) or 0)
        # Fallback to Net Profit if Net PnL After Costs not available (older runs)
        if net_profit == 0:
            net_profit = float(metrics.get("Net Profit", 0) or 0)
        brokerage_ratio = float(metrics.get("Brokerage Ratio %", 50) or 50)
        profit_factor = float(metrics.get("Profit Factor", 0) or 0)

        # Minimum trade threshold — strategies with < 50 trades are unreliable
        if trades < 50:
            return float("-inf")

        # Negative profit → bottom of the ranking
        if net_profit <= 0:
            return net_profit  # negative = auto-sorts to bottom

        # Trade frequency bonus: log2 gives diminishing returns
        # log2(100)=6.6, log2(1000)=10, log2(10000)=13.3
        import math
        trade_bonus = math.log2(max(trades, 1))

        # Brokerage efficiency: lower ratio = better
        # Clamped so a 0% ratio gives 1.0 and 100%+ gives near-zero
        brokerage_eff = max(0.01, (100 - min(brokerage_ratio, 99)) / 100)

        # Profit factor bonus: rewards consistent winners
        pf_bonus = min(profit_factor, 5.0)  # cap at 5 to avoid outlier dominance

        # Final score: multiplicative so ALL factors must be strong
        score = net_profit * trade_bonus * brokerage_eff * pf_bonus
        return score

    except Exception:
        return float("-inf")



def flatten_params(params: dict) -> dict:
    flat = {}
    for k, v in params.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                flat[f"{k}.{kk}"] = vv
        else:
            flat[k] = v
    return flat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True, help="Path to your strategy .py file exposing main()")
    ap.add_argument("--n", type=int, default=200, help="Number of random combinations to try")
    ap.add_argument("--workers", type=int, default=2, help="Parallel worker processes")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--rank", choices=["net_profit", "sharpe", "profit_factor", "composite", "priority"], default="priority")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    os.makedirs(RUNS_DIR, exist_ok=True)
    batches = make_batches(args.n, args.seed)

    to_run = []
    for batch_id, cfg in batches:
        batch_dir = write_batch_input(batch_id, cfg)
        metrics_file = os.path.join(batch_dir, "metrics.json")
        if os.path.exists(metrics_file):
            # only skip if it actually SUCCEEDED last time -- failed
            # batches get retried, since the underlying issue may now be fixed
            try:
                with open(metrics_file) as f:
                    prev = json.load(f)
                if prev.get("status", {}).get("ok"):
                    continue  # genuinely done -> resume support
            except Exception:
                pass  # corrupt/partial file -> retry it
        to_run.append(batch_dir)

    print(f"Running {len(to_run)} batches ({len(batches) - len(to_run)} already done)...")

    python_exe = sys.executable
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(run_one, bd, args.strategy, python_exe): bd for bd in to_run}
        done = 0
        for fut in as_completed(futures):
            batch_dir, result = fut.result()
            done += 1
            ok = "OK" if result.returncode == 0 else "FAIL"
            print(f"[{done}/{len(to_run)}] {batch_dir} -> {ok}")
            if result.returncode != 0:
                print(result.stderr[-1500:])

    # ---- Aggregate every batch's params + metrics into one table ----
    rows = []
    for batch_id, _ in batches:
        batch_dir = os.path.join(RUNS_DIR, f"batch_{batch_id:04d}")
        params_path = os.path.join(batch_dir, "params.json")
        metrics_path = os.path.join(batch_dir, "metrics.json")
        if not (os.path.exists(params_path) and os.path.exists(metrics_path)):
            continue
        with open(params_path) as f:
            params = json.load(f)
        with open(metrics_path) as f:
            result = json.load(f)

        if not result["status"]["ok"]:
            rows.append({
                "batch": f"batch_{batch_id:04d}", "status": "FAILED",
                "error": (result["status"]["error"] or "")[:200],
                **flatten_params(params),
            })
            continue

        metrics = result["metrics"]
        row = {"batch": f"batch_{batch_id:04d}", "status": "OK", **flatten_params(params), **metrics}
        if args.rank == "priority":
            row["composite_score"] = priority_score(metrics)
        else:
            row["composite_score"] = composite_score(metrics)
        rows.append(row)

    results_df = pd.DataFrame(rows)
    results_csv = os.path.join(RUNS_DIR, "optimization_results.csv")
    results_df.to_csv(results_csv, index=False)
    print(f"\nSaved master results: {results_csv}")

    # ---- Rank and copy the best N into runs/best_10 ----
    ok_df = results_df[results_df["status"] == "OK"].copy()
    if ok_df.empty:
        print("No successful runs to rank. Check the FAIL logs above.")
        return

    rank_col = "composite_score" if args.rank in ("composite", "priority") else RANK_METRIC_MAP[args.rank]
    ok_df[rank_col] = pd.to_numeric(ok_df[rank_col], errors="coerce")
    ok_df = ok_df.sort_values(rank_col, ascending=False)

    best_dir = os.path.join(RUNS_DIR, "best_10")
    if os.path.exists(best_dir):
        shutil.rmtree(best_dir)
    os.makedirs(best_dir)

    top_rows = ok_df.head(args.top)
    for b in top_rows["batch"].tolist():
        shutil.copytree(os.path.join(RUNS_DIR, b), os.path.join(best_dir, b))
    top_rows.to_csv(os.path.join(best_dir, "top_summary.csv"), index=False)

    print(f"\nTop {args.top} batches by {rank_col}:")
    print(top_rows[["batch", rank_col]].to_string(index=False))
    print(f"\nCopied full outputs to: {best_dir}")
    print(f"Open the dashboard with: streamlit run dashboard.py")


if __name__ == "__main__":
    main()

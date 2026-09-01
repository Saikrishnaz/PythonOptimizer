"""
walk_forward/runner.py
-----------------------
Main Walk-Forward Testing orchestrator.

This is the core engine that coordinates the entire WFO process:
1. Validates configuration
2. Generates windows
3. For each step: run IS optimization → select params → run OOS backtest
4. Aggregates all OOS results
5. Analyzes parameter stability
6. Computes robustness score
7. Generates candidate parameters

CRITICAL DESIGN PRINCIPLE:
- This module is an ORCHESTRATOR — it calls the existing optimizer engine
  and worker, never duplicating optimizer logic.
- OOS data NEVER influences parameter optimization.
- Parameters are FROZEN before OOS execution.
"""
import json
import os
import sys
import time
import traceback
import glob
from datetime import datetime
from typing import Dict, Any, Optional, List

import pandas as pd
import numpy as np

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimizer_engine import run_optimization, run_one, write_batch_input, flatten_params
from worker import parse_technical_stats, compute_basic_metrics_from_trades

from .models import (
    WFOConfig, WFOWindow, WFOStepResult, StepState,
    SelectionConfig, RobustnessWeights
)
from .window_generator import generate_windows, validate_windows
from .selector import select_best_params
from .aggregator import aggregate_oos_results, compute_robustness_score
from .stability import compute_stability, generate_candidates
from . import persistence


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WalkForwardRunner:
    """
    Main Walk-Forward Testing orchestrator.
    
    Usage:
        runner = WalkForwardRunner(config)
        runner.run()  # Executes all steps sequentially
    """

    def __init__(self, config: WFOConfig):
        self.config = config
        self.run_id = config.run_id
        self.windows: List[WFOWindow] = []
        self.step_results: List[WFOStepResult] = []
        self.progress = {}
        self._stop_requested = False

    def stop(self):
        """Request graceful stop of the WFO run."""
        self._stop_requested = True

    def _update_progress(self, **kwargs):
        """Update and persist progress for SSE streaming."""
        self.progress.update(kwargs)
        persistence.save_wfo_progress(self.run_id, self.progress)

    # =========================================================================
    # MAIN EXECUTION
    # =========================================================================

    def run(self) -> Dict[str, Any]:
        """
        Execute the full Walk-Forward Testing pipeline.
        
        Returns:
            Final WFO result summary
        """
        try:
            # Initialize progress
            self.progress = {
                "run_id": self.run_id,
                "status": "initializing",
                "current_step": 0,
                "total_steps": 0,
                "stage": "initializing",
                "message": "Validating configuration...",
                "started_at": time.time(),
            }
            self._update_progress()

            # 1. Generate and validate windows
            self.windows = generate_windows(self.config)
            
            if not self.windows:
                self._update_progress(
                    status="error",
                    message="No valid windows could be generated."
                )
                return {"error": "No valid windows generated"}

            errors = validate_windows(
                self.windows,
                self.config.dataset_start,
                self.config.dataset_end,
            )
            if errors:
                self._update_progress(
                    status="error",
                    message=f"Window validation failed: {errors[0]}",
                    validation_errors=errors,
                )
                return {"error": "Validation failed", "errors": errors}

            # 2. Persist config and create directories
            persistence.save_wfo_config(self.run_id, self.config)
            persistence.ensure_dirs(self.run_id, len(self.windows))

            # Initialize step results
            self.step_results = [
                WFOStepResult(step=w.step, window=w) for w in self.windows
            ]

            self._update_progress(
                status="running",
                total_steps=len(self.windows),
                message=f"Starting Walk-Forward with {len(self.windows)} steps",
            )

            # 3. Execute each step
            for i, window in enumerate(self.windows):
                if self._stop_requested:
                    self._update_progress(
                        status="stopped",
                        message=f"Walk-Forward stopped at step {window.step}"
                    )
                    return {"status": "stopped", "completed_steps": i}

                step_result = self.step_results[i]

                # Check resume — skip completed steps
                if persistence.is_step_completed(self.run_id, window.step):
                    self._update_progress(
                        current_step=window.step,
                        stage="skipped",
                        message=f"Step {window.step} already completed (resume)"
                    )
                    # Reload saved results
                    self._reload_step_result(step_result)
                    continue

                # Execute this step
                try:
                    self._execute_step(step_result, window)
                except Exception as e:
                    step_result.state = StepState.FAILED.value
                    step_result.error = str(e)
                    persistence.save_step_state(
                        self.run_id, window.step,
                        StepState.FAILED.value, str(e)
                    )
                    self._update_progress(
                        current_step=window.step,
                        stage="failed",
                        message=f"Step {window.step} failed: {str(e)[:200]}"
                    )
                    # Continue to next step instead of aborting
                    continue

            # 4. Aggregate results
            self._update_progress(
                stage="aggregating",
                message="Aggregating OOS results..."
            )
            return self._finalize()

        except Exception as e:
            self._update_progress(
                status="error",
                message=f"WFO failed: {str(e)[:500]}",
                error=traceback.format_exc(),
            )
            return {"error": str(e)}

    # =========================================================================
    # STEP EXECUTION
    # =========================================================================

    def _execute_step(self, step_result: WFOStepResult, window: WFOWindow):
        """Execute a single WFO step: IS optimization → parameter selection → OOS backtest."""
        step = window.step

        # ---- Phase 1: IS Optimization ----
        self._update_progress(
            current_step=step,
            stage="optimizing",
            message=f"Step {step}: Running IS optimization ({window.is_start} → {window.is_end})"
        )
        persistence.save_step_state(self.run_id, step, StepState.OPTIMIZING.value)

        # Build IS configuration — override date parameters
        is_fixed_params = self._build_date_overridden_params(
            window.is_start, window.is_end
        )

        # Generate unique optimization ID for this step's IS run
        opt_id = f"wfo_{self.run_id}_step{step:03d}_is"

        # Run the existing optimizer engine
        opt_result = run_optimization(
            script_path=self.config.strategy_path,
            entry_style=self.config.entry_style,
            config_class_name=self.config.config_class_name,
            fixed_params=is_fixed_params,
            optimize_params=self.config.optimize_params,
            mode=self.config.optimization_method,
            num_iterations=self.config.optimization_iterations,
            num_workers=self.config.num_workers,
            seed=self.config.seed + step,  # Different seed per step for diversity
            ranking_metric=self.config.ranking_metric,
            top_n=10,
            optimization_id=opt_id,
            drawdown_optimization=self.config.drawdown_optimization,
            dd_min_trades_per_day=self.config.dd_min_trades_per_day,
            dd_target_trades_per_day=self.config.dd_target_trades_per_day,
        )

        step_result.optimization_id = opt_id
        persistence.save_step_state(self.run_id, step, StepState.OPTIMIZED.value)

        # ---- Phase 2: Parameter Selection ----
        self._update_progress(
            current_step=step,
            stage="selecting",
            message=f"Step {step}: Selecting best parameters..."
        )
        persistence.save_step_state(self.run_id, step, StepState.SELECTING.value)

        # Find the results file
        opt_dir = os.path.join(SCRIPT_DIR, "optimizations", opt_id)
        results_path = os.path.join(opt_dir, "optimization_results.parquet")

        selected_params, selection_info = select_best_params(
            results_path, self.config.selection
        )

        if selected_params is None:
            raise RuntimeError(
                f"No parameters passed selection criteria: {selection_info.get('error', 'Unknown')}"
            )

        # Remove non-parameter keys from selected params
        param_keys_to_remove = {'batch', 'status', 'composite_score', 'elapsed_seconds'}
        for k in param_keys_to_remove:
            selected_params.pop(k, None)

        step_result.selected_params = selected_params
        step_result.is_metrics = selection_info.get("metrics", {})
        step_result.is_score = selection_info.get("score", 0)
        step_result.is_rank = selection_info.get("rank", 0)

        persistence.save_selected_params(self.run_id, step, selected_params)
        persistence.save_is_metrics(self.run_id, step, {
            "params": selected_params,
            "selection_info": selection_info,
        })

        # ---- Phase 3: OOS Backtest (FROZEN PARAMETERS) ----
        self._update_progress(
            current_step=step,
            stage="oos_running",
            message=f"Step {step}: Running OOS backtest ({window.oos_start} → {window.oos_end})"
        )
        persistence.save_step_state(self.run_id, step, StepState.OOS_RUNNING.value)

        # Build OOS params — frozen parameters + OOS date override
        oos_metrics = self._run_oos_backtest(step, selected_params, window)

        # Store OOS results
        step_result.oos_metrics = oos_metrics
        step_result.oos_trades_count = int(oos_metrics.get("Total Trades", 0) or 0)
        step_result.oos_net_profit = float(oos_metrics.get("Net Profit", 0) or 0)
        step_result.oos_profit_factor = float(oos_metrics.get("Profit Factor", 0) or 0)
        step_result.oos_sharpe = float(oos_metrics.get("Sharpe Ratio", 0) or 0)
        step_result.oos_max_drawdown = float(oos_metrics.get("Overall Max Drawdown", 0) or 0)
        step_result.oos_win_rate = float(oos_metrics.get("Win Rate %", 0) or 0)
        step_result.state = StepState.COMPLETED.value

        persistence.save_oos_metrics(self.run_id, step, oos_metrics)
        persistence.save_step_state(self.run_id, step, StepState.COMPLETED.value)

        self._update_progress(
            current_step=step,
            stage="completed",
            message=f"Step {step}: Completed — OOS Profit: ${step_result.oos_net_profit:.2f}"
        )

    def _build_date_overridden_params(self, start_date: str, end_date: str) -> dict:
        """
        Build fixed params dict with date overrides.
        
        Handles both flat (start_date/end_date) and nested
        (Backtest_period.start_date/end_date) date parameter styles.
        """
        params = dict(self.config.fixed_params)

        if self.config.date_param_style == "nested" and self.config.date_param_name:
            # Nested style: e.g. Backtest_period = {start_date: ..., end_date: ...}
            period_key = self.config.date_param_name
            if period_key in params and isinstance(params[period_key], dict):
                params[period_key] = dict(params[period_key])
                params[period_key]["start_date"] = start_date
                params[period_key]["end_date"] = end_date
            else:
                params[period_key] = {
                    "start_date": start_date,
                    "end_date": end_date,
                }
        else:
            # Flat style: start_date = ..., end_date = ...
            params["start_date"] = start_date
            params["end_date"] = end_date

        return params

    def _run_oos_backtest(
        self,
        step: int,
        frozen_params: Dict[str, Any],
        window: WFOWindow,
    ) -> Dict[str, Any]:
        """
        Run a single OOS backtest with frozen parameters.
        
        Uses the existing worker subprocess pipeline
        without going through the full optimization engine.
        """
        # Build the complete param set: fixed + frozen + OOS dates
        oos_params = self._build_date_overridden_params(
            window.oos_start, window.oos_end
        )
        # Merge frozen optimized params on top
        oos_params.update(frozen_params)

        # Create a temporary batch directory for the OOS run
        step_dir = persistence.get_step_dir(self.run_id, step)
        oos_batch_dir = os.path.join(step_dir, "oos_run")
        os.makedirs(oos_batch_dir, exist_ok=True)

        # Write params.json
        params_path = os.path.join(oos_batch_dir, "params.json")
        with open(params_path, "w") as f:
            json.dump(oos_params, f, indent=2, default=persistence._json_default)

        # Run using existing worker
        python_exe = sys.executable
        batch_dir, result = run_one(
            oos_batch_dir,
            self.config.strategy_path,
            self.config.entry_style,
            self.config.config_class_name,
            python_exe,
        )

        # Parse results
        metrics_path = os.path.join(oos_batch_dir, "metrics.json")
        if os.path.exists(metrics_path):
            with open(metrics_path) as f:
                result_data = json.load(f)

            if result_data["status"]["ok"]:
                metrics = result_data["metrics"]
            else:
                raise RuntimeError(
                    f"OOS backtest failed: {result_data['status'].get('error', 'Unknown error')[:500]}"
                )
        else:
            raise RuntimeError("OOS backtest produced no metrics.json output")

        # Copy trade files to step directory
        trade_files = glob.glob(os.path.join(oos_batch_dir, "*trade*.csv"))
        for tf in trade_files:
            dest = os.path.join(step_dir, "oos_trades.csv")
            try:
                import shutil
                shutil.copy2(tf, dest)
            except Exception:
                pass

        return metrics

    # =========================================================================
    # RESUME SUPPORT
    # =========================================================================

    def _reload_step_result(self, step_result: WFOStepResult):
        """Reload a completed step's results from disk."""
        step = step_result.step
        step_result.state = StepState.COMPLETED.value

        # Load selected params
        params = persistence.load_selected_params(self.run_id, step)
        if params:
            step_result.selected_params = params

        # Load OOS metrics
        oos = persistence.load_oos_metrics(self.run_id, step)
        if oos:
            step_result.oos_metrics = oos
            step_result.oos_trades_count = int(oos.get("Total Trades", 0) or 0)
            step_result.oos_net_profit = float(oos.get("Net Profit", 0) or 0)
            step_result.oos_profit_factor = float(oos.get("Profit Factor", 0) or 0)
            step_result.oos_sharpe = float(oos.get("Sharpe Ratio", 0) or 0)
            step_result.oos_max_drawdown = float(oos.get("Overall Max Drawdown", 0) or 0)
            step_result.oos_win_rate = float(oos.get("Win Rate %", 0) or 0)

    # =========================================================================
    # FINALIZATION
    # =========================================================================

    def _finalize(self) -> Dict[str, Any]:
        """Aggregate all OOS results, compute stability, robustness, candidates."""
        
        # Aggregate OOS
        oos_aggregate = aggregate_oos_results(self.step_results)

        # Parameter stability
        # Determine which params are actually optimization params
        opt_param_names = list(self.config.optimize_params.keys())
        if not opt_param_names:
            # If no explicit optimize params, infer from selected params
            completed = [s for s in self.step_results if s.state == "completed"]
            if completed:
                # Find keys that vary across steps
                all_keys = set()
                for s in completed:
                    all_keys.update(s.selected_params.keys())
                opt_param_names = list(all_keys)

        stability = compute_stability(self.step_results, opt_param_names)

        # Robustness score
        robustness = compute_robustness_score(
            oos_aggregate,
            stability.get("overall_stability_score", 0),
            self.config.robustness_weights,
        )

        # Generate candidates
        candidates = generate_candidates(
            self.step_results, stability, opt_param_names
        )

        # Save aggregate results
        step_dicts = []
        for sr in self.step_results:
            step_dicts.append({
                "step": sr.step,
                "state": sr.state,
                "is_start": sr.window.is_start,
                "is_end": sr.window.is_end,
                "oos_start": sr.window.oos_start,
                "oos_end": sr.window.oos_end,
                "is_score": sr.is_score,
                "oos_net_profit": sr.oos_net_profit,
                "oos_profit_factor": sr.oos_profit_factor,
                "oos_sharpe": sr.oos_sharpe,
                "oos_max_drawdown": sr.oos_max_drawdown,
                "oos_win_rate": sr.oos_win_rate,
                "oos_trades": sr.oos_trades_count,
                "selected_params": json.dumps(sr.selected_params, default=str),
            })

        persistence.save_aggregate_results(
            self.run_id, step_dicts, oos_aggregate,
            stability, robustness, candidates
        )

        # Final progress update
        self._update_progress(
            status="completed",
            stage="completed",
            message="Walk-Forward Testing completed successfully",
            completed_at=datetime.now().isoformat(),
            oos_summary={
                "net_profit": oos_aggregate.get("net_profit", 0),
                "profitable_periods": oos_aggregate.get("profitable_periods", 0),
                "total_periods": oos_aggregate.get("total_steps", 0),
                "robustness_label": robustness.get("label", ""),
            },
        )

        return {
            "status": "completed",
            "run_id": self.run_id,
            "total_steps": len(self.windows),
            "completed_steps": sum(
                1 for s in self.step_results if s.state == "completed"
            ),
            "oos_aggregate": oos_aggregate,
            "robustness": robustness,
            "stability_score": stability.get("overall_stability_score", 0),
            "num_candidates": len(candidates),
        }

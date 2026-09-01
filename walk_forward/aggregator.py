"""
walk_forward/aggregator.py
---------------------------
OOS result aggregation, robustness scoring, and combined performance analysis.

Combines all OOS periods into one chronological dataset and computes
comprehensive performance metrics. Never mixes IS results into OOS performance.
"""
import math
from typing import List, Dict, Any, Optional

import pandas as pd
import numpy as np

from .models import (
    WFOStepResult, RobustnessWeights, RobustnessLabel
)


# =============================================================================
# OOS AGGREGATION
# =============================================================================

def aggregate_oos_results(step_results: List[WFOStepResult]) -> Dict[str, Any]:
    """
    Combine all OOS results into one chronological dataset.
    
    Computes: Net Profit, Return, PF, Sharpe, Sortino, Max DD,
    Win Rate, Total Trades, Avg Trade, Expectancy, Recovery Factor,
    CAGR, monthly/annual returns, losing periods, consecutive losses.
    
    Returns:
        Dict with all aggregated OOS metrics
    """
    completed = [s for s in step_results if s.state == "completed"]
    
    if not completed:
        return {"error": "No completed steps to aggregate"}

    # Aggregate per-step OOS metrics
    total_trades = sum(s.oos_trades_count for s in completed)
    total_net_profit = sum(s.oos_net_profit for s in completed)
    
    # Collect per-step details
    step_profits = [s.oos_net_profit for s in completed]
    step_pfs = [s.oos_profit_factor for s in completed if s.oos_profit_factor > 0]
    step_sharpes = [s.oos_sharpe for s in completed if s.oos_sharpe != 0]
    step_dds = [s.oos_max_drawdown for s in completed]
    step_win_rates = [s.oos_win_rate for s in completed if s.oos_trades_count > 0]
    step_trades = [s.oos_trades_count for s in completed]

    profitable_periods = sum(1 for p in step_profits if p > 0)
    total_periods = len(completed)

    # Average metrics
    avg_profit_factor = float(np.mean(step_pfs)) if step_pfs else 0.0
    avg_sharpe = float(np.mean(step_sharpes)) if step_sharpes else 0.0
    avg_win_rate = float(np.mean(step_win_rates)) if step_win_rates else 0.0
    worst_drawdown = float(min(step_dds)) if step_dds else 0.0

    # Consecutive losing periods
    max_consec_losing = 0
    current_consec = 0
    for p in step_profits:
        if p <= 0:
            current_consec += 1
            max_consec_losing = max(max_consec_losing, current_consec)
        else:
            current_consec = 0

    # Average trade
    avg_trade = total_net_profit / total_trades if total_trades > 0 else 0.0

    # Expectancy (simplified)
    if total_trades > 0 and step_win_rates:
        avg_wr = np.mean(step_win_rates) / 100.0
        expectancy = avg_trade  # Per-trade expectancy
    else:
        expectancy = 0.0

    # Recovery factor
    recovery_factor = abs(total_net_profit / worst_drawdown) if worst_drawdown != 0 else 0.0

    return {
        "total_steps": total_periods,
        "total_trades": total_trades,
        "net_profit": round(total_net_profit, 2),
        "avg_profit_factor": round(avg_profit_factor, 4),
        "avg_sharpe": round(avg_sharpe, 4),
        "avg_win_rate": round(avg_win_rate, 2),
        "worst_drawdown": round(worst_drawdown, 2),
        "profitable_periods": profitable_periods,
        "profitable_periods_pct": round(profitable_periods / total_periods * 100, 1) if total_periods > 0 else 0,
        "avg_trade": round(avg_trade, 2),
        "expectancy": round(expectancy, 2),
        "recovery_factor": round(recovery_factor, 4),
        "max_consecutive_losing_periods": max_consec_losing,
        "step_profits": [round(p, 2) for p in step_profits],
        "step_trades": step_trades,
        "step_profit_factors": [round(p, 4) for p in step_pfs],
        "step_sharpes": [round(s, 4) for s in step_sharpes],
        "step_drawdowns": [round(d, 2) for d in step_dds],
        "step_win_rates": [round(w, 2) for w in step_win_rates],
    }


# =============================================================================
# ROBUSTNESS SCORING
# =============================================================================

def compute_robustness_score(
    oos_aggregate: Dict[str, Any],
    stability_score: float,
    weights: RobustnessWeights,
) -> Dict[str, Any]:
    """
    Compute multi-component robustness score.
    
    Components (all normalized to 0-1 range):
    - OOS Sharpe
    - OOS Profit Factor  
    - OOS Drawdown (lower = better)
    - OOS Return
    - Trade count
    - Consistency (% profitable OOS periods)
    - Parameter stability
    
    Returns:
        Dict with individual components + overall score + label
    """
    components = {}

    # OOS Sharpe (normalize: 0=bad, 1=excellent at Sharpe=3+)
    sharpe = oos_aggregate.get("avg_sharpe", 0)
    components["oos_sharpe"] = {
        "raw": sharpe,
        "normalized": min(max(sharpe / 3.0, 0), 1.0),
        "weight": weights.oos_sharpe,
    }

    # OOS Profit Factor (normalize: 1.0=breakeven, 2.0+=excellent)
    pf = oos_aggregate.get("avg_profit_factor", 0)
    components["oos_profit_factor"] = {
        "raw": pf,
        "normalized": min(max((pf - 1.0) / 1.5, 0), 1.0),
        "weight": weights.oos_profit_factor,
    }

    # OOS Drawdown (lower absolute = better)
    dd = abs(oos_aggregate.get("worst_drawdown", 0))
    net = abs(oos_aggregate.get("net_profit", 1)) or 1
    dd_ratio = dd / net if net > 0 else 1.0
    components["oos_drawdown"] = {
        "raw": -dd,
        "normalized": min(max(1.0 - dd_ratio, 0), 1.0),
        "weight": weights.oos_drawdown,
    }

    # OOS Return (positive net profit = good)
    net_profit = oos_aggregate.get("net_profit", 0)
    components["oos_return"] = {
        "raw": net_profit,
        "normalized": 1.0 if net_profit > 0 else 0.0,
        "weight": weights.oos_return,
    }

    # Trade count (more trades = more statistical significance)
    trades = oos_aggregate.get("total_trades", 0)
    components["trade_count"] = {
        "raw": trades,
        "normalized": min(trades / 500, 1.0),  # 500+ trades = full score
        "weight": weights.trade_count,
    }

    # Consistency (% of profitable OOS periods)
    consistency = oos_aggregate.get("profitable_periods_pct", 0) / 100.0
    components["consistency"] = {
        "raw": oos_aggregate.get("profitable_periods_pct", 0),
        "normalized": consistency,
        "weight": weights.consistency,
    }

    # Parameter stability (passed in from stability analysis)
    components["parameter_stability"] = {
        "raw": stability_score,
        "normalized": min(max(stability_score, 0), 1.0),
        "weight": weights.parameter_stability,
    }

    # Weighted overall score
    total_weight = sum(c["weight"] for c in components.values())
    if total_weight > 0:
        overall = sum(
            c["normalized"] * c["weight"] for c in components.values()
        ) / total_weight
    else:
        overall = 0.0

    # Determine label
    label = _determine_label(overall, components, oos_aggregate)

    return {
        "overall_score": round(overall, 4),
        "label": label,
        "components": components,
    }


def _determine_label(
    overall: float,
    components: Dict,
    aggregate: Dict,
) -> str:
    """Determine robustness label based on score and components."""
    total_trades = aggregate.get("total_trades", 0)
    total_steps = aggregate.get("total_steps", 0)
    profitable_pct = aggregate.get("profitable_periods_pct", 0)
    
    if total_steps < 3 or total_trades < 50:
        return RobustnessLabel.INSUFFICIENT.value

    if overall >= 0.75 and profitable_pct >= 75:
        return RobustnessLabel.ROBUST.value
    elif overall >= 0.55 and profitable_pct >= 60:
        return RobustnessLabel.PROMISING.value
    elif overall >= 0.35:
        # Check for overfit indicators
        stability = components.get("parameter_stability", {}).get("normalized", 0)
        if stability < 0.3:
            return RobustnessLabel.OVERFIT_RISK.value
        return RobustnessLabel.WEAK.value
    else:
        consistency = components.get("consistency", {}).get("normalized", 0)
        if consistency < 0.4:
            return RobustnessLabel.UNSTABLE.value
        return RobustnessLabel.WEAK.value

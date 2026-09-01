"""
walk_forward/stability.py
--------------------------
Parameter stability analysis across walk-forward steps.

Measures how stable optimized parameters are across multiple windows.
High stability (low CV) suggests the strategy has a robust parameter
region rather than being overfit to a specific data period.

Also generates candidate parameter sets using multiple methods.
"""
from typing import List, Dict, Any, Optional
import numpy as np

from .models import WFOStepResult, CandidateParams, CandidateMethod


# =============================================================================
# PARAMETER STABILITY
# =============================================================================

def compute_stability(
    step_results: List[WFOStepResult],
    param_names: List[str],
) -> Dict[str, Any]:
    """
    Compute parameter stability across WFO steps.
    
    For each parameter, computes:
    - Values per step
    - Mean
    - Std
    - CV (Coefficient of Variation = std/|mean|)
    - Range (min-max)
    
    Returns:
        Dict with 'parameters' (list of param stats) and 'overall_stability_score'
    """
    completed = [s for s in step_results if s.state == "completed" and s.selected_params]

    if not completed or not param_names:
        return {"parameters": [], "overall_stability_score": 0.0}

    param_stats = []
    cvs = []

    for name in param_names:
        values = []
        for s in completed:
            val = s.selected_params.get(name)
            if val is not None:
                try:
                    values.append(float(val))
                except (ValueError, TypeError):
                    # Skip non-numeric params (strings, booleans)
                    break

        if not values or len(values) < 2:
            # Non-numeric or insufficient data
            step_values = {f"step_{s.step}": s.selected_params.get(name) for s in completed}
            param_stats.append({
                "name": name,
                "values": step_values,
                "mean": None,
                "std": None,
                "cv": None,
                "min": None,
                "max": None,
                "is_numeric": False,
            })
            continue

        mean_val = float(np.mean(values))
        std_val = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
        cv = abs(std_val / mean_val) if abs(mean_val) > 1e-10 else 0.0

        step_values = {}
        for s in completed:
            val = s.selected_params.get(name)
            try:
                step_values[f"step_{s.step}"] = float(val)
            except (ValueError, TypeError):
                step_values[f"step_{s.step}"] = val

        param_stats.append({
            "name": name,
            "values": step_values,
            "mean": round(mean_val, 6),
            "std": round(std_val, 6),
            "cv": round(cv, 6),
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "is_numeric": True,
        })
        cvs.append(cv)

    # Overall stability score (1 - average CV, clamped to 0-1)
    if cvs:
        avg_cv = float(np.mean(cvs))
        overall_score = max(0.0, min(1.0, 1.0 - avg_cv))
    else:
        overall_score = 0.0

    return {
        "parameters": param_stats,
        "overall_stability_score": round(overall_score, 4),
    }


# =============================================================================
# CANDIDATE GENERATION
# =============================================================================

def generate_candidates(
    step_results: List[WFOStepResult],
    stability: Dict[str, Any],
    param_names: List[str],
) -> List[Dict[str, Any]]:
    """
    Generate multiple candidate parameter sets using different methods.
    
    Method A: Latest IS optimization parameters (most recent step)
    Method B: Most stable parameter region (median values)
    Method C: Robust aggregate (OOS-performance-weighted average)
    Method D: Each step's parameters (for user selection)
    
    Returns:
        List of candidate dicts
    """
    completed = [s for s in step_results if s.state == "completed" and s.selected_params]

    if not completed:
        return []

    candidates = []

    # Method A: Latest IS optimization parameters
    latest = completed[-1]
    candidates.append(CandidateParams(
        method=CandidateMethod.LATEST_IS.value,
        label="Latest IS Parameters",
        description=f"Parameters from the most recent IS optimization (Step {latest.step}). "
                    f"Best adapted to the latest market regime.",
        params=dict(latest.selected_params),
        source_step=latest.step,
        confidence="medium",
    ).to_dict())

    # Method B: Most stable parameter region (median values)
    stable_params = {}
    for p_stat in stability.get("parameters", []):
        name = p_stat["name"]
        if not p_stat.get("is_numeric", False):
            # For non-numeric, use the most common value
            vals = [s.selected_params.get(name) for s in completed if name in s.selected_params]
            if vals:
                # Most frequent value
                from collections import Counter
                stable_params[name] = Counter(vals).most_common(1)[0][0]
            continue

        values = [s.selected_params.get(name) for s in completed if name in s.selected_params]
        numeric_vals = []
        for v in values:
            try:
                numeric_vals.append(float(v))
            except (ValueError, TypeError):
                continue

        if numeric_vals:
            median_val = float(np.median(numeric_vals))
            # Preserve integer types
            orig_val = completed[0].selected_params.get(name)
            if isinstance(orig_val, int):
                stable_params[name] = int(round(median_val))
            else:
                stable_params[name] = round(median_val, 6)

    if stable_params:
        candidates.append(CandidateParams(
            method=CandidateMethod.MOST_STABLE.value,
            label="Most Stable Region",
            description="Median parameter values across all steps. Represents the most "
                        "stable parameter region with lowest variance.",
            params=stable_params,
            confidence="high" if stability.get("overall_stability_score", 0) > 0.7 else "medium",
        ).to_dict())

    # Method C: Robust aggregate (weighted by OOS performance)
    # Weight by OOS net profit (only positive profits contribute)
    positive_steps = [s for s in completed if s.oos_net_profit > 0]
    if positive_steps and len(positive_steps) >= 2:
        total_profit = sum(s.oos_net_profit for s in positive_steps)
        if total_profit > 0:
            weighted_params = {}
            for name in param_names:
                weighted_sum = 0.0
                weight_sum = 0.0
                is_int = False

                for s in positive_steps:
                    val = s.selected_params.get(name)
                    if val is None:
                        continue
                    try:
                        fval = float(val)
                        if isinstance(val, int):
                            is_int = True
                        weight = s.oos_net_profit / total_profit
                        weighted_sum += fval * weight
                        weight_sum += weight
                    except (ValueError, TypeError):
                        break

                if weight_sum > 0:
                    result = weighted_sum / weight_sum
                    weighted_params[name] = int(round(result)) if is_int else round(result, 6)

            if weighted_params:
                candidates.append(CandidateParams(
                    method=CandidateMethod.ROBUST_AGGREGATE.value,
                    label="Robust Aggregate",
                    description="Performance-weighted average of parameters from profitable "
                                "OOS periods. Balances stability with OOS effectiveness.",
                    params=weighted_params,
                    confidence="medium",
                ).to_dict())

    # Method D: User-selectable step parameters
    for s in completed:
        oos_label = "profitable" if s.oos_net_profit > 0 else "unprofitable"
        candidates.append(CandidateParams(
            method=CandidateMethod.USER_SELECTED.value,
            label=f"Step {s.step} Parameters",
            description=f"Parameters from Step {s.step} ({s.window.is_start} to {s.window.is_end}). "
                        f"OOS: ${s.oos_net_profit:.2f} ({oos_label})",
            params=dict(s.selected_params),
            source_step=s.step,
            confidence="low",
        ).to_dict())

    return candidates

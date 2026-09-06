"""
walk_forward/report.py
-----------------------
Detailed post-run report for a Walk-Forward Testing run.

The aggregate files a run already writes answer "what were the numbers?".
This module answers "what do the numbers mean?" — it stitches every step's
OOS trades into one chronological equity curve, compares in-sample against
out-of-sample step by step, and turns the result into a written summary with
explicit findings.

Nothing here re-runs a backtest. Every input is read from what the run
already persisted, so a report can be (re)generated for any finished run —
including runs that completed before this module existed.

Outputs, all under wfo_runs/wfo_<run_id>/aggregate/:
    report.json               the structured report (served to the dashboard)
    report.html               a standalone, self-contained page
    combined_oos_trades.csv   every OOS trade, in chronological order
    combined_oos_equity.csv   the equity curve those trades produce
"""
import html
import json
import math
import os
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import persistence
from .models import WFOConfig
from .window_generator import generate_windows


# Columns a strategy might use for realised per-trade profit. The first four
# match worker.compute_basic_metrics_from_trades so a trade log that already
# works with the optimizer works here too.
PNL_COLUMNS = [
    "net_pnl", "pnl", "profit", "Net PnL", "P&L", "realized_pnl",
    "profit_with_hedges_inr", "profit_with_hedges_points",
    "profit_in_inr", "profit_points", "net_profit",
]

# Preferred order for the timestamp that places a trade on the calendar: a
# trade belongs to the moment it was closed, not the moment it was opened.
TIME_COLUMN_HINTS = [
    ("exit", "time"), ("exit", "stamp"), ("exit", "date"),
    ("entry", "time"), ("entry", "stamp"), ("entry", "date"),
    ("close", "time"), ("", "date"),
]

TRADING_DAYS_PER_YEAR = 252


# =============================================================================
# SMALL HELPERS
# =============================================================================

def _f(value, default: float = 0.0) -> float:
    """float() that survives None, NaN, '', and stray strings."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _round(value, digits: int = 2):
    v = _f(value, float("nan"))
    if math.isnan(v):
        return None
    return round(v, digits)


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def _months_between(start, end) -> float:
    """Calendar span in months — used to put IS and OOS on the same footing."""
    a, b = _parse_date(start), _parse_date(end)
    if not a or not b:
        return 0.0
    return max((b - a).days / 30.4375, 0.0)


def find_pnl_column(df: pd.DataFrame) -> Optional[str]:
    for col in PNL_COLUMNS:
        if col in df.columns:
            return col
    # Fall back to any numeric column whose name mentions profit or pnl.
    for col in df.columns:
        low = col.lower()
        if ("pnl" in low or "profit" in low) and pd.api.types.is_numeric_dtype(df[col]):
            return col
    return None


def find_time_column(df: pd.DataFrame) -> Optional[str]:
    lowered = {c.lower(): c for c in df.columns}
    for first, second in TIME_COLUMN_HINTS:
        for low, original in lowered.items():
            if first and first not in low:
                continue
            if second and second not in low:
                continue
            return original
    return None


# =============================================================================
# TRADE LOADING
# =============================================================================

def load_step_trades(run_id: str, step: int) -> Optional[pd.DataFrame]:
    """
    Load one step's OOS trades as a two-column frame: time, pnl.

    Returns None when the step has no usable trade log — a step can legitimately
    produce zero trades, and an unreadable log must not sink the whole report.
    """
    step_dir = persistence.get_step_dir(run_id, step)
    candidates = [
        os.path.join(step_dir, "oos_trades.csv"),
        os.path.join(step_dir, "oos_run", "python_trades_M1.csv"),
    ]
    # Any *trade*.csv the strategy wrote inside its OOS run directory.
    oos_run = os.path.join(step_dir, "oos_run")
    if os.path.isdir(oos_run):
        for name in sorted(os.listdir(oos_run)):
            if "trade" in name.lower() and name.lower().endswith(".csv"):
                candidates.append(os.path.join(oos_run, name))

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        pnl_col = find_pnl_column(df)
        if pnl_col is None or df.empty:
            continue

        out = pd.DataFrame({"pnl": pd.to_numeric(df[pnl_col], errors="coerce")})
        time_col = find_time_column(df)
        if time_col is not None:
            out["time"] = pd.to_datetime(df[time_col], errors="coerce")
        else:
            out["time"] = pd.NaT
        out["step"] = step
        out = out.dropna(subset=["pnl"])
        if out.empty:
            continue
        # Chronological within the step when timestamps exist; otherwise the
        # file's own order is the best available proxy for sequence.
        if out["time"].notna().any():
            out = out.sort_values("time", kind="stable")
        return out.reset_index(drop=True)

    return None


def load_combined_oos_trades(run_id: str, steps: List[int]) -> pd.DataFrame:
    """
    Every OOS trade from every completed step, in walk-forward order.

    Steps are concatenated by step number rather than sorted globally by
    timestamp: the walk-forward sequence *is* the chronology, and sorting
    globally would silently reorder trades if two windows overlap.
    """
    frames = []
    for step in steps:
        df = load_step_trades(run_id, step)
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["step", "time", "pnl"])
    combined = pd.concat(frames, ignore_index=True)
    return combined[["step", "time", "pnl"]]


# =============================================================================
# TRADE-LEVEL ANALYTICS
# =============================================================================

def drawdown_profile(equity: np.ndarray) -> Dict[str, Any]:
    """Max drawdown, where it happened, and how long it lasted (in trades)."""
    if equity.size == 0:
        return {"max_drawdown": 0.0, "max_drawdown_pct": 0.0,
                "trough_index": 0, "longest_underwater_trades": 0,
                "recovered": True}

    running_max = np.maximum.accumulate(equity)
    drawdown = equity - running_max
    trough = int(np.argmin(drawdown))
    max_dd = float(drawdown[trough])

    peak_value = float(running_max[trough])
    max_dd_pct = (max_dd / peak_value * 100.0) if peak_value > 0 else 0.0

    # Longest run of consecutive trades spent below a previous peak.
    underwater = drawdown < 0
    longest = current = 0
    for flag in underwater:
        current = current + 1 if flag else 0
        longest = max(longest, current)

    return {
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "trough_index": trough,
        "longest_underwater_trades": int(longest),
        "recovered": bool(equity[-1] >= running_max[trough]),
    }


def trade_statistics(pnl: np.ndarray, initial_capital: float,
                     span_days: Optional[float] = None) -> Dict[str, Any]:
    """Full performance statistics for one sequence of trade P&Ls."""
    n = int(pnl.size)
    if n == 0:
        return {"total_trades": 0}

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(losses.sum())
    net_profit = float(pnl.sum())

    equity = initial_capital + np.cumsum(pnl)
    dd = drawdown_profile(equity)

    std = float(pnl.std(ddof=1)) if n > 1 else 0.0
    if std > 0 and span_days and span_days > 0:
        trades_per_year = n / (span_days / 365.25)
        sharpe = float(pnl.mean() / std * math.sqrt(max(trades_per_year, 1.0)))
    elif std > 0:
        # No calendar information — fall back to the optimizer's convention.
        sharpe = float(pnl.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR))
    else:
        sharpe = 0.0

    # Longest winning and losing streaks.
    max_win_streak = max_loss_streak = win_streak = loss_streak = 0
    for value in pnl:
        if value > 0:
            win_streak, loss_streak = win_streak + 1, 0
        elif value < 0:
            loss_streak, win_streak = loss_streak + 1, 0
        else:
            win_streak = loss_streak = 0
        max_win_streak = max(max_win_streak, win_streak)
        max_loss_streak = max(max_loss_streak, loss_streak)

    avg_win = float(wins.mean()) if wins.size else 0.0
    avg_loss = float(losses.mean()) if losses.size else 0.0
    win_rate = wins.size / n * 100.0

    return {
        "total_trades": n,
        "winning_trades": int(wins.size),
        "losing_trades": int(losses.size),
        "win_rate": round(win_rate, 2),
        "net_profit": round(net_profit, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(abs(gross_profit / gross_loss), 4) if gross_loss else 0.0,
        "avg_trade": round(net_profit / n, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff_ratio": round(abs(avg_win / avg_loss), 4) if avg_loss else 0.0,
        "expectancy": round(
            (win_rate / 100.0) * avg_win + (1 - win_rate / 100.0) * avg_loss, 2),
        "best_trade": round(float(pnl.max()), 2),
        "worst_trade": round(float(pnl.min()), 2),
        "std_dev": round(std, 4),
        "sharpe_ratio": round(sharpe, 4),
        "max_consecutive_wins": int(max_win_streak),
        "max_consecutive_losses": int(max_loss_streak),
        "final_equity": round(float(equity[-1]), 2),
        "return_pct": round(net_profit / initial_capital * 100.0, 2) if initial_capital else 0.0,
        "recovery_factor": round(abs(net_profit / dd["max_drawdown"]), 4)
                           if dd["max_drawdown"] else 0.0,
        **dd,
    }


def monthly_breakdown(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    """Per-month P&L, for runs whose trade logs carry timestamps."""
    if trades.empty or trades["time"].isna().all():
        return []
    dated = trades.dropna(subset=["time"]).copy()
    if dated.empty:
        return []
    dated["month"] = dated["time"].dt.to_period("M").astype(str)
    grouped = dated.groupby("month")["pnl"]
    out = []
    for month, series in grouped:
        values = series.to_numpy(dtype=float)
        out.append({
            "month": month,
            "trades": int(values.size),
            "net_profit": round(float(values.sum()), 2),
            "win_rate": round(float((values > 0).sum()) / values.size * 100.0, 1),
        })
    return out


def sample_equity_curve(trades: pd.DataFrame, initial_capital: float,
                        max_points: int = 400) -> List[Dict[str, Any]]:
    """
    The combined OOS equity curve, downsampled for the browser.

    A 5,000-trade curve is plotted a few hundred pixels wide, so sending every
    point is wasted bandwidth. Peaks and troughs are preserved by keeping the
    stride uniform and always including the final point.
    """
    if trades.empty:
        return []
    pnl = trades["pnl"].to_numpy(dtype=float)
    equity = initial_capital + np.cumsum(pnl)
    n = equity.size
    stride = max(1, n // max_points)
    idx = list(range(0, n, stride))
    if idx[-1] != n - 1:
        idx.append(n - 1)

    times = trades["time"].to_numpy()
    steps = trades["step"].to_numpy()
    points = []
    for i in idx:
        ts = times[i]
        points.append({
            "i": int(i + 1),
            "equity": round(float(equity[i]), 2),
            "step": int(steps[i]),
            "date": (pd.Timestamp(ts).strftime("%Y-%m-%d")
                     if not pd.isna(ts) else None),
        })
    return points


# =============================================================================
# IN-SAMPLE VS OUT-OF-SAMPLE
# =============================================================================

def load_is_metrics(run_id: str, step: int) -> Dict[str, Any]:
    """The IS metrics of the parameter set that step selected."""
    path = os.path.join(persistence.get_step_dir(run_id, step), "is_metrics.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {}
    return (data.get("selection_info") or {}).get("metrics", {}) or {}


def step_efficiency(is_metrics: Dict[str, Any], oos_metrics: Dict[str, Any],
                    is_months: float, oos_months: float) -> Optional[float]:
    """
    Walk-forward efficiency for one step: OOS profit rate ÷ IS profit rate.

    Rates rather than totals, because the IS window is normally several times
    longer than the OOS window — comparing raw profit would make every step
    look like a catastrophic degradation.

    Returns None when the comparison is meaningless (no IS profit to degrade
    from, or missing window lengths).
    """
    if is_months <= 0 or oos_months <= 0:
        return None
    is_profit = _f(is_metrics.get("Net Profit"), float("nan"))
    oos_profit = _f(oos_metrics.get("Net Profit"), float("nan"))
    if math.isnan(is_profit) or math.isnan(oos_profit):
        return None
    is_rate = is_profit / is_months
    if is_rate <= 0:
        # A step whose IS parameters were not profitable in-sample has no edge
        # to carry forward; efficiency is undefined rather than zero.
        return None
    return round((oos_profit / oos_months) / is_rate, 4)


def build_is_oos_comparison(run_id: str, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Step-by-step IS vs OOS table plus the aggregate degradation picture."""
    rows = []
    efficiencies = []
    is_total = oos_total = 0.0
    is_positive_oos_negative = 0

    for s in steps:
        step_no = int(s.get("step", 0))
        is_metrics = load_is_metrics(run_id, step_no)
        oos_metrics = persistence.load_oos_metrics(run_id, step_no) or {}

        is_months = _months_between(s.get("is_start"), s.get("is_end"))
        oos_months = _months_between(s.get("oos_start"), s.get("oos_end"))
        eff = step_efficiency(is_metrics, oos_metrics, is_months, oos_months)
        if eff is not None:
            efficiencies.append(eff)

        is_profit = _f(is_metrics.get("Net Profit"))
        oos_profit = _f(oos_metrics.get("Net Profit"))
        is_total += is_profit
        oos_total += oos_profit
        if is_profit > 0 and oos_profit < 0:
            is_positive_oos_negative += 1

        rows.append({
            "step": step_no,
            "is_period": f"{s.get('is_start', '')} → {s.get('is_end', '')}",
            "oos_period": f"{s.get('oos_start', '')} → {s.get('oos_end', '')}",
            "is_months": round(is_months, 1),
            "oos_months": round(oos_months, 1),
            "is_net_profit": round(is_profit, 2),
            "oos_net_profit": round(oos_profit, 2),
            "is_profit_factor": _round(is_metrics.get("Profit Factor"), 4),
            "oos_profit_factor": _round(oos_metrics.get("Profit Factor"), 4),
            "is_sharpe": _round(is_metrics.get("Sharpe Ratio"), 4),
            "oos_sharpe": _round(oos_metrics.get("Sharpe Ratio"), 4),
            "is_trades": int(_f(is_metrics.get("Total Trades"))),
            "oos_trades": int(_f(oos_metrics.get("Total Trades"))),
            "efficiency": eff,
        })

    overall = None
    if efficiencies:
        overall = round(float(np.median(efficiencies)), 4)

    return {
        "steps": rows,
        "median_efficiency": overall,
        "mean_efficiency": round(float(np.mean(efficiencies)), 4) if efficiencies else None,
        "steps_with_efficiency": len(efficiencies),
        "is_total_net_profit": round(is_total, 2),
        "oos_total_net_profit": round(oos_total, 2),
        "is_positive_oos_negative": is_positive_oos_negative,
    }


# =============================================================================
# OOS INTEGRITY
# =============================================================================

def _dates_from_params(params: Dict[str, Any], config: WFOConfig) -> Tuple[Optional[str], Optional[str]]:
    """Pull the backtest window back out of a saved params.json."""
    name = config.date_param_name
    if config.date_param_style == "nested" and name and isinstance(params.get(name), dict):
        container = params[name]
        return container.get("start_date"), container.get("end_date")
    if name and f"{name}.start_date" in params:
        return params.get(f"{name}.start_date"), params.get(f"{name}.end_date")
    return params.get("start_date"), params.get("end_date")


def check_oos_integrity(run_id: str, config: WFOConfig,
                        step_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Verify that each step's OOS backtest actually ran on its OOS window.

    The whole value of walk-forward testing rests on this one property, and it
    is cheap to confirm: every OOS run left the parameters it was given on
    disk. A mismatch means the numbers on this page describe data the
    optimizer had already seen, and the run proves nothing.
    """
    checked, verified, mismatched = 0, 0, []

    for row in step_rows:
        step = int(row.get("step", 0))
        params_path = os.path.join(
            persistence.get_step_dir(run_id, step), "oos_run", "params.json")
        if not os.path.exists(params_path):
            continue
        try:
            with open(params_path) as f:
                params = json.load(f)
        except Exception:
            continue

        checked += 1
        actual_start, actual_end = _dates_from_params(params, config)
        expected_start = row.get("oos_start")
        expected_end = row.get("oos_end")
        if not expected_start or not expected_end or not actual_start:
            continue

        if str(actual_start) == str(expected_start) and str(actual_end) == str(expected_end):
            verified += 1
        else:
            mismatched.append({
                "step": step,
                "expected": f"{expected_start} → {expected_end}",
                "actual": f"{actual_start} → {actual_end}",
                "ran_in_sample_window": (str(actual_start) == str(row.get("is_start"))
                                         and str(actual_end) == str(row.get("is_end"))),
            })

    if checked == 0:
        status = "unknown"
    elif mismatched:
        status = "mismatch"
    else:
        status = "ok"

    return {
        "status": status,
        "checked": checked,
        "verified": verified,
        "mismatched": mismatched,
    }


# =============================================================================
# FINDINGS AND VERDICT
# =============================================================================

def build_findings(oos_agg: Dict[str, Any], combined: Dict[str, Any],
                   is_oos: Dict[str, Any], stability: List[Dict[str, Any]],
                   robustness: Dict[str, Any],
                   steps: List[Dict[str, Any]],
                   integrity: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Concrete, checkable observations — the part of the report worth reading
    before the numbers. Each finding names the evidence that produced it.
    """
    findings = []

    def add(level, title, detail):
        findings.append({"level": level, "title": title, "detail": detail})

    # ---- Integrity first: it decides whether the rest means anything -------
    if integrity.get("status") == "mismatch":
        bad = integrity["mismatched"]
        in_sample = [m for m in bad if m.get("ran_in_sample_window")]
        detail = (f"{len(bad)} of {integrity['checked']} step(s) ran a backtest "
                  f"window other than the one they were supposed to validate on. ")
        if in_sample:
            detail += (f"{len(in_sample)} of them re-ran their own in-sample "
                       f"window (step {in_sample[0]['step']}: expected "
                       f"{in_sample[0]['expected']}, actually ran "
                       f"{in_sample[0]['actual']}). ")
        detail += ("Every out-of-sample figure on this page therefore describes "
                   "data the optimizer had already seen. Re-run this "
                   "walk-forward test before drawing any conclusion from it.")
        add("warning", "Out-of-sample windows were not honoured", detail)
    elif integrity.get("status") == "ok":
        add("good", "Out-of-sample windows verified",
            f"All {integrity['verified']} step(s) ran their backtest on exactly "
            "the out-of-sample window they were assigned — confirmed against "
            "the parameters each OOS run recorded on disk.")

    total_steps = int(oos_agg.get("total_steps", 0) or 0)
    total_trades = int(combined.get("total_trades", oos_agg.get("total_trades", 0)) or 0)
    profitable_pct = _f(oos_agg.get("profitable_periods_pct"))
    failed_steps = [s for s in steps if s.get("state") not in ("completed", None)]

    # ---- Sample size -------------------------------------------------------
    if total_steps < 3:
        add("warning", "Too few walk-forward steps",
            f"Only {total_steps} step(s) completed. Three or more OOS periods "
            "are the minimum for any statement about consistency.")
    if total_trades < 100:
        add("warning", "Small out-of-sample trade sample",
            f"{total_trades} OOS trades in total. Metrics computed on fewer "
            "than ~100 trades move a lot with a handful of outcomes.")
    elif total_trades >= 500:
        add("good", "Healthy out-of-sample sample size",
            f"{total_trades} OOS trades across {total_steps} periods.")

    if failed_steps:
        add("warning", f"{len(failed_steps)} step(s) did not complete",
            "Steps " + ", ".join(str(s.get("step")) for s in failed_steps) +
            " are excluded from every aggregate on this page.")

    # ---- Consistency -------------------------------------------------------
    if total_steps:
        if profitable_pct >= 70:
            add("good", "Consistent across periods",
                f"{oos_agg.get('profitable_periods', 0)} of {total_steps} OOS "
                f"periods were profitable ({profitable_pct:.0f}%).")
        elif profitable_pct < 50:
            add("warning", "Majority of OOS periods lost money",
                f"Only {oos_agg.get('profitable_periods', 0)} of {total_steps} "
                f"periods were profitable ({profitable_pct:.0f}%). Total profit "
                "is being carried by a minority of windows.")

    consec = int(oos_agg.get("max_consecutive_losing_periods", 0) or 0)
    if consec >= 3:
        add("warning", "Extended losing stretch",
            f"{consec} consecutive OOS periods lost money. Live, that is the "
            "stretch you would have to sit through before the edge reappears.")

    # ---- IS → OOS degradation ---------------------------------------------
    # Skipped entirely when the OOS windows were not honoured: comparing a
    # window against itself always looks like a perfect carry-over, and saying
    # so here would flatly contradict the integrity warning above.
    eff = None if integrity.get("status") == "mismatch" else is_oos.get("median_efficiency")
    if eff is not None:
        if eff >= 0.75:
            add("good", "Out-of-sample held up against in-sample",
                f"Median walk-forward efficiency {eff:.2f} — OOS captured about "
                f"{eff * 100:.0f}% of the in-sample profit rate.")
        elif eff >= 0.4:
            add("info", "Moderate out-of-sample degradation",
                f"Median walk-forward efficiency {eff:.2f}. Some decay from IS "
                "to OOS is normal; this is within the range where the edge "
                "survives but is smaller than the optimizer suggests.")
        else:
            add("warning", "Heavy out-of-sample degradation",
                f"Median walk-forward efficiency {eff:.2f} — OOS kept only "
                f"{max(eff, 0) * 100:.0f}% of the in-sample profit rate. That "
                "gap is the signature of parameters fitted to noise.")

    flipped = int(is_oos.get("is_positive_oos_negative", 0) or 0)
    if flipped and total_steps and integrity.get("status") != "mismatch":
        share = flipped / total_steps * 100
        level = "warning" if share >= 40 else "info"
        add(level, f"{flipped} step(s) profitable in-sample, losing out-of-sample",
            f"{share:.0f}% of steps reversed sign between IS and OOS.")

    # ---- Drawdown ----------------------------------------------------------
    combined_dd = _f(combined.get("max_drawdown"))
    worst_step_dd = _f(oos_agg.get("worst_drawdown"))
    if combined_dd < 0 and worst_step_dd < 0 and abs(combined_dd) > abs(worst_step_dd) * 1.5:
        add("warning", "Drawdowns chain across period boundaries",
            f"The combined OOS curve draws down {abs(combined_dd):,.0f} versus "
            f"{abs(worst_step_dd):,.0f} for the worst single period. Per-period "
            "drawdown understates what a continuous account would have felt.")
    if combined.get("longest_underwater_trades"):
        add("info", "Time spent below a previous peak",
            f"The longest underwater stretch lasted "
            f"{combined['longest_underwater_trades']} consecutive trades.")

    # ---- Parameter stability ----------------------------------------------
    unstable = [p for p in stability
                if p.get("is_numeric") and _f(p.get("cv")) > 0.5]
    stable = [p for p in stability
              if p.get("is_numeric") and 0 < _f(p.get("cv")) <= 0.15]
    if unstable:
        names = ", ".join(f"{p['name']} (CV {_f(p.get('cv')):.2f})" for p in unstable[:5])
        add("warning", f"{len(unstable)} parameter(s) unstable across steps",
            f"{names}. A parameter the optimizer re-picks from a different "
            "region every window is fitting the window, not the market.")
    if stable:
        names = ", ".join(p["name"] for p in stable[:5])
        add("good", f"{len(stable)} parameter(s) stable across steps",
            f"{names} varied by less than 15% of their mean — evidence of a "
            "real parameter plateau rather than a spike.")

    # ---- Profitability -----------------------------------------------------
    net = _f(oos_agg.get("net_profit"))
    if net <= 0:
        add("warning", "Out-of-sample total is not profitable",
            f"Combined OOS net profit is {net:,.2f}.")

    pf = _f(combined.get("profit_factor"))
    if pf and pf < 1.1:
        add("warning", "Thin profit factor",
            f"Combined OOS profit factor {pf:.2f}. Below roughly 1.1 there is "
            "little room for slippage, spread widening, or missed fills.")

    label = robustness.get("label")
    if label:
        add("info", f"Robustness assessment: {label}",
            f"Weighted robustness score "
            f"{_f(robustness.get('overall_score')) * 100:.1f}%.")

    return findings


def build_summary(config: WFOConfig, oos_agg: Dict[str, Any],
                  combined: Dict[str, Any], is_oos: Dict[str, Any],
                  robustness: Dict[str, Any],
                  integrity: Dict[str, Any]) -> List[str]:
    """The written summary — a few sentences a person can read out loud."""
    lines = []
    if integrity.get("status") == "mismatch":
        lines.append(
            f"READ THIS FIRST: {len(integrity['mismatched'])} of "
            f"{integrity['checked']} steps did not run on their assigned "
            f"out-of-sample window, so nothing below is out-of-sample evidence. "
            f"The figures are reproduced only so the run can be diagnosed; the "
            f"walk-forward test needs to be re-run before it means anything."
        )

    total_steps = int(oos_agg.get("total_steps", 0) or 0)
    net = _f(oos_agg.get("net_profit"))
    trades = int(combined.get("total_trades", oos_agg.get("total_trades", 0)) or 0)

    mode = "expanding" if config.window_mode == "expanding" else "rolling"
    lines.append(
        f"{config.strategy_name or 'The strategy'} was walk-forward tested over "
        f"{total_steps} {mode} step(s) between {config.wfo_start or '?'} and "
        f"{config.wfo_end or '?'}, optimising {config.is_duration_months} month(s) "
        f"in-sample and validating the next {config.oos_duration_months} month(s) "
        f"out-of-sample, stepping forward {config.step_duration_months} month(s) "
        f"each time. Each in-sample window ran {config.optimization_iterations} "
        f"{config.optimization_method} iterations."
    )

    verb = "made" if net >= 0 else "lost"
    lines.append(
        f"Across all out-of-sample periods the strategy {verb} "
        f"{abs(net):,.2f} on {trades} trades, with "
        f"{oos_agg.get('profitable_periods', 0)} of {total_steps} periods "
        f"profitable ({_f(oos_agg.get('profitable_periods_pct')):.0f}%). "
        f"Stitched into one continuous account the OOS curve peaked-to-troughed "
        f"{abs(_f(combined.get('max_drawdown'))):,.2f} "
        f"({abs(_f(combined.get('max_drawdown_pct'))):.1f}% of equity at the peak), "
        f"finishing with a profit factor of {_f(combined.get('profit_factor')):.2f} "
        f"and a win rate of {_f(combined.get('win_rate')):.1f}%."
    )

    eff = None if integrity.get("status") == "mismatch" else is_oos.get("median_efficiency")
    if eff is not None:
        lines.append(
            f"Median walk-forward efficiency was {eff:.2f}: for every unit of "
            f"monthly profit the optimizer found in-sample, roughly "
            f"{max(eff, 0):.2f} survived into the untouched data. "
            + ("That is a healthy carry-over." if eff >= 0.75 else
               "That is a meaningful haircut but not a collapse." if eff >= 0.4 else
               "Most of the in-sample edge did not survive.")
        )

    label = robustness.get("label", "")
    score = _f(robustness.get("overall_score")) * 100
    if label:
        lines.append(
            f"Weighing out-of-sample performance, consistency and parameter "
            f"stability together, this run scores {score:.1f}% and is labelled "
            f"\"{label}\". This is an assessment of the evidence collected here, "
            f"not a forecast."
        )

    return lines


def build_verdict(robustness: Dict[str, Any], findings: List[Dict[str, str]],
                  integrity: Dict[str, Any]) -> Dict[str, str]:
    """A short headline plus what to do next, derived from the findings."""
    label = robustness.get("label", "Insufficient Evidence")
    warnings = [f for f in findings if f["level"] == "warning"]

    # A run whose OOS windows were not honoured has no verdict to give: the
    # score was computed from in-sample data wearing an out-of-sample label.
    if integrity.get("status") == "mismatch":
        return {
            "label": "Invalid — OOS Not Honoured",
            "score_pct": round(_f(robustness.get("overall_score")) * 100, 1),
            "warning_count": len(warnings),
            "recommendation": (
                "Do not act on this run. Its out-of-sample backtests were "
                "executed on the wrong date windows, so the scores below "
                "measure in-sample fit, not validation. Re-run the "
                "walk-forward test and read the new report instead."),
        }

    recommendations = {
        "Robust": "Evidence supports moving to forward/paper testing with the "
                  "candidate parameters. Size conservatively and keep watching "
                  "the drawdown profile against what is shown here.",
        "Promising": "Worth carrying forward, but validate on a longer history "
                     "or a second instrument before committing capital.",
        "Weak": "The edge is present but thin. Address the warnings below "
                "before trusting these parameters with real money.",
        "Unstable": "Results swing too much between periods to rely on. Revisit "
                    "the strategy logic or widen the parameter ranges rather "
                    "than re-optimising the same space.",
        "Overfit Risk": "Parameter choices move sharply window to window while "
                        "out-of-sample results lag in-sample. Reduce the number "
                        "of optimised parameters and re-test.",
        "Insufficient Evidence": "Not enough completed steps or trades to draw a "
                                 "conclusion. Extend the date range or shorten "
                                 "the step size to produce more OOS periods.",
    }

    return {
        "label": label,
        "score_pct": round(_f(robustness.get("overall_score")) * 100, 1),
        "warning_count": len(warnings),
        "recommendation": recommendations.get(
            label, "Review the findings below before acting on these parameters."),
    }


# =============================================================================
# REPORT ASSEMBLY
# =============================================================================

def _initial_capital(config: WFOConfig) -> float:
    """Starting equity, for percentage returns and drawdowns."""
    for key in ("initial_capital", "starting_capital", "capital", "initial_balance"):
        if key in (config.fixed_params or {}):
            value = _f(config.fixed_params[key])
            if value > 0:
                return value
    return 100000.0


def build_report(run_id: str) -> Dict[str, Any]:
    """Assemble the full report for a run from everything it persisted."""
    config = persistence.load_wfo_config(run_id)
    if config is None:
        raise FileNotFoundError(f"WFO run not found: {run_id}")

    progress = persistence.load_wfo_progress(run_id) or {}
    aggregate = persistence.load_aggregate_results(run_id) or {}

    summary_blob = aggregate.get("summary", {}) or {}
    oos_agg = summary_blob.get("oos_aggregate", {}) or {}
    robustness = aggregate.get("robustness", summary_blob.get("robustness", {})) or {}
    stability = aggregate.get("stability", []) or []
    candidates = aggregate.get("candidates", []) or []
    step_rows = aggregate.get("step_results", []) or []

    # A run that never reached aggregation still has per-step files on disk.
    if not step_rows:
        step_rows = _step_rows_from_disk(run_id, config)

    completed_steps = [int(s["step"]) for s in step_rows
                       if str(s.get("state", "")) == "completed"]

    trades = load_combined_oos_trades(run_id, completed_steps)
    capital = _initial_capital(config)

    span_days = None
    if not trades.empty and trades["time"].notna().any():
        valid = trades["time"].dropna()
        span_days = max((valid.max() - valid.min()).days, 1)

    combined = trade_statistics(
        trades["pnl"].to_numpy(dtype=float), capital, span_days)
    combined["span_days"] = span_days
    combined["initial_capital"] = capital

    is_oos = build_is_oos_comparison(run_id, step_rows)
    integrity = check_oos_integrity(run_id, config, step_rows)
    findings = build_findings(oos_agg, combined, is_oos, stability,
                              robustness, step_rows, integrity)

    report = {
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": progress.get("status", "unknown"),
        "strategy": {
            "name": config.strategy_name,
            "path": config.strategy_path,
            "data_path": config.data_path,
            "timeframe": config.timeframe,
        },
        "configuration": {
            "window_mode": config.window_mode,
            "is_duration_months": config.is_duration_months,
            "oos_duration_months": config.oos_duration_months,
            "step_duration_months": config.step_duration_months,
            "wfo_start": config.wfo_start,
            "wfo_end": config.wfo_end,
            "dataset_start": config.dataset_start,
            "dataset_end": config.dataset_end,
            "optimization_method": config.optimization_method,
            "optimization_iterations": config.optimization_iterations,
            "num_workers": config.num_workers,
            "seed": config.seed,
            "ranking_metric": config.ranking_metric,
            "selection_metric": config.selection.primary_metric,
            "selection_direction": config.selection.primary_direction,
            "optimized_parameters": list((config.optimize_params or {}).keys()),
            "drawdown_optimization": config.drawdown_optimization,
            "created_at": config.created_at,
            "initial_capital": capital,
        },
        "verdict": build_verdict(robustness, findings, integrity),
        "summary": build_summary(config, oos_agg, combined, is_oos,
                                 robustness, integrity),
        "findings": findings,
        "integrity": integrity,
        "oos_aggregate": oos_agg,
        "combined_oos": combined,
        "equity_curve": sample_equity_curve(trades, capital),
        "monthly": monthly_breakdown(trades),
        "is_vs_oos": is_oos,
        "steps": step_rows,
        "stability": stability,
        "robustness": robustness,
        "candidates": candidates,
    }
    return report


def _step_rows_from_disk(run_id: str, config: WFOConfig) -> List[Dict[str, Any]]:
    """Rebuild the step table for a run that stopped before aggregation."""
    try:
        windows = generate_windows(config)
    except Exception:
        return []

    rows = []
    for w in windows:
        state = persistence.load_step_state(run_id, w.step)
        oos = persistence.load_oos_metrics(run_id, w.step) or {}
        params = persistence.load_selected_params(run_id, w.step) or {}
        rows.append({
            "step": w.step,
            "state": state,
            "is_start": w.is_start,
            "is_end": w.is_end,
            "oos_start": w.oos_start,
            "oos_end": w.oos_end,
            "is_score": 0.0,
            "oos_net_profit": _f(oos.get("Net Profit")),
            "oos_profit_factor": _f(oos.get("Profit Factor")),
            "oos_sharpe": _f(oos.get("Sharpe Ratio")),
            "oos_max_drawdown": _f(oos.get("Overall Max Drawdown")),
            "oos_win_rate": _f(oos.get("Win Rate %")),
            "oos_trades": int(_f(oos.get("Total Trades"))),
            "selected_params": json.dumps(params, default=str),
        })
    return rows


def save_combined_trade_files(run_id: str, trades: pd.DataFrame,
                              initial_capital: float):
    """
    Write the two combined CSVs the persistence layout has always documented.

    They are the natural export for anything downstream — Monte Carlo, a
    spreadsheet, an external analytics tool — and until now nothing produced
    them.
    """
    agg_dir = persistence.get_aggregate_dir(run_id)
    os.makedirs(agg_dir, exist_ok=True)
    if trades.empty:
        return

    out = trades.copy()
    out.to_csv(os.path.join(agg_dir, "combined_oos_trades.csv"), index=False)

    equity = initial_capital + out["pnl"].cumsum()
    running_max = equity.cummax()
    pd.DataFrame({
        "trade_index": range(1, len(out) + 1),
        "step": out["step"].values,
        "time": out["time"].values,
        "pnl": out["pnl"].values,
        "equity": equity.values,
        "drawdown": (equity - running_max).values,
    }).to_csv(os.path.join(agg_dir, "combined_oos_equity.csv"), index=False)


def generate_report(run_id: str) -> Dict[str, Any]:
    """Build the report, persist report.json / report.html, and return it."""
    report = build_report(run_id)

    agg_dir = persistence.get_aggregate_dir(run_id)
    os.makedirs(agg_dir, exist_ok=True)

    with open(os.path.join(agg_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=persistence._json_default)

    with open(os.path.join(agg_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(render_html(report))

    # Regenerate the combined CSVs alongside the report so all three always
    # describe the same set of trades.
    config = persistence.load_wfo_config(run_id)
    completed = [int(s["step"]) for s in report["steps"]
                 if str(s.get("state", "")) == "completed"]
    trades = load_combined_oos_trades(run_id, completed)
    save_combined_trade_files(run_id, trades, _initial_capital(config))

    return report


def load_report(run_id: str) -> Optional[Dict[str, Any]]:
    """Read a previously generated report, or None if there isn't one."""
    path = os.path.join(persistence.get_aggregate_dir(run_id), "report.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def report_html_path(run_id: str) -> str:
    return os.path.join(persistence.get_aggregate_dir(run_id), "report.html")


# =============================================================================
# STANDALONE HTML RENDERING
# =============================================================================

REPORT_CSS = """
:root{--bg:#0a0e17;--panel:#141b2b;--panel2:#0f1520;--line:#1e293b;--text:#e2e8f0;
--muted:#94a3b8;--dim:#64748b;--head:#f8fafc;--blue:#3b82f6;--green:#10b981;
--red:#ef4444;--yellow:#f59e0b;--orange:#f97316;--purple:#8b5cf6;--cyan:#06b6d4;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,sans-serif;}
.wrap{max-width:1100px;margin:0 auto;padding:32px 20px 80px;}
h1{font-size:26px;margin:0 0 4px;color:var(--head);}
h2{font-size:17px;margin:36px 0 12px;color:var(--head);
border-bottom:1px solid var(--line);padding-bottom:8px;}
h3{font-size:14px;margin:20px 0 8px;color:var(--head);}
.sub{color:var(--muted);font-size:13px;margin:0 0 20px;}
.mono{font-family:'JetBrains Mono',ui-monospace,Consolas,monospace;}
.verdict{display:flex;flex-wrap:wrap;gap:16px;align-items:center;
background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;}
.badge{font-size:18px;font-weight:700;padding:8px 16px;border-radius:8px;}
.badge.robust{color:var(--green);background:rgba(16,185,129,.12);}
.badge.promising{color:var(--blue);background:rgba(59,130,246,.12);}
.badge.weak{color:var(--yellow);background:rgba(245,158,11,.12);}
.badge.unstable{color:var(--red);background:rgba(239,68,68,.12);}
.badge.overfitrisk{color:var(--orange);background:rgba(249,115,22,.12);}
.badge.insufficientevidence{color:var(--dim);background:rgba(100,116,139,.14);}
.badge.invalidoosnothonoured{color:var(--red);background:rgba(239,68,68,.18);}
.alarm{border:1px solid rgba(239,68,68,.5);background:rgba(239,68,68,.08);
border-radius:12px;padding:16px 18px;margin:18px 0 0;}
.alarm .h{color:var(--red);font-weight:700;font-size:15px;margin-bottom:6px;}
.alarm .b{color:var(--text);font-size:13px;}
.verdict .rec{flex:1;min-width:280px;color:var(--muted);font-size:13px;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:12px;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;}
.card .k{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);}
.card .v{font-size:19px;font-weight:700;color:var(--head);margin-top:4px;
font-family:'JetBrains Mono',ui-monospace,Consolas,monospace;}
.v.pos{color:var(--green);} .v.neg{color:var(--red);}
p.para{color:var(--text);margin:0 0 12px;}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px;}
th{background:var(--panel2);color:var(--muted);font-size:10px;letter-spacing:.05em;
text-transform:uppercase;text-align:right;padding:8px 10px;border-bottom:1px solid var(--line);}
th:first-child,td:first-child{text-align:left;}
td{padding:7px 10px;border-bottom:1px solid rgba(30,41,59,.6);text-align:right;
font-family:'JetBrains Mono',ui-monospace,Consolas,monospace;}
tr:hover td{background:rgba(59,130,246,.05);}
td.pos{color:var(--green);} td.neg{color:var(--red);}
.finding{display:flex;gap:12px;padding:12px 14px;border-radius:10px;margin-bottom:10px;
border:1px solid var(--line);background:var(--panel);}
.finding .dot{width:8px;height:8px;border-radius:50%;margin-top:7px;flex:0 0 auto;}
.finding.warning{border-color:rgba(245,158,11,.35);} .finding.warning .dot{background:var(--yellow);}
.finding.good{border-color:rgba(16,185,129,.3);} .finding.good .dot{background:var(--green);}
.finding.info{border-color:var(--line);} .finding.info .dot{background:var(--blue);}
.finding .t{font-weight:600;color:var(--head);}
.finding .d{color:var(--muted);font-size:12.5px;}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;}
.scroll{overflow-x:auto;}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px 20px;
font-size:12.5px;}
.kv div{display:flex;justify-content:space-between;gap:12px;
border-bottom:1px dashed rgba(30,41,59,.8);padding:5px 0;}
.kv span:first-child{color:var(--dim);} .kv span:last-child{color:var(--text);}
pre{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px;
font-size:11px;overflow:auto;max-height:280px;color:var(--text);}
.foot{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
color:var(--dim);font-size:11.5px;}
@media print{body{background:#fff;color:#111;}.card,.finding,.chart{break-inside:avoid;}}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _num(value, digits=2, dash="—") -> str:
    v = _f(value, float("nan"))
    if math.isnan(v):
        return dash
    return f"{v:,.{digits}f}"


def _cls(value) -> str:
    v = _f(value, 0.0)
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _svg_equity(points: List[Dict[str, Any]], capital: float) -> str:
    """A dependency-free equity curve, drawn as inline SVG."""
    if len(points) < 2:
        return '<div class="chart" style="color:#64748b;font-size:12px;">' \
               'No combined equity curve available — the OOS trade logs for ' \
               'this run could not be read.</div>'

    width, height, pad = 1040, 260, 8
    values = [p["equity"] for p in points]
    lo, hi = min(min(values), capital), max(max(values), capital)
    if hi - lo < 1e-9:
        hi = lo + 1.0

    def x(i):
        return pad + i * (width - 2 * pad) / (len(points) - 1)

    def y(v):
        return height - pad - (v - lo) * (height - 2 * pad) / (hi - lo)

    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    area = f"{pad},{height - pad} {line} {width - pad},{height - pad}"
    base = y(capital)
    final_up = values[-1] >= capital
    stroke = "#10b981" if final_up else "#ef4444"
    fill = "rgba(16,185,129,.14)" if final_up else "rgba(239,68,68,.14)"

    # Step boundaries, so the reader can see where each OOS window starts.
    marks = []
    last_step = points[0]["step"]
    for i, p in enumerate(points):
        if p["step"] != last_step:
            marks.append(f'<line x1="{x(i):.1f}" y1="{pad}" x2="{x(i):.1f}" '
                         f'y2="{height - pad}" stroke="#1e293b" stroke-width="1"/>')
            last_step = p["step"]

    return f"""<div class="chart">
<svg viewBox="0 0 {width} {height}" width="100%" height="{height}"
     preserveAspectRatio="none" role="img" aria-label="Combined out-of-sample equity curve">
  <polygon points="{area}" fill="{fill}"/>
  {''.join(marks)}
  <line x1="{pad}" y1="{base:.1f}" x2="{width - pad}" y2="{base:.1f}"
        stroke="#64748b" stroke-dasharray="4 4" stroke-width="1"/>
  <polyline points="{line}" fill="none" stroke="{stroke}" stroke-width="2"/>
</svg>
<div style="display:flex;justify-content:space-between;color:#64748b;font-size:11px;margin-top:6px;">
  <span>{_e(points[0].get('date') or 'start')} · {_num(lo, 0)}</span>
  <span>vertical lines mark OOS step boundaries · dashed line = starting capital</span>
  <span>{_e(points[-1].get('date') or 'end')} · {_num(hi, 0)}</span>
</div></div>"""


def _svg_step_bars(steps: List[Dict[str, Any]]) -> str:
    """Per-step OOS profit as a bar chart."""
    values = [_f(s.get("oos_net_profit")) for s in steps]
    if not values:
        return ""
    width, height, pad = 1040, 180, 10
    span = max(abs(min(values)), abs(max(values)), 1.0)
    zero = height / 2
    bar_w = (width - 2 * pad) / len(values)

    bars = []
    for i, v in enumerate(values):
        h = abs(v) / span * (height / 2 - pad)
        x = pad + i * bar_w + bar_w * 0.15
        w = bar_w * 0.7
        y = zero - h if v >= 0 else zero
        color = "#10b981" if v >= 0 else "#ef4444"
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                    f'height="{max(h, 1):.1f}" fill="{color}" opacity="0.85"/>')

    return f"""<div class="chart" style="margin-top:12px;">
<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" preserveAspectRatio="none"
     role="img" aria-label="Out-of-sample profit by step">
  {''.join(bars)}
  <line x1="{pad}" y1="{zero}" x2="{width - pad}" y2="{zero}" stroke="#334155" stroke-width="1"/>
</svg>
<div style="color:#64748b;font-size:11px;margin-top:6px;">
  Out-of-sample net profit per step (step 1 on the left, step {len(values)} on the right)
</div></div>"""


def render_html(report: Dict[str, Any]) -> str:
    """Render the report as one self-contained HTML page (no external assets)."""
    cfg = report.get("configuration", {})
    strat = report.get("strategy", {})
    verdict = report.get("verdict", {})
    combined = report.get("combined_oos", {})
    oos = report.get("oos_aggregate", {})
    is_oos = report.get("is_vs_oos", {})

    badge_cls = "".join(ch for ch in str(verdict.get("label", "")).lower()
                        if ch.isalnum())

    # ---- headline cards ----
    cards = [
        ("OOS Net Profit", _num(oos.get("net_profit")), _cls(oos.get("net_profit"))),
        ("OOS Trades", _num(combined.get("total_trades"), 0), ""),
        ("Profit Factor", _num(combined.get("profit_factor")), ""),
        ("Win Rate", _num(combined.get("win_rate"), 1) + "%", ""),
        ("Max Drawdown", _num(combined.get("max_drawdown")), "neg"),
        ("Max DD %", _num(combined.get("max_drawdown_pct"), 1) + "%", "neg"),
        ("Recovery Factor", _num(combined.get("recovery_factor")), ""),
        ("Sharpe (OOS)", _num(combined.get("sharpe_ratio")), ""),
        ("Profitable Periods",
         f"{oos.get('profitable_periods', 0)}/{oos.get('total_steps', 0)}", ""),
        ("WF Efficiency",
         _num(is_oos.get("median_efficiency")) if is_oos.get("median_efficiency") is not None else "—", ""),
        ("Expectancy / Trade", _num(combined.get("expectancy")), _cls(combined.get("expectancy"))),
        ("Worst Losing Streak", _num(combined.get("max_consecutive_losses"), 0), ""),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{_e(k)}</div>'
        f'<div class="v {c}">{v}</div></div>' for k, v, c in cards)

    # ---- findings ----
    findings_html = "".join(
        f'<div class="finding {_e(f["level"])}"><div class="dot"></div><div>'
        f'<div class="t">{_e(f["title"])}</div>'
        f'<div class="d">{_e(f["detail"])}</div></div></div>'
        for f in report.get("findings", []))

    # ---- steps table ----
    step_rows = ""
    for s in report.get("steps", []):
        profit = _f(s.get("oos_net_profit"))
        step_rows += (
            f'<tr><td>{_e(s.get("step"))}</td>'
            f'<td style="text-align:left;color:#06b6d4;">{_e(s.get("is_start"))} → {_e(s.get("is_end"))}</td>'
            f'<td style="text-align:left;color:#8b5cf6;">{_e(s.get("oos_start"))} → {_e(s.get("oos_end"))}</td>'
            f'<td class="{_cls(profit)}">{_num(profit)}</td>'
            f'<td>{_num(s.get("oos_profit_factor"))}</td>'
            f'<td>{_num(s.get("oos_sharpe"))}</td>'
            f'<td class="neg">{_num(s.get("oos_max_drawdown"))}</td>'
            f'<td>{_num(s.get("oos_win_rate"), 1)}%</td>'
            f'<td>{_num(s.get("oos_trades"), 0)}</td>'
            f'<td style="text-align:left;">{_e(s.get("state"))}</td></tr>')

    # ---- IS vs OOS table ----
    iso_rows = ""
    for r in is_oos.get("steps", []):
        eff = r.get("efficiency")
        eff_cls = "" if eff is None else ("pos" if eff >= 0.6 else "neg" if eff < 0.3 else "")
        iso_rows += (
            f'<tr><td>{_e(r.get("step"))}</td>'
            f'<td class="{_cls(r.get("is_net_profit"))}">{_num(r.get("is_net_profit"))}</td>'
            f'<td class="{_cls(r.get("oos_net_profit"))}">{_num(r.get("oos_net_profit"))}</td>'
            f'<td>{_num(r.get("is_profit_factor"))}</td>'
            f'<td>{_num(r.get("oos_profit_factor"))}</td>'
            f'<td>{_num(r.get("is_trades"), 0)}</td>'
            f'<td>{_num(r.get("oos_trades"), 0)}</td>'
            f'<td class="{eff_cls}">{"—" if eff is None else _num(eff)}</td></tr>')

    # ---- stability table ----
    stab_rows = ""
    for p in report.get("stability", []):
        if not p.get("is_numeric"):
            continue
        cv = _f(p.get("cv"))
        cls = "pos" if cv <= 0.15 else ("neg" if cv > 0.5 else "")
        stab_rows += (
            f'<tr><td>{_e(p.get("name"))}</td><td>{_num(p.get("mean"), 4)}</td>'
            f'<td>{_num(p.get("std"), 4)}</td><td class="{cls}">{_num(cv, 4)}</td>'
            f'<td>{_num(p.get("min"), 4)}</td><td>{_num(p.get("max"), 4)}</td></tr>')

    # ---- monthly table ----
    monthly = report.get("monthly", [])
    monthly_rows = "".join(
        f'<tr><td>{_e(m["month"])}</td><td>{_num(m["trades"], 0)}</td>'
        f'<td class="{_cls(m["net_profit"])}">{_num(m["net_profit"])}</td>'
        f'<td>{_num(m["win_rate"], 1)}%</td></tr>' for m in monthly)

    # ---- candidates ----
    cand_html = ""
    for c in report.get("candidates", []):
        if c.get("method") == "user_selected":
            continue
        cand_html += (
            f'<h3>{_e(c.get("label"))} '
            f'<span style="color:#64748b;font-weight:400;">({_e(c.get("confidence"))} confidence)</span></h3>'
            f'<p class="para" style="color:#94a3b8;font-size:12.5px;">{_e(c.get("description"))}</p>'
            f'<pre>{_e(json.dumps(c.get("params", {}), indent=2, default=str))}</pre>')

    summary_html = "".join(f'<p class="para">{_e(line)}</p>'
                           for line in report.get("summary", []))

    # ---- integrity banner ----
    integrity = report.get("integrity", {}) or {}
    integrity_html = ""
    if integrity.get("status") == "mismatch":
        rows = "".join(
            f'<tr><td>{_e(m["step"])}</td><td style="text-align:left;">{_e(m["expected"])}</td>'
            f'<td style="text-align:left;" class="neg">{_e(m["actual"])}</td>'
            f'<td style="text-align:left;">{"in-sample window" if m.get("ran_in_sample_window") else "other window"}</td></tr>'
            for m in integrity["mismatched"])
        integrity_html = f"""<div class="alarm">
<div class="h">This run's out-of-sample windows were not honoured</div>
<div class="b">{len(integrity['mismatched'])} of {integrity['checked']} steps
executed their backtest on a different date range than the one they were meant
to validate on. Where the range shown is the step's own in-sample window, the
"out-of-sample" result is simply the in-sample result repeated. Treat every
number on this page as diagnostic only.</div>
<div class="scroll"><table><thead><tr><th>Step</th><th>Assigned OOS window</th>
<th>Window actually run</th><th>Which window</th></tr></thead>
<tbody>{rows}</tbody></table></div></div>"""
    elif integrity.get("status") == "ok":
        integrity_html = (
            f'<p class="sub" style="margin-top:14px;color:#10b981;">'
            f'✓ Out-of-sample windows verified on all {integrity["verified"]} '
            f'step(s), checked against the parameters each OOS backtest recorded.</p>')

    config_html = "".join(
        f"<div><span>{_e(k.replace('_', ' ').title())}</span>"
        f"<span class='mono'>{_e(v if not isinstance(v, list) else ', '.join(map(str, v)) or '—')}</span></div>"
        for k, v in cfg.items())

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Walk-Forward Report — {_e(report.get('run_id'))}</title>
<style>{REPORT_CSS}</style></head><body><div class="wrap">

<h1>Walk-Forward Report</h1>
<p class="sub mono">{_e(strat.get('name'))} · run {_e(report.get('run_id'))} ·
generated {_e(report.get('generated_at'))} · status {_e(report.get('status'))}</p>

<div class="verdict">
  <div class="badge {badge_cls}">{_e(verdict.get('label'))} · {_num(verdict.get('score_pct'), 1)}%</div>
  <div class="rec">{_e(verdict.get('recommendation'))}</div>
</div>
{integrity_html}

<h2>Out-of-Sample Performance</h2>
<div class="grid">{cards_html}</div>

<h2>Summary</h2>
{summary_html}

<h2>Findings ({len(report.get('findings', []))})</h2>
{findings_html or '<p class="para">No findings recorded.</p>'}

<h2>Combined Out-of-Sample Equity</h2>
{_svg_equity(report.get('equity_curve', []), _f(cfg.get('initial_capital'), 100000.0))}
{_svg_step_bars(report.get('steps', []))}

<h2>In-Sample vs Out-of-Sample</h2>
{'<p class="sub" style="color:#ef4444;">These two columns are not independent for this run — the OOS windows were not honoured, so efficiency is measuring a window against itself.</p>' if integrity.get('status') == 'mismatch' else ''}
<p class="sub">Efficiency compares monthly profit rates, so the longer in-sample
window is not penalised for simply covering more time. 1.00 means the
out-of-sample period earned at the same rate the optimizer found in-sample.
Blank means the step had no in-sample profit to carry forward.</p>
<div class="scroll"><table>
<thead><tr><th>Step</th><th>IS Profit</th><th>OOS Profit</th><th>IS PF</th>
<th>OOS PF</th><th>IS Trades</th><th>OOS Trades</th><th>Efficiency</th></tr></thead>
<tbody>{iso_rows}</tbody></table></div>

<h2>Step Results</h2>
<div class="scroll"><table>
<thead><tr><th>Step</th><th>IS Period</th><th>OOS Period</th><th>OOS Profit</th>
<th>PF</th><th>Sharpe</th><th>Max DD</th><th>Win %</th><th>Trades</th><th>State</th></tr></thead>
<tbody>{step_rows}</tbody></table></div>

<h2>Parameter Stability</h2>
<p class="sub">CV is the coefficient of variation (std ÷ |mean|) of each optimised
parameter across steps. Low CV means the optimizer kept landing in the same
region; high CV means it moved somewhere different every window.</p>
<div class="scroll"><table>
<thead><tr><th>Parameter</th><th>Mean</th><th>Std</th><th>CV</th><th>Min</th><th>Max</th></tr></thead>
<tbody>{stab_rows or '<tr><td colspan="6">No numeric parameters tracked.</td></tr>'}</tbody>
</table></div>

{'<h2>Monthly Out-of-Sample Breakdown</h2><div class="scroll"><table><thead><tr><th>Month</th><th>Trades</th><th>Net Profit</th><th>Win Rate</th></tr></thead><tbody>' + monthly_rows + '</tbody></table></div>' if monthly_rows else ''}

{'<h2>Candidate Parameters</h2>' + cand_html if cand_html else ''}

<h2>Run Configuration</h2>
<div class="kv">{config_html}</div>

<div class="foot">
Every performance figure above is measured on out-of-sample data only —
periods the optimizer never saw when it chose those parameters. Past
out-of-sample results describe what happened on this dataset; they are
evidence, not a prediction.
</div>
</div></body></html>"""

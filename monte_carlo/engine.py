"""
monte_carlo/engine.py
---------------------
The simulation core.

Given one sequence of realised trade P&Ls, each method produces thousands of
alternative sequences that the same edge could plausibly have generated, and
the distribution of outcomes is summarised.

    reorder          Same trades, different order. Net profit is fixed by
                     construction; what varies is the path — so this isolates
                     how much of the drawdown was sequencing luck.
    resample         Bootstrap with replacement. Both profit and drawdown vary;
                     the standard "how repeatable is this edge?" test.
    block_bootstrap  Bootstrap in contiguous blocks, preserving streaks and
                     autocorrelation that plain resampling destroys.
    skip             Randomly miss a share of trades. Answers "what if I was
                     away, throttled, or the fill never came?".
    noise            Perturb every trade's P&L. Stresses the result against
                     slippage, wider spreads and fee changes.

MEMORY
    A run of 10,000 simulations over 5,000 trades is a 50-million-cell matrix.
    Nothing here ever materialises that: simulations are processed in chunks
    sized to a fixed cell budget, each chunk is reduced to per-simulation
    metrics, and only those metrics (plus a downsampled equity checkpoint grid)
    survive the chunk.
"""
import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import numpy as np


METHODS = ("reorder", "resample", "block_bootstrap", "skip", "noise")

METHOD_LABELS = {
    "reorder": "Trade Order Shuffle",
    "resample": "Bootstrap (with replacement)",
    "block_bootstrap": "Block Bootstrap",
    "skip": "Random Trade Skip",
    "noise": "P&L Noise / Slippage Stress",
}

METHOD_DESCRIPTIONS = {
    "reorder": "The same trades in a different order. Total profit is "
               "unchanged, so every difference you see is pure sequencing "
               "luck — mostly in the drawdown.",
    "resample": "Trades drawn at random with replacement, so some repeat and "
                "others never appear. The broadest test of whether the edge "
                "is repeatable or came from a handful of outliers.",
    "block_bootstrap": "Resampling in contiguous blocks, which keeps winning "
                       "and losing streaks intact. Use when trades are "
                       "correlated with each other rather than independent.",
    "skip": "A random share of trades is missed entirely — downtime, throttling "
            "or fills that never came. Shows how much the result depends on "
            "catching every signal.",
    "noise": "Every trade's P&L is nudged up or down at random. Stresses the "
             "result against slippage, spread widening and fee changes.",
}

# Percentiles reported for every metric.
PERCENTILES = (1, 5, 10, 25, 50, 75, 90, 95, 99)

# Cells (simulations × trades) held in memory at once. Reducing a chunk needs
# roughly seven arrays of its size alive at once (paths, equity, running max,
# drawdown, drawdown %, and the two boolean masks), so the real peak is about
# 8 bytes × budget × 7 — around 85 MB here, against the 1.5 GB a 10,000 × 20,000
# run would need if the matrix were materialised in one piece.
CHUNK_CELL_BUDGET = 1_500_000

# Points kept per equity path for the percentile band chart.
EQUITY_CHECKPOINTS = 120

TRADING_DAYS_PER_YEAR = 252

MAX_SIMULATIONS = 200_000


@dataclass
class MonteCarloConfig:
    """Everything that decides what a run does."""
    methods: List[str] = field(default_factory=lambda: ["resample"])
    simulations: int = 2000
    initial_capital: float = 100000.0
    seed: int = 42

    # Method-specific knobs.
    keep_pct: float = 90.0      # 'skip': share of trades that still happen
    noise_pct: float = 10.0     # 'noise': std-dev of the per-trade perturbation
    block_size: int = 10        # 'block_bootstrap': trades per block
    horizon: Optional[int] = None   # trades per simulated path (default: as many as the source has)

    # Risk questions.
    ruin_pct: float = 50.0      # equity drop from the start that counts as ruin
    dd_limit_pct: float = 20.0  # drawdown the user considers unacceptable

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MonteCarloConfig":
        known = set(cls.__dataclass_fields__.keys())
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def validated(self, n_trades: int) -> "MonteCarloConfig":
        """Clamp everything to a range the engine can actually honour."""
        methods = [m for m in (self.methods or []) if m in METHODS]
        if not methods:
            methods = ["resample"]

        horizon = int(self.horizon or n_trades)
        horizon = max(1, min(horizon, max(n_trades * 10, 1)))

        return MonteCarloConfig(
            methods=methods,
            simulations=int(max(100, min(int(self.simulations or 2000), MAX_SIMULATIONS))),
            initial_capital=float(self.initial_capital) if self.initial_capital else 100000.0,
            seed=int(self.seed),
            keep_pct=float(min(max(self.keep_pct, 1.0), 100.0)),
            noise_pct=float(min(max(self.noise_pct, 0.0), 100.0)),
            block_size=int(min(max(self.block_size, 1), max(n_trades, 1))),
            horizon=horizon,
            ruin_pct=float(min(max(self.ruin_pct, 1.0), 100.0)),
            dd_limit_pct=float(min(max(self.dd_limit_pct, 0.1), 100.0)),
        )


# =============================================================================
# SINGLE-SEQUENCE METRICS (the actual backtest, for comparison)
# =============================================================================

def sequence_metrics(pnl: np.ndarray, initial_capital: float) -> Dict[str, float]:
    """Metrics for one concrete sequence — the trades as they really happened."""
    pnl = np.asarray(pnl, dtype=float)
    n = pnl.size
    if n == 0:
        return {}

    equity = initial_capital + np.cumsum(pnl)
    running_max = np.maximum.accumulate(equity)
    drawdown = equity - running_max
    # Relative to the peak it fell from, which is what an account actually feels.
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_pct = np.where(running_max > 0, drawdown / running_max * 100.0, 0.0)

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_loss = float(losses.sum())
    std = float(pnl.std(ddof=1)) if n > 1 else 0.0

    return {
        "net_profit": float(pnl.sum()),
        "final_equity": float(equity[-1]),
        "return_pct": float(pnl.sum() / initial_capital * 100.0) if initial_capital else 0.0,
        "max_drawdown": float(drawdown.min()),
        "max_drawdown_pct": float(dd_pct.min()),
        "profit_factor": float(abs(wins.sum() / gross_loss)) if gross_loss else 0.0,
        "win_rate": float(wins.size / n * 100.0),
        "sharpe_ratio": float(pnl.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR)) if std else 0.0,
        "avg_trade": float(pnl.mean()),
        "total_trades": int(n),
    }


# =============================================================================
# PATH GENERATION
# =============================================================================

def _generate_chunk(pnl: np.ndarray, method: str, rows: int,
                    cfg: MonteCarloConfig, rng: np.random.Generator):
    """
    One block of simulated P&L paths, shape (rows, horizon).

    Returns (paths, counted_mask) where counted_mask marks the cells that
    represent a real trade — every method fills the matrix completely, but
    'skip' writes zeros where a trade did not happen and those must not be
    counted as break-even trades in the win rate or profit factor.
    """
    n = pnl.size
    horizon = cfg.horizon or n

    if method == "reorder":
        # rng.permuted shuffles each row independently.
        base = np.tile(pnl, (rows, 1))
        paths = rng.permuted(base, axis=1)
        if horizon != n:
            paths = paths[:, :horizon] if horizon < n else np.tile(paths, (1, math.ceil(horizon / n)))[:, :horizon]
        return paths, None

    if method == "resample":
        idx = rng.integers(0, n, size=(rows, horizon))
        return pnl[idx], None

    if method == "block_bootstrap":
        block = min(cfg.block_size, n)
        n_blocks = math.ceil(horizon / block)
        starts = rng.integers(0, n, size=(rows, n_blocks, 1))
        offsets = np.arange(block).reshape(1, 1, block)
        # Wrap around the end of the log so every block is a full block; a
        # truncated tail block would quietly under-weight the final trades.
        idx = (starts + offsets) % n
        return pnl[idx.reshape(rows, -1)[:, :horizon]], None

    if method == "skip":
        keep = cfg.keep_pct / 100.0
        idx = rng.integers(0, n, size=(rows, horizon)) if horizon != n else None
        base = pnl[idx] if idx is not None else np.tile(pnl, (rows, 1))
        mask = rng.random((rows, horizon)) < keep
        return np.where(mask, base, 0.0), mask

    if method == "noise":
        idx = rng.integers(0, n, size=(rows, horizon)) if horizon != n else None
        base = pnl[idx] if idx is not None else np.tile(pnl, (rows, 1))
        # Multiplicative noise: a big trade is exposed to proportionally more
        # slippage than a small one, which is how execution costs behave.
        factor = 1.0 + rng.normal(0.0, cfg.noise_pct / 100.0, size=base.shape)
        return base * factor, None

    raise ValueError(f"Unknown Monte Carlo method: {method}")


def _chunk_rows(horizon: int) -> int:
    return max(1, min(CHUNK_CELL_BUDGET // max(horizon, 1), 5000))


# =============================================================================
# SIMULATION
# =============================================================================

def _summarise(values: np.ndarray) -> Dict[str, Any]:
    """Distribution summary for one metric across all simulations."""
    if values.size == 0:
        return {}
    pcts = np.percentile(values, PERCENTILES)
    return {
        "mean": round(float(values.mean()), 4),
        "std": round(float(values.std(ddof=1)) if values.size > 1 else 0.0, 4),
        "min": round(float(values.min()), 4),
        "max": round(float(values.max()), 4),
        "percentiles": {str(p): round(float(v), 4) for p, v in zip(PERCENTILES, pcts)},
    }


def _histogram(values: np.ndarray, bins: int = 40) -> Dict[str, Any]:
    """
    Bin one metric for plotting, without ever raising.

    Two things break a naive np.histogram call here:

    * A degenerate distribution. `reorder` cannot change the total, and `noise`
      at 0% cannot change anything, so those net-profit distributions are a
      single value. Worse, they are only *nearly* single: summing the same
      trades in a different order drifts by a few ULPs, and on a nine-figure
      account that drift is far too narrow for 40 distinct float64 edges —
      numpy then refuses with "Too many bins for data range". Such a
      distribution is reported as degenerate and drawn as a note rather than a
      meaningless single spike.
    * Too few samples. 100 simulations spread over 40 bins is noise, so the bin
      count follows the sample size.
    """
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {"edges": [], "counts": [], "degenerate": True, "value": None}

    lo = float(values.min())
    hi = float(values.max())

    # The narrowest span that can still hold `bins` distinct float64 edges.
    # Anything tighter is a single value wearing a rounding error.
    resolution = np.spacing(max(abs(lo), abs(hi), 1.0)) * bins * 4
    if hi - lo <= resolution:
        return {
            "edges": [round(lo, 6), round(hi, 6)],
            "counts": [int(values.size)],
            "degenerate": True,
            "value": round((lo + hi) / 2.0, 6),
        }

    bins = int(max(8, min(bins, values.size // 5)))
    counts, edges = np.histogram(values, bins=bins, range=(lo, hi))
    return {
        "edges": [round(float(e), 4) for e in edges],
        "counts": [int(c) for c in counts],
        "degenerate": False,
    }


def _percentile_rank(values: np.ndarray, target: float) -> float:
    """Where the real backtest sits inside the simulated distribution."""
    if values.size == 0:
        return 0.0
    return round(float((values <= target).mean() * 100.0), 2)


def simulate_method(pnl: np.ndarray, method: str, cfg: MonteCarloConfig,
                    actual: Dict[str, float]) -> Dict[str, Any]:
    """Run every simulation for one method and reduce it to a summary."""
    rng = np.random.default_rng(cfg.seed)
    horizon = cfg.horizon or pnl.size
    capital = cfg.initial_capital
    ruin_level = capital * (1.0 - cfg.ruin_pct / 100.0)

    total = cfg.simulations
    rows_per_chunk = _chunk_rows(horizon)

    # Per-simulation metric accumulators — the only thing that grows with the
    # number of simulations, at 8 bytes each.
    net_profit, max_dd, max_dd_pct = [], [], []
    profit_factor, win_rate, sharpe, ruined = [], [], [], []
    checkpoints = []

    checkpoint_idx = np.unique(np.linspace(
        0, horizon - 1, min(EQUITY_CHECKPOINTS, horizon)).astype(int))

    done = 0
    while done < total:
        rows = min(rows_per_chunk, total - done)
        paths, counted = _generate_chunk(pnl, method, rows, cfg, rng)

        equity = capital + np.cumsum(paths, axis=1)
        running_max = np.maximum.accumulate(equity, axis=1)
        drawdown = equity - running_max

        net_profit.append(equity[:, -1] - capital)
        max_dd.append(drawdown.min(axis=1))
        with np.errstate(divide="ignore", invalid="ignore"):
            dd_pct = np.where(running_max > 0, drawdown / running_max * 100.0, 0.0)
        max_dd_pct.append(dd_pct.min(axis=1))
        ruined.append((equity <= ruin_level).any(axis=1))

        # Win rate and profit factor ignore cells that are not real trades.
        if counted is None:
            wins = paths > 0
            losses = paths < 0
            n_counted = np.full(rows, paths.shape[1], dtype=float)
        else:
            wins = (paths > 0) & counted
            losses = (paths < 0) & counted
            n_counted = counted.sum(axis=1).astype(float)

        gross_win = np.where(wins, paths, 0.0).sum(axis=1)
        gross_loss = np.where(losses, paths, 0.0).sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            profit_factor.append(np.where(gross_loss < 0,
                                          np.abs(gross_win / gross_loss), 0.0))
            win_rate.append(np.where(n_counted > 0,
                                     wins.sum(axis=1) / n_counted * 100.0, 0.0))

        std = paths.std(axis=1, ddof=1) if paths.shape[1] > 1 else np.zeros(rows)
        with np.errstate(divide="ignore", invalid="ignore"):
            sharpe.append(np.where(std > 0,
                                   paths.mean(axis=1) / std * math.sqrt(TRADING_DAYS_PER_YEAR),
                                   0.0))

        checkpoints.append(equity[:, checkpoint_idx])

        done += rows
        # Free the big arrays before the next chunk allocates its own.
        del paths, equity, running_max, drawdown, wins, losses

    net_profit = np.concatenate(net_profit)
    max_dd = np.concatenate(max_dd)
    max_dd_pct = np.concatenate(max_dd_pct)
    profit_factor = np.concatenate(profit_factor)
    win_rate = np.concatenate(win_rate)
    sharpe = np.concatenate(sharpe)
    ruined = np.concatenate(ruined)
    checkpoints = np.concatenate(checkpoints, axis=0)

    # Equity percentile bands.
    band_levels = (5, 25, 50, 75, 95)
    bands = np.percentile(checkpoints, band_levels, axis=0)
    equity_bands = {
        "trade_index": [int(i + 1) for i in checkpoint_idx],
        **{f"p{level}": [round(float(v), 2) for v in bands[i]]
           for i, level in enumerate(band_levels)},
    }

    actual_dd_pct = abs(actual.get("max_drawdown_pct", 0.0))
    dd_limit = cfg.dd_limit_pct

    return {
        "method": method,
        "label": METHOD_LABELS[method],
        "description": METHOD_DESCRIPTIONS[method],
        "simulations": int(total),
        "horizon": int(horizon),
        "metrics": {
            "net_profit": _summarise(net_profit),
            "max_drawdown": _summarise(max_dd),
            "max_drawdown_pct": _summarise(max_dd_pct),
            "profit_factor": _summarise(profit_factor),
            "win_rate": _summarise(win_rate),
            "sharpe_ratio": _summarise(sharpe),
        },
        "probabilities": {
            "profit": round(float((net_profit > 0).mean() * 100.0), 2),
            "loss": round(float((net_profit <= 0).mean() * 100.0), 2),
            "ruin": round(float(ruined.mean() * 100.0), 2),
            "dd_exceeds_limit": round(float((np.abs(max_dd_pct) > dd_limit).mean() * 100.0), 2),
            "dd_exceeds_actual": round(float((np.abs(max_dd_pct) > actual_dd_pct).mean() * 100.0), 2),
            "beats_actual_profit": round(
                float((net_profit > actual.get("net_profit", 0.0)).mean() * 100.0), 2),
        },
        "risk": {
            "var_95": round(float(np.percentile(net_profit, 5)), 2),
            "cvar_95": round(float(net_profit[net_profit <= np.percentile(net_profit, 5)].mean()), 2),
            "worst_case_profit": round(float(net_profit.min()), 2),
            "worst_case_drawdown": round(float(max_dd.min()), 2),
            "worst_case_drawdown_pct": round(float(max_dd_pct.min()), 2),
            "median_drawdown_pct": round(float(np.percentile(max_dd_pct, 50)), 2),
        },
        "actual_percentile": {
            "net_profit": _percentile_rank(net_profit, actual.get("net_profit", 0.0)),
            "max_drawdown": _percentile_rank(max_dd, actual.get("max_drawdown", 0.0)),
            "profit_factor": _percentile_rank(profit_factor, actual.get("profit_factor", 0.0)),
        },
        "histograms": {
            "net_profit": _histogram(net_profit),
            "max_drawdown_pct": _histogram(max_dd_pct),
        },
        "equity_bands": equity_bands,
    }


def build_findings(pnl: np.ndarray, actual: Dict[str, float],
                   results: Dict[str, Any], cfg: MonteCarloConfig) -> List[Dict[str, str]]:
    """Plain-language readings of what the distributions say."""
    findings: List[Dict[str, str]] = []

    def add(level, title, detail):
        findings.append({"level": level, "title": title, "detail": detail})

    n = int(pnl.size)
    if n < 30:
        add("warning", "Very small trade sample",
            f"{n} trades. Resampling {n} outcomes cannot manufacture evidence "
            "that is not in them — treat every number here as indicative only.")
    elif n < 100:
        add("info", "Small trade sample",
            f"{n} trades. The distributions below are wide because the input "
            "is short, not necessarily because the strategy is unstable.")

    for method, res in results.items():
        probs = res["probabilities"]
        label = res["label"]

        if method == "reorder":
            median_dd = abs(res["risk"]["median_drawdown_pct"])
            worst_dd = abs(res["risk"]["worst_case_drawdown_pct"])
            actual_dd = abs(actual.get("max_drawdown_pct", 0.0))
            if worst_dd > actual_dd * 1.5 and actual_dd > 0:
                add("warning", "Sequencing flattered the drawdown",
                    f"Reordering the same trades produces drawdowns up to "
                    f"{worst_dd:.1f}% against the {actual_dd:.1f}% the backtest "
                    f"reported (median {median_dd:.1f}%). The reported drawdown "
                    "is a lucky draw, not a ceiling — size for the wider range.")
            else:
                add("good", "Drawdown is not a sequencing artefact",
                    f"Shuffling trade order moves the worst drawdown to "
                    f"{worst_dd:.1f}% versus {actual_dd:.1f}% actual — the "
                    "reported figure is representative.")

        # A reorder cannot change the total, so "how often was it profitable?"
        # is always 0% or 100% and says nothing. Its drawdown reading above is
        # the whole point of the method.
        if method == "reorder":
            continue

        if probs["profit"] < 60:
            add("warning", f"{label}: profit is close to a coin flip",
                f"Only {probs['profit']:.0f}% of simulated runs finished "
                "profitable. An edge that survives resampling should clear "
                "this comfortably.")
        elif probs["profit"] >= 90:
            add("good", f"{label}: profit holds up under resampling",
                f"{probs['profit']:.0f}% of simulated runs finished profitable.")

        if probs["ruin"] >= 1:
            add("warning", f"{label}: measurable risk of ruin",
                f"{probs['ruin']:.2f}% of runs lost {cfg.ruin_pct:.0f}% of "
                "starting capital at some point.")

        if probs["dd_exceeds_limit"] >= 25:
            add("warning", f"{label}: drawdown limit breached often",
                f"{probs['dd_exceeds_limit']:.0f}% of runs exceeded your "
                f"{cfg.dd_limit_pct:.0f}% drawdown limit.")

        rank = res["actual_percentile"]["net_profit"]
        if rank >= 90:
            add("warning", f"{label}: the backtest was a top-decile draw",
                f"The real result sits at the {rank:.0f}th percentile of the "
                "simulated distribution. Planning around it means planning "
                "around a good outcome, not a typical one.")
        elif rank <= 40:
            add("good", f"{label}: the backtest was not a lucky draw",
                f"The real result sits at the {rank:.0f}th percentile — the "
                "median simulation did as well or better.")

    return findings


def _json_safe(obj):
    """
    Replace NaN and infinity with None throughout the result.

    Starlette serialises responses with allow_nan=False, so a single stray
    non-finite float turns a completed simulation into a 500. Every metric here
    is guarded at source, but the result document is small and this makes that
    class of failure impossible rather than merely unlikely.
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def simulate(pnl, config: MonteCarloConfig) -> Dict[str, Any]:
    """
    Run every requested method over one trade log.

    Returns the full result document: the actual backtest's metrics, one
    distribution summary per method, and written findings.
    """
    pnl = np.asarray(pnl, dtype=float)
    pnl = pnl[np.isfinite(pnl)]
    if pnl.size == 0:
        raise ValueError("The selected source contains no usable trade P&L values.")

    cfg = config.validated(pnl.size)
    actual = sequence_metrics(pnl, cfg.initial_capital)

    results = {}
    for method in cfg.methods:
        results[method] = simulate_method(pnl, method, cfg, actual)

    return _json_safe({
        "config": cfg.to_dict(),
        "actual": actual,
        "input": {
            "total_trades": int(pnl.size),
            "gross_profit": round(float(pnl[pnl > 0].sum()), 2),
            "gross_loss": round(float(pnl[pnl < 0].sum()), 2),
            "best_trade": round(float(pnl.max()), 2),
            "worst_trade": round(float(pnl.min()), 2),
        },
        "results": results,
        "findings": build_findings(pnl, actual, results, cfg),
    })

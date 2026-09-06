"""
monte_carlo
-----------
Monte Carlo robustness analysis for backtest and walk-forward trade logs.

A backtest is one sample from a distribution of possible outcomes. This
package resamples the trades that actually happened to show what the rest of
that distribution looks like — how much of the result was edge and how much
was the order the trades happened to arrive in.
"""
from .engine import (
    METHODS, MonteCarloConfig, simulate, sequence_metrics,
)
from .sources import resolve_source, list_sources

__all__ = [
    "METHODS", "MonteCarloConfig", "simulate", "sequence_metrics",
    "resolve_source", "list_sources",
]

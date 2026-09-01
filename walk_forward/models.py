"""
walk_forward/models.py
----------------------
Data models, enums, and configuration schemas for Walk-Forward Testing.
"""
import enum
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime


# =============================================================================
# ENUMS
# =============================================================================

class WindowMode(str, enum.Enum):
    """Walk-forward window sliding mode."""
    ROLLING = "rolling"
    EXPANDING = "expanding"


class StepState(str, enum.Enum):
    """State of a single WFO step."""
    PENDING = "pending"
    OPTIMIZING = "optimizing"
    OPTIMIZED = "optimized"
    SELECTING = "selecting"
    OOS_RUNNING = "oos_running"
    COMPLETED = "completed"
    FAILED = "failed"


class CandidateMethod(str, enum.Enum):
    """Method used to generate a final candidate parameter set."""
    LATEST_IS = "latest_is"
    MOST_STABLE = "most_stable"
    ROBUST_AGGREGATE = "robust_aggregate"
    USER_SELECTED = "user_selected"


class RobustnessLabel(str, enum.Enum):
    """Robustness assessment labels — never 'Perfect' or 'Guaranteed'."""
    ROBUST = "Robust"
    PROMISING = "Promising"
    WEAK = "Weak"
    UNSTABLE = "Unstable"
    OVERFIT_RISK = "Overfit Risk"
    INSUFFICIENT = "Insufficient Evidence"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class WFOWindow:
    """A single walk-forward step window definition."""
    step: int
    is_start: str  # ISO date string
    is_end: str
    oos_start: str
    oos_end: str
    status: str = "Ready"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SelectionRule:
    """A single selection constraint rule.
    
    Supports flexible rules like:
    - metric='Total Trades', direction='max' (maximize trades)
    - metric='Overall Max Drawdown', direction='min', threshold=-5000 (min DD, must be > -5000)
    - metric='Profit Factor', direction='max', threshold=1.3 (max PF, must be >= 1.3)
    """
    metric: str
    direction: str = "max"       # 'max' or 'min'
    threshold: Optional[float] = None   # If set, acts as a filter
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SelectionConfig:
    """Configuration for deterministic parameter selection."""
    primary_metric: str = "composite_score"
    primary_direction: str = "max"  # 'max' or 'min' — maximize or minimize primary metric
    rules: List[SelectionRule] = field(default_factory=lambda: [
        SelectionRule(metric="Total Trades", direction="max", threshold=100, enabled=True),
        SelectionRule(metric="Overall Max Drawdown", direction="min", threshold=None, enabled=False),
        SelectionRule(metric="Profit Factor", direction="max", threshold=1.0, enabled=False),
        SelectionRule(metric="Sharpe Ratio", direction="max", threshold=None, enabled=False),
        SelectionRule(metric="Net Profit", direction="max", threshold=0, enabled=False),
    ])

    def to_dict(self) -> dict:
        return {
            "primary_metric": self.primary_metric,
            "primary_direction": self.primary_direction,
            "rules": [r.to_dict() for r in self.rules],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SelectionConfig":
        rules = [SelectionRule(**r) for r in d.get("rules", [])]
        return cls(
            primary_metric=d.get("primary_metric", "composite_score"),
            primary_direction=d.get("primary_direction", "max"),
            rules=rules,
        )


@dataclass
class RobustnessWeights:
    """Configurable weights for robustness score components."""
    oos_sharpe: float = 0.20
    oos_profit_factor: float = 0.15
    oos_drawdown: float = 0.15
    oos_return: float = 0.10
    trade_count: float = 0.10
    consistency: float = 0.15  # % of profitable OOS periods
    parameter_stability: float = 0.15

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RobustnessWeights":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class WFOConfig:
    """Full Walk-Forward Testing configuration."""
    # Strategy
    strategy_path: str = ""
    strategy_name: str = ""
    entry_style: str = "config_class"
    config_class_name: Optional[str] = None

    # Data
    data_path: str = ""
    timeframe: str = "15min"

    # Date ranges
    dataset_start: str = ""  # Auto-detected from data
    dataset_end: str = ""    # Auto-detected from data
    wfo_start: str = ""      # User-configured WFO start
    wfo_end: str = ""        # User-configured WFO end

    # Window configuration
    window_mode: str = "rolling"  # 'rolling' or 'expanding'
    is_duration_months: int = 24
    oos_duration_months: int = 6
    step_duration_months: int = 6
    num_steps: Optional[int] = None  # None = auto-calculate

    # Optimization settings
    optimization_method: str = "random"  # 'grid', 'random', 'bayesian'
    optimization_iterations: int = 1000
    num_workers: int = 2
    seed: int = 42
    ranking_metric: str = "composite"

    # Drawdown optimization
    drawdown_optimization: str = "disabled"  # 'disabled', 'auto', 'manual'
    dd_min_trades_per_day: float = 2.0
    dd_target_trades_per_day: float = 8.0

    # Fixed params (non-optimizable strategy params)
    fixed_params: Dict[str, Any] = field(default_factory=dict)
    # Optimizable params with ranges
    optimize_params: Dict[str, Any] = field(default_factory=dict)

    # Date parameter mapping (how the strategy receives date overrides)
    date_param_style: str = "flat"  # 'flat' (start_date/end_date) or 'nested' (Backtest_period.start_date/end_date)
    date_param_name: str = ""       # e.g. 'Backtest_period' for nested style

    # Selection configuration
    selection: SelectionConfig = field(default_factory=SelectionConfig)

    # Robustness weights
    robustness_weights: RobustnessWeights = field(default_factory=RobustnessWeights)

    # Metadata
    created_at: str = ""
    run_id: str = ""

    def to_dict(self) -> dict:
        d = {}
        for k, v in asdict(self).items():
            d[k] = v
        # Override nested objects to use their own to_dict
        d["selection"] = self.selection.to_dict()
        d["robustness_weights"] = self.robustness_weights.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "WFOConfig":
        selection = SelectionConfig.from_dict(d.pop("selection", {}))
        weights = RobustnessWeights.from_dict(d.pop("robustness_weights", {}))
        # Remove unknown keys
        known = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(selection=selection, robustness_weights=weights, **filtered)


@dataclass
class WFOStepResult:
    """Result of a single WFO step."""
    step: int
    window: WFOWindow
    state: str = StepState.PENDING.value

    # IS results
    optimization_id: str = ""
    selected_params: Dict[str, Any] = field(default_factory=dict)
    is_metrics: Dict[str, Any] = field(default_factory=dict)
    is_score: float = 0.0
    is_rank: int = 0

    # OOS results
    oos_metrics: Dict[str, Any] = field(default_factory=dict)
    oos_trades_count: int = 0
    oos_net_profit: float = 0.0
    oos_profit_factor: float = 0.0
    oos_sharpe: float = 0.0
    oos_max_drawdown: float = 0.0
    oos_win_rate: float = 0.0

    # Error info
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["window"] = self.window.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "WFOStepResult":
        window = WFOWindow(**d.pop("window", {}))
        return cls(window=window, **{k: v for k, v in d.items()
                                     if k in cls.__dataclass_fields__})


@dataclass
class CandidateParams:
    """A candidate parameter set generated from WFO analysis."""
    method: str
    label: str
    description: str
    params: Dict[str, Any] = field(default_factory=dict)
    source_step: Optional[int] = None  # For USER_SELECTED method
    confidence: str = ""  # 'high', 'medium', 'low'

    def to_dict(self) -> dict:
        return asdict(self)

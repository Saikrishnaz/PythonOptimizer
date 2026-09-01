# walk_forward/

The Walk-Forward Testing (WFO) engine — out-of-sample validation for the
parameters the optimizer finds.

An optimizer will always find *something*. Walk-forward asks the harder
question: **do the parameters chosen on past data still work on data they were
never fitted to?**

---

## The idea

The dataset is cut into overlapping windows. Each window has an **in-sample (IS)**
period used for optimization, followed by an **out-of-sample (OOS)** period used
only for verification.

```
Step 1:  [────── IS: 24 months ──────][─ OOS: 6mo ─]
Step 2:          [────── IS: 24 months ──────][─ OOS: 6mo ─]
Step 3:                  [────── IS: 24 months ──────][─ OOS: 6mo ─]
                                                   └── window slides by step_duration
```

For each step the engine:

1. Runs a full optimization on the IS period.
2. Selects one parameter set using **deterministic rules** — no judgement calls.
3. **Freezes** those parameters and runs a single backtest on the OOS period.
4. Keeps only the OOS result as evidence.

Then it stitches every OOS period into one continuous track record, measures how
much the chosen parameters moved between steps, and scores the whole thing.

### The rule the code is built around

> OOS data never influences optimization. Parameters are frozen before the OOS
> backtest runs.

`runner.py` is an **orchestrator** — it calls the existing
[`optimizer_engine`](../optimizer_engine.py) and `worker.py` rather than
reimplementing them, so IS optimization behaves identically to a normal run.

**Rolling** windows slide the IS start forward each step. **Expanding**
(anchored) windows keep the IS start fixed so the training set grows.

---

## Module map

| File | Responsibility |
|---|---|
| `models.py` | Dataclasses and enums — `WFOConfig`, `WFOWindow`, `WFOStepResult`, `SelectionConfig`, `SelectionRule`, `RobustnessWeights`, `CandidateParams`, and the `StepState` / `WindowMode` / `CandidateMethod` / `RobustnessLabel` enums |
| `window_generator.py` | Detects a dataset's date range, generates and validates windows, auto-calculates the step count |
| `runner.py` | `WalkForwardRunner` — the orchestrator that drives every step |
| `selector.py` | `select_best_params()` — deterministic, rule-based parameter selection |
| `aggregator.py` | Combines all OOS periods into one dataset; computes the robustness score |
| `stability.py` | Parameter stability across steps; generates candidate parameter sets |
| `persistence.py` | All file I/O, step state, and resume support |

---

## Configuration

`WFOConfig` (in `models.py`) with its defaults:

### Strategy and data
| Field | Default | Notes |
|---|---|---|
| `strategy_path` / `strategy_name` | — | The script to test |
| `entry_style` | `"config_class"` | Or `"function_kwargs"` |
| `config_class_name` | `None` | For `config_class` style |
| `data_path` | — | Dataset, used for date-range detection |
| `timeframe` | `"15min"` | |

### Windows
| Field | Default | Notes |
|---|---|---|
| `window_mode` | `"rolling"` | Or `"expanding"` |
| `is_duration_months` | `24` | In-sample length |
| `oos_duration_months` | `6` | Out-of-sample length |
| `step_duration_months` | `6` | How far each step advances |
| `num_steps` | `None` | `None` auto-calculates from the date range |
| `wfo_start` / `wfo_end` | — | Defaults to the detected `dataset_start` / `dataset_end` |

### Per-step optimization
| Field | Default |
|---|---|
| `optimization_method` | `"random"` (`grid`, `random`, `bayesian`) |
| `optimization_iterations` | `1000` |
| `num_workers` | `2` |
| `seed` | `42` — offset per step (`seed + step`) so windows don't sample identically |
| `ranking_metric` | `"composite"` |
| `drawdown_optimization` | `"disabled"` (`auto`, `manual`) |

### Date injection

The engine has to tell your strategy which period to run. `date_param_style`
controls how:

- `"flat"` — sets `start_date` / `end_date` directly
- `"nested"` — sets `<date_param_name>.start_date` / `.end_date`, e.g.
  `Backtest_period.start_date`

[`script_analyzer.py`](../script_analyzer.py) detects which style a script uses
and reports it as `wfo_compatibility`. A script with no start/end date parameters
cannot be walk-forward tested.

---

## Parameter selection

After each IS optimization, `selector.py` picks one parameter set — from rules,
not by eye.

A `SelectionRule` is `(metric, direction, threshold, enabled)`:

- **`threshold` set** → a hard filter. Candidates failing it are discarded
  (`direction="max"` means "at least"; `"min"` means "at most").
- **`threshold` unset** → contributes to ordering only.

`SelectionConfig` defaults to `primary_metric="composite_score"`, maximized, with
these rules (only the first enabled):

| Metric | Direction | Threshold | Enabled |
|---|---|---|---|
| Total Trades | max | 100 | ✅ |
| Overall Max Drawdown | min | — | ❌ |
| Profit Factor | max | 1.0 | ❌ |
| Sharpe Ratio | max | — | ❌ |
| Net Profit | max | 0 | ❌ |

Only rows with `status == "OK"` are considered. If nothing survives the
constraints, the step fails loudly rather than quietly relaxing them.

---

## Robustness score

`aggregator.py` scores the aggregated OOS record on seven weighted components
(`RobustnessWeights`):

| Component | Weight |
|---|---|
| OOS Sharpe | 0.20 |
| OOS Profit Factor | 0.15 |
| OOS Drawdown | 0.15 |
| Parameter stability | 0.15 |
| Consistency (% profitable OOS periods) | 0.15 |
| OOS Return | 0.10 |
| Trade count | 0.10 |

The result is labelled: **Robust**, **Promising**, **Weak**, **Unstable**,
**Overfit Risk**, or **Insufficient Evidence**.

> Note the deliberate absence of "Perfect" or "Guaranteed". The labels are
> calibrated to describe evidence, not to sell a result.

---

## Parameter stability

`stability.py` measures how much each parameter moved across steps, using the
coefficient of variation. Low CV means the strategy has a broad parameter region
that keeps working; high CV means each window wanted something different, which
is a classic overfitting signature.

It then generates candidate parameter sets by several `CandidateMethod`s:

- **`latest_is`** — what the most recent window chose
- **`most_stable`** — the values that varied least across windows
- **`robust_aggregate`** — a central tendency across all steps
- **`user_selected`** — whatever you pick in the dashboard

---

## Output layout

```
wfo_runs/
└── wfo_<run_id>/
    ├── config.json                     # the full WFOConfig
    ├── wfo_progress.json               # live progress for the SSE stream
    ├── step_001/
    │   ├── state.json                  # pending | optimizing | optimized |
    │   │                               #   selecting | oos_running | completed | failed
    │   ├── selected_parameters.json    # the frozen parameters
    │   ├── is_metrics.json             # in-sample result (context only)
    │   ├── oos_metrics.json            # out-of-sample result (the evidence)
    │   ├── oos_trades.csv
    │   └── oos_equity.csv
    ├── step_002/
    └── aggregate/
        ├── combined_oos_trades.csv
        ├── combined_oos_equity.csv
        ├── step_results.csv
        ├── parameter_stability.csv
        ├── robustness_score.json
        ├── candidates.json
        └── final_summary.json
```

Each step's IS optimization is a normal optimization run, stored under
`optimizations/wfo_<run_id>_step<NNN>_is/` — so you can open it in the Results
tab like any other run.

`wfo_runs/` is created on demand and may not exist in a fresh checkout.

---

## Resume

Step state is written to disk as each step advances, so an interrupted WFO run
resumes at step granularity: `get_completed_steps()` and `is_step_completed()`
let the runner skip finished steps and restart from the first incomplete one.

This is coarser than the batch-level resume in the main optimizer — a partially
finished step's IS optimization does resume batch-by-batch through the normal
engine, but the step itself is redone from its selection phase.

---

## API

Driven from the dashboard's Walk-Forward tab (`WalkForwardManager` in
[`../static/app.js`](../static/app.js)):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/walk-forward/runs` | List runs |
| `POST` | `/api/walk-forward/create` | Validate a config and preview its windows |
| `POST` | `/api/walk-forward/{id}/start` | Start a run |
| `POST` | `/api/walk-forward/{id}/stop` | Stop a run |
| `GET` | `/api/walk-forward/{id}/progress` | SSE progress stream |
| `GET` | `/api/walk-forward/{id}` | Status and config |
| `GET` | `/api/walk-forward/{id}/windows` | Window definitions |
| `GET` | `/api/walk-forward/{id}/steps` | Per-step results |
| `GET` | `/api/walk-forward/{id}/results` | Aggregated OOS performance |
| `GET` | `/api/walk-forward/{id}/parameters` | Stability analysis |
| `GET` | `/api/walk-forward/{id}/candidates` | Candidate parameter sets |
| `POST` | `/api/walk-forward/{id}/full-sample` | Run a candidate over the whole dataset |
| `GET` | `/api/walk-forward/{id}/export/{type}` | Export data |
| `DELETE` | `/api/walk-forward/{id}` | Delete a run |

**Create is a two-step flow.** `POST /create` validates and returns a window
preview for you to review; `POST /{id}/start` actually launches it.

---

## Sizing a run

A WFO run is *N* full optimizations. With the defaults — 1000 iterations per
step, 8 steps — that is 8000 backtests plus 8 OOS runs. Budget accordingly, and
sanity-check the per-batch cost with a small ordinary optimization first.

Windows are validated before anything runs: too few steps for the date range, or
an IS/OOS/step combination that doesn't fit, is rejected at the preview stage
rather than halfway through.

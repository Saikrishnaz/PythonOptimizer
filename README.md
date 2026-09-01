# Python Algo Optimizer

A dashboard-driven parameter optimizer for Python backtest scripts.

Point it at a strategy `.py` file, and it reads that file's parameters, lets you
pick which to sweep and over what ranges, then runs hundreds or thousands of
backtests in parallel and ranks the results. It is **strategy-agnostic**: it
learns a script's parameters by parsing it, so you don't register anything or
inherit from a base class.

It also includes a **Walk-Forward Testing** engine for out-of-sample validation,
and a standalone **chart explorer** for inspecting tick data.

---

## Contents

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Directory map](#directory-map)
- [Core modules](#core-modules)
- [Search modes](#search-modes)
- [Ranking](#ranking)
- [Progress, stop and resume](#progress-stop-and-resume)
- [Output layout](#output-layout)
- [HTTP API](#http-api)
- [Troubleshooting](#troubleshooting)

---

## Quick start

### Requirements

Python 3.12 (the project's `__pycache__` and installed packages target 3.12).

```powershell
py -3.12 -m pip install -r requirements.txt
```

`requirements.txt` pulls in FastAPI + uvicorn (server), pandas/numpy/pyarrow
(data), openpyxl (Excel reports), scikit-optimize (Bayesian search), plus
`pandas-ta` and `backtesting` which are needed only by the bundled NIFTY
credit-spread strategy.

### Run

```powershell
py -3.12 server.py
```

Then open **http://localhost:8000**.

The server binds `0.0.0.0:8000` and serves the dashboard from `static/`.

### Use

1. Drop your strategy into [backtests/](backtests/) — see
   [backtests/README.md](backtests/README.md) for the contract it must satisfy.
2. Pick it in the sidebar and hit **Analyze**. The parameters it found appear as
   a form, split into *fixed* and *optimizable*.
3. Set ranges on the parameters you want to sweep, choose a mode and an
   iteration count, and hit **Start Optimization**.
4. Watch the **Progress** tab; results land in **Results**, ranked.

There is also a headless CLI ([run_optimizer.py](run_optimizer.py)) that predates
the dashboard. It uses [config.py](config.py) for its ranges rather than the UI
and writes to `runs/` instead of `optimizations/`. The dashboard path is the
maintained one.

---

## How it works

```
  Browser (static/app.js)
        │  POST /api/optimize/start
        ▼
  server.py ── background thread ──▶ optimizer_engine.run_optimization()
        │                                    │
        │  SSE: GET /api/optimize/progress   │  ProcessPoolExecutor
        │  ◀── reads progress.json           │  (num_workers processes)
        │                                    ▼
        │                            worker.py  (one subprocess per batch)
        │                                    │  imports your strategy,
        │                                    │  calls its entry point,
        │                                    │  writes metrics.json
        ▼                                    ▼
  optimizations/<run_id>/runs/batch_0001/, batch_0002/, ...
```

Each batch is a **separate OS process with its own working directory**. That is
deliberate: your strategy can `print()`, write CSVs, and dump Excel reports
without colliding with the 19 other batches running beside it, and a strategy
that segfaults takes down one batch instead of the whole run.

The engine never imports your strategy itself — only `worker.py` does, in the
child process.

---

## Directory map

| Path | What it is |
|---|---|
| [server.py](server.py) | FastAPI backend — all HTTP endpoints, SSE progress streams, run management |
| [optimizer_engine.py](optimizer_engine.py) | The optimizer. Parameter generation, parallel execution, progress accounting, resume, ranking, aggregation |
| [worker.py](worker.py) | Runs one batch in a subprocess. Imports the strategy, calls it, parses its output into `metrics.json` |
| [script_analyzer.py](script_analyzer.py) | AST parser that discovers a strategy's parameters without executing it |
| [backtest_analytics.py](backtest_analytics.py) | Shared drawdown-episode analytics any strategy can reuse |
| [data_converter.py](data_converter.py) | CSV → Parquet conversion (5–10× faster loads) |
| [config.py](config.py) | Ranges + fixed config for the legacy `run_optimizer.py` CLI only |
| [run_optimizer.py](run_optimizer.py) | Legacy standalone CLI optimizer |
| [backtests/](backtests/) | Your strategy scripts — [README](backtests/README.md) |
| [static/](static/) | Dashboard frontend — [README](static/README.md) |
| [walk_forward/](walk_forward/) | Walk-forward testing engine — [README](walk_forward/README.md) |
| [Charts/](Charts/) | Standalone tick-data chart explorer — [README](Charts/README.md) |
| `optimizations/` | **Created at runtime.** One folder per optimization run, plus `user_data.json` (saved/favourited runs and their groupings) |
| `wfo_runs/` | **Created at runtime.** One folder per walk-forward run |
| `runs/` | **Created at runtime.** Output of the legacy `run_optimizer.py` CLI only |

`optimizations/` and `wfo_runs/` are output directories — the code creates them
on demand, so they may not exist in a fresh checkout. They also grow large: a
1000-batch run is 1000 subfolders, each holding whatever your strategy wrote.

---

## Core modules

### `script_analyzer.py` — parameter discovery

Parses a strategy with Python's `ast` module (never executes it) and looks for,
in order:

1. `CONFIG = { ... }` or `CONFIG = dict(...)` at module level
2. A config class with a substantial `__init__` (the `StrategyConfig` pattern)
3. `def main(...)` keyword arguments with defaults
4. `def run_backtest(...)` arguments

It infers each parameter's type (`int`, `float`, `bool`, `str`, `path`,
`date_str`, `time_str`, `dict`, `list`) and classifies it as **fixed** or
**optimizable** — paths, dicts, lists, and names containing `path`/`dir`/`file`/
`symbol`/`debug` are treated as fixed; numbers and bools are optimizable.

It also reports **walk-forward compatibility**: whether the script exposes
start/end dates (flat like `start_date`, or nested like
`Backtest_period.start_date`), a data path, and a timeframe.

### `worker.py` — one batch

Given a batch directory, it `chdir`s into it so all strategy output lands there,
deserializes `params.json` back into Python types (`"09:15:00"` → `datetime.time`),
calls the strategy's entry point, then finds its results:

1. the largest `*.xlsx` in the folder → reads its **"Technical Statistics"** sheet
2. otherwise a `*trade*.csv` → computes Net Profit, Win Rate, Profit Factor,
   Sharpe and Max Drawdown itself
3. otherwise records zeros (a valid outcome: restrictive parameters, no trades)

Metric names are normalised onto canonical keys (`net profit ($)`, `net pnl`,
`max drawdown` → `Net Profit`, `Overall Max Drawdown`, …) so the ranking
functions work across strategies that label things differently.

Strategy exceptions are caught and recorded in `metrics.json` with
`status.ok = false`. **The worker still exits 0 in that case** — which is why the
engine determines a batch's outcome by reading `metrics.json`, never from the
exit code.

### `optimizer_engine.py` — the optimizer

`run_optimization()` is the entry point. It plans the parameter combinations,
creates a batch folder per combination, runs them through a `ProcessPoolExecutor`,
tracks progress, then aggregates every batch's params + metrics into a ranked
table.

Notable functions:

| Function | Purpose |
|---|---|
| `generate_grid_params(params, limit, seed)` | Cartesian product; with a `limit` it samples indices instead of building the whole product |
| `generate_random_params(params, n, seed)` | Seeded random sampling, duplicates skipped |
| `build_bayesian_optimizer()` / `ask_bayesian_points()` | scikit-optimize wrapper for the ask/tell loop |
| `score_metrics(metrics, ranking_metric)` | Single source of truth for a batch's score — used by both the Bayesian objective and the final ranking |
| `read_batch_state(dir)` | `ok` / `failed` / `pending`, read from disk |
| `build_progress_snapshot(opt_dir)` | Rebuilds a run's progress by scanning its batch folders |
| `run_one(...)` / `run_one_slim(...)` | Subprocess launcher; the slim variant is what the pool uses |

---

## Search modes

### Random (default)

Seeded uniform sampling across the ranges, snapped to `step` where given, with
exact duplicates skipped. Reproducible for a given seed.

### Grid

Exhaustive cartesian product of the ranges. When the grid is larger than your
iteration count, it **samples indices into the grid and decodes them** rather
than materialising the full product — asking for 1000 points costs 1000 dicts
whether the grid holds ten thousand combinations or ten billion.

> Measured: 1000 points from a 990,000-combination grid — 0.2 MB and 0.01 s,
> versus 181.8 MB and 3.91 s for build-then-sample.

A grid with no iteration limit still refuses to materialise more than 1,000,000
combinations.

### Bayesian

A real **ask → evaluate → tell** loop over scikit-optimize:

1. The first `BAYESIAN_INITIAL_POINTS` (10) points are quasi-random exploration.
2. Each wave of `num_workers` points runs in parallel.
3. Their scores are fed back with `opt.tell()`, updating the surrogate model.
4. The next wave is chosen from what the model has learned.
5. Repeat until `num_iterations` points have been evaluated.

The objective is the negated ranking score (skopt minimises). Failed batches get
an objective slightly worse than the worst real one — not an infinity, which a
Gaussian process cannot fit.

Once the search converges, skopt starts proposing points it has already tried and
falls back to random ones. That is normal and is reported once per wave:

```
[optimizer] Bayesian search has converged — 2/4 points in this wave fell back to random exploration
```

Because Bayesian points are chosen *from earlier results*, they cannot be
regenerated from a seed. The plan is therefore written to `sweep_plan.json` as it
grows, so a resumed run pairs each batch folder with the parameters it was
actually created with.

If scikit-optimize is not installed, Bayesian mode falls back to random search
with a warning.

---

## Ranking

Set in the dashboard's **Ranking Metric** dropdown:

| Value | Meaning |
|---|---|
| `priority` | Net PnL × log₂(trades) × brokerage efficiency × profit factor. Rejects runs with < 10 trades. Rewards profitable, active, cost-efficient strategies |
| `composite` | `Sharpe × 2 + ProfitFactor × 1.5 + NetProfit/MaxDD` |
| `net_profit` | Raw Net Profit |
| `sharpe` | Sharpe Ratio |
| `profit_factor` | Profit Factor |

The winning score lands in the `composite_score` column of the results table
regardless of which metric produced it.

### Drawdown optimization

An optional second pass that re-scores results to favour **low drawdown** while
enforcing a minimum trading frequency:

- **Disabled** — rank purely by the metric above.
- **Manual** — you supply min/target trades-per-day. Below the minimum, a result
  is rejected outright; between minimum and target the score ramps from 0.3 to
  1.0.
- **Auto** — the same thing with thresholds derived from the run's own results
  (25th percentile for the minimum, 75th for the target), computed after all
  batches finish.

---

## Progress, stop and resume

### Progress is counted from disk

A batch counts as done only once it has left a `metrics.json` behind, and its
outcome comes from that file's `status.ok`. A worker that dies without writing
anything is recorded as a failure (with the reason) rather than silently
counting as progress. `progress.json` therefore always satisfies:

```
completed == ok + failed        percent == completed / total × 100
```

### Stop

**Stop** sets a flag the engine checks after every batch. Nothing further is
dispatched; batches already in flight are allowed to finish so their results
aren't thrown away. The run ends as `stopped` and stays resumable.

### Resume

A run can be resumed after a stop, a crash, or a server restart:

- On startup the server marks any run left in a live status (`running`,
  `aggregating`, `resuming`, `stopping`) as **`interrupted`**, with its counters
  rebuilt from the batch folders on disk.
- **Resume** replays the run's saved `optimization_config.json` against its
  existing `runs/` folder. Batches with a successful `metrics.json` are skipped;
  failed and unfinished ones are re-run.
- The dashboard reattaches automatically on page load via
  `GET /api/optimize/active`, so reloading during a run puts you back on the live
  progress bar rather than an empty tab.

The original `started_at` is preserved across resumes, and each restart is
appended to a `resume_history` list in the run's config.

---

## Output layout

```
optimizations/
└── 20260901_120705_MyStrategy/          # <timestamp>_<script name>
    ├── optimization_config.json         # everything needed to resume: script path,
    │                                    #   fixed + optimizable params, mode, seed,
    │                                    #   workers, ranking, status, resume_history
    ├── progress.json                    # live counters (see below)
    ├── sweep_plan.json                  # the exact parameter plan, in batch order
    ├── optimization_results.csv         # every batch: params + metrics + score
    ├── optimization_results.parquet     # same, compressed
    ├── top_results.csv                  # the top N
    └── runs/
        ├── batch_0001/
        │   ├── params.json              # this batch's full parameter set
        │   ├── metrics.json             # status, parsed metrics, elapsed seconds
        │   └── ...                      # whatever your strategy wrote (xlsx, csv)
        ├── batch_0002/
        └── ...
```

`progress.json`:

| Field | Meaning |
|---|---|
| `total` | Total batches planned |
| `completed` / `ok` / `failed` | Finished, and the split. Always `completed == ok + failed` |
| `pending` / `remaining` | Batches with no result yet / what a resume would run |
| `percent` | `completed / total × 100` |
| `status` | `running`, `aggregating`, `stopping`, `stopped`, `interrupted`, `completed`, `error` |
| `eta_seconds` | Expected, from observed wall-clock throughput |
| `eta_max_seconds` | Worst case: slowest batch seen × ⌈remaining / workers⌉ |
| `elapsed_seconds` | Time in the current run |
| `resumable` | Whether anything is left to run |
| `mode` / `workers` / `resumed` / `queued` | Run context |

Walk-forward output lives under `wfo_runs/` — see
[walk_forward/README.md](walk_forward/README.md).

---

## HTTP API

### Scripts

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/scripts` | List `.py` files in `backtests/` |
| `POST` | `/api/scripts/analyze` | Parse a script's parameter schema |

### Optimization

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/optimize/start` | Start a run; returns its `optimization_id` |
| `POST` | `/api/optimize/resume/{id}` | Resume an interrupted/stopped/errored run |
| `POST` | `/api/optimize/stop/{id}` | Stop a run (stays resumable) |
| `GET` | `/api/optimize/progress/{id}` | SSE stream of live progress |
| `GET` | `/api/optimize/status/{id}` | One-shot progress snapshot |
| `GET` | `/api/optimize/active` | The run the dashboard should attach to |

### Results

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/optimizations` | List all runs with status and percent |
| `GET` | `/api/optimizations/{id}/results?top=N` | Ranked results table |
| `GET` | `/api/optimizations/{id}/batch/{batch}` | One batch's params, metrics, files |
| `DELETE` | `/api/optimizations/{id}` | Delete a run (refused while running) |
| `GET` | `/api/download/{id}/{batch}/excel` | Download a batch's Excel report |
| `GET` | `/api/excel-viewer/{id}/{batch}` | Excel sheets rendered as HTML |

### Data and charts

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/data/convert` | Convert a CSV to Parquet |
| `GET` | `/api/data/range` | Detect a dataset's date range |
| `GET` | `/api/chart/ohlc` | OHLC bars for charting |
| `GET` | `/api/chart/trades/{id}/{batch}` | Trade markers for a batch |

### Walk-forward

`/api/walk-forward/` — `runs`, `create`, `{id}/start`, `{id}/stop`,
`{id}/progress` (SSE), `{id}`, `{id}/windows`, `{id}/steps`, `{id}/results`,
`{id}/parameters`, `{id}/candidates`, `{id}/full-sample`, `{id}/export/{type}`,
and `DELETE {id}`.

### Saved runs

`/api/user-data` plus `/save`, `/delete`, `/group`, `/group/delete` — backs the
dashboard's star/favourite feature, stored in `optimizations/user_data.json`.

> Because it lives inside `optimizations/`, deleting that directory also clears
> your saved runs and groups.

---

## Troubleshooting

**Every batch fails immediately.**
Open a failing batch in the Results table — `metrics.json` holds the full
traceback from your strategy. Most often the entry point signature doesn't match
what was detected, or a data path in the fixed parameters is wrong.

**Batches die without producing any result.**
They are recorded as failures with the worker's exit code and stderr tail. The
usual cause is memory pressure: `num_workers` is how many full copies of your
strategy (and its data) run at once. Twenty workers each loading a large dataset
will exhaust RAM. Lower the worker count, or convert your CSV to Parquet first.

**A run says "interrupted" after restarting the server.**
Expected — that is how an orphaned run is labelled. Click **Resume** on its
history card to finish the outstanding batches.

**Bayesian mode runs fewer iterations than requested.**
Check that scikit-optimize is installed (`py -3.12 -c "import skopt"`). Without
it, the engine warns and falls back to random search.

**Grid mode refuses to start: "search space is too large".**
That guard only applies when the grid is being built exhaustively. Set an
iteration count below the grid size and it will sample instead.

**A batch is killed after an hour.**
`BATCH_TIMEOUT_SECONDS` in `optimizer_engine.py` caps a single batch at 3600 s.
Raise it if your strategy legitimately runs longer.

**The dashboard shows an old percentage.**
Progress is recomputed from the batch folders whenever a run isn't live, so
reloading the page corrects stale counters.

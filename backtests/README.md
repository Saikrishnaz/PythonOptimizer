# backtests/

Your strategy scripts live here. Anything ending in `.py` (and not starting with
`__`) shows up in the dashboard's script dropdown.

There is no base class to inherit and nothing to register. The optimizer reads
your script with Python's AST parser to discover its parameters, then runs it as
a subprocess once per parameter combination.

---

## The contract

A script is optimizable if it satisfies three things:

1. **An entry point** the optimizer can call — `main()` or `run_backtest()`.
2. **Parameters with defaults**, in one of the shapes below.
3. **Output the optimizer can read** — an Excel report or a trades CSV, written
   into the current working directory.

That's it. Everything else about your strategy is your business.

---

## 1. Entry point styles

The analyzer picks one of these automatically and records it as `entry_style`.

### `function_kwargs` — parameters as keyword arguments

```python
def main(fast=20, slow=50, atr_mult=1.5, data_path="data.parquet"):
    ...
```

The optimizer calls `main(**params)`. Arguments your function doesn't declare
are dropped before the call, so extra keys never raise `TypeError` — unless you
declare `**kwargs`, in which case you receive everything.

Used by `CreadspreadBacktest_FIXED.py` and `MeanReversionBacktestXAUUSD.py`.

### `config_class` — a config object

```python
class StrategyConfig:
    def __init__(self, st_length=5, st_multiplier=1.5, data_path="data.parquet", ...):
        self.st_length = st_length
        ...

def run_backtest(config: StrategyConfig):
    ...
```

The optimizer builds `StrategyConfig(**params)` — filtering to the arguments
`__init__` actually accepts — then calls `run_backtest(config)`. The class needs
more than three `__init__` parameters to be recognised as a config class.

Used by all four `SupertrendBacktestXAUUSD*.py` scripts.

### `config_dict` — a module-level CONFIG

```python
CONFIG = {
    "fast": 20,
    "slow": 50,
    "data_path": "data.parquet",
}
```

Recognised as `CONFIG = {...}` or `CONFIG = dict(...)`. If a script has both a
`CONFIG` dict and a config class, the dict's values win and the class supplies
any parameters the dict omits.

---

## 2. How parameters are detected

[`script_analyzer.py`](../script_analyzer.py) parses your file — it never
executes it, so import side effects and slow module-level work don't run during
analysis.

### Types

Inferred from each default value:

| Inferred type | From |
|---|---|
| `int`, `float`, `bool` | numeric / boolean literals |
| `path` | a string with a separator ending in `.csv`, `.parquet`, `.xlsx`, `.json`, `.txt` |
| `date_str` | `"2026-01-02"` |
| `time_str` | `"09:15"` or `"09:15:00"` |
| `dict`, `list` | dict/list/tuple literals |
| `str` | anything else |

`datetime.time(9, 15)` and `datetime.date(2026, 1, 2)` calls in your defaults are
recognised and converted to `"09:15:00"` / `"2026-01-02"` strings.

### Fixed vs optimizable

The dashboard splits parameters into two groups. A parameter is **fixed** if:

- its type is `path`, `dict`, or `list`, or
- its name contains `path`, `dir`, `file`, `csv`, `parquet`, `xlsx`, `report`,
  `symbol`, `show_pnl`, `display`, or `debug`

Everything numeric or boolean is **optimizable**. This is only the default
grouping — you choose what actually gets swept in the UI.

> Naming matters. Call something `sl_multiplier` and it is offered for sweeping;
> call it `sl_file` and it won't be.

### Round-tripping

Parameters are written to `params.json` per batch and read back by the worker.
Strings matching `HH:MM:SS` are converted back to `datetime.time` objects before
your entry point is called, so time parameters survive the trip.

---

## 3. Producing readable results

The worker looks for output in this order, inside the batch's own directory:

### Preferred: an Excel report with a "Technical Statistics" sheet

The **largest** `.xlsx` in the folder is read, specifically a sheet named
`Technical Statistics`, using column A as the metric name and column B as its
value.

```
| Metric              | Value  |
|---------------------|--------|
| Total Trades        | 412    |
| Net Profit          | 18500  |
| Win Rate %          | 54.3   |
| Profit Factor       | 1.62   |
| Sharpe Ratio        | 1.21   |
| Overall Max Drawdown| -4200  |
```

Metric names are normalised, so these all map onto `Net Profit`:
`net profit`, `net profit ($)`, `net profit (inr)`, `net profit (rs)`, `net pnl`.
Likewise `max drawdown` → `Overall Max Drawdown`, and `win rate` → `Win Rate %`.

Every other row is kept under its own name and becomes a column in the results
table — so custom metrics like `Brokerage Ratio %` or `Net PnL After Costs` come
through and can be ranked on.

> If your statistics use their own wording (e.g. `Net Profit (points)`), emit an
> explicit canonical `Net Profit` row **as well**. Both columns then survive
> instead of one collapsing into the other.

### Fallback: a trades CSV

Any `*trade*.csv` or `*python_trade*.csv`. The worker finds the P&L column by
trying, in order:

```
net_pnl, pnl, profit, Net PnL, P&L, realized_pnl,
profit_with_hedges_inr, profit_with_hedges_points,
profit_in_inr, profit_points
```

then computes Total Trades, Win Rate %, Net Profit, Profit Factor, Sharpe Ratio,
Overall Max Drawdown, Average Win and Average Loss itself.

### Neither

Recorded as a successful run with zero trades and zero profit. This is a real
outcome — restrictive parameters that never triggered an entry — not an error.

---

## 4. Writing files

**Write to relative paths.** The worker `chdir`s into the batch's own directory
before calling you, so `df.to_csv("trades.csv")` lands in
`optimizations/<run>/runs/batch_0042/trades.csv` and never collides with the
other batches running at the same time.

If an output path arrives as a parameter, the worker rewrites it to its basename
when the parameter name contains `trade`, `report`, `val_`, or `out` — so an
absolute path baked into a default won't send twenty parallel batches at the same
file.

---

## 5. Errors

Raise normally. The worker catches the exception, records the type, message and
full traceback in `metrics.json` under `status.error`, and marks the batch
failed. It shows up in the Results table with the first 200 characters of the
error, and the full traceback is in the batch's `metrics.json`.

A failed batch is retried on resume, since whatever broke may since be fixed.

---

## 6. Walk-forward compatibility

To be usable by the [walk-forward engine](../walk_forward/README.md), a script
must expose a start date and an end date, either flat:

```python
def main(start_date="2026-01-01", end_date="2026-06-30", ...):
```

or nested:

```python
CONFIG = {
    "Backtest_period": {"start_date": "2026-01-01", "end_date": "2026-06-30"},
}
```

Both styles are detected. A data path parameter (name containing `data`, `tick`,
`spot` or `input`, and *not* `report`/`output`/`trades`) and a timeframe
parameter (`timeframe`, `tf`, `period`, `interval`) are also detected and used
where present.

---

## 7. Practical notes

**Keep stdout modest.** Batch stdout is discarded rather than captured, so
printing is free from a memory standpoint — but it still costs the child process
I/O time. On a 1000-batch sweep, a per-bar progress log is real wall-clock time.

**Convert big CSVs to Parquet.** The dashboard's data tools do this, or call
[`data_converter.py`](../data_converter.py) directly. Parquet loads roughly 5–10×
faster, which matters when it happens once per batch.

**Mind `num_workers`.** Each worker is a full process loading your strategy and
its data. Twenty workers means twenty copies of the dataset resident at once.

**Reuse the shared analytics.** [`backtest_analytics.py`](../backtest_analytics.py)
produces the drawdown-episode and drawdown-statistics tables without you having
to reimplement them. It resolves the P&L and timestamp columns per strategy, so
it works with a trade log of any shape.

---

## Bundled strategies

| Script | Entry style | Notes |
|---|---|---|
| `CreadspreadBacktest_FIXED.py` | `function_kwargs` | NIFTY options credit spread. Needs `pandas-ta` and `backtesting` |
| `MeanReversionBacktestXAUUSD.py` | `function_kwargs` | XAUUSD mean reversion |
| `SupertrendBacktestXAUUSD_FIXED.py` | `config_class` | Supertrend, `StrategyConfig` + `run_backtest()` |
| `SupertrendBacktestXAUUSD_FIXED_Initial.py` | `config_class` | Earlier revision of the above |
| `SupertrendBacktestXAUUSD_M1.py` | `config_class` | 1-minute variant |
| `SupertrendBacktestXAUUSD_M1_New.py` | `config_class` | Later 1-minute variant |

# Charts/

A standalone tick-data chart explorer. **Separate from the optimizer** — its own
Flask app, its own port, its own frontend.

Use it to look at the data before or after optimizing: load a tick dataset,
resample it to any timeframe, and overlay indicators computed server-side.

> The optimizer dashboard has its own chart tab (TradingView Lightweight Charts,
> in [`../static/app.js`](../static/app.js)) for plotting a batch's trades. This
> subproject is the older, standalone data explorer. They share no code.

---

## Running it

```powershell
cd Charts
py -3.12 server.py
```

Then open **http://localhost:5000**. Flask runs with `debug=True`, so it
reloads on edit.

Needs `flask` in addition to the root `requirements.txt`:

```powershell
py -3.12 -m pip install flask
```

### Before it will start: point it at a dataset

`server.py` has the dataset path hardcoded near the top:

```python
PARQUET_FILE = r"c:\Users\ADMIN\Desktop\Charts\XAUUSD.._202601020100_202608101443.parquet"
```

Two things to know:

1. **That path is stale.** It points at `Desktop\Charts\`, not this directory
   (`Desktop\PythonOptimizer\Charts\`).
2. **The dataset is not in the repository.** Tick parquet files run to hundreds
   of megabytes and aren't committed.

Edit `PARQUET_FILE` to wherever your dataset actually lives before starting the
server.

The engine expects a **tick** dataset with a `DatetimeIndex` and a `Bid` column —
the format [`csv_to_parquet.py`](csv_to_parquet.py) produces from an MT5 tick
export.

---

## Files

| File | What it does |
|---|---|
| `server.py` | Flask app — 5 routes, ~66 lines |
| `data_engine.py` | `DataEngine` — loads the parquet, resamples ticks to OHLC, caches timeframes |
| `indicators.py` | `IndicatorRegistry` plus six indicator implementations |
| `csv_to_parquet.py` | CLI: convert an MT5 tick CSV to compressed Parquet |
| `generate_ohlc.py` | Batch-export OHLC CSVs from a tick parquet into `generated_ohlc/` |
| `templates/index.html` | The whole frontend (~20 KB) |
| `static/` | Currently empty — the page is self-contained |
| `image.png` | Reference screenshot |

---

## How the data engine works

`DataEngine.load_data()` reads the tick parquet once, then builds a **1-minute
base** by resampling the `Bid` series:

```python
resampled = tick_df["Bid"].resample("1min").ohlc()
resampled["volume"] = tick_df["Bid"].resample("1min").count()
```

Volume is *tick count* per bar, not traded volume — tick data has no volume
field, so this is a proxy for activity.

Every other timeframe (5m, 15m, 1h, 4h, 1D) is derived from that 1-minute base
and cached in `timeframe_cache`, so the first request for a timeframe pays the
resampling cost and the rest are instant.

Loading happens lazily on the first request via a `@app.before_request` hook, so
startup is fast but the first page load waits for the parquet.

Symbol metadata (`MULTIBANK` / `XAUUSD` / "Gold Spot / U.S. Dollar") is hardcoded
in `DataEngine.__init__` — change it there for a different instrument.

---

## Indicators

Computed on the **backend** and sent to the browser as ready-to-plot series.
Registered through a decorator:

```python
@IndicatorRegistry.register(
    name="sma",
    display_name="Simple Moving Average",
    category="overlay",
    overlay=True,
    params_schema=[...],
)
def calculate_sma(df, period=20, color="#2962FF") -> dict:
    ...
```

Built in:

| Name | Overlay | Parameters |
|---|---|---|
| `supertrend` | yes | `length`, `multiplier` |
| `sma` | yes | `period`, `color` |
| `ema` | yes | `period`, `color` |
| `bollinger` | yes | `period`, `std_dev` |
| `rsi` | no (own pane) | `period`, `color` |
| `macd` | no (own pane) | `fast`, `slow`, `signal` |

To add one: write the function, decorate it with a `params_schema` describing its
inputs, and it appears in the UI automatically — `/api/indicators/list` is
generated from the registry.

---

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The chart page |
| `GET` | `/api/info` | Symbol, broker, and available date range |
| `GET` | `/api/ohlc` | OHLC bars for a timeframe |
| `GET` | `/api/indicators/list` | Every registered indicator and its parameter schema |
| `POST` | `/api/indicators/calculate` | Compute one indicator over the current timeframe |

---

## Utilities

### `csv_to_parquet.py`

```powershell
py -3.12 csv_to_parquet.py <input.csv> [output.parquet]
```

Handles MT5 tick exports (`<DATE>` + `<TIME>` columns, `<BID>`/`<ASK>`/`<LAST>`),
auto-detects the delimiter (tab, semicolon, comma), builds a `DatetimeIndex`,
sorts it, and writes zstd-compressed Parquet. Typically ~70% smaller and far
faster to load.

The root project has its own enhanced copy at
[`../data_converter.py`](../data_converter.py), which the optimizer dashboard
exposes through `POST /api/data/convert`. Prefer that one when working inside the
optimizer; this copy exists so `Charts/` stays standalone.

### `generate_ohlc.py`

```powershell
py -3.12 generate_ohlc.py
```

Reads a tick parquet and writes OHLC CSVs for each timeframe into
`generated_ohlc/`. The input filename is hardcoded at the top of the script —
edit it before running. The output directory is created on demand and is not
committed.

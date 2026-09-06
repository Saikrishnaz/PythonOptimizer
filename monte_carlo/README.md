# monte_carlo/

Monte Carlo robustness analysis for a finished trade log.

## The idea

A backtest gives you one number. That number is one draw from a distribution
of outcomes the same strategy could have produced — the trades arrived in a
particular order, some fills happened and others would not have, costs were
what they were on the day. Monte Carlo resamples the trades that actually
happened so you can see the rest of that distribution.

The question it answers is not "how much did this make?" but **"how much of
this was edge, and how much was the draw?"**

Nothing here re-runs a backtest. Every method operates on a realised sequence
of per-trade P&L, which makes a run take seconds instead of hours — and means
a Monte Carlo result can never contradict the backtest it came from, only
contextualise it.

## Methods

| Method | What it changes | What it tells you |
| --- | --- | --- |
| `reorder` | The order of the same trades | Net profit is fixed by construction, so every difference is sequencing luck — almost all of it in the drawdown. If shuffling produces far deeper drawdowns than the backtest reported, the reported figure was a good draw, not a ceiling. |
| `resample` | Draws trades with replacement | The broad "is this repeatable?" test. Some trades repeat, others never appear, so both profit and drawdown move. |
| `block_bootstrap` | Draws contiguous blocks | Same as `resample` but keeps winning and losing streaks intact. Use it when trades are correlated rather than independent — trend strategies usually are. |
| `skip` | Drops a random share of trades | Downtime, throttling, fills that never came. Shows how much the result depends on catching every signal. |
| `noise` | Perturbs every trade's P&L | Slippage, spread widening, fee changes. The perturbation is multiplicative, so a large trade is exposed to proportionally more cost — which is how execution actually behaves. |

Several methods can run in one job; each gets its own distribution.

## What comes back

Per method:

- **Percentiles** (1/5/10/25/50/75/90/95/99) for net profit, max drawdown,
  drawdown %, profit factor, win rate and Sharpe
- **Probabilities** — of profit, of ruin, of exceeding your drawdown limit, of
  exceeding the drawdown the backtest actually had, of beating its profit
- **Risk** — VaR 95, CVaR 95, worst case, median drawdown
- **Where the real backtest sits** in each distribution, as a percentile. A
  backtest at the 95th percentile of its own resampled distribution was a
  lucky draw; planning around it is planning around a good day.
- **Equity percentile bands**, a **profit histogram** and a **drawdown
  histogram**, all downsampled for the browser
- **Findings** — the same numbers written out in words

## Memory

A run of 10,000 simulations over 20,000 trades is a 200-million-cell matrix —
1.5 GB if it were materialised in one piece. It never is. Simulations are
generated in chunks sized to a fixed cell budget (`CHUNK_CELL_BUDGET`), each
chunk is reduced to per-simulation metrics, and only those metrics plus a
120-point equity checkpoint grid survive it. Measured peak for that run is
about 90 MB, in roughly 30 seconds.

The result document is small for the same reason: distributions are summarised
before they are stored, so a saved run is tens of kilobytes rather than the
gigabytes of paths that produced it.

## Module map

| File | Role |
| --- | --- |
| `engine.py` | The simulation core: path generation per method, chunked reduction, summaries, findings. |
| `sources.py` | Turns a source spec into an array of trade P&Ls. Column detection is shared with `walk_forward/report.py` rather than duplicated. |
| `persistence.py` | Saves, lists, loads and deletes runs under `monte_carlo_runs/`. |

## Sources

| Spec | Trades used |
| --- | --- |
| `{"type": "batch", "optimization_id": ..., "batch_id": ...}` | One optimization batch's trade log |
| `{"type": "wfo", "run_id": ...}` | Every out-of-sample trade of a walk-forward run, in walk-forward order |
| `{"type": "wfo_step", "run_id": ..., "step": n}` | One walk-forward step's OOS trades |
| `{"type": "csv", "path": ...}` | Any trade CSV on disk |

Walk-forward OOS trades are the most honest source available: they are the
only trades in the project that were produced by parameters chosen without
seeing them.

A batch only appears in the picker if its strategy actually wrote a
`*trades*.csv`. Batches that produced only an xlsx cannot be resampled,
because per-trade P&L is what every method needs.

## Output layout

```
monte_carlo_runs/
    mc_<run_id>/
        result.json      config, actual metrics, one block per method, findings
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/monte-carlo/methods` | The available methods and what each is for |
| GET | `/api/monte-carlo/sources` | Optimizations and walk-forward runs that can supply trades |
| GET | `/api/monte-carlo/batches/{opt_id}` | Batches of one run that kept a trade log, best first |
| POST | `/api/monte-carlo/inspect` | What a source contains, without simulating |
| POST | `/api/monte-carlo/run` | Run a simulation |
| GET | `/api/monte-carlo/runs` | Saved runs |
| GET | `/api/monte-carlo/runs/{id}` | One saved run |
| DELETE | `/api/monte-carlo/runs/{id}` | Delete a saved run |

`run` and `inspect` are declared `def`, not `async def`, so FastAPI executes
them in its thread pool. A few seconds of numpy on the event loop would stall
every live SSE progress stream.

## Sizing a run

Cost scales with `simulations × trades × methods`. On this project's own trade
logs the engine sustains roughly 11 million simulated trades per second, and
the dashboard uses that figure to show an estimate before you commit. Some
reference points:

| Simulations | Trades | Methods | Cells | Rough time |
| --- | --- | --- | --- | --- |
| 2,000 | 1,000 | 2 | 4M | under a second |
| 10,000 | 5,000 | 3 | 150M | ~15 s |
| 10,000 | 20,000 | 1 | 200M | ~30 s |

`simulations` is capped at 200,000.

## Reading the output honestly

- Resampling cannot manufacture evidence that is not in the input. Fifty
  trades resampled ten thousand times is still fifty trades, and the engine
  says so in its findings.
- `reorder` always reports a 0% or 100% probability of profit, because it
  cannot change the total. That number is meaningless for this method and is
  deliberately left out of its findings; read its drawdown instead.
- These distributions describe the trades the strategy took on the data it was
  given. They say nothing about a regime it has not traded through.

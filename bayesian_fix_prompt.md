# Prompt: Fix Bayesian Optimization in PythonOptimizer

## Context

I have an optimizer dashboard at `c:\Users\ADMIN\Desktop\PythonOptimizer` that runs backtests with different parameter combinations. It supports Grid, Random, and Bayesian optimization modes.

**The Bayesian optimization is broken — it only ever runs 10 batches regardless of the `num_iterations` setting (e.g., if I set 1000 iterations, I still get only 10).**

## Root Cause (already diagnosed)

In `c:\Users\ADMIN\Desktop\PythonOptimizer\optimizer_engine.py`, the function `generate_bayesian_params()` (line ~284) has two bugs:

1. **Line 317**: `for _ in range(min(n, 10))` — hard-caps at 10 iterations regardless of `n`
2. **No feedback loop**: The function calls `opt.ask()` to get initial random points but NEVER calls `opt.tell(point, score)` to feed results back. It generates 10 random points and returns — there's no actual Bayesian learning happening.

The function currently:
- Creates a `skopt.Optimizer` with `n_initial_points=min(n, 10)`
- Asks for 10 random points
- Returns them — done. No iteration, no learning.

## What Needs to Happen

Implement **proper iterative Bayesian optimization** with the feedback loop:

1. Generate a small initial batch of random points (e.g., `n_initial_points` = ~10 or configurable)
2. Run those batches, collect their metric scores
3. Call `opt.tell(points, scores)` to update the surrogate model
4. Call `opt.ask()` to get the next batch of smarter points
5. Repeat steps 2-4 until all `n` iterations are complete

## Key Files

- **`c:\Users\ADMIN\Desktop\PythonOptimizer\optimizer_engine.py`** — Contains `generate_bayesian_params()` (the broken function) and `run_optimization()` (the main loop that calls it). The main loop currently generates ALL parameter combos upfront and then runs them in parallel — this architecture needs to change for Bayesian since points must be generated iteratively based on results.
- **`c:\Users\ADMIN\Desktop\PythonOptimizer\worker.py`** — Subprocess worker that runs a single batch and writes `metrics.json`
- **`c:\Users\ADMIN\Desktop\PythonOptimizer\server.py`** — FastAPI server that exposes `/api/optimize/start` and `/api/optimize/progress` endpoints
- **`c:\Users\ADMIN\Desktop\PythonOptimizer\static/app.js`** — Frontend that displays progress

## Constraints

- **Do NOT disrupt existing code** — Grid and Random modes must continue working exactly as before
- **Resume support must still work** — completed batches (those with `metrics.json`) should be skipped on restart
- **Progress reporting must still work** — the `/api/optimize/progress` endpoint polls `progress.json` for live updates
- **Parallel workers** — Bayesian can run small parallel batches (e.g., ask for `num_workers` points at a time, run them in parallel, tell results, ask again)
- **The ranking metric** used to score batches is computed in `run_optimization()` around line ~560-620 via `priority_score()`. The Bayesian optimizer should minimize the negative of this score (since skopt minimizes by default)

## Architecture Suggestion

Instead of generating all combos upfront, the Bayesian path in `run_optimization()` should:

```
initial_points = min(n_initial_points, num_iterations)
remaining = num_iterations - initial_points

# Phase 1: Random exploration
points = opt.ask(n_points=initial_points)
run batches, collect scores
opt.tell(points, scores)

# Phase 2: Bayesian exploitation  
while remaining > 0:
    batch_size = min(num_workers, remaining)
    points = opt.ask(n_points=batch_size)
    run batches, collect scores
    opt.tell(points, scores)
    remaining -= batch_size
```

Please investigate the full codebase and implement a proper fix. Make sure to test that Grid/Random modes are unaffected.

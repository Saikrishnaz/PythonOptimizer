"""
server.py
---------
FastAPI backend for the Universal Python Algo Optimizer Dashboard.

Endpoints:
  GET  /api/scripts                    — List all .py files in backtests/
  POST /api/scripts/analyze            — Analyze a script's CONFIG parameters
  POST /api/optimize/start             — Start an optimization run
  GET  /api/optimize/progress/{id}     — SSE stream of live progress
  POST /api/optimize/stop/{id}         — Stop a running optimization
  POST /api/optimize/resume/{id}       — Resume an interrupted/stopped run
  GET  /api/optimize/status/{id}       — Progress snapshot for one run
  GET  /api/optimize/active            — Run the dashboard should attach to
  GET  /api/optimize/runs              — All running + queued runs, and the limit
  POST /api/optimize/max-concurrent    — Change how many may run at once
  DELETE /api/optimize/queue/{id}      — Drop a run out of the queue
  GET  /api/optimizations              — List all optimization runs
  GET  /api/optimizations/{id}/config  — Saved settings for one run
  GET  /api/optimizations/{id}/results — Get results for an optimization
  DELETE /api/optimizations/{id}       — Delete an optimization run
  POST /api/data/convert               — Convert CSV → Parquet
  GET  /api/chart/ohlc                 — Get OHLC data for charting
  GET  /api/chart/trades/{opt_id}/{batch} — Get trade markers for a batch
"""
import asyncio
import json
import os
import shutil
import threading
import time
import glob
from datetime import datetime
from typing import Optional

import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd

from script_analyzer import analyze_script
from data_converter import convert_csv_to_parquet, find_data_files_in_params
from optimizer_engine import (
    run_optimization,
    atomic_write_json,
    build_progress_snapshot,
    count_finished_batches,
)

# =============================================================================
# APP SETUP
# =============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKTESTS_DIR = os.path.join(SCRIPT_DIR, "backtests")
OPTIMIZATIONS_DIR = os.path.join(SCRIPT_DIR, "optimizations")

app = FastAPI(title="Universal Algo Optimizer", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Mount static files
STATIC_DIR = os.path.join(SCRIPT_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Track running optimizations
running_optimizations = {}  # id -> {"thread": Thread, "stop_flag": Event}

# How long an SSE stream may stay silent before it sends a keepalive comment.
SSE_KEEPALIVE_SECONDS = 15


# =============================================================================
# PYDANTIC MODELS
# =============================================================================
class AnalyzeRequest(BaseModel):
    script_name: str

class OptimizeRequest(BaseModel):
    script_path: str
    entry_style: str = "function_kwargs"
    config_class_name: Optional[str] = None
    fixed_params: dict = {}
    optimize_params: dict = {}
    mode: str = "random"
    num_iterations: int = 100
    num_workers: int = 2
    seed: int = 42
    ranking_metric: str = "priority"
    top_n: int = 10
    drawdown_optimization: str = "disabled"
    dd_min_trades_per_day: float = 2.0
    dd_target_trades_per_day: float = 8.0

class ConvertRequest(BaseModel):
    csv_path: str


# =============================================================================
# ROUTES — DASHBOARD
# =============================================================================
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Dashboard not found. Place index.html in static/</h1>")

@app.get("/chart", response_class=HTMLResponse)
async def serve_chart():
    chart_path = os.path.join(STATIC_DIR, "chart.html")
    if os.path.exists(chart_path):
        with open(chart_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Chart page not found</h1>")


# =============================================================================
# ROUTES — SCRIPTS
# =============================================================================
@app.get("/api/scripts")
async def list_scripts():
    """List all Python backtest scripts in backtests/ directory."""
    if not os.path.exists(BACKTESTS_DIR):
        os.makedirs(BACKTESTS_DIR, exist_ok=True)
    
    scripts = []
    for f in sorted(os.listdir(BACKTESTS_DIR)):
        if f.endswith(".py") and not f.startswith("__"):
            full_path = os.path.join(BACKTESTS_DIR, f)
            size_kb = os.path.getsize(full_path) / 1024
            scripts.append({
                "name": f,
                "path": full_path,
                "size_kb": round(size_kb, 1),
                "modified": datetime.fromtimestamp(os.path.getmtime(full_path)).isoformat(),
            })
    
    return {"status": "success", "scripts": scripts}


@app.post("/api/scripts/analyze")
async def analyze_script_endpoint(req: AnalyzeRequest):
    """Analyze a script and return its CONFIG parameter schema."""
    script_path = os.path.join(BACKTESTS_DIR, req.script_name)
    if not os.path.exists(script_path):
        raise HTTPException(404, f"Script not found: {req.script_name}")
    
    try:
        result = analyze_script(script_path)
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(500, f"Analysis failed: {str(e)}")


# =============================================================================
# OPTIMIZATION — RESUME SUPPORT
# =============================================================================
# Statuses that mean "this run was mid-flight". Anything left in one of these
# after the process died is an orphan, and is what Resume picks up.
# "planning" is a Bayesian sweep fitting its surrogate between waves: no batch
# is running, but the run is very much alive, so it belongs here or the orphan
# sweeper would leave a dead planning run unmarked.
LIVE_STATUSES = ("running", "aggregating", "resuming", "stopping", "planning")


def _opt_dir(optimization_id: str) -> str:
    return os.path.join(OPTIMIZATIONS_DIR, optimization_id)


def _read_json(path: str, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _is_live(optimization_id: str) -> bool:
    handle = running_optimizations.get(optimization_id)
    return bool(handle and handle["thread"].is_alive())


def _start_thread(optimization_id: str, cfg: dict, resume: bool = False):
    """
    Put a run on a background thread immediately, no capacity check.

    Only the scheduler calls this; everything else goes through
    _launch_optimization(), which decides between starting and queueing.
    """
    stop_event = threading.Event()

    def run_in_thread():
        try:
            run_optimization(
                script_path=cfg["script_path"],
                entry_style=cfg.get("entry_style") or "function_kwargs",
                config_class_name=cfg.get("config_class_name"),
                fixed_params=cfg.get("fixed_params") or {},
                optimize_params=cfg.get("optimize_params") or {},
                mode=cfg.get("mode") or "random",
                num_iterations=int(cfg.get("num_iterations") or 100),
                num_workers=int(cfg.get("num_workers") or 2),
                seed=int(cfg.get("seed") or 42),
                ranking_metric=cfg.get("ranking_metric") or "priority",
                top_n=int(cfg.get("top_n") or 10),
                optimization_id=optimization_id,
                drawdown_optimization=cfg.get("drawdown_optimization") or "disabled",
                dd_min_trades_per_day=float(cfg.get("dd_min_trades_per_day") or 2.0),
                dd_target_trades_per_day=float(cfg.get("dd_target_trades_per_day") or 8.0),
                resume=resume,
                stop_flag=stop_event,
            )
        except Exception as e:
            # Keep the batch tally in progress.json so the run stays resumable
            # instead of collapsing to a bare error record.
            opt_dir = _opt_dir(optimization_id)
            os.makedirs(opt_dir, exist_ok=True)
            try:
                snapshot = build_progress_snapshot(opt_dir)
            except Exception:
                snapshot = {}
            snapshot.update({"status": "error", "error": str(e)})
            try:
                atomic_write_json(os.path.join(opt_dir, "progress.json"), snapshot)
            except Exception:
                pass
        finally:
            running_optimizations.pop(optimization_id, None)
            # A slot just freed up — let the next queued run take it.
            _schedule_queued()

    thread = threading.Thread(target=run_in_thread, daemon=True)
    running_optimizations[optimization_id] = {"thread": thread, "stop_flag": stop_event}
    thread.start()


# =============================================================================
# OPTIMIZATION — SCHEDULER (concurrency limit + queue)
# =============================================================================
# Runs execute concurrently up to MAX_CONCURRENT; anything beyond that waits in
# OPTIMIZATION_QUEUE and starts automatically as slots free up. The default of 1
# keeps a single heavy sweep to itself, which is what a backtest that already
# uses num_workers processes usually wants — raise it from the dashboard.
OPTIMIZATION_QUEUE = []          # [{"optimization_id", "cfg", "resume", "queued_at"}]
_scheduler_lock = threading.RLock()
MAX_CONCURRENT = 1

# Statuses an orphaned run can be left in when the process dies. "queued" is
# included because a queued run's scheduler entry lives only in memory.
ORPHANABLE_STATUSES = LIVE_STATUSES + ("queued",)


def _running_ids() -> list:
    return [oid for oid in list(running_optimizations) if _is_live(oid)]


def _new_optimization_id(script_path: str) -> str:
    """
    A run id that is unique even when two runs start in the same second.

    The id is also the run's folder name, so a collision meant the second run
    would adopt the first one's directory — and, because the engine skips
    batches that already have results, silently resume into it instead of
    running its own sweep.
    """
    stem = os.path.splitext(os.path.basename(script_path))[0]
    base = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + stem

    candidate = base
    suffix = 2
    while (os.path.exists(_opt_dir(candidate))
           or _is_live(candidate)
           or _queued_index(candidate) >= 0):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _queued_index(optimization_id: str) -> int:
    for i, item in enumerate(OPTIMIZATION_QUEUE):
        if item["optimization_id"] == optimization_id:
            return i
    return -1


def _write_queued_progress(optimization_id: str, cfg: dict, position: int):
    """
    Give a queued run a progress.json right away.

    Without it the run is invisible until it starts — no history card, nothing
    in the progress list, and no way to tell it apart from a run that vanished.
    """
    opt_dir = _opt_dir(optimization_id)
    os.makedirs(opt_dir, exist_ok=True)

    total = int(cfg.get("num_iterations") or 0)
    snapshot = build_progress_snapshot(opt_dir) if os.path.isdir(opt_dir) else {}
    snapshot.update({
        "optimization_id": optimization_id,
        "status": "queued",
        "queued_at": datetime.now().isoformat(),
        "queue_position": position,
        "mode": cfg.get("mode"),
        "workers": cfg.get("num_workers"),
    })
    snapshot.setdefault("total", total)
    for key, value in (("completed", 0), ("ok", 0), ("failed", 0),
                       ("percent", 0.0), ("pending", snapshot.get("total", total))):
        snapshot.setdefault(key, value)

    try:
        atomic_write_json(os.path.join(opt_dir, "progress.json"), snapshot)
    except Exception:
        pass

    # Record the config too, so a queued run can be edited or resumed like any
    # other. A resume already has one; never overwrite it.
    config_path = os.path.join(opt_dir, "optimization_config.json")
    if not os.path.exists(config_path):
        try:
            atomic_write_json(config_path, {
                **cfg,
                "script_name": os.path.basename(cfg.get("script_path") or ""),
                "started_at": datetime.now().isoformat(),
                "status": "queued",
            })
        except Exception:
            pass


def _schedule_queued():
    """Start queued runs while there is spare capacity."""
    with _scheduler_lock:
        while OPTIMIZATION_QUEUE and len(_running_ids()) < MAX_CONCURRENT:
            item = OPTIMIZATION_QUEUE.pop(0)
            try:
                _start_thread(item["optimization_id"], item["cfg"], item["resume"])
                print(f"[scheduler] Started queued run {item['optimization_id']}")
            except Exception as e:
                print(f"[scheduler] Could not start {item['optimization_id']}: {e}")
        # Positions shift as runs leave the queue.
        for i, item in enumerate(OPTIMIZATION_QUEUE):
            _write_queued_progress(item["optimization_id"], item["cfg"], i + 1)


def _launch_optimization(optimization_id: str, cfg: dict, resume: bool = False):
    """
    Run now if a slot is free, otherwise queue.

    Shared by /start and /resume so both paths get the same stop-flag wiring,
    the same failure handling, and the same concurrency limit.

    Returns "started" or "queued".
    """
    with _scheduler_lock:
        if _is_live(optimization_id):
            raise HTTPException(409, "Optimization with this ID already running")
        if _queued_index(optimization_id) >= 0:
            raise HTTPException(409, "Optimization with this ID is already queued")
        running_optimizations.pop(optimization_id, None)  # clear a dead handle

        if len(_running_ids()) < MAX_CONCURRENT:
            _start_thread(optimization_id, cfg, resume)
            return "started"

        position = len(OPTIMIZATION_QUEUE) + 1
        OPTIMIZATION_QUEUE.append({
            "optimization_id": optimization_id,
            "cfg": cfg,
            "resume": resume,
            "queued_at": datetime.now().isoformat(),
        })
        _write_queued_progress(optimization_id, cfg, position)
        return "queued"


def reconcile_interrupted_runs():
    """
    Flag runs that were still in flight when the previous process died.

    Nothing is running yet at startup, so any progress.json left in a live
    status belongs to a process that is gone. Marking those 'interrupted' —
    with their counters rebuilt from the batch folders — is what lets the
    dashboard offer Resume instead of showing a run that will never advance.
    """
    if not os.path.isdir(OPTIMIZATIONS_DIR):
        return

    for opt_id in os.listdir(OPTIMIZATIONS_DIR):
        opt_dir = _opt_dir(opt_id)
        progress_path = os.path.join(opt_dir, "progress.json")
        if not os.path.isfile(progress_path):
            continue

        progress = _read_json(progress_path)
        if not isinstance(progress, dict) or progress.get("status") not in ORPHANABLE_STATUSES:
            continue

        try:
            snapshot = build_progress_snapshot(opt_dir)
            snapshot["status"] = "interrupted"
            snapshot["interrupted_at"] = datetime.now().isoformat()
            atomic_write_json(progress_path, snapshot)
        except Exception:
            continue

        config_path = os.path.join(opt_dir, "optimization_config.json")
        config = _read_json(config_path)
        if isinstance(config, dict):
            config["status"] = "interrupted"
            try:
                atomic_write_json(config_path, config)
            except Exception:
                pass

        print(f"[resume] Marked interrupted: {opt_id} "
              f"({snapshot.get('completed', 0)}/{snapshot.get('total', 0)} done, "
              f"{snapshot.get('remaining', 0)} to go)")


@app.on_event("startup")
async def _startup_reconcile():
    reconcile_interrupted_runs()


# =============================================================================
# ROUTES — OPTIMIZATION
# =============================================================================
@app.post("/api/optimize/start")
async def start_optimization(req: OptimizeRequest, background_tasks: BackgroundTasks):
    """Start an optimization run in the background."""
    # Validate script exists
    if not os.path.exists(req.script_path):
        raise HTTPException(404, f"Script not found: {req.script_path}")

    optimization_id = _new_optimization_id(req.script_path)

    cfg = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    outcome = _launch_optimization(optimization_id, cfg, resume=False)

    if outcome == "queued":
        position = _queued_index(optimization_id) + 1
        message = (f"Queued at position {position} — starts automatically when a "
                   f"slot frees up ({MAX_CONCURRENT} run"
                   f"{'' if MAX_CONCURRENT == 1 else 's'} at a time)")
    else:
        message = (f"Optimization started with {req.num_iterations} iterations "
                   f"using {req.num_workers} workers")

    return {
        "status": outcome,
        "optimization_id": optimization_id,
        "queue_position": _queued_index(optimization_id) + 1 if outcome == "queued" else 0,
        "message": message,
    }


@app.post("/api/optimize/resume/{optimization_id}")
async def resume_optimization(optimization_id: str):
    """
    Resume an interrupted, stopped, or errored optimization.

    Replays the run's saved config against its existing runs/ folder. The
    engine skips every batch that already produced a successful metrics.json,
    so only the unfinished and failed batches are executed — no restarting
    from batch 1 after a server restart.
    """
    opt_dir = _opt_dir(optimization_id)
    if not os.path.isdir(opt_dir):
        raise HTTPException(404, f"Optimization not found: {optimization_id}")

    if _is_live(optimization_id):
        raise HTTPException(409, "Optimization is already running")
    if _queued_index(optimization_id) >= 0:
        raise HTTPException(409, "Optimization is already queued")
    running_optimizations.pop(optimization_id, None)  # clear a dead handle

    config = _read_json(os.path.join(opt_dir, "optimization_config.json"))
    if not isinstance(config, dict) or not config.get("script_path"):
        raise HTTPException(400, "optimization_config.json is missing or unusable — this run cannot be resumed")

    # The run may have been created before the strategy file was moved.
    if not os.path.exists(config["script_path"]):
        fallback = os.path.join(BACKTESTS_DIR, config.get("script_name") or "")
        if config.get("script_name") and os.path.exists(fallback):
            config["script_path"] = fallback
        else:
            raise HTTPException(400, f"Strategy script not found: {config['script_path']}")

    snapshot = build_progress_snapshot(opt_dir)
    remaining = int(snapshot.get("remaining") or 0)
    if snapshot.get("total") and remaining == 0:
        raise HTTPException(409, "Nothing left to resume — every batch already succeeded")

    snapshot["status"] = "resuming"
    snapshot["resumed_at"] = datetime.now().isoformat()
    atomic_write_json(os.path.join(opt_dir, "progress.json"), snapshot)

    outcome = _launch_optimization(optimization_id, config, resume=True)
    if outcome == "queued":
        position = _queued_index(optimization_id) + 1
        return {
            "status": "queued",
            "optimization_id": optimization_id,
            "queue_position": position,
            "total": snapshot.get("total", 0),
            "completed": snapshot.get("completed", 0),
            "remaining": remaining,
            "message": f"Queued at position {position} — {remaining} batches will "
                       f"run when a slot frees up",
        }

    return {
        "status": "resumed",
        "optimization_id": optimization_id,
        "total": snapshot.get("total", 0),
        "completed": snapshot.get("completed", 0),
        "remaining": remaining,
        "message": f"Resuming {remaining} of {snapshot.get('total', 0)} batches "
                   f"({snapshot.get('completed', 0)} already done)",
    }


@app.get("/api/optimize/status/{optimization_id}")
async def get_optimization_status(optimization_id: str):
    """
    Current progress for one run.

    While a run is live its progress.json is already the source of truth;
    otherwise the counters are rebuilt from the batch folders, which also
    repairs runs recorded before progress counting was fixed.
    """
    opt_dir = _opt_dir(optimization_id)
    if not os.path.isdir(opt_dir):
        raise HTTPException(404, f"Optimization not found: {optimization_id}")

    live = _is_live(optimization_id)
    if live:
        progress = _read_json(os.path.join(opt_dir, "progress.json")) or {}
    else:
        progress = build_progress_snapshot(opt_dir)
    progress["is_running"] = live

    return {"status": "success", "optimization_id": optimization_id, "progress": progress}


@app.get("/api/optimize/runs")
async def list_active_runs():
    """
    Everything the Progress tab needs: the runs executing now, the ones waiting
    for a slot, and the current concurrency limit.
    """
    running = []
    for opt_id in sorted(_running_ids()):
        progress = _read_json(os.path.join(_opt_dir(opt_id), "progress.json")) or {}
        config = _read_json(os.path.join(_opt_dir(opt_id), "optimization_config.json")) or {}
        running.append({
            "optimization_id": opt_id,
            "script_name": config.get("script_name", ""),
            "mode": config.get("mode", ""),
            "num_workers": config.get("num_workers"),
            "progress": progress,
        })

    queued = []
    with _scheduler_lock:
        for i, item in enumerate(OPTIMIZATION_QUEUE):
            cfg = item["cfg"]
            queued.append({
                "optimization_id": item["optimization_id"],
                "script_name": os.path.basename(cfg.get("script_path") or ""),
                "mode": cfg.get("mode"),
                "num_iterations": cfg.get("num_iterations"),
                "num_workers": cfg.get("num_workers"),
                "resume": item["resume"],
                "queued_at": item["queued_at"],
                "position": i + 1,
            })

    worker_total = sum(int(r.get("num_workers") or 0) for r in running)
    return {
        "status": "success",
        "max_concurrent": MAX_CONCURRENT,
        "cpu_count": os.cpu_count() or 0,
        "worker_total": worker_total,
        "running": running,
        "queued": queued,
    }


class ConcurrencyRequest(BaseModel):
    max_concurrent: int


@app.post("/api/optimize/max-concurrent")
async def set_max_concurrent(req: ConcurrencyRequest):
    """
    Change how many optimizations may run at once.

    Raising it immediately starts whatever the queue can now afford; lowering it
    never interrupts a run that is already going — the surplus simply drains as
    those runs finish.
    """
    global MAX_CONCURRENT
    value = int(req.max_concurrent)
    if value < 1 or value > 16:
        raise HTTPException(400, "max_concurrent must be between 1 and 16")

    with _scheduler_lock:
        MAX_CONCURRENT = value
    _schedule_queued()

    return {
        "status": "success",
        "max_concurrent": MAX_CONCURRENT,
        "running": len(_running_ids()),
        "queued": len(OPTIMIZATION_QUEUE),
    }


@app.delete("/api/optimize/queue/{optimization_id}")
async def cancel_queued_optimization(optimization_id: str):
    """
    Drop a run out of the queue before it starts.

    A queued run that never executed leaves nothing worth keeping, so its
    folder is removed too — but only if no batch ever produced anything, so a
    queued *resume* never destroys the results it was going to build on.
    """
    with _scheduler_lock:
        index = _queued_index(optimization_id)
        if index < 0:
            raise HTTPException(404, "That optimization is not queued")
        OPTIMIZATION_QUEUE.pop(index)

    opt_dir = _opt_dir(optimization_id)
    runs_dir = os.path.join(opt_dir, "runs")
    has_results = False
    if os.path.isdir(runs_dir):
        has_results = count_finished_batches(
            runs_dir, len([d for d in os.listdir(runs_dir) if d.startswith("batch_")])
        ) > 0

    if not has_results and os.path.isdir(opt_dir):
        shutil.rmtree(opt_dir, ignore_errors=True)
    elif os.path.isdir(opt_dir):
        snapshot = build_progress_snapshot(opt_dir)
        snapshot["status"] = "interrupted"
        atomic_write_json(os.path.join(opt_dir, "progress.json"), snapshot)

    _schedule_queued()
    return {"status": "cancelled", "optimization_id": optimization_id,
            "removed": not has_results}


@app.get("/api/optimize/active")
async def get_active_optimization():
    """
    The run the dashboard should attach to on load.

    Prefers a run this process is actually driving; otherwise reports the most
    recent unfinished run, so reloading the page after a server restart lands
    on a Resume prompt instead of an empty progress tab.
    """
    live = [oid for oid in list(running_optimizations) if _is_live(oid)]
    if live:
        opt_id = sorted(live)[-1]
        return {
            "status": "success",
            "optimization_id": opt_id,
            "is_running": True,
            "resumable": False,
            "progress": _read_json(os.path.join(_opt_dir(opt_id), "progress.json")) or {},
        }

    if os.path.isdir(OPTIMIZATIONS_DIR):
        for opt_id in sorted(os.listdir(OPTIMIZATIONS_DIR), reverse=True):
            opt_dir = _opt_dir(opt_id)
            progress = _read_json(os.path.join(opt_dir, "progress.json"))
            if not isinstance(progress, dict):
                continue
            if progress.get("status") in LIVE_STATUSES + ("interrupted", "stopped"):
                snapshot = build_progress_snapshot(opt_dir)
                snapshot["status"] = "interrupted" if progress.get("status") in LIVE_STATUSES \
                    else progress.get("status")
                return {
                    "status": "success",
                    "optimization_id": opt_id,
                    "is_running": False,
                    "resumable": bool(snapshot.get("resumable")),
                    "progress": snapshot,
                }

    return {"status": "success", "optimization_id": None, "is_running": False, "resumable": False}


@app.get("/api/optimize/progress/{optimization_id}")
async def stream_progress(optimization_id: str):
    """SSE stream of live optimization progress."""
    opt_dir = os.path.join(OPTIMIZATIONS_DIR, optimization_id)
    progress_path = os.path.join(opt_dir, "progress.json")
    
    async def event_generator():
        last_data = ""
        last_sent = time.monotonic()
        while True:
            if os.path.exists(progress_path):
                try:
                    with open(progress_path, "r") as f:
                        data = f.read()
                    if data and data != last_data:
                        # Parse before emitting: a half-written file must never
                        # reach the client as a malformed, silently dropped frame.
                        progress = json.loads(data)
                        last_data = data
                        yield f"data: {json.dumps(progress)}\n\n"
                        last_sent = time.monotonic()

                        if progress.get("status") in ("completed", "error", "stopped", "interrupted"):
                            yield f"data: {json.dumps({'status': 'done', 'final_status': progress.get('status'), 'resumable': bool(progress.get('resumable'))})}\n\n"
                            return
                except Exception:
                    pass

            # progress.json only changes when a batch finishes, and a batch can
            # take minutes. With nothing on the wire the connection is dropped
            # as idle, EventSource reconnects, and the client re-checks status —
            # a reconnect storm for a run that is simply working. An SSE comment
            # keeps it alive and is ignored by the browser.
            if time.monotonic() - last_sent >= SSE_KEEPALIVE_SECONDS:
                yield ": keepalive\n\n"
                last_sent = time.monotonic()

            await asyncio.sleep(1)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.post("/api/optimize/stop/{optimization_id}")
async def stop_optimization(optimization_id: str):
    """
    Stop a running optimization.

    The stop flag is now honoured by the engine: no further batches are
    dispatched, batches already in flight are allowed to finish, and the run
    is left in a resumable 'stopped' state.
    """
    opt_dir = _opt_dir(optimization_id)

    # A run still waiting for a slot has nothing to interrupt — just take it
    # out of the queue.
    with _scheduler_lock:
        index = _queued_index(optimization_id)
        if index >= 0:
            OPTIMIZATION_QUEUE.pop(index)
            dequeued = True
        else:
            dequeued = False

    if dequeued:
        if os.path.isdir(opt_dir):
            snapshot = build_progress_snapshot(opt_dir)
            snapshot["status"] = "interrupted"
            try:
                atomic_write_json(os.path.join(opt_dir, "progress.json"), snapshot)
            except Exception:
                pass
        _schedule_queued()
        return {
            "status": "dequeued",
            "optimization_id": optimization_id,
            "message": "Removed from the queue before it started",
        }

    if optimization_id in running_optimizations:
        running_optimizations[optimization_id]["stop_flag"].set()
        progress_path = os.path.join(opt_dir, "progress.json")
        if os.path.exists(progress_path):
            progress = _read_json(progress_path) or {}
            progress["status"] = "stopping"
            try:
                atomic_write_json(progress_path, progress)
            except Exception:
                pass
        return {
            "status": "stopping",
            "optimization_id": optimization_id,
            "message": "Stopping — batches already running will finish first",
        }

    # Not driven by this process (e.g. the server was restarted): mark the
    # stale run stopped on disk so it can be resumed from the dashboard.
    if os.path.isdir(opt_dir):
        snapshot = build_progress_snapshot(opt_dir)
        if snapshot.get("status") in LIVE_STATUSES + ("interrupted",):
            snapshot["status"] = "stopped"
            atomic_write_json(os.path.join(opt_dir, "progress.json"), snapshot)
            return {"status": "stopped", "optimization_id": optimization_id}

    raise HTTPException(404, "Optimization not found or already completed")


# =============================================================================
# ROUTES — OPTIMIZATION MANAGEMENT
# =============================================================================
@app.get("/api/optimizations")
async def list_optimizations():
    """List all optimization runs."""
    if not os.path.exists(OPTIMIZATIONS_DIR):
        return {"status": "success", "optimizations": []}
    
    optimizations = []
    for opt_id in sorted(os.listdir(OPTIMIZATIONS_DIR), reverse=True):
        opt_dir = os.path.join(OPTIMIZATIONS_DIR, opt_id)
        if not os.path.isdir(opt_dir):
            continue
        
        config_path = os.path.join(opt_dir, "optimization_config.json")
        progress_path = os.path.join(opt_dir, "progress.json")
        
        opt_info = {"id": opt_id, "status": "unknown"}
        
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    config = json.load(f)
                opt_info.update({
                    "script_name": config.get("script_name", ""),
                    "mode": config.get("mode", ""),
                    "num_iterations": config.get("num_iterations", 0),
                    "started_at": config.get("started_at", ""),
                    "status": config.get("status", "unknown"),
                })
            except Exception:
                pass
        
        if os.path.exists(progress_path):
            try:
                with open(progress_path) as f:
                    progress = json.load(f)

                total = int(progress.get("total") or opt_info.get("num_iterations") or 0)
                status = progress.get("status", opt_info.get("status", "unknown"))

                # Recount from the batch folders. It's a stat per batch — far
                # cheaper than the os.walk below — and it keeps the card honest
                # for runs whose stored counter over-reported progress, and for
                # runs whose process died without writing a final tally.
                finished = count_finished_batches(os.path.join(opt_dir, "runs"), total)
                if not _is_live(opt_id) and status in LIVE_STATUSES:
                    status = "interrupted"

                # 'remaining' counts batches that produced no result at all.
                # A resume also retries recorded failures, so the resume
                # endpoint may report a slightly larger number — this one is
                # the floor, and is never an overstatement.
                remaining = max(total - finished, 0)
                opt_info.update({
                    "total": total,
                    "completed": finished,
                    "failed": progress.get("failed", 0),
                    "percent": round(finished / total * 100, 1) if total else 0.0,
                    "remaining": remaining,
                    "resumable": remaining > 0 and not _is_live(opt_id),
                    "status": status,
                })
            except Exception:
                pass

        # Calculate directory size
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(opt_dir):
            for f in filenames:
                total_size += os.path.getsize(os.path.join(dirpath, f))
        opt_info["size_mb"] = round(total_size / (1024 * 1024), 2)
        
        optimizations.append(opt_info)
    
    return {"status": "success", "optimizations": optimizations}


@app.get("/api/optimizations/{optimization_id}/results")
async def get_optimization_results(optimization_id: str, top: int = 50):
    """Get results for a specific optimization run."""
    opt_dir = os.path.join(OPTIMIZATIONS_DIR, optimization_id)
    
    # Prefer Parquet, fallback to CSV
    results_parquet = os.path.join(opt_dir, "optimization_results.parquet")
    results_csv = os.path.join(opt_dir, "optimization_results.csv")
    
    if os.path.exists(results_parquet):
        df = pd.read_parquet(results_parquet)
    elif os.path.exists(results_csv):
        df = pd.read_csv(results_csv)
    else:
        raise HTTPException(404, "Results not found")
    
    # Filter successful runs and sort
    ok_df = df[df["status"] == "OK"].copy()
    if "composite_score" in ok_df.columns:
        ok_df["composite_score"] = pd.to_numeric(ok_df["composite_score"], errors="coerce")
        ok_df = ok_df.sort_values("composite_score", ascending=False)
    
    # Get top results
    top_results = ok_df.head(top).replace({np.nan: None, np.inf: None, -np.inf: None})
    all_results = df.replace({np.nan: None, np.inf: None, -np.inf: None})
    
    # Load config
    config = {}
    config_path = os.path.join(opt_dir, "optimization_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
    
    return {
        "status": "success",
        "optimization_id": optimization_id,
        "config": config,
        "total_results": len(df),
        "successful": len(ok_df),
        "failed": len(df) - len(ok_df),
        "top_results": top_results.to_dict(orient="records"),
        "all_results": all_results.to_dict(orient="records"),
        "columns": list(df.columns),
    }


@app.get("/api/optimizations/{optimization_id}/batch/{batch_id}")
async def get_batch_details(optimization_id: str, batch_id: str):
    """Get detailed results for a specific batch."""
    batch_dir = os.path.join(OPTIMIZATIONS_DIR, optimization_id, "runs", batch_id)
    
    if not os.path.exists(batch_dir):
        raise HTTPException(404, f"Batch {batch_id} not found")
    
    result = {}
    
    # Load params
    params_path = os.path.join(batch_dir, "params.json")
    if os.path.exists(params_path):
        with open(params_path) as f:
            result["params"] = json.load(f)
    
    # Load metrics
    metrics_path = os.path.join(batch_dir, "metrics.json")
    if os.path.exists(metrics_path):
        with open(metrics_path) as f:
            result["metrics"] = json.load(f)
    
    # Check for trade files
    trade_files = glob.glob(os.path.join(batch_dir, "*trade*.csv")) + \
                  glob.glob(os.path.join(batch_dir, "*python_trade*.csv"))
    result["has_trades"] = len(trade_files) > 0
    result["trade_files"] = [os.path.basename(f) for f in trade_files]
    
    # Check for xlsx
    xlsx_files = glob.glob(os.path.join(batch_dir, "*.xlsx"))
    result["has_report"] = len(xlsx_files) > 0
    result["report_files"] = [os.path.basename(f) for f in xlsx_files]

    # The parent run's settings — mode, workers, seed, ranking, swept ranges.
    # Included so "load this batch back into the optimizer" needs one request
    # instead of also pulling the full results table just to read the config.
    result["config"] = _read_json(
        os.path.join(_opt_dir(optimization_id), "optimization_config.json")
    ) or {}

    return {"status": "success", "batch_id": batch_id, **result}


@app.get("/api/optimizations/{optimization_id}/config")
async def get_optimization_config(optimization_id: str):
    """
    Just the run's saved configuration.

    The results endpoint also carries the config, but it loads and returns
    every result row with it — far too much work when all the caller wants is
    to restore the run's settings into the dashboard form.
    """
    opt_dir = _opt_dir(optimization_id)
    if not os.path.isdir(opt_dir):
        raise HTTPException(404, f"Optimization not found: {optimization_id}")

    config = _read_json(os.path.join(opt_dir, "optimization_config.json"))
    if not config:
        raise HTTPException(404, "This run has no saved configuration")

    return {"status": "success", "optimization_id": optimization_id, "config": config}


@app.delete("/api/optimizations/{optimization_id}")
async def delete_optimization(optimization_id: str):
    """Delete an optimization run and all its data."""
    opt_dir = os.path.join(OPTIMIZATIONS_DIR, optimization_id)
    if not os.path.exists(opt_dir):
        raise HTTPException(404, "Optimization not found")
    
    # Don't delete if running
    if _is_live(optimization_id):
        raise HTTPException(409, "Cannot delete a running optimization. Stop it first.")
    with _scheduler_lock:
        if _queued_index(optimization_id) >= 0:
            raise HTTPException(409, "Cannot delete a queued optimization. Cancel it first.")
    running_optimizations.pop(optimization_id, None)
    
    shutil.rmtree(opt_dir)
    return {"status": "deleted", "optimization_id": optimization_id}


# =============================================================================
# ROUTES — DATA CONVERSION
# =============================================================================
@app.post("/api/data/convert")
async def convert_data(req: ConvertRequest):
    """Convert a CSV file to Parquet format."""
    try:
        result = convert_csv_to_parquet(req.csv_path)
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(500, f"Conversion failed: {str(e)}")


# =============================================================================
# ROUTES — CHARTING
# =============================================================================
# =============================================================================
# USER DATA (GROUPS & SAVED BACKTESTS)
# =============================================================================
USER_DATA_FILE = os.path.join(OPTIMIZATIONS_DIR, "user_data.json")

def load_user_data():
    if not os.path.exists(USER_DATA_FILE):
        return {"groups": [], "favorites": {}}
    try:
        with open(USER_DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"groups": [], "favorites": {}}

def save_user_data(data):
    with open(USER_DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

class SaveFavoriteRequest(BaseModel):
    opt_id: str
    batch_id: str
    group: str
    review: str
    metrics: dict
    timestamp: str
    # The batch's full parameter set, stored alongside the review so a saved
    # entry still describes what it ran even if the run folder is deleted.
    # Optional, so anything posting the older payload keeps working.
    params: dict = {}

@app.get("/api/user-data")
async def get_user_data():
    return load_user_data()

@app.post("/api/user-data/save")
async def save_favorite(req: SaveFavoriteRequest):
    data = load_user_data()
    
    # Add group if it doesn't exist
    if req.group and req.group not in data["groups"]:
        data["groups"].append(req.group)
        
    key = f"{req.opt_id}/{req.batch_id}"
    
    existing = data["favorites"].get(key, {})
    metrics = req.metrics if req.metrics else existing.get("metrics", {})
    timestamp = req.timestamp if req.timestamp else existing.get("timestamp", "")
    # Editing a review must never wipe parameters captured on an earlier save.
    params = req.params if req.params else existing.get("params", {})

    data["favorites"][key] = {
        "opt_id": req.opt_id,
        "batch_id": req.batch_id,
        "group": req.group,
        "review": req.review,
        "metrics": metrics,
        "timestamp": timestamp,
        "params": params,
    }
    
    save_user_data(data)
    return {"status": "success"}

class AddGroupRequest(BaseModel):
    group: str

@app.post("/api/user-data/group")
async def add_group(req: AddGroupRequest):
    data = load_user_data()
    if req.group and req.group not in data["groups"]:
        data["groups"].append(req.group)
        save_user_data(data)
    return {"status": "success"}

class DeleteGroupRequest(BaseModel):
    group: str

@app.post("/api/user-data/group/delete")
async def delete_group(req: DeleteGroupRequest):
    data = load_user_data()
    if req.group in data["groups"]:
        data["groups"].remove(req.group)
        keys_to_delete = [k for k, v in data["favorites"].items() if v.get("group") == req.group]
        for k in keys_to_delete:
            del data["favorites"][k]
        save_user_data(data)
    return {"status": "success"}

class DeleteFavoriteRequest(BaseModel):
    opt_id: str
    batch_id: str

@app.post("/api/user-data/delete")
async def delete_favorite(req: DeleteFavoriteRequest):
    data = load_user_data()
    key = f"{req.opt_id}/{req.batch_id}"
    if key in data["favorites"]:
        del data["favorites"][key]
        save_user_data(data)
    return {"status": "success"}

@app.get("/api/chart/trades/{optimization_id}/{batch_id}")
async def get_trade_data(optimization_id: str, batch_id: str):
    """Get trade data for chart visualization."""
    batch_dir = os.path.join(OPTIMIZATIONS_DIR, optimization_id, "runs", batch_id)
    
    if not os.path.exists(batch_dir):
        raise HTTPException(404, "Batch not found")
    
    # Find trade CSV
    trade_files = glob.glob(os.path.join(batch_dir, "*trade*.csv")) + \
                  glob.glob(os.path.join(batch_dir, "*python_trade*.csv"))
    
    if not trade_files:
        return {"status": "success", "trades": [], "markers": []}
    
    df = pd.read_csv(trade_files[0])
    
    # Build trade markers for TradingView Lightweight Charts
    markers = []
    trades = []
    
    # Detect column names
    entry_ts_col = None
    exit_ts_col = None
    entry_price_col = None
    exit_price_col = None
    direction_col = None
    pnl_col = None
    
    for col in df.columns:
        cl = col.lower()
        if 'entry' in cl and ('time' in cl or 'stamp' in cl or 'date' in cl):
            entry_ts_col = col
        if 'exit' in cl and ('time' in cl or 'stamp' in cl or 'date' in cl):
            exit_ts_col = col
        if 'entry' in cl and 'price' in cl:
            entry_price_col = col
        if 'exit' in cl and 'price' in cl:
            exit_price_col = col
        if cl in ('direction', 'side', 'type', 'signal'):
            direction_col = col
        if cl in ('net_pnl', 'pnl', 'profit', 'net pnl'):
            pnl_col = col
    
    if entry_ts_col and entry_price_col:
        for _, row in df.iterrows():
            try:
                entry_time = pd.to_datetime(row[entry_ts_col])
                entry_unix = int(entry_time.timestamp())
                entry_price = float(row[entry_price_col])
                
                direction = str(row.get(direction_col, "LONG")).upper() if direction_col else "LONG"
                is_long = direction in ("LONG", "BUY", "1", "L")
                
                pnl_val = float(row[pnl_col]) if pnl_col and pd.notna(row.get(pnl_col)) else 0
                
                # Entry marker
                markers.append({
                    "time": entry_unix,
                    "position": "belowBar" if is_long else "aboveBar",
                    "color": "#089981" if is_long else "#f23645",
                    "shape": "arrowUp" if is_long else "arrowDown",
                    "text": f"{'BUY' if is_long else 'SELL'} @ {entry_price:.2f}"
                })
                
                # Exit marker
                if exit_ts_col and exit_price_col:
                    exit_time = pd.to_datetime(row[exit_ts_col])
                    exit_unix = int(exit_time.timestamp())
                    exit_price = float(row[exit_price_col])
                    
                    win = pnl_val > 0
                    markers.append({
                        "time": exit_unix,
                        "position": "aboveBar" if is_long else "belowBar",
                        "color": "#089981" if win else "#f23645",
                        "shape": "circle",
                        "text": f"EXIT @ {exit_price:.2f} ({'+' if pnl_val >= 0 else ''}{pnl_val:.2f})"
                    })
                
                trade_record = {
                    "entry_time": entry_unix,
                    "entry_price": entry_price,
                    "direction": "LONG" if is_long else "SHORT",
                    "pnl": pnl_val,
                }
                if exit_ts_col and exit_price_col:
                    trade_record["exit_time"] = exit_unix
                    trade_record["exit_price"] = exit_price
                
                trades.append(trade_record)
            except Exception:
                continue
    
    return {
        "status": "success",
        "total_trades": len(trades),
        "trades": trades,
        "markers": markers,
    }


@app.get("/api/download/{optimization_id}/{batch_id}/excel")
async def download_batch_excel(optimization_id: str, batch_id: str):
    """Download the Excel report for a specific batch."""
    batch_dir = os.path.join(OPTIMIZATIONS_DIR, optimization_id, "runs", batch_id)
    if not os.path.exists(batch_dir):
        raise HTTPException(404, "Batch directory not found")
        
    xlsx_files = glob.glob(os.path.join(batch_dir, "*.xlsx"))
    if not xlsx_files:
        raise HTTPException(404, "No Excel report found for this batch")
        
    xlsx_path = max(xlsx_files, key=os.path.getsize)
    filename = os.path.basename(xlsx_path)
    
    return FileResponse(
        path=xlsx_path, 
        filename=filename, 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.get("/api/excel-viewer/{optimization_id}/{batch_id}")
async def view_batch_excel(optimization_id: str, batch_id: str):
    """Return Excel sheets as HTML tables for viewing in the dashboard."""
    batch_dir = os.path.join(OPTIMIZATIONS_DIR, optimization_id, "runs", batch_id)
    if not os.path.exists(batch_dir):
        raise HTTPException(404, "Batch directory not found")
        
    xlsx_files = glob.glob(os.path.join(batch_dir, "*.xlsx"))
    if not xlsx_files:
        raise HTTPException(404, "No Excel report found for this batch")
        
    xlsx_path = max(xlsx_files, key=os.path.getsize)
    
    try:
        # Read all sheets
        sheets_dict = pd.read_excel(xlsx_path, sheet_name=None)
        html_sheets = {}
        for sheet_name, df in sheets_dict.items():
            # Limit to 1000 rows to prevent browser crash on massive backtests
            if len(df) > 1000:
                df = df.head(1000)
                # Add a row to indicate truncation
                df.loc[len(df)] = ["..."] * len(df.columns)
            html_sheets[sheet_name] = df.to_html(classes="excel-table", index=False, escape=False)
        return {"sheets": html_sheets}
    except Exception as e:
        raise HTTPException(500, f"Failed to parse Excel: {str(e)}")

@app.get("/api/chart/ohlc")
async def get_ohlc_data(
    data_path: str = "",
    timeframe: str = "15m",
    start_date: str = None,
    end_date: str = None,
):
    """Get OHLC candle data for chart visualization."""
    if not data_path or not os.path.exists(data_path):
        raise HTTPException(404, f"Data file not found: {data_path}")
    
    try:
        # Load data (prefer parquet)
        parquet_path = os.path.splitext(data_path)[0] + ".parquet"
        if os.path.exists(parquet_path):
            df = pd.read_parquet(parquet_path)
        elif data_path.endswith(".parquet"):
            df = pd.read_parquet(data_path)
        else:
            df = pd.read_csv(data_path, sep=None, engine='python', low_memory=False)
        
        # Ensure datetime index
        if not isinstance(df.index, pd.DatetimeIndex):
            # Try common datetime column patterns
            for col in ['datetime', 'Date', 'Timestamp', 'time']:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], errors='coerce')
                    df.set_index(col, inplace=True)
                    break
            
            # MT5 format
            if '<DATE>' in df.columns and '<TIME>' in df.columns:
                df['datetime'] = pd.to_datetime(df['<DATE>'].astype(str) + ' ' + df['<TIME>'].astype(str))
                df.set_index('datetime', inplace=True)
        
        # Rename columns
        rename_map = {
            '<BID>': 'Bid', '<ASK>': 'Ask', '<VOLUME>': 'Volume',
            '<OPEN>': 'Open', '<HIGH>': 'High', '<LOW>': 'Low', '<CLOSE>': 'Close',
            'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close',
        }
        df.rename(columns=rename_map, inplace=True)
        
        # If tick data (has Bid column), resample to OHLC
        if 'Bid' in df.columns:
            tf_map = {
                '1m': '1min', '5m': '5min', '15m': '15min', '30m': '30min',
                '1h': '1h', '4h': '4h', '1D': '1d',
            }
            pandas_tf = tf_map.get(timeframe, '15min')
            ohlc = df['Bid'].resample(pandas_tf).ohlc()
            ohlc['volume'] = df['Bid'].resample(pandas_tf).count()
            ohlc.dropna(subset=['open'], how='all', inplace=True)
            ohlc.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
            df = ohlc
        elif 'Open' in df.columns:
            # Already OHLC, resample if needed
            pass
        
        # Filter by date range
        if start_date:
            df = df[df.index >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df.index <= pd.to_datetime(end_date)]
        
        # Format for TradingView Lightweight Charts
        records = []
        timestamps = df.index.astype('datetime64[s]').astype('int64').tolist()
        for i in range(len(df)):
            try:
                records.append({
                    "time": int(timestamps[i]),
                    "open": round(float(df.iloc[i].get("Open", 0)), 3),
                    "high": round(float(df.iloc[i].get("High", 0)), 3),
                    "low": round(float(df.iloc[i].get("Low", 0)), 3),
                    "close": round(float(df.iloc[i].get("Close", 0)), 3),
                    "volume": int(df.iloc[i].get("Volume", 0)) if "Volume" in df.columns else 0,
                })
            except Exception:
                continue
        
        return {
            "status": "success",
            "timeframe": timeframe,
            "count": len(records),
            "data": records,
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to load OHLC data: {str(e)}")


# =============================================================================
# ROUTES — DATA RANGE DETECTION
# =============================================================================
@app.get("/api/data/range")
async def detect_data_range_endpoint(data_path: str = ""):
    """Detect the available date range from a dataset file."""
    if not data_path or not os.path.exists(data_path):
        raise HTTPException(404, f"Data file not found: {data_path}")

    try:
        from walk_forward.window_generator import detect_data_range
        start, end = detect_data_range(data_path)
        return {"status": "success", "start": start, "end": end, "data_path": data_path}
    except Exception as e:
        raise HTTPException(500, f"Failed to detect data range: {str(e)}")


# =============================================================================
# ROUTES — WALK-FORWARD TESTING
# =============================================================================
from walk_forward.models import WFOConfig, SelectionConfig, SelectionRule, RobustnessWeights
from walk_forward.window_generator import generate_windows, validate_windows, detect_data_range, calculate_max_steps
from walk_forward.runner import WalkForwardRunner
from walk_forward import persistence as wfo_persistence

# Track running WFO instances
running_wfo = {}  # run_id -> {"thread": Thread, "runner": WalkForwardRunner}


class WFOCreateRequest(BaseModel):
    """Request to create/preview a WFO run."""
    strategy_path: str
    strategy_name: str = ""
    entry_style: str = "config_class"
    config_class_name: Optional[str] = None
    data_path: str = ""
    timeframe: str = "15min"
    wfo_start: str = ""
    wfo_end: str = ""
    window_mode: str = "rolling"
    is_duration_months: int = 24
    oos_duration_months: int = 6
    step_duration_months: int = 6
    num_steps: Optional[int] = None
    optimization_method: str = "random"
    optimization_iterations: int = 1000
    num_workers: int = 2
    seed: int = 42
    ranking_metric: str = "composite"
    fixed_params: dict = {}
    optimize_params: dict = {}
    date_param_style: str = "flat"
    date_param_name: str = ""
    selection_metric: str = "composite_score"
    selection_direction: str = "max"
    selection_rules: list = []
    robustness_weights: dict = {}
    drawdown_optimization: str = "disabled"
    dd_min_trades_per_day: float = 2.0
    dd_target_trades_per_day: float = 8.0


@app.get("/api/walk-forward/runs")
async def list_wfo_runs():
    """List all Walk-Forward Testing runs."""
    runs = wfo_persistence.list_wfo_runs()
    return {"status": "success", "runs": runs}


@app.post("/api/walk-forward/create")
async def create_wfo(req: WFOCreateRequest):
    """Validate WFO config, generate window preview, return for user review."""
    if not os.path.exists(req.strategy_path):
        raise HTTPException(404, f"Strategy not found: {req.strategy_path}")

    try:
        # Auto-detect data range if a data path is available
        dataset_start = ""
        dataset_end = ""
        if req.data_path and os.path.exists(req.data_path):
            try:
                dataset_start, dataset_end = detect_data_range(req.data_path)
            except Exception:
                pass

        # Build selection config
        rules = []
        for r in req.selection_rules:
            rules.append(SelectionRule(
                metric=r.get("metric", ""),
                direction=r.get("direction", "max"),
                threshold=r.get("threshold"),
                enabled=r.get("enabled", True),
            ))
        selection = SelectionConfig(
            primary_metric=req.selection_metric,
            primary_direction=req.selection_direction,
            rules=rules if rules else SelectionConfig().rules,
        )

        # Build robustness weights
        weights = RobustnessWeights.from_dict(req.robustness_weights) if req.robustness_weights else RobustnessWeights()

        # Build WFO config
        config = WFOConfig(
            strategy_path=req.strategy_path,
            strategy_name=req.strategy_name or os.path.basename(req.strategy_path),
            entry_style=req.entry_style,
            config_class_name=req.config_class_name,
            data_path=req.data_path,
            timeframe=req.timeframe,
            dataset_start=dataset_start,
            dataset_end=dataset_end,
            wfo_start=req.wfo_start,
            wfo_end=req.wfo_end,
            window_mode=req.window_mode,
            is_duration_months=req.is_duration_months,
            oos_duration_months=req.oos_duration_months,
            step_duration_months=req.step_duration_months,
            num_steps=req.num_steps,
            optimization_method=req.optimization_method,
            optimization_iterations=req.optimization_iterations,
            num_workers=req.num_workers,
            seed=req.seed,
            ranking_metric=req.ranking_metric,
            fixed_params=req.fixed_params,
            optimize_params=req.optimize_params,
            date_param_style=req.date_param_style,
            date_param_name=req.date_param_name,
            selection=selection,
            robustness_weights=weights,
            drawdown_optimization=req.drawdown_optimization,
            dd_min_trades_per_day=req.dd_min_trades_per_day,
            dd_target_trades_per_day=req.dd_target_trades_per_day,
        )

        # Generate windows
        windows = generate_windows(config)
        max_steps = len(windows)

        # Validate
        ds_start = dataset_start or req.wfo_start
        ds_end = dataset_end or req.wfo_end
        errors = validate_windows(windows, ds_start, ds_end)

        return {
            "status": "success",
            "windows": [w.to_dict() for w in windows],
            "total_windows": len(windows),
            "max_steps": max_steps,
            "dataset_start": dataset_start,
            "dataset_end": dataset_end,
            "validation_errors": errors,
            "config": config.to_dict(),
        }
    except Exception as e:
        raise HTTPException(500, f"WFO creation failed: {str(e)}")


@app.post("/api/walk-forward/{run_id}/start")
async def start_wfo(run_id: str, req: WFOCreateRequest):
    """Start a Walk-Forward Testing run in the background."""
    if run_id in running_wfo:
        raise HTTPException(409, "WFO run already in progress")

    try:
        # Build full config (same logic as create)
        dataset_start = ""
        dataset_end = ""
        if req.data_path and os.path.exists(req.data_path):
            try:
                dataset_start, dataset_end = detect_data_range(req.data_path)
            except Exception:
                pass

        rules = []
        for r in req.selection_rules:
            rules.append(SelectionRule(
                metric=r.get("metric", ""),
                direction=r.get("direction", "max"),
                threshold=r.get("threshold"),
                enabled=r.get("enabled", True),
            ))
        selection = SelectionConfig(
            primary_metric=req.selection_metric,
            primary_direction=req.selection_direction,
            rules=rules if rules else SelectionConfig().rules,
        )
        weights = RobustnessWeights.from_dict(req.robustness_weights) if req.robustness_weights else RobustnessWeights()

        config = WFOConfig(
            strategy_path=req.strategy_path,
            strategy_name=req.strategy_name or os.path.basename(req.strategy_path),
            entry_style=req.entry_style,
            config_class_name=req.config_class_name,
            data_path=req.data_path,
            timeframe=req.timeframe,
            dataset_start=dataset_start,
            dataset_end=dataset_end,
            wfo_start=req.wfo_start,
            wfo_end=req.wfo_end,
            window_mode=req.window_mode,
            is_duration_months=req.is_duration_months,
            oos_duration_months=req.oos_duration_months,
            step_duration_months=req.step_duration_months,
            num_steps=req.num_steps,
            optimization_method=req.optimization_method,
            optimization_iterations=req.optimization_iterations,
            num_workers=req.num_workers,
            seed=req.seed,
            ranking_metric=req.ranking_metric,
            fixed_params=req.fixed_params,
            optimize_params=req.optimize_params,
            date_param_style=req.date_param_style,
            date_param_name=req.date_param_name,
            selection=selection,
            robustness_weights=weights,
            drawdown_optimization=req.drawdown_optimization,
            dd_min_trades_per_day=req.dd_min_trades_per_day,
            dd_target_trades_per_day=req.dd_target_trades_per_day,
            run_id=run_id,
            created_at=datetime.now().isoformat(),
        )

        runner = WalkForwardRunner(config)

        def run_in_thread():
            try:
                runner.run()
            except Exception as e:
                wfo_persistence.save_wfo_progress(run_id, {
                    "status": "error",
                    "error": str(e),
                })
            finally:
                running_wfo.pop(run_id, None)

        thread = threading.Thread(target=run_in_thread, daemon=True)
        running_wfo[run_id] = {"thread": thread, "runner": runner}
        thread.start()

        return {
            "status": "started",
            "run_id": run_id,
            "message": f"Walk-Forward started with {config.optimization_iterations} iterations per step",
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to start WFO: {str(e)}")


@app.post("/api/walk-forward/{run_id}/stop")
async def stop_wfo(run_id: str):
    """Stop a running Walk-Forward test."""
    if run_id in running_wfo:
        running_wfo[run_id]["runner"].stop()
        return {"status": "stopping", "run_id": run_id}
    raise HTTPException(404, "WFO run not found or already completed")


@app.get("/api/walk-forward/{run_id}/progress")
async def stream_wfo_progress(run_id: str):
    """SSE stream of live WFO progress."""
    async def event_generator():
        last_data = ""
        last_sent = time.monotonic()
        while True:
            progress = wfo_persistence.load_wfo_progress(run_id)
            if progress:
                data = json.dumps(progress)
                if data != last_data:
                    last_data = data
                    yield f"data: {data}\n\n"
                    last_sent = time.monotonic()

                    status = progress.get("status", "")
                    if status in ("completed", "error", "stopped"):
                        yield f"data: {json.dumps({'status': 'done'})}\n\n"
                        return

            # A walk-forward step is a whole optimization, so this stream can be
            # silent for far longer than the optimizer's. Same keepalive.
            if time.monotonic() - last_sent >= SSE_KEEPALIVE_SECONDS:
                yield ": keepalive\n\n"
                last_sent = time.monotonic()

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/walk-forward/{run_id}")
async def get_wfo_run(run_id: str):
    """Get WFO run status and config."""
    config = wfo_persistence.load_wfo_config(run_id)
    progress = wfo_persistence.load_wfo_progress(run_id)
    aggregate = wfo_persistence.load_aggregate_results(run_id)

    if config is None and progress is None:
        raise HTTPException(404, f"WFO run not found: {run_id}")

    return {
        "status": "success",
        "run_id": run_id,
        "config": config.to_dict() if config else None,
        "progress": progress,
        "aggregate": aggregate,
    }


@app.get("/api/walk-forward/{run_id}/windows")
async def get_wfo_windows(run_id: str):
    """Get window definitions for a WFO run."""
    config = wfo_persistence.load_wfo_config(run_id)
    if config is None:
        raise HTTPException(404, "WFO run not found")

    windows = generate_windows(config)
    return {
        "status": "success",
        "windows": [w.to_dict() for w in windows],
    }


@app.get("/api/walk-forward/{run_id}/steps")
async def get_wfo_steps(run_id: str):
    """Get all step results for a WFO run."""
    aggregate = wfo_persistence.load_aggregate_results(run_id)
    if aggregate is None:
        # Try to build from individual step files
        config = wfo_persistence.load_wfo_config(run_id)
        if config is None:
            raise HTTPException(404, "WFO run not found")

        windows = generate_windows(config)
        steps = []
        for w in windows:
            state = wfo_persistence.load_step_state(run_id, w.step)
            params = wfo_persistence.load_selected_params(run_id, w.step)
            oos = wfo_persistence.load_oos_metrics(run_id, w.step)
            steps.append({
                "step": w.step,
                "state": state,
                "is_start": w.is_start,
                "is_end": w.is_end,
                "oos_start": w.oos_start,
                "oos_end": w.oos_end,
                "selected_params": params,
                "oos_metrics": oos,
            })
        return {"status": "success", "steps": steps}

    return {
        "status": "success",
        "steps": aggregate.get("step_results", []),
    }


@app.get("/api/walk-forward/{run_id}/results")
async def get_wfo_results(run_id: str):
    """Get aggregated OOS results for a WFO run."""
    aggregate = wfo_persistence.load_aggregate_results(run_id)
    if aggregate is None:
        raise HTTPException(404, "WFO results not found — run may still be in progress")

    return {
        "status": "success",
        "run_id": run_id,
        **aggregate,
    }


@app.get("/api/walk-forward/{run_id}/parameters")
async def get_wfo_parameters(run_id: str):
    """Get parameter stability analysis for a WFO run."""
    aggregate = wfo_persistence.load_aggregate_results(run_id)
    if aggregate is None:
        raise HTTPException(404, "WFO results not found")

    return {
        "status": "success",
        "stability": aggregate.get("stability", []),
    }


@app.get("/api/walk-forward/{run_id}/candidates")
async def get_wfo_candidates(run_id: str):
    """Get candidate parameter sets for a WFO run."""
    aggregate = wfo_persistence.load_aggregate_results(run_id)
    if aggregate is None:
        raise HTTPException(404, "WFO results not found")

    return {
        "status": "success",
        "candidates": aggregate.get("candidates", []),
    }


class FullSampleRequest(BaseModel):
    candidate_params: dict
    candidate_label: str = ""


@app.post("/api/walk-forward/{run_id}/full-sample")
async def run_full_sample(run_id: str, req: FullSampleRequest):
    """
    Run a full-sample backtest for comparison analysis.
    Clearly labeled as FULL-SAMPLE ANALYSIS — NOT OOS VALIDATION.
    """
    config = wfo_persistence.load_wfo_config(run_id)
    if config is None:
        raise HTTPException(404, "WFO run not found")

    try:
        from optimizer_engine import run_single_backtest

        # Build full-sample params
        params = dict(config.fixed_params)
        # Override with full dataset date range
        if config.date_param_style == "nested" and config.date_param_name:
            key = config.date_param_name
            if key in params and isinstance(params[key], dict):
                params[key] = dict(params[key])
                params[key]["start_date"] = config.dataset_start or config.wfo_start
                params[key]["end_date"] = config.dataset_end or config.wfo_end
            else:
                params[key] = {
                    "start_date": config.dataset_start or config.wfo_start,
                    "end_date": config.dataset_end or config.wfo_end,
                }
        else:
            params["start_date"] = config.dataset_start or config.wfo_start
            params["end_date"] = config.dataset_end or config.wfo_end

        # Merge candidate params
        params.update(req.candidate_params)

        # Run in a temp directory
        wfo_dir = wfo_persistence.get_wfo_dir(run_id)
        fs_dir = os.path.join(wfo_dir, "full_sample",
                              req.candidate_label.replace(" ", "_") or "candidate")
        os.makedirs(fs_dir, exist_ok=True)

        result = run_single_backtest(
            fs_dir, config.strategy_path, params,
            config.entry_style, config.config_class_name,
        )

        return {
            "status": "success",
            "label": "FULL-SAMPLE ANALYSIS",
            "warning": "This is NOT OOS validation. Do not use these results to validate the strategy.",
            "candidate_label": req.candidate_label,
            "metrics": result.get("metrics", {}),
            "result_status": result.get("status", {}),
        }
    except Exception as e:
        raise HTTPException(500, f"Full-sample backtest failed: {str(e)}")



@app.delete("/api/walk-forward/{run_id}")
async def delete_wfo_run(run_id: str):
    """Delete a WFO run and all its data."""
    if run_id in running_wfo:
        raise HTTPException(409, "Cannot delete a running WFO. Stop it first.")

    if wfo_persistence.delete_wfo_run(run_id):
        return {"status": "deleted", "run_id": run_id}
    raise HTTPException(404, "WFO run not found")


@app.get("/api/walk-forward/{run_id}/export/{export_type}")
async def export_wfo_data(run_id: str, export_type: str):
    """
    Export WFO data in various formats.
    
    Types: config, windows, steps, stability, candidates, summary
    """
    config = wfo_persistence.load_wfo_config(run_id)
    if config is None:
        raise HTTPException(404, "WFO run not found")

    aggregate = wfo_persistence.load_aggregate_results(run_id)

    if export_type == "config":
        return config.to_dict()
    elif export_type == "windows":
        windows = generate_windows(config)
        return {"windows": [w.to_dict() for w in windows]}
    elif export_type == "steps":
        if aggregate:
            return {"steps": aggregate.get("step_results", [])}
        return {"steps": []}
    elif export_type == "stability":
        if aggregate:
            return {"stability": aggregate.get("stability", [])}
        return {"stability": []}
    elif export_type == "candidates":
        if aggregate:
            return {"candidates": aggregate.get("candidates", [])}
        return {"candidates": []}
    elif export_type == "summary":
        if aggregate:
            return aggregate.get("summary", {})
        return {}
    else:
        raise HTTPException(400, f"Unknown export type: {export_type}")


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    print("=" * 65)
    print("  Universal Python Algo Optimizer — Dashboard Server")
    print("=" * 65)
    print(f"  Backtests Dir  : {BACKTESTS_DIR}")
    print(f"  Optimizations  : {OPTIMIZATIONS_DIR}")
    print(f"  WFO Runs       : {wfo_persistence.WFO_RUNS_DIR}")
    print(f"  Static Files   : {STATIC_DIR}")
    print("=" * 65)
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)


# static/

The dashboard frontend, served by [`server.py`](../server.py) at
**http://localhost:8000**.

Three files, no build step, no framework, no package manager:

| File | Size | What it is |
|---|---|---|
| `index.html` | ~43 KB | Full page markup — sidebar, tabs, modals |
| `app.js` | ~206 KB | All behaviour, in one IIFE on `DOMContentLoaded` |
| `style.css` | ~61 KB | Dark theme, CSS custom properties |

Edit a file, reload the browser. That's the whole workflow.

External dependencies are CDN `<script>` tags in `index.html`: **TradingView
Lightweight Charts** for the chart tab and **Font Awesome** for icons.

---

## Layout

```
┌──────────────────────────────────────────────────────────────┐
│ Header: nav buttons (History · Chart · Results · Saved · WFO) │
├───────────────┬──────────────────────────────────────────────┤
│ Sidebar       │ Tab bar: Progress | Results | Chart View      │
│               │                                              │
│ Script select │ ┌──────────────────────────────────────────┐ │
│ Analyze       │ │                                          │ │
│ ───────────── │ │  Active tab content                      │ │
│ Parameters    │ │                                          │ │
│  (fixed /     │ │                                          │ │
│   optimizable)│ │                                          │ │
│ ───────────── │ │                                          │ │
│ Mode, iters,  │ │                                          │ │
│ workers, seed │ │                                          │ │
│ Ranking       │ └──────────────────────────────────────────┘ │
│ Drawdown opt  │                                              │
│ ───────────── │                                              │
│ Start / Stop  │                                              │
└───────────────┴──────────────────────────────────────────────┘
```

Tabs are plain `data-tab` attributes toggled by `switchTab()`; the header nav
buttons switch to the same tabs or open the History modal.

---

## `app.js` structure

The whole file is one `DOMContentLoaded` handler. Sections, in order:

| Section | Responsibility |
|---|---|
| **State** | `analysisResult`, `currentOptId`, `progressSSE`, `currentResults`, chart handles, `savedUserData` |
| **DOM elements** | Every `getElementById` up front, as `const`s |
| **Initialization** | `loadScripts()`, `fetchUserData()`, tab/nav wiring, `reattachToActiveOptimization()` |
| **Navigation** | Header buttons and tab switching |
| **Scripts** | Populate the dropdown from `/api/scripts`; `POST /api/scripts/analyze` on Analyze |
| **Parameter rendering** | Build the form from the analysis result, split fixed/optimizable |
| **Search space estimation** | Live combination count as you edit ranges |
| **Drawdown mode toggle** | Show/hide the auto vs manual threshold inputs |
| **Optimization control** | Collect the form into a payload, `POST /api/optimize/start`, Stop |
| **Progress streaming (SSE)** | The live progress panel — see below |
| **Results** | Sortable table, batch detail modal, chart hand-off |
| **History** | Modal listing every run with Resume / View / Delete / Save |
| **Charting** | Lightweight Charts candles plus trade markers |
| **Utilities** | `formatValue`, `formatDuration`, `escapeHtml`, `setGlobalStatus`, `showToast` |
| **Excel viewer** | Renders a batch's `.xlsx` sheets as HTML tables |
| **Load & edit** | Reload a past run's parameters back into the sidebar form |
| **Saved backtests** | Star/group/annotate runs, persisted via `/api/user-data` |
| **Inline SVG charts** | `sparkline()`, `bandChart()`, `histogramChart()` — dependency-free plots for the report and Monte Carlo. Lightweight Charts is loaded for OHLC candles and is the wrong tool for a static distribution plot |
| **Walk-forward** | `class WalkForwardManager` — its own tab, wizard, SSE stream, and the detailed report panel (`loadReport` / `renderReport`) |
| **Monte Carlo** | `class MonteCarloManager` — source picker, method selection, run, results, history. Entered from its own nav button, from a batch modal (`fromBatch`), or from a walk-forward run (`fromWfoRun`) |

Functions the HTML calls via inline `onclick` are attached to `window`
(`window.viewExcel`, `window.openSaveModal`, `window.loadOptimization`,
`window.resumeOptimization`, `window.renderSavedBacktests`).

---

## Progress streaming

The progress panel is driven by Server-Sent Events from
`GET /api/optimize/progress/{id}`.

```
startProgressStream(optId)
   ├── ensureProgressPanel()      build/reset the panel markup
   ├── new EventSource(...)
   ├── onmessage → applyProgress(payload)
   │      └── {status: "done"} → onOptimizationComplete() or onOptimizationHalted()
   └── onerror   → ask /api/optimize/status/{id} before concluding anything
```

**`applyProgress(p)`** paints one payload. It prefers the server's `percent`
field (which counts a batch as done only once it has produced a result) and
falls back to `completed / total` for older payloads. It clamps to 0–100 and
renders one decimal place, which is legible at 1000 batches.

**`onerror` does not mean finished.** `EventSource` retries on its own, so the
handler asks the server for the run's real status and only finishes if the server
agrees the run is over. A dropped connection used to be reported as "Optimization
completed!" while batches were still running.

**`reattachToActiveOptimization()`** runs at page load. It asks
`GET /api/optimize/active` and either reattaches to the live stream or, for an
interrupted run, renders a Resume banner. Without it, reloading the page during a
run left the Progress tab empty with no way back into it.

Panel element IDs, if you're restyling: `pCompleted`, `pTotal`, `pFailed`,
`pPending`, `pBar`, `pPercent`, `pETA`, `pBanner`.

---

## `style.css`

Dark theme built on CSS custom properties defined on `:root` — `--bg-primary`,
`--bg-card`, `--bg-input`, `--text-heading`, `--text-body`, `--text-muted`,
`--border-color`, `--accent-blue`, `--accent-purple`, `--green`, `--red`,
`--yellow` and their `-bg` / `-border` variants. Retheme by editing those.

Sections mirror the markup: Reset & Base, Scrollbar, Layout, Header, Form
Elements, Buttons, Parameter Cards, Main Content, Progress Panel, Results Table,
Optimizations List, Chart Container, Modal, Batch Detail Modal, Empty State,
Toasts, Spinner, Utilities, Excel Viewer, Walk-Forward.

Run-status pills are `.opt-card-status.<status>` — `completed`, `running`,
`error`, `stopped`, `stopping`, `interrupted`, `resuming`, `aggregating`. Adding
a new status means adding a matching rule here.

Fonts are Inter for text and JetBrains Mono for numbers.

---

## Conventions

- **No build step.** Don't introduce one without also changing how `server.py`
  serves this directory.
- **One `IIFE`.** Everything is scoped inside the `DOMContentLoaded` callback;
  only the `window.*` handlers above are global on purpose.
- **Escape interpolated text.** Table cells and history cards use
  `escapeHtml()`; keep doing that for anything derived from a filename, error
  message, or user note.
- **Toasts, not alerts.** `showToast(message, 'success' | 'error' | 'warning' |
  'info')`.
- **Status dot.** `setGlobalStatus('running' | 'idle', 'text')` drives the header
  indicator.

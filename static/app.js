/**
 * Universal Algo Optimizer — Dashboard Application
 * Handles: Script analysis, parameter configuration, optimization control,
 * results display, and TradingView Lightweight Charts integration.
 */

document.addEventListener('DOMContentLoaded', () => {
    // =========================================================================
    // STATE
    // =========================================================================
    let analysisResult = null;  // Current script analysis
    let currentOptId = null;    // Running optimization ID
    const progressStreams = new Map();  // optId -> EventSource, one per running run
    let runsPollTimer = null;   // picks up queue advances and runs started elsewhere
    let currentResults = null;  // Latest results data
    let lwChart = null;         // Lightweight Charts instance
    let candleSeries = null;    // Candlestick series
    let currentDataPath = null; // Data path for charting
    let sortCol = null;
    let sortAsc = false;
    let resultsView = 'top';    // 'top' | 'failed' — which results table is shown
    let selectedRunIds = [];    // runs currently shown in the Results tab
    // Runs this page has actually touched — started, resumed, watched, or
    // opened from History. Deliberately NOT every run on disk: the picker is
    // for the work in front of you, and it starts empty after a reload.
    // Everything older lives in the History tab.
    const sessionRuns = new Map();  // optId -> {id, script_name, completed, total, status, started_at}
    let savedUserData = { groups: [], favorites: {} }; // New state for Saved Backtests

    // =========================================================================
    // DOM ELEMENTS
    // =========================================================================
    const scriptSelect = document.getElementById('scriptSelect');
    const analyzeBtn = document.getElementById('analyzeBtn');
    const scriptInfo = document.getElementById('scriptInfo');
    const paramsSection = document.getElementById('paramsSection');
    const paramsList = document.getElementById('paramsList');
    const paramCount = document.getElementById('paramCount');
    const startOptBtn = document.getElementById('startOptBtn');
    const stopOptBtn = document.getElementById('stopOptBtn');
    const progressTab = document.getElementById('progressTab');
    const progressContent = document.getElementById('progressContent');
    const emptyProgress = document.getElementById('emptyProgress');
    const resultsTab = document.getElementById('resultsTab');
    const resultsContainer = document.getElementById('resultsContainer');
    const resultsBadge = document.getElementById('resultsBadge');
    const globalStatusDot = document.getElementById('globalStatusDot');
    const globalStatusText = document.getElementById('globalStatusText');
    const chartCanvas = document.getElementById('chartCanvas');
    const chartTfSelect = document.getElementById('chartTfSelect');
    const chartFitBtn = document.getElementById('chartFitBtn');
    const chartSymbolLabel = document.getElementById('chartSymbolLabel');
    const chartTimeframeLabel = document.getElementById('chartTimeframeLabel');
    const batchModal = document.getElementById('batchModal');
    const batchModalTitle = document.getElementById('batchModalTitle');
    const batchModalBody = document.getElementById('batchModalBody');
    const historyModal = document.getElementById('historyModal');
    const historyList = document.getElementById('historyList');

    // =========================================================================
    // INITIALIZATION
    // =========================================================================
    loadScripts();
    // A folder link needs the saved groups before it can open one.
    fetchUserData().then(applyDeepLink);
    window.addEventListener('hashchange', applyDeepLink);
    setupNavigation();
    setupTabs();
    setupSectionToggles();
    // refreshRuns starts the poll itself, but only if something is in flight.
    refreshRuns().then(showLastInterruptedRun);

    async function fetchUserData() {
        try {
            const res = await fetch('/api/user-data');
            if (res.ok) {
                savedUserData = await res.json();
            }
        } catch (e) {
            console.error('Failed to load user data');
        }
    }

    // =========================================================================
    // NAVIGATION
    // =========================================================================
    function setupNavigation() {
        document.querySelectorAll('.header-nav-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const view = btn.dataset.view;
                document.querySelectorAll('.header-nav-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');

                if (view === 'history') {
                    loadHistory();
                    historyModal.classList.add('active');
                } else if (view === 'chart') {
                    switchTab('chart');
                } else if (view === 'results') {
                    switchTab('results');
                } else if (view === 'saved') {
                    switchTab('saved');
                    if (window.renderSavedBacktests) window.renderSavedBacktests();
                } else if (view === 'walkforward') {
                    switchTab('walkforward');
                    if (window.wfoManager) window.wfoManager.loadHistory();
                } else {
                    switchTab('progress');
                }
            });
        });
    }

    function setupTabs() {
        document.querySelectorAll('.tab-item').forEach(tab => {
            tab.addEventListener('click', () => switchTab(tab.dataset.tab));
        });
    }

    function switchTab(tabName) {
        document.querySelectorAll('.tab-item').forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.dataset.tab === tabName));
        if (tabName === 'chart') {
            setTimeout(() => resizeChart(), 100);
        }
    }

    function setupSectionToggles() {
        document.querySelectorAll('.section-header').forEach(header => {
            header.addEventListener('click', () => {
                const section = header.dataset.section;
                const body = document.querySelector(`.section-body[data-section="${section}"]`);
                header.classList.toggle('collapsed');
                body.classList.toggle('collapsed');
            });
        });
    }

    // =========================================================================
    // SCRIPTS
    // =========================================================================
    async function loadScripts() {
        try {
            const res = await fetch('/api/scripts');
            const data = await res.json();
            scriptSelect.innerHTML = '<option value="">— Select a script —</option>';
            data.scripts.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.name;
                opt.textContent = `${s.name} (${s.size_kb} KB)`;
                opt.dataset.path = s.path;
                scriptSelect.appendChild(opt);
            });
        } catch (e) {
            showToast('Failed to load scripts', 'error');
        }
    }

    scriptSelect.addEventListener('change', () => {
        analyzeBtn.disabled = !scriptSelect.value;
        paramsSection.style.display = 'none';
        startOptBtn.disabled = true;
        analysisResult = null;
    });

    analyzeBtn.addEventListener('click', analyzeScript);

    async function analyzeScript() {
        const name = scriptSelect.value;
        if (!name) return;

        analyzeBtn.disabled = true;
        analyzeBtn.innerHTML = '<div class="spinner"></div> Analyzing...';

        try {
            const res = await fetch('/api/scripts/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ script_name: name })
            });
            const data = await res.json();

            if (data.status !== 'success') {
                throw new Error(data.detail || 'Analysis failed');
            }

            analysisResult = data;
            renderParameters(data.parameters);
            // Pass analysis to Walk-Forward manager
            if (window.wfoManager) window.wfoManager.updateFromAnalysis(data);
            scriptInfo.innerHTML = `
                <div style="margin-top:8px;">
                    <span style="color: var(--accent-blue);">Entry:</span> ${data.entry_point || 'auto'}
                    <span style="color: var(--text-muted); margin-left: 8px;">Style:</span> ${data.entry_style}
                    ${data.config_class_name ? `<br><span style="color: var(--accent-purple);">Config Class:</span> ${data.config_class_name}` : ''}
                </div>
            `;
            paramsSection.style.display = '';
            startOptBtn.disabled = false;
            showToast(`Detected ${data.parameters.length} parameters`, 'success');
        } catch (e) {
            showToast(`Analysis failed: ${e.message}`, 'error');
        } finally {
            analyzeBtn.disabled = false;
            analyzeBtn.innerHTML = '<i class="fa-solid fa-magnifying-glass-chart"></i> Analyze Script';
        }
    }

    // =========================================================================
    // PARAMETER RENDERING
    // =========================================================================
    function renderParameters(params) {
        paramsList.innerHTML = '';
        let count = 0;

        params.forEach((p, idx) => {
            count++;
            const isOptimizable = p.category === 'optimizable';
            const card = document.createElement('div');
            card.className = 'param-card';
            card.dataset.paramIdx = idx;
            card.dataset.paramName = p.name;

            let valueDisplay = '';
            const val = p.value;

            if (p.type === 'bool') {
                valueDisplay = `
                    <select class="form-input param-fixed-value" data-name="${p.name}" style="width:80px;">
                        <option value="true" ${val === true ? 'selected' : ''}>True</option>
                        <option value="false" ${val !== true ? 'selected' : ''}>False</option>
                    </select>`;
            } else if (p.type === 'path') {
                const shortPath = val ? (val.length > 35 ? '...' + val.slice(-35) : val) : '(none)';
                valueDisplay = `
                    <input type="text" class="form-input param-fixed-value" data-name="${p.name}" value="${escapeHtml(val || '')}" title="${escapeHtml(val || '')}" style="font-size:10px;">
                    <button class="cell-action-btn convert-parquet-btn" data-path="${escapeHtml(val || '')}" title="Convert to Parquet"><i class="fa-solid fa-bolt"></i></button>`;
            } else if (p.type === 'dict' || p.type === 'list') {
                const jsonVal = (val !== null && val !== undefined) ? JSON.stringify(val) : '';
                valueDisplay = `<input type="text" 
                    class="form-input param-fixed-value" data-name="${p.name}" 
                    value='${escapeHtml(jsonVal)}' 
                    title="JSON — edit carefully" style="font-size:11px;">`;
            } else {
                valueDisplay = `<input type="${p.type === 'int' ? 'number' : p.type === 'float' ? 'number' : 'text'}" 
                    class="form-input param-fixed-value" data-name="${p.name}" 
                    value="${val !== null && val !== undefined ? val : ''}" 
                    ${p.type === 'float' ? 'step="0.01"' : p.type === 'int' ? 'step="1"' : ''}>`;
            }

            let rangeInputs = '';
            if (isOptimizable && p.type !== 'path') {
                if (p.type === 'bool') {
                    rangeInputs = `
                        <div class="param-range-row" style="display:none;" data-range-for="${p.name}">
                            <div style="grid-column: span 3; font-size:11px; color: var(--text-secondary);">
                                Will test both <strong>True</strong> and <strong>False</strong>
                            </div>
                        </div>`;
                } else if (p.type === 'str' || p.type === 'time_str' || p.type === 'date_str') {
                    rangeInputs = `
                        <div class="param-range-row" style="display:none;" data-range-for="${p.name}">
                            <div style="grid-column: span 3;">
                                <label class="param-range-label">Choices (comma-separated)</label>
                                <input type="text" class="form-input param-choices" data-name="${p.name}" value="${val || ''}" placeholder="val1, val2, val3">
                            </div>
                        </div>`;
                } else {
                    const defaultVal = typeof val === 'number' ? val : 0;
                    const step = p.type === 'int' ? 1 : 0.1;
                    const minDefault = Math.max(0, defaultVal - defaultVal * 0.5);
                    const maxDefault = defaultVal + defaultVal * 0.5;
                    rangeInputs = `
                        <div class="param-range-row" style="display:none;" data-range-for="${p.name}">
                            <div>
                                <label class="param-range-label">Min</label>
                                <input type="number" class="form-input param-range-min" data-name="${p.name}" value="${roundSmart(minDefault, p.type)}" step="${step}">
                            </div>
                            <div>
                                <label class="param-range-label">Max</label>
                                <input type="number" class="form-input param-range-max" data-name="${p.name}" value="${roundSmart(maxDefault, p.type)}" step="${step}">
                            </div>
                            <div>
                                <label class="param-range-label">Step</label>
                                <input type="number" class="form-input param-range-step" data-name="${p.name}" value="${step}" step="${p.type === 'int' ? 1 : 0.01}">
                            </div>
                        </div>`;
                }
            }

            card.innerHTML = `
                <div class="param-header">
                    <div style="display:flex; align-items:center; gap:8px;">
                        <span class="param-name">${p.name}</span>
                        <span class="param-type-badge ${p.type}">${p.type}</span>
                    </div>
                    ${isOptimizable && p.type !== 'path' ? `
                        <label class="toggle-switch" title="Toggle optimization for this parameter">
                            <input type="checkbox" class="param-optimize-toggle" data-name="${p.name}">
                            <span class="toggle-slider"></span>
                        </label>
                    ` : ''}
                </div>
                <div class="param-value-row">${valueDisplay}</div>
                ${rangeInputs}
            `;

            paramsList.appendChild(card);
        });

        paramCount.textContent = count;

        // Setup optimize toggles
        document.querySelectorAll('.param-optimize-toggle').forEach(toggle => {
            toggle.addEventListener('change', (e) => {
                const name = e.target.dataset.name;
                const card = e.target.closest('.param-card');
                const rangeRow = card.querySelector(`[data-range-for="${name}"]`);
                
                card.classList.toggle('optimizing', e.target.checked);
                if (rangeRow) {
                    rangeRow.style.display = e.target.checked ? '' : 'none';
                }
            });
        });

        // Setup convert to parquet buttons
        document.querySelectorAll('.convert-parquet-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const path = e.currentTarget.dataset.path;
                if (!path) return;
                btn.disabled = true;
                btn.innerHTML = '<div class="spinner" style="width:12px;height:12px;"></div>';
                try {
                    const res = await fetch('/api/data/convert', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ csv_path: path })
                    });
                    const data = await res.json();
                    showToast(data.message || 'Converted successfully', 'success');
                } catch (e) {
                    showToast('Conversion failed', 'error');
                }
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-bolt"></i>';
            });
        });

        // Trigger initial calculation
        updateEstimates();
        updateWfoEstimates();
    }

    // =========================================================================
    // SEARCH SPACE ESTIMATION
    // =========================================================================
    function calculateGridCombinations() {
        if (!analysisResult || !analysisResult.parameters) return 0;
        let total = 1;
        const cards = document.querySelectorAll('.param-card');

        cards.forEach(card => {
            const isOptimizing = card.querySelector('.param-optimize-toggle')?.checked;
            if (!isOptimizing) return;

            const name = card.dataset.paramName;
            // The analysis payload exposes `parameters`; reading `params` here
            // threw as soon as any toggle was switched on, which silently killed
            // the estimate panel and the oversized-grid guard along with it.
            const paramDef = analysisResult.parameters.find(p => p.name === name);
            if (!paramDef) return;

            if (paramDef.type === 'bool') {
                total *= 2;
            } else if (paramDef.type === 'str' || paramDef.type === 'time_str' || paramDef.type === 'date_str') {
                const choicesStr = card.querySelector('.param-choices')?.value || '';
                const count = choicesStr.split(',').filter(s => s.trim()).length;
                total *= Math.max(1, count);
            } else {
                const min = parseFloat(card.querySelector('.param-range-min')?.value || 0);
                const max = parseFloat(card.querySelector('.param-range-max')?.value || 0);
                const step = parseFloat(card.querySelector('.param-range-step')?.value || 1);
                if (step > 0 && max >= min) {
                    total *= Math.floor((max - min) / step) + 1;
                }
            }
        });
        return total;
    }

    function updateEstimates() {
        const estDiv = document.getElementById('optEstimates');
        const startBtn = document.getElementById('startOptBtn');
        if (!estDiv || !startBtn || !analysisResult) return;

        const spaceSize = calculateGridCombinations();
        const mode = document.getElementById('optModeSelect')?.value || 'grid';
        const numWorkers = parseInt(document.getElementById('workersInput')?.value) || 2;
        const maxIterations = parseInt(document.getElementById('iterationsInput')?.value) || 100;
        const assumedTimePerIter = 0.1; // seconds
        
        let itersToRun = spaceSize;
        let isDanger = false;
        let html = '';

        if (mode === 'grid') {
            if (spaceSize > 1000000) {
                isDanger = true;
                html = `Grid space too large (<span class="est-highlight">${spaceSize.toLocaleString()}</span>).<br>Reduce ranges or use Random mode.`;
            } else {
                html = `Total Combinations: <span class="est-highlight">${spaceSize.toLocaleString()}</span>`;
            }
        } else {
            itersToRun = maxIterations;
            html = `Search Space: <span class="est-highlight">${spaceSize.toLocaleString()}</span><br>Evaluating: <span class="est-highlight">${maxIterations.toLocaleString()}</span>`;
        }

        const estTime = (itersToRun / numWorkers) * assumedTimePerIter;
        if (!isDanger) {
            html += `<br><small>Est. time: ~${estTime > 60 ? (estTime/60).toFixed(1) + 'm' : Math.round(estTime) + 's'} <span style="opacity:0.6">(at 0.1s/iter)</span></small>`;
        }

        estDiv.innerHTML = html;
        estDiv.className = isDanger ? 'opt-estimates danger' : 'opt-estimates';
        estDiv.style.display = 'block';
        startBtn.disabled = isDanger;
    }

    function updateWfoEstimates() {
        const estDiv = document.getElementById('wfoEstimates');
        const startBtn = document.getElementById('wfoStartBtn');
        if (!estDiv || !startBtn || !analysisResult) return;

        const spaceSize = calculateGridCombinations();
        const mode = document.getElementById('wfoOptMethod')?.value || 'grid';
        const numWorkers = parseInt(document.getElementById('wfoWorkers')?.value) || 2;
        const maxIterations = parseInt(document.getElementById('wfoIterations')?.value) || 1000;
        const stepMonths = parseInt(document.getElementById('wfoStepSize')?.value) || 6;
        
        // Very rough estimate of number of WFO steps assuming dataset span is required.
        // Actually we don't know the dataset span precisely here without asking the server or WFO panel state.
        // We will just use 5 steps as an average for the estimate UI, or let the user know it's per step.
        const assumedSteps = 5; 
        const assumedTimePerIter = 0.1; 
        
        let itersPerStep = spaceSize;
        let isDanger = false;
        let html = '';

        if (mode === 'grid') {
            if (spaceSize > 1000000) {
                isDanger = true;
                html = `Grid space too large (<span class="est-highlight">${spaceSize.toLocaleString()}</span>).<br>Reduce ranges or use Random mode.`;
            } else {
                html = `Combinations per step: <span class="est-highlight">${spaceSize.toLocaleString()}</span>`;
            }
        } else {
            itersPerStep = maxIterations;
            html = `Search Space: <span class="est-highlight">${spaceSize.toLocaleString()}</span><br>Evaluating per step: <span class="est-highlight">${maxIterations.toLocaleString()}</span>`;
        }

        const estTimePerStep = (itersPerStep / numWorkers) * assumedTimePerIter;
        if (!isDanger) {
            html += `<br><small>Est. time per step: ~${estTimePerStep > 60 ? (estTimePerStep/60).toFixed(1) + 'm' : Math.round(estTimePerStep) + 's'}</small>`;
        }

        estDiv.innerHTML = html;
        estDiv.className = isDanger ? 'opt-estimates danger' : 'opt-estimates';
        estDiv.style.display = 'block';
        
        // Disable WFO actions if grid is too large
        const previewBtn = document.getElementById('wfoPreviewBtn');
        if (isDanger) {
            startBtn.disabled = true;
            if (previewBtn) previewBtn.disabled = true;
        } else {
            // Only re-enable start if there are valid generated windows
            // The preview button can always be enabled if not in danger
            if (previewBtn) previewBtn.disabled = false;
        }
    }

    // Attach listeners for dynamic estimation
    document.getElementById('paramsList')?.addEventListener('input', () => { updateEstimates(); updateWfoEstimates(); });
    document.getElementById('paramsList')?.addEventListener('change', () => { updateEstimates(); updateWfoEstimates(); });
    
    document.getElementById('optModeSelect')?.addEventListener('change', updateEstimates);
    document.getElementById('iterationsInput')?.addEventListener('input', updateEstimates);
    document.getElementById('workersInput')?.addEventListener('input', updateEstimates);
    
    document.getElementById('wfoOptMethod')?.addEventListener('change', updateWfoEstimates);
    document.getElementById('wfoIterations')?.addEventListener('input', updateWfoEstimates);
    document.getElementById('wfoWorkers')?.addEventListener('input', updateWfoEstimates);

    // =========================================================================
    // DRAWDOWN OPTIMIZATION MODE TOGGLE
    // =========================================================================
    function setupDdOptToggle(selectId, autoInfoId, manualInputsId, descriptionId) {
        const select = document.getElementById(selectId);
        if (!select) return;
        select.addEventListener('change', () => {
            const mode = select.value;
            const autoInfo = document.getElementById(autoInfoId);
            const manualInputs = document.getElementById(manualInputsId);
            const desc = descriptionId ? document.getElementById(descriptionId) : null;
            
            if (autoInfo) autoInfo.style.display = mode === 'auto' ? '' : 'none';
            if (manualInputs) manualInputs.style.display = mode === 'manual' ? '' : 'none';
            if (desc) desc.style.display = mode !== 'disabled' ? '' : 'none';
        });
    }
    // Sidebar drawdown toggle
    setupDdOptToggle('ddOptMode', 'ddOptAutoInfo', 'ddOptManualInputs', 'ddOptDescription');
    // WFO drawdown toggle
    setupDdOptToggle('wfoDdOptMode', 'wfoDdAutoInfo', 'wfoDdManualInputs', null);

    // =========================================================================
    // OPTIMIZATION CONTROL
    // =========================================================================
    startOptBtn.addEventListener('click', startOptimization);
    stopOptBtn.addEventListener('click', stopOptimization);

    async function startOptimization() {
        if (!analysisResult) return;

        const fixedParams = {};
        const optimizeParams = {};

        // Gather parameter values
        analysisResult.parameters.forEach(p => {
            const toggle = document.querySelector(`.param-optimize-toggle[data-name="${p.name}"]`);
            const isOptimizing = toggle && toggle.checked;

            if (isOptimizing) {
                if (p.type === 'bool') {
                    optimizeParams[p.name] = { choices: [true, false], type: 'bool' };
                } else if (p.type === 'str' || p.type === 'time_str' || p.type === 'date_str') {
                    const choicesEl = document.querySelector(`.param-choices[data-name="${p.name}"]`);
                    if (choicesEl) {
                        const choices = choicesEl.value.split(',').map(s => s.trim()).filter(s => s);
                        optimizeParams[p.name] = { choices, type: p.type };
                    }
                } else {
                    const minEl = document.querySelector(`.param-range-min[data-name="${p.name}"]`);
                    const maxEl = document.querySelector(`.param-range-max[data-name="${p.name}"]`);
                    const stepEl = document.querySelector(`.param-range-step[data-name="${p.name}"]`);
                    if (minEl && maxEl && stepEl) {
                        optimizeParams[p.name] = {
                            min: parseFloat(minEl.value),
                            max: parseFloat(maxEl.value),
                            step: parseFloat(stepEl.value),
                            type: p.type,
                        };
                    }
                }
            } else {
                const valEl = document.querySelector(`.param-fixed-value[data-name="${p.name}"]`);
                if (valEl) {
                    let val = valEl.value;
                    if (valEl.tagName === 'SELECT') {
                        val = val === 'true' ? true : val === 'false' ? false : val;
                    } else if (p.type === 'int') {
                        val = parseInt(val) || 0;
                    } else if (p.type === 'float') {
                        val = parseFloat(val) || 0;
                    } else if (p.type === 'dict' || p.type === 'list') {
                        try { val = JSON.parse(val); } catch(e) { /* keep as string */ }
                    }
                    fixedParams[p.name] = val;
                }
            }
        });

        if (Object.keys(optimizeParams).length === 0) {
            showToast('No parameters selected for optimization. Performing a single validation run.', 'info');
        }

        // Find data path for charting
        for (const [key, val] of Object.entries(fixedParams)) {
            if (typeof val === 'string' && (val.endsWith('.csv') || val.endsWith('.parquet'))) {
                currentDataPath = val;
                break;
            }
        }

        const selectedOpt = scriptSelect.selectedOptions[0];
        const payload = {
            script_path: selectedOpt.dataset.path,
            entry_style: analysisResult.entry_style,
            config_class_name: analysisResult.config_class_name,
            fixed_params: fixedParams,
            optimize_params: optimizeParams,
            mode: document.getElementById('optModeSelect').value,
            num_iterations: parseInt(document.getElementById('iterationsInput').value),
            num_workers: parseInt(document.getElementById('workersInput').value),
            seed: parseInt(document.getElementById('seedInput').value),
            ranking_metric: document.getElementById('rankingSelect').value,
            top_n: parseInt(document.getElementById('topNInput').value),
            drawdown_optimization: document.getElementById('ddOptMode').value,
            dd_min_trades_per_day: parseFloat(document.getElementById('ddMinTpd').value) || 2,
            dd_target_trades_per_day: parseFloat(document.getElementById('ddTargetTpd').value) || 8,
        };

        startOptBtn.disabled = true;
        startOptBtn.innerHTML = '<div class="spinner"></div> Starting...';

        try {
            const res = await fetch('/api/optimize/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();

            if (data.status === 'started' || data.status === 'queued') {
                currentOptId = data.optimization_id;
                rememberRun(data.optimization_id, {
                    script_name: scriptSelect.value,
                    status: data.status === 'queued' ? 'queued' : 'running',
                    completed: 0,
                    total: payload.num_iterations,
                });
                showToast(data.message, data.status === 'queued' ? 'info' : 'success');
                if (data.status === 'started') {
                    // Queued runs get their stream when the scheduler starts them.
                    startProgressStream(currentOptId);
                    stopOptBtn.classList.remove('hidden');
                }
                switchTab('progress');
            } else {
                throw new Error(data.detail || 'Failed to start');
            }
            refreshRuns();
        } catch (e) {
            showToast(`Start failed: ${e.message}`, 'error');
        }
        // Never latched off: queueing another run while one is going is the
        // point of the concurrency limit.
        startOptBtn.disabled = false;
        startOptBtn.innerHTML = '<i class="fa-solid fa-rocket"></i> Start Optimization';
    }

    /** Sidebar Stop — acts on the run the dashboard is currently focused on. */
    async function stopOptimization() {
        if (!currentOptId) return;
        await stopRun(currentOptId);
    }

    // =========================================================================
    // PROGRESS STREAMING (SSE)
    // =========================================================================
    const FINISHED_STATUSES = ['completed', 'stopped', 'error', 'interrupted'];

    /** Compact duration: 45s, 12m 30s, 3h 05m. */
    function formatDuration(seconds) {
        const s = Math.max(0, Math.round(Number(seconds) || 0));
        if (s < 60) return `${s}s`;
        if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
        return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
    }

    /**
     * The Progress tab hosts one panel per run, keyed by optimization id, plus
     * a toolbar and the queue. Panels are addressed by `data-opt-id` and their
     * innards by class, so any number can coexist — the old markup used fixed
     * element ids, which meant a second run overwrote the first one's panel.
     */
    function runsContainer() {
        let host = document.getElementById('runsHost');
        if (!host) {
            host = document.createElement('div');
            host.id = 'runsHost';
            host.innerHTML = `
                <div id="runsToolbar"></div>
                <div id="runsPanels"></div>
                <div id="queuePanel"></div>`;
            progressContent.prepend(host);
        }
        return host;
    }

    function runPanelFor(optId) {
        emptyProgress.classList.add('hidden');
        runsContainer();

        const panels = document.getElementById('runsPanels');
        let panel = panels.querySelector(`[data-opt-id="${optId}"]`);
        if (panel) return panel;

        panel = document.createElement('div');
        panel.className = 'progress-panel';
        panel.dataset.optId = optId;
        panel.innerHTML = `
            <div class="progress-header">
                <div class="progress-title">
                    <i class="fa-solid fa-gauge-high" style="color:var(--accent-blue);margin-right:6px;"></i>
                    <span class="p-name">${escapeHtml(optId)}</span>
                    <span class="p-status-pill"></span>
                </div>
                <div class="progress-stats">
                    <div class="progress-stat">
                        <div class="progress-stat-value p-completed">0</div>
                        <div class="progress-stat-label">Completed</div>
                    </div>
                    <div class="progress-stat">
                        <div class="progress-stat-value p-total">0</div>
                        <div class="progress-stat-label">Total</div>
                    </div>
                    <div class="progress-stat">
                        <div class="progress-stat-value p-failed" style="color:var(--red);">0</div>
                        <div class="progress-stat-label">Failed</div>
                    </div>
                    <div class="progress-stat">
                        <div class="progress-stat-value p-pending" style="color:var(--text-muted);">0</div>
                        <div class="progress-stat-label">Remaining</div>
                    </div>
                </div>
            </div>
            <div class="p-banner"></div>
            <div class="progress-bar-container">
                <div class="progress-bar p-bar" style="width:0%"></div>
            </div>
            <div class="progress-detail">
                <span class="p-percent">0%</span>
                <span class="p-eta">ETA: calculating...</span>
            </div>
            <div class="progress-actions">
                <button class="btn btn-secondary btn-sm p-results-btn">
                    <i class="fa-solid fa-table"></i> View Results
                </button>
                <button class="btn btn-danger btn-sm p-stop-btn">
                    <i class="fa-solid fa-stop"></i> Stop
                </button>
            </div>`;
        panels.appendChild(panel);

        panel.querySelector('.p-results-btn').addEventListener('click', async () => {
            await loadResults(optId);
            switchTab('results');
        });
        panel.querySelector('.p-stop-btn').addEventListener('click', () => stopRun(optId));
        return panel;
    }

    /**
     * Paint one progress payload into that run's panel.
     *
     * The bar follows the server's `percent`, which counts a batch as done only
     * once it has actually produced a result. Older runs (and any payload
     * without `percent`) fall back to completed/total.
     */
    function applyProgress(optId, p) {
        const panel = document.querySelector(`#runsPanels [data-opt-id="${optId}"]`);
        if (!p || !panel) return;

        const total = Number(p.total) || 0;
        const completed = Number(p.completed) || 0;
        const raw = (typeof p.percent === 'number')
            ? p.percent
            : (total > 0 ? (completed / total) * 100 : 0);
        const pct = Math.max(0, Math.min(100, raw));

        const remaining = (typeof p.pending === 'number')
            ? p.pending
            : Math.max(total - completed, 0);

        const set = (cls, val) => {
            const el = panel.querySelector(cls);
            if (el) el.textContent = val;
        };

        set('.p-completed', completed);
        set('.p-total', total);
        set('.p-failed', p.failed || 0);
        set('.p-pending', remaining);
        set('.p-percent', `${pct.toFixed(1)}%`);

        const pill = panel.querySelector('.p-status-pill');
        if (pill && p.status) {
            pill.textContent = p.status;
            pill.className = `p-status-pill opt-card-status ${p.status}`;
        }

        const stopBtn = panel.querySelector('.p-stop-btn');
        if (stopBtn) stopBtn.classList.toggle('hidden', FINISHED_STATUSES.includes(p.status));

        const elBar = panel.querySelector('.p-bar');
        if (elBar) elBar.style.width = `${pct.toFixed(1)}%`;

        const elETA = panel.querySelector('.p-eta');
        if (elETA) {
            if (p.status === 'aggregating') {
                elETA.textContent = 'Aggregating results...';
            } else if (p.status === 'planning') {
                // Bayesian surrogate fit between waves — no batch is running,
                // and on a large sweep this legitimately takes minutes.
                elETA.textContent = 'Planning next batches (fitting model)...';
            } else if (p.status === 'stopping') {
                elETA.textContent = 'Stopping — finishing in-flight batches...';
            } else if (FINISHED_STATUSES.includes(p.status)) {
                elETA.textContent = `Status: ${p.status}`
                    + (p.elapsed_seconds ? ` · took ${formatDuration(p.elapsed_seconds)}` : '');
            } else if (p.eta_seconds && p.eta_seconds > 0) {
                // Expected from observed throughput; worst case assumes every
                // remaining batch is as slow as the slowest one seen so far.
                let text = `ETA: ${formatDuration(p.eta_seconds)}`;
                if (p.eta_max_seconds && p.eta_max_seconds > p.eta_seconds) {
                    text += ` · max ~${formatDuration(p.eta_max_seconds)}`;
                }
                elETA.textContent = text;
            } else {
                elETA.textContent = 'ETA: calculating...';
            }
        }

        return { pct, total, completed };
    }

    /** Offer to finish a run that stopped part-way instead of starting over. */
    function showResumeBanner(optId, p) {
        const panel = document.querySelector(`#runsPanels [data-opt-id="${optId}"]`);
        const banner = panel && panel.querySelector('.p-banner');
        if (!banner) return;

        const remaining = p.remaining || p.pending || 0;
        if (!remaining) { banner.innerHTML = ''; return; }

        banner.innerHTML = `
            <div class="resume-banner">
                <div class="resume-banner-text">
                    <i class="fa-solid fa-circle-pause"></i>
                    <span><strong>${escapeHtml(String(p.status || 'interrupted'))}</strong> —
                    ${p.completed || 0} of ${p.total || 0} batches done,
                    <strong>${remaining}</strong> left to run.</span>
                </div>
                <button class="btn btn-success btn-sm p-resume-btn">
                    <i class="fa-solid fa-play"></i> Resume
                </button>
            </div>
        `;
        const btn = banner.querySelector('.p-resume-btn');
        if (btn) btn.addEventListener('click', () => resumeOptimization(optId));
    }

    function closeProgressStream(optId) {
        const stream = progressStreams.get(optId);
        if (stream) stream.close();
        progressStreams.delete(optId);
    }

    /** Reflect however many runs are live in the header status indicator. */
    function updateGlobalStatus() {
        const live = progressStreams.size;
        if (live === 0) {
            setGlobalStatus('idle', 'Idle');
        } else if (live === 1) {
            const [optId] = progressStreams.keys();
            const panel = document.querySelector(`#runsPanels [data-opt-id="${optId}"]`);
            const pct = panel ? panel.querySelector('.p-percent')?.textContent : '';
            setGlobalStatus('running', pct ? `Optimizing ${pct}` : 'Optimizing...');
        } else {
            setGlobalStatus('running', `${live} optimizations running`);
        }
    }

    function startProgressStream(optId) {
        // One stream per run; re-attaching to a run already streamed is a no-op.
        if (progressStreams.has(optId)) return;

        runPanelFor(optId);

        const stream = new EventSource(`/api/optimize/progress/${optId}`);
        progressStreams.set(optId, stream);

        stream.onmessage = (event) => {
            try {
                const p = JSON.parse(event.data);

                if (p.status === 'done') {
                    closeProgressStream(optId);
                    if (p.final_status && p.final_status !== 'completed') {
                        onOptimizationHalted(optId, p.final_status);
                    } else {
                        onOptimizationComplete(optId);
                    }
                    return;
                }

                applyProgress(optId, p);
                updateGlobalStatus();
            } catch (e) { /* ignore parse errors */ }
        };

        stream.onerror = async () => {
            // EventSource retries by itself. Only finish when the server agrees
            // the run is actually over — a dropped connection used to be
            // reported as "completed" while batches were still running.
            try {
                const res = await fetch(`/api/optimize/status/${optId}`);
                if (!res.ok) return;
                const p = (await res.json()).progress || {};
                if (p.is_running) return;
                if (!FINISHED_STATUSES.includes(p.status)) return;

                closeProgressStream(optId);
                if (p.status === 'completed' && !p.resumable) {
                    onOptimizationComplete(optId);
                } else {
                    onOptimizationHalted(optId, p.status);
                }
            } catch (e) { /* leave the stream retrying */ }
        };
    }

    /** Stop one run, whether it is executing or still waiting in the queue. */
    async function stopRun(optId) {
        try {
            const res = await fetch(`/api/optimize/stop/${optId}`, { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Stop failed');
            // The run stays resumable either way.
            showToast(data.message || 'Optimization stopped', 'warning');
        } catch (e) {
            showToast(`Stop failed: ${e.message}`, 'error');
        }
        refreshRuns();
    }
    window.stopRun = stopRun;

    /** Drop a run out of the queue before it ever starts. */
    async function cancelQueued(optId) {
        try {
            const res = await fetch(`/api/optimize/queue/${optId}`, { method: 'DELETE' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Cancel failed');
            showToast(`Removed ${optId} from the queue`, 'warning');
        } catch (e) {
            showToast(`Cancel failed: ${e.message}`, 'error');
        }
        refreshRuns();
    }
    window.cancelQueued = cancelQueued;

    async function setMaxConcurrent(value) {
        try {
            const res = await fetch('/api/optimize/max-concurrent', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ max_concurrent: Number(value) }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Could not change the limit');
            showToast(`Running up to ${data.max_concurrent} optimization(s) at a time`, 'success');
        } catch (e) {
            showToast(e.message, 'error');
        }
        refreshRuns();
    }

    function renderRunsToolbar(data) {
        const bar = document.getElementById('runsToolbar');
        if (!bar) return;

        const running = data.running.length;
        const queued = data.queued.length;
        const workers = data.worker_total || 0;
        const cpus = data.cpu_count || 0;
        // Each worker is a separate process running your strategy, so the sum
        // across concurrent runs is what actually competes for the machine.
        const overloaded = cpus > 0 && workers > cpus;

        bar.innerHTML = `
            <div class="runs-toolbar">
                <div class="runs-toolbar-info">
                    <span><i class="fa-solid fa-bolt" style="color:var(--accent-blue);"></i>
                        <strong>${running}</strong> running</span>
                    <span><i class="fa-regular fa-clock"></i> <strong>${queued}</strong> queued</span>
                    <span class="${overloaded ? 'runs-warn' : ''}"
                          title="Total worker processes across all running optimizations">
                        <i class="fa-solid fa-microchip"></i> ${workers} worker(s)${cpus ? ` / ${cpus} CPUs` : ''}
                    </span>
                </div>
                <div class="runs-toolbar-limit">
                    <label class="form-label" style="margin:0;">Run at once</label>
                    <input type="number" class="form-input" id="maxConcurrentInput"
                           value="${data.max_concurrent}" min="1" max="16" style="width:70px;">
                </div>
            </div>
            ${overloaded ? `
            <div class="runs-warn-banner">
                <i class="fa-solid fa-triangle-exclamation"></i>
                ${workers} worker processes across ${running} runs on ${cpus} CPUs — batches may be
                starved or killed. Lower the limit or the per-run worker count.
            </div>` : ''}`;

        const input = document.getElementById('maxConcurrentInput');
        if (input) {
            input.addEventListener('change', () => {
                const v = Math.max(1, Math.min(16, parseInt(input.value) || 1));
                input.value = v;
                setMaxConcurrent(v);
            });
        }
    }

    function renderQueue(data) {
        const host = document.getElementById('queuePanel');
        if (!host) return;

        if (!data.queued.length) { host.innerHTML = ''; return; }

        host.innerHTML = `
            <div class="progress-panel">
                <div class="progress-header">
                    <div class="progress-title">
                        <i class="fa-regular fa-clock" style="color:var(--yellow);margin-right:6px;"></i>
                        Queued — starts automatically as slots free up
                    </div>
                </div>
                <div class="queue-list">
                    ${data.queued.map(q => `
                        <div class="queue-item">
                            <span class="queue-pos">${q.position}</span>
                            <div class="queue-meta">
                                <div class="queue-name">${escapeHtml(q.script_name || q.optimization_id)}</div>
                                <div class="queue-sub">
                                    ${escapeHtml(String(q.mode || ''))} ·
                                    ${q.num_iterations ?? '—'} iters ·
                                    ${q.num_workers ?? '—'} workers${q.resume ? ' · resume' : ''}
                                </div>
                            </div>
                            <button class="btn btn-danger btn-sm"
                                    onclick="cancelQueued('${q.optimization_id}')">
                                <i class="fa-solid fa-xmark"></i> Cancel
                            </button>
                        </div>`).join('')}
                </div>
            </div>`;
    }

    /**
     * Sync the Progress tab with the server: a panel and a live stream per
     * running optimization, plus the queue.
     *
     * This also covers reattaching after a page reload or a server restart —
     * without it the tab came back empty with no way into a run already in
     * flight, and the only visible option was to start over from batch 1.
     */
    async function refreshRuns() {
        let data;
        try {
            const res = await fetch('/api/optimize/runs');
            if (!res.ok) return;
            data = await res.json();
        } catch (e) {
            return;   // dashboard works fine without it
        }

        runsContainer();
        renderRunsToolbar(data);
        renderQueue(data);

        const live = new Set(data.running.map(r => r.optimization_id));

        for (const run of data.running) {
            runPanelFor(run.optimization_id);
            applyProgress(run.optimization_id, run.progress);
            startProgressStream(run.optimization_id);
            rememberRun(run.optimization_id, {
                script_name: run.script_name,
                status: (run.progress || {}).status || 'running',
                completed: (run.progress || {}).completed,
                total: (run.progress || {}).total,
            });
            if (!currentOptId) currentOptId = run.optimization_id;
        }
        for (const q of data.queued) {
            rememberRun(q.optimization_id, {
                script_name: q.script_name,
                status: 'queued',
                completed: 0,
                total: q.num_iterations,
            });
        }

        // Drop streams for runs the server no longer reports as running.
        for (const optId of [...progressStreams.keys()]) {
            if (!live.has(optId)) closeProgressStream(optId);
        }

        // A finished panel stays until the user starts something else, so the
        // final numbers remain readable; only clear when nothing is left.
        if (!data.running.length && !data.queued.length && !progressStreams.size) {
            const panels = document.getElementById('runsPanels');
            if (panels && !panels.children.length) emptyProgress.classList.remove('hidden');
        }

        // Nothing in flight means nothing to poll for.
        if (data.running.length || data.queued.length) startRunsPolling();
        else stopRunsPolling();

        updateGlobalStatus();
        return data;
    }

    /**
     * Poll for things SSE cannot report: the queue advancing, and runs started
     * from another tab.
     *
     * Deliberately slow, and only while something is actually in flight — live
     * progress already arrives over SSE, so this is just a catch-up tick.
     */
    function startRunsPolling() {
        if (runsPollTimer) return;
        runsPollTimer = setInterval(refreshRuns, 15000);
    }

    function stopRunsPolling() {
        if (!runsPollTimer) return;
        clearInterval(runsPollTimer);
        runsPollTimer = null;
    }

    async function onOptimizationComplete(optId) {
        startOptBtn.disabled = false;

        rememberRun(optId, { status: 'completed' });
        const data = await refreshRuns();
        const stillBusy = data && (data.running.length || data.queued.length);

        // With several runs in flight, yanking the view to Results every time
        // one finishes would fight the user. Only follow through when this was
        // the last thing running.
        if (!stillBusy) {
            stopOptBtn.classList.add('hidden');
            await loadResults(optId);
            switchTab('results');
            showToast(`${optId} completed`, 'success');
        } else {
            showToast(`${optId} completed — pick it in the Results tab to view`, 'success');
            // Keep the already-open Results view in sync if it is showing runs.
            if (selectedRunIds.length) renderResults(currentResults);
        }
    }

    /** A run ended without finishing — keep its panel with a Resume option. */
    async function onOptimizationHalted(optId, status) {
        startOptBtn.disabled = false;
        showToast(`${optId} ${status || 'interrupted'} — you can resume it`, 'warning');

        try {
            const res = await fetch(`/api/optimize/status/${optId}`);
            if (res.ok) {
                const p = (await res.json()).progress || {};
                runPanelFor(optId);
                applyProgress(optId, p);
                showResumeBanner(optId, p);
            }
        } catch (e) { /* banner is best-effort */ }

        const data = await refreshRuns();
        if (!(data && (data.running.length || data.queued.length))) {
            stopOptBtn.classList.add('hidden');
        }
        switchTab('progress');
    }

    /**
     * Resume a run instead of restarting it.
     *
     * Batches that already succeeded are skipped by the engine, so this picks
     * up from wherever the previous process left off.
     */
    async function resumeOptimization(optId) {
        try {
            const res = await fetch(`/api/optimize/resume/${optId}`, { method: 'POST' });
            const data = await res.json();
            if (!res.ok || !['resumed', 'queued'].includes(data.status)) {
                throw new Error(data.detail || 'Resume failed');
            }

            currentOptId = optId;
            rememberRun(optId, {
                status: data.status === 'queued' ? 'queued' : 'running',
                completed: data.completed,
                total: data.total,
            });
            showToast(data.message || 'Optimization resumed',
                      data.status === 'queued' ? 'info' : 'success');
            if (data.status === 'resumed') {
                startProgressStream(optId);
                stopOptBtn.classList.remove('hidden');
            }
            historyModal.classList.remove('active');
            switchTab('progress');
            refreshRuns();
        } catch (e) {
            showToast(`Resume failed: ${e.message}`, 'error');
        }
    }
    window.resumeOptimization = resumeOptimization;

    /**
     * Show any run that was left unfinished, so it can be resumed.
     *
     * refreshRuns() covers everything currently running or queued; this adds
     * the most recent orphan from a previous process, which has no live stream
     * to discover it by.
     */
    async function showLastInterruptedRun() {
        try {
            const res = await fetch('/api/optimize/active');
            if (!res.ok) return;
            const data = await res.json();
            if (!data.optimization_id || data.is_running || !data.resumable) return;

            currentOptId = currentOptId || data.optimization_id;
            runPanelFor(data.optimization_id);
            applyProgress(data.optimization_id, data.progress);
            showResumeBanner(data.optimization_id, data.progress || {});
        } catch (e) { /* dashboard works fine without it */ }
    }

    // =========================================================================
    // RESULTS
    // =========================================================================
    /** Show one run's results. Kept for every existing call site. */
    async function loadResults(optId) {
        return loadResultsFor([optId]);
    }

    /**
     * Show one or more runs in the Results tab.
     *
     * Runs finish at different times while others are still going, so the tab
     * has to be able to show any of them — and, when more than one is ticked,
     * all of them together with a Run column so two sweeps can be compared.
     */
    async function loadResultsFor(ids) {
        const wanted = [...new Set((ids || []).filter(Boolean))];
        if (!wanted.length) return;

        try {
            const payloads = [];
            for (const id of wanted) {
                const res = await fetch(`/api/optimizations/${id}/results?top=100`);
                if (!res.ok) continue;
                const data = await res.json();
                if (data.status === 'success') payloads.push([id, data]);
            }
            if (!payloads.length) throw new Error('no results for the selected run(s)');

            selectedRunIds = payloads.map(([id]) => id);
            currentOptId = selectedRunIds[0];

            // Opening a run's results — including from History — puts it in the
            // picker, so two past runs can be pulled up and compared.
            for (const [id, data] of payloads) {
                rememberRun(id, {
                    script_name: (data.config || {}).script_name,
                    started_at: (data.config || {}).started_at,
                    completed: data.successful,
                    total: data.total_results,
                    status: (data.config || {}).status || 'completed',
                });
            }
            resultsView = 'top';   // don't carry a failed-view filter across runs

            // A single run keeps its payload untouched, so nothing downstream
            // sees a different shape than before.
            currentResults = payloads.length === 1 ? payloads[0][1] : mergeResults(payloads);
            resultsBadge.textContent = currentResults.successful || 0;
            renderResults(currentResults);
        } catch (e) {
            showToast(`Failed to load results: ${e.message}`, 'error');
        }
    }

    /** Combine several runs' payloads into one table, tagging each row's run. */
    function mergeResults(payloads) {
        const tag = (rows, id) => (rows || []).map(r => ({ ...r, __run: id }));
        const merged = {
            status: 'success',
            __merged: true,
            optimization_id: payloads.map(([id]) => id).join(', '),
            config: payloads[0][1].config,
            top_results: [], all_results: [], columns: [],
            total_results: 0, successful: 0, failed: 0,
        };

        const cols = new Set();
        for (const [id, data] of payloads) {
            merged.top_results.push(...tag(data.top_results, id));
            merged.all_results.push(...tag(data.all_results, id));
            merged.total_results += data.total_results || 0;
            merged.successful += data.successful || 0;
            merged.failed += data.failed || 0;
            (data.columns || []).forEach(c => cols.add(c));
        }
        merged.columns = [...cols];

        // Best first across every run, so the comparison is immediately useful.
        merged.top_results.sort((a, b) =>
            (Number(b.composite_score) || -Infinity) - (Number(a.composite_score) || -Infinity));
        return merged;
    }

    /** Note a run this session is working with, merging in whatever we know. */
    function rememberRun(optId, meta) {
        if (!optId) return;
        const prev = sessionRuns.get(optId) || { id: optId };
        sessionRuns.set(optId, { ...prev, ...meta, id: optId });
    }

    /** Session runs, newest first — what the picker offers. */
    function sessionRunList() {
        return [...sessionRuns.values()].sort((a, b) => String(b.id).localeCompare(String(a.id)));
    }

    /** "3 Sep 00:40" — every run of a script shares its name, so time is the label. */
    function runWhen(run) {
        let d = run.started_at ? new Date(run.started_at) : null;
        if (!d || isNaN(d)) {
            const m = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/.exec(run.id || '');
            d = m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
        }
        if (!d || isNaN(d)) return run.id;
        return d.toLocaleString(undefined, {
            day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
        });
    }

    /**
     * A single compact control for choosing which run(s) the table shows.
     *
     * One row, whatever the run count. A chip per run wrapped onto several
     * lines and ate half the panel — and since every run of a script carries
     * the same name, the chips were indistinguishable anyway. The dropdown
     * scrolls instead of growing, and leads with the start time.
     */
    function runSwitcherHtml() {
        const runs = sessionRunList();
        if (runs.length < 2) return '';

        const selected = runs.filter(r => selectedRunIds.includes(r.id));
        const first = selected[0] || runs[0];
        const summary = selected.length > 1
            ? `${selected.length} runs compared`
            : `${runWhen(first)} · ${first.completed || 0}/${first.total || 0}`;

        const options = runs.map(r => {
            const on = selectedRunIds.includes(r.id);
            return `
                <label class="run-option ${on ? 'active' : ''}" title="${escapeAttr(r.id)}">
                    <input type="checkbox" data-run-id="${escapeAttr(r.id)}" ${on ? 'checked' : ''}>
                    <span class="run-option-when">${escapeHtml(runWhen(r))}</span>
                    <span class="run-option-name">${escapeHtml(r.script_name || r.id)}</span>
                    <span class="run-option-meta">${r.completed || 0}/${r.total || 0}</span>
                    <span class="run-option-status opt-card-status ${escapeAttr(r.status || '')}">${escapeHtml(r.status || '')}</span>
                </label>`;
        }).join('');

        return `
            <div class="run-picker">
                <button type="button" class="run-picker-btn" id="runPickerBtn">
                    <i class="fa-solid fa-layer-group"></i>
                    <span class="run-picker-current">${escapeHtml(summary)}</span>
                    <span class="run-picker-count">${runs.length}</span>
                    <i class="fa-solid fa-chevron-down"></i>
                </button>
                <div class="run-picker-menu hidden" id="runPickerMenu">
                    <div class="run-picker-head">Tick one to view · several to compare</div>
                    <div class="run-picker-list">${options}</div>
                </div>
            </div>`;
    }

    /** Render the results body with the run picker above it. */
    function setResultsHtml(bodyHtml) {
        resultsContainer.innerHTML = runSwitcherHtml() + bodyHtml;

        const btn = document.getElementById('runPickerBtn');
        const menu = document.getElementById('runPickerMenu');
        if (btn && menu) {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                menu.classList.toggle('hidden');
            });
            menu.addEventListener('click', (e) => e.stopPropagation());
        }

        resultsContainer.querySelectorAll('.run-option input[data-run-id]').forEach(cb => {
            cb.addEventListener('change', () => {
                const picked = [...resultsContainer.querySelectorAll('.run-option input:checked')]
                    .map(x => x.dataset.runId);
                // Never leave the tab with nothing selected.
                loadResultsFor(picked.length ? picked : [cb.dataset.runId]);
            });
        });
    }

    // Any click outside the dropdown closes it.
    document.addEventListener('click', () => {
        const menu = document.getElementById('runPickerMenu');
        if (menu) menu.classList.add('hidden');
    });

    /** Every batch that ran but didn't succeed. */
    function failedRowsOf(data) {
        return (data.all_results || []).filter(r => r.status !== 'OK');
    }

    /**
     * Table of failed batches, with the same per-row actions the successful
     * table has. A failed batch is often the one you most want to reload —
     * to reproduce the failure with its exact parameters.
     */
    function buildFailedTable(rows, heading, extraHeaderHtml = '') {
        let html = `
            <div style="padding:0 0 12px; display:flex; justify-content:space-between; align-items:center;">
                <div style="font-size:14px; font-weight:600; color:#ef4444;">
                    <i class="fa-solid fa-triangle-exclamation" style="margin-right:6px;"></i>
                    ${heading}
                </div>
                <div class="btn-group">${extraHeaderHtml}</div>
            </div>
            <div class="results-table-wrapper" style="max-height:calc(100vh - 200px); overflow:auto;">
                <table class="results-table">
                    <thead><tr>
                        <th>Batch</th>
                        <th>Status</th>
                        <th>Error</th>
                        <th>Actions</th>
                    </tr></thead><tbody>`;

        rows.forEach(row => {
            const batch = row.batch || '';
            const err = row.error || 'Unknown Error';
            // Merged rows carry their own run; single-run rows use the focused one.
            const rowOptId = row.__run || currentOptId;
            html += `<tr>
                <td>${escapeHtml(batch)}</td>
                <td style="color:#ef4444;font-weight:bold">${escapeHtml(String(row.status || ''))}</td>
                <td style="color:#f87171;font-size:12px;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeAttr(err)}">${escapeHtml(err)}</td>
                <td style="display:flex; gap:4px;">
                    <button class="cell-action-btn" onclick="viewBatch('${rowOptId}', '${batch}')" title="View Details">
                        <i class="fa-solid fa-eye"></i>
                    </button>
                    <button class="cell-action-btn" onclick="loadOptimization('${rowOptId}', '${batch}')" title="Load into Optimizer">
                        <i class="fa-solid fa-pen-to-square"></i>
                    </button>
                </td>
            </tr>`;
        });

        return html + '</tbody></table></div>';
    }

    window.setResultsView = function(view) {
        resultsView = view;
        if (currentResults) renderResults(currentResults);
    };

    function renderResults(data) {
        const failed = failedRowsOf(data);

        // Failed-batch view, when the user asked for it from the toggle.
        if (resultsView === 'failed' && failed.length > 0) {
            setResultsHtml(buildFailedTable(
                failed,
                `Failed Batches — ${failed.length} of ${data.total_results}`,
                `<button class="btn btn-secondary btn-sm" onclick="setResultsView('top')">
                    <i class="fa-solid fa-trophy"></i> Back to Top Results
                 </button>`
            ));
            return;
        }

        if (!data.top_results || data.top_results.length === 0) {
            if (data.all_results && data.all_results.length > 0) {
                setResultsHtml(buildFailedTable(
                    data.all_results,
                    `Optimization Failed — ${data.total_results} batches failed`
                ));
            } else {
                setResultsHtml(`
                    <div class="empty-state">
                        <i class="fa-solid fa-exclamation-triangle"></i>
                        <h3>No Successful Results</h3>
                        <p>All batches failed or produced no trades. Check script parameters.</p>
                    </div>`);
            }
            return;
        }

        // Determine which metrics to show
        const metricCols = ['composite_score', 'Total Trades', 'Net Profit', 'Win Rate %', 
                            'Profit Factor', 'Sharpe Ratio', 'Overall Max Drawdown', 'CAGR %', 'elapsed_seconds'];
        const availableCols = metricCols.filter(c => data.top_results.some(r => r[c] !== undefined && r[c] !== null));

        // Get optimized param names from config
        const optParamNames = data.config && data.config.optimize_params 
            ? Object.keys(data.config.optimize_params) : [];

        let rows = [...data.top_results];

        // Sort
        if (sortCol) {
            rows.sort((a, b) => {
                let va = a[sortCol], vb = b[sortCol];
                if (va == null) va = -Infinity;
                if (vb == null) vb = -Infinity;
                va = typeof va === 'string' ? parseFloat(va) || 0 : va;
                vb = typeof vb === 'string' ? parseFloat(vb) || 0 : vb;
                return sortAsc ? va - vb : vb - va;
            });
        }

        let html = `
            <div style="padding:0 0 12px; display:flex; justify-content:space-between; align-items:center;">
                <div style="font-size:14px; font-weight:600; color:var(--text-heading);">
                    <i class="fa-solid fa-trophy" style="color:#fbbf24;margin-right:6px;"></i>
                    Top Results — ${data.successful} successful / ${data.total_results} total
                    ${data.__merged ? `<span style="color:var(--text-muted);font-weight:500;"> across ${selectedRunIds.length} runs</span>` : ''}
                </div>
                <div class="btn-group">
                    ${failed.length > 0 ? `
                    <button class="btn btn-secondary btn-sm" onclick="setResultsView('failed')"
                            title="Failed batches can be loaded back into the optimizer too">
                        <i class="fa-solid fa-triangle-exclamation" style="color:var(--red);"></i>
                        ${failed.length} Failed
                    </button>` : ''}
                    <button class="btn btn-secondary btn-sm" onclick="shareCurrentResults()"
                            title="Copy a link that reopens exactly this view">
                        <i class="fa-solid fa-link"></i> Share
                    </button>
                    <button class="btn btn-secondary btn-sm" onclick="exportResultsCSV()">
                        <i class="fa-solid fa-download"></i> Export CSV
                    </button>
                </div>
            </div>
            <div class="results-table-wrapper" style="max-height:calc(100vh - 200px); overflow:auto;">
                <table class="results-table">
                    <thead><tr>
                        <th>#</th>
                        ${data.__merged ? '<th>Run</th>' : ''}
                        <th>Batch</th>`;

        // Optimized param columns
        optParamNames.forEach(name => {
            const isSorted = sortCol === name;
            html += `<th class="${isSorted ? 'sorted' : ''}" data-sort="${name}">${name}</th>`;
        });

        // Metric columns
        availableCols.forEach(col => {
            const isSorted = sortCol === col;
            const label = col.replace(/_/g, ' ').replace('composite score', 'Score');
            html += `<th class="${isSorted ? 'sorted' : ''}" data-sort="${col}">${label}</th>`;
        });

        html += `<th>Actions</th></tr></thead><tbody>`;

        rows.forEach((row, i) => {
            const rank = i + 1;
            const rankClass = rank <= 3 ? `rank-${rank}` : '';
            // In a merged view each row belongs to a different run, so every
            // action below has to target that row's run, not the focused one.
            const rowOptId = row.__run || currentOptId;
            html += `<tr class="${rankClass}">
                <td>${rank}</td>
                ${data.__merged ? `<td class="run-cell" title="${escapeAttr(rowOptId)}">${escapeHtml(runWhen(sessionRuns.get(rowOptId) || { id: rowOptId }))}</td>` : ''}
                <td>${row.batch || ''}</td>`;

            optParamNames.forEach(name => {
                const v = row[name];
                html += `<td>${formatValue(v)}</td>`;
            });

            availableCols.forEach(col => {
                const v = row[col];
                let cls = '';
                if (col === 'composite_score') cls = 'cell-score';
                else if (col === 'Net Profit' || col === 'CAGR %') {
                    cls = v > 0 ? 'cell-positive' : v < 0 ? 'cell-negative' : '';
                }
                else if (col === 'Overall Max Drawdown') cls = 'cell-negative';
                html += `<td class="${cls}">${formatValue(v)}</td>`;
            });

            const key = `${rowOptId}/${row.batch}`;
            const isSaved = savedUserData && savedUserData.favorites && savedUserData.favorites[key];
            const starColor = isSaved ? 'var(--accent-yellow, #f59e0b)' : 'inherit';
            const starClass = isSaved ? 'fa-solid fa-star' : 'fa-regular fa-star';

            html += `<td style="display:flex; gap:4px;">
                <button class="cell-action-btn" onclick="openSaveModal('${rowOptId}', '${row.batch}')" title="Save / Review">
                    <i class="${starClass}" style="color:${starColor};"></i>
                </button>
                <button class="cell-action-btn" onclick="viewBatch('${rowOptId}', '${row.batch}')" title="View Details">
                    <i class="fa-solid fa-eye"></i>
                </button>
                <button class="cell-action-btn" onclick="viewBatchChart('${rowOptId}', '${row.batch}')" title="View on Chart">
                    <i class="fa-solid fa-chart-line"></i>
                </button>
                <button class="cell-action-btn" onclick="viewExcel('${rowOptId}', '${row.batch}')" title="View Excel Report">
                    <i class="fa-solid fa-table"></i>
                </button>
                <button class="cell-action-btn" onclick="loadOptimization('${rowOptId}', '${row.batch}')" title="Load & Edit Parameters">
                    <i class="fa-solid fa-pen-to-square"></i>
                </button>
                <button class="cell-action-btn" onclick="shareBatch('${rowOptId}', '${row.batch}')" title="Copy a link to this batch">
                    <i class="fa-solid fa-link"></i>
                </button>
                <a class="cell-action-btn" href="/api/download/${rowOptId}/${row.batch}/excel" title="Download Excel Report" download style="display:inline-flex;align-items:center;justify-content:center;text-decoration:none;">
                    <i class="fa-solid fa-file-excel"></i>
                </a>
            </td></tr>`;
        });

        html += '</tbody></table></div>';
        setResultsHtml(html);

        // Setup sort headers
        resultsContainer.querySelectorAll('th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const col = th.dataset.sort;
                if (sortCol === col) {
                    sortAsc = !sortAsc;
                } else {
                    sortCol = col;
                    sortAsc = false;
                }
                renderResults(currentResults);
            });
        });
    }

    // Make functions global for onclick handlers
    window.viewBatch = async function(optId, batchId) {
        try {
            const res = await fetch(`/api/optimizations/${optId}/batch/${batchId}`);
            const data = await res.json();
            showBatchModal(batchId, data, optId);
        } catch (e) {
            showToast('Failed to load batch details', 'error');
        }
    };

    window.viewBatchChart = async function(optId, batchId) {
        switchTab('chart');
        if (currentDataPath) {
            await loadChartData(currentDataPath, chartTfSelect.value);
            // Load trade markers
            try {
                const res = await fetch(`/api/chart/trades/${optId}/${batchId}`);
                const data = await res.json();
                if (data.markers && data.markers.length > 0 && candleSeries) {
                    candleSeries.setMarkers(data.markers.sort((a, b) => a.time - b.time));
                    chartSymbolLabel.textContent = `${batchId} — ${data.total_trades} trades`;
                }
            } catch (e) { /* ignore */ }
        } else {
            showToast('No data path found for charting', 'warning');
        }
    };

    window.exportResultsCSV = function() {
        if (!currentResults || !currentResults.all_results) return;
        const rows = currentResults.all_results;
        if (rows.length === 0) return;

        const cols = Object.keys(rows[0]);
        let csv = cols.join(',') + '\n';
        rows.forEach(row => {
            csv += cols.map(c => {
                let v = row[c];
                if (v === null || v === undefined) return '';
                if (typeof v === 'string' && v.includes(',')) return `"${v}"`;
                return v;
            }).join(',') + '\n';
        });

        const blob = new Blob([csv], { type: 'text/csv' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `optimization_results_${currentOptId || 'export'}.csv`;
        a.click();
        URL.revokeObjectURL(url);
    };

    function showBatchModal(batchId, data, optId) {
        batchModalTitle.textContent = `Batch: ${batchId}`;

        let html = '<div class="batch-detail-grid">';

        // Metrics section
        if (data.metrics && data.metrics.metrics) {
            html += '<div class="batch-detail-section"><h4><i class="fa-solid fa-chart-pie" style="color:var(--accent-blue);margin-right:4px;"></i> Metrics</h4>';
            for (const [key, val] of Object.entries(data.metrics.metrics)) {
                if (key.startsWith('_')) continue;
                const cls = key.includes('Profit') || key.includes('PnL') ? (val > 0 ? 'cell-positive' : val < 0 ? 'cell-negative' : '') : '';
                html += `<div class="metric-row"><span class="label">${key}</span><span class="value ${cls}">${formatValue(val)}</span></div>`;
            }
            html += '</div>';
        }

        // Params section
        if (data.params) {
            html += '<div class="batch-detail-section"><h4><i class="fa-solid fa-sliders" style="color:var(--accent-purple);margin-right:4px;"></i> Parameters</h4>';
            for (const [key, val] of Object.entries(data.params)) {
                html += `<div class="metric-row"><span class="label">${key}</span><span class="value">${formatValue(val)}</span></div>`;
            }
            html += '</div>';
        }

        html += '</div>';

        // Files info
        if (data.trade_files && data.trade_files.length > 0) {
            html += '<div style="margin-top:16px;"><span class="text-xs text-muted">Trade Files:</span> ' +
                data.trade_files.map(f => `<span class="param-type-badge str">${f}</span>`).join(' ') + '</div>';
        }

        batchModalBody.innerHTML = html;
        batchModal.classList.add('active');

        // Wire up chart button
        document.getElementById('batchViewChartBtn').onclick = () => {
            batchModal.classList.remove('active');
            viewBatchChart(optId, batchId);
        };

        // The parameters are already in hand, so copying needs no extra request.
        const copyBtn = document.getElementById('batchCopyParamsBtn');
        if (copyBtn) {
            const params = data.params || {};
            const n = Object.keys(params).length;
            copyBtn.disabled = n === 0;
            copyBtn.onclick = () => copyToClipboard(
                JSON.stringify(params, null, 2), `${n} parameters copied`);
        }

        // Reuse the already-fetched payload instead of re-requesting the batch.
        const loadBtn = document.getElementById('batchLoadBtn');
        if (loadBtn) {
            loadBtn.onclick = () => {
                batchModal.classList.remove('active');
                showLoadBatchModal(optId, batchId, data);
            };
        }
    }

    document.getElementById('closeBatchModal').addEventListener('click', () => batchModal.classList.remove('active'));
    document.getElementById('batchCloseBtnFooter').addEventListener('click', () => batchModal.classList.remove('active'));
    batchModal.addEventListener('click', (e) => { if (e.target === batchModal) batchModal.classList.remove('active'); });

    // =========================================================================
    // HISTORY
    // =========================================================================
    document.getElementById('closeHistoryModal').addEventListener('click', () => historyModal.classList.remove('active'));
    historyModal.addEventListener('click', (e) => { if (e.target === historyModal) historyModal.classList.remove('active'); });

    async function loadHistory() {
        try {
            const res = await fetch('/api/optimizations');
            const data = await res.json();

            if (!data.optimizations || data.optimizations.length === 0) {
                historyList.innerHTML = '<div class="empty-state"><i class="fa-solid fa-folder-open"></i><h3>No Optimizations Yet</h3></div>';
                return;
            }

            historyList.innerHTML = '';
            data.optimizations.forEach(opt => {
                const card = document.createElement('div');
                card.className = 'opt-card';
                const fav = savedUserData.favorites && savedUserData.favorites[`${opt.id}/`];
                let savedInfoHtml = '';
                if (fav) {
                    savedInfoHtml = `
                        <div style="margin: 12px 0 0 0; padding: 8px 12px; background: rgba(245, 158, 11, 0.05); border-left: 2px solid var(--accent-yellow, #f59e0b); border-radius: 0 4px 4px 0; display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap;">
                            <div style="font-size: 12px; color: var(--accent-yellow, #f59e0b); font-weight: 600;">
                                <i class="fa-solid fa-folder"></i> ${escapeHtml(fav.group || '')}
                            </div>
                            <div style="font-size: 12px; color: var(--text-muted);">:</div>
                            <div style="font-size: 12px; color: var(--text-body); font-style: italic;">
                                ${escapeHtml(fav.review || 'No notes')}
                            </div>
                        </div>
                    `;
                }

                card.innerHTML = `
                    <div class="opt-card-header">
                        <div class="opt-card-title">${opt.script_name || opt.id}</div>
                        <span class="opt-card-status ${opt.status}">${opt.status}</span>
                    </div>
                    <div class="opt-card-meta">
                        <span><i class="fa-solid fa-flask"></i> ${opt.mode || ''}</span>
                        <span><i class="fa-solid fa-layer-group"></i> ${opt.completed || 0}/${opt.total || opt.num_iterations || 0}${typeof opt.percent === 'number' ? ` (${opt.percent}%)` : ''}</span>
                        <span><i class="fa-solid fa-hard-drive"></i> ${opt.size_mb || 0} MB</span>
                        <span><i class="fa-regular fa-clock"></i> ${opt.started_at ? new Date(opt.started_at).toLocaleDateString() : ''}</span>
                    </div>
                    ${savedInfoHtml}
                    <div class="opt-card-actions" style="margin-top: 12px;">
                        <button class="btn btn-secondary btn-sm" onclick="openSaveModal('${opt.id}', '')" title="Save / Review" style="color: ${savedUserData.favorites && savedUserData.favorites[`${opt.id}/`] ? 'var(--accent-yellow, #f59e0b)' : 'inherit'};">
                            <i class="${savedUserData.favorites && savedUserData.favorites[`${opt.id}/`] ? 'fa-solid' : 'fa-regular'} fa-star"></i> Save
                        </button>
                        ${opt.resumable ? `
                        <button class="btn btn-success btn-sm resume-opt-btn" data-id="${opt.id}"
                                title="Continue this run — at least ${opt.remaining || 0} batches still have no result">
                            <i class="fa-solid fa-play"></i> Resume
                        </button>` : ''}
                        <button class="btn btn-secondary btn-sm edit-opt-btn" data-id="${opt.id}"
                                title="Load this run's settings and parameter ranges into the optimizer">
                            <i class="fa-solid fa-pen-to-square"></i> Edit
                        </button>
                        <button class="btn btn-secondary btn-sm" onclick="shareRun('${opt.id}')"
                                title="Copy a link that opens this run's results">
                            <i class="fa-solid fa-link"></i> Share
                        </button>
                        <button class="btn btn-secondary btn-sm view-opt-btn" data-id="${opt.id}">
                            <i class="fa-solid fa-eye"></i> View Results
                        </button>
                        <button class="btn btn-danger btn-sm delete-opt-btn" data-id="${opt.id}">
                            <i class="fa-solid fa-trash"></i> Delete
                        </button>
                    </div>
                `;
                historyList.appendChild(card);
            });

            // Wire up buttons
            historyList.querySelectorAll('.resume-opt-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    resumeOptimization(btn.dataset.id);
                });
            });

            historyList.querySelectorAll('.edit-opt-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    editOptimization(btn.dataset.id);
                });
            });

            historyList.querySelectorAll('.view-opt-btn').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const id = btn.dataset.id;
                    historyModal.classList.remove('active');
                    await loadResults(id);
                    switchTab('results');
                });
            });

            historyList.querySelectorAll('.delete-opt-btn').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const id = btn.dataset.id;
                    if (confirm(`Delete optimization "${id}" and all its data?`)) {
                        try {
                            await fetch(`/api/optimizations/${id}`, { method: 'DELETE' });
                            showToast('Optimization deleted', 'success');
                            loadHistory();
                        } catch (e) {
                            showToast('Delete failed', 'error');
                        }
                    }
                });
            });

        } catch (e) {
            showToast('Failed to load history', 'error');
        }
    }

    // =========================================================================
    // CHARTING — TradingView Lightweight Charts
    // =========================================================================
    function initChart() {
        if (lwChart) {
            lwChart.remove();
            lwChart = null;
            candleSeries = null;
        }

        lwChart = LightweightCharts.createChart(chartCanvas, {
            width: chartCanvas.clientWidth,
            height: chartCanvas.clientHeight,
            layout: {
                background: { color: '#0a0e17' },
                textColor: '#94a3b8',
                fontFamily: 'Inter, sans-serif',
                fontSize: 11,
            },
            grid: {
                vertLines: { color: '#1e293b' },
                horzLines: { color: '#1e293b' },
            },
            crosshair: {
                mode: 0,
                vertLine: { color: '#475569', width: 1, style: 2 },
                horzLine: { color: '#475569', width: 1, style: 2 },
            },
            rightPriceScale: {
                borderColor: '#1e293b',
                textColor: '#94a3b8',
            },
            timeScale: {
                borderColor: '#1e293b',
                timeVisible: true,
                secondsVisible: false,
            },
        });

        candleSeries = lwChart.addCandlestickSeries({
            upColor: '#22c55e',
            downColor: '#ef4444',
            borderUpColor: '#22c55e',
            borderDownColor: '#ef4444',
            wickUpColor: '#22c55e',
            wickDownColor: '#ef4444',
        });
    }

    function resizeChart() {
        if (lwChart && chartCanvas) {
            lwChart.resize(chartCanvas.clientWidth, chartCanvas.clientHeight);
        }
    }

    window.addEventListener('resize', resizeChart);

    async function loadChartData(dataPath, tf) {
        if (!dataPath) return;

        initChart();
        chartSymbolLabel.textContent = 'Loading...';

        try {
            const params = new URLSearchParams({ data_path: dataPath, timeframe: tf });
            const res = await fetch(`/api/chart/ohlc?${params}`);
            const data = await res.json();

            if (data.status === 'success' && data.data.length > 0) {
                candleSeries.setData(data.data);
                lwChart.timeScale().fitContent();
                chartSymbolLabel.textContent = dataPath.split(/[/\\]/).pop();
                chartTimeframeLabel.textContent = data.timeframe;
            } else {
                chartSymbolLabel.textContent = 'No data';
            }
        } catch (e) {
            chartSymbolLabel.textContent = 'Load failed';
            showToast('Failed to load chart data', 'error');
        }
    }

    chartTfSelect.addEventListener('change', () => {
        if (currentDataPath) loadChartData(currentDataPath, chartTfSelect.value);
    });

    chartFitBtn.addEventListener('click', () => {
        if (lwChart) lwChart.timeScale().fitContent();
    });

    // =========================================================================
    // UTILITIES
    // =========================================================================
    function formatValue(v) {
        if (v === null || v === undefined) return '—';
        if (typeof v === 'number') {
            if (Number.isInteger(v)) return v.toLocaleString();
            return v.toFixed(2);
        }
        if (typeof v === 'boolean') return v ? 'True' : 'False';
        if (typeof v === 'object') return JSON.stringify(v);
        return String(v);
    }

    function roundSmart(val, type) {
        if (type === 'int') return Math.round(val);
        return Math.round(val * 100) / 100;
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    /**
     * escapeHtml() leaves quotes alone, which is fine in text nodes but breaks
     * out of a double-quoted attribute. Python tracebacks routinely contain
     * quotes, so anything going into title="..." needs this instead.
     */
    function escapeAttr(str) {
        return escapeHtml(str == null ? '' : String(str))
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // =========================================================================
    // SHARING — clipboard + deep links
    // =========================================================================

    /** Copy text, with a fallback for contexts without the async clipboard. */
    async function copyToClipboard(text, label = 'Copied') {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
            } else {
                // Plain http over a LAN address has no async clipboard API.
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.setAttribute('readonly', '');
                ta.style.cssText = 'position:fixed;top:-1000px;opacity:0;';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                ta.remove();
            }
            showToast(`${label} to clipboard`, 'success');
            return true;
        } catch (e) {
            showToast(`Could not copy: ${e.message}`, 'error');
            return false;
        }
    }

    /**
     * A link that reopens this exact view.
     *
     * The target lives in the URL hash, so one link works from localhost, a LAN
     * address or an ngrok tunnel without any server-side routing.
     */
    function buildShareLink(hash) {
        return `${location.origin}${location.pathname}#${hash}`;
    }

    window.shareRun = (optId) =>
        copyToClipboard(buildShareLink(`run=${encodeURIComponent(optId)}`), 'Run link copied');

    window.shareRuns = (ids) =>
        copyToClipboard(buildShareLink(`runs=${ids.map(encodeURIComponent).join(',')}`),
                        `Link to ${ids.length} runs copied`);

    window.shareBatch = (optId, batchId) =>
        copyToClipboard(
            buildShareLink(`batch=${encodeURIComponent(optId)}/${encodeURIComponent(batchId)}`),
            'Batch link copied');

    window.shareFolder = (name) =>
        copyToClipboard(buildShareLink(`folder=${encodeURIComponent(name)}`), 'Folder link copied');

    /** Share whatever the Results tab is currently showing. */
    window.shareCurrentResults = () => {
        if (!selectedRunIds.length) {
            showToast('Nothing loaded to share yet', 'warning');
            return;
        }
        return selectedRunIds.length === 1
            ? window.shareRun(selectedRunIds[0])
            : window.shareRuns(selectedRunIds);
    };

    /** Open whatever a shared link points at. */
    async function applyDeepLink() {
        const raw = (location.hash || '').replace(/^#/, '');
        const eq = raw.indexOf('=');
        if (eq < 1) return;

        const key = raw.slice(0, eq);
        const rawValue = raw.slice(eq + 1);
        if (!rawValue) return;

        const dec = (s) => { try { return decodeURIComponent(s); } catch (e) { return s; } };

        try {
            if (key === 'run') {
                await loadResultsFor([dec(rawValue)]);
                switchTab('results');
            } else if (key === 'runs') {
                const ids = rawValue.split(',').map(s => dec(s.trim())).filter(Boolean);
                if (ids.length) {
                    await loadResultsFor(ids);
                    switchTab('results');
                }
            } else if (key === 'batch') {
                // optimisation ids never contain a slash, so the first one splits.
                const slash = rawValue.indexOf('/');
                if (slash < 0) return;
                const optId = dec(rawValue.slice(0, slash));
                const batchId = dec(rawValue.slice(slash + 1));
                if (!optId || !batchId) return;
                await loadResultsFor([optId]);
                switchTab('results');
                viewBatch(optId, batchId);
            } else if (key === 'folder') {
                switchTab('saved');
                renderSavedBacktests(dec(rawValue));
            }
        } catch (e) {
            showToast(`Could not open that link: ${e.message}`, 'error');
        }
    }

    function setGlobalStatus(state, text) {
        globalStatusDot.className = 'status-dot ' + state;
        globalStatusText.textContent = text;
    }

    function showToast(message, type = 'info') {
        const container = document.getElementById('toastContainer');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;

        const icons = { success: 'fa-check-circle', error: 'fa-times-circle', warning: 'fa-exclamation-triangle', info: 'fa-info-circle' };
        toast.innerHTML = `
            <i class="fa-solid ${icons[type] || icons.info} toast-icon"></i>
            <span class="toast-message">${message}</span>
        `;

        container.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(100%)';
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    }

    // =========================================================================
    // EXCEL VIEWER MODAL
    // =========================================================================
    window.viewExcel = async function(optId, batchId) {
        const modal = document.getElementById('excelModal');
        const tabsContainer = document.getElementById('excelTabs');
        const contentContainer = document.getElementById('excelContent');
        
        modal.classList.add('active');
        tabsContainer.innerHTML = '';
        contentContainer.innerHTML = '<div style="padding:20px;text-align:center;"><div class="spinner" style="margin:0 auto 10px;"></div><p>Loading Excel report...</p></div>';

        try {
            const res = await fetch(`/api/excel-viewer/${optId}/${batchId}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            
            tabsContainer.innerHTML = '';
            contentContainer.innerHTML = '';
            
            if (!data.sheets || Object.keys(data.sheets).length === 0) {
                contentContainer.innerHTML = '<div style="padding:20px;">No sheets found or unable to parse.</div>';
                return;
            }

            let first = true;
            for (const [sheetName, htmlTable] of Object.entries(data.sheets)) {
                // Tab button
                const tab = document.createElement('div');
                tab.className = `excel-tab ${first ? 'active' : ''}`;
                tab.textContent = sheetName;
                
                // Sheet content
                const sheet = document.createElement('div');
                sheet.className = `excel-sheet ${first ? 'active' : ''}`;
                sheet.innerHTML = htmlTable;

                // Tab click handler
                tab.onclick = () => {
                    document.querySelectorAll('.excel-tab').forEach(t => t.classList.remove('active'));
                    document.querySelectorAll('.excel-sheet').forEach(s => s.classList.remove('active'));
                    tab.classList.add('active');
                    sheet.classList.add('active');
                };

                tabsContainer.appendChild(tab);
                contentContainer.appendChild(sheet);
                first = false;
            }
        } catch (e) {
            contentContainer.innerHTML = `<div style="padding:20px;color:#ef4444;"><i class="fa-solid fa-circle-exclamation"></i> Error loading Excel: ${e.message}</div>`;
        }
    };

    window.closeExcelModal = function() {
        document.getElementById('excelModal').classList.remove('active');
    };

    // =========================================================================
    // LOAD & EDIT OPTIMIZATION
    // =========================================================================
    /** Set one sidebar field and let its listeners react. */
    function setFieldValue(id, value) {
        if (value === undefined || value === null || value === '') return;
        const el = document.getElementById(id);
        if (!el) return;
        el.value = value;
        el.dispatchEvent(new Event('change'));
    }

    /**
     * Restore the Optimization Settings header: mode, iterations, parallel
     * workers, seed, ranking metric, top N, and the drawdown options.
     */
    function applyRunSettings(config) {
        setFieldValue('optModeSelect', config.mode);
        setFieldValue('iterationsInput', config.num_iterations);
        setFieldValue('workersInput', config.num_workers);
        setFieldValue('seedInput', config.seed);
        setFieldValue('rankingSelect', config.ranking_metric);
        setFieldValue('topNInput', config.top_n);
        // Dispatching change on the mode select toggles the auto/manual panels.
        setFieldValue('ddOptMode', config.drawdown_optimization);
        setFieldValue('ddMinTpd', config.dd_min_trades_per_day);
        setFieldValue('ddTargetTpd', config.dd_target_trades_per_day);
    }

    /** Restore one parameter card: its value, and whether it is being swept. */
    function applyParamToForm(p, paramValues, config, optimizeParams) {
        const name = p.name;

        // Prefer the value supplied by the caller (a batch's own parameters),
        // then the run's fixed params, then the script's current default.
        let value = paramValues[name];
        if (value === undefined && config.fixed_params) value = config.fixed_params[name];

        if (value !== undefined) {
            const el = document.querySelector(`.param-fixed-value[data-name="${name}"]`);
            if (el) {
                if (el.tagName === 'SELECT') {
                    el.value = String(value);            // bool cards use a select
                } else if (value !== null && typeof value === 'object') {
                    el.value = JSON.stringify(value);    // dict / list params
                } else {
                    el.value = value;
                }
            }
        }

        const toggle = document.querySelector(`.param-optimize-toggle[data-name="${name}"]`);
        if (!toggle) return;   // fixed-only parameter, e.g. a path

        const spec = optimizeParams[name];
        toggle.checked = Boolean(spec);
        // Let the existing handler show/hide the range row and restyle the card.
        toggle.dispatchEvent(new Event('change'));
        if (!spec) return;

        const card = toggle.closest('.param-card');
        if (!card) return;

        if (Array.isArray(spec.choices)) {
            // Bool sweeps carry choices too, but their range row has no input.
            const choicesEl = card.querySelector('.param-choices');
            if (choicesEl) choicesEl.value = spec.choices.join(', ');
            return;
        }

        const set = (selector, v) => {
            const el = card.querySelector(selector);
            if (el && v !== undefined && v !== null) el.value = v;
        };
        set('.param-range-min', spec.min);
        set('.param-range-max', spec.max);
        set('.param-range-step', spec.step);
    }

    /**
     * Rebuild the whole sidebar form from a saved run.
     *
     * `mode` decides what happens to the parameters the run was sweeping:
     *   'sweep' — put their ranges back, so the same search can run again
     *   'pin'   — freeze every parameter at `paramValues`, to reproduce one
     *             exact backtest (handy for debugging a failed batch)
     *
     * The Optimization Settings header is restored either way.
     */
    async function applyConfigToForm(config, paramValues, mode) {
        const scriptName = config.script_name
            || (config.script_path || '').split(/[\\/]/).pop();
        if (!scriptName) throw new Error('this run has no script recorded');

        const known = Array.from(scriptSelect.options).some(o => o.value === scriptName);
        if (!known) throw new Error(`${scriptName} is no longer in backtests/`);

        // Analyse the script and await it, rather than clicking the button and
        // polling the DOM for the params section to appear.
        scriptSelect.value = scriptName;
        scriptSelect.dispatchEvent(new Event('change'));
        await analyzeScript();
        if (!analysisResult || !analysisResult.parameters) {
            throw new Error('script analysis failed');
        }

        applyRunSettings(config);

        const optimizeParams = (mode === 'sweep' && config.optimize_params) || {};
        analysisResult.parameters.forEach(p =>
            applyParamToForm(p, paramValues || {}, config, optimizeParams)
        );

        updateEstimates();
        updateWfoEstimates();

        // The script may have changed since the run: warn about parameters that
        // no longer exist rather than dropping them silently.
        const current = new Set(analysisResult.parameters.map(p => p.name));
        const missing = Object.keys(optimizeParams).filter(n => !current.has(n));

        return { scriptName, sweptCount: Object.keys(optimizeParams).length, missing };
    }

    /** Report what a load restored, including anything that no longer exists. */
    function reportLoad(info, summary) {
        showToast(`Loaded ${info.scriptName} — ${summary}`, 'success');
        if (info.missing.length) {
            showToast(
                `${info.scriptName} no longer has: ${info.missing.join(', ')} — skipped`,
                'warning'
            );
        }
    }

    // ---- "Load into Optimizer" modal (batch level) --------------------------
    let loadBatchModal = null;

    function ensureLoadBatchModal() {
        if (loadBatchModal) return loadBatchModal;

        loadBatchModal = document.createElement('div');
        loadBatchModal.className = 'modal-overlay';
        loadBatchModal.id = 'loadBatchModal';
        loadBatchModal.innerHTML = `
            <div class="modal-card" style="max-width: 560px;">
                <div class="modal-header">
                    <span class="modal-title">Load into Optimizer</span>
                    <button class="modal-close" id="closeLoadBatchModal"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div class="modal-body" id="loadBatchModalBody"></div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" id="loadBatchCancelBtn">Cancel</button>
                    <button class="btn btn-secondary" id="loadBatchSweepBtn">
                        <i class="fa-solid fa-arrows-left-right"></i> Re-run this sweep
                    </button>
                    <button class="btn btn-primary" id="loadBatchPinBtn">
                        <i class="fa-solid fa-thumbtack"></i> Reproduce this batch
                    </button>
                </div>
            </div>`;
        document.body.appendChild(loadBatchModal);

        const close = () => loadBatchModal.classList.remove('active');
        loadBatchModal.querySelector('#closeLoadBatchModal').addEventListener('click', close);
        loadBatchModal.querySelector('#loadBatchCancelBtn').addEventListener('click', close);
        loadBatchModal.addEventListener('click', (e) => {
            if (e.target === loadBatchModal) close();
        });
        return loadBatchModal;
    }

    /** The Optimization Settings a load will restore, as label/value rows. */
    function configSummaryHtml(config, heading) {
        const swept = Object.keys(config.optimize_params || {});
        const row = (label, value) =>
            `<div class="metric-row"><span class="label">${escapeHtml(label)}</span>` +
            `<span class="value">${escapeHtml(String(value))}</span></div>`;

        return `
            <div class="batch-detail-section">
                <h4><i class="fa-solid fa-gears" style="color:var(--accent-blue);margin-right:4px;"></i> ${escapeHtml(heading)}</h4>
                ${row('Script', config.script_name || '—')}
                ${row('Optimization Mode', config.mode || '—')}
                ${row('Iterations', config.num_iterations ?? '—')}
                ${row('Parallel Workers', config.num_workers ?? '—')}
                ${row('Random Seed', config.seed ?? '—')}
                ${row('Ranking Metric', config.ranking_metric || '—')}
                ${row('Top N Results', config.top_n ?? '—')}
                ${row('Drawdown Optimization', config.drawdown_optimization || 'disabled')}
                ${row('Swept parameters', swept.length ? swept.join(', ') : 'none')}
            </div>`;
    }

    function showLoadBatchModal(optId, batchId, data) {
        const modal = ensureLoadBatchModal();
        const config = data.config || {};
        const swept = Object.keys(config.optimize_params || {});

        let outcome = 'never ran';
        if (data.metrics && data.metrics.status) {
            outcome = data.metrics.status.ok ? 'succeeded' : 'failed';
        }

        modal.querySelector('#loadBatchModalBody').innerHTML = `
            <div style="margin-bottom:12px; font-size:12px; color:var(--text-muted);">
                Restoring <strong style="color:var(--text-heading);">${escapeHtml(batchId)}</strong>
                from <strong style="color:var(--text-heading);">${escapeHtml(optId)}</strong>
                — this batch <strong style="color:var(--text-heading);">${outcome}</strong>.
            </div>
            ${configSummaryHtml(config, 'Settings to restore')}
            <div style="margin-top:12px; font-size:11px; color:var(--text-muted); line-height:1.7;">
                <strong style="color:var(--text-body);">Re-run this sweep</strong> —
                restores the ranges above so the same search runs again.<br>
                <strong style="color:var(--text-body);">Reproduce this batch</strong> —
                pins every parameter to this batch's exact values for a single run.
            </div>`;

        const sweepBtn = modal.querySelector('#loadBatchSweepBtn');
        const pinBtn = modal.querySelector('#loadBatchPinBtn');
        sweepBtn.disabled = swept.length === 0;
        sweepBtn.title = swept.length ? '' : 'This run had no swept parameters';

        // Assigned, not added, so reopening the modal never stacks handlers.
        const run = async (mode) => {
            modal.classList.remove('active');
            try {
                const info = await applyConfigToForm(config, data.params || {}, mode);
                reportLoad(info, mode === 'sweep'
                    ? `${info.sweptCount} parameter(s) ready to sweep`
                    : `pinned to ${batchId}`);
            } catch (e) {
                showToast(`Load failed: ${e.message}`, 'error');
            }
        };
        sweepBtn.onclick = () => run('sweep');
        pinBtn.onclick = () => run('pin');

        modal.classList.add('active');
    }

    /** Load one batch — successful, failed, or never run — back into the form. */
    window.loadOptimization = async function(optId, batchId) {
        try {
            const res = await fetch(`/api/optimizations/${optId}/batch/${batchId}`);
            if (!res.ok) throw new Error(`batch ${batchId} not found`);
            const data = await res.json();

            batchModal.classList.remove('active');
            historyModal.classList.remove('active');
            showLoadBatchModal(optId, batchId, data);
        } catch (e) {
            showToast(`Failed to load batch: ${e.message}`, 'error');
        }
    };

    /**
     * Load a whole run's setup from the History list — every Optimization
     * Setting plus the swept parameter ranges, ready to edit and re-run.
     */
    window.editOptimization = async function(optId) {
        try {
            const res = await fetch(`/api/optimizations/${optId}/config`);
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                throw new Error(body.detail || `no saved configuration for ${optId}`);
            }
            const config = (await res.json()).config || {};

            historyModal.classList.remove('active');
            const info = await applyConfigToForm(config, config.fixed_params || {}, 'sweep');
            reportLoad(info, info.sweptCount
                ? `${info.sweptCount} parameter(s) ready to sweep`
                : 'settings restored');
        } catch (e) {
            showToast(`Edit failed: ${e.message}`, 'error');
        }
    };

    // =========================================================================
    // SAVED BACKTESTS & GROUPS
    // =========================================================================
    window.openSaveModal = function(optId, batchId) {
        document.getElementById('saveOptId').value = optId;
        document.getElementById('saveBatchId').value = batchId;
        
        const key = `${optId}/${batchId}`;
        const existing = savedUserData.favorites && savedUserData.favorites[key];
        
        // Populate select options
        const select = document.getElementById('saveGroupSelect');
        select.innerHTML = '<option value="">Select Existing Group...</option>';
        if (savedUserData.groups) {
            savedUserData.groups.forEach(g => {
                const opt = document.createElement('option');
                opt.value = g;
                opt.textContent = g;
                select.appendChild(opt);
            });
        }
        
        if (existing) {
            if (savedUserData.groups.includes(existing.group)) {
                select.value = existing.group;
                document.getElementById('saveGroupNew').value = '';
            } else {
                select.value = '';
                document.getElementById('saveGroupNew').value = existing.group || '';
            }
            document.getElementById('saveReview').value = existing.review || '';
        } else {
            select.value = '';
            document.getElementById('saveGroupNew').value = '';
            document.getElementById('saveReview').value = '';
        }
        
        loadReviewParams(optId, batchId);
        document.getElementById('saveModal').classList.add('active');
    };

    // The batch's parameters, held so Save can store them with the review.
    let reviewParams = {};

    /**
     * Show the batch's complete parameter set in the review modal, ready to copy.
     *
     * params.json is the merged set the batch actually ran with — the fixed
     * parameters and its point in the sweep together — so nothing is missing.
     */
    async function loadReviewParams(optId, batchId) {
        const block = document.getElementById('saveParamsBlock');
        const preview = document.getElementById('saveParamsPreview');
        const count = document.getElementById('saveParamsCount');
        const btn = document.getElementById('copyParamsBtn');
        if (!block || !preview || !btn) return;

        reviewParams = {};
        if (!batchId) {
            // A whole run has no single parameter set.
            block.classList.add('hidden');
            return;
        }

        block.classList.remove('hidden');
        preview.textContent = 'Loading parameters…';
        if (count) count.textContent = '';
        btn.disabled = true;

        try {
            const res = await fetch(`/api/optimizations/${optId}/batch/${batchId}`);
            if (!res.ok) throw new Error(`batch ${batchId} not found`);
            const data = await res.json();

            reviewParams = data.params || {};
            const text = JSON.stringify(reviewParams, null, 2);
            const n = Object.keys(reviewParams).length;

            preview.textContent = text;
            if (count) count.textContent = `(${n})`;
            btn.disabled = n === 0;
            btn.onclick = () => copyToClipboard(text, `${n} parameters copied`);
        } catch (e) {
            preview.textContent = `Could not load parameters: ${e.message}`;
            if (count) count.textContent = '';
        }
    }

    window.closeSaveModal = function() {
        document.getElementById('saveModal').classList.remove('active');
    };

    window.submitSaveBacktest = async function() {
        const optId = document.getElementById('saveOptId').value;
        const batchId = document.getElementById('saveBatchId').value;
        const review = document.getElementById('saveReview').value;
        const groupSel = document.getElementById('saveGroupSelect').value;
        const groupNew = document.getElementById('saveGroupNew').value;
        const group = groupNew.trim() || groupSel;
        
        if (!group) {
            showToast('Please select or enter a group name.', 'warning');
            return;
        }

        // We need to fetch the metrics if they are available
        let metrics = {};
        if (currentResults && currentResults.all_results) {
            const row = currentResults.all_results.find(r => r.batch === batchId);
            if (row) metrics = row.metrics || {};
        }
        
        try {
            const res = await fetch('/api/user-data/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    opt_id: optId,
                    batch_id: batchId,
                    group: group,
                    review: review,
                    metrics: metrics,
                    // Kept with the review so the entry still describes what it
                    // ran even if the run folder is later deleted.
                    params: reviewParams,
                    timestamp: new Date().toISOString()
                })
            });
            
            if (res.ok) {
                showToast('Backtest saved successfully!', 'success');
                closeSaveModal();
                await fetchUserData(); // Refresh global state
                if (currentResults) renderResults(currentResults); // Refresh table stars
                if (document.getElementById('savedTab').classList.contains('active')) {
                    renderSavedBacktests();
                }
            } else {
                throw new Error('Server error');
            }
        } catch (e) {
            showToast('Failed to save backtest: ' + e.message, 'error');
        }
    };

    window.deleteSavedBacktest = async function(optId, batchId) {
        if (!confirm('Are you sure you want to remove this backtest from your saved favorites?')) return;
        try {
            const res = await fetch('/api/user-data/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ opt_id: optId, batch_id: batchId })
            });
            if (res.ok) {
                showToast('Backtest removed from favorites.', 'info');
                await fetchUserData();
                if (currentResults) renderResults(currentResults);
                renderSavedBacktests();
            }
        } catch (e) {
            showToast('Failed to delete saved backtest.', 'error');
        }
    };

    window.deleteGroup = async function(groupName) {
        if (!confirm(`Are you sure you want to delete the group "${groupName}" and ALL its saved backtests?`)) return;
        try {
            const res = await fetch('/api/user-data/group/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ group: groupName })
            });
            if (res.ok) {
                showToast('Group deleted.', 'info');
                await fetchUserData();
                if (currentResults) renderResults(currentResults);
                renderSavedBacktests();
            }
        } catch (e) {
            showToast('Failed to delete group.', 'error');
        }
    };

    /**
     * @param {string} [selectGroup] open this folder instead of the first one —
     *                 used when a shared folder link is followed.
     */
    window.renderSavedBacktests = function(selectGroup) {
        const groupsList = document.getElementById('groupsList');
        groupsList.innerHTML = '';

        if (!savedUserData.groups || savedUserData.groups.length === 0) {
            groupsList.innerHTML = '<div style="color:var(--text-secondary); font-size:12px;">No groups yet.</div>';
            renderSavedGroup(null);
            return;
        }

        const wanted = savedUserData.groups.includes(selectGroup) ? selectGroup : null;

        savedUserData.groups.forEach((g, i) => {
            const btn = document.createElement('div');
            btn.className = 'group-btn';
            btn.style.padding = '8px 12px';
            btn.style.margin = '4px 0';
            btn.style.borderRadius = '4px';
            btn.style.cursor = 'pointer';
            btn.style.fontSize = '14px';
            btn.style.color = 'var(--text-body)';
            btn.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span><i class="fa-solid fa-folder" style="color:var(--accent-blue); margin-right:8px;"></i> ${g}</span>
                    <i class="fa-solid fa-trash-can" onclick="event.stopPropagation(); deleteGroup('${escapeHtml(g)}')" style="color:var(--text-muted); padding:4px;" title="Delete Group"></i>
                </div>
            `;
            
            btn.onclick = () => {
                document.querySelectorAll('.group-btn').forEach(b => {
                    b.style.background = 'transparent';
                    b.style.fontWeight = '400';
                });
                btn.style.background = 'var(--bg-panel)';
                btn.style.fontWeight = '600';
                renderSavedGroup(g);
            };
            
            groupsList.appendChild(btn);

            // Auto-select: the requested folder, else the first one as before.
            if (wanted ? g === wanted : i === 0) btn.click();
        });
    };

    /**
     * Open every optimization saved in a folder together in the Results tab.
     *
     * A folder can hold whole runs and individual batches; both carry their
     * run id, so the distinct set of those is what gets compared.
     */
    window.compareGroupInResults = async function(groupName) {
        const ids = [...new Set(Object.values(savedUserData.favorites || {})
            .filter(f => f.group === groupName)
            .map(f => f.opt_id)
            .filter(Boolean))];

        if (!ids.length) {
            showToast(`"${groupName}" has nothing saved in it yet`, 'warning');
            return;
        }

        ids.forEach(id => rememberRun(id, {}));
        await loadResultsFor(ids);
        switchTab('results');
        showToast(
            ids.length === 1
                ? `Opened the one optimization saved in "${groupName}"`
                : `Comparing ${ids.length} optimizations from "${groupName}"`,
            'success');
    };

    window.createNewGroup = async function() {
        const groupName = prompt("Enter new group name:");
        if (groupName && groupName.trim()) {
            try {
                const res = await fetch('/api/user-data/group', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ group: groupName.trim() })
                });
                if (res.ok) {
                    showToast('Group created!', 'success');
                    await fetchUserData();
                    if (document.getElementById('savedTab').classList.contains('active')) {
                        renderSavedBacktests();
                    }
                }
            } catch (e) {
                showToast('Failed to create group', 'error');
            }
        }
    };

    window.viewOptResults = async function(id) {
        await loadResults(id);
        switchTab('results');
    };

    function renderSavedGroup(groupName) {
        const container = document.getElementById('savedBacktestsContainer');
        const title = document.getElementById('currentGroupTitle');
        container.innerHTML = '';
        title.textContent = groupName ? groupName : 'All Saved';

        // Folder-level actions follow whichever folder is open.
        const compareBtn = document.getElementById('compareGroupBtn');
        const shareBtn = document.getElementById('shareGroupBtn');
        if (compareBtn) {
            compareBtn.disabled = !groupName;
            compareBtn.onclick = groupName ? () => compareGroupInResults(groupName) : null;
        }
        if (shareBtn) {
            shareBtn.disabled = !groupName;
            shareBtn.onclick = groupName ? () => window.shareFolder(groupName) : null;
        }

        if (!groupName) return;

        const favorites = Object.values(savedUserData.favorites || {}).filter(f => f.group === groupName);
        if (favorites.length === 0) {
            container.innerHTML = '<div style="color:var(--text-secondary);">No backtests saved in this group.</div>';
            return;
        }
        
        favorites.forEach(fav => {
            const card = document.createElement('div');
            card.style.background = 'var(--bg-card)';
            card.style.border = '1px solid var(--border-color)';
            card.style.borderRadius = '8px';
            card.style.padding = '16px';
            card.style.display = 'flex';
            card.style.flexDirection = 'column';
            card.style.gap = '12px';
            
            let metricsHtml = '';
            if (fav.metrics && Object.keys(fav.metrics).length > 0) {
                const profit = fav.metrics['Net Profit'] || 0;
                const pColor = profit > 0 ? 'var(--accent-green)' : (profit < 0 ? 'var(--accent-red)' : 'var(--text-body)');
                metricsHtml = `
                    <div style="background:var(--bg-panel); padding:8px; border-radius:4px;">
                        <div style="display:flex; justify-content:space-between; font-size:12px;">
                            <span style="color:var(--text-secondary);">Net Profit:</span>
                            <span style="color:${pColor}; font-weight:600;">$${profit.toFixed(2)}</span>
                        </div>
                        <div style="display:flex; justify-content:space-between; font-size:12px;">
                            <span style="color:var(--text-secondary);">Trades:</span>
                            <span>${fav.metrics['Total Trades'] || 0}</span>
                        </div>
                    </div>
                `;
            }
            
            let actionsHtml = '';
            if (fav.batch_id) {
                actionsHtml = `
                    <button class="btn btn-secondary" style="flex:1; padding:4px;" onclick="openSaveModal('${fav.opt_id}', '${fav.batch_id}')" title="Edit Review"><i class="fa-solid fa-pen"></i></button>
                    <button class="btn btn-secondary" style="flex:1; padding:4px;" onclick="viewBatchChart('${fav.opt_id}', '${fav.batch_id}')" title="View Chart"><i class="fa-solid fa-chart-line"></i></button>
                    <button class="btn btn-secondary" style="flex:1; padding:4px;" onclick="viewExcel('${fav.opt_id}', '${fav.batch_id}')" title="View Excel"><i class="fa-solid fa-table"></i></button>
                    <button class="btn btn-success" style="flex:1; padding:4px;" onclick="loadOptimization('${fav.opt_id}', '${fav.batch_id}')" title="Load Params"><i class="fa-solid fa-rocket"></i></button>
                `;
            } else {
                actionsHtml = `
                    <button class="btn btn-secondary" style="flex:1; padding:4px;" onclick="openSaveModal('${fav.opt_id}', '')" title="Edit Review"><i class="fa-solid fa-pen"></i></button>
                    <button class="btn btn-success" style="flex:1; padding:4px;" onclick="viewOptResults('${fav.opt_id}')" title="View Results"><i class="fa-solid fa-eye"></i> View Results</button>
                `;
            }
            
            card.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:start;">
                    <div style="font-size:11px; color:var(--accent-blue); font-family:monospace;">
                        ${fav.batch_id ? fav.batch_id : 'Optimization Run'}
                    </div>
                    <button class="cell-action-btn" onclick="deleteSavedBacktest('${fav.opt_id}', '${fav.batch_id}')" title="Remove from Saved">
                        <i class="fa-solid fa-trash" style="color:var(--accent-red); font-size:12px;"></i>
                    </button>
                </div>
                <div style="font-size:12px; color:var(--text-secondary); word-break:break-all;">
                    ${fav.opt_id}
                </div>
                <div style="font-size:13px; color:var(--text-body); background:var(--bg-panel); padding:8px; border-radius:4px; font-style:italic;">
                    "${escapeHtml(fav.review || 'No notes')}"
                </div>
                ${metricsHtml}
                <div style="display:flex; gap:4px; margin-top:auto;">
                    ${actionsHtml}
                </div>
            `;
            container.appendChild(card);
        });
    }

    // =========================================================================
    // WALK-FORWARD TESTING MODULE
    // =========================================================================
    class WalkForwardManager {
        constructor() {
            this.analysisData = null;
            this.wfoWindows = [];
            this.currentRunId = null;
            this.sseConnection = null;
            this.setupEventListeners();
        }

        setupEventListeners() {
            // Mode toggle
            document.querySelectorAll('.wfo-mode-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    document.querySelectorAll('.wfo-mode-btn').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    const desc = document.getElementById('wfoModeDesc');
                    if (btn.dataset.mode === 'expanding') {
                        desc.textContent = 'Expanding: IS start is anchored. IS end extends forward by step size each iteration.';
                    } else {
                        desc.textContent = 'Rolling: IS window slides forward by step size. IS length stays fixed.';
                    }
                });
            });

            // Preview button
            document.getElementById('wfoPreviewBtn').addEventListener('click', () => this.generatePreview());

            // Start button
            document.getElementById('wfoStartBtn').addEventListener('click', () => this.startWFO());

            // Stop button
            document.getElementById('wfoStopBtn').addEventListener('click', () => this.stopWFO());

            // Enable preview when analysis is available
            const origAnalyze = window._originalAnalyzeCallback;
        }

        updateFromAnalysis(data) {
            this.analysisData = data;
            const previewBtn = document.getElementById('wfoPreviewBtn');
            previewBtn.disabled = false;

            // Show compatibility
            const compat = data.wfo_compatibility;
            if (compat) {
                const el = document.getElementById('wfoCompat');
                el.style.display = 'block';
                el.innerHTML = `
                    <div class="wfo-compat-item"><span class="${compat.has_start_date ? 'check' : 'cross'}">${compat.has_start_date ? '✓' : '✗'}</span> start_date parameter ${compat.start_date_param ? '(' + compat.start_date_param + ')' : ''}</div>
                    <div class="wfo-compat-item"><span class="${compat.has_end_date ? 'check' : 'cross'}">${compat.has_end_date ? '✓' : '✗'}</span> end_date parameter ${compat.end_date_param ? '(' + compat.end_date_param + ')' : ''}</div>
                    <div class="wfo-compat-item"><span class="${compat.has_data_path ? 'check' : 'cross'}">${compat.has_data_path ? '✓' : '✗'}</span> Data path ${compat.data_path_param ? '(' + compat.data_path_param + ')' : ''}</div>
                    <div class="wfo-compat-item"><span class="${compat.has_timeframe ? 'check' : 'cross'}">${compat.has_timeframe ? '✓' : '✗'}</span> Timeframe ${compat.timeframe_param ? '(' + compat.timeframe_param + ')' : ''}</div>
                    <div class="wfo-compat-result ${compat.compatible ? 'yes' : 'no'}">${compat.compatible ? '✓ WFO Compatible' : '✗ Not WFO Compatible — requires start_date and end_date parameters'}</div>
                `;
            }

            // Auto-detect data range
            if (data.parameters) {
                const dataParam = data.parameters.find(p => {
                    const n = p.name.toLowerCase();
                    return (p.type === 'path' || (typeof p.value === 'string' && (p.value.includes('.csv') || p.value.includes('.parquet')))) &&
                           (n.includes('data') || n.includes('tick') || n.includes('path'));
                });
                if (dataParam && dataParam.value) {
                    this.detectDataRange(dataParam.value);
                }
            }
        }

        async detectDataRange(dataPath) {
            try {
                const res = await fetch(`/api/data/range?data_path=${encodeURIComponent(dataPath)}`);
                const data = await res.json();
                if (data.status === 'success') {
                    document.getElementById('wfoStartDate').value = data.start;
                    document.getElementById('wfoEndDate').value = data.end;
                    if (window.showToast) window.showToast(`Dataset range detected: ${data.start} to ${data.end}`, 'success');
                }
            } catch (e) {
                console.warn('Could not detect data range:', e);
            }
        }

        getConfig() {
            if (!this.analysisData) return null;
            const compat = this.analysisData.wfo_compatibility || {};
            const fixedParams = {};
            const optimizeParams = {};

            if (this.analysisData.parameters) {
                this.analysisData.parameters.forEach(p => {
                    const el = document.querySelector(`.param-fixed-value[data-name="${p.name}"]`);
                    if (!el) {
                        fixedParams[p.name] = p.value;
                        return;
                    }
                    const mode = el.closest('.param-card')?.classList.contains('optimizing') ? 'optimize' : 'fixed';
                    if (mode === 'fixed') {
                        let val = el.value;
                        if (p.type === 'int') val = parseInt(val);
                        else if (p.type === 'float') val = parseFloat(val);
                        else if (p.type === 'bool') val = el.value === 'true';
                        else if (p.type === 'dict' || p.type === 'list') {
                            try { val = JSON.parse(val); } catch(e) { /* keep as string */ }
                        }
                        fixedParams[p.name] = val;
                    } else {
                        const minEl = document.querySelector(`.param-range-min[data-name="${p.name}"]`);
                        const maxEl = document.querySelector(`.param-range-max[data-name="${p.name}"]`);
                        const stepEl = document.querySelector(`.param-range-step[data-name="${p.name}"]`);
                        if (minEl && maxEl) {
                            optimizeParams[p.name] = {
                                min: parseFloat(minEl.value),
                                max: parseFloat(maxEl.value),
                                step: stepEl ? parseFloat(stepEl.value) : 1,
                                type: p.type,
                            };
                        }
                    }
                });
            }

            return {
                strategy_path: this.analysisData.script_path,
                strategy_name: this.analysisData.script_name,
                entry_style: this.analysisData.entry_style,
                config_class_name: this.analysisData.config_class_name || null,
                data_path: compat.data_path_param ? (fixedParams[compat.data_path_param] || '') : '',
                timeframe: compat.timeframe_param ? (fixedParams[compat.timeframe_param] || '15min') : '15min',
                wfo_start: document.getElementById('wfoStartDate').value,
                wfo_end: document.getElementById('wfoEndDate').value,
                window_mode: document.querySelector('.wfo-mode-btn.active')?.dataset.mode || 'rolling',
                is_duration_months: parseInt(document.getElementById('wfoIsDuration').value) || 24,
                oos_duration_months: parseInt(document.getElementById('wfoOosDuration').value) || 6,
                step_duration_months: parseInt(document.getElementById('wfoStepSize').value) || 6,
                optimization_method: document.getElementById('wfoOptMethod').value,
                optimization_iterations: parseInt(document.getElementById('wfoIterations').value) || 1000,
                num_workers: parseInt(document.getElementById('wfoWorkers').value) || 2,
                seed: parseInt(document.getElementById('wfoSeed').value) || 42,
                ranking_metric: document.getElementById('wfoRankingMetric').value,
                fixed_params: fixedParams,
                optimize_params: optimizeParams,
                date_param_style: compat.date_param_style || 'flat',
                date_param_name: compat.date_param_name || '',
                selection_metric: 'composite_score',
                selection_direction: 'max',
                selection_rules: [],
                robustness_weights: {},
                drawdown_optimization: document.getElementById('wfoDdOptMode')?.value || 'disabled',
                dd_min_trades_per_day: parseFloat(document.getElementById('wfoDdMinTpd')?.value) || 2,
                dd_target_trades_per_day: parseFloat(document.getElementById('wfoDdTargetTpd')?.value) || 8,
            };
        }

        async generatePreview() {
            const config = this.getConfig();
            if (!config) { if (window.showToast) window.showToast('Analyze a script first', 'error'); return; }
            if (!config.wfo_start || !config.wfo_end) { if (window.showToast) window.showToast('Set WFO start and end dates', 'error'); return; }

            try {
                const res = await fetch('/api/walk-forward/create', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(config),
                });
                const data = await res.json();
                if (data.status === 'success') {
                    this.wfoWindows = data.windows;
                    this.renderPreview(data);
                    document.getElementById('wfoStartBtn').disabled = false;
                } else {
                    if (window.showToast) window.showToast(data.detail || 'Preview failed', 'error');
                }
            } catch (e) {
                if (window.showToast) window.showToast('Preview request failed', 'error');
            }
        }

        renderPreview(data) {
            const section = document.getElementById('wfoPreviewSection');
            section.style.display = 'block';
            const content = document.getElementById('wfoPreviewContent');

            let html = '';
            if (data.validation_errors && data.validation_errors.length > 0) {
                html += `<div class="wfo-full-sample-warning" style="margin-bottom:12px;"><i class="fa-solid fa-triangle-exclamation"></i><span class="text">${data.validation_errors.join(' | ')}</span></div>`;
            }
            if (data.dataset_start) {
                html += `<div style="font-size:12px; color:var(--text-secondary); margin-bottom:10px;">Dataset range: <strong>${data.dataset_start}</strong> to <strong>${data.dataset_end}</strong> | Windows: <strong>${data.total_windows}</strong></div>`;
            }

            html += `<table class="wfo-preview-table"><thead><tr><th>Step</th><th>IS Start</th><th>IS End</th><th>OOS Start</th><th>OOS End</th><th>Status</th></tr></thead><tbody>`;
            data.windows.forEach(w => {
                html += `<tr>
                    <td style="text-align:center; font-weight:700;">${w.step}</td>
                    <td class="is-period">${w.is_start}</td>
                    <td class="is-period">${w.is_end}</td>
                    <td class="oos-period">${w.oos_start}</td>
                    <td class="oos-period">${w.oos_end}</td>
                    <td><span class="wfo-status ready">Ready</span></td>
                </tr>`;
            });
            html += `</tbody></table>`;
            content.innerHTML = html;
        }

        async startWFO() {
            console.log('[WFO] startWFO() called');
            const config = this.getConfig();
            console.log('[WFO] config:', config);
            console.log('[WFO] optimize_params:', config ? config.optimize_params : 'null config');
            console.log('[WFO] fixed_params:', config ? config.fixed_params : 'null config');
            if (!config) {
                console.error('[WFO] config is null — analysisData missing?', this.analysisData);
                if (window.showToast) window.showToast('Analyze a script first', 'error');
                return;
            }
            if (Object.keys(config.optimize_params).length === 0) {
                console.error('[WFO] No optimize_params found! All params detected as fixed.');
                if (window.showToast) window.showToast('No parameters set to optimize. Toggle parameters ON in the sidebar and set sweep ranges.', 'error');
                return;
            }

            const timestamp = new Date().toISOString().replace(/[^0-9]/g, '').slice(0, 14);
            this.currentRunId = `${timestamp}_${config.strategy_name.replace('.py', '')}`;
            console.log('[WFO] Starting run:', this.currentRunId);

            try {
                const res = await fetch(`/api/walk-forward/${this.currentRunId}/start`, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(config),
                });
                const data = await res.json();
                console.log('[WFO] Server response:', data);
                if (data.status === 'started') {
                    if (window.showToast) window.showToast(`Walk-Forward started: ${this.currentRunId}`, 'success');
                    document.getElementById('wfoStartBtn').classList.add('hidden');
                    document.getElementById('wfoStopBtn').classList.remove('hidden');
                    document.getElementById('wfoProgressSection').style.display = 'block';
                    this.startProgressSSE();
                } else {
                    if (window.showToast) window.showToast(data.detail || 'Failed to start', 'error');
                }
            } catch (e) {
                console.error('[WFO] Start request failed:', e);
                if (window.showToast) window.showToast('Start request failed: ' + e.message, 'error');
            }
        }

        async stopWFO() {
            if (!this.currentRunId) return;
            try {
                await fetch(`/api/walk-forward/${this.currentRunId}/stop`, { method: 'POST' });
                if (window.showToast) window.showToast('Walk-Forward stopping...', 'info');
            } catch (e) { console.error(e); }
        }

        startProgressSSE() {
            if (this.sseConnection) this.sseConnection.close();
            const runId = this.currentRunId;
            const es = new EventSource(`/api/walk-forward/${runId}/progress`);
            this.sseConnection = es;

            es.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.status === 'done') {
                    es.close();
                    this.sseConnection = null;
                    document.getElementById('wfoStopBtn').classList.add('hidden');
                    document.getElementById('wfoStartBtn').classList.remove('hidden');
                    document.getElementById('wfoStartBtn').disabled = true;
                    this.loadResults(runId);
                    return;
                }
                this.renderProgress(data);
            };

            es.onerror = () => {
                es.close();
                this.sseConnection = null;
            };
        }

        renderProgress(data) {
            const content = document.getElementById('wfoProgressContent');
            const current = data.current_step || 0;
            const total = data.total_steps || 1;
            const pct = Math.round((current / total) * 100);
            const stage = data.stage || '';
            const msg = data.message || '';

            content.innerHTML = `
                <div class="wfo-progress-container">
                    <div class="wfo-progress-header">
                        <h4>${msg}</h4>
                        <span style="color:var(--accent-blue); font-weight:700; font-family:'JetBrains Mono',monospace;">${pct}%</span>
                    </div>
                    <div class="wfo-progress-bar">
                        <div class="wfo-progress-fill" style="width:${pct}%"></div>
                    </div>
                    <div class="wfo-progress-detail">
                        <div class="wfo-progress-stat"><div class="label">Step</div><div class="value">${current} / ${total}</div></div>
                        <div class="wfo-progress-stat"><div class="label">Stage</div><div class="value">${stage}</div></div>
                        <div class="wfo-progress-stat"><div class="label">Status</div><div class="value">${data.status || '—'}</div></div>
                        <div class="wfo-progress-stat"><div class="label">Run ID</div><div class="value" style="font-size:10px;">${data.run_id || '—'}</div></div>
                    </div>
                </div>
            `;
        }

        async loadResults(runId) {
            try {
                const res = await fetch(`/api/walk-forward/${runId}/results`);
                const data = await res.json();
                if (data.status === 'success') {
                    this.renderSteps(data.step_results || []);
                    this.renderSummary(data.summary?.oos_aggregate || {});
                    this.renderRobustness(data.robustness || data.summary?.robustness || {});
                    this.renderStability(data.stability || []);
                    this.renderCandidates(data.candidates || [], runId);
                    this.renderExport(runId);
                }
            } catch (e) {
                console.error('Failed to load WFO results:', e);
            }
        }

        renderSteps(steps) {
            if (!steps || !steps.length) return;
            const section = document.getElementById('wfoStepsSection');
            section.style.display = 'block';
            const content = document.getElementById('wfoStepsContent');

            let html = `<table class="wfo-steps-table"><thead><tr>
                <th>Step</th><th>IS Period</th><th>OOS Period</th><th>IS Score</th>
                <th>OOS Profit</th><th>OOS PF</th><th>OOS Sharpe</th><th>OOS DD</th><th>OOS WR%</th><th>OOS Trades</th><th>State</th>
            </tr></thead><tbody>`;

            steps.forEach(s => {
                const profit = parseFloat(s.oos_net_profit || 0);
                const profitClass = profit >= 0 ? 'positive' : 'negative';
                html += `<tr>
                    <td style="font-weight:700;">${s.step}</td>
                    <td style="font-size:10px; color:var(--accent-cyan);">${s.is_start || ''} → ${s.is_end || ''}</td>
                    <td style="font-size:10px; color:var(--accent-purple);">${s.oos_start || ''} → ${s.oos_end || ''}</td>
                    <td>${(s.is_score || 0).toFixed(2)}</td>
                    <td style="color:var(--${profitClass === 'positive' ? 'green' : 'red'});">$${profit.toFixed(2)}</td>
                    <td>${(s.oos_profit_factor || 0).toFixed(2)}</td>
                    <td>${(s.oos_sharpe || 0).toFixed(2)}</td>
                    <td>${(s.oos_max_drawdown || 0).toFixed(2)}</td>
                    <td>${(s.oos_win_rate || 0).toFixed(1)}%</td>
                    <td>${s.oos_trades || 0}</td>
                    <td><span class="wfo-status ${s.state || 'pending'}">${s.state || 'pending'}</span></td>
                </tr>`;
            });
            html += '</tbody></table>';
            content.innerHTML = html;
        }

        renderSummary(agg) {
            if (!agg || !agg.total_trades) return;
            const section = document.getElementById('wfoSummarySection');
            section.style.display = 'block';
            const content = document.getElementById('wfoSummaryContent');

            const metrics = [
                { label: 'Net Profit', value: `$${(agg.net_profit || 0).toFixed(2)}`, cls: (agg.net_profit||0) >= 0 ? 'positive' : 'negative' },
                { label: 'Total Trades', value: agg.total_trades || 0 },
                { label: 'Avg Profit Factor', value: (agg.avg_profit_factor || 0).toFixed(2) },
                { label: 'Avg Sharpe', value: (agg.avg_sharpe || 0).toFixed(2) },
                { label: 'Avg Win Rate', value: `${(agg.avg_win_rate || 0).toFixed(1)}%` },
                { label: 'Worst Drawdown', value: `$${(agg.worst_drawdown || 0).toFixed(2)}`, cls: 'negative' },
                { label: 'Profitable Periods', value: `${agg.profitable_periods || 0}/${agg.total_steps || 0} (${(agg.profitable_periods_pct || 0).toFixed(0)}%)` },
                { label: 'Avg Trade', value: `$${(agg.avg_trade || 0).toFixed(2)}` },
                { label: 'Recovery Factor', value: (agg.recovery_factor || 0).toFixed(2) },
                { label: 'Max Consec Losing', value: agg.max_consecutive_losing_periods || 0 },
            ];

            content.innerHTML = `<div class="wfo-summary-grid">${metrics.map(m =>
                `<div class="wfo-metric-card"><div class="metric-label">${m.label}</div><div class="metric-value ${m.cls || ''}">${m.value}</div></div>`
            ).join('')}</div>`;
        }

        renderRobustness(rob) {
            if (!rob || !rob.overall_score) return;
            const section = document.getElementById('wfoRobustnessSection');
            section.style.display = 'block';
            const content = document.getElementById('wfoRobustnessContent');

            const labelClass = (rob.label || '').toLowerCase().replace(/\s+/g, '').replace('overfitris', 'overfit').replace('insufficientevidence', 'insufficient');
            let html = `<div class="wfo-robustness">
                <div class="wfo-robustness-label ${labelClass}">${rob.label || 'Unknown'} — ${(rob.overall_score * 100).toFixed(1)}%</div>
                <div class="wfo-robustness-components">`;

            const components = rob.components || {};
            Object.entries(components).forEach(([key, comp]) => {
                const pct = ((comp.normalized || 0) * 100).toFixed(0);
                html += `<div class="wfo-rob-component">
                    <div class="name">${key.replace(/_/g, ' ')}</div>
                    <div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>
                    <div class="score">${pct}% (raw: ${typeof comp.raw === 'number' ? comp.raw.toFixed(2) : comp.raw})</div>
                </div>`;
            });
            html += '</div></div>';
            content.innerHTML = html;
        }

        renderStability(stability) {
            if (!stability || !stability.length) return;
            const section = document.getElementById('wfoStabilitySection');
            section.style.display = 'block';
            const content = document.getElementById('wfoStabilityContent');

            let html = `<table class="wfo-steps-table wfo-stability-table"><thead><tr>
                <th style="text-align:left;">Parameter</th><th>Mean</th><th>Std</th><th>CV</th><th>Min</th><th>Max</th>
            </tr></thead><tbody>`;

            stability.forEach(p => {
                if (!p.is_numeric) return;
                const cv = p.cv || 0;
                const cvClass = cv < 0.1 ? 'low-cv' : (cv < 0.3 ? 'med-cv' : 'high-cv');
                html += `<tr>
                    <td style="text-align:left; font-weight:600;">${p.name}</td>
                    <td>${p.mean != null ? p.mean.toFixed(4) : '—'}</td>
                    <td>${p.std != null ? p.std.toFixed(4) : '—'}</td>
                    <td class="${cvClass}">${cv.toFixed(4)}</td>
                    <td>${p.min != null ? p.min.toFixed(4) : '—'}</td>
                    <td>${p.max != null ? p.max.toFixed(4) : '—'}</td>
                </tr>`;
            });
            html += '</tbody></table>';
            content.innerHTML = html;
        }

        renderCandidates(candidates, runId) {
            if (!candidates || !candidates.length) return;
            const section = document.getElementById('wfoCandidatesSection');
            section.style.display = 'block';
            const content = document.getElementById('wfoCandidatesContent');

            // Filter to non-user_selected for main cards
            const mainCandidates = candidates.filter(c => c.method !== 'user_selected');
            const stepCandidates = candidates.filter(c => c.method === 'user_selected');

            let html = '<div class="wfo-candidates-grid">';
            mainCandidates.forEach((c, i) => {
                html += `<div class="wfo-candidate-card">
                    <div class="card-method">${c.method.replace(/_/g, ' ')}</div>
                    <div class="card-label">${c.label}</div>
                    <div class="card-desc">${c.description}</div>
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span class="card-confidence ${c.confidence}">${c.confidence} confidence</span>
                        <button class="btn btn-primary btn-sm" onclick="window.wfoManager.runFullSample('${runId}', ${i})">
                            <i class="fa-solid fa-play"></i> Full-Sample
                        </button>
                    </div>
                    <details style="margin-top:8px;"><summary style="cursor:pointer; font-size:11px; color:var(--text-muted);">View Parameters</summary>
                        <pre style="font-size:10px; color:var(--text-primary); margin-top:4px; max-height:150px; overflow:auto; background:var(--bg-secondary); padding:8px; border-radius:4px;">${JSON.stringify(c.params, null, 2)}</pre>
                    </details>
                </div>`;
            });
            html += '</div>';

            if (stepCandidates.length > 0) {
                html += `<details style="margin-top:16px;"><summary style="cursor:pointer; font-size:13px; font-weight:600; color:var(--text-heading);">Per-Step Parameters (${stepCandidates.length})</summary>
                <div class="wfo-candidates-grid" style="margin-top:10px;">`;
                stepCandidates.forEach((c, i) => {
                    html += `<div class="wfo-candidate-card">
                        <div class="card-method">step ${c.source_step}</div>
                        <div class="card-label">${c.label}</div>
                        <div class="card-desc">${c.description}</div>
                    </div>`;
                });
                html += '</div></details>';
            }
            content.innerHTML = html;
            this._candidates = candidates;
        }

        async runFullSample(runId, candidateIndex) {
            const candidates = this._candidates.filter(c => c.method !== 'user_selected');
            const candidate = candidates[candidateIndex];
            if (!candidate) return;

            if (window.showToast) window.showToast(`Running full-sample backtest: ${candidate.label}...`, 'info');

            try {
                const res = await fetch(`/api/walk-forward/${runId}/full-sample`, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        candidate_params: candidate.params,
                        candidate_label: candidate.label,
                    }),
                });
                const data = await res.json();
                if (data.status === 'success') {
                    this.renderFullSample(data);
                    if (window.showToast) window.showToast('Full-sample backtest completed', 'success');
                } else {
                    if (window.showToast) window.showToast(data.detail || 'Full-sample failed', 'error');
                }
            } catch (e) {
                if (window.showToast) window.showToast('Full-sample request failed', 'error');
            }
        }

        renderFullSample(data) {
            const section = document.getElementById('wfoFullSampleSection');
            section.style.display = 'block';
            const content = document.getElementById('wfoFullSampleContent');

            const m = data.metrics || {};
            const metrics = [
                { label: 'Total Trades', value: m['Total Trades'] || 0 },
                { label: 'Net Profit', value: `$${(m['Net Profit'] || 0).toFixed(2)}`, cls: (m['Net Profit']||0) >= 0 ? 'positive' : 'negative' },
                { label: 'Win Rate', value: `${(m['Win Rate %'] || 0).toFixed(1)}%` },
                { label: 'Profit Factor', value: (m['Profit Factor'] || 0).toFixed(2) },
                { label: 'Sharpe Ratio', value: (m['Sharpe Ratio'] || 0).toFixed(2) },
                { label: 'Max Drawdown', value: `$${(m['Overall Max Drawdown'] || 0).toFixed(2)}` },
            ];

            content.innerHTML = `
                <div style="font-size:12px; color:var(--text-secondary); margin-bottom:10px;">
                    Candidate: <strong>${data.candidate_label}</strong>
                </div>
                <div class="wfo-summary-grid">${metrics.map(me =>
                    `<div class="wfo-metric-card"><div class="metric-label">${me.label}</div><div class="metric-value ${me.cls || ''}">${me.value}</div></div>`
                ).join('')}</div>
            `;
        }

        renderExport(runId) {
            const section = document.getElementById('wfoExportSection');
            section.style.display = 'block';
            const bar = document.getElementById('wfoExportBar');
            const types = ['config', 'windows', 'steps', 'stability', 'candidates', 'summary'];
            bar.innerHTML = types.map(t =>
                `<button class="btn btn-secondary btn-sm" onclick="window.open('/api/walk-forward/${runId}/export/${t}', '_blank')">
                    <i class="fa-solid fa-download"></i> ${t.charAt(0).toUpperCase() + t.slice(1)}
                </button>`
            ).join('');
        }

        async loadHistory() {
            try {
                const res = await fetch('/api/walk-forward/runs');
                const data = await res.json();
                const content = document.getElementById('wfoHistoryContent');

                if (!data.runs || data.runs.length === 0) {
                    content.innerHTML = `<div class="empty-state" style="padding:40px; text-align:center;">
                        <i class="fa-solid fa-forward" style="font-size:32px; color:var(--text-muted); margin-bottom:12px;"></i>
                        <h3 style="color:var(--text-heading); margin:0 0 8px;">No Walk-Forward Runs</h3>
                        <p style="color:var(--text-secondary); font-size:13px;">Analyze a script, configure parameters, and generate windows to begin.</p>
                    </div>`;
                    return;
                }

                let html = `<table class="wfo-preview-table"><thead><tr>
                    <th>Run ID</th><th>Strategy</th><th>Mode</th><th>Period</th><th>Status</th><th>Steps</th><th>Size</th><th>Actions</th>
                </tr></thead><tbody>`;

                data.runs.forEach(r => {
                    const statusClass = r.status === 'completed' ? 'completed' : (r.status === 'running' ? 'running' : 'pending');
                    html += `<tr>
                        <td style="font-family:'JetBrains Mono',monospace; font-size:10px;">${r.run_id}</td>
                        <td>${r.strategy_name || '—'}</td>
                        <td>${r.window_mode || '—'}</td>
                        <td style="font-size:10px;">${r.wfo_start || ''} → ${r.wfo_end || ''}</td>
                        <td><span class="wfo-status ${statusClass}">${r.status || 'unknown'}</span></td>
                        <td>${r.current_step || 0}/${r.total_steps || 0}</td>
                        <td>${r.size_mb || 0} MB</td>
                        <td style="display:flex; gap:4px;">
                            <button class="btn btn-secondary btn-sm" onclick="window.wfoManager.viewRun('${r.run_id}')" title="View Results"><i class="fa-solid fa-eye"></i></button>
                            <button class="btn btn-danger btn-sm" onclick="window.wfoManager.deleteRun('${r.run_id}')" title="Delete"><i class="fa-solid fa-trash"></i></button>
                        </td>
                    </tr>`;
                });
                html += '</tbody></table>';
                content.innerHTML = html;
            } catch (e) {
                console.error('Failed to load WFO history:', e);
            }
        }

        async viewRun(runId) {
            this.currentRunId = runId;
            await this.loadResults(runId);
            if (window.showToast) window.showToast(`Loaded WFO run: ${runId}`, 'success');
        }

        async deleteRun(runId) {
            if (!confirm(`Delete WFO run ${runId}? This cannot be undone.`)) return;
            try {
                await fetch(`/api/walk-forward/${runId}`, { method: 'DELETE' });
                if (window.showToast) window.showToast('WFO run deleted', 'success');
                this.loadHistory();
            } catch (e) {
                if (window.showToast) window.showToast('Delete failed', 'error');
            }
        }
    }

    // Initialize WFO Manager
    window.wfoManager = new WalkForwardManager();

    // Hook into existing analysis callback to pass data to WFO
    const _origHandleAnalysis = window._handleAnalysisResult;
    window._handleAnalysisResult = function(data) {
        if (_origHandleAnalysis) _origHandleAnalysis(data);
        if (window.wfoManager && data) window.wfoManager.updateFromAnalysis(data);
    };

});

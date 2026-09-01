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
    let progressSSE = null;     // SSE connection for progress
    let currentResults = null;  // Latest results data
    let lwChart = null;         // Lightweight Charts instance
    let candleSeries = null;    // Candlestick series
    let currentDataPath = null; // Data path for charting
    let sortCol = null;
    let sortAsc = false;
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
    fetchUserData();
    setupNavigation();
    setupTabs();
    setupSectionToggles();
    reattachToActiveOptimization();

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
        if (!analysisResult) return 0;
        let total = 1;
        const cards = document.querySelectorAll('.param-card');
        
        cards.forEach(card => {
            const isOptimizing = card.querySelector('.param-optimize-toggle')?.checked;
            if (!isOptimizing) return;
            
            const name = card.dataset.paramName;
            const paramDef = analysisResult.params.find(p => p.name === name);
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

            if (data.status === 'started') {
                currentOptId = data.optimization_id;
                showToast(data.message, 'success');
                startProgressStream(currentOptId);
                stopOptBtn.classList.remove('hidden');
                setGlobalStatus('running', 'Optimizing...');
                switchTab('progress');
            } else {
                throw new Error(data.detail || 'Failed to start');
            }
        } catch (e) {
            showToast(`Start failed: ${e.message}`, 'error');
            startOptBtn.disabled = false;
        }
        startOptBtn.innerHTML = '<i class="fa-solid fa-rocket"></i> Start Optimization';
    }

    async function stopOptimization() {
        if (!currentOptId) return;
        try {
            const res = await fetch(`/api/optimize/stop/${currentOptId}`, { method: 'POST' });
            const data = await res.json();
            // The run now stops for real, and stays resumable afterwards.
            showToast(data.message || 'Optimization stopped — resume it any time', 'warning');
        } catch (e) {
            showToast('Failed to stop', 'error');
        }
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

    function ensureProgressPanel() {
        emptyProgress.classList.add('hidden');

        let panel = document.getElementById('activeProgressPanel');
        if (!panel) {
            panel = document.createElement('div');
            panel.id = 'activeProgressPanel';
            panel.className = 'progress-panel';
            progressContent.prepend(panel);
        }

        panel.innerHTML = `
            <div class="progress-header">
                <div class="progress-title"><i class="fa-solid fa-gauge-high" style="color:var(--accent-blue);margin-right:6px;"></i> Optimization Progress</div>
                <div class="progress-stats">
                    <div class="progress-stat">
                        <div class="progress-stat-value" id="pCompleted">0</div>
                        <div class="progress-stat-label">Completed</div>
                    </div>
                    <div class="progress-stat">
                        <div class="progress-stat-value" id="pTotal">0</div>
                        <div class="progress-stat-label">Total</div>
                    </div>
                    <div class="progress-stat">
                        <div class="progress-stat-value" id="pFailed" style="color:var(--red);">0</div>
                        <div class="progress-stat-label">Failed</div>
                    </div>
                    <div class="progress-stat">
                        <div class="progress-stat-value" id="pPending" style="color:var(--text-muted);">0</div>
                        <div class="progress-stat-label">Remaining</div>
                    </div>
                </div>
            </div>
            <div id="pBanner"></div>
            <div class="progress-bar-container">
                <div class="progress-bar" id="pBar" style="width:0%"></div>
            </div>
            <div class="progress-detail">
                <span id="pPercent">0%</span>
                <span id="pETA">ETA: calculating...</span>
            </div>
        `;
        return panel;
    }

    /**
     * Paint one progress payload.
     *
     * The bar follows the server's `percent`, which counts a batch as done only
     * once it has actually produced a result. Older runs (and any payload
     * without `percent`) fall back to completed/total.
     */
    function applyProgress(p) {
        if (!p || !document.getElementById('pBar')) return;

        const total = Number(p.total) || 0;
        const completed = Number(p.completed) || 0;
        const raw = (typeof p.percent === 'number')
            ? p.percent
            : (total > 0 ? (completed / total) * 100 : 0);
        const pct = Math.max(0, Math.min(100, raw));

        const remaining = (typeof p.pending === 'number')
            ? p.pending
            : Math.max(total - completed, 0);

        const set = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };

        set('pCompleted', completed);
        set('pTotal', total);
        set('pFailed', p.failed || 0);
        set('pPending', remaining);
        set('pPercent', `${pct.toFixed(1)}%`);

        const elBar = document.getElementById('pBar');
        if (elBar) elBar.style.width = `${pct.toFixed(1)}%`;

        const elETA = document.getElementById('pETA');
        if (elETA) {
            if (p.status === 'aggregating') {
                elETA.textContent = 'Aggregating results...';
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
        const banner = document.getElementById('pBanner');
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
                <button class="btn btn-success btn-sm" id="pResumeBtn">
                    <i class="fa-solid fa-play"></i> Resume
                </button>
            </div>
        `;
        const btn = document.getElementById('pResumeBtn');
        if (btn) btn.addEventListener('click', () => resumeOptimization(optId));
    }

    function startProgressStream(optId) {
        if (progressSSE) progressSSE.close();

        ensureProgressPanel();

        progressSSE = new EventSource(`/api/optimize/progress/${optId}`);

        progressSSE.onmessage = (event) => {
            try {
                const p = JSON.parse(event.data);

                if (p.status === 'done') {
                    progressSSE.close();
                    progressSSE = null;
                    if (p.final_status && p.final_status !== 'completed') {
                        onOptimizationHalted(optId, p.final_status);
                    } else {
                        onOptimizationComplete(optId);
                    }
                    return;
                }

                const view = applyProgress(p);
                if (view) {
                    setGlobalStatus('running', `${view.pct.toFixed(1)}% — ${view.completed}/${view.total}`);
                }
            } catch (e) { /* ignore parse errors */ }
        };

        progressSSE.onerror = async () => {
            // EventSource retries by itself. Only finish when the server agrees
            // the run is actually over — a dropped connection used to be
            // reported as "completed" while batches were still running.
            try {
                const res = await fetch(`/api/optimize/status/${optId}`);
                if (!res.ok) return;
                const p = (await res.json()).progress || {};
                if (p.is_running) return;
                if (!FINISHED_STATUSES.includes(p.status)) return;

                if (progressSSE) { progressSSE.close(); progressSSE = null; }
                if (p.status === 'completed' && !p.resumable) {
                    onOptimizationComplete(optId);
                } else {
                    onOptimizationHalted(optId, p.status);
                }
            } catch (e) { /* leave the stream retrying */ }
        };
    }

    async function onOptimizationComplete(optId) {
        startOptBtn.disabled = false;
        stopOptBtn.classList.add('hidden');
        setGlobalStatus('idle', 'Completed');
        showToast('Optimization completed!', 'success');

        // Load results
        await loadResults(optId);
        switchTab('results');
    }

    /** A run ended without finishing — keep the user on Progress with a Resume option. */
    async function onOptimizationHalted(optId, status) {
        startOptBtn.disabled = false;
        stopOptBtn.classList.add('hidden');
        setGlobalStatus('idle', status || 'interrupted');
        showToast(`Optimization ${status || 'interrupted'} — you can resume it`, 'warning');

        try {
            const res = await fetch(`/api/optimize/status/${optId}`);
            if (res.ok) {
                const p = (await res.json()).progress || {};
                ensureProgressPanel();
                applyProgress(p);
                showResumeBanner(optId, p);
            }
        } catch (e) { /* banner is best-effort */ }
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
            if (!res.ok || data.status !== 'resumed') {
                throw new Error(data.detail || 'Resume failed');
            }

            currentOptId = optId;
            showToast(data.message || 'Optimization resumed', 'success');
            startProgressStream(optId);
            startOptBtn.disabled = true;
            stopOptBtn.classList.remove('hidden');
            setGlobalStatus('running', 'Resuming...');
            historyModal.classList.remove('active');
            switchTab('progress');
        } catch (e) {
            showToast(`Resume failed: ${e.message}`, 'error');
        }
    }
    window.resumeOptimization = resumeOptimization;

    /**
     * Reattach the dashboard to whatever the server is doing.
     *
     * Without this, reloading the page (or restarting the server) left the
     * Progress tab empty with no way back into a run that was already in
     * flight — the only visible option was to start over from batch 1.
     */
    async function reattachToActiveOptimization() {
        try {
            const res = await fetch('/api/optimize/active');
            if (!res.ok) return;
            const data = await res.json();
            if (!data.optimization_id) return;

            currentOptId = data.optimization_id;

            if (data.is_running) {
                startProgressStream(currentOptId);
                applyProgress(data.progress);
                startOptBtn.disabled = true;
                stopOptBtn.classList.remove('hidden');
                setGlobalStatus('running', 'Optimizing...');
            } else if (data.resumable) {
                ensureProgressPanel();
                applyProgress(data.progress);
                showResumeBanner(currentOptId, data.progress || {});
                setGlobalStatus('idle', data.progress?.status || 'interrupted');
            }
        } catch (e) { /* dashboard works fine without it */ }
    }

    // =========================================================================
    // RESULTS
    // =========================================================================
    async function loadResults(optId) {
        try {
            const res = await fetch(`/api/optimizations/${optId}/results?top=100`);
            const data = await res.json();

            if (data.status !== 'success') throw new Error('Failed to load results');

            currentResults = data;
            currentOptId = optId;
            resultsBadge.textContent = data.successful || 0;
            renderResults(data);
        } catch (e) {
            showToast(`Failed to load results: ${e.message}`, 'error');
        }
    }

    function renderResults(data) {
        if (!data.top_results || data.top_results.length === 0) {
            if (data.all_results && data.all_results.length > 0) {
                // Show failed results
                let html = `
                    <div style="padding:0 0 12px; display:flex; justify-content:space-between; align-items:center;">
                        <div style="font-size:14px; font-weight:600; color:#ef4444;">
                            <i class="fa-solid fa-triangle-exclamation" style="margin-right:6px;"></i>
                            Optimization Failed — ${data.total_results} batches failed
                        </div>
                    </div>
                    <div class="results-table-wrapper" style="max-height:calc(100vh - 200px); overflow:auto;">
                        <table class="results-table">
                            <thead><tr>
                                <th>Batch</th>
                                <th>Status</th>
                                <th>Error</th>
                            </tr></thead><tbody>`;
                data.all_results.forEach(row => {
                    html += `<tr>
                        <td>${row.batch || ''}</td>
                        <td style="color:#ef4444;font-weight:bold">${row.status}</td>
                        <td style="color:#f87171;font-size:12px;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${(row.error || '').replace(/"/g, '&quot;')}">${row.error || 'Unknown Error'}</td>
                    </tr>`;
                });
                html += `</tbody></table></div>`;
                resultsContainer.innerHTML = html;
            } else {
                resultsContainer.innerHTML = `
                    <div class="empty-state">
                        <i class="fa-solid fa-exclamation-triangle"></i>
                        <h3>No Successful Results</h3>
                        <p>All batches failed or produced no trades. Check script parameters.</p>
                    </div>`;
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
                </div>
                <div class="btn-group">
                    <button class="btn btn-secondary btn-sm" onclick="exportResultsCSV()">
                        <i class="fa-solid fa-download"></i> Export CSV
                    </button>
                </div>
            </div>
            <div class="results-table-wrapper" style="max-height:calc(100vh - 200px); overflow:auto;">
                <table class="results-table">
                    <thead><tr>
                        <th>#</th>
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
            html += `<tr class="${rankClass}">
                <td>${rank}</td>
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

            const key = `${currentOptId}/${row.batch}`;
            const isSaved = savedUserData && savedUserData.favorites && savedUserData.favorites[key];
            const starColor = isSaved ? 'var(--accent-yellow, #f59e0b)' : 'inherit';
            const starClass = isSaved ? 'fa-solid fa-star' : 'fa-regular fa-star';

            html += `<td style="display:flex; gap:4px;">
                <button class="cell-action-btn" onclick="openSaveModal('${currentOptId}', '${row.batch}')" title="Save / Review">
                    <i class="${starClass}" style="color:${starColor};"></i>
                </button>
                <button class="cell-action-btn" onclick="viewBatch('${currentOptId}', '${row.batch}')" title="View Details">
                    <i class="fa-solid fa-eye"></i>
                </button>
                <button class="cell-action-btn" onclick="viewBatchChart('${currentOptId}', '${row.batch}')" title="View on Chart">
                    <i class="fa-solid fa-chart-line"></i>
                </button>
                <button class="cell-action-btn" onclick="viewExcel('${currentOptId}', '${row.batch}')" title="View Excel Report">
                    <i class="fa-solid fa-table"></i>
                </button>
                <button class="cell-action-btn" onclick="loadOptimization('${currentOptId}', '${row.batch}')" title="Load & Edit Parameters">
                    <i class="fa-solid fa-pen-to-square"></i>
                </button>
                <a class="cell-action-btn" href="/api/download/${currentOptId}/${row.batch}/excel" title="Download Excel Report" download style="display:inline-flex;align-items:center;justify-content:center;text-decoration:none;">
                    <i class="fa-solid fa-file-excel"></i>
                </a>
            </td></tr>`;
        });

        html += '</tbody></table></div>';
        resultsContainer.innerHTML = html;

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
    window.loadOptimization = async function(optId, batchId) {
        try {
            // Fetch batch params and opt config
            const [resBatch, resOpt] = await Promise.all([
                fetch(`/api/optimizations/${optId}/batch/${batchId}`),
                fetch(`/api/optimizations/${optId}/results?top=1`)
            ]);
            
            if (!resBatch.ok) throw new Error('Failed to fetch batch');
            const batchData = await resBatch.json();
            const batchParams = batchData.params;
            
            const optData = await resOpt.json();
            const scriptPath = optData.config.script_path;
            // Get just the filename to match the select dropdown
            const scriptName = scriptPath.split(/[\\/]/).pop();

            // Switch to New tab
            switchTab('new');
            
            // Select the script
            const scriptSelect = document.getElementById('scriptSelect');
            scriptSelect.value = scriptName;
            scriptSelect.dispatchEvent(new Event('change'));
            
            // Trigger analysis
            document.getElementById('analyzeBtn').click();
            
            showToast('Loading script parameters...', 'info');

            // Poll until params section is visible (analysis done)
            const checkDone = setInterval(() => {
                if (document.getElementById('paramsSection').style.display !== 'none') {
                    clearInterval(checkDone);
                    
                    // Uncheck all opt checkboxes to make them fixed
                    document.querySelectorAll('.opt-checkbox').forEach(cb => {
                        cb.checked = false;
                        cb.dispatchEvent(new Event('change'));
                    });

                    // Wait a tiny bit for the UI to toggle the range inputs off
                    setTimeout(() => {
                        for (const [key, val] of Object.entries(batchParams)) {
                            // Only set if we have a single fixed input for it
                            const input = document.querySelector(`.param-fixed-value[data-name="${key}"]`);
                            if (input) {
                                if (input.type === 'checkbox') {
                                    input.checked = val;
                                } else if (typeof val === 'object' && val !== null) {
                                    input.value = JSON.stringify(val);
                                } else {
                                    input.value = val;
                                }
                            }
                        }
                        showToast('Parameters loaded! Ready for a single validation run or further tweaking.', 'success');
                    }, 100);
                }
            }, 200);
            
            // Safety timeout
            setTimeout(() => clearInterval(checkDone), 10000);

        } catch (e) {
            showToast('Failed to load optimization: ' + e.message, 'error');
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
        
        document.getElementById('saveModal').classList.add('active');
    };

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

    window.renderSavedBacktests = function() {
        const groupsList = document.getElementById('groupsList');
        groupsList.innerHTML = '';
        
        if (!savedUserData.groups || savedUserData.groups.length === 0) {
            groupsList.innerHTML = '<div style="color:var(--text-secondary); font-size:12px;">No groups yet.</div>';
            renderSavedGroup(null);
            return;
        }
        
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
            
            if (i === 0) btn.click(); // Auto-select first group
        });
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

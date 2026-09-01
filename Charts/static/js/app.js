/**
 * TradingView Lightweight Charts Web Application
 * Data Engine: Parquet MULTIBANK Ticks (XAUUSD)
 * Indicator Engine: Bit-exact Pine Script Supertrend + Technical Indicators
 * Drawing & Position Mapper Engine: Price Lines, Trendlines, Long/Short Position Mappers with R:R, SL/TP & LocalStorage
 * Interactivity: Left-Click Tool Selection, Floating Delete Toolbar, Delete/Backspace Keyboard Shortcut
 */

document.addEventListener('DOMContentLoaded', () => {
    // =========================================================================
    // GLOBAL STATE
    // =========================================================================
    let currentTimeframe = '15m';
    let currentTimezoneOffset = 5.5; // Default UTC+05:30 (IST)
    let currentTimezoneLabel = 'UTC+05:30';

    let mainChart = null;
    let candlestickSeries = null;
    
    let subChart = null;
    let subChartSeriesMap = {};

    let activeIndicators = new Map(); // id -> { id, meta, params, seriesList, title, dataMap }
    let indicatorIdCounter = 1;
    let editingIndicatorId = null;

    let availableIndicatorsList = [];
    let selectedIndicatorForConfig = null;

    let currentOHLCData = [];

    // DRAWING & POSITION MAPPER STATE
    let activeTool = 'crosshair'; // 'crosshair', 'hline', 'trendline', 'long_pos', 'short_pos', 'fib', 'ruler'
    let drawings = [];
    let drawingIdCounter = 1;
    let selectedDrawingId = null;
    let editingPositionId = null;
    let dragHandle = null; // { drawingId, handleType: 'entry'|'tp'|'sl'|'line'|'p1'|'p2' }
    let tempDrawingPoint = null; // { type, point1: { time, price }, mousePos: { x, y } }
    let hideDrawings = false;
    let lastContextMenuPrice = 0;

    // =========================================================================
    // DOM ELEMENTS
    // =========================================================================
    const mainChartCanvas = document.getElementById('mainChartCanvas');
    const chartContainer = document.getElementById('chartContainer');
    const drawingCanvasOverlay = document.getElementById('drawingCanvasOverlay');
    let overlayCtx = drawingCanvasOverlay ? drawingCanvasOverlay.getContext('2d') : null;

    const subChartCanvasWrapper = document.getElementById('subChartCanvasWrapper');
    const subChartCanvas = document.getElementById('subChartCanvas');
    const subChartTitle = document.getElementById('subChartTitle');
    const closeSubChartBtn = document.getElementById('closeSubChartBtn');
    const headerSpinner = document.getElementById('headerSpinner');

    // Context Menu DOM Elements
    const chartContextMenu = document.getElementById('chartContextMenu');
    const ctxDeleteSelectedDrawing = document.getElementById('ctxDeleteSelectedDrawing');
    const ctxDeleteSelectedLabel = document.getElementById('ctxDeleteSelectedLabel');
    const ctxAddHLine = document.getElementById('ctxAddHLine');
    const ctxAddHLineLabel = document.getElementById('ctxAddHLineLabel');
    const ctxAddLongPos = document.getElementById('ctxAddLongPos');
    const ctxAddLongPosLabel = document.getElementById('ctxAddLongPosLabel');
    const ctxAddShortPos = document.getElementById('ctxAddShortPos');
    const ctxAddShortPosLabel = document.getElementById('ctxAddShortPosLabel');
    const ctxClearDrawings = document.getElementById('ctxClearDrawings');
    const ctxResetView = document.getElementById('ctxResetView');
    const ctxRemoveIndicators = document.getElementById('ctxRemoveIndicators');
    const ctxResetAll = document.getElementById('ctxResetAll');
    const ctxFitContent = document.getElementById('ctxFitContent');
    const ctxToggleTheme = document.getElementById('ctxToggleTheme');

    // Floating Selected Drawing Action Toolbar
    const floatingDrawingBar = document.getElementById('floatingDrawingBar');
    const floatingBarTitle = document.getElementById('floatingBarTitle');
    const floatingBarSettingsBtn = document.getElementById('floatingBarSettingsBtn');
    const floatingBarDeleteBtn = document.getElementById('floatingBarDeleteBtn');
    const floatingBarCloseBtn = document.getElementById('floatingBarCloseBtn');

    // Header & Legend Elements
    const legendTf = document.getElementById('legendTf');
    const brokerTag = document.getElementById('brokerTag');
    const valO = document.getElementById('valO');
    const valH = document.getElementById('valH');
    const valL = document.getElementById('valL');
    const valC = document.getElementById('valC');
    const valChange = document.getElementById('valChange');
    
    const quoteSellPrice = document.getElementById('quoteSellPrice');
    const quoteBuyPrice = document.getElementById('quoteBuyPrice');
    const quoteSpread = document.getElementById('quoteSpread');
    const indicatorsLegend = document.getElementById('indicatorsLegend');

    // Toolbar Buttons
    const toolButtons = document.querySelectorAll('.tv-left-toolbar .tool-btn');
    const toolHideDrawings = document.getElementById('toolHideDrawings');
    const toolClearDrawings = document.getElementById('toolClearDrawings');
    const leftResetBtn = document.getElementById('leftResetBtn');

    const tfButtons = document.querySelectorAll('.tf-btn');
    const rangeButtons = document.querySelectorAll('.range-btn');
    const timezoneSelect = document.getElementById('timezoneSelect');
    const themeToggleBtn = document.getElementById('themeToggleBtn');
    const fullscreenBtn = document.getElementById('fullscreenBtn');
    const clockDisplay = document.getElementById('clockDisplay');

    // Indicators Modal Elements
    const openIndicatorsBtn = document.getElementById('openIndicatorsBtn');
    const indicatorsModal = document.getElementById('indicatorsModal');
    const modalHeaderTitle = document.getElementById('modalHeaderTitle');
    const modalSearchBox = document.getElementById('modalSearchBox');
    const closeIndicatorsModal = document.getElementById('closeIndicatorsModal');
    const indicatorSearchInput = document.getElementById('indicatorSearchInput');
    const indicatorsListContainer = document.getElementById('indicatorsListContainer');
    
    const indicatorConfigPanel = document.getElementById('indicatorConfigPanel');
    const configTitle = document.getElementById('configTitle');
    const paramsForm = document.getElementById('paramsForm');
    const cancelConfigBtn = document.getElementById('cancelConfigBtn');
    const applyIndicatorBtn = document.getElementById('applyIndicatorBtn');

    // Position Mapper Modal Elements
    const positionModal = document.getElementById('positionModal');
    const posModalTitle = document.getElementById('posModalTitle');
    const posTypeSelect = document.getElementById('posTypeSelect');
    const posEntryInput = document.getElementById('posEntryInput');
    const posTpInput = document.getElementById('posTpInput');
    const posSlInput = document.getElementById('posSlInput');
    const posLotsInput = document.getElementById('posLotsInput');
    const closePosModal = document.getElementById('closePosModal');
    const cancelPosModal = document.getElementById('cancelPosModal');
    const savePosModal = document.getElementById('savePosModal');

    // =========================================================================
    // LOCALSTORAGE PERSISTENCE ENGINE
    // =========================================================================
    function saveSettingsToLocalStorage() {
        try {
            // 1. Timeframe & Timezone & Theme
            localStorage.setItem('tv_timeframe', currentTimeframe);
            localStorage.setItem('tv_timezone', timezoneSelect.value);
            localStorage.setItem('tv_theme', document.body.classList.contains('dark-theme') ? 'dark' : 'light');

            // 2. Active Indicators List
            const indArray = [];
            activeIndicators.forEach((indObj, id) => {
                indArray.push({
                    id: id,
                    meta: indObj.meta,
                    params: indObj.params
                });
            });
            localStorage.setItem('tv_active_indicators', JSON.stringify(indArray));

            // 3. Drawings & Position Mappers
            saveDrawingsToLocalStorage();
        } catch (e) {
            console.error('Failed to save settings to localStorage:', e);
        }
    }

    function saveDrawingsToLocalStorage() {
        try {
            localStorage.setItem('tv_drawings', JSON.stringify(drawings));
        } catch (e) {
            console.error('Failed to save drawings to localStorage:', e);
        }
    }

    function restoreSettingsFromLocalStorage() {
        try {
            // Restore Theme
            const savedTheme = localStorage.getItem('tv_theme');
            if (savedTheme === 'dark') {
                document.body.classList.add('dark-theme');
                themeToggleBtn.innerHTML = '<i class="fa-solid fa-sun"></i>';
            }

            // Restore Timezone
            const savedTz = localStorage.getItem('tv_timezone');
            if (savedTz && timezoneSelect.querySelector(`option[value="${savedTz}"]`)) {
                timezoneSelect.value = savedTz;
                currentTimezoneOffset = getOffsetHoursFromSelect(savedTz);
                const selectedText = timezoneSelect.options[timezoneSelect.selectedIndex].text;
                currentTimezoneLabel = selectedText.split(' ')[0];
            }

            // Restore Timeframe
            const savedTf = localStorage.getItem('tv_timeframe');
            if (savedTf) {
                currentTimeframe = savedTf;
                tfButtons.forEach(b => b.classList.remove('active'));
                const matchedBtn = document.querySelector(`.tf-btn[data-tf="${savedTf}"]`);
                if (matchedBtn) matchedBtn.classList.add('active');
            }

            // Restore Drawings
            const savedDrawings = localStorage.getItem('tv_drawings');
            if (savedDrawings) {
                const parsed = JSON.parse(savedDrawings);
                if (Array.isArray(parsed)) {
                    drawings = parsed;
                    drawingIdCounter = drawings.reduce((max, d) => {
                        const num = parseInt((d.id || '').replace('draw_', ''), 10);
                        return isNaN(num) ? max : Math.max(max, num);
                    }, 0) + 1;
                }
            }
        } catch (e) {
            console.error('Failed to restore settings from localStorage:', e);
        }
    }

    // =========================================================================
    // TIMEZONE HELPER FUNCTIONS
    // =========================================================================
    function getOffsetHoursFromSelect(val) {
        if (val === 'UTC') return 0;
        if (val === 'UTC+3') return 3;
        if (val === 'UTC+5.5') return 5.5;
        if (val === 'UTC-5') return -5;
        if (val === 'UTC+1') return 1;
        if (val === 'UTC+8') return 8;
        if (val === 'UTC+9') return 9;
        return 0;
    }

    function formatTickMarkTimezone(utcSecs, offsetHours) {
        if (typeof utcSecs !== 'number') return '';
        const d = new Date((utcSecs + offsetHours * 3600) * 1000);
        const hrs = String(d.getUTCHours()).padStart(2, '0');
        const mins = String(d.getUTCMinutes()).padStart(2, '0');
        return `${hrs}:${mins}`;
    }

    function formatFullDateTimezone(utcSecs, offsetHours) {
        if (typeof utcSecs !== 'number') return '';
        const d = new Date((utcSecs + offsetHours * 3600) * 1000);
        const year = d.getUTCFullYear();
        const month = String(d.getUTCMonth() + 1).padStart(2, '0');
        const day = String(d.getUTCDate()).padStart(2, '0');
        const hrs = String(d.getUTCHours()).padStart(2, '0');
        const mins = String(d.getUTCMinutes()).padStart(2, '0');
        return `${year}-${month}-${day} ${hrs}:${mins}`;
    }

    // =========================================================================
    // 1. INITIALIZE LIGHTWEIGHT CHARTS
    // =========================================================================
    function initMainChart() {
        // Ensure overlay canvas is preserved
        let overlayCanvas = document.getElementById('drawingCanvasOverlay');
        mainChartCanvas.innerHTML = '';
        if (!overlayCanvas) {
            overlayCanvas = document.createElement('canvas');
            overlayCanvas.id = 'drawingCanvasOverlay';
        }
        mainChartCanvas.appendChild(overlayCanvas);
        overlayCtx = overlayCanvas.getContext('2d');

        const isDark = document.body.classList.contains('dark-theme');

        mainChart = LightweightCharts.createChart(mainChartCanvas, {
            width: mainChartCanvas.clientWidth,
            height: mainChartCanvas.clientHeight,
            layout: {
                background: { type: 'solid', color: isDark ? '#131722' : '#ffffff' },
                textColor: isDark ? '#d1d4dc' : '#131722',
                fontFamily: '-apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, sans-serif',
            },
            grid: {
                vertLines: { color: isDark ? '#1f232f' : '#f0f3fa' },
                horzLines: { color: isDark ? '#1f232f' : '#f0f3fa' },
            },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
            },
            rightPriceScale: {
                borderColor: isDark ? '#2a2e39' : '#e0e3eb',
                scaleMargins: {
                    top: 0.1,
                    bottom: 0.15,
                },
            },
            timeScale: {
                borderColor: isDark ? '#2a2e39' : '#e0e3eb',
                timeVisible: true,
                secondsVisible: false,
                tickMarkFormatter: (time) => formatTickMarkTimezone(time, currentTimezoneOffset),
            },
            localization: {
                timeFormatter: (time) => formatFullDateTimezone(time, currentTimezoneOffset),
            }
        });

        candlestickSeries = mainChart.addCandlestickSeries({
            upColor: '#089981',
            downColor: '#f23645',
            borderUpColor: '#089981',
            borderDownColor: '#f23645',
            wickUpColor: '#089981',
            wickDownColor: '#f23645',
        });

        // Sync drawing canvas overlay with time/price scale changes
        mainChart.timeScale().subscribeVisibleLogicalRangeChange(() => {
            redrawCanvasOverlay();
            updateFloatingBarPosition();
        });

        mainChart.subscribeCrosshairMove((param) => {
            redrawCanvasOverlay();

            if (!param.time || !param.seriesData || !candlestickSeries) {
                updateLegendToLastBar();
                return;
            }

            const bar = param.seriesData.get(candlestickSeries);
            if (bar) {
                updateLegendOHLC(bar);
            }

            activeIndicators.forEach((indObj) => {
                if (indObj.updateLegendValue && param.time) {
                    indObj.updateLegendValue(param.time);
                }
            });
        });

        window.addEventListener('resize', () => {
            if (mainChart) {
                mainChart.applyOptions({
                    width: mainChartCanvas.clientWidth,
                    height: mainChartCanvas.clientHeight
                });
            }
            if (subChart && subChartCanvas) {
                subChart.applyOptions({
                    width: subChartCanvas.clientWidth,
                    height: subChartCanvas.clientHeight
                });
            }
            resizeOverlayCanvas();
        });

        // Setup Right-Click Context Menu Listener on Chart Workspace
        chartContainer.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            
            const rect = mainChartCanvas.getBoundingClientRect();
            let x = e.clientX - rect.left;
            let y = e.clientY - rect.top;

            if (candlestickSeries && y >= 0 && y <= rect.height) {
                lastContextMenuPrice = candlestickSeries.coordinateToPrice(y) || 0;
                if (ctxAddHLineLabel) ctxAddHLineLabel.textContent = `Add Price Line @ ${lastContextMenuPrice.toFixed(3)}`;
                if (ctxAddLongPosLabel) ctxAddLongPosLabel.textContent = `Add Long Position Mapper Here`;
                if (ctxAddShortPosLabel) ctxAddShortPosLabel.textContent = `Add Short Position Mapper Here`;
            }

            // Check if right clicked on a selected drawing
            if (selectedDrawingId) {
                const sel = drawings.find(d => d.id === selectedDrawingId);
                if (sel && ctxDeleteSelectedDrawing) {
                    ctxDeleteSelectedDrawing.classList.remove('hidden');
                    ctxDeleteSelectedLabel.textContent = `Delete ${getDrawingDisplayName(sel.type)}`;
                }
            } else {
                if (ctxDeleteSelectedDrawing) ctxDeleteSelectedDrawing.classList.add('hidden');
            }

            if (x + 240 > rect.width) x = rect.width - 245;
            if (y + 240 > rect.height) y = rect.height - 245;

            chartContextMenu.style.left = `${x + 44}px`;
            chartContextMenu.style.top = `${y + 44}px`;
            chartContextMenu.classList.remove('hidden');
        });

        document.addEventListener('click', (e) => {
            if (chartContextMenu && !chartContextMenu.contains(e.target)) {
                chartContextMenu.classList.add('hidden');
            }
        });

        // Keyboard Shortcut: Delete or Backspace key deletes selected drawing tool
        window.addEventListener('keydown', (e) => {
            const activeEl = document.activeElement;
            const isInput = activeEl && (activeEl.tagName === 'INPUT' || activeEl.tagName === 'SELECT' || activeEl.tagName === 'TEXTAREA');

            if (!isInput && (e.key === 'Delete' || e.key === 'Backspace') && selectedDrawingId) {
                e.preventDefault();
                deleteSelectedDrawing();
            }
        });

        setupDrawingCanvasEvents();
        setupFloatingBarEvents();
        resizeOverlayCanvas();
    }

    function initSubChart() {
        if (!subChartCanvas) return;
        subChartCanvas.innerHTML = '';
        const isDark = document.body.classList.contains('dark-theme');

        subChart = LightweightCharts.createChart(subChartCanvas, {
            width: subChartCanvas.clientWidth,
            height: subChartCanvas.clientHeight,
            layout: {
                background: { type: 'solid', color: isDark ? '#131722' : '#ffffff' },
                textColor: isDark ? '#d1d4dc' : '#131722',
            },
            grid: {
                vertLines: { color: isDark ? '#1f232f' : '#f0f3fa' },
                horzLines: { color: isDark ? '#1f232f' : '#f0f3fa' },
            },
            rightPriceScale: {
                borderColor: isDark ? '#2a2e39' : '#e0e3eb',
            },
            timeScale: {
                borderColor: isDark ? '#2a2e39' : '#e0e3eb',
                timeVisible: true,
                tickMarkFormatter: (time) => formatTickMarkTimezone(time, currentTimezoneOffset),
            },
            localization: {
                timeFormatter: (time) => formatFullDateTimezone(time, currentTimezoneOffset),
            }
        });

        mainChart.timeScale().subscribeVisibleTimeRangeChange((timeRange) => {
            if (subChart && timeRange) {
                subChart.timeScale().setVisibleTimeRange(timeRange);
            }
        });
    }

    // =========================================================================
    // 2. DRAWING ENGINE & CANVAS OVERLAY RENDERING
    // =========================================================================
    function resizeOverlayCanvas() {
        if (!drawingCanvasOverlay || !mainChartCanvas) return;
        const rect = mainChartCanvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        drawingCanvasOverlay.width = rect.width * dpr;
        drawingCanvasOverlay.height = rect.height * dpr;
        drawingCanvasOverlay.style.width = `${rect.width}px`;
        drawingCanvasOverlay.style.height = `${rect.height}px`;
        if (overlayCtx) {
            overlayCtx.scale(dpr, dpr);
        }
        redrawCanvasOverlay();
        updateFloatingBarPosition();
    }

    function redrawCanvasOverlay() {
        if (!drawingCanvasOverlay || !overlayCtx || !mainChartCanvas) return;
        const rect = mainChartCanvas.getBoundingClientRect();
        const width = rect.width;
        const height = rect.height;

        overlayCtx.clearRect(0, 0, width, height);

        if (hideDrawings || !mainChart || !candlestickSeries) return;

        const timeToX = (time) => mainChart.timeScale().timeToCoordinate(time);
        const priceToY = (price) => candlestickSeries.priceToCoordinate(price);

        drawings.forEach((drawing) => {
            if (drawing.type === 'hline') {
                drawHorizontalLine(drawing, width, priceToY);
            } else if (drawing.type === 'long_pos' || drawing.type === 'short_pos') {
                drawPositionMapper(drawing, width, timeToX, priceToY);
            } else if (drawing.type === 'trendline') {
                drawTrendline(drawing, timeToX, priceToY);
            } else if (drawing.type === 'fib') {
                drawFibonacci(drawing, width, timeToX, priceToY);
            } else if (drawing.type === 'ruler') {
                drawRuler(drawing, timeToX, priceToY);
            }
        });

        if (tempDrawingPoint && tempDrawingPoint.mousePos) {
            drawTempDrawing(tempDrawingPoint, width, timeToX, priceToY);
        }
    }

    function drawHorizontalLine(drawing, canvasWidth, priceToY) {
        const y = priceToY(drawing.price);
        if (y === null || y === undefined || isNaN(y)) return;

        const color = drawing.color || '#2962FF';
        const isSelected = drawing.id === selectedDrawingId;

        overlayCtx.save();
        overlayCtx.beginPath();
        overlayCtx.setLineDash(drawing.dashed ? [6, 4] : []);
        overlayCtx.lineWidth = isSelected ? 2.5 : 1.5;
        overlayCtx.strokeStyle = color;
        overlayCtx.moveTo(0, y);
        overlayCtx.lineTo(canvasWidth, y);
        overlayCtx.stroke();

        // Draw Price Badge on Right Scale
        const priceStr = drawing.price.toFixed(3);
        overlayCtx.font = 'bold 11px sans-serif';
        const textWidth = overlayCtx.measureText(priceStr).width;
        const badgeWidth = textWidth + 12;
        const badgeHeight = 18;
        const badgeX = canvasWidth - badgeWidth - 4;
        const badgeY = y - badgeHeight / 2;

        overlayCtx.fillStyle = color;
        overlayCtx.beginPath();
        if (overlayCtx.roundRect) overlayCtx.roundRect(badgeX, badgeY, badgeWidth, badgeHeight, 3);
        else overlayCtx.rect(badgeX, badgeY, badgeWidth, badgeHeight);
        overlayCtx.fill();

        overlayCtx.fillStyle = '#ffffff';
        overlayCtx.textAlign = 'center';
        overlayCtx.textBaseline = 'middle';
        overlayCtx.fillText(priceStr, badgeX + badgeWidth / 2, y);

        if (isSelected) {
            drawHandleNode(canvasWidth / 2, y, color, true);
        }

        overlayCtx.restore();
    }

    function drawPositionMapper(drawing, canvasWidth, timeToX, priceToY) {
        const yEntry = priceToY(drawing.entryPrice);
        const yTP = priceToY(drawing.tpPrice);
        const ySL = priceToY(drawing.slPrice);

        if (yEntry === null || yTP === null || ySL === null || isNaN(yEntry) || isNaN(yTP) || isNaN(ySL)) return;

        let x1 = drawing.startTime ? timeToX(drawing.startTime) : null;
        let x2 = drawing.endTime ? timeToX(drawing.endTime) : null;

        if (x1 === null || x1 === undefined) x1 = Math.max(40, canvasWidth - 320);
        if (x2 === null || x2 === undefined) x2 = Math.min(canvasWidth - 60, x1 + 220);
        if (x2 <= x1 + 20) x2 = x1 + 220;

        const width = x2 - x1;
        const isLong = drawing.type === 'long_pos';
        const isSelected = drawing.id === selectedDrawingId;

        overlayCtx.save();

        // Highlight Border if Selected
        if (isSelected) {
            overlayCtx.lineWidth = 2;
            overlayCtx.strokeStyle = isLong ? '#089981' : '#f23645';
            overlayCtx.setLineDash([2, 2]);
            const minY = Math.min(yEntry, yTP, ySL);
            const maxY = Math.max(yEntry, yTP, ySL);
            overlayCtx.strokeRect(x1 - 2, minY - 2, width + 4, (maxY - minY) + 4);
            overlayCtx.setLineDash([]);
        }

        // 1. Take Profit Zone (Green)
        const tpTop = Math.min(yEntry, yTP);
        const tpHeight = Math.abs(yEntry - yTP);
        overlayCtx.fillStyle = 'rgba(8, 153, 129, 0.20)';
        overlayCtx.fillRect(x1, tpTop, width, tpHeight);
        overlayCtx.strokeStyle = '#089981';
        overlayCtx.lineWidth = 1.2;
        overlayCtx.strokeRect(x1, tpTop, width, tpHeight);

        // 2. Stop Loss Zone (Red)
        const slTop = Math.min(yEntry, ySL);
        const slHeight = Math.abs(yEntry - ySL);
        overlayCtx.fillStyle = 'rgba(242, 54, 69, 0.20)';
        overlayCtx.fillRect(x1, slTop, width, slHeight);
        overlayCtx.strokeStyle = '#f23645';
        overlayCtx.lineWidth = 1.2;
        overlayCtx.strokeRect(x1, slTop, width, slHeight);

        // 3. Entry Line (Blue / Middle)
        overlayCtx.beginPath();
        overlayCtx.setLineDash([4, 4]);
        overlayCtx.strokeStyle = '#2962FF';
        overlayCtx.lineWidth = 2;
        overlayCtx.moveTo(x1, yEntry);
        overlayCtx.lineTo(x2, yEntry);
        overlayCtx.stroke();
        overlayCtx.setLineDash([]);

        // 4. Metrics Calculations for XAUUSD (Gold: $1 = 10 pips, 1 Lot = 100 oz)
        const risk = Math.abs(drawing.entryPrice - drawing.slPrice);
        const reward = Math.abs(drawing.tpPrice - drawing.entryPrice);
        const rrRatio = risk > 0 ? (reward / risk).toFixed(2) : '0.00';
        
        const lots = drawing.lots || 1.0;
        const targetPips = (reward * 10).toFixed(1);
        const slPips = (risk * 10).toFixed(1);
        const targetPnL = (reward * 100 * lots).toFixed(2);
        const slPnL = (risk * 100 * lots).toFixed(2);

        // 5. Position Info Callout Badge
        const cardWidth = Math.min(width - 10, 240);
        const cardHeight = 36;
        const cardX = x1 + (width - cardWidth) / 2;
        const cardY = isLong ? Math.max(10, tpTop + 6) : Math.max(10, slTop + 6);

        overlayCtx.fillStyle = 'rgba(19, 23, 34, 0.88)';
        overlayCtx.beginPath();
        if (overlayCtx.roundRect) overlayCtx.roundRect(cardX, cardY, cardWidth, cardHeight, 5);
        else overlayCtx.rect(cardX, cardY, cardWidth, cardHeight);
        overlayCtx.fill();
        overlayCtx.strokeStyle = isLong ? '#089981' : '#f23645';
        overlayCtx.lineWidth = 1.5;
        overlayCtx.stroke();

        overlayCtx.fillStyle = '#089981';
        overlayCtx.font = '600 11px sans-serif';
        overlayCtx.textAlign = 'center';
        overlayCtx.fillText(
            `R:R ${rrRatio}  |  TP: +$${targetPnL} (+${targetPips} pips)`,
            cardX + cardWidth / 2,
            cardY + 14
        );
        overlayCtx.fillStyle = '#f23645';
        overlayCtx.fillText(
            `SL: -$${slPnL} (-${slPips} pips)  |  Lots: ${lots}`,
            cardX + cardWidth / 2,
            cardY + 28
        );

        // 6. Interactive Drag Handles
        const handleX = x1 + width / 2;
        drawHandleNode(handleX, yTP, '#089981', isSelected);
        drawHandleNode(handleX, yEntry, '#2962FF', isSelected);
        drawHandleNode(handleX, ySL, '#f23645', isSelected);

        overlayCtx.restore();
    }

    function drawTrendline(drawing, timeToX, priceToY) {
        const x1 = timeToX(drawing.point1.time);
        const y1 = priceToY(drawing.point1.price);
        const x2 = timeToX(drawing.point2.time);
        const y2 = priceToY(drawing.point2.price);

        if (x1 === null || y1 === null || x2 === null || y2 === null) return;

        const isSelected = drawing.id === selectedDrawingId;

        overlayCtx.save();
        overlayCtx.beginPath();
        overlayCtx.lineWidth = isSelected ? 2.5 : 1.8;
        overlayCtx.strokeStyle = drawing.color || '#2962FF';
        overlayCtx.moveTo(x1, y1);
        overlayCtx.lineTo(x2, y2);
        overlayCtx.stroke();

        drawHandleNode(x1, y1, '#2962FF', isSelected);
        drawHandleNode(x2, y2, '#2962FF', isSelected);
        overlayCtx.restore();
    }

    function drawFibonacci(drawing, canvasWidth, timeToX, priceToY) {
        const x1 = timeToX(drawing.point1.time);
        const y1 = priceToY(drawing.point1.price);
        const x2 = timeToX(drawing.point2.time);
        const y2 = priceToY(drawing.point2.price);

        if (x1 === null || y1 === null || x2 === null || y2 === null) return;

        const minX = Math.min(x1, x2);
        const maxX = Math.max(x1, x2, minX + 150);
        const p1 = drawing.point1.price;
        const p2 = drawing.point2.price;
        const diff = p2 - p1;

        const fibLevels = [
            { ratio: 0.0, color: '#787B86' },
            { ratio: 0.236, color: '#F23645' },
            { ratio: 0.382, color: '#FF9800' },
            { ratio: 0.5, color: '#4CAF50' },
            { ratio: 0.618, color: '#089981' },
            { ratio: 0.786, color: '#2962FF' },
            { ratio: 1.0, color: '#787B86' }
        ];

        overlayCtx.save();
        fibLevels.forEach((lvl) => {
            const levelPrice = p1 + diff * lvl.ratio;
            const y = priceToY(levelPrice);
            if (y !== null && !isNaN(y)) {
                overlayCtx.beginPath();
                overlayCtx.strokeStyle = lvl.color;
                overlayCtx.lineWidth = 1;
                overlayCtx.moveTo(minX, y);
                overlayCtx.lineTo(maxX, y);
                overlayCtx.stroke();

                overlayCtx.fillStyle = lvl.color;
                overlayCtx.font = '10px sans-serif';
                overlayCtx.fillText(`${lvl.ratio} (${levelPrice.toFixed(2)})`, minX + 4, y - 3);
            }
        });
        overlayCtx.restore();
    }

    function drawRuler(drawing, timeToX, priceToY) {
        const x1 = timeToX(drawing.point1.time);
        const y1 = priceToY(drawing.point1.price);
        const x2 = timeToX(drawing.point2.time);
        const y2 = priceToY(drawing.point2.price);

        if (x1 === null || y1 === null || x2 === null || y2 === null) return;

        const priceDiff = drawing.point2.price - drawing.point1.price;
        const pct = ((priceDiff / drawing.point1.price) * 100).toFixed(2);
        const pips = (Math.abs(priceDiff) * 10).toFixed(1);

        overlayCtx.save();
        overlayCtx.fillStyle = priceDiff >= 0 ? 'rgba(8, 153, 129, 0.18)' : 'rgba(242, 54, 69, 0.18)';
        overlayCtx.strokeStyle = priceDiff >= 0 ? '#089981' : '#f23645';
        overlayCtx.lineWidth = 1.5;

        const rectX = Math.min(x1, x2);
        const rectY = Math.min(y1, y2);
        const rectW = Math.abs(x2 - x1);
        const rectH = Math.abs(y2 - y1);

        overlayCtx.fillRect(rectX, rectY, rectW, rectH);
        overlayCtx.strokeRect(rectX, rectY, rectW, rectH);

        // Callout badge
        overlayCtx.fillStyle = 'rgba(19, 23, 34, 0.85)';
        overlayCtx.beginPath();
        if (overlayCtx.roundRect) overlayCtx.roundRect(rectX + rectW / 2 - 75, rectY + rectH / 2 - 12, 150, 24, 4);
        else overlayCtx.rect(rectX + rectW / 2 - 75, rectY + rectH / 2 - 12, 150, 24);
        overlayCtx.fill();

        overlayCtx.fillStyle = '#ffffff';
        overlayCtx.font = 'bold 11px sans-serif';
        overlayCtx.textAlign = 'center';
        overlayCtx.fillText(`${priceDiff >= 0 ? '+' : ''}${priceDiff.toFixed(2)} (${pct}%) ${pips} pips`, rectX + rectW / 2, rectY + rectH / 2 + 4);

        overlayCtx.restore();
    }

    function drawTempDrawing(tempObj, canvasWidth, timeToX, priceToY) {
        const x1 = timeToX(tempObj.point1.time);
        const y1 = priceToY(tempObj.point1.price);
        const x2 = tempObj.mousePos ? tempObj.mousePos.x : x1;
        const y2 = tempObj.mousePos ? tempObj.mousePos.y : y1;

        if (x1 === null || y1 === null) return;

        overlayCtx.save();
        overlayCtx.beginPath();
        overlayCtx.setLineDash([4, 4]);
        overlayCtx.strokeStyle = '#2962FF';
        overlayCtx.lineWidth = 1.5;
        overlayCtx.moveTo(x1, y1);
        overlayCtx.lineTo(x2, y2);
        overlayCtx.stroke();
        overlayCtx.restore();
    }

    function drawHandleNode(x, y, color, isSelected) {
        overlayCtx.beginPath();
        overlayCtx.arc(x, y, isSelected ? 6 : 5, 0, 2 * Math.PI);
        overlayCtx.fillStyle = '#ffffff';
        overlayCtx.strokeStyle = color;
        overlayCtx.lineWidth = 2;
        overlayCtx.fill();
        overlayCtx.stroke();
    }

    // =========================================================================
    // FLOATING ACTION TOOLBAR & SELECTION CONTROLS
    // =========================================================================
    function setupFloatingBarEvents() {
        if (floatingBarDeleteBtn) {
            floatingBarDeleteBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                deleteSelectedDrawing();
            });
        }

        if (floatingBarSettingsBtn) {
            floatingBarSettingsBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (selectedDrawingId) {
                    const sel = drawings.find(d => d.id === selectedDrawingId);
                    if (sel && (sel.type === 'long_pos' || sel.type === 'short_pos')) {
                        openPositionSettingsModal(sel);
                    }
                }
            });
        }

        if (floatingBarCloseBtn) {
            floatingBarCloseBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                deselectDrawing();
            });
        }
    }

    function updateFloatingBarPosition() {
        if (!selectedDrawingId || !floatingDrawingBar || !mainChartCanvas) {
            if (floatingDrawingBar) floatingDrawingBar.classList.add('hidden');
            return;
        }

        const drawing = drawings.find(d => d.id === selectedDrawingId);
        if (!drawing || !mainChart || !candlestickSeries) {
            floatingDrawingBar.classList.add('hidden');
            return;
        }

        const priceToY = (p) => candlestickSeries.priceToCoordinate(p);
        const timeToX = (t) => mainChart.timeScale().timeToCoordinate(t);

        let anchorX = mainChartCanvas.clientWidth / 2;
        let anchorY = 60;

        if (drawing.type === 'hline') {
            const y = priceToY(drawing.price);
            if (y !== null && !isNaN(y)) anchorY = Math.max(50, y - 35);
        } else if (drawing.type === 'long_pos' || drawing.type === 'short_pos') {
            const yEntry = priceToY(drawing.entryPrice);
            const x1 = drawing.startTime ? timeToX(drawing.startTime) : null;
            if (x1 !== null && x1 !== undefined) anchorX = x1 + 110;
            if (yEntry !== null && !isNaN(yEntry)) anchorY = Math.max(50, yEntry - 45);
        } else if (drawing.type === 'trendline' || drawing.type === 'fib' || drawing.type === 'ruler') {
            const x1 = timeToX(drawing.point1.time);
            const y1 = priceToY(drawing.point1.price);
            if (x1 !== null && y1 !== null) {
                anchorX = x1;
                anchorY = Math.max(50, y1 - 35);
            }
        }

        floatingBarTitle.textContent = getDrawingDisplayName(drawing.type);
        
        // Show or hide settings gear icon depending on type
        if (drawing.type === 'long_pos' || drawing.type === 'short_pos') {
            floatingBarSettingsBtn.style.display = 'flex';
        } else {
            floatingBarSettingsBtn.style.display = 'none';
        }

        const barWidth = 160;
        anchorX = Math.max(60, Math.min(mainChartCanvas.clientWidth - barWidth - 20, anchorX - barWidth / 2));

        floatingDrawingBar.style.left = `${anchorX + 44}px`;
        floatingDrawingBar.style.top = `${anchorY + 44}px`;
        floatingDrawingBar.classList.remove('hidden');
    }

    function selectDrawing(drawingId) {
        selectedDrawingId = drawingId;
        redrawCanvasOverlay();
        updateFloatingBarPosition();
    }

    function deselectDrawing() {
        selectedDrawingId = null;
        if (floatingDrawingBar) floatingDrawingBar.classList.add('hidden');
        redrawCanvasOverlay();
    }

    function deleteSelectedDrawing() {
        if (!selectedDrawingId) return;
        drawings = drawings.filter(d => d.id !== selectedDrawingId);
        selectedDrawingId = null;
        if (floatingDrawingBar) floatingDrawingBar.classList.add('hidden');
        saveDrawingsToLocalStorage();
        redrawCanvasOverlay();
    }

    function getDrawingDisplayName(type) {
        if (type === 'long_pos') return 'Long Position';
        if (type === 'short_pos') return 'Short Position';
        if (type === 'hline') return 'Price Line';
        if (type === 'trendline') return 'Trendline';
        if (type === 'fib') return 'Fibonacci';
        if (type === 'ruler') return 'Ruler';
        return 'Drawing';
    }

    // Setup Event Handling for Drawing Canvas
    function setupDrawingCanvasEvents() {
        if (!drawingCanvasOverlay) return;

        // Toolbar Buttons Selection
        toolButtons.forEach((btn) => {
            btn.addEventListener('click', () => {
                const tool = btn.dataset.tool;
                if (!tool) return;

                toolButtons.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                activeTool = tool;
                tempDrawingPoint = null;

            });
        });

        if (toolHideDrawings) {
            toolHideDrawings.addEventListener('click', () => {
                hideDrawings = !hideDrawings;
                toolHideDrawings.innerHTML = hideDrawings ? '<i class="fa-regular fa-eye-slash"></i>' : '<i class="fa-regular fa-eye"></i>';
                redrawCanvasOverlay();
            });
        }

        if (toolClearDrawings) {
            toolClearDrawings.addEventListener('click', () => {
                clearAllDrawings();
            });
        }

        mainChartCanvas.addEventListener('mousedown', (e) => {
            if (e.button !== 0) return; // Only process left click

            const rect = mainChartCanvas.getBoundingClientRect();
            const mouseX = e.clientX - rect.left;
            const mouseY = e.clientY - rect.top;

            if (!candlestickSeries || !mainChart) return;
            const price = candlestickSeries.coordinateToPrice(mouseY);
            const time = mainChart.timeScale().coordinateToTime(mouseX);

            // 1. Check if clicking on an existing handle
            const hitHandle = findHandleAt(mouseX, mouseY);
            if (hitHandle) {
                e.stopPropagation();
                dragHandle = hitHandle;
                selectDrawing(hitHandle.drawingId);
                return;
            }

            // 2. Check if clicking directly on an existing drawing object
            const hitDrawing = findDrawingAt(mouseX, mouseY);
            if (hitDrawing && activeTool === 'crosshair') {
                e.stopPropagation();
                selectDrawing(hitDrawing.id);
                return;
            }

            // 3. Add New Drawings Based on Active Tool
            if (activeTool !== 'crosshair') {
                e.stopPropagation();
                if (activeTool === 'hline' && price) {
                    addHorizontalLine(price);
                    setActiveTool('crosshair');
                } else if (activeTool === 'long_pos' && price) {
                    addPositionMapper('long_pos', price, time);
                    setActiveTool('crosshair');
                } else if (activeTool === 'short_pos' && price) {
                    addPositionMapper('short_pos', price, time);
                    setActiveTool('crosshair');
                } else if ((activeTool === 'trendline' || activeTool === 'fib' || activeTool === 'ruler') && price && time) {
                    if (!tempDrawingPoint) {
                        tempDrawingPoint = { type: activeTool, point1: { time, price }, mousePos: { x: mouseX, y: mouseY } };
                    } else {
                        addTwoPointDrawing(activeTool, tempDrawingPoint.point1, { time, price });
                        tempDrawingPoint = null;
                        setActiveTool('crosshair');
                    }
                }
            } else if (!hitDrawing) {
                // Clicked on empty canvas space -> deselect
                deselectDrawing();
            }
        }, true);

        mainChartCanvas.addEventListener('mousemove', (e) => {
            const rect = mainChartCanvas.getBoundingClientRect();
            const mouseX = e.clientX - rect.left;
            const mouseY = e.clientY - rect.top;

            if (!candlestickSeries || !mainChart) return;

            // Update Dragging Handle
            if (dragHandle) {
                e.stopPropagation();
                const newPrice = candlestickSeries.coordinateToPrice(mouseY);
                const newTime = mainChart.timeScale().coordinateToTime(mouseX);
                if (newPrice) {
                    updateDrawingHandle(dragHandle, newPrice, newTime);
                    redrawCanvasOverlay();
                    updateFloatingBarPosition();
                }
                return;
            }

            // Update Temp Drawing Cursor Line
            if (tempDrawingPoint) {
                e.stopPropagation();
                tempDrawingPoint.mousePos = { x: mouseX, y: mouseY };
                redrawCanvasOverlay();
                return;
            }

            // Update Cursor Style on Hover over Handles or Drawings
            const hitHandle = findHandleAt(mouseX, mouseY);
            const hitDrawing = findDrawingAt(mouseX, mouseY);

            // Find lightweight chart internal canvas to manipulate its cursor
            const lwCanvas = mainChartCanvas.querySelector('canvas');
            const targetCanvas = lwCanvas || mainChartCanvas;

            if (hitHandle) {
                e.stopPropagation();
                targetCanvas.style.cursor = 'ns-resize';
            } else if (hitDrawing) {
                e.stopPropagation();
                targetCanvas.style.cursor = 'pointer';
            } else {
                if (activeTool !== 'crosshair') {
                    e.stopPropagation();
                    targetCanvas.style.cursor = 'crosshair';
                } else {
                    targetCanvas.style.cursor = ''; // Let LW charts decide
                }
            }
        }, true);

        mainChartCanvas.addEventListener('mouseup', (e) => {
            if (dragHandle) {
                e.stopPropagation();
                dragHandle = null;
                saveDrawingsToLocalStorage();
            } else if (tempDrawingPoint) {
                e.stopPropagation();
            }
        }, true);

        mainChartCanvas.addEventListener('dblclick', (e) => {
            const rect = mainChartCanvas.getBoundingClientRect();
            const mouseX = e.clientX - rect.left;
            const mouseY = e.clientY - rect.top;

            const hitDrawing = findDrawingAt(mouseX, mouseY);
            if (hitDrawing && (hitDrawing.type === 'long_pos' || hitDrawing.type === 'short_pos')) {
                e.stopPropagation();
                openPositionSettingsModal(hitDrawing);
            }
        }, true);
    }

    function setActiveTool(toolName) {
        activeTool = toolName;
        toolButtons.forEach(b => b.classList.remove('active'));
        const matched = document.querySelector(`.tv-left-toolbar .tool-btn[data-tool="${toolName}"]`);
        if (matched) matched.classList.add('active');
    }


    function addHorizontalLine(price) {
        const id = `draw_${drawingIdCounter++}`;
        const newDrawing = {
            id: id,
            type: 'hline',
            price: price,
            color: '#2962FF',
            dashed: false
        };
        drawings.push(newDrawing);
        selectDrawing(id);
        saveDrawingsToLocalStorage();
        redrawCanvasOverlay();
    }

    function addPositionMapper(type, entryPrice, startTime) {
        const id = `draw_${drawingIdCounter++}`;
        const isLong = type === 'long_pos';
        
        // Default target: +10.00 / SL: -5.00 for XAUUSD Gold
        const tpPrice = isLong ? entryPrice + 10.00 : entryPrice - 10.00;
        const slPrice = isLong ? entryPrice - 5.00 : entryPrice + 5.00;

        const newDrawing = {
            id: id,
            type: type,
            entryPrice: roundVal(entryPrice, 3),
            tpPrice: roundVal(tpPrice, 3),
            slPrice: roundVal(slPrice, 3),
            startTime: startTime || (currentOHLCData.length > 0 ? currentOHLCData[currentOHLCData.length - 1].time : null),
            endTime: null,
            lots: 1.0
        };

        drawings.push(newDrawing);
        selectDrawing(id);
        saveDrawingsToLocalStorage();
        redrawCanvasOverlay();
    }

    function addTwoPointDrawing(type, point1, point2) {
        const id = `draw_${drawingIdCounter++}`;
        const newDrawing = {
            id: id,
            type: type,
            point1: point1,
            point2: point2,
            color: '#2962FF'
        };
        drawings.push(newDrawing);
        selectDrawing(id);
        saveDrawingsToLocalStorage();
        redrawCanvasOverlay();
    }

    function distToSegment(px, py, x1, y1, x2, y2) {
        const l2 = (x2 - x1) * (x2 - x1) + (y2 - y1) * (y2 - y1);
        if (l2 === 0) return Math.hypot(px - x1, py - y1);
        let t = ((px - x1) * (x2 - x1) + (py - y1) * (y2 - y1)) / l2;
        t = Math.max(0, Math.min(1, t));
        return Math.hypot(px - (x1 + t * (x2 - x1)), py - (y1 + t * (y2 - y1)));
    }

    function findHandleAt(mouseX, mouseY) {
        if (!mainChart || !candlestickSeries) return null;
        const timeToX = (t) => mainChart.timeScale().timeToCoordinate(t);
        const priceToY = (p) => candlestickSeries.priceToCoordinate(p);

        for (let i = drawings.length - 1; i >= 0; i--) {
            const d = drawings[i];

            if (d.type === 'hline') {
                const y = priceToY(d.price);
                if (y !== null && Math.abs(mouseY - y) <= 8) {
                    return { drawingId: d.id, handleType: 'line' };
                }
            } else if (d.type === 'long_pos' || d.type === 'short_pos') {
                const yEntry = priceToY(d.entryPrice);
                const yTP = priceToY(d.tpPrice);
                const ySL = priceToY(d.slPrice);
                
                let x1 = d.startTime ? timeToX(d.startTime) : null;
                const canvasW = mainChartCanvas.clientWidth;
                if (x1 === null || x1 === undefined) x1 = Math.max(40, canvasW - 320);
                const handleX = x1 + 110;

                if (yTP !== null && Math.hypot(mouseX - handleX, mouseY - yTP) <= 10) {
                    return { drawingId: d.id, handleType: 'tp' };
                }
                if (yEntry !== null && Math.hypot(mouseX - handleX, mouseY - yEntry) <= 10) {
                    return { drawingId: d.id, handleType: 'entry' };
                }
                if (ySL !== null && Math.hypot(mouseX - handleX, mouseY - ySL) <= 10) {
                    return { drawingId: d.id, handleType: 'sl' };
                }
            } else if (d.type === 'trendline' || d.type === 'fib' || d.type === 'ruler') {
                const x1 = timeToX(d.point1.time);
                const y1 = priceToY(d.point1.price);
                const x2 = timeToX(d.point2.time);
                const y2 = priceToY(d.point2.price);

                if (x1 !== null && y1 !== null && Math.hypot(mouseX - x1, mouseY - y1) <= 10) {
                    return { drawingId: d.id, handleType: 'p1' };
                }
                if (x2 !== null && y2 !== null && Math.hypot(mouseX - x2, mouseY - y2) <= 10) {
                    return { drawingId: d.id, handleType: 'p2' };
                }
            }
        }
        return null;
    }

    function findDrawingAt(mouseX, mouseY) {
        if (!mainChart || !candlestickSeries) return null;
        const priceToY = (p) => candlestickSeries.priceToCoordinate(p);
        const timeToX = (t) => mainChart.timeScale().timeToCoordinate(t);

        for (let i = drawings.length - 1; i >= 0; i--) {
            const d = drawings[i];

            if (d.type === 'long_pos' || d.type === 'short_pos') {
                const yEntry = priceToY(d.entryPrice);
                const yTP = priceToY(d.tpPrice);
                const ySL = priceToY(d.slPrice);
                let x1 = d.startTime ? timeToX(d.startTime) : null;
                const canvasW = mainChartCanvas.clientWidth;
                if (x1 === null || x1 === undefined) x1 = Math.max(40, canvasW - 320);
                const x2 = x1 + 220;

                if (yEntry !== null && yTP !== null && ySL !== null) {
                    const minY = Math.min(yEntry, yTP, ySL);
                    const maxY = Math.max(yEntry, yTP, ySL);
                    if (mouseX >= x1 - 10 && mouseX <= x2 + 10 && mouseY >= minY - 10 && mouseY <= maxY + 10) {
                        return d;
                    }
                }
            } else if (d.type === 'hline') {
                const y = priceToY(d.price);
                if (y !== null && Math.abs(mouseY - y) <= 8) {
                    return d;
                }
            } else if (d.type === 'trendline') {
                const x1 = timeToX(d.point1.time);
                const y1 = priceToY(d.point1.price);
                const x2 = timeToX(d.point2.time);
                const y2 = priceToY(d.point2.price);
                if (x1 !== null && y1 !== null && x2 !== null && y2 !== null) {
                    if (distToSegment(mouseX, mouseY, x1, y1, x2, y2) <= 8) {
                        return d;
                    }
                }
            } else if (d.type === 'fib' || d.type === 'ruler') {
                const x1 = timeToX(d.point1.time);
                const y1 = priceToY(d.point1.price);
                const x2 = timeToX(d.point2.time);
                const y2 = priceToY(d.point2.price);
                if (x1 !== null && y1 !== null && x2 !== null && y2 !== null) {
                    const minX = Math.min(x1, x2);
                    const maxX = Math.max(x1, x2, minX + 150);
                    const minY = Math.min(y1, y2);
                    const maxY = Math.max(y1, y2);
                    if (mouseX >= minX - 10 && mouseX <= maxX + 10 && mouseY >= minY - 10 && mouseY <= maxY + 10) {
                        return d;
                    }
                }
            }
        }
        return null;
    }

    function updateDrawingHandle(handleObj, newPrice, newTime) {
        const drawing = drawings.find(d => d.id === handleObj.drawingId);
        if (!drawing) return;

        if (handleObj.handleType === 'line') {
            drawing.price = roundVal(newPrice, 3);
        } else if (handleObj.handleType === 'tp') {
            drawing.tpPrice = roundVal(newPrice, 3);
        } else if (handleObj.handleType === 'sl') {
            drawing.slPrice = roundVal(newPrice, 3);
        } else if (handleObj.handleType === 'entry') {
            const diff = newPrice - drawing.entryPrice;
            drawing.entryPrice = roundVal(newPrice, 3);
            drawing.tpPrice = roundVal(drawing.tpPrice + diff, 3);
            drawing.slPrice = roundVal(drawing.slPrice + diff, 3);
        } else if (handleObj.handleType === 'p1') {
            drawing.point1.price = roundVal(newPrice, 3);
            if (newTime) drawing.point1.time = newTime;
        } else if (handleObj.handleType === 'p2') {
            drawing.point2.price = roundVal(newPrice, 3);
            if (newTime) drawing.point2.time = newTime;
        }
    }

    function clearAllDrawings() {
        drawings = [];
        selectedDrawingId = null;
        tempDrawingPoint = null;
        if (floatingDrawingBar) floatingDrawingBar.classList.add('hidden');
        localStorage.removeItem('tv_drawings');
        redrawCanvasOverlay();
    }

    function roundVal(val, decimals = 3) {
        return parseFloat(val.toFixed(decimals));
    }

    // Context Menu Action Handlers for Price Lines & Mappers
    if (ctxDeleteSelectedDrawing) {
        ctxDeleteSelectedDrawing.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            deleteSelectedDrawing();
        });
    }

    if (ctxAddHLine) {
        ctxAddHLine.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            if (lastContextMenuPrice > 0) {
                addHorizontalLine(lastContextMenuPrice);
            }
        });
    }

    if (ctxAddLongPos) {
        ctxAddLongPos.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            if (lastContextMenuPrice > 0) {
                addPositionMapper('long_pos', lastContextMenuPrice, null);
            }
        });
    }

    if (ctxAddShortPos) {
        ctxAddShortPos.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            if (lastContextMenuPrice > 0) {
                addPositionMapper('short_pos', lastContextMenuPrice, null);
            }
        });
    }

    if (ctxClearDrawings) {
        ctxClearDrawings.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            clearAllDrawings();
        });
    }

    // Position Settings Modal Functions
    function openPositionSettingsModal(posDrawing) {
        editingPositionId = posDrawing.id;
        posModalTitle.textContent = `Configure ${posDrawing.type === 'long_pos' ? 'Long' : 'Short'} Position`;
        posTypeSelect.value = posDrawing.type === 'long_pos' ? 'long' : 'short';
        posEntryInput.value = posDrawing.entryPrice;
        posTpInput.value = posDrawing.tpPrice;
        posSlInput.value = posDrawing.slPrice;
        posLotsInput.value = posDrawing.lots || 1.0;

        positionModal.classList.remove('hidden');
    }

    if (closePosModal) closePosModal.addEventListener('click', () => positionModal.classList.add('hidden'));
    if (cancelPosModal) cancelPosModal.addEventListener('click', () => positionModal.classList.add('hidden'));

    if (savePosModal) {
        savePosModal.addEventListener('click', () => {
            if (!editingPositionId) return;
            const drawing = drawings.find(d => d.id === editingPositionId);
            if (drawing) {
                drawing.type = posTypeSelect.value === 'long' ? 'long_pos' : 'short_pos';
                drawing.entryPrice = parseFloat(posEntryInput.value) || drawing.entryPrice;
                drawing.tpPrice = parseFloat(posTpInput.value) || drawing.tpPrice;
                drawing.slPrice = parseFloat(posSlInput.value) || drawing.slPrice;
                drawing.lots = parseFloat(posLotsInput.value) || 1.0;

                saveDrawingsToLocalStorage();
                redrawCanvasOverlay();
                updateFloatingBarPosition();
            }
            positionModal.classList.add('hidden');
        });
    }

    // =========================================================================
    // 3. DATA FETCHING & TIMEFRAME UPDATES
    // =========================================================================
    async function loadDatasetInfo() {
        try {
            const res = await fetch('/api/info');
            const json = await res.json();
            if (json.status === 'success') {
                const info = json.data;
                brokerTag.textContent = info.broker || 'MULTIBANK';
            }
        } catch (err) {
            console.error('Failed to load dataset info:', err);
        }
    }

    async function loadOHLCData(tf = '15m') {
        currentTimeframe = tf;
        legendTf.textContent = tf;
        headerSpinner.classList.remove('hidden');
        saveSettingsToLocalStorage();

        try {
            const res = await fetch(`/api/ohlc?tf=${tf}`);
            const json = await res.json();
            if (json.status === 'success' && json.data) {
                currentOHLCData = json.data;
                candlestickSeries.setData(currentOHLCData);
                
                updateLegendToLastBar();
                jumpToLatestCandles();
                recalculateActiveIndicators();
                redrawCanvasOverlay();
                updateFloatingBarPosition();
            }
        } catch (err) {
            console.error(`Failed to fetch OHLC for ${tf}:`, err);
        } finally {
            headerSpinner.classList.add('hidden');
        }
    }

    function jumpToLatestCandles() {
        const totalBars = currentOHLCData.length;
        if (totalBars > 0 && mainChart) {
            mainChart.timeScale().setVisibleLogicalRange({
                from: Math.max(0, totalBars - 150),
                to: totalBars - 1
            });
        }
    }

    function updateLegendOHLC(bar, prevClose = null) {
        valO.textContent = bar.open.toFixed(3);
        valH.textContent = bar.high.toFixed(3);
        valL.textContent = bar.low.toFixed(3);
        valC.textContent = bar.close.toFixed(3);

        const diff = prevClose ? (bar.close - prevClose) : (bar.close - bar.open);
        const pct = prevClose ? ((diff / prevClose) * 100) : ((diff / bar.open) * 100);

        valChange.textContent = `${diff >= 0 ? '+' : ''}${diff.toFixed(3)} (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%)`;
        valChange.className = `ohlc-change ${diff >= 0 ? 'up' : 'down'}`;

        const sellP = bar.close;
        const buyP = bar.close + 0.61;
        quoteSellPrice.textContent = sellP.toFixed(3);
        quoteBuyPrice.textContent = buyP.toFixed(3);
        quoteSpread.textContent = "61.0";
    }

    function updateLegendToLastBar() {
        if (currentOHLCData && currentOHLCData.length > 0) {
            const lastBar = currentOHLCData[currentOHLCData.length - 1];
            const prevBar = currentOHLCData.length > 1 ? currentOHLCData[currentOHLCData.length - 2] : null;
            updateLegendOHLC(lastBar, prevBar ? prevBar.close : null);
        }
    }

    // =========================================================================
    // 4. RESET CHART ENGINE & RIGHT-CLICK CONTEXT MENU ACTIONS
    // =========================================================================
    function removeAllIndicators() {
        const activeIds = Array.from(activeIndicators.keys());
        activeIds.forEach((id) => {
            removeIndicatorSeries(id, true);
        });

        candlestickSeries.setMarkers([]);
        subChartCanvasWrapper.classList.add('hidden');
        saveSettingsToLocalStorage();
    }

    function resetChartAll() {
        headerSpinner.classList.remove('hidden');

        removeAllIndicators();
        clearAllDrawings();
        localStorage.removeItem('tv_active_indicators');
        localStorage.removeItem('tv_drawings');

        tfButtons.forEach(b => b.classList.remove('active'));
        const default15mBtn = document.querySelector('.tf-btn[data-tf="15m"]');
        if (default15mBtn) default15mBtn.classList.add('active');

        rangeButtons.forEach(b => b.classList.remove('active'));
        const default1YBtn = document.querySelector('.range-btn[data-range="1Y"]');
        if (default1YBtn) default1YBtn.classList.add('active');

        jumpToLatestCandles();

        loadDefaultSupertrend().finally(() => {
            headerSpinner.classList.add('hidden');
            saveSettingsToLocalStorage();
        });
    }

    // Context Menu Action Handlers
    if (ctxResetView) {
        ctxResetView.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            jumpToLatestCandles();
        });
    }

    if (ctxRemoveIndicators) {
        ctxRemoveIndicators.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            removeAllIndicators();
        });
    }

    if (ctxResetAll) {
        ctxResetAll.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            resetChartAll();
        });
    }

    if (ctxFitContent) {
        ctxFitContent.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            if (mainChart) mainChart.timeScale().fitContent();
        });
    }

    if (ctxToggleTheme) {
        ctxToggleTheme.addEventListener('click', () => {
            chartContextMenu.classList.add('hidden');
            themeToggleBtn.click();
        });
    }

    if (leftResetBtn) {
        leftResetBtn.addEventListener('click', () => resetChartAll());
    }

    // =========================================================================
    // 5. INDICATOR MANAGEMENT & RE-CONFIGURATION ENGINE
    // =========================================================================
    async function loadAvailableIndicators() {
        try {
            const res = await fetch('/api/indicators/list');
            const json = await res.json();
            if (json.status === 'success') {
                availableIndicatorsList = json.indicators;
                renderIndicatorsModalList(availableIndicatorsList);
            }
        } catch (err) {
            console.error('Failed to fetch available indicators:', err);
        }
    }

    function renderIndicatorsModalList(list) {
        indicatorsListContainer.innerHTML = '';
        list.forEach((ind) => {
            const card = document.createElement('div');
            card.className = 'indicator-card';
            card.innerHTML = `
                <div class="indicator-info">
                    <h5>${ind.display_name}</h5>
                    <span>${ind.category} • ${ind.overlay ? 'Chart Overlay' : 'Separate Sub-Pane'}</span>
                </div>
                <button class="add-btn">Add to Chart</button>
            `;
            card.addEventListener('click', () => openIndicatorConfigForNew(ind));
            indicatorsListContainer.appendChild(card);
        });
    }

    function openIndicatorConfigForNew(indicator) {
        editingIndicatorId = null;
        selectedIndicatorForConfig = indicator;
        modalHeaderTitle.textContent = "Indicators & Strategies";
        modalSearchBox.classList.add('hidden');
        indicatorsListContainer.classList.add('hidden');
        
        configTitle.textContent = `Configure ${indicator.display_name}`;
        paramsForm.innerHTML = '';

        indicator.params.forEach((p) => {
            const group = document.createElement('div');
            group.className = 'form-group';
            
            const label = document.createElement('label');
            label.textContent = p.label;
            
            const input = document.createElement('input');
            input.type = p.type === 'color' ? 'color' : (p.type === 'float' || p.type === 'int' ? 'number' : 'text');
            if (p.type === 'float') input.step = '0.01';
            input.value = p.default;
            input.dataset.paramName = p.name;
            input.dataset.paramType = p.type;

            group.appendChild(label);
            group.appendChild(input);
            paramsForm.appendChild(group);
        });

        indicatorConfigPanel.classList.remove('hidden');
        indicatorsModal.classList.remove('hidden');
    }

    function openIndicatorConfigForEdit(indId) {
        if (!activeIndicators.has(indId)) return;
        const indObj = activeIndicators.get(indId);

        editingIndicatorId = indId;
        selectedIndicatorForConfig = indObj.meta;

        modalHeaderTitle.textContent = "Edit Indicator Settings";
        modalSearchBox.classList.add('hidden');
        indicatorsListContainer.classList.add('hidden');
        
        configTitle.textContent = `Edit Settings: ${indObj.title}`;
        paramsForm.innerHTML = '';

        indObj.meta.params.forEach((p) => {
            const group = document.createElement('div');
            group.className = 'form-group';
            
            const label = document.createElement('label');
            label.textContent = p.label;
            
            const input = document.createElement('input');
            input.type = p.type === 'color' ? 'color' : (p.type === 'float' || p.type === 'int' ? 'number' : 'text');
            if (p.type === 'float') input.step = '0.01';
            
            const currentVal = indObj.params[p.name] !== undefined ? indObj.params[p.name] : p.default;
            input.value = currentVal;
            input.dataset.paramName = p.name;
            input.dataset.paramType = p.type;

            group.appendChild(label);
            group.appendChild(input);
            paramsForm.appendChild(group);
        });

        indicatorConfigPanel.classList.remove('hidden');
        indicatorsModal.classList.remove('hidden');
    }

    async function applySelectedIndicator() {
        if (!selectedIndicatorForConfig) return;

        const paramInputs = paramsForm.querySelectorAll('input');
        const params = {};

        paramInputs.forEach((inp) => {
            const pName = inp.dataset.paramName;
            const pType = inp.dataset.paramType;
            if (pType === 'int') params[pName] = parseInt(inp.value, 10);
            else if (pType === 'float') params[pName] = parseFloat(inp.value);
            else params[pName] = inp.value;
        });

        const targetId = editingIndicatorId ? editingIndicatorId : `ind_${indicatorIdCounter++}`;

        headerSpinner.classList.remove('hidden');
        await computeAndAttachIndicator(targetId, selectedIndicatorForConfig, params);
        headerSpinner.classList.add('hidden');

        saveSettingsToLocalStorage();
        resetModalState();
    }

    function resetModalState() {
        editingIndicatorId = null;
        selectedIndicatorForConfig = null;
        modalHeaderTitle.textContent = "Indicators & Strategies";
        modalSearchBox.classList.remove('hidden');
        indicatorsListContainer.classList.remove('hidden');
        indicatorConfigPanel.classList.add('hidden');
        indicatorsModal.classList.add('hidden');
    }

    async function computeAndAttachIndicator(indId, meta, params) {
        try {
            const res = await fetch('/api/indicators/calculate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: meta.name,
                    timeframe: currentTimeframe,
                    params: params
                })
            });

            const json = await res.json();
            if (json.status === 'success') {
                const indResult = json.indicator;

                if (activeIndicators.has(indId)) {
                    removeIndicatorSeries(indId, false);
                }

                const attachedObj = renderIndicatorSeries(indId, meta, params, indResult);
                activeIndicators.set(indId, attachedObj);

                renderOrUpdateIndicatorLegendItem(indId, attachedObj);
                saveSettingsToLocalStorage();
            }
        } catch (err) {
            console.error('Error calculating indicator:', err);
        }
    }

    function renderIndicatorSeries(indId, meta, params, indResult) {
        const seriesList = [];
        let legendValSpan = null;

        if (indResult.type === 'supertrend') {
            const stLineData = indResult.series.st_line;
            const markers = indResult.series.markers;

            const stSeries = mainChart.addLineSeries({
                color: '#089981',
                lineWidth: 2,
                title: indResult.title
            });

            const stColoredData = stLineData.map(d => ({
                time: d.time,
                value: d.value,
                color: d.direction === 1 ? '#089981' : '#f23645'
            }));
            stSeries.setData(stColoredData);

            candlestickSeries.setMarkers(markers);
            seriesList.push(stSeries);

            return {
                id: indId,
                meta: meta,
                params: params,
                title: indResult.title,
                seriesList: seriesList,
                dataMap: new Map(stLineData.map(d => [d.time, d.value])),
                updateLegendValue: (time) => {
                    if (legendValSpan) {
                        const val = stLineData.find(d => d.time === time);
                        legendValSpan.textContent = val ? val.value.toFixed(3) : '';
                    }
                }
            };
        }
        else if (indResult.type === 'line') {
            const lineSeries = mainChart.addLineSeries({
                color: params.color || indResult.color || '#2962FF',
                lineWidth: 2,
                title: indResult.title
            });
            lineSeries.setData(indResult.series);
            seriesList.push(lineSeries);

            const dataMap = new Map(indResult.series.map(d => [d.time, d.value]));

            return {
                id: indId,
                meta: meta,
                params: params,
                title: indResult.title,
                seriesList: seriesList,
                dataMap: dataMap,
                updateLegendValue: (time) => {
                    if (legendValSpan) {
                        const val = dataMap.get(time);
                        legendValSpan.textContent = val !== undefined ? val.toFixed(3) : '';
                    }
                }
            };
        }
        else if (indResult.type === 'oscillator') {
            subChartCanvasWrapper.classList.remove('hidden');
            subChartTitle.textContent = indResult.title;

            if (!subChart) initSubChart();

            Object.values(subChartSeriesMap).forEach(s => subChart.removeSeries(s));
            subChartSeriesMap = {};

            const rsiSeries = subChart.addLineSeries({
                color: params.color || indResult.color || '#7E57C2',
                lineWidth: 2,
            });
            rsiSeries.setData(indResult.series);
            subChartSeriesMap['rsi'] = rsiSeries;
            seriesList.push(rsiSeries);

            const dataMap = new Map(indResult.series.map(d => [d.time, d.value]));

            return {
                id: indId,
                meta: meta,
                params: params,
                title: indResult.title,
                seriesList: seriesList,
                dataMap: dataMap,
                isSubChart: true,
                updateLegendValue: (time) => {
                    if (legendValSpan) {
                        const val = dataMap.get(time);
                        legendValSpan.textContent = val !== undefined ? val.toFixed(2) : '';
                    }
                }
            };
        }
        else if (indResult.type === 'macd') {
            subChartCanvasWrapper.classList.remove('hidden');
            subChartTitle.textContent = indResult.title;

            if (!subChart) initSubChart();

            Object.values(subChartSeriesMap).forEach(s => subChart.removeSeries(s));
            subChartSeriesMap = {};

            const histSeries = subChart.addHistogramSeries({
                priceFormat: { type: 'volume' }
            });
            histSeries.setData(indResult.series.histogram);

            const macdSeries = subChart.addLineSeries({ color: '#2962FF', lineWidth: 2 });
            macdSeries.setData(indResult.series.macd);

            const sigSeries = subChart.addLineSeries({ color: '#FF6D00', lineWidth: 1 });
            sigSeries.setData(indResult.series.signal);

            subChartSeriesMap['hist'] = histSeries;
            subChartSeriesMap['macd'] = macdSeries;
            subChartSeriesMap['sig'] = sigSeries;

            seriesList.push(histSeries, macdSeries, sigSeries);

            return {
                id: indId,
                meta: meta,
                params: params,
                title: indResult.title,
                seriesList: seriesList,
                isSubChart: true,
                updateLegendValue: (time) => {}
            };
        }
        else if (indResult.type === 'bollinger') {
            const upper = mainChart.addLineSeries({ color: '#2962FF', lineWidth: 1 });
            const middle = mainChart.addLineSeries({ color: '#FF6D00', lineWidth: 1 });
            const lower = mainChart.addLineSeries({ color: '#2962FF', lineWidth: 1 });

            upper.setData(indResult.series.upper);
            middle.setData(indResult.series.middle);
            lower.setData(indResult.series.lower);

            seriesList.push(upper, middle, lower);

            return {
                id: indId,
                meta: meta,
                params: params,
                title: indResult.title,
                seriesList: seriesList,
                updateLegendValue: (time) => {}
            };
        }

        return null;
    }

    function renderOrUpdateIndicatorLegendItem(indId, attachedObj) {
        let item = document.getElementById(`legend_item_${indId}`);
        
        if (!item) {
            item = document.createElement('div');
            item.className = 'ind-legend-item';
            item.id = `legend_item_${indId}`;
            indicatorsLegend.appendChild(item);
        }

        item.innerHTML = `
            <span class="ind-title">${attachedObj.title}</span>
            <span class="ind-val" id="legend_val_${indId}"></span>
            <div class="ind-legend-controls">
                <button class="ind-icon-btn eye-btn" title="Toggle Visibility"><i class="fa-regular fa-eye"></i></button>
                <button class="ind-icon-btn gear-btn" title="Edit Settings"><i class="fa-solid fa-gear"></i></button>
                <button class="ind-icon-btn remove-btn" title="Remove Indicator"><i class="fa-solid fa-xmark"></i></button>
            </div>
        `;

        attachedObj.updateLegendValue = (time) => {
            const valSpan = item.querySelector(`#legend_val_${indId}`);
            if (valSpan && attachedObj.dataMap) {
                const v = attachedObj.dataMap.get(time);
                if (v !== undefined) valSpan.textContent = typeof v === 'number' ? v.toFixed(3) : v;
            }
        };

        let visible = true;
        const eyeBtn = item.querySelector('.eye-btn');
        eyeBtn.addEventListener('click', () => {
            visible = !visible;
            eyeBtn.innerHTML = visible ? '<i class="fa-regular fa-eye"></i>' : '<i class="fa-regular fa-eye-slash"></i>';
            attachedObj.seriesList.forEach(s => s.applyOptions({ visible: visible }));
        });

        const gearBtn = item.querySelector('.gear-btn');
        gearBtn.addEventListener('click', () => {
            openIndicatorConfigForEdit(indId);
        });

        const removeBtn = item.querySelector('.remove-btn');
        removeBtn.addEventListener('click', () => {
            removeIndicatorSeries(indId, true);
        });
    }

    function removeIndicatorSeries(indId, removeDom = true) {
        if (!activeIndicators.has(indId)) return;
        const indObj = activeIndicators.get(indId);

        indObj.seriesList.forEach(s => {
            if (indObj.isSubChart && subChart) {
                subChart.removeSeries(s);
            } else if (mainChart) {
                mainChart.removeSeries(s);
            }
        });

        if (indObj.isSubChart) {
            subChartCanvasWrapper.classList.add('hidden');
        }

        if (indObj.meta && indObj.meta.name === 'supertrend') {
            candlestickSeries.setMarkers([]);
        }

        if (removeDom) {
            const legendItem = document.getElementById(`legend_item_${indId}`);
            if (legendItem) legendItem.remove();
            activeIndicators.delete(indId);
            saveSettingsToLocalStorage();
        }
    }

    function recalculateActiveIndicators() {
        activeIndicators.forEach((indObj, indId) => {
            computeAndAttachIndicator(indId, indObj.meta, indObj.params);
        });
    }

    async function restoreOrLoadDefaultIndicators() {
        try {
            const savedInds = localStorage.getItem('tv_active_indicators');
            if (savedInds) {
                const parsedList = JSON.parse(savedInds);
                if (Array.isArray(parsedList) && parsedList.length > 0) {
                    for (const indData of parsedList) {
                        await computeAndAttachIndicator(indData.id, indData.meta, indData.params);
                    }
                    return;
                }
            }
        } catch (e) {
            console.error('Error restoring saved indicators:', e);
        }

        await loadDefaultSupertrend();
    }

    async function loadDefaultSupertrend() {
        const supertrendMeta = {
            name: "supertrend",
            display_name: "Supertrend (Pine Script Math)",
            category: "Overlay",
            overlay: true,
            params: [
                {"name": "length", "label": "ATR Length", "type": "int", "default": 5},
                {"name": "multiplier", "label": "Multiplier", "type": "float", "default": 1.5}
            ]
        };
        const params = { length: 5, multiplier: 1.5 };
        await computeAndAttachIndicator("ind_supertrend_default", supertrendMeta, params);
    }

    // =========================================================================
    // 6. EVENT LISTENERS & GENERAL UI LOGIC
    // =========================================================================
    tfButtons.forEach((btn) => {
        btn.addEventListener('click', () => {
            tfButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const tf = btn.dataset.tf;
            loadOHLCData(tf);
        });
    });

    rangeButtons.forEach((btn) => {
        btn.addEventListener('click', () => {
            rangeButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const range = btn.dataset.range;
            const totalBars = currentOHLCData.length;
            if (totalBars === 0) return;

            let barsToShow = 150;
            if (range === '1D') barsToShow = 26;
            else if (range === '5D') barsToShow = 130;
            else if (range === '1M') barsToShow = 800;
            else if (range === '3M') barsToShow = 2400;
            else if (range === '6M') barsToShow = 4800;
            else if (range === 'ALL') barsToShow = totalBars;

            mainChart.timeScale().setVisibleLogicalRange({
                from: Math.max(0, totalBars - barsToShow),
                to: totalBars - 1
            });
        });
    });

    timezoneSelect.addEventListener('change', () => {
        const selectedVal = timezoneSelect.value;
        currentTimezoneOffset = getOffsetHoursFromSelect(selectedVal);
        const selectedText = timezoneSelect.options[timezoneSelect.selectedIndex].text;
        currentTimezoneLabel = selectedText.split(' ')[0];
        saveSettingsToLocalStorage();

        mainChart.applyOptions({
            timeScale: {
                tickMarkFormatter: (time) => formatTickMarkTimezone(time, currentTimezoneOffset),
            },
            localization: {
                timeFormatter: (time) => formatFullDateTimezone(time, currentTimezoneOffset),
            }
        });

        if (subChart) {
            subChart.applyOptions({
                timeScale: {
                    tickMarkFormatter: (time) => formatTickMarkTimezone(time, currentTimezoneOffset),
                },
                localization: {
                    timeFormatter: (time) => formatFullDateTimezone(time, currentTimezoneOffset),
                }
            });
        }
    });

    openIndicatorsBtn.addEventListener('click', () => {
        resetModalState();
        indicatorsModal.classList.remove('hidden');
    });

    closeIndicatorsModal.addEventListener('click', () => {
        resetModalState();
    });

    cancelConfigBtn.addEventListener('click', () => {
        resetModalState();
    });

    applyIndicatorBtn.addEventListener('click', () => {
        applySelectedIndicator();
    });

    closeSubChartBtn.addEventListener('click', () => {
        subChartCanvasWrapper.classList.add('hidden');
    });

    themeToggleBtn.addEventListener('click', () => {
        document.body.classList.toggle('dark-theme');
        const isDark = document.body.classList.contains('dark-theme');
        themeToggleBtn.innerHTML = isDark ? '<i class="fa-solid fa-sun"></i>' : '<i class="fa-solid fa-moon"></i>';
        saveSettingsToLocalStorage();

        const bg = isDark ? '#131722' : '#ffffff';
        const txt = isDark ? '#d1d4dc' : '#131722';
        const grid = isDark ? '#1f232f' : '#f0f3fa';
        const border = isDark ? '#2a2e39' : '#e0e3eb';

        mainChart.applyOptions({
            layout: { background: { color: bg }, textColor: txt },
            grid: { vertLines: { color: grid }, horzLines: { color: grid } },
            rightPriceScale: { borderColor: border },
            timeScale: { borderColor: border }
        });

        if (subChart) {
            subChart.applyOptions({
                layout: { background: { color: bg }, textColor: txt },
                grid: { vertLines: { color: grid }, horzLines: { color: grid } },
                rightPriceScale: { borderColor: border },
                timeScale: { borderColor: border }
            });
        }

        redrawCanvasOverlay();
        updateFloatingBarPosition();
    });

    fullscreenBtn.addEventListener('click', () => {
        if (!document.fullscreenElement) {
            document.documentElement.requestFullscreen();
        } else {
            if (document.exitFullscreen) document.exitFullscreen();
        }
    });

    setInterval(() => {
        const now = new Date();
        const utcSecs = (now.getTime() / 1000) + (now.getTimezoneOffset() * 60);
        const tzSecs = utcSecs + (currentTimezoneOffset * 3600);
        const tzDate = new Date(tzSecs * 1000);

        const hrs = String(tzDate.getUTCHours()).padStart(2, '0');
        const mins = String(tzDate.getUTCMinutes()).padStart(2, '0');
        const secs = String(tzDate.getUTCSeconds()).padStart(2, '0');
        clockDisplay.textContent = `${hrs}:${mins}:${secs} ${currentTimezoneLabel}`;
    }, 1000);

    // =========================================================================
    // INITIALIZATION BOOTSTRAP
    // =========================================================================
    async function bootstrap() {
        restoreSettingsFromLocalStorage();
        initMainChart();
        await loadDatasetInfo();
        await loadOHLCData(currentTimeframe);
        await loadAvailableIndicators();
        await restoreOrLoadDefaultIndicators();
    }

    bootstrap();
});

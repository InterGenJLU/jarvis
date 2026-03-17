/**
 * JARVIS Metrics Dashboard — Frontend
 *
 * Chart.js charts, WebSocket live updates, data explorer with pagination.
 */

(function () {
    'use strict';

    // --- Auth token (from URL query param) ---
    const _urlToken = new URLSearchParams(location.search).get('token') || '';
    function authUrl(url) {
        if (!_urlToken) return url;
        const sep = url.includes('?') ? '&' : '?';
        return url + sep + 'token=' + encodeURIComponent(_urlToken);
    }

    // --- State ---
    let currentHours = 24;
    let explorerOffset = 0;
    const explorerLimit = 50;
    let explorerTotal = 0;

    // Chart instances (for destroy/recreate)
    let chartInteractions = null;
    let chartTokens = null;
    let chartProviders = null;
    let chartSkills = null;

    // WebSocket
    let ws = null;
    let wsRetryTimer = null;

    // --- DOM refs ---
    const timeRange = document.getElementById('time-range');
    const wsBadge = document.getElementById('ws-status');

    // Summary cards
    const valInteractions = document.getElementById('val-interactions');
    const valTokens = document.getElementById('val-tokens');
    const valFallback = document.getElementById('val-fallback');
    const valLatency = document.getElementById('val-latency');
    const valTtft = document.getElementById('val-ttft');
    const valCost = document.getElementById('val-cost');

    // Explorer
    const interactionsBody = document.getElementById('interactions-body');
    const pageInfo = document.getElementById('page-info');
    const btnPrev = document.getElementById('btn-prev');
    const btnNext = document.getElementById('btn-next');
    const btnExport = document.getElementById('btn-export');

    // Filters
    const filterProvider = document.getElementById('filter-provider');
    const filterRoute = document.getElementById('filter-route');
    const filterMethod = document.getElementById('filter-method');
    const filterInput = document.getElementById('filter-input');
    const filterErrors = document.getElementById('filter-errors');
    const filterFallback = document.getElementById('filter-fallback');

    // --- Chart.js defaults ---
    Chart.defaults.color = '#94a3b8';
    Chart.defaults.borderColor = 'rgba(42, 53, 72, 0.6)';
    Chart.defaults.font.family = "'Consolas', 'Fira Code', monospace";
    Chart.defaults.font.size = 11;

    // --- Helpers ---
    function formatNum(n) {
        if (n == null) return '--';
        if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
        if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
        return String(n);
    }

    function formatMs(ms) {
        if (ms == null || ms === 0) return '--';
        if (ms >= 1000) return (ms / 1000).toFixed(1) + 's';
        return Math.round(ms) + 'ms';
    }

    function formatTimestamp(ts) {
        if (!ts) return '--';
        const d = new Date(ts * 1000);
        const hh = String(d.getHours()).padStart(2, '0');
        const mm = String(d.getMinutes()).padStart(2, '0');
        const ss = String(d.getSeconds()).padStart(2, '0');
        const mon = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${mon}/${day} ${hh}:${mm}:${ss}`;
    }

    function formatBucketLabel(ts, bucket) {
        const d = new Date(ts * 1000);
        if (bucket === 'day') {
            return `${d.getMonth() + 1}/${d.getDate()}`;
        }
        // Show hour in 12h format: "5am", "2pm"
        let h = d.getHours();
        const ampm = h >= 12 ? 'pm' : 'am';
        h = h % 12 || 12;
        return `${h}${ampm}`;
    }

    function buildFilterParams() {
        const params = new URLSearchParams();
        if (filterProvider.value) params.set('provider', filterProvider.value);
        if (filterRoute.value) params.set('route_layer', filterRoute.value);
        if (filterMethod.value) params.set('method', filterMethod.value);
        if (filterInput.value) params.set('input_method', filterInput.value);
        if (filterErrors.checked) params.set('error_only', '1');
        if (filterFallback.checked) params.set('fallback_only', '1');
        return params;
    }

    // --- API Fetchers ---
    async function fetchSummary() {
        try {
            const r = await fetch(authUrl(`/api/metrics/summary?hours=${currentHours}`));
            if (!r.ok) return;
            const d = await r.json();
            valInteractions.textContent = formatNum(d.total_interactions);
            valTokens.textContent = formatNum(d.total_tokens);

            const fbText = d.fallback_count > 0
                ? `${d.fallback_count} (${d.fallback_rate}%)`
                : '0';
            valFallback.textContent = fbText;
            valFallback.classList.toggle('highlight', d.fallback_count > 0);

            valLatency.textContent = formatMs(d.avg_latency_ms);
            valTtft.textContent = formatMs(d.avg_ttft_ms);
            valCost.textContent = d.claude_cost_estimate > 0
                ? `$${d.claude_cost_estimate.toFixed(4)}`
                : '$0';
        } catch (e) {
            console.error('fetchSummary:', e);
        }
    }

    async function fetchTimeseries() {
        try {
            const bucket = currentHours > 48 ? 'day' : 'hour';
            const r = await fetch(authUrl(`/api/metrics/timeseries?hours=${currentHours}&bucket=${bucket}`));
            if (!r.ok) return;
            const data = await r.json();

            const labels = data.map(d => formatBucketLabel(d.bucket_start, bucket));
            const interactions = data.map(d => d.interactions);
            const promptTok = data.map(d => d.prompt_tok || 0);
            const completionTok = data.map(d => d.completion_tok || 0);
            const estimatedTok = data.map(d => d.estimated_tok || 0);
            const qwenCounts = data.map(d => d.qwen_count || 0);
            const claudeCounts = data.map(d => d.claude_count || 0);

            // Interactions chart
            if (chartInteractions) chartInteractions.destroy();
            chartInteractions = new Chart(document.getElementById('chart-interactions'), {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Qwen',
                            data: qwenCounts,
                            borderColor: '#34d399',
                            backgroundColor: 'rgba(52, 211, 153, 0.1)',
                            fill: true,
                            tension: 0.3,
                        },
                        {
                            label: 'Claude',
                            data: claudeCounts,
                            borderColor: '#fbbf24',
                            backgroundColor: 'rgba(251, 191, 36, 0.1)',
                            fill: true,
                            tension: 0.3,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: 'top' } },
                    scales: {
                        y: { beginAtZero: true, ticks: { precision: 0 } },
                    },
                },
            });

            // Token usage chart — Input vs Output
            if (chartTokens) chartTokens.destroy();
            chartTokens = new Chart(document.getElementById('chart-tokens'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Input',
                            data: promptTok,
                            backgroundColor: 'rgba(56, 189, 248, 0.6)',
                        },
                        {
                            label: 'Output',
                            data: completionTok,
                            backgroundColor: 'rgba(52, 211, 153, 0.6)',
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: 'top' } },
                    scales: {
                        x: { stacked: true },
                        y: { stacked: true, beginAtZero: true },
                    },
                },
            });
        } catch (e) {
            console.error('fetchTimeseries:', e);
        }
    }

    async function fetchProviderChart() {
        // Web Search Performance — pages fetched and latency
        try {
            const r = await fetch(authUrl(`/api/metrics/search_stats?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();

            if (!data.length) {
                // No search data — show empty state
                if (chartProviders) chartProviders.destroy();
                chartProviders = null;
                const canvas = document.getElementById('chart-providers');
                const ctx = canvas.getContext('2d');
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                ctx.fillStyle = '#64748b';
                ctx.font = '14px system-ui';
                ctx.textAlign = 'center';
                ctx.fillText('No web searches yet', canvas.width / 2, canvas.height / 2);
                return;
            }

            const labels = data.map(d => {
                const dt = new Date(d.timestamp * 1000);
                let h = dt.getHours();
                const ampm = h >= 12 ? 'pm' : 'am';
                h = h % 12 || 12;
                return `${h}:${String(dt.getMinutes()).padStart(2, '0')}${ampm}`;
            });
            const pagesOk = data.map(d => d.search_pages_ok || 0);
            const pagesFailed = data.map(d => (d.search_pages_total || 0) - (d.search_pages_ok || 0));
            const latency = data.map(d => d.search_latency_ms ? d.search_latency_ms / 1000 : 0);

            if (chartProviders) chartProviders.destroy();
            chartProviders = new Chart(document.getElementById('chart-providers'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Pages OK',
                            data: pagesOk,
                            backgroundColor: 'rgba(52, 211, 153, 0.7)',
                            yAxisID: 'y',
                        },
                        {
                            label: 'Pages Failed',
                            data: pagesFailed,
                            backgroundColor: 'rgba(148, 163, 184, 0.5)',
                            yAxisID: 'y',
                        },
                        {
                            label: 'Latency (s)',
                            data: latency,
                            type: 'line',
                            borderColor: '#38bdf8',
                            backgroundColor: 'rgba(56, 189, 248, 0.1)',
                            fill: true,
                            tension: 0.3,
                            yAxisID: 'y1',
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: 'top' } },
                    scales: {
                        y: {
                            type: 'linear',
                            position: 'left',
                            beginAtZero: true,
                            ticks: { precision: 0 },
                            title: { display: true, text: 'Pages' },
                        },
                        y1: {
                            type: 'linear',
                            position: 'right',
                            beginAtZero: true,
                            grid: { drawOnChartArea: false },
                            title: { display: true, text: 'Seconds' },
                        },
                        x: { stacked: true },
                    },
                },
            });
        } catch (e) {
            console.error('fetchProviderChart:', e);
        }
    }

    async function fetchSkillsChart() {
        try {
            // Aggregate tools_called from recent interactions
            const r = await fetch(authUrl(`/api/metrics/interactions?limit=200`));
            if (!r.ok) return;
            const data = await r.json();

            const toolCounts = {};
            for (const row of data.rows) {
                if (row.tools_called) {
                    for (const tool of row.tools_called.split(', ')) {
                        const t = tool.trim();
                        if (t) toolCounts[t] = (toolCounts[t] || 0) + 1;
                    }
                }
            }

            // Sort by count descending, take top 10
            const sorted = Object.entries(toolCounts).sort((a, b) => b[1] - a[1]).slice(0, 10);
            const labels = sorted.map(d => d[0]);
            const values = sorted.map(d => d[1]);

            if (chartSkills) chartSkills.destroy();
            chartSkills = new Chart(document.getElementById('chart-skills'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [{
                        label: 'Interactions',
                        data: values,
                        backgroundColor: 'rgba(56, 189, 248, 0.5)',
                        borderColor: '#38bdf8',
                        borderWidth: 1,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    indexAxis: 'y',
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { beginAtZero: true, ticks: { precision: 0 } },
                    },
                },
            });
        } catch (e) {
            console.error('fetchSkillsChart:', e);
        }
    }

    async function fetchInteractions() {
        try {
            const params = buildFilterParams();
            params.set('offset', explorerOffset);
            params.set('limit', explorerLimit);

            const r = await fetch(authUrl(`/api/metrics/interactions?${params}`));
            if (!r.ok) return;
            const d = await r.json();

            explorerTotal = d.total;
            renderInteractionsTable(d.rows);

            const start = d.total === 0 ? 0 : d.offset + 1;
            const end = Math.min(d.offset + d.limit, d.total);
            pageInfo.textContent = `${start}–${end} of ${d.total}`;
            btnPrev.disabled = d.offset === 0;
            btnNext.disabled = (d.offset + d.limit) >= d.total;
        } catch (e) {
            console.error('fetchInteractions:', e);
        }
    }

    function renderInteractionsTable(rows) {
        interactionsBody.innerHTML = '';
        if (rows.length === 0) {
            const tr = document.createElement('tr');
            tr.innerHTML = '<td colspan="11" style="text-align:center;color:var(--text-dim);padding:20px;">No interactions found</td>';
            interactionsBody.appendChild(tr);
            return;
        }

        // Human-readable method names
        const METHOD_LABELS = {
            'stream': 'Direct',
            'stream_with_tools': 'Tool Select',
            'continue_after_tool_call': 'Tool Synthesis',
            'generate': 'Direct',
            'chat': 'Direct (API)',
        };

        // Clean input method display
        function formatInput(raw) {
            if (!raw) return '--';
            if (raw === 'web:desktop' || raw === 'desktop') return 'Desktop';
            if (raw === 'web:mobile' || raw === 'mobile') return 'Mobile';
            if (raw === 'voice') return 'Voice';
            if (raw === 'web') return 'Web';
            return raw;
        }

        for (const row of rows) {
            const tr = document.createElement('tr');
            const tokens = row.prompt_tokens || row.completion_tokens
                ? `${row.prompt_tokens || 0}/${row.completion_tokens || 0}`
                : row.estimated_tokens ? `~${row.estimated_tokens}` : '--';

            const providerClass = row.provider === 'claude' ? 'provider-claude'
                : row.provider === 'qwen' ? 'provider-qwen' : '';
            const errorClass = row.error ? 'has-error' : '';
            const methodLabel = METHOD_LABELS[row.method] || row.method || '--';

            tr.innerHTML = `
                <td>${formatTimestamp(row.timestamp)}</td>
                <td class="${providerClass}">${row.provider || '--'}</td>
                <td>${methodLabel}</td>
                <td>${row.model ? row.model.substring(0, 20) : '--'}</td>
                <td>${tokens}</td>
                <td>${formatMs(row.latency_ms)}</td>
                <td>${formatMs(row.ttft_ms)}</td>
                <td>${row.route_layer || '--'}</td>
                <td>${row.tools_called || '--'}</td>
                <td>${row.synthesis_category || '--'}</td>
                <td>${formatInput(row.input_method)}</td>
                <td class="${errorClass}">${row.error ? row.error.substring(0, 30) : '--'}</td>
            `;
            interactionsBody.appendChild(tr);
        }
    }

    async function fetchFilters() {
        try {
            const r = await fetch(authUrl('/api/metrics/filters'));
            if (!r.ok) return;
            const d = await r.json();

            populateSelect(filterProvider, d.providers, 'All Providers');
            populateSelect(filterRoute, d.route_layers || d.skills || [], 'All Routes');
            populateSelect(filterMethod, d.methods, 'All Methods');
            populateSelect(filterInput, d.input_methods, 'All Inputs');
        } catch (e) {
            console.error('fetchFilters:', e);
        }
    }

    function populateSelect(select, values, defaultLabel) {
        const current = select.value;
        select.innerHTML = '';
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = defaultLabel;
        select.appendChild(opt);
        for (const v of values) {
            const o = document.createElement('option');
            o.value = v;
            o.textContent = v;
            if (v === current) o.selected = true;
            select.appendChild(o);
        }
    }

    // --- Refresh all ---
    async function refreshAll() {
        await Promise.all([
            fetchSummary(),
            fetchTimeseries(),
            fetchProviderChart(),
            fetchSkillsChart(),
            fetchInteractions(),
            fetchFilters(),
        ]);
    }

    // --- WebSocket ---
    function connectWS() {
        if (ws && ws.readyState <= 1) return;

        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(authUrl(`${proto}//${location.host}/ws/dashboard`));

        ws.onopen = () => {
            wsBadge.classList.remove('disconnected');
            wsBadge.classList.add('connected');
            if (wsRetryTimer) {
                clearTimeout(wsRetryTimer);
                wsRetryTimer = null;
            }
        };

        ws.onmessage = (evt) => {
            try {
                const msg = JSON.parse(evt.data);
                if (msg.type === 'new_metric') {
                    // Live update — refresh summary + charts
                    fetchSummary();
                    // Refresh table if on first page with no filters
                    if (explorerOffset === 0) {
                        fetchInteractions();
                    }
                }
            } catch (e) {
                // ignore
            }
        };

        ws.onclose = () => {
            wsBadge.classList.remove('connected');
            wsBadge.classList.add('disconnected');
            wsRetryTimer = setTimeout(connectWS, 3000);
        };

        ws.onerror = () => {
            ws.close();
        };
    }

    // --- Event listeners ---
    timeRange.addEventListener('change', () => {
        currentHours = parseInt(timeRange.value, 10);
        explorerOffset = 0;
        refreshAll();
    });

    btnPrev.addEventListener('click', () => {
        explorerOffset = Math.max(0, explorerOffset - explorerLimit);
        fetchInteractions();
    });

    btnNext.addEventListener('click', () => {
        if (explorerOffset + explorerLimit < explorerTotal) {
            explorerOffset += explorerLimit;
            fetchInteractions();
        }
    });

    // Filter changes reset pagination
    [filterProvider, filterRoute, filterMethod, filterInput].forEach(el => {
        el.addEventListener('change', () => {
            explorerOffset = 0;
            fetchInteractions();
        });
    });

    [filterErrors, filterFallback].forEach(el => {
        el.addEventListener('change', () => {
            explorerOffset = 0;
            fetchInteractions();
        });
    });

    btnExport.addEventListener('click', () => {
        const params = buildFilterParams();
        window.location.href = `/api/metrics/export?${params}`;
    });

    // --- Init ---
    refreshAll();
    connectWS();

    // Auto-refresh every 60s
    setInterval(refreshAll, 60_000);
})();

/**
 * JARVIS Metrics Dashboard — Frontend
 *
 * Chart.js charts, WebSocket live updates, data explorer with pagination.
 */

(function () {
    'use strict';

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
    const filterSkill = document.getElementById('filter-skill');
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
        return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    }

    function buildFilterParams() {
        const params = new URLSearchParams();
        if (filterProvider.value) params.set('provider', filterProvider.value);
        if (filterSkill.value) params.set('skill', filterSkill.value);
        if (filterMethod.value) params.set('method', filterMethod.value);
        if (filterInput.value) params.set('input_method', filterInput.value);
        if (filterErrors.checked) params.set('error_only', '1');
        if (filterFallback.checked) params.set('fallback_only', '1');
        return params;
    }

    // --- API Fetchers ---
    async function fetchSummary() {
        try {
            const r = await fetch(`/api/metrics/summary?hours=${currentHours}`);
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
            const r = await fetch(`/api/metrics/timeseries?hours=${currentHours}&bucket=${bucket}`);
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

            // Token usage chart
            if (chartTokens) chartTokens.destroy();
            chartTokens = new Chart(document.getElementById('chart-tokens'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Prompt',
                            data: promptTok,
                            backgroundColor: 'rgba(56, 189, 248, 0.6)',
                        },
                        {
                            label: 'Completion',
                            data: completionTok,
                            backgroundColor: 'rgba(52, 211, 153, 0.6)',
                        },
                        {
                            label: 'Estimated',
                            data: estimatedTok,
                            backgroundColor: 'rgba(148, 163, 184, 0.4)',
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

    async function fetchProviderChart(summaryData) {
        // Uses summary data already fetched, or fetch fresh
        try {
            let providers;
            if (summaryData) {
                providers = summaryData;
            } else {
                const r = await fetch(`/api/metrics/summary?hours=${currentHours}`);
                if (!r.ok) return;
                const d = await r.json();
                providers = d.provider_breakdown;
            }

            const labels = Object.keys(providers);
            const values = Object.values(providers);
            const colors = labels.map(l => {
                if (l === 'qwen') return '#34d399';
                if (l === 'claude') return '#fbbf24';
                return '#94a3b8';
            });

            if (chartProviders) chartProviders.destroy();
            chartProviders = new Chart(document.getElementById('chart-providers'), {
                type: 'doughnut',
                data: {
                    labels,
                    datasets: [{
                        data: values,
                        backgroundColor: colors,
                        borderColor: '#0a0e1a',
                        borderWidth: 2,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'bottom' },
                    },
                },
            });
        } catch (e) {
            console.error('fetchProviderChart:', e);
        }
    }

    async function fetchSkillsChart() {
        try {
            const r = await fetch(`/api/metrics/skills?hours=${currentHours}`);
            if (!r.ok) return;
            const data = await r.json();

            // Top 10 skills
            const top = data.slice(0, 10);
            const labels = top.map(d => d.skill || 'Unknown');
            const values = top.map(d => d.interactions);

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

            const r = await fetch(`/api/metrics/interactions?${params}`);
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

        for (const row of rows) {
            const tr = document.createElement('tr');
            const tokens = row.prompt_tokens || row.completion_tokens
                ? `${row.prompt_tokens || 0}/${row.completion_tokens || 0}`
                : row.estimated_tokens ? `~${row.estimated_tokens}` : '--';

            const providerClass = row.provider === 'claude' ? 'provider-claude'
                : row.provider === 'qwen' ? 'provider-qwen' : '';
            const errorClass = row.error ? 'has-error' : '';

            tr.innerHTML = `
                <td>${formatTimestamp(row.timestamp)}</td>
                <td class="${providerClass}">${row.provider || '--'}</td>
                <td>${row.method || '--'}</td>
                <td>${row.model ? row.model.substring(0, 20) : '--'}</td>
                <td>${tokens}</td>
                <td>${formatMs(row.latency_ms)}</td>
                <td>${formatMs(row.ttft_ms)}</td>
                <td>${row.skill || '--'}</td>
                <td>${row.intent || '--'}</td>
                <td>${row.input_method || '--'}</td>
                <td class="${errorClass}">${row.error ? row.error.substring(0, 30) : '--'}</td>
            `;
            interactionsBody.appendChild(tr);
        }
    }

    async function fetchFilters() {
        try {
            const r = await fetch('/api/metrics/filters');
            if (!r.ok) return;
            const d = await r.json();

            populateSelect(filterProvider, d.providers, 'All Providers');
            populateSelect(filterSkill, d.skills, 'All Skills');
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
            fetchSkillsChart(),
            fetchInteractions(),
            fetchFilters(),
        ]);
        // Provider chart uses summary data, fetch separately
        const r = await fetch(`/api/metrics/summary?hours=${currentHours}`);
        if (r.ok) {
            const d = await r.json();
            await fetchProviderChart(d.provider_breakdown);
        }
    }

    // --- WebSocket ---
    function connectWS() {
        if (ws && ws.readyState <= 1) return;

        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${proto}//${location.host}/ws/dashboard`);

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
    [filterProvider, filterSkill, filterMethod, filterInput].forEach(el => {
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

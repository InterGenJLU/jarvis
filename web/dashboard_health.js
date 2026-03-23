/* JARVIS System Health Dashboard */

(function () {
    'use strict';

    // Chart.js global font sizing — readable on 4K
    Chart.defaults.font.size = 14;
    Chart.defaults.plugins.legend.labels.font = { size: 13 };
    Chart.defaults.plugins.title.font = { size: 15 };

    let currentHours = 168;
    const charts = {};

    const token = new URLSearchParams(location.search).get('token');

    function authUrl(url) {
        return token ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : url;
    }

    function fmtTimestamp(ts) {
        const d = new Date(ts * 1000);
        return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
    }

    function fmtGB(mb) {
        if (mb == null || mb === '?') return '?';
        return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb + ' MB';
    }

    // ── Chart builders ──────────────────────────────────────────────

    function lineChart(canvasId, datasets, yTitle) {
        if (charts[canvasId]) charts[canvasId].destroy();

        const hasData = datasets.some(ds => ds.values && ds.values.length > 0);
        if (!hasData) {
            _emptyCanvas(canvasId, 'No data yet — snapshots accumulate every 10 minutes');
            charts[canvasId] = null;
            return;
        }

        const ref = datasets.find(ds => ds.values && ds.values.length > 0);
        const labels = ref.timestamps.map(fmtTimestamp);

        charts[canvasId] = new Chart(document.getElementById(canvasId), {
            type: 'line',
            data: {
                labels,
                datasets: datasets.filter(ds => ds.values && ds.values.length).map(ds => ({
                    label: ds.label,
                    data: ds.values,
                    borderColor: ds.color,
                    backgroundColor: ds.color.replace(')', ', 0.1)').replace('rgb', 'rgba'),
                    fill: ds.fill || false,
                    tension: 0.3,
                    pointRadius: ds.values.length > 100 ? 0 : 2,
                    yAxisID: ds.yAxisID || 'y',
                })),
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } } },
                scales: _buildScales(datasets, yTitle),
            },
        });
    }

    function _buildScales(datasets, yTitle) {
        const hasY1 = datasets.some(ds => ds.yAxisID === 'y1');
        const scales = {
            y: { beginAtZero: true, title: { display: true, text: yTitle } },
            x: { ticks: { maxTicksLimit: 12, maxRotation: 45 } },
        };
        if (hasY1) {
            const y1ds = datasets.find(ds => ds.yAxisID === 'y1');
            scales.y1 = {
                beginAtZero: true, position: 'right',
                grid: { drawOnChartArea: false },
                title: { display: true, text: y1ds.y1Title || '' },
            };
        }
        return scales;
    }

    function _emptyCanvas(canvasId, msg) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#64748b';
        ctx.font = '14px system-ui';
        ctx.textAlign = 'center';
        ctx.fillText(msg, canvas.width / 2, canvas.height / 2);
    }

    // ── Data extraction helpers ─────────────────────────────────────

    function trend(trendObj) {
        if (!trendObj || !trendObj.data || !trendObj.data.length) {
            return { values: [], timestamps: [] };
        }
        return {
            values: trendObj.data.map(d => d.value),
            timestamps: trendObj.data.map(d => d.timestamp),
        };
    }

    function latest(trendObj) {
        if (trendObj && trendObj.stats && trendObj.stats.latest != null) {
            return trendObj.stats.latest;
        }
        return null;
    }

    // ── Fetch and render ────────────────────────────────────────────

    async function fetchHealthData() {
        const metricsParam = [
            'cpu.temp', 'cpu.load',
            'gpu0.temp', 'gpu0.vram_used_mb', 'gpu0.vram_total_mb', 'gpu0.util',
            'gpu1.temp', 'gpu1.vram_used_mb', 'gpu1.vram_total_mb', 'gpu1.util',
            'ram.percent', 'ram.used', 'ram.total', 'ram.proc_rss_mb',
            'network.ping_ms',
            'llama-server.latency_ms',
        ].join(',');

        try {
            const r = await fetch(authUrl(`/api/events/health?hours=${currentHours}&metrics=${metricsParam}`));
            if (!r.ok) return;
            const data = await r.json();
            const t = data.trends || {};

            // ── Summary cards ───────────────────────────────────

            _setCard('val-cpu-temp', latest(t['cpu.temp']), '°C');
            _setCard('val-gpu0-temp', latest(t['gpu0.temp']), '°C');
            _setCard('val-gpu1-temp', latest(t['gpu1.temp']), '°C');
            // RAM card — used/total like VRAM cards
            _setVramCard('val-ram', t['ram.used'], t['ram.total']);
            _setCard('val-proc', latest(t['ram.proc_rss_mb']), ' MB');
            _setCard('val-ping', latest(t['network.ping_ms']), ' ms');

            // VRAM cards — used/total
            _setVramCard('val-gpu0-vram', t['gpu0.vram_used_mb'], t['gpu0.vram_total_mb']);
            _setVramCard('val-gpu1-vram', t['gpu1.vram_used_mb'], t['gpu1.vram_total_mb']);

            // ── Temperature chart ───────────────────────────────

            const cpuT = trend(t['cpu.temp']);
            const gpu0T = trend(t['gpu0.temp']);
            const gpu1T = trend(t['gpu1.temp']);

            lineChart('chart-temps', [
                { ...cpuT, label: 'CPU (Ryzen 9 5900X)', color: 'rgb(251, 191, 36)' },
                { ...gpu0T, label: 'RX 7600', color: 'rgb(148, 163, 184)' },
                { ...gpu1T, label: 'RX 7900 XT', color: 'rgb(248, 113, 113)' },
            ], '°C');

            // ── CPU Load ────────────────────────────────────────

            const cpuLoad = trend(t['cpu.load']);
            lineChart('chart-cpu-load', [
                { ...cpuLoad, label: 'CPU Load %', color: 'rgb(251, 191, 36)', fill: true },
            ], '%');

            // ── GPU0 VRAM ───────────────────────────────────────

            const gpu0Vram = trend(t['gpu0.vram_used_mb']);
            lineChart('chart-gpu0-vram', [
                { ...gpu0Vram, label: 'VRAM Used (MB)', color: 'rgb(148, 163, 184)', fill: true },
            ], 'MB');

            // ── GPU1 VRAM ───────────────────────────────────────

            const gpu1Vram = trend(t['gpu1.vram_used_mb']);
            lineChart('chart-gpu1-vram', [
                { ...gpu1Vram, label: 'VRAM Used (MB)', color: 'rgb(248, 113, 113)', fill: true },
            ], 'MB');

            // ── System RAM ──────────────────────────────────────

            const ramUsed = trend(t['ram.used']);
            lineChart('chart-ram', [
                { ...ramUsed, label: 'System RAM (GB)', color: 'rgb(52, 211, 153)', fill: true },
            ], 'GB');

            // ── JARVIS Process ──────────────────────────────────

            const procMem = trend(t['ram.proc_rss_mb']);
            lineChart('chart-proc', [
                { ...procMem, label: 'JARVIS RSS (MB)', color: 'rgb(167, 139, 250)', fill: true },
            ], 'MB');

            // ── Network Ping ────────────────────────────────────

            const ping = trend(t['network.ping_ms']);
            lineChart('chart-ping', [
                { ...ping, label: 'Ping (ms)', color: 'rgb(56, 189, 248)', fill: true },
            ], 'ms');

            // ── LLM Health Check ────────────────────────────────

            const llmLat = trend(t['llama-server.latency_ms']);
            lineChart('chart-llm', [
                { ...llmLat, label: 'Health Check Latency (ms)', color: 'rgb(52, 211, 153)', fill: true },
            ], 'ms');

        } catch (e) { console.error('fetchHealthData:', e); }
    }

    function _setCard(id, value, suffix) {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = value != null ? value + suffix : '--';
    }

    function _setVramCard(id, usedTrend, totalTrend) {
        const el = document.getElementById(id);
        if (!el) return;
        const used = latest(usedTrend);
        const total = latest(totalTrend);
        if (used != null) {
            el.textContent = `${fmtGB(used)} / ${fmtGB(total)}`;
        } else {
            el.textContent = '--';
        }
    }

    // ── Init ────────────────────────────────────────────────────────

    document.getElementById('time-range').addEventListener('change', function () {
        currentHours = parseInt(this.value);
        fetchHealthData();
    });

    fetchHealthData();
    setInterval(fetchHealthData, 60000);
})();

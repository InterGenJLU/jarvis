/* JARVIS System Health Dashboard */

(function () {
    'use strict';

    let currentHours = 168;
    let chartTemps = null;
    let chartVram = null;
    let chartRam = null;
    let chartPing = null;

    const token = new URLSearchParams(location.search).get('token');

    function authUrl(url) {
        return token ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : url;
    }

    function fmtTimestamp(ts) {
        const d = new Date(ts * 1000);
        return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
    }

    function buildLineChart(canvasId, datasets, yTitle, existingChart) {
        if (existingChart) existingChart.destroy();

        // Check if any dataset has data
        const hasData = datasets.some(ds => ds.data && ds.data.length > 0);
        if (!hasData) {
            const canvas = document.getElementById(canvasId);
            const ctx = canvas.getContext('2d');
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.fillStyle = '#64748b';
            ctx.font = '14px system-ui';
            ctx.textAlign = 'center';
            ctx.fillText('No data yet — snapshots start accumulating after 10 minutes', canvas.width / 2, canvas.height / 2);
            return null;
        }

        // Use timestamps from the first dataset with data
        const refDs = datasets.find(ds => ds.data && ds.data.length > 0);
        const labels = refDs.timestamps.map(fmtTimestamp);

        return new Chart(document.getElementById(canvasId), {
            type: 'line',
            data: {
                labels,
                datasets: datasets.map(ds => ({
                    label: ds.label,
                    data: ds.data,
                    borderColor: ds.color,
                    backgroundColor: ds.color.replace(')', ', 0.1)').replace('rgb', 'rgba'),
                    fill: ds.fill || false,
                    tension: 0.3,
                    pointRadius: ds.data.length > 100 ? 0 : 2,
                })),
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: 'top' } },
                scales: {
                    y: { beginAtZero: ds => ds.beginAtZero !== false,
                         title: { display: true, text: yTitle } },
                    x: { ticks: { maxTicksLimit: 12, maxRotation: 45 } },
                },
            },
        });
    }

    async function fetchHealthData() {
        const metricsParam = [
            'cpu.temp', 'cpu.load',
            'gpu0.temp', 'gpu1.temp',
            'gpu1.vram_used_mb', 'gpu1.vram_total_mb',
            'ram.percent', 'ram.used', 'ram.proc_rss_mb',
            'network.ping_ms',
        ].join(',');

        try {
            const r = await fetch(authUrl(`/api/events/health?hours=${currentHours}&metrics=${metricsParam}`));
            if (!r.ok) return;
            const data = await r.json();
            const trends = data.trends || {};

            // Summary cards — use latest values
            _updateCard('val-cpu-temp', trends['cpu.temp'], '°C');
            _updateCard('val-gpu-temp', trends['gpu1.temp'], '°C');
            _updateCard('val-ram', trends['ram.used'], ' GB');
            _updateCard('val-proc', trends['ram.proc_rss_mb'], ' MB');
            _updateCard('val-ping', trends['network.ping_ms'], 'ms');

            // VRAM card — show used/total
            if (trends['gpu1.vram_used_mb'] && trends['gpu1.vram_used_mb'].stats) {
                const used = trends['gpu1.vram_used_mb'].stats.latest;
                const total = trends['gpu1.vram_total_mb'] ? trends['gpu1.vram_total_mb'].stats.latest : '?';
                document.getElementById('val-vram').textContent = `${_fmtGB(used)}/${_fmtGB(total)}`;
            }

            // Temperature chart
            chartTemps = buildLineChart('chart-temps', [
                _trendLine(trends['cpu.temp'], 'CPU', 'rgb(251, 191, 36)'),
                _trendLine(trends['gpu0.temp'], 'GPU0 (Display)', 'rgb(148, 163, 184)'),
                _trendLine(trends['gpu1.temp'], 'GPU1 (Compute)', 'rgb(248, 113, 113)'),
            ], '°C', chartTemps);

            // VRAM chart
            chartVram = buildLineChart('chart-vram', [
                _trendLine(trends['gpu1.vram_used_mb'], 'VRAM Used (MB)', 'rgb(56, 189, 248)', true),
            ], 'MB', chartVram);

            // RAM chart
            chartRam = buildLineChart('chart-ram', [
                _trendLine(trends['ram.used'], 'System RAM (GB)', 'rgb(52, 211, 153)', true),
                _trendLine(trends['ram.proc_rss_mb'], 'JARVIS Process (MB)', 'rgb(167, 139, 250)'),
            ], 'GB / MB', chartRam);

            // Ping chart
            chartPing = buildLineChart('chart-ping', [
                _trendLine(trends['network.ping_ms'], 'Ping (ms)', 'rgb(56, 189, 248)', true),
            ], 'ms', chartPing);

        } catch (e) { console.error('fetchHealthData:', e); }
    }

    function _trendLine(trendObj, label, color, fill) {
        if (!trendObj || !trendObj.data || !trendObj.data.length) {
            return { label, data: [], timestamps: [], color, fill };
        }
        return {
            label,
            data: trendObj.data.map(d => d.value),
            timestamps: trendObj.data.map(d => d.timestamp),
            color,
            fill: fill || false,
        };
    }

    function _updateCard(id, trendObj, suffix) {
        const el = document.getElementById(id);
        if (trendObj && trendObj.stats && trendObj.stats.latest != null) {
            el.textContent = trendObj.stats.latest + suffix;
        } else {
            el.textContent = '--';
        }
    }

    function _fmtGB(mb) {
        if (mb == null || mb === '?') return '?';
        return mb >= 1024 ? (mb / 1024).toFixed(1) + 'GB' : mb + 'MB';
    }

    // ── Init ────────────────────────────────────────────────────────

    document.getElementById('time-range').addEventListener('change', function () {
        currentHours = parseInt(this.value);
        fetchHealthData();
    });

    fetchHealthData();
    setInterval(fetchHealthData, 60000);  // Refresh every 60s
})();

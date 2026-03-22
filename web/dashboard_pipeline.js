/* JARVIS Pipeline Performance Dashboard */

(function () {
    'use strict';

    let currentHours = 24;
    let chartTTS = null;
    let chartSpeaker = null;
    let chartRouting = null;
    let chartSearch = null;

    const token = new URLSearchParams(location.search).get('token');

    function authUrl(url) {
        return token ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : url;
    }

    function fmtTime(ts) {
        const d = new Date(ts * 1000);
        let h = d.getHours();
        const ampm = h >= 12 ? 'pm' : 'am';
        h = h % 12 || 12;
        return `${h}:${String(d.getMinutes()).padStart(2, '0')}${ampm}`;
    }

    function fmtDate(ts) {
        const d = new Date(ts * 1000);
        return `${d.getMonth() + 1}/${d.getDate()} ${fmtTime(ts)}`;
    }

    // ── TTS ──────────────────────────────────────────────────────────

    async function fetchTTS() {
        try {
            const r = await fetch(authUrl(`/api/events/tts?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();

            document.getElementById('val-tts-count').textContent = data.total_syntheses;
            document.getElementById('val-tts-cache').textContent = data.cache_hit_rate + '%';
            document.getElementById('val-tts-gen').textContent = data.avg_generation_s
                ? data.avg_generation_s.toFixed(2) + 's' : '--';

            const pts = data.data_points || [];
            if (!pts.length) {
                _emptyChart('chart-tts', 'No TTS data yet');
                return;
            }

            const labels = pts.map(d => fmtTime(d.timestamp));
            const genTimes = pts.map(d => d.generation_time_s || 0);
            const rtfs = pts.map(d => d.rtf || 0);

            if (chartTTS) chartTTS.destroy();
            chartTTS = new Chart(document.getElementById('chart-tts'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Generation Time (s)',
                            data: genTimes,
                            backgroundColor: 'rgba(52, 211, 153, 0.7)',
                            yAxisID: 'y',
                        },
                        {
                            label: 'RTF (x realtime)',
                            data: rtfs,
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
                        y: { beginAtZero: true, title: { display: true, text: 'Seconds' } },
                        y1: { position: 'right', beginAtZero: true, grid: { drawOnChartArea: false },
                              title: { display: true, text: 'RTF' } },
                    },
                },
            });
        } catch (e) { console.error('fetchTTS:', e); }
    }

    // ── Speaker ID ──────────────────────────────────────────────────

    async function fetchSpeakerID() {
        try {
            const r = await fetch(authUrl(`/api/events/speaker_id?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();

            document.getElementById('val-spk-match').textContent =
                data.total > 0 ? data.match_rate + '%' : '--';

            const scores = data.scores || [];
            if (!scores.length) {
                _emptyChart('chart-speaker', 'No speaker ID data yet');
                return;
            }

            const labels = scores.map(d => fmtTime(d.timestamp));
            const vals = scores.map(d => d.best_score || 0);
            const thresholds = scores.map(d => d.threshold || 0.7);
            const colors = scores.map(d => d.matched
                ? 'rgba(52, 211, 153, 0.8)' : 'rgba(248, 113, 113, 0.8)');

            if (chartSpeaker) chartSpeaker.destroy();
            chartSpeaker = new Chart(document.getElementById('chart-speaker'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Confidence Score',
                            data: vals,
                            backgroundColor: colors,
                        },
                        {
                            label: 'Threshold',
                            data: thresholds,
                            type: 'line',
                            borderColor: '#fbbf24',
                            borderDash: [5, 5],
                            pointRadius: 0,
                            fill: false,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: 'top' } },
                    scales: {
                        y: { beginAtZero: true, max: 1.0, title: { display: true, text: 'Score' } },
                    },
                },
            });
        } catch (e) { console.error('fetchSpeakerID:', e); }
    }

    // ── Routing ─────────────────────────────────────────────────────

    async function fetchRouting() {
        try {
            const r = await fetch(authUrl(`/api/events/routing?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();

            document.getElementById('val-route-handled').textContent =
                data.total > 0 ? `${data.handled}/${data.total}` : '--';

            const intents = data.intents || {};
            const labels = Object.keys(intents);
            const values = Object.values(intents);

            if (!labels.length) {
                _emptyChart('chart-routing', 'No routing data yet');
                return;
            }

            // Color palette
            const palette = [
                '#38bdf8', '#34d399', '#fbbf24', '#f87171', '#a78bfa',
                '#fb923c', '#2dd4bf', '#e879f9', '#818cf8', '#94a3b8',
            ];

            if (chartRouting) chartRouting.destroy();
            chartRouting = new Chart(document.getElementById('chart-routing'), {
                type: 'doughnut',
                data: {
                    labels: labels.map(l => l.length > 25 ? l.slice(0, 25) + '...' : l),
                    datasets: [{
                        data: values,
                        backgroundColor: labels.map((_, i) => palette[i % palette.length]),
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'right', labels: { boxWidth: 12, font: { size: 11 } } },
                    },
                },
            });
        } catch (e) { console.error('fetchRouting:', e); }
    }

    // ── Web Search ──────────────────────────────────────────────────

    async function fetchSearch() {
        try {
            const r = await fetch(authUrl(`/api/metrics/search_stats?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();

            if (!data.length) {
                _emptyChart('chart-search', 'No web searches in this window');
                return;
            }

            const labels = data.map(d => fmtTime(d.timestamp));
            const pagesOk = data.map(d => d.search_pages_ok || 0);
            const pagesFailed = data.map(d => (d.search_pages_total || 0) - (d.search_pages_ok || 0));
            const latency = data.map(d => d.search_latency_ms ? d.search_latency_ms / 1000 : 0);

            if (chartSearch) chartSearch.destroy();
            chartSearch = new Chart(document.getElementById('chart-search'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        { label: 'Pages OK', data: pagesOk, backgroundColor: 'rgba(52, 211, 153, 0.7)', yAxisID: 'y' },
                        { label: 'Pages Failed', data: pagesFailed, backgroundColor: 'rgba(148, 163, 184, 0.5)', yAxisID: 'y' },
                        { label: 'Latency (s)', data: latency, type: 'line', borderColor: '#38bdf8',
                          backgroundColor: 'rgba(56, 189, 248, 0.1)', fill: true, tension: 0.3, yAxisID: 'y1' },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: 'top' } },
                    scales: {
                        y: { beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: 'Pages' } },
                        y1: { position: 'right', beginAtZero: true, grid: { drawOnChartArea: false },
                              title: { display: true, text: 'Seconds' } },
                        x: { stacked: true },
                    },
                },
            });
        } catch (e) { console.error('fetchSearch:', e); }
    }

    // ── STT (summary card only) ─────────────────────────────────────

    async function fetchSTT() {
        try {
            const r = await fetch(authUrl(`/api/events/stt?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();
            document.getElementById('val-stt-rate').textContent =
                data.total > 0 ? data.success_rate + '%' : '--';
        } catch (e) { console.error('fetchSTT:', e); }
    }

    // ── Watchdog ────────────────────────────────────────────────────

    async function fetchWatchdog() {
        try {
            const r = await fetch(authUrl(`/api/events/watchdog?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();

            const tbody = document.getElementById('watchdog-body');
            tbody.innerHTML = '';

            if (!data.events || !data.events.length) {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-dim);">No watchdog interventions</td></tr>';
                return;
            }

            for (const e of data.events) {
                const tr = document.createElement('tr');
                const severityClass = e.severity === 'error' ? 'color:var(--error)'
                    : e.severity === 'warn' ? 'color:var(--warning)' : '';
                tr.innerHTML = `
                    <td>${fmtDate(e.timestamp)}</td>
                    <td>${e.event.replace('watchdog_', '')}</td>
                    <td>${e.message || ''}</td>
                    <td style="${severityClass}">${e.severity}</td>
                `;
                tbody.appendChild(tr);
            }
        } catch (e) { console.error('fetchWatchdog:', e); }
    }

    // ── Helpers ──────────────────────────────────────────────────────

    function _emptyChart(canvasId, message) {
        const canvas = document.getElementById(canvasId);
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#64748b';
        ctx.font = '14px system-ui';
        ctx.textAlign = 'center';
        ctx.fillText(message, canvas.width / 2, canvas.height / 2);
    }

    function refreshAll() {
        fetchTTS();
        fetchSpeakerID();
        fetchRouting();
        fetchSearch();
        fetchSTT();
        fetchWatchdog();
    }

    // ── Init ────────────────────────────────────────────────────────

    document.getElementById('time-range').addEventListener('change', function () {
        currentHours = parseInt(this.value);
        refreshAll();
    });

    refreshAll();
    setInterval(refreshAll, 30000);  // Refresh every 30s
})();

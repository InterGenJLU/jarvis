/* JARVIS Pipeline Performance Dashboard */

(function () {
    'use strict';

    let currentHours = 24;
    const charts = {};

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

    function emptyChart(id, msg) {
        if (charts[id]) { charts[id].destroy(); charts[id] = null; }
        const canvas = document.getElementById(id);
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#64748b';
        ctx.font = '14px system-ui';
        ctx.textAlign = 'center';
        ctx.fillText(msg, canvas.width / 2, canvas.height / 2);
    }

    // ── TTS ──────────────────────────────────────────────────────────

    async function fetchTTS() {
        try {
            const r = await fetch(authUrl(`/api/events/tts?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();

            document.getElementById('val-tts-count').textContent = data.total_syntheses || 0;
            document.getElementById('val-tts-cache').textContent =
                (data.total_syntheses + data.cache_hits) > 0
                    ? `${data.cache_hits} (${data.cache_hit_rate}%)`
                    : '--';
            document.getElementById('val-tts-gen').textContent =
                data.avg_generation_s ? data.avg_generation_s.toFixed(2) + 's' : '--';

            const pts = data.data_points || [];
            if (!pts.length) { emptyChart('chart-tts', 'No TTS data yet'); return; }

            const labels = pts.map(d => fmtTime(d.timestamp));
            if (charts['chart-tts']) charts['chart-tts'].destroy();
            charts['chart-tts'] = new Chart(document.getElementById('chart-tts'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Generation Time (s)',
                            data: pts.map(d => d.generation_time_s || 0),
                            backgroundColor: 'rgba(52, 211, 153, 0.7)',
                            yAxisID: 'y',
                        },
                        {
                            label: 'Realtime Factor (x)',
                            data: pts.map(d => d.rtf || 0),
                            type: 'line',
                            borderColor: '#38bdf8',
                            backgroundColor: 'rgba(56, 189, 248, 0.1)',
                            fill: true, tension: 0.3,
                            yAxisID: 'y1',
                        },
                    ],
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { position: 'top' } },
                    scales: {
                        y: { beginAtZero: true, title: { display: true, text: 'Seconds' } },
                        y1: { position: 'right', beginAtZero: true,
                              grid: { drawOnChartArea: false },
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
                data.total > 0 ? `${data.match_rate}% (${data.matched}/${data.total})` : '--';

            const scores = data.scores || [];
            if (!scores.length) { emptyChart('chart-speaker', 'No speaker ID data yet'); return; }

            const labels = scores.map(d => fmtTime(d.timestamp));
            const colors = scores.map(d => d.matched
                ? 'rgba(52, 211, 153, 0.8)' : 'rgba(248, 113, 113, 0.8)');

            if (charts['chart-speaker']) charts['chart-speaker'].destroy();
            charts['chart-speaker'] = new Chart(document.getElementById('chart-speaker'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        { label: 'Confidence Score', data: scores.map(d => d.best_score || 0),
                          backgroundColor: colors },
                        { label: 'Threshold', data: scores.map(d => d.threshold || 0.7),
                          type: 'line', borderColor: '#fbbf24', borderDash: [5, 5],
                          pointRadius: 0, fill: false },
                    ],
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { position: 'top' } },
                    scales: { y: { beginAtZero: true, max: 1.0,
                              title: { display: true, text: 'Score' } } },
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
                data.total > 0 ? `${data.handled} / ${data.total}` : '--';
            document.getElementById('val-route-latency').textContent =
                data.avg_latency_ms > 0 ? `${data.avg_latency_ms}ms` : '--';

            // Distribution doughnut
            const intents = data.intents || {};
            const iLabels = Object.keys(intents);
            const iValues = Object.values(intents);

            if (!iLabels.length) {
                emptyChart('chart-routing', 'No routing data yet');
            } else {
                const palette = [
                    '#38bdf8', '#34d399', '#fbbf24', '#f87171', '#a78bfa',
                    '#fb923c', '#2dd4bf', '#e879f9', '#818cf8', '#94a3b8',
                ];
                if (charts['chart-routing']) charts['chart-routing'].destroy();
                charts['chart-routing'] = new Chart(document.getElementById('chart-routing'), {
                    type: 'doughnut',
                    data: {
                        labels: iLabels.map(l => l.length > 30 ? l.slice(0, 30) + '...' : l),
                        datasets: [{ data: iValues,
                            backgroundColor: iLabels.map((_, i) => palette[i % palette.length]) }],
                    },
                    options: {
                        responsive: true, maintainAspectRatio: false,
                        plugins: { legend: { position: 'right',
                            labels: { boxWidth: 12, font: { size: 11 } } } },
                    },
                });
            }

            // Routing latency chart
            const pts = data.data_points || [];
            if (!pts.length) {
                emptyChart('chart-route-latency', 'No routing latency data yet');
            } else {
                const latLabels = pts.map(d => fmtTime(d.timestamp));
                const latValues = pts.map(d => d.latency_ms || 0);
                const latColors = pts.map(d =>
                    d.status === 'handled' ? 'rgba(52, 211, 153, 0.7)' : 'rgba(56, 189, 248, 0.7)');

                if (charts['chart-route-latency']) charts['chart-route-latency'].destroy();
                charts['chart-route-latency'] = new Chart(document.getElementById('chart-route-latency'), {
                    type: 'bar',
                    data: {
                        labels: latLabels,
                        datasets: [{
                            label: 'Routing Latency (ms)',
                            data: latValues,
                            backgroundColor: latColors,
                        }],
                    },
                    options: {
                        responsive: true, maintainAspectRatio: false,
                        plugins: {
                            legend: { position: 'top' },
                            tooltip: {
                                callbacks: {
                                    afterLabel: function(ctx) {
                                        const pt = pts[ctx.dataIndex];
                                        return pt.intent || '';
                                    }
                                }
                            }
                        },
                        scales: { y: { beginAtZero: true,
                                  title: { display: true, text: 'ms' } } },
                    },
                });
            }

        } catch (e) { console.error('fetchRouting:', e); }
    }

    // ── Routing Latency Chart ───────────────────────────────────────
    // Populated inside fetchRouting() since we need the same data source

    // ── STT ─────────────────────────────────────────────────────────

    async function fetchSTT() {
        try {
            const r = await fetch(authUrl(`/api/events/stt?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();

            document.getElementById('val-stt-count').textContent = data.total || 0;
            document.getElementById('val-stt-rate').textContent =
                data.total > 0 ? `${data.success_rate}%` : '--';

            const pts = data.data_points || [];
            if (!pts.length) { emptyChart('chart-stt', 'No STT data yet'); return; }

            const labels = pts.map(d => fmtTime(d.timestamp));
            const durations = pts.map(d => d.audio_duration_s || 0);
            const colors = pts.map(d =>
                d.status === 'success' ? 'rgba(52, 211, 153, 0.7)'
                : d.status === 'empty' ? 'rgba(251, 191, 36, 0.7)'
                : 'rgba(248, 113, 113, 0.7)');

            if (charts['chart-stt']) charts['chart-stt'].destroy();
            charts['chart-stt'] = new Chart(document.getElementById('chart-stt'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [{
                        label: 'Audio Duration (s)',
                        data: durations,
                        backgroundColor: colors,
                    }],
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'top' },
                        tooltip: {
                            callbacks: {
                                afterLabel: function(ctx) {
                                    const pt = pts[ctx.dataIndex];
                                    return pt.text_preview ? `"${pt.text_preview}"` : '';
                                }
                            }
                        }
                    },
                    scales: { y: { beginAtZero: true,
                              title: { display: true, text: 'Seconds' } } },
                },
            });
        } catch (e) { console.error('fetchSTT:', e); }
    }

    // ── Web Search ──────────────────────────────────────────────────

    async function fetchSearch() {
        try {
            const r = await fetch(authUrl(`/api/metrics/search_stats?hours=${currentHours}`));
            if (!r.ok) return;
            const data = await r.json();

            if (!data.length) { emptyChart('chart-search', 'No web searches in this window'); return; }

            const labels = data.map(d => fmtTime(d.timestamp));
            if (charts['chart-search']) charts['chart-search'].destroy();
            charts['chart-search'] = new Chart(document.getElementById('chart-search'), {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        { label: 'Pages OK', data: data.map(d => d.search_pages_ok || 0),
                          backgroundColor: 'rgba(52, 211, 153, 0.7)', yAxisID: 'y' },
                        { label: 'Pages Failed',
                          data: data.map(d => (d.search_pages_total || 0) - (d.search_pages_ok || 0)),
                          backgroundColor: 'rgba(148, 163, 184, 0.5)', yAxisID: 'y' },
                        { label: 'Latency (s)',
                          data: data.map(d => d.search_latency_ms ? d.search_latency_ms / 1000 : 0),
                          type: 'line', borderColor: '#38bdf8',
                          backgroundColor: 'rgba(56, 189, 248, 0.1)',
                          fill: true, tension: 0.3, yAxisID: 'y1' },
                    ],
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { position: 'top' } },
                    scales: {
                        y: { beginAtZero: true, ticks: { precision: 0 },
                             title: { display: true, text: 'Pages' } },
                        y1: { position: 'right', beginAtZero: true,
                              grid: { drawOnChartArea: false },
                              title: { display: true, text: 'Seconds' } },
                        x: { stacked: true },
                    },
                },
            });
        } catch (e) { console.error('fetchSearch:', e); }
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
                tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-dim);padding:24px;">No watchdog interventions — all clear</td></tr>';
                return;
            }

            for (const e of data.events) {
                const tr = document.createElement('tr');
                const sColor = e.severity === 'error' ? 'color:var(--error)'
                    : e.severity === 'warn' ? 'color:var(--warning)' : '';
                tr.innerHTML = `
                    <td>${fmtDate(e.timestamp)}</td>
                    <td>${e.event.replace('watchdog_', '').replace(/_/g, ' ')}</td>
                    <td>${e.message || ''}</td>
                    <td style="${sColor}">${e.severity}</td>
                `;
                tbody.appendChild(tr);
            }
        } catch (e) { console.error('fetchWatchdog:', e); }
    }

    // ── Refresh all ─────────────────────────────────────────────────

    function refreshAll() {
        fetchTTS();
        fetchSpeakerID();
        fetchRouting();
        fetchSTT();
        fetchSearch();
        fetchWatchdog();
    }

    // ── Init ────────────────────────────────────────────────────────

    document.getElementById('time-range').addEventListener('change', function () {
        currentHours = parseInt(this.value);
        refreshAll();
    });

    refreshAll();
    setInterval(refreshAll, 30000);
})();

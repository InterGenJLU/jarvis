/* J.A.R.V.I.S. Memory & System Health Dashboard */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
    userId: 'christopher',
    facts: { offset: 0, limit: 50, total: 0, category: '', sort: 'last_referenced' },
    ilog: { offset: 0, limit: 50, total: 0, type: '', days: 30 },
    charts: {},
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fmtBytes(b) {
    if (b === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(b) / Math.log(k));
    return parseFloat((b / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function fmtTs(unix) {
    if (!unix) return '—';
    const d = new Date(unix * 1000);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fmtPct(v) {
    return typeof v === 'number' ? v.toFixed(1) + '%' : '--';
}

function statusBadge(status) {
    const cls = status === 'ok' ? 'status-ok' : status === 'warning' ? 'status-warn' : status === 'missing' ? 'status-missing' : 'status-err';
    return `<span class="status-badge ${cls}">${status}</span>`;
}

function typeTag(t) {
    const classes = { research: 'tag-research', tool_call: 'tag-tool', document: 'tag-doc', skill: 'tag-skill' };
    return `<span class="type-tag ${classes[t] || ''}">${t}</span>`;
}

async function apiFetch(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
const TYPE_COLORS = {
    research: '#38bdf8',
    tool_call: '#34d399',
    document: '#a78bfa',
    skill: '#fbbf24',
    other: '#64748b',
};

const CATEGORY_PALETTE = [
    '#38bdf8', '#34d399', '#a78bfa', '#fbbf24', '#f87171', '#fb923c', '#e879f9', '#818cf8'
];

function destroyChart(id) {
    if (state.charts[id]) { state.charts[id].destroy(); delete state.charts[id]; }
}

function renderFactsCategoryChart(byCat) {
    destroyChart('facts-cat');
    const labels = Object.keys(byCat);
    const data = Object.values(byCat);
    if (!labels.length) return;
    const ctx = document.getElementById('chart-facts-cat').getContext('2d');
    state.charts['facts-cat'] = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{ data, backgroundColor: CATEGORY_PALETTE.slice(0, labels.length), borderWidth: 0 }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: { position: 'right', labels: { color: '#94a3b8', font: { size: 11 } } }
            }
        }
    });
}

function renderInteractionTypesChart(byType) {
    destroyChart('itype');
    const labels = Object.keys(byType);
    const data = Object.values(byType);
    if (!labels.length) return;
    const ctx = document.getElementById('chart-interaction-types').getContext('2d');
    state.charts['itype'] = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{
                data,
                backgroundColor: labels.map(l => TYPE_COLORS[l] || TYPE_COLORS.other),
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: { position: 'right', labels: { color: '#94a3b8', font: { size: 11 } } }
            }
        }
    });
}

function renderContextGauge(usagePct) {
    destroyChart('ctx-gauge');
    const used = Math.min(usagePct, 100);
    const free = 100 - used;
    const ctx = document.getElementById('chart-context-gauge').getContext('2d');
    state.charts['ctx-gauge'] = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['Used', 'Free'],
            datasets: [{
                data: [used, free],
                backgroundColor: [used > 80 ? '#f87171' : used > 60 ? '#fbbf24' : '#34d399', '#1a2236'],
                borderWidth: 0,
            }]
        },
        options: {
            responsive: true,
            circumference: 180,
            rotation: -90,
            plugins: {
                legend: { display: false },
                tooltip: { callbacks: { label: (ctx) => ctx.label + ': ' + ctx.raw.toFixed(1) + '%' } }
            }
        }
    });
}

function renderTimeseriesChart(series) {
    destroyChart('itime');
    if (!series.length) return;
    const labels = series.map(d => d.date);
    const ctx = document.getElementById('chart-interactions-time').getContext('2d');
    const types = ['research', 'tool_call', 'document', 'skill'];
    const datasets = types.map(t => ({
        label: t,
        data: series.map(d => d[t] || 0),
        borderColor: TYPE_COLORS[t],
        backgroundColor: TYPE_COLORS[t] + '33',
        fill: true,
        tension: 0.3,
        borderWidth: 2,
        pointRadius: 2,
    }));
    state.charts['itime'] = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            scales: {
                x: { ticks: { color: '#64748b', maxTicksLimit: 8, font: { size: 10 } }, grid: { color: '#1a2236' } },
                y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#1a2236' }, beginAtZero: true }
            },
            plugins: {
                legend: { labels: { color: '#94a3b8', font: { size: 11 } } }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
async function loadSummary() {
    try {
        const data = await apiFetch(`/api/memory/summary?user_id=${state.userId}`);

        // Cards
        const facts = data.facts || {};
        const ilog = data.interactions || {};
        const faiss = data.faiss || {};
        const ctx = data.context || {};

        document.getElementById('val-facts').textContent = (facts.total ?? '--').toLocaleString();
        document.getElementById('val-interactions').textContent = (ilog.total_7d ?? '--').toLocaleString();
        document.getElementById('val-faiss').textContent = (faiss.vectors ?? '--').toLocaleString();
        document.getElementById('val-context').textContent = fmtPct(ctx.usage_pct);
        document.getElementById('val-ctx-tokens').textContent = (ctx.estimated_tokens ?? '--').toLocaleString();

        renderFactsCategoryChart(facts.by_category || {});
        renderInteractionTypesChart(ilog.by_type || {});
        renderContextGauge(ctx.usage_pct || 0);
    } catch (e) {
        console.error('Summary load failed:', e);
    }
}

async function loadTimeseries() {
    try {
        const data = await apiFetch(`/api/memory/timeseries?days=30&user_id=${state.userId}`);
        renderTimeseriesChart(data.series || []);
    } catch (e) {
        console.error('Timeseries load failed:', e);
    }
}

// ---------------------------------------------------------------------------
// DB Health
// ---------------------------------------------------------------------------
async function loadDbHealth() {
    try {
        const data = await apiFetch('/api/memory/db-health');
        const stores = data.stores || [];
        document.getElementById('val-db-size').textContent = fmtBytes(data.total_bytes || 0);

        const tbody = document.getElementById('health-body');
        tbody.innerHTML = '';
        for (const s of stores) {
            const rows = Object.entries(s.row_counts || {})
                .map(([k, v]) => `${k}: ${v?.toLocaleString() ?? '—'}`).join('<br>');
            tbody.insertAdjacentHTML('beforeend', `
                <tr>
                    <td class="store-name">${s.name}</td>
                    <td>${fmtBytes(s.size_bytes)}</td>
                    <td class="record-counts">${rows || '—'}</td>
                    <td>${s.last_modified || '—'}</td>
                    <td>${statusBadge(s.status)}</td>
                </tr>
            `);
        }
    } catch (e) {
        console.error('DB health load failed:', e);
    }
}

// ---------------------------------------------------------------------------
// Facts Explorer
// ---------------------------------------------------------------------------
async function loadFacts() {
    const { offset, limit, category, sort } = state.facts;
    const params = new URLSearchParams({
        user_id: state.userId, offset, limit, sort,
        ...(category ? { category } : {}),
    });
    try {
        const data = await apiFetch(`/api/memory/facts?${params}`);
        state.facts.total = data.total || 0;
        renderFacts(data.facts || []);
        updatePagination('facts', offset, limit, state.facts.total);
    } catch (e) {
        console.error('Facts load failed:', e);
    }
}

function renderFacts(facts) {
    const tbody = document.getElementById('facts-body');
    tbody.innerHTML = '';
    if (!facts.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty-row">No facts found</td></tr>';
        return;
    }
    for (const f of facts) {
        const conf = typeof f.confidence === 'number' ? (f.confidence * 100).toFixed(0) + '%' : '—';
        const created = fmtTs(f.created_at);
        const row = document.createElement('tr');
        row.dataset.factId = f.fact_id;
        row.innerHTML = `
            <td><span class="cat-badge cat-${f.category}">${f.category}</span></td>
            <td class="cell-subject">${escHtml(f.subject)}</td>
            <td class="cell-content">${escHtml(f.content)}</td>
            <td class="cell-source">${escHtml(f.source)}</td>
            <td>${conf}</td>
            <td>${f.times_referenced ?? 0}</td>
            <td class="cell-time">${created}</td>
            <td><button class="btn-delete" data-id="${f.fact_id}" title="Delete fact">✕</button></td>
        `;
        tbody.appendChild(row);
    }
    // Delete buttons
    tbody.querySelectorAll('.btn-delete').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm('Delete this fact?')) return;
            const id = btn.dataset.id;
            try {
                const r = await fetch(`/api/memory/facts/${encodeURIComponent(id)}`, { method: 'DELETE' });
                if (r.ok) {
                    btn.closest('tr').remove();
                    state.facts.total--;
                    updatePagination('facts', state.facts.offset, state.facts.limit, state.facts.total);
                }
            } catch (e) {
                console.error('Delete failed:', e);
            }
        });
    });
}

// ---------------------------------------------------------------------------
// Interaction Log
// ---------------------------------------------------------------------------
async function loadInteractions() {
    const { offset, limit, type, days } = state.ilog;
    const params = new URLSearchParams({
        user_id: state.userId, offset, limit, days,
        ...(type ? { type } : {}),
    });
    try {
        const data = await apiFetch(`/api/memory/interactions?${params}`);
        state.ilog.total = data.total || 0;
        renderInteractions(data.interactions || []);
        updatePagination('ilog', offset, limit, state.ilog.total);
    } catch (e) {
        console.error('Interactions load failed:', e);
    }
}

function renderInteractions(rows) {
    const tbody = document.getElementById('ilog-body');
    tbody.innerHTML = '';
    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty-row">No interactions found</td></tr>';
        return;
    }
    for (const r of rows) {
        tbody.insertAdjacentHTML('beforeend', `
            <tr>
                <td class="cell-time">${fmtTs(r.created_at)}</td>
                <td>${typeTag(r.type)}</td>
                <td class="cell-query">${escHtml(r.query || '')}</td>
                <td class="cell-summary">${escHtml(r.answer_summary || '')}</td>
            </tr>
        `);
    }
}

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------
function updatePagination(which, offset, limit, total) {
    const page = Math.floor(offset / limit) + 1;
    const pages = Math.max(1, Math.ceil(total / limit));
    document.getElementById(`${which}-page-info`).textContent = `Page ${page} of ${pages} (${total.toLocaleString()} total)`;
    document.getElementById(`${which}-prev`).disabled = offset <= 0;
    document.getElementById(`${which}-next`).disabled = offset + limit >= total;
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------
function escHtml(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
function initTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
            btn.classList.add('active');
            document.getElementById(`tab-${tab}`).classList.remove('hidden');
        });
    });
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------
function initFilters() {
    document.getElementById('filter-category').addEventListener('change', e => {
        state.facts.category = e.target.value;
        state.facts.offset = 0;
        loadFacts();
    });
    document.getElementById('filter-sort').addEventListener('change', e => {
        state.facts.sort = e.target.value;
        state.facts.offset = 0;
        loadFacts();
    });
    document.getElementById('filter-type').addEventListener('change', e => {
        state.ilog.type = e.target.value;
        state.ilog.offset = 0;
        loadInteractions();
    });
    document.getElementById('filter-days').addEventListener('change', e => {
        state.ilog.days = parseInt(e.target.value);
        state.ilog.offset = 0;
        loadInteractions();
    });

    document.getElementById('facts-prev').addEventListener('click', () => {
        state.facts.offset = Math.max(0, state.facts.offset - state.facts.limit);
        loadFacts();
    });
    document.getElementById('facts-next').addEventListener('click', () => {
        state.facts.offset += state.facts.limit;
        loadFacts();
    });
    document.getElementById('ilog-prev').addEventListener('click', () => {
        state.ilog.offset = Math.max(0, state.ilog.offset - state.ilog.limit);
        loadInteractions();
    });
    document.getElementById('ilog-next').addEventListener('click', () => {
        state.ilog.offset += state.ilog.limit;
        loadInteractions();
    });

    document.getElementById('user-select').addEventListener('change', e => {
        state.userId = e.target.value;
        state.facts.offset = 0;
        state.ilog.offset = 0;
        loadAll();
    });

    document.getElementById('btn-refresh').addEventListener('click', loadAll);
}

function loadAll() {
    loadSummary();
    loadTimeseries();
    loadDbHealth();
    loadFacts();
    loadInteractions();
}

document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initFilters();
    loadAll();
});

/* J.A.R.V.I.S. Memory & System Health Dashboard */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
    facts: { offset: 0, limit: 50, total: 0, category: '', sort: 'last_referenced', user: '' },
    ilog: { offset: 0, limit: 50, total: 0, type: '', days: 30, user: '' },
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
    const cls = { ok: 'status-ok', warning: 'status-warn', missing: 'status-missing', error: 'status-err' }[status] || 'status-err';
    return `<span class="status-badge ${cls}">${status}</span>`;
}

function typeTag(t) {
    const cls = { research: 'tag-research', tool_call: 'tag-tool', document: 'tag-doc', skill: 'tag-skill' }[t] || '';
    return `<span class="type-tag ${cls}">${t}</span>`;
}

function escHtml(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function apiFetch(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
}

// ---------------------------------------------------------------------------
// Charts — legend font 15px for 4K readability
// ---------------------------------------------------------------------------
const LEGEND_OPTS = { color: '#94a3b8', font: { size: 15 } };

const TYPE_COLORS = {
    research: '#38bdf8', tool_call: '#34d399', document: '#a78bfa', skill: '#fbbf24', other: '#64748b',
};
const CAT_PALETTE = ['#38bdf8','#34d399','#a78bfa','#fbbf24','#f87171','#fb923c','#e879f9','#818cf8'];

function destroyChart(id) {
    if (state.charts[id]) { state.charts[id].destroy(); delete state.charts[id]; }
}

function renderFactsCategoryChart(byCat) {
    destroyChart('facts-cat');
    const labels = Object.keys(byCat);
    if (!labels.length) return;
    const ctx = document.getElementById('chart-facts-cat').getContext('2d');
    state.charts['facts-cat'] = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{ data: Object.values(byCat), backgroundColor: CAT_PALETTE.slice(0, labels.length), borderWidth: 0 }]
        },
        options: {
            responsive: true,
            plugins: { legend: { position: 'right', labels: LEGEND_OPTS } }
        }
    });
}

function renderInteractionTypesChart(byType) {
    destroyChart('itype');
    const labels = Object.keys(byType);
    if (!labels.length) return;
    const ctx = document.getElementById('chart-interaction-types').getContext('2d');
    state.charts['itype'] = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{ data: Object.values(byType), backgroundColor: labels.map(l => TYPE_COLORS[l] || TYPE_COLORS.other), borderWidth: 0 }]
        },
        options: {
            responsive: true,
            plugins: { legend: { position: 'right', labels: LEGEND_OPTS } }
        }
    });
}

function renderContextGauge(usagePct) {
    destroyChart('ctx-gauge');
    const used = Math.min(usagePct, 100);
    const ctx = document.getElementById('chart-context-gauge').getContext('2d');
    state.charts['ctx-gauge'] = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['Used', 'Free'],
            datasets: [{
                data: [used, 100 - used],
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
                tooltip: { callbacks: { label: c => c.label + ': ' + c.raw.toFixed(1) + '%' } }
            }
        }
    });
}

function renderTimeseriesChart(series) {
    destroyChart('itime');
    if (!series.length) return;
    const ctx = document.getElementById('chart-interactions-time').getContext('2d');
    const types = ['research', 'tool_call', 'document', 'skill'];
    const datasets = types.map(t => ({
        label: t,
        data: series.map(d => d[t] || 0),
        borderColor: TYPE_COLORS[t],
        backgroundColor: TYPE_COLORS[t] + '33',
        fill: true, tension: 0.3, borderWidth: 2, pointRadius: 2,
    }));
    state.charts['itime'] = new Chart(ctx, {
        type: 'line',
        data: { labels: series.map(d => d.date), datasets },
        options: {
            responsive: true,
            scales: {
                x: { ticks: { color: '#64748b', maxTicksLimit: 8, font: { size: 12 } }, grid: { color: '#1a2236' } },
                y: { ticks: { color: '#64748b', font: { size: 12 } }, grid: { color: '#1a2236' }, beginAtZero: true }
            },
            plugins: { legend: { labels: LEGEND_OPTS } }
        }
    });
}

// ---------------------------------------------------------------------------
// Summary cards
// ---------------------------------------------------------------------------
async function loadSummary() {
    try {
        // No user_id param — backend aggregates all users
        const data = await apiFetch('/api/memory/summary');

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
        const data = await apiFetch('/api/memory/timeseries?days=30');
        renderTimeseriesChart(data.series || []);
    } catch (e) {
        console.error('Timeseries load failed:', e);
    }
}

// ---------------------------------------------------------------------------
// DB Health — card grid render
// ---------------------------------------------------------------------------
async function loadDbHealth() {
    try {
        const data = await apiFetch('/api/memory/db-health');
        const stores = data.stores || [];
        document.getElementById('val-db-size').textContent = fmtBytes(data.total_bytes || 0);

        const grid = document.getElementById('health-grid');
        grid.innerHTML = '';
        for (const s of stores) {
            const records = Object.entries(s.row_counts || {})
                .map(([k, v]) => `<span class="rec-label">${k}:</span> <strong>${v?.toLocaleString() ?? '—'}</strong>`)
                .join('<br>');
            grid.insertAdjacentHTML('beforeend', `
                <div class="health-card">
                    <div class="health-card-name">${escHtml(s.name)}</div>
                    <div class="health-card-records">${records || '—'}</div>
                    <div class="health-card-meta">
                        <span>${fmtBytes(s.size_bytes)} · ${s.last_modified || 'unknown'}</span>
                        ${statusBadge(s.status)}
                    </div>
                </div>
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
    const { offset, limit, category, sort, user } = state.facts;
    const params = new URLSearchParams({ offset, limit, sort });
    if (category) params.set('category', category);
    if (user) params.set('user_id', user);
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
        const row = document.createElement('tr');
        row.innerHTML = `
            <td><span class="cat-badge cat-${escHtml(f.category)}">${escHtml(f.category)}</span></td>
            <td class="cell-subject">${escHtml(f.subject)}</td>
            <td class="cell-content">${escHtml(f.content)}</td>
            <td class="cell-source">${escHtml(f.source)}</td>
            <td>${conf}</td>
            <td>${f.times_referenced ?? 0}</td>
            <td class="cell-time">${fmtTs(f.created_at)}</td>
            <td><button class="btn-delete" data-id="${escHtml(f.fact_id)}" title="Delete fact">✕</button></td>
        `;
        tbody.appendChild(row);
    }
    tbody.querySelectorAll('.btn-delete').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm('Delete this fact?')) return;
            try {
                const r = await fetch(`/api/memory/facts/${encodeURIComponent(btn.dataset.id)}`, { method: 'DELETE' });
                if (r.ok) { btn.closest('tr').remove(); state.facts.total--; updatePagination('facts', state.facts.offset, state.facts.limit, state.facts.total); }
            } catch (e) { console.error('Delete failed:', e); }
        });
    });
}

// ---------------------------------------------------------------------------
// Interaction Log
// ---------------------------------------------------------------------------
async function loadInteractions() {
    const { offset, limit, type, days, user } = state.ilog;
    const params = new URLSearchParams({ offset, limit, days });
    if (type) params.set('type', type);
    if (user) params.set('user_id', user);
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
        tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No interactions found</td></tr>';
        return;
    }
    for (const r of rows) {
        tbody.insertAdjacentHTML('beforeend', `
            <tr>
                <td class="cell-time">${fmtTs(r.created_at)}</td>
                <td class="cell-user">${escHtml(r.user_id || '—')}</td>
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
// Tabs
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
// Filters + pagination wiring
// ---------------------------------------------------------------------------
function initFilters() {
    document.getElementById('filter-category').addEventListener('change', e => { state.facts.category = e.target.value; state.facts.offset = 0; loadFacts(); });
    document.getElementById('filter-user-facts').addEventListener('change', e => { state.facts.user = e.target.value; state.facts.offset = 0; loadFacts(); });
    document.getElementById('filter-sort').addEventListener('change', e => { state.facts.sort = e.target.value; state.facts.offset = 0; loadFacts(); });
    document.getElementById('filter-type').addEventListener('change', e => { state.ilog.type = e.target.value; state.ilog.offset = 0; loadInteractions(); });
    document.getElementById('filter-user-ilog').addEventListener('change', e => { state.ilog.user = e.target.value; state.ilog.offset = 0; loadInteractions(); });
    document.getElementById('filter-days').addEventListener('change', e => { state.ilog.days = parseInt(e.target.value); state.ilog.offset = 0; loadInteractions(); });

    document.getElementById('facts-prev').addEventListener('click', () => { state.facts.offset = Math.max(0, state.facts.offset - state.facts.limit); loadFacts(); });
    document.getElementById('facts-next').addEventListener('click', () => { state.facts.offset += state.facts.limit; loadFacts(); });
    document.getElementById('ilog-prev').addEventListener('click', () => { state.ilog.offset = Math.max(0, state.ilog.offset - state.ilog.limit); loadInteractions(); });
    document.getElementById('ilog-next').addEventListener('click', () => { state.ilog.offset += state.ilog.limit; loadInteractions(); });

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

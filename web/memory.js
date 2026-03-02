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
// Color constants
// ---------------------------------------------------------------------------
const USER_DISPLAY = { christopher: 'Chris', erica: 'Erica' };
const LEGEND_OPTS = { color: '#94a3b8', font: { size: 15 } };

// Per-category color pairs: [chris_color, erica_color]
const CAT_COLORS = {
    general:      ['#34d399', '#047857'],
    preference:   ['#38bdf8', '#0284c7'],
    relationship: ['#a78bfa', '#7c3aed'],
    work:         ['#fbbf24', '#d97706'],
    location:     ['#fb923c', '#ea580c'],
    health:       ['#f87171', '#b91c1c'],
    habit:        ['#818cf8', '#4f46e5'],
};
const CAT_FALLBACK = ['#64748b', '#334155'];

// Per-type colors: { christopher: hex, erica: hex }
const TYPE_COLORS_USER = {
    research:  { christopher: '#38bdf8', erica: '#0284c7' },
    tool_call: { christopher: '#34d399', erica: '#047857' },
    document:  { christopher: '#a78bfa', erica: '#7c3aed' },
    skill:     { christopher: '#fbbf24', erica: '#d97706' },
    other:     { christopher: '#64748b', erica: '#334155' },
};

function destroyChart(id) {
    if (state.charts[id]) { state.charts[id].destroy(); delete state.charts[id]; }
}

// ---------------------------------------------------------------------------
// Chart 1: Facts by Category — slices per category·user
// ---------------------------------------------------------------------------
function renderFactsCategoryChart(items) {
    destroyChart('facts-cat');
    if (!items || !items.length) return;
    const labels = items.map(i => `${i.category} · ${USER_DISPLAY[i.user_id] || i.user_id}`);
    const colors = items.map(i => {
        const pair = CAT_COLORS[i.category] || CAT_FALLBACK;
        return i.user_id === 'christopher' ? pair[0] : pair[1];
    });
    const ctx = document.getElementById('chart-facts-cat').getContext('2d');
    state.charts['facts-cat'] = new Chart(ctx, {
        type: 'doughnut',
        data: { labels, datasets: [{ data: items.map(i => i.count), backgroundColor: colors, borderWidth: 0 }] },
        options: { responsive: true, plugins: { legend: { position: 'right', labels: LEGEND_OPTS } } }
    });
}

// ---------------------------------------------------------------------------
// Chart 4: Interactions by Type (7d) — slices per type·user
// ---------------------------------------------------------------------------
function renderInteractionTypesChart(items) {
    destroyChart('itype');
    if (!items || !items.length) return;
    const labels = items.map(i => `${i.type} · ${USER_DISPLAY[i.user_id] || i.user_id}`);
    const colors = items.map(i => {
        const tc = TYPE_COLORS_USER[i.type] || TYPE_COLORS_USER.other;
        return i.user_id === 'christopher' ? tc.christopher : tc.erica;
    });
    const ctx = document.getElementById('chart-interaction-types').getContext('2d');
    state.charts['itype'] = new Chart(ctx, {
        type: 'doughnut',
        data: { labels, datasets: [{ data: items.map(i => i.count), backgroundColor: colors, borderWidth: 0 }] },
        options: { responsive: true, plugins: { legend: { position: 'right', labels: LEGEND_OPTS } } }
    });
}

// ---------------------------------------------------------------------------
// Chart 3: Context Budget gauge (session-level, shared — not per-user)
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// Chart 2: Interactions Over Time (30d) — one line per type·user
// Chris = solid, Erica = dashed
// ---------------------------------------------------------------------------
function renderTimeseriesChart(seriesByUser) {
    destroyChart('itime');
    const users = Object.keys(seriesByUser || {});
    if (!users.length) return;

    const allDates = [...new Set(users.flatMap(u => seriesByUser[u].map(d => d.date)))].sort();
    const types = ['research', 'tool_call', 'document', 'skill'];
    const datasets = [];

    for (const user of users) {
        const dateMap = Object.fromEntries(seriesByUser[user].map(d => [d.date, d]));
        const shortName = USER_DISPLAY[user] || user;
        const isDashed = user !== 'christopher';

        for (const type of types) {
            const typeData = allDates.map(d => dateMap[d]?.[type] || 0);
            if (typeData.every(v => v === 0)) continue;
            const tc = TYPE_COLORS_USER[type] || TYPE_COLORS_USER.other;
            const color = isDashed ? tc.erica : tc.christopher;
            datasets.push({
                label: `${type} · ${shortName}`,
                data: typeData,
                borderColor: color,
                backgroundColor: color + '22',
                borderDash: isDashed ? [5, 3] : [],
                fill: true,
                tension: 0.3,
                borderWidth: isDashed ? 1.5 : 2,
                pointRadius: 2,
            });
        }
    }

    const ctx = document.getElementById('chart-interactions-time').getContext('2d');
    state.charts['itime'] = new Chart(ctx, {
        type: 'line',
        data: { labels: allDates, datasets },
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
// Sparkline — tiny timeline chart embedded in health cards
// ---------------------------------------------------------------------------
function renderSparkline(canvasId, timeline) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !timeline || timeline.length < 1) return;
    destroyChart(canvasId);
    // Set canvas resolution to match its displayed CSS width (avoids stretch artifacts)
    const displayWidth = canvas.offsetWidth;
    if (displayWidth > 0) canvas.width = displayWidth;
    canvas.height = 50;
    const ctx = canvas.getContext('2d');
    state.charts[canvasId] = new Chart(ctx, {
        type: 'line',
        data: {
            labels: timeline.map(t => t.date),
            datasets: [{
                data: timeline.map(t => t.count),
                borderColor: '#38bdf8',
                backgroundColor: '#38bdf815',
                borderWidth: 1.5,
                fill: true,
                tension: 0.4,
                pointRadius: 0,
            }]
        },
        options: {
            responsive: false,
            animation: false,
            scales: { x: { display: false }, y: { display: false, beginAtZero: true } },
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
        }
    });
}

// ---------------------------------------------------------------------------
// Summary cards
// ---------------------------------------------------------------------------
async function loadSummary() {
    try {
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

        renderFactsCategoryChart(facts.by_category_user || []);
        renderInteractionTypesChart(ilog.by_type_user || []);
        renderContextGauge(ctx.usage_pct || 0);
    } catch (e) {
        console.error('Summary load failed:', e);
    }
}

async function loadTimeseries() {
    try {
        const data = await apiFetch('/api/memory/timeseries?days=30');
        renderTimeseriesChart(data.series_by_user || {});
    } catch (e) {
        console.error('Timeseries load failed:', e);
    }
}

// ---------------------------------------------------------------------------
// DB Health — card grid with sparklines
// ---------------------------------------------------------------------------
async function loadDbHealth() {
    try {
        const data = await apiFetch('/api/memory/db-health');
        const stores = data.stores || [];
        document.getElementById('val-db-size').textContent = fmtBytes(data.total_bytes || 0);

        const grid = document.getElementById('health-grid');
        grid.innerHTML = '';

        stores.forEach((s, i) => {
            const canvasId = `sparkline-${i}`;
            const records = Object.entries(s.row_counts || {})
                .map(([k, v]) => `<span class="rec-label">${k}:</span> <strong>${v?.toLocaleString() ?? '—'}</strong>`)
                .join('<br>');
            const hasTimeline = s.timeline && s.timeline.length > 0;
            grid.insertAdjacentHTML('beforeend', `
                <div class="health-card">
                    <div class="health-card-name">${escHtml(s.name)}</div>
                    <div class="health-card-records">${records || '—'}</div>
                    ${hasTimeline ? `<canvas id="${canvasId}" class="health-sparkline" width="220" height="50"></canvas>` : ''}
                    <div class="health-card-meta">
                        <span>${fmtBytes(s.size_bytes)} · ${s.last_modified || 'unknown'}</span>
                        ${statusBadge(s.status)}
                    </div>
                </div>
            `);
            if (hasTimeline) {
                requestAnimationFrame(() => renderSparkline(canvasId, s.timeline));
            }
        });
    } catch (e) {
        console.error('DB health load failed:', e);
    }
}

// ---------------------------------------------------------------------------
// Inline editing for facts table
// ---------------------------------------------------------------------------
const FACT_CATEGORIES = ['general', 'preference', 'relationship', 'work', 'location', 'health', 'habit'];

function makeCellEditable(td, field, factId) {
    if (td.classList.contains('cell-editing')) return;
    td.classList.add('cell-editing');

    const original = td.dataset.value;
    let saved = false;
    let el;

    if (field === 'category') {
        el = document.createElement('select');
        el.className = 'cell-edit';
        FACT_CATEGORIES.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c; opt.textContent = c;
            if (c === original) opt.selected = true;
            el.appendChild(opt);
        });
    } else {
        el = document.createElement('input');
        el.type = 'text';
        el.className = 'cell-edit';
        el.value = original;
    }

    td.innerHTML = '';
    td.appendChild(el);
    el.focus();
    if (el.tagName === 'INPUT') el.select();

    function restore(text) {
        saved = true;
        td.classList.remove('cell-editing');
        td.dataset.value = text;
        if (field === 'category') {
            td.innerHTML = `<span class="cat-badge cat-${escHtml(text)}">${escHtml(text)}</span>`;
        } else {
            td.textContent = text;
        }
        td.onclick = () => makeCellEditable(td, field, factId);
    }

    async function trySave() {
        if (saved) return;
        const newVal = el.value.trim();
        if (!newVal || newVal === original) { restore(original); return; }
        try {
            const r = await fetch(`/api/memory/facts/${encodeURIComponent(factId)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ [field]: newVal }),
            });
            restore(r.ok ? newVal : original);
        } catch (e) {
            console.error('Fact update failed:', e);
            restore(original);
        }
    }

    if (field === 'category') {
        el.addEventListener('change', trySave);
        el.addEventListener('blur', () => { if (!saved) restore(original); });
    } else {
        el.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
            if (e.key === 'Escape') { e.preventDefault(); saved = true; restore(original); }
        });
        el.addEventListener('blur', trySave);
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
            <td class="cell-editable" data-field="category" data-id="${escHtml(f.fact_id)}" data-value="${escHtml(f.category)}">
                <span class="cat-badge cat-${escHtml(f.category)}">${escHtml(f.category)}</span>
            </td>
            <td class="cell-subject cell-editable" data-field="subject" data-id="${escHtml(f.fact_id)}" data-value="${escHtml(f.subject)}">${escHtml(f.subject)}</td>
            <td class="cell-content cell-editable" data-field="content" data-id="${escHtml(f.fact_id)}" data-value="${escHtml(f.content)}">${escHtml(f.content)}</td>
            <td class="cell-source cell-editable" data-field="source" data-id="${escHtml(f.fact_id)}" data-value="${escHtml(f.source)}">${escHtml(f.source)}</td>
            <td>${conf}</td>
            <td>${f.times_referenced ?? 0}</td>
            <td class="cell-time">${fmtTs(f.created_at)}</td>
            <td><button class="btn-delete" data-id="${escHtml(f.fact_id)}" title="Delete fact">✕</button></td>
        `;
        tbody.appendChild(row);
    }
    tbody.querySelectorAll('.cell-editable').forEach(td => {
        td.title = 'Click to edit';
        td.onclick = () => makeCellEditable(td, td.dataset.field, td.dataset.id);
    });
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
        tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No interactions found</td></tr>';
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
                <td><button class="btn-delete" data-id="${escHtml(r.interaction_id)}" title="Delete entry">✕</button></td>
            </tr>
        `);
    }
    tbody.querySelectorAll('.btn-delete').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm('Delete this interaction log entry?')) return;
            try {
                const resp = await fetch(`/api/memory/interactions/${encodeURIComponent(btn.dataset.id)}`, { method: 'DELETE' });
                if (resp.ok) { btn.closest('tr').remove(); state.ilog.total--; updatePagination('ilog', state.ilog.offset, state.ilog.limit, state.ilog.total); }
            } catch (e) { console.error('Delete failed:', e); }
        });
    });
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

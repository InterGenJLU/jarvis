/* JARVIS Governance Approval Dashboard */

(function () {
    'use strict';

    const token = new URLSearchParams(location.search).get('token');
    let currentProposalId = null;

    function authUrl(url) {
        return token ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : url;
    }

    function fmtTime(ts) {
        if (!ts) return '--';
        const d = new Date(ts * 1000);
        return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
    }

    function escHtml(s) {
        const d = document.createElement('div');
        d.textContent = s || '';
        return d.innerHTML;
    }

    const TIER_NAMES = { 0: 'Read', 1: 'Config', 2: 'Prompt', 3: 'Logic', 4: 'Architecture' };

    // ── Status cards ────────────────────────────────────────────────

    async function loadStatus() {
        try {
            const r = await fetch(authUrl('/api/governance/status'));
            if (!r.ok) return;
            const s = await r.json();

            document.getElementById('val-status').textContent = s.healthy ? 'Healthy' : 'UNHEALTHY';
            document.getElementById('val-status').style.color = s.healthy ? '#34d399' : '#f87171';

            document.getElementById('val-integrity').textContent = s.integrity_verified ? 'Verified' : 'FAILED';
            document.getElementById('val-integrity').style.color = s.integrity_verified ? '#34d399' : '#f87171';

            const circuitEl = document.getElementById('val-circuit');
            circuitEl.textContent = s.circuit_breaker_open ? 'TRIPPED' : 'Closed';
            circuitEl.style.color = s.circuit_breaker_open ? '#f87171' : '#34d399';

            // Show/hide reset instructions
            const resetBanner = document.getElementById('circuit-reset-banner');
            if (resetBanner) {
                if (s.circuit_breaker_open) {
                    resetBanner.style.display = 'block';
                } else {
                    resetBanner.style.display = 'none';
                }
            }

            const tierVal = typeof s.max_autonomous_tier === 'number' ? s.max_autonomous_tier : s.max_autonomous_tier;
            document.getElementById('val-tier').textContent = `Tier ${tierVal} (${TIER_NAMES[tierVal] || '?'})`;

            document.getElementById('val-commandments').textContent = s.commandments_loaded ? 'Loaded' : 'MISSING';
            document.getElementById('val-commandments').style.color = s.commandments_loaded ? '#34d399' : '#f87171';

            const badge = document.getElementById('gov-health');
            badge.textContent = s.healthy ? 'Healthy' : 'ALERT';
            badge.className = 'gov-health-badge ' + (s.healthy ? 'gov-healthy' : 'gov-unhealthy');
        } catch (e) {
            console.error('loadStatus:', e);
        }
    }

    // ── Proposals list ──────────────────────────────────────────────

    async function loadProposals() {
        try {
            const statusFilter = document.getElementById('filter-status').value;
            const url = statusFilter
                ? `/api/governance/proposals?status=${statusFilter}`
                : '/api/governance/proposals';
            const r = await fetch(authUrl(url));
            if (!r.ok) return;
            const data = await r.json();
            const proposals = data.proposals || [];

            document.getElementById('val-pending').textContent =
                proposals.filter(p => p.status === 'pending').length ||
                (statusFilter === 'pending' ? proposals.length : '--');

            const list = document.getElementById('proposals-list');
            list.innerHTML = '';

            if (!proposals.length) {
                list.innerHTML = '<div class="empty-state">No proposals match this filter</div>';
                return;
            }

            for (const p of proposals) {
                const card = document.createElement('div');
                card.className = 'proposal-card';
                card.onclick = () => openReview(p.id);
                card.innerHTML = `
                    <div class="proposal-card-header">
                        <span class="proposal-action">${escHtml(p.action)}</span>
                        <div>
                            <span class="proposal-tier tier-${p.tier}">Tier ${p.tier} — ${TIER_NAMES[p.tier] || '?'}</span>
                            <span class="proposal-status status-${p.status}">${p.status.replace(/_/g, ' ')}</span>
                        </div>
                    </div>
                    <div class="proposal-description">${escHtml(p.description)}</div>
                    <div class="proposal-meta">
                        ID: <code>${escHtml(p.id)}</code> · Submitted ${fmtTime(p.created_at)} · Expires ${fmtTime(p.expires_at)}
                        ${p.review_comment ? ' · Comment: ' + escHtml(p.review_comment) : ''}
                    </div>
                `;
                list.appendChild(card);
            }
        } catch (e) {
            console.error('loadProposals:', e);
        }
    }

    // ── Review modal ────────────────────────────────────────────────

    async function openReview(proposalId) {
        try {
            const r = await fetch(authUrl(`/api/governance/proposals/${proposalId}`));
            if (!r.ok) return;
            const p = await r.json();
            currentProposalId = proposalId;

            const modal = document.getElementById('review-modal');
            const body = document.getElementById('modal-body');
            const confDisplay = document.getElementById('confirmation-display');
            confDisplay.classList.add('hidden');

            // Show/hide action buttons based on status
            const actions = document.querySelector('.modal-actions');
            const commentBox = document.querySelector('.modal-comment');
            if (p.status === 'pending') {
                actions.style.display = 'flex';
                commentBox.style.display = 'block';
            } else {
                actions.style.display = 'none';
                commentBox.style.display = 'none';
            }

            document.getElementById('modal-title').textContent =
                `${p.action} — Tier ${p.tier} (${TIER_NAMES[p.tier] || '?'})`;

            let html = '';

            // Description
            html += `<div class="proposal-detail-row">
                <div class="proposal-detail-label">Description</div>
                <div class="proposal-detail-value">${escHtml(p.description)}</div>
            </div>`;

            // Justification
            if (p.justification) {
                html += `<div class="proposal-detail-row">
                    <div class="proposal-detail-label">Justification</div>
                    <div class="proposal-detail-value">${escHtml(p.justification)}</div>
                </div>`;
            }

            // Diff
            if (p.diff) {
                const diffHtml = escHtml(p.diff).split('\n').map(line => {
                    if (line.startsWith('+')) return `<span class="diff-add">${line}</span>`;
                    if (line.startsWith('-')) return `<span class="diff-remove">${line}</span>`;
                    return line;
                }).join('\n');
                html += `<div class="proposal-detail-row">
                    <div class="proposal-detail-label">Changes</div>
                    <div class="proposal-diff">${diffHtml}</div>
                </div>`;
            }

            // Rollback plan
            if (p.rollback_plan) {
                html += `<div class="proposal-detail-row">
                    <div class="proposal-detail-label">Rollback Plan</div>
                    <div class="proposal-detail-value">${escHtml(p.rollback_plan)}</div>
                </div>`;
            }

            // Status + timestamps
            html += `<div class="proposal-detail-row">
                <div class="proposal-detail-label">Status</div>
                <div class="proposal-detail-value">
                    <span class="proposal-status status-${p.status}">${p.status.replace(/_/g, ' ')}</span>
                    · Created ${fmtTime(p.created_at)}
                    ${p.reviewed_at ? ' · Reviewed ' + fmtTime(p.reviewed_at) : ''}
                    ${p.confirmed_at ? ' · Confirmed ' + fmtTime(p.confirmed_at) : ''}
                </div>
            </div>`;

            body.innerHTML = html;
            modal.classList.remove('hidden');
        } catch (e) {
            console.error('openReview:', e);
        }
    }

    // ── Review actions ──────────────────────────────────────────────

    document.querySelectorAll('.btn-action').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!currentProposalId) return;
            const decision = btn.dataset.decision;
            const comment = document.getElementById('review-comment').value.trim();

            try {
                const r = await fetch(authUrl(`/api/governance/proposals/${currentProposalId}/review`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ decision, comment: comment || null }),
                });
                if (!r.ok) {
                    alert('Review failed: ' + (await r.text()));
                    return;
                }
                const result = await r.json();

                if (decision === 'approve' && result.confirmation_code) {
                    // Show proposal ID + confirmation code
                    const confDisplay = document.getElementById('confirmation-display');
                    document.getElementById('confirmation-id').textContent = currentProposalId;
                    document.getElementById('confirmation-code').textContent = result.confirmation_code;
                    document.querySelector('.confirmation-hint').innerHTML =
                        `Enter these in the console with: <code>jarvis-approve</code>`;
                    confDisplay.classList.remove('hidden');

                    // Hide action buttons
                    document.querySelector('.modal-actions').style.display = 'none';
                    document.querySelector('.modal-comment').style.display = 'none';
                } else {
                    // Close modal and refresh
                    document.getElementById('review-modal').classList.add('hidden');
                    loadProposals();
                }
            } catch (e) {
                console.error('Review action:', e);
                alert('Error submitting review');
            }
        });
    });

    // ── Modal close ─────────────────────────────────────────────────

    document.getElementById('modal-close').addEventListener('click', () => {
        document.getElementById('review-modal').classList.add('hidden');
        currentProposalId = null;
        document.getElementById('review-comment').value = '';
        loadProposals();
    });

    document.querySelector('.modal-backdrop').addEventListener('click', () => {
        document.getElementById('modal-close').click();
    });

    // ── Test proposal ─────────────────────────────────────────────

    document.getElementById('btn-test-proposal').addEventListener('click', async () => {
        try {
            const r = await fetch(authUrl('/api/governance/test-proposal'), { method: 'POST' });
            if (r.ok) {
                loadProposals();
            } else {
                alert('Failed to submit test proposal');
            }
        } catch (e) {
            console.error('Test proposal:', e);
        }
    });

    // ── Filter ──────────────────────────────────────────────────────

    document.getElementById('filter-status').addEventListener('change', loadProposals);

    // ── Init ────────────────────────────────────────────────────────

    loadStatus();
    loadProposals();
    setInterval(() => { loadStatus(); loadProposals(); }, 15000);
})();

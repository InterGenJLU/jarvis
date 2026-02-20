/**
 * JARVIS Web UI — WebSocket client + rendering
 */

(function () {
    'use strict';

    // --- DOM refs ---
    const messagesEl = document.getElementById('messages');
    const chatArea = document.getElementById('chat-area');
    const userInput = document.getElementById('user-input');
    const btnSend = document.getElementById('btn-send');
    const btnPaste = document.getElementById('btn-paste');
    const btnClear = document.getElementById('btn-clear');
    const btnHelp = document.getElementById('btn-help');
    const statusEl = document.getElementById('connection-status');
    const voiceToggle = document.getElementById('voice-toggle');
    const btnRestart = document.getElementById('btn-restart');
    const docIndicator = document.getElementById('doc-indicator');
    const docInfo = document.getElementById('doc-info');
    const docClearBtn = document.getElementById('doc-clear-btn');

    // Paste modal
    const pasteModal = document.getElementById('paste-modal');
    const pasteTextarea = document.getElementById('paste-textarea');
    const pasteSubmit = document.getElementById('paste-submit');
    const pasteCancel = document.getElementById('paste-cancel');
    const pasteModalClose = document.getElementById('paste-modal-close');

    // Append modal
    const appendModal = document.getElementById('append-modal');
    const appendTextarea = document.getElementById('append-textarea');
    const appendSubmit = document.getElementById('append-submit');
    const appendCancel = document.getElementById('append-cancel');
    const appendModalClose = document.getElementById('append-modal-close');

    // File path modal
    const fileModal = document.getElementById('file-modal');
    const filePathInput = document.getElementById('file-path-input');
    const fileSubmit = document.getElementById('file-submit');
    const fileCancel = document.getElementById('file-cancel');
    const fileModalClose = document.getElementById('file-modal-close');

    // Help modal
    const helpModal = document.getElementById('help-modal');
    const helpModalClose = document.getElementById('help-modal-close');
    const helpClose = document.getElementById('help-close');

    // New toolbar buttons
    const btnAppend = document.getElementById('btn-append');
    const btnFile = document.getElementById('btn-file');
    const btnClipboard = document.getElementById('btn-clipboard');
    const btnContext = document.getElementById('btn-context');

    // Announcement banner container
    const announcementContainer = document.getElementById('announcement-container');

    // Header stats
    const statLlm = document.getElementById('stat-llm');
    const statSkills = document.getElementById('stat-skills');
    const statMemory = document.getElementById('stat-memory');
    const statContext = document.getElementById('stat-context');
    const statWeb = document.getElementById('stat-web');
    const statNews = document.getElementById('stat-news');
    const statReminders = document.getElementById('stat-reminders');
    const statCalendar = document.getElementById('stat-calendar');

    // File browser
    const btnBrowseToggle = document.getElementById('btn-browse-toggle');
    const fileBrowser = document.getElementById('file-browser');
    const browserList = document.getElementById('browser-list');
    const browserCurrentPath = document.getElementById('browser-current-path');

    // Sidebar
    const sidebar = document.getElementById('sidebar');
    const sidebarOverlay = document.getElementById('sidebar-overlay');
    const sidebarClose = document.getElementById('sidebar-close');
    const sessionListEl = document.getElementById('session-list');
    const btnViewMore = document.getElementById('btn-view-more');
    const btnHamburger = document.getElementById('btn-hamburger');

    // --- State ---
    let ws = null;
    let processing = false;
    let reconnectDelay = 1000;
    const MAX_RECONNECT_DELAY = 30000;
    let reconnectAttempt = 0;
    let reconnectTimer = null;

    // Session sidebar state
    let sessions = [];
    let activeSessionId = null;
    let sessionOffset = 0;
    let sidebarOpen = false;
    let sessionsHasMore = false;

    // Command history
    const commandHistory = [];
    let historyIndex = -1;
    let pendingInput = '';

    // --- WebSocket ---
    function connect() {
        setStatus('connecting');
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${protocol}//${location.host}/ws`);

        ws.onopen = function () {
            setStatus('connected');
            reconnectDelay = 1000;
            if (reconnectAttempt > 0) {
                addInfoMessage('Reconnected to server');
            }
            reconnectAttempt = 0;
            // Restore voice preference
            const voicePref = localStorage.getItem('jarvis-voice') === 'true';
            voiceToggle.checked = voicePref;
            ws.send(JSON.stringify({ type: 'toggle_voice', enabled: voicePref }));
        };

        ws.onmessage = function (event) {
            let msg;
            try {
                msg = JSON.parse(event.data);
            } catch {
                return;
            }
            handleServerMessage(msg);
        };

        ws.onclose = function () {
            ws = null;
            reconnectAttempt++;
            var delay = reconnectDelay;
            setStatus('reconnecting');
            statusEl.textContent = 'Reconnecting (#' + reconnectAttempt + ')...';
            reconnectTimer = setTimeout(connect, delay);
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
        };

        ws.onerror = function () {
            // onclose will handle reconnect
        };
    }

    function setStatus(state) {
        statusEl.textContent = state.charAt(0).toUpperCase() + state.slice(1);
        statusEl.className = 'status ' + state;
    }

    // --- Server message handler ---
    function handleServerMessage(msg) {
        switch (msg.type) {
            case 'response':
                removeThinking();
                addMessage('jarvis', msg.content);
                setProcessing(false);
                break;

            case 'stats':
                addStats(msg.data);
                break;

            case 'announcement':
                addAnnouncement(msg.content);
                break;

            case 'info':
                addInfoMessage(msg.content);
                break;

            case 'error':
                removeThinking();
                addMessage('error', msg.content);
                setProcessing(false);
                break;

            case 'doc_status':
                updateDocIndicator(msg);
                break;

            case 'voice_status':
                voiceToggle.checked = msg.enabled;
                break;

            case 'health_report':
                addHealthReport(msg.data);
                break;

            case 'history':
                loadHistory(msg.messages, msg.session_id || null);
                break;

            case 'session_list':
                handleSessionList(msg);
                break;

            case 'system_stats':
                updateSystemStats(msg.data);
                break;

            case 'stream_start':
                removeThinking();
                startStreaming();
                break;

            case 'stream_token':
                appendStreamToken(msg.token);
                break;

            case 'stream_end':
                endStreaming(msg.full_response);
                setProcessing(false);
                break;
        }
    }

    // --- Streaming ---
    let streamingBubble = null;

    function startStreaming() {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message jarvis';

        const sender = document.createElement('div');
        sender.className = 'message-sender';
        sender.textContent = 'J.A.R.V.I.S.';

        streamingBubble = document.createElement('div');
        streamingBubble.className = 'message-bubble streaming';
        streamingBubble.textContent = '';

        messageDiv.appendChild(sender);
        messageDiv.appendChild(streamingBubble);
        messagesEl.appendChild(messageDiv);
        scrollToBottom();
    }

    function appendStreamToken(token) {
        if (streamingBubble) {
            streamingBubble.textContent += token;
            scrollToBottom();
        }
    }

    function endStreaming(fullResponse) {
        if (streamingBubble) {
            streamingBubble.classList.remove('streaming');
            if (fullResponse) {
                streamingBubble.innerHTML = renderMarkdown(fullResponse);
            }
            // Add timestamp to the streamed message
            var parent = streamingBubble.parentElement;
            if (parent) {
                var ts = document.createElement('div');
                ts.className = 'message-timestamp';
                ts.textContent = formatTimestamp(Date.now() / 1000);
                parent.appendChild(ts);
            }
        }
        streamingBubble = null;
        scrollToBottom();
    }

    // --- Message rendering ---
    function addMessage(role, content) {
        if (!content) return;

        const messageDiv = document.createElement('div');
        messageDiv.className = 'message ' + role;

        if (role !== 'error') {
            const sender = document.createElement('div');
            sender.className = 'message-sender';
            sender.textContent = role === 'user' ? 'YOU' : 'J.A.R.V.I.S.';
            messageDiv.appendChild(sender);
        }

        const bubble = document.createElement('div');
        bubble.className = 'message-bubble';
        if (role === 'jarvis') {
            bubble.innerHTML = renderMarkdown(content);
        } else {
            bubble.textContent = content;
        }
        messageDiv.appendChild(bubble);

        // Timestamp on live messages
        if (role !== 'error') {
            const ts = document.createElement('div');
            ts.className = 'message-timestamp';
            ts.textContent = formatTimestamp(Date.now() / 1000);
            messageDiv.appendChild(ts);
        }

        messagesEl.appendChild(messageDiv);
        scrollToBottom();
    }

    function addStats(data) {
        const last = messagesEl.lastElementChild;
        if (!last || !last.classList.contains('jarvis')) return;

        const panel = document.createElement('div');
        panel.className = 'stats-panel';

        const items = [];
        if (data.layer) items.push(['Layer', data.layer]);
        if (data.skill_name) items.push(['Skill', data.skill_name]);
        if (data.handler) items.push(['Handler', data.handler]);
        if (data.confidence != null) items.push(['Conf', (data.confidence * 100).toFixed(0) + '%']);
        if (data.total_ms != null) items.push(['Time', data.total_ms + 'ms']);
        if (data.llm_model) items.push(['LLM', data.llm_model]);
        if (data.llm_tokens) items.push(['Tokens', data.llm_tokens]);

        items.forEach(([label, value]) => {
            const item = document.createElement('span');
            item.className = 'stat-item';
            item.innerHTML = `<span class="stat-label">${label}:</span> <span class="stat-value">${value}</span>`;
            panel.appendChild(item);
        });

        last.appendChild(panel);
        scrollToBottom();
    }

    function updateSystemStats(data) {
        // LLM
        if (data.llm && data.llm.model) {
            statLlm.textContent = data.llm.model;
            statLlm.className = 'hud-value active';
        } else {
            statLlm.textContent = 'OFF';
            statLlm.className = 'hud-value inactive';
        }

        // Skills
        statSkills.textContent = data.skills_loaded;
        statSkills.className = 'hud-value active';

        // Memory
        if (data.memory) {
            statMemory.textContent = data.memory.vectors + ' vectors' +
                (data.memory.proactive ? ' · proactive' : '');
            statMemory.className = 'hud-value active';
        } else {
            statMemory.textContent = 'OFF';
            statMemory.className = 'hud-value inactive';
        }

        // Context window
        if (data.context_window) {
            statContext.textContent = data.context_window.segments + ' seg · ' +
                data.context_window.tokens + ' tok';
            statContext.className = 'hud-value';
        } else {
            statContext.textContent = 'OFF';
            statContext.className = 'hud-value inactive';
        }

        // Web research
        if (data.web_research) {
            statWeb.textContent = 'ON';
            statWeb.className = 'hud-value active';
        } else {
            statWeb.textContent = 'OFF';
            statWeb.className = 'hud-value inactive';
        }

        // News
        if (data.news) {
            statNews.textContent = data.news.feeds + ' feeds';
            statNews.className = 'hud-value active';
        } else {
            statNews.textContent = 'OFF';
            statNews.className = 'hud-value inactive';
        }

        // Reminders
        if (data.reminders) {
            statReminders.textContent = data.reminders.active + ' active';
            statReminders.className = data.reminders.active > 0 ? 'hud-value active' : 'hud-value';
        } else {
            statReminders.textContent = 'OFF';
            statReminders.className = 'hud-value inactive';
        }

        // Calendar
        if (data.calendar) {
            statCalendar.textContent = 'SYNCED';
            statCalendar.className = 'hud-value active';
        } else {
            statCalendar.textContent = 'OFF';
            statCalendar.className = 'hud-value inactive';
        }
    }

    function addAnnouncement(content) {
        // Add inline record in chat
        const div = document.createElement('div');
        div.className = 'announcement-inline';
        div.textContent = content;
        messagesEl.appendChild(div);
        scrollToBottom();

        // Show floating banner at top
        var banner = document.createElement('div');
        banner.className = 'announcement-banner';
        banner.textContent = content;
        var closeBtn = document.createElement('button');
        closeBtn.className = 'announcement-close';
        closeBtn.innerHTML = '&times;';
        closeBtn.addEventListener('click', function () {
            dismissBanner(banner);
        });
        banner.appendChild(closeBtn);
        announcementContainer.appendChild(banner);

        // Auto-dismiss after 10s
        setTimeout(function () {
            dismissBanner(banner);
        }, 10000);
    }

    function dismissBanner(banner) {
        if (!banner.parentNode) return;
        banner.classList.add('dismissing');
        setTimeout(function () {
            if (banner.parentNode) banner.parentNode.removeChild(banner);
        }, 300);
    }

    function addInfoMessage(content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message info';
        const bubble = document.createElement('div');
        bubble.className = 'message-bubble';
        bubble.textContent = content;
        messageDiv.appendChild(bubble);
        messagesEl.appendChild(messageDiv);
        scrollToBottom();
    }

    // --- Health report rendering ---
    const LAYER_TITLES = {
        bare_metal: 'Layer 1 — Bare Metal',
        services: 'Layer 2 — Services & Processes',
        internals: 'Layer 3 — JARVIS Internals',
        data_stores: 'Layer 4 — Data Stores',
        self_assessment: 'Layer 5 — Self-Assessment',
    };

    function addHealthReport(data) {
        const wrapper = document.createElement('div');
        wrapper.className = 'health-report';

        // Corner brackets
        wrapper.innerHTML = '<div class="hud-corner hud-tl"></div><div class="hud-corner hud-tr"></div>'
            + '<div class="hud-corner hud-bl"></div><div class="hud-corner hud-br"></div>'
            + '<div class="hud-scanline"></div>';

        // Header
        const header = document.createElement('div');
        header.className = 'health-header';
        var ts = new Date();
        var timeStr = ts.toLocaleTimeString('en-US', { hour12: false }) + '.' + String(ts.getMilliseconds()).padStart(3, '0');
        header.innerHTML = '<div class="health-title">\u25c8 System Health Report</div>'
            + '<div class="health-timestamp">' + ts.toLocaleDateString() + ' &nbsp; ' + timeStr + '</div>';
        wrapper.appendChild(header);

        // Layers
        const layerOrder = ['bare_metal', 'services', 'internals', 'data_stores', 'self_assessment'];
        let totalGreen = 0, totalYellow = 0, totalRed = 0;

        layerOrder.forEach(function (key) {
            const checks = data[key];
            if (!checks || checks.length === 0) return;

            const section = document.createElement('div');
            section.className = 'health-layer';

            const layerHeader = document.createElement('div');
            layerHeader.className = 'health-layer-title';
            layerHeader.textContent = LAYER_TITLES[key] || key;
            section.appendChild(layerHeader);

            checks.forEach(function (check) {
                if (check.status === 'green') totalGreen++;
                else if (check.status === 'yellow') totalYellow++;
                else if (check.status === 'red') totalRed++;

                const row = document.createElement('div');
                row.className = 'health-check';

                const dot = document.createElement('span');
                dot.className = 'health-dot health-' + check.status;
                dot.textContent = check.status === 'red' ? '\u2716' : '\u25cf';

                const name = document.createElement('span');
                name.className = 'health-name';
                name.textContent = check.name;

                const summary = document.createElement('span');
                summary.className = 'health-summary';
                summary.textContent = check.summary;

                row.appendChild(dot);
                row.appendChild(name);
                row.appendChild(summary);
                section.appendChild(row);
            });

            wrapper.appendChild(section);
        });

        // Summary footer
        const footer = document.createElement('div');
        footer.className = 'health-footer';
        let summaryHTML = '<span class="health-count green">\u25cf ' + totalGreen + ' passed</span>';
        if (totalYellow > 0) {
            summaryHTML += '<span class="health-count yellow">\u25cf ' + totalYellow + ' warning' + (totalYellow !== 1 ? 's' : '') + '</span>';
        }
        if (totalRed > 0) {
            summaryHTML += '<span class="health-count red">\u2716 ' + totalRed + ' critical</span>';
        }
        footer.innerHTML = summaryHTML;
        wrapper.appendChild(footer);

        messagesEl.appendChild(wrapper);
        scrollToBottom();
    }

    // --- Thinking indicator ---
    function showThinking() {
        removeThinking();
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message jarvis thinking-msg';
        const sender = document.createElement('div');
        sender.className = 'message-sender';
        sender.textContent = 'J.A.R.V.I.S.';
        const bubble = document.createElement('div');
        bubble.className = 'message-bubble';
        bubble.innerHTML = '<div class="thinking"><span></span><span></span><span></span></div>';
        messageDiv.appendChild(sender);
        messageDiv.appendChild(bubble);
        messagesEl.appendChild(messageDiv);
        scrollToBottom();
    }

    function removeThinking() {
        const el = messagesEl.querySelector('.thinking-msg');
        if (el) el.remove();
    }

    // --- Document buffer indicator ---
    function updateDocIndicator(data) {
        if (data.active) {
            docInfo.textContent = `Document loaded: ~${data.tokens} tokens (${data.source})`;
            docIndicator.classList.remove('hidden');
        } else {
            docIndicator.classList.add('hidden');
        }
    }

    // --- Processing state ---
    function setProcessing(state) {
        processing = state;
        btnSend.disabled = state;
        userInput.disabled = state;
        if (!state) {
            userInput.focus();
        }
    }

    // --- Send message ---
    function sendMessage() {
        const text = userInput.value.trim();
        if (!text || processing || !ws || ws.readyState !== WebSocket.OPEN) return;

        // Record in command history (avoid duplicating last entry)
        if (commandHistory.length === 0 || commandHistory[commandHistory.length - 1] !== text) {
            commandHistory.push(text);
        }
        historyIndex = -1;
        pendingInput = '';

        addMessage('user', text);
        ws.send(JSON.stringify({ type: 'message', content: text }));
        userInput.value = '';
        userInput.style.height = 'auto';
        setProcessing(true);
        showThinking();
    }

    // --- Auto-resize textarea ---
    userInput.addEventListener('input', function () {
        this.style.height = 'auto';
        this.style.height = Math.min(this.scrollHeight, 200) + 'px';
    });

    // --- Keyboard: Enter to send, Shift+Enter for newline, Up/Down for history ---
    userInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        } else if (e.key === 'ArrowUp' && commandHistory.length > 0) {
            // Only navigate history when cursor is at position 0 (or input is single-line)
            if (userInput.selectionStart === 0 || !userInput.value.includes('\n')) {
                e.preventDefault();
                if (historyIndex === -1) {
                    pendingInput = userInput.value;
                    historyIndex = commandHistory.length - 1;
                } else if (historyIndex > 0) {
                    historyIndex--;
                }
                userInput.value = commandHistory[historyIndex];
                userInput.style.height = 'auto';
                userInput.style.height = Math.min(userInput.scrollHeight, 200) + 'px';
            }
        } else if (e.key === 'ArrowDown' && historyIndex !== -1) {
            e.preventDefault();
            if (historyIndex < commandHistory.length - 1) {
                historyIndex++;
                userInput.value = commandHistory[historyIndex];
            } else {
                historyIndex = -1;
                userInput.value = pendingInput;
            }
            userInput.style.height = 'auto';
            userInput.style.height = Math.min(userInput.scrollHeight, 200) + 'px';
        }
    });

    // --- Button handlers ---
    btnSend.addEventListener('click', sendMessage);

    btnPaste.addEventListener('click', function () {
        pasteTextarea.value = '';
        pasteModal.classList.remove('hidden');
        pasteTextarea.focus();
    });

    btnClear.addEventListener('click', function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'slash_command', command: '/clear' }));
        }
    });

    btnHelp.addEventListener('click', function () {
        helpModal.classList.remove('hidden');
    });

    btnRestart.addEventListener('click', function () {
        if (!confirm('Restart JARVIS server?')) return;
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'restart' }));
        }
        addInfoMessage('Server restarting — reconnecting...');
        setStatus('connecting');
    });

    // --- Paste modal ---
    pasteSubmit.addEventListener('click', function () {
        const text = pasteTextarea.value.trim();
        if (!text) return;
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'slash_command', command: '/paste', content: text }));
        }
        pasteModal.classList.add('hidden');
    });

    pasteCancel.addEventListener('click', function () {
        pasteModal.classList.add('hidden');
    });

    pasteModalClose.addEventListener('click', function () {
        pasteModal.classList.add('hidden');
    });

    // --- Help modal ---
    helpModalClose.addEventListener('click', function () {
        helpModal.classList.add('hidden');
    });

    helpClose.addEventListener('click', function () {
        helpModal.classList.add('hidden');
    });

    // --- New toolbar button handlers ---
    btnAppend.addEventListener('click', function () {
        appendTextarea.value = '';
        appendModal.classList.remove('hidden');
        appendTextarea.focus();
    });

    btnFile.addEventListener('click', function () {
        filePathInput.value = '';
        fileBrowser.classList.add('hidden');
        browserVisible = false;
        btnBrowseToggle.textContent = 'Browse';
        fileModal.classList.remove('hidden');
        filePathInput.focus();
    });

    btnClipboard.addEventListener('click', function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'slash_command', command: '/clipboard' }));
        }
    });

    btnContext.addEventListener('click', function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'slash_command', command: '/context' }));
        }
    });

    // --- Append modal ---
    appendSubmit.addEventListener('click', function () {
        var text = appendTextarea.value.trim();
        if (!text) return;
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'slash_command', command: '/append', content: text }));
        }
        appendModal.classList.add('hidden');
    });

    appendCancel.addEventListener('click', function () {
        appendModal.classList.add('hidden');
    });

    appendModalClose.addEventListener('click', function () {
        appendModal.classList.add('hidden');
    });

    // --- File path modal ---
    fileSubmit.addEventListener('click', function () {
        var path = filePathInput.value.trim();
        if (!path) return;
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'slash_command', command: '/file', path: path }));
        }
        fileModal.classList.add('hidden');
    });

    fileCancel.addEventListener('click', function () {
        fileModal.classList.add('hidden');
    });

    fileModalClose.addEventListener('click', function () {
        fileModal.classList.add('hidden');
    });

    filePathInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            fileSubmit.click();
        }
    });

    // --- File browser ---
    let browserPath = '/home/user';
    let browserVisible = false;

    btnBrowseToggle.addEventListener('click', function () {
        browserVisible = !browserVisible;
        if (browserVisible) {
            fileBrowser.classList.remove('hidden');
            btnBrowseToggle.textContent = 'Hide';
            // Use current input value as starting directory if possible
            var currentVal = filePathInput.value.trim();
            if (currentVal) {
                var dir = currentVal.endsWith('/') ? currentVal :
                    currentVal.substring(0, currentVal.lastIndexOf('/') + 1);
                if (dir) browserPath = dir;
            }
            browseTo(browserPath);
        } else {
            fileBrowser.classList.add('hidden');
            btnBrowseToggle.textContent = 'Browse';
        }
    });

    function browseTo(path) {
        browserCurrentPath.textContent = path;
        browserList.innerHTML = '<div class="file-browser-loading">Loading...</div>';

        fetch('/api/browse?path=' + encodeURIComponent(path))
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data.error) {
                    browserList.innerHTML = '<div class="file-browser-loading">' +
                        escapeHtml(data.error) + '</div>';
                    return;
                }

                browserPath = data.path;
                browserCurrentPath.textContent = data.path;
                browserList.innerHTML = '';

                // Parent directory entry
                if (data.parent) {
                    var parentEntry = createFileEntry('..', 'dir', null, false);
                    parentEntry.addEventListener('click', function () {
                        browseTo(data.parent);
                    });
                    browserList.appendChild(parentEntry);
                }

                // Directory and file entries
                data.entries.forEach(function (entry) {
                    var el = createFileEntry(
                        entry.name,
                        entry.type,
                        entry.size,
                        entry.binary || false
                    );

                    if (entry.type === 'dir') {
                        el.addEventListener('click', function () {
                            browseTo(data.path + '/' + entry.name);
                        });
                    } else if (!entry.binary) {
                        el.addEventListener('click', function () {
                            filePathInput.value = data.path + '/' + entry.name;
                            fileBrowser.classList.add('hidden');
                            browserVisible = false;
                            btnBrowseToggle.textContent = 'Browse';
                        });
                    }

                    browserList.appendChild(el);
                });
            })
            .catch(function () {
                browserList.innerHTML = '<div class="file-browser-loading">Error loading directory</div>';
            });
    }

    function createFileEntry(name, type, size, binary) {
        var el = document.createElement('div');
        el.className = 'file-entry' + (type === 'dir' ? ' dir' : '') + (binary ? ' disabled' : '');

        var icon = document.createElement('span');
        icon.className = 'file-entry-icon ' + type;
        icon.textContent = type === 'dir' ? '\uD83D\uDCC1' : '\uD83D\uDCC4';

        var nameEl = document.createElement('span');
        nameEl.className = 'file-entry-name';
        nameEl.textContent = name;

        el.appendChild(icon);
        el.appendChild(nameEl);

        if (type === 'file' && size != null) {
            var sizeEl = document.createElement('span');
            sizeEl.className = 'file-entry-size';
            sizeEl.textContent = formatFileSize(size);
            el.appendChild(sizeEl);
        }

        return el;
    }

    function formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    // --- Drag/drop file handling ---
    var BINARY_EXTS = new Set([
        '.exe', '.bin', '.so', '.dll', '.dylib', '.o', '.a',
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tiff',
        '.mp3', '.mp4', '.wav', '.flac', '.ogg', '.avi', '.mkv', '.mov', '.webm',
        '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar', '.zst',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.gguf', '.npy', '.npz', '.pt', '.pth', '.onnx', '.safetensors',
        '.db', '.sqlite', '.sqlite3',
        '.pyc', '.class', '.wasm',
    ]);

    function getFileExt(name) {
        var idx = name.lastIndexOf('.');
        return idx >= 0 ? name.slice(idx).toLowerCase() : '';
    }

    chatArea.addEventListener('dragover', function (e) {
        e.preventDefault();
        e.stopPropagation();
        chatArea.classList.add('dragover');
    });

    chatArea.addEventListener('dragleave', function (e) {
        e.preventDefault();
        e.stopPropagation();
        chatArea.classList.remove('dragover');
    });

    chatArea.addEventListener('drop', function (e) {
        e.preventDefault();
        e.stopPropagation();
        chatArea.classList.remove('dragover');

        var files = e.dataTransfer.files;
        if (!files || files.length === 0) return;

        var file = files[0]; // Handle first file only
        var ext = getFileExt(file.name);

        if (BINARY_EXTS.has(ext)) {
            addInfoMessage('Cannot load binary file (' + ext + '): ' + file.name);
            return;
        }

        if (file.size > 500000) {
            // Large file — upload via POST
            var formData = new FormData();
            formData.append('file', file);
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/upload');
            xhr.onload = function () {
                if (xhr.status === 200) {
                    var resp = JSON.parse(xhr.responseText);
                    updateDocIndicator(resp);
                    addInfoMessage('Document loaded: ~' + resp.tokens + ' tokens, ' + resp.lines + ' lines (file:' + file.name + ')');
                } else {
                    var err = JSON.parse(xhr.responseText);
                    addInfoMessage('Upload failed: ' + (err.error || 'unknown error'));
                }
            };
            xhr.onerror = function () {
                addInfoMessage('Upload failed: network error');
            };
            xhr.send(formData);
        } else {
            // Small file — read client-side, send via WebSocket
            var reader = new FileReader();
            reader.onload = function () {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({
                        type: 'file_drop',
                        filename: file.name,
                        content: reader.result,
                    }));
                }
            };
            reader.readAsText(file);
        }
    });

    // Close modals on backdrop click
    [pasteModal, helpModal, appendModal, fileModal].forEach(function (modal) {
        modal.addEventListener('click', function (e) {
            if (e.target === modal) modal.classList.add('hidden');
        });
    });

    // Close modals on Escape, Ctrl+L to clear display
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            pasteModal.classList.add('hidden');
            helpModal.classList.add('hidden');
            appendModal.classList.add('hidden');
            fileModal.classList.add('hidden');
            if (browserVisible) {
                fileBrowser.classList.add('hidden');
                browserVisible = false;
                btnBrowseToggle.textContent = 'Browse';
            }
            if (sidebarOpen) {
                toggleSidebar();
            }
        } else if (e.key === 'l' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            messagesEl.innerHTML = '';
        }
    });

    // --- Document buffer clear button ---
    docClearBtn.addEventListener('click', function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'slash_command', command: '/clear' }));
        }
    });

    // --- Voice toggle ---
    voiceToggle.addEventListener('change', function () {
        const enabled = voiceToggle.checked;
        localStorage.setItem('jarvis-voice', enabled);
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'toggle_voice', enabled: enabled }));
        }
    });

    // --- History loading ---
    function loadHistory(messages, sessionId) {
        if (!messages || messages.length === 0) return;

        if (sessionId) {
            activeSessionId = sessionId;
            highlightActiveSession();
        }

        // Build a document fragment for performance
        var frag = document.createDocumentFragment();

        messages.forEach(function (msg) {
            var role = msg.role === 'user' ? 'user' : 'jarvis';
            var messageDiv = document.createElement('div');
            messageDiv.className = 'message ' + role + ' history-msg';

            var sender = document.createElement('div');
            sender.className = 'message-sender';
            sender.textContent = role === 'user' ? 'YOU' : 'J.A.R.V.I.S.';
            messageDiv.appendChild(sender);

            var bubble = document.createElement('div');
            bubble.className = 'message-bubble';
            if (role === 'jarvis') {
                bubble.innerHTML = renderMarkdown(msg.content || '');
            } else {
                bubble.textContent = msg.content || '';
            }
            messageDiv.appendChild(bubble);

            if (msg.timestamp) {
                var ts = document.createElement('div');
                ts.className = 'message-timestamp';
                ts.textContent = formatTimestamp(msg.timestamp);
                messageDiv.appendChild(ts);
            }

            frag.appendChild(messageDiv);
        });

        // Insert before any existing live messages
        if (messagesEl.firstChild) {
            messagesEl.insertBefore(frag, messagesEl.firstChild);
        } else {
            messagesEl.appendChild(frag);
        }

        scrollToBottom();
    }

    function formatTimestamp(ts) {
        var d = new Date(ts * 1000);
        var now = new Date();
        var diff = now - d;
        var dayMs = 86400000;

        if (diff < dayMs && d.getDate() === now.getDate()) {
            return 'Today ' + d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
        } else if (diff < 2 * dayMs) {
            return 'Yesterday ' + d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
        } else if (diff < 7 * dayMs) {
            return d.toLocaleDateString('en-US', { weekday: 'long' }) + ' ' +
                d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
        }
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' +
            d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
    }

    // --- Markdown rendering (no library) ---
    function escapeHtml(text) {
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(text));
        return div.innerHTML;
    }

    function renderMarkdown(text) {
        // Escape HTML first to prevent XSS
        var html = escapeHtml(text);

        // Fenced code blocks: ```lang\n...\n```
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function (match, lang, code) {
            var id = 'code-' + Math.random().toString(36).substring(2, 10);
            return '<div class="code-block">' +
                (lang ? '<span class="code-lang">' + lang + '</span>' : '') +
                '<button class="code-copy" data-target="' + id + '" title="Copy code">Copy</button>' +
                '<pre><code id="' + id + '">' + code.replace(/\n$/, '') + '</code></pre>' +
                '</div>';
        });

        // Inline code: `code`
        html = html.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');

        // Bold: **text**
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

        // Italic: *text* (but not inside ** which we already handled)
        html = html.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');

        // URLs: https://... or http://...
        html = html.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');

        return html;
    }

    // Copy button handler (delegated)
    messagesEl.addEventListener('click', function (e) {
        if (e.target.classList.contains('code-copy')) {
            var targetId = e.target.getAttribute('data-target');
            var codeEl = document.getElementById(targetId);
            if (codeEl) {
                navigator.clipboard.writeText(codeEl.textContent).then(function () {
                    e.target.textContent = 'Copied!';
                    setTimeout(function () { e.target.textContent = 'Copy'; }, 1500);
                });
            }
        }
    });

    // --- Scroll ---
    function scrollToBottom() {
        requestAnimationFrame(function () {
            chatArea.scrollTop = chatArea.scrollHeight;
        });
    }

    // --- Session sidebar ---

    function toggleSidebar() {
        sidebarOpen = !sidebarOpen;
        sidebar.classList.toggle('open', sidebarOpen);
        btnHamburger.classList.toggle('active', sidebarOpen);
        document.body.classList.toggle('sidebar-open', sidebarOpen);

        // Mobile overlay
        if (sidebarOpen) {
            sidebarOverlay.classList.remove('hidden');
            // Slight delay for transition
            requestAnimationFrame(function () {
                sidebarOverlay.classList.add('visible');
            });
        } else {
            sidebarOverlay.classList.remove('visible');
            setTimeout(function () {
                sidebarOverlay.classList.add('hidden');
            }, 250);
        }

        // Load sessions on first open
        if (sidebarOpen && sessions.length === 0) {
            fetchSessions(0);
        }
    }

    function fetchSessions(offset) {
        fetch('/api/sessions?offset=' + offset + '&limit=10')
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data.sessions) {
                    if (offset === 0) {
                        sessions = data.sessions;
                    } else {
                        sessions = sessions.concat(data.sessions);
                    }
                    sessionsHasMore = data.has_more;
                    sessionOffset = offset + data.sessions.length;
                    renderSessionList();
                }
            })
            .catch(function () {
                sessionListEl.innerHTML = '<div style="padding:16px;color:var(--text-dim);font-size:0.82rem;">Failed to load sessions</div>';
            });
    }

    function renderSessionList() {
        sessionListEl.innerHTML = '';

        sessions.forEach(function (s, idx) {
            var entry = document.createElement('div');
            entry.className = 'session-entry';
            entry.setAttribute('data-session-id', s.id);
            if (s.id === activeSessionId) {
                entry.classList.add('active');
            }

            // Time row
            var timeEl = document.createElement('div');
            timeEl.className = 'session-entry-time';
            timeEl.textContent = formatTimestamp(s.start_ts);

            // Live badge for most recent session
            if (idx === 0) {
                var badge = document.createElement('span');
                badge.className = 'session-live-badge';
                badge.textContent = 'LIVE';
                timeEl.appendChild(badge);
            }
            entry.appendChild(timeEl);

            // Name/preview
            var nameEl = document.createElement('div');
            nameEl.className = 'session-entry-name';
            nameEl.textContent = s.custom_name || s.preview || '(empty session)';
            entry.appendChild(nameEl);

            // Meta (message count)
            var metaEl = document.createElement('div');
            metaEl.className = 'session-entry-meta';
            metaEl.textContent = s.message_count + ' message' + (s.message_count !== 1 ? 's' : '');
            entry.appendChild(metaEl);

            // Click to load session
            entry.addEventListener('click', function () {
                loadSessionById(s.id);
            });

            // Double-click to rename
            nameEl.addEventListener('dblclick', function (e) {
                e.stopPropagation();
                startRename(s.id, nameEl, s);
            });

            sessionListEl.appendChild(entry);
        });

        // View more button
        if (sessionsHasMore) {
            btnViewMore.classList.remove('hidden');
        } else {
            btnViewMore.classList.add('hidden');
        }
    }

    function highlightActiveSession() {
        var entries = sessionListEl.querySelectorAll('.session-entry');
        entries.forEach(function (el) {
            var id = el.getAttribute('data-session-id');
            el.classList.toggle('active', id === activeSessionId);
        });
    }

    function loadSessionById(sessionId) {
        activeSessionId = sessionId;
        highlightActiveSession();

        // Clear chat and show loading
        messagesEl.innerHTML = '';
        addInfoMessage('Loading session...');

        fetch('/api/session/' + encodeURIComponent(sessionId))
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                messagesEl.innerHTML = '';
                if (data.messages && data.messages.length > 0) {
                    var frag = document.createDocumentFragment();
                    data.messages.forEach(function (msg) {
                        var role = msg.role === 'user' ? 'user' : 'jarvis';
                        var messageDiv = document.createElement('div');
                        messageDiv.className = 'message ' + role;

                        var sender = document.createElement('div');
                        sender.className = 'message-sender';
                        sender.textContent = role === 'user' ? 'YOU' : 'J.A.R.V.I.S.';
                        messageDiv.appendChild(sender);

                        var bubble = document.createElement('div');
                        bubble.className = 'message-bubble';
                        if (role === 'jarvis') {
                            bubble.innerHTML = renderMarkdown(msg.content || '');
                        } else {
                            bubble.textContent = msg.content || '';
                        }
                        messageDiv.appendChild(bubble);

                        if (msg.timestamp) {
                            var ts = document.createElement('div');
                            ts.className = 'message-timestamp';
                            ts.textContent = formatTimestamp(msg.timestamp);
                            messageDiv.appendChild(ts);
                        }

                        frag.appendChild(messageDiv);
                    });
                    messagesEl.appendChild(frag);
                } else {
                    addInfoMessage('No messages in this session');
                }
                scrollToBottom();
            })
            .catch(function () {
                messagesEl.innerHTML = '';
                addInfoMessage('Failed to load session');
            });

        // Close sidebar on mobile after selection
        if (window.innerWidth <= 768 && sidebarOpen) {
            toggleSidebar();
        }
    }

    function startRename(sessionId, nameEl, session) {
        var currentName = session.custom_name || session.preview || '';
        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'session-rename-input';
        input.value = currentName;

        nameEl.textContent = '';
        nameEl.appendChild(input);
        input.focus();
        input.select();

        function save() {
            var newName = input.value.trim();
            if (newName && newName !== currentName) {
                session.custom_name = newName;
                fetch('/api/session/' + encodeURIComponent(sessionId) + '/rename', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: newName }),
                });
            }
            nameEl.textContent = newName || session.preview || '(empty session)';
        }

        function cancel() {
            nameEl.textContent = currentName || session.preview || '(empty session)';
        }

        input.addEventListener('blur', save);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                input.removeEventListener('blur', save);
                save();
            } else if (e.key === 'Escape') {
                e.preventDefault();
                input.removeEventListener('blur', save);
                cancel();
            }
        });
    }

    function handleSessionList(msg) {
        sessions = msg.sessions || [];
        sessionsHasMore = msg.has_more || false;
        sessionOffset = sessions.length;
        renderSessionList();
    }

    // Sidebar event handlers
    btnHamburger.addEventListener('click', toggleSidebar);
    sidebarClose.addEventListener('click', toggleSidebar);
    sidebarOverlay.addEventListener('click', toggleSidebar);

    btnViewMore.addEventListener('click', function () {
        fetchSessions(sessionOffset);
    });

    // --- Init ---
    connect();

})();

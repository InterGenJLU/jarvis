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
    const docIndicator = document.getElementById('doc-indicator');
    const docInfo = document.getElementById('doc-info');
    const docClearBtn = document.getElementById('doc-clear-btn');

    // Paste modal
    const pasteModal = document.getElementById('paste-modal');
    const pasteTextarea = document.getElementById('paste-textarea');
    const pasteSubmit = document.getElementById('paste-submit');
    const pasteCancel = document.getElementById('paste-cancel');
    const pasteModalClose = document.getElementById('paste-modal-close');

    // Help modal
    const helpModal = document.getElementById('help-modal');
    const helpModalClose = document.getElementById('help-modal-close');
    const helpClose = document.getElementById('help-close');

    // --- State ---
    let ws = null;
    let processing = false;
    let reconnectDelay = 1000;
    const MAX_RECONNECT_DELAY = 30000;

    // --- WebSocket ---
    function connect() {
        setStatus('connecting');
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${protocol}//${location.host}/ws`);

        ws.onopen = function () {
            setStatus('connected');
            reconnectDelay = 1000;
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
            setStatus('disconnected');
            ws = null;
            setTimeout(connect, reconnectDelay);
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
        sender.textContent = 'JARVIS';

        streamingBubble = document.createElement('div');
        streamingBubble.className = 'message-bubble';
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
        if (streamingBubble && fullResponse) {
            streamingBubble.textContent = fullResponse;
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
            sender.textContent = role === 'user' ? 'You' : 'JARVIS';
            messageDiv.appendChild(sender);
        }

        const bubble = document.createElement('div');
        bubble.className = 'message-bubble';
        bubble.textContent = content;
        messageDiv.appendChild(bubble);

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

    function addAnnouncement(content) {
        const div = document.createElement('div');
        div.className = 'announcement';
        div.textContent = content;
        messagesEl.appendChild(div);
        scrollToBottom();
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

    // --- Thinking indicator ---
    function showThinking() {
        removeThinking();
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message jarvis thinking-msg';
        const sender = document.createElement('div');
        sender.className = 'message-sender';
        sender.textContent = 'JARVIS';
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

    // --- Keyboard: Enter to send, Shift+Enter for newline ---
    userInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
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

    // Close modals on backdrop click
    [pasteModal, helpModal].forEach(function (modal) {
        modal.addEventListener('click', function (e) {
            if (e.target === modal) modal.classList.add('hidden');
        });
    });

    // Close modals on Escape
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            pasteModal.classList.add('hidden');
            helpModal.classList.add('hidden');
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

    // --- Scroll ---
    function scrollToBottom() {
        requestAnimationFrame(function () {
            chatArea.scrollTop = chatArea.scrollHeight;
        });
    }

    // --- Init ---
    connect();

})();

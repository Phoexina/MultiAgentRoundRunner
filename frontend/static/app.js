/**
 * app.js — Multi-agent Team Chat Frontend
 *
 * Architecture:
 *  - WSClient       : WebSocket connection + reconnect
 *  - ConversationSidebar : left-sidebar conversations + sessions
 *  - PipelinePanel  : right-panel pipeline step editor
 *  - ChatUI         : middle pane — chat view + session view mode
 *  - App            : wires everything together
 */

'use strict';

// ============================================================
// Utilities
// ============================================================

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function fmtTime(iso) {
    if (!iso) return '';
    try {
        let s = iso.trim();
        // SQLite CURRENT_TIMESTAMP produces 'YYYY-MM-DD HH:MM:SS' (UTC, no 'Z').
        // Append 'Z' so the browser interprets it as UTC and converts to local time.
        if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(s)) {
            s = s.replace(' ', 'T') + 'Z';
        }
        const d = new Date(s);
        return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    } catch { return ''; }
}

// ============================================================
// WSClient
// ============================================================

class WSClient {
    constructor(url, onMessage) {
        this.url = url;
        this.onMessage = onMessage;
        this.ws = null;
        this._reconnectDelay = 1000;
        this._connecting = false;
        this.onStatusChange = null;
        this.connect();
    }

    connect() {
        if (this._connecting) return;
        this._connecting = true;
        try {
            this.ws = new WebSocket(this.url);
        } catch (e) {
            this._scheduleReconnect();
            return;
        }

        this.ws.onopen = () => {
            this._connecting = false;
            this._reconnectDelay = 1000;
            this.onStatusChange && this.onStatusChange(true);
            // Request to restore previous conversation on reconnect
            if (this._lastConvId) {
                this.send({ type: 'select_conversation', id: this._lastConvId });
            }
        };

        this.ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                this.onMessage(msg);
            } catch (err) {
                console.error('WS parse error', err);
            }
        };

        this.ws.onclose = () => {
            this._connecting = false;
            this.onStatusChange && this.onStatusChange(false);
            this._scheduleReconnect();
        };

        this.ws.onerror = () => {
            this._connecting = false;
        };
    }

    send(obj) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(obj));
        }
    }

    setLastConvId(convId) {
        this._lastConvId = convId;
    }

    _scheduleReconnect() {
        setTimeout(() => this.connect(), this._reconnectDelay);
        this._reconnectDelay = Math.min(this._reconnectDelay * 1.5, 10000);
    }
}

// ============================================================
// ConversationSidebar
// ============================================================

class ConversationSidebar {
    constructor(ws, onSelectConversation, onSelectSession) {
        this.ws = ws;
        this.onSelectConversation = onSelectConversation;
        this.onSelectSession = onSelectSession;

        this.conversations = [];
        this.sessions = [];
        this.activeConvId = null;
        this.activeSessionId = null;
        this.currentPage = 0;
        this.totalPages = 1;

        // Live process status for running sessions: { session_id -> { pid, duration, idle } }
        this.sessionStatus = new Map();

        // Sidebar DOM
        this.$projectTitle = document.getElementById('project-title');
        this.$sessionList = document.getElementById('session-sidebar-list');
        this.$pagination = document.getElementById('session-pagination');
        this.$prevBtn = document.getElementById('session-page-prev');
        this.$nextBtn = document.getElementById('session-page-next');
        this.$pageInfo = document.getElementById('session-page-info');
        this.$switchBtn = document.getElementById('switch-conv-btn');
        this.$renameBtn = document.getElementById('rename-conv-btn');
        this.$newBtn = document.getElementById('new-conv-btn');

        // Modal DOM
        this.$modal = document.getElementById('conv-modal');
        this.$modalBackdrop = document.getElementById('conv-modal-backdrop');
        this.$modalClose = document.getElementById('conv-modal-close');
        this.$modalList = document.getElementById('conv-modal-list');
        this.$modalNewBtn = document.getElementById('conv-modal-new');

        this.$prevBtn.addEventListener('click', () => this._changePage(-1));
        this.$nextBtn.addEventListener('click', () => this._changePage(1));
        this.$switchBtn.addEventListener('click', () => this._openModal());
        this.$renameBtn.addEventListener('click', () => this._renameActive());
        this.$newBtn.addEventListener('click', () => this._createConversation());
        this.$modalClose.addEventListener('click', () => this._closeModal());
        this.$modalBackdrop.addEventListener('click', () => this._closeModal());
        this.$modalNewBtn.addEventListener('click', () => {
            this._closeModal();
            this._createConversation();
        });
    }

    setConversations(convs) {
        this.conversations = convs;
        this._renderModal();
    }

    addConversation(conv) {
        this.conversations.unshift(conv);
        this._renderModal();
    }

    updateConversation(id, name) {
        const c = this.conversations.find(c => c.id == id);
        if (c) {
            c.name = name;
            if (c.id == this.activeConvId) {
                this.$projectTitle.textContent = name;
            }
            this._renderModal();
        }
    }

    removeConversation(id) {
        this.conversations = this.conversations.filter(c => c.id != id);
        if (this.activeConvId == id) {
            this.activeConvId = null;
            this.$projectTitle.textContent = '未选择项目';
        }
        this._renderModal();
    }

    // Called when switching to a conversation — updates title, clears sessions
    setActive(convId, convName) {
        this.activeConvId = convId;
        this.activeSessionId = null;
        this.sessions = [];
        this.currentPage = 0;
        this.totalPages = 1;
        this.$pagination.style.display = 'none';
        this.$projectTitle.textContent = convName || '未选择项目';
        this._renderSessions();
        this._closeModal();
    }

    setSessions(sessions, page, totalPages) {
        this.sessions = sessions || [];
        if (page !== undefined) this.currentPage = page;
        if (totalPages !== undefined) this.totalPages = totalPages;
        this._renderSessions();
        this._renderPagination();
    }

    addSession(session) {
        if (session.conversation_id != this.activeConvId) return;
        if (this.currentPage !== 0) {
            // New session appeared while viewing an older page — jump to page 0
            this.ws.send({ type: 'get_sessions', conversation_id: this.activeConvId, page: 0 });
            return;
        }
        if (this.sessions.find(s => s.id === session.id)) return;
        this.sessions.unshift(session);
        this._renderSessions();
        this._renderPagination();
    }

    _changePage(delta) {
        const newPage = this.currentPage + delta;
        if (newPage < 0 || newPage >= this.totalPages) return;
        this.ws.send({ type: 'get_sessions', conversation_id: this.activeConvId, page: newPage });
    }

    _renderPagination() {
        if (this.totalPages <= 1) {
            this.$pagination.style.display = 'none';
            return;
        }
        this.$pagination.style.display = 'flex';
        this.$pageInfo.textContent = `${this.currentPage + 1} / ${this.totalPages}`;
        this.$prevBtn.disabled = this.currentPage === 0;
        this.$nextBtn.disabled = this.currentPage >= this.totalPages - 1;
    }

    updateSessionStatus(sessionId, status) {
        this.sessionStatus.set(sessionId, status);
        this._updateStatusBadge(sessionId);
    }

    clearSessionStatus(sessionId) {
        this.sessionStatus.delete(sessionId);
        this._updateStatusBadge(sessionId);
    }

    _updateStatusBadge(sessionId) {
        const el = this.$sessionList.querySelector(`[data-id="${sessionId}"]`);
        if (!el) return;
        const existing = el.querySelector('.session-status-badge');
        const isRunning = this.sessionStatus.has(sessionId);
        if (isRunning && !existing) {
            const badge = document.createElement('span');
            badge.className = 'session-status-badge running';
            badge.textContent = '运行中';
            el.appendChild(badge);
        } else if (!isRunning && existing) {
            existing.remove();
        }
    }

    _renderModal() {
        this.$modalList.innerHTML = '';
        if (!this.conversations.length) {
            this.$modalList.innerHTML = '<div style="color:#7f849c;font-size:12px;padding:12px 10px;">暂无项目</div>';
            return;
        }
        for (const conv of this.conversations) {
            const el = document.createElement('div');
            el.className = 'conv-modal-item' + (conv.id == this.activeConvId ? ' active' : '');
            el.dataset.id = conv.id;
            el.innerHTML = `
                <span class="conv-modal-item-dot"></span>
                <span class="conv-modal-item-name" title="${escapeHtml(conv.name)}">${escapeHtml(conv.name)}</span>
                <button class="conv-modal-item-del" title="删除">×</button>
            `;
            el.querySelector('.conv-modal-item-name').addEventListener('click', () => {
                this.onSelectConversation(conv.id, conv.name);
            });
            el.querySelector('.conv-modal-item-del').addEventListener('click', (e) => {
                e.stopPropagation();
                if (confirm(`删除项目「${conv.name}」及其所有数据？`)) {
                    this.ws.send({ type: 'delete_conversation', id: conv.id });
                }
            });
            this.$modalList.appendChild(el);
        }
    }

    _renderSessions() {
        this.$sessionList.innerHTML = '';
        if (!this.sessions.length) {
            this.$sessionList.innerHTML = '<div style="color:#7f849c;font-size:12px;padding:8px;">暂无记录</div>';
            return;
        }
        for (const s of this.sessions) {
            const el = document.createElement('div');
            el.className = 'session-sidebar-item' + (s.id == this.activeSessionId ? ' active' : '');
            el.dataset.id = s.id;
            const agentType = s.agent_type || s.agent || 'claude';
            const badgeClass = agentType === 'codex' ? 'badge-codex' : 'badge-claude';

            // Build label: "Round N-M" (step_index is 1-based)
            const roundIdx = s.round_index || s.round_number || 0;
            const stepIdx  = s.step_index || 0;
            const stepPart = (roundIdx > 0 && stepIdx > 0) ? `-${stepIdx}` : '';
            const label = `Round ${roundIdx}${stepPart}`;

            // Running badge: show if this session has live PID data
            const liveStatus = this.sessionStatus.get(s.id);
            const statusBadge = liveStatus ? '<span class="session-status-badge running">运行中</span>' : '';

            // Simplified sidebar: just agent badge + round label + running badge
            el.innerHTML = `
                <span class="session-agent-badge ${badgeClass}">${escapeHtml(agentType)}</span>
                <span class="session-sidebar-label">${label}</span>
                ${statusBadge}
            `;
            el.querySelector('.session-sidebar-label').addEventListener('click', () => {
                this.$sessionList.querySelectorAll('.session-sidebar-item.active')
                    .forEach(e => e.classList.remove('active'));
                el.classList.add('active');
                this.activeSessionId = s.id;
                this.onSelectSession(s);
            });
            this.$sessionList.appendChild(el);
        }
    }

    _showResumeModal(session) {
        // Create a simple modal for resume input
        const existingModal = document.getElementById('resume-modal');
        if (existingModal) existingModal.remove();

        const agentType = session.agent_type || session.agent || 'agent';
        const roundIdx  = session.round_index || session.round_number || '?';
        const stepIdx   = session.step_index || 0;
        const stepStr   = stepIdx > 0 ? `-${stepIdx}` : '';

        const modal = document.createElement('div');
        modal.id = 'resume-modal';
        modal.className = 'conv-modal';
        modal.innerHTML = `
            <div class="conv-modal-backdrop"></div>
            <div class="conv-modal-content">
                <div class="conv-modal-header">
                    <span>继续会话 - ${escapeHtml(agentType)} Round ${roundIdx}${stepStr}</span>
                    <button class="conv-modal-close-btn">×</button>
                </div>
                <div class="resume-modal-body">
                    <p style="color:#a6adc8;font-size:12px;margin-bottom:10px;">
                        输入新的提示词继续运行此会话：
                    </p>
                    <textarea id="resume-input" placeholder="输入提示词，如：继续" style="width:100%;height:80px;padding:8px;border:1px solid #313244;border-radius:6px;background:#181825;color:#cdd6f4;font-size:13px;resize:vertical;"></textarea>
                </div>
                <div class="conv-modal-footer">
                    <button id="resume-confirm-btn">确认继续</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        // Default input "继续"
        const inputEl = modal.querySelector('#resume-input');
        inputEl.value = '继续';

        // Close handlers
        modal.querySelector('.conv-modal-backdrop').addEventListener('click', () => modal.remove());
        modal.querySelector('.conv-modal-close-btn').addEventListener('click', () => modal.remove());

        // Confirm handler
        modal.querySelector('#resume-confirm-btn').addEventListener('click', () => {
            const userInput = inputEl.value.trim();
            this.ws.send({
                type: 'resume_session',
                session_id: session.id,
                user_input: userInput || null
            });
            modal.remove();
        });

        // Focus input
        inputEl.focus();
    }

    _openModal() {
        this._renderModal();
        this.$modal.classList.remove('hidden');
    }

    _closeModal() {
        this.$modal.classList.add('hidden');
    }

    _createConversation() {
        const name = prompt('新项目名称', '新项目 ' + (this.conversations.length + 1));
        if (name !== null && name.trim()) {
            this.ws.send({ type: 'create_conversation', name: name.trim() });
        }
    }

    _renameActive() {
        if (!this.activeConvId) return;
        const conv = this.conversations.find(c => c.id == this.activeConvId);
        const currentName = conv ? conv.name : '';
        const newName = prompt('项目名称', currentName);
        if (newName !== null && newName.trim() && newName.trim() !== currentName) {
            this.ws.send({ type: 'rename_conversation', id: this.activeConvId, name: newName.trim() });
        }
    }
}

// ============================================================
// PipelinePanel
// ============================================================

class PipelinePanel {
    constructor(ws) {
        this.ws = ws;
        this.currentConvId = null;
        this.workingDir = '';
        this.steps = [];
        this.defaultTemplate = '';

        this.$header = document.getElementById('pipeline-conv-name');
        this.$workdirSection = document.getElementById('pipeline-workdir');
        this.$workdirInput = document.getElementById('pipeline-workdir-input');
        this.$list = document.getElementById('pipeline-steps-list');
        this.$actions = document.getElementById('pipeline-actions');
        this.$noConv = document.getElementById('pipeline-no-conv');
        this.$addBtn = document.getElementById('add-step-btn');
        this.$saveBtn = document.getElementById('save-pipeline-btn');

        this.$workdirInput.addEventListener('input', (e) => {
            this.workingDir = e.target.value;
        });
        this.$addBtn.addEventListener('click', () => this._addStep());
        this.$saveBtn.addEventListener('click', () => this._save());
    }

    setDefaultTemplate(template) {
        this.defaultTemplate = template || '';
    }

    load(convId, convName, steps, workingDir) {
        this.currentConvId = convId;
        this.workingDir = workingDir || '';
        // Pre-fill default templates if steps have empty/null templates
        this.steps = steps.length ? steps.map(s => ({
            agent_type: s.agent_type,
            prompt_template: (s.prompt_template && s.prompt_template.trim())
                ? s.prompt_template
                : this.defaultTemplate,
        })) : [
            { agent_type: 'codex',  prompt_template: this.defaultTemplate },
            { agent_type: 'claude', prompt_template: this.defaultTemplate },
        ];
        this.$header.textContent = convName;
        this.$workdirSection.style.display = 'block';
        this.$workdirInput.value = this.workingDir;
        this.$noConv.style.display = 'none';
        this.$actions.style.display = 'flex';
        this._render();
        this.$saveBtn.textContent = '保存流程';
        this.$saveBtn.classList.remove('saved');
    }

    _addStep() {
        const lastAgent = this.steps.length > 0 ? this.steps[this.steps.length - 1].agent_type : 'claude';
        const newAgent = lastAgent === 'codex' ? 'claude' : 'codex';
        this.steps.push({ agent_type: newAgent, prompt_template: this.defaultTemplate });
        this._render();
    }

    _removeStep(idx) {
        this.steps.splice(idx, 1);
        this._render();
    }

    _render() {
        this.$list.innerHTML = '';
        if (!this.currentConvId) {
            this.$noConv.style.display = 'block';
            return;
        }
        this.steps.forEach((step, i) => {
            const el = document.createElement('div');
            el.className = 'pipeline-step-row';
            const hasCustom = !!(step.prompt_template && step.prompt_template.trim());
            el.innerHTML = `
                <span class="step-num">${i + 1}</span>
                <select class="step-agent-select">
                    <option value="codex"  ${step.agent_type === 'codex'  ? 'selected' : ''}>Codex</option>
                    <option value="claude" ${step.agent_type === 'claude' ? 'selected' : ''}>Claude</option>
                </select>
                <button class="step-prompt-btn ${hasCustom ? 'has-custom' : ''}" title="${hasCustom ? '已自定义提示词' : '使用默认模板'}">提示词</button>
                <button class="step-del-btn" title="删除步骤">×</button>
            `;
            el.querySelector('.step-agent-select').addEventListener('change', (e) => {
                this.steps[i].agent_type = e.target.value;
            });
            el.querySelector('.step-prompt-btn').addEventListener('click', () => {
                this._openPromptModal(i);
            });
            el.querySelector('.step-del-btn').addEventListener('click', () => this._removeStep(i));
            this.$list.appendChild(el);
        });
    }

    _openPromptModal(idx) {
        const existing = document.getElementById('prompt-edit-modal');
        if (existing) existing.remove();

        const step = this.steps[idx];
        const value = step.prompt_template || '';

        const modal = document.createElement('div');
        modal.id = 'prompt-edit-modal';
        modal.className = 'conv-modal';
        modal.innerHTML = `
            <div class="conv-modal-backdrop"></div>
            <div class="conv-modal-content prompt-edit-modal-content">
                <div class="conv-modal-header">
                    <span>步骤 ${idx + 1} · ${step.agent_type} · 提示词模板</span>
                    <button class="prompt-modal-close">×</button>
                </div>
                <div class="prompt-edit-body">
                    <div class="prompt-vars-hint">可用变量：{round_number} {chat_history} {user_input} {last_summary}</div>
                    <textarea class="prompt-edit-textarea" placeholder="留空使用默认模板">${escapeHtml(value)}</textarea>
                </div>
                <div class="conv-modal-footer prompt-edit-footer">
                    <button class="prompt-clear-btn">清空（用默认）</button>
                    <button class="prompt-confirm-btn">确认</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        const textarea = modal.querySelector('.prompt-edit-textarea');
        textarea.focus();
        textarea.setSelectionRange(textarea.value.length, textarea.value.length);

        const close = () => modal.remove();
        modal.querySelector('.conv-modal-backdrop').addEventListener('click', close);
        modal.querySelector('.prompt-modal-close').addEventListener('click', close);
        modal.querySelector('.prompt-clear-btn').addEventListener('click', () => {
            this.steps[idx].prompt_template = '';
            this._render();
            close();
        });
        modal.querySelector('.prompt-confirm-btn').addEventListener('click', () => {
            this.steps[idx].prompt_template = textarea.value;
            this._render();
            close();
        });
    }

    _save() {
        if (!this.currentConvId) return;
        const steps = this.steps.map(s => ({
            agent_type: s.agent_type,
            prompt_template: s.prompt_template || null,
        }));
        this.ws.send({
            type: 'save_pipeline',
            conversation_id: this.currentConvId,
            working_dir: this.workingDir || null,
            steps
        });
        this.$saveBtn.textContent = '已保存 ✓';
        this.$saveBtn.classList.add('saved');
        setTimeout(() => {
            this.$saveBtn.textContent = '保存流程';
            this.$saveBtn.classList.remove('saved');
        }, 2000);
    }
}

// ============================================================
// Session Log Viewer (used by ChatUI for session view mode)
// ============================================================

function parseLogEntry(log) {
    const stream = log.stream;
    const content = log.content;
    const evType = log.event_type;

    if (stream === 'stderr') {
        if (!content.trim()) return null;
        return { kind: 'error', text: content };
    }

    if (stream === 'event') {
        let ev;
        try { ev = JSON.parse(content); } catch { return null; }
        if (!ev || typeof ev !== 'object') return null;

        const t = ev.type;

        // session_init
        if (evType === 'session_init' || t === 'system') {
            const sid = ev.session_id || (ev.data && ev.data.session_id) || '';
            return { kind: 'session', text: `会话初始化${sid ? ' · ' + sid.slice(0, 12) + '…' : ''}` };
        }

        // done
        if (evType === 'done' || t === 'done') {
            const summary = ev.summary || '';
            return { kind: 'done', text: `完成${summary ? ' · ' + summary.slice(0, 80) : ''}` };
        }

        // error / timeout
        if (evType === 'error' || evType === 'timeout') {
            return { kind: 'error', text: content };
        }

        // Claude: assistant message with text blocks
        if (t === 'assistant' || t === 'message') {
            const msg = ev.message || ev;
            const blocks = msg.content || [];
            const parts = [];
            for (const b of blocks) {
                if (b.type === 'text' && b.text) parts.push(b.text);
                else if (b.type === 'tool_use') {
                    return { kind: 'tool_use', name: b.name || 'tool', input: b.input };
                }
            }
            if (parts.length) return { kind: 'text', text: parts.join('\n') };
            return null;
        }

        // Claude: stream_event text_delta
        if (t === 'stream_event') {
            const delta = (ev.event && ev.event.delta) || {};
            if (delta.type === 'text_delta' && delta.text) {
                return { kind: 'text', text: delta.text };
            }
            return null;
        }

        // Claude: tool_result
        if (t === 'tool_result') {
            const out = ev.output || ev.content || '';
            return { kind: 'tool_result', text: typeof out === 'string' ? out : JSON.stringify(out) };
        }

        // Codex: item.started / item.completed
        if (t === 'item.started' || t === 'item.completed') {
            const item = ev.item || {};

            // agent_message → text
            if (item.type === 'agent_message') {
                return { kind: 'text', text: item.text || '' };
            }

            // message (alternative format) → text
            if (item.type === 'message') {
                const parts = [];
                for (const p of (item.content || [])) {
                    if (p.type === 'output_text' && p.text) parts.push(p.text);
                }
                if (parts.length) return { kind: 'text', text: parts.join('\n') };
            }

            // mcp_tool_call
            if (item.type === 'mcp_tool_call') {
                const server = item.server || 'unknown';
                const tool = item.tool || 'unknown';
                const toolLabel = `mcp:${server}/${tool}`;
                if (t === 'item.started') {
                    return { kind: 'tool_use', name: toolLabel, input: item.arguments || {} };
                }
                // item.completed: extract text parts from result.content[]
                const resultContent = (item.result && Array.isArray(item.result.content))
                    ? item.result.content : [];
                const textParts = resultContent
                    .filter(c => c.type === 'text' && c.text)
                    .map(c => c.text);
                const status = item.status || 'completed';
                return {
                    kind: 'tool_result',
                    text: `${toolLabel} (${status})${textParts.length ? '\n' + textParts.join('\n') : ''}`
                };
            }

            // command_execution — use aggregated_output (not output)
            if (item.type === 'command_execution') {
                if (t === 'item.started') {
                    return { kind: 'tool_use', name: 'command', input: { command: item.command || '' } };
                }
                const sections = [];
                if (item.command) sections.push(`command: ${item.command}`);
                sections.push(`status: ${item.status || 'completed'}`);
                if (item.exit_code != null) sections.push(`exit_code: ${item.exit_code}`);
                const out = (item.aggregated_output || '').trimEnd();
                if (out) sections.push(out);
                return { kind: 'tool_result', text: sections.join('\n') };
            }

            // file_change — only on item.completed
            if (item.type === 'file_change' && t === 'item.completed') {
                const changes = Array.isArray(item.changes) ? item.changes : [];
                return {
                    kind: 'tool_use',
                    name: 'file_change',
                    input: { status: item.status || 'completed', files: changes }
                };
            }

            return null;
        }

        return null;
    }

    if (stream === 'stdout' && content.trim()) {
        // Skip raw JSON lines
        if (content.trim().startsWith('{')) return null;
        return { kind: 'text', text: content };
    }

    return null;
}

function renderLogEntries(logs) {
    const items = [];
    for (const log of logs) {
        // In the history view, skip individual text_delta stream_events — the
        // complete text is already captured in the final assistant event, so
        // showing every delta would create hundreds of tiny redundant bubbles.
        if (log.event_type === 'stream_event') continue;

        const entry = parseLogEntry(log);
        if (!entry) continue;

        const div = document.createElement('div');
        div.className = 'conv-bubble';

        switch (entry.kind) {
            case 'text':
                div.classList.add('text-bubble');
                div.innerHTML = `<div class="bubble-body">${escapeHtml(entry.text)}</div>`;
                break;
            case 'tool_use':
                div.classList.add('tool-bubble');
                div.innerHTML = `<div class="bubble-header"><span>工具调用: ${escapeHtml(entry.name || '')}</span></div>
                    <div class="bubble-body">${escapeHtml(JSON.stringify(entry.input || {}, null, 2))}</div>`;
                break;
            case 'tool_result':
                div.classList.add('tool-result-bubble');
                div.innerHTML = `<div class="bubble-header"><span>工具结果</span></div>
                    <div class="bubble-body">${escapeHtml(String(entry.text || '').slice(0, 1000))}</div>`;
                break;
            case 'done':
                div.classList.add('done-bubble');
                div.innerHTML = `<div class="bubble-body">${escapeHtml(entry.text)}</div>`;
                break;
            case 'error':
                div.classList.add('error-bubble');
                div.innerHTML = `<div class="bubble-body">${escapeHtml(entry.text)}</div>`;
                break;
            case 'session':
                div.classList.add('session-bubble');
                div.innerHTML = `<div class="bubble-body">${escapeHtml(entry.text)}</div>`;
                break;
        }
        if (div.innerHTML) items.push(div);
    }
    return items;
}

// ============================================================
// Structured message renderer (used by _openSessionView with /messages endpoint)
// ============================================================

/**
 * Render a list of structured messages from the /api/sessions/{id}/messages endpoint.
 * Each message: { role, type, content, metadata? }
 */
function renderStructuredMessages(messages) {
    const items = [];
    for (const msg of messages) {
        const div = document.createElement('div');
        div.className = 'conv-bubble';

        const role = msg.role || 'assistant';
        const mtype = msg.type || 'text';
        const content = msg.content || '';
        const meta = msg.metadata || {};

        switch (mtype) {
            case 'text':
                div.classList.add(role === 'user' ? 'user-bubble' : 'text-bubble');
                div.innerHTML = `<div class="bubble-role">${escapeHtml(role)}</div><div class="bubble-body">${escapeHtml(content)}</div>`;
                break;

            case 'thinking':
                div.classList.add('thinking-bubble');
                div.innerHTML = `<div class="bubble-header"><span>思考过程</span></div>
                    <div class="bubble-body">${escapeHtml(content.slice(0, 500))}${content.length > 500 ? '…' : ''}</div>`;
                break;

            case 'tool_use': {
                const inputStr = meta.tool_input
                    ? JSON.stringify(meta.tool_input, null, 2)
                    : '';
                div.classList.add('tool-bubble');
                div.innerHTML = `<div class="bubble-header"><span>工具调用: ${escapeHtml(meta.tool_name || content)}</span></div>
                    <div class="bubble-body">${escapeHtml(inputStr.slice(0, 2000))}${inputStr.length > 2000 ? '\n…' : ''}</div>`;
                break;
            }

            case 'tool_result':
                div.classList.add('tool-result-bubble');
                div.innerHTML = `<div class="bubble-header"><span>工具结果</span></div>
                    <div class="bubble-body">${escapeHtml(String(content).slice(0, 1000))}${content.length > 1000 ? '\n…' : ''}</div>`;
                break;
        }

        if (div.innerHTML) items.push(div);
    }
    return items;
}

// ============================================================
// ChatUI
// ============================================================

class ChatUI {
    constructor(ws) {
        this.ws = ws;
        this.currentConvId = null;
        this.inSessionView = false;

        this.$main = document.getElementById('chat-main');
        this.$title = document.getElementById('chat-title');
        this.$messages = document.getElementById('messages');
        this.$sessionView = document.getElementById('session-view');
        this.$sessionViewHeader = document.getElementById('session-view-header');
        this.$sessionViewTitle = document.getElementById('session-view-title');
        this.$backBtn = document.getElementById('back-btn');
        this.$interventionBanner = document.getElementById('intervention-banner');
        this.$countdown = document.getElementById('countdown');
        this.$interventionMessage = document.getElementById('intervention-message');
        this.$skipBtn = document.getElementById('skip-btn');
        this.$input = document.getElementById('message-input');
        this.$sendBtn = document.getElementById('send-btn');
        this.$startBtn = document.getElementById('start-btn');
        this.$retryBtn = document.getElementById('retry-btn');
        this.$stopBtn = document.getElementById('stop-btn');
        this.$stopAfterBtn = document.getElementById('stop-after-btn');
        this.$status = document.getElementById('status');
        this.$roundCounter = document.getElementById('round-counter');
        this.$connStatus = document.getElementById('connection-status');

        this._countdownTimer = null;
        this.viewingSessionId = null;
        this._currentSessionData = null;
        this.hasHistory = false;
        this.currentState = 'idle';

        this.$backBtn.addEventListener('click', () => this.showChatView());
        this.$skipBtn.addEventListener('click', () => ws.send({ type: 'control', action: 'skip_intervention' }));
        this.$sendBtn.addEventListener('click', () => this._sendMessage());
        this.$input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._sendMessage(); }
        });
        this.$startBtn.addEventListener('click', () => {
            // Fresh start only when truly idle with no history; otherwise continue from last position
            const action = this.hasHistory ? 'continue' : 'start';
            ws.send({ type: 'control', action, conversation_id: this.currentConvId });
        });
        this.$retryBtn.addEventListener('click', () => {
            ws.send({ type: 'control', action: 'retry', conversation_id: this.currentConvId });
        });
        this.$stopBtn.addEventListener('click', () => {
            ws.send({ type: 'control', action: 'stop' });
        });
        this.$stopAfterBtn.addEventListener('click', () => {
            ws.send({ type: 'control', action: 'stop_after_current' });
        });
    }

    setConversation(convId, convName) {
        this.currentConvId = convId;
        this.$title.textContent = convName || 'Agent 团队协作';
        this.hasHistory = false;
        this._updateButtonStates();
        this.showChatView();
    }

    clearMessages() {
        this.$messages.innerHTML = '';
    }

    loadHistory(messages) {
        this.clearMessages();
        for (const msg of messages) {
            this._appendMessage(msg.role, msg.content, msg.session_id, msg.timestamp, false, msg.round_index, msg.step_index);
        }
        this._scrollToBottom();
    }

    appendMessage(role, content, sessionId, timestamp, roundIndex, stepIndex) {
        this._appendMessage(role, content, sessionId, timestamp, true, roundIndex, stepIndex);
    }

    _appendMessage(role, content, sessionId, timestamp, scroll, roundIndex, stepIndex) {
        const div = document.createElement('div');
        div.className = `message message-${role}`;

        const roleMap = { user: '用户', codex: 'Codex', claude: 'Claude', system: '系统' };
        const roleLabel = roleMap[role] || role;

        // Build "Round X-Y" suffix for agent messages
        let roundLabel = '';
        if (role !== 'user' && role !== 'system' && roundIndex) {
            roundLabel = stepIndex
                ? ` · Round ${roundIndex}-${stepIndex}`
                : ` · Round ${roundIndex}`;
        }

        // Handle null/undefined content (session in progress)
        const displayContent = content ? escapeHtml(content) : '<span style="color:#888;">运行中...</span>';

        div.innerHTML = `
            <div class="message-header">
                <span class="role">${escapeHtml(roleLabel)}${escapeHtml(roundLabel)}</span>
                <span class="timestamp">${fmtTime(timestamp)}</span>
            </div>
            <div class="message-content">${displayContent}</div>
        `;

        if (sessionId && role !== 'user' && role !== 'system') {
            const btn = document.createElement('button');
            btn.className = 'view-log-link';
            btn.textContent = '查看运行日志';
            btn.addEventListener('click', () => {
                this._openSessionView(sessionId, `${roleLabel} · ${fmtTime(timestamp)}`);
            });
            div.appendChild(btn);
        }

        this.$messages.appendChild(div);
        if (scroll) this._scrollToBottom();
    }

    async _openSessionView(sessionId, label, sessionData) {
        this.$sessionViewTitle.innerHTML = '';
        this.$sessionView.innerHTML = '<div style="color:#888;padding:16px;">加载中...</div>';
        this.viewingSessionId = sessionId;
        this._currentSessionData = sessionData;
        this._renderSessionHeader();
        this.showSessionView();

        try {
            // Try structured messages from the session file first (cc-switch approach)
            const resp = await fetch(`/api/sessions/${sessionId}/messages`);
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const data = await resp.json();

            // Update session data from response
            if (data.session) {
                this._currentSessionData = data.session;
                this._renderSessionHeader();
            }

            this.$sessionView.innerHTML = '';

            if (data.source === 'file' && data.messages && data.messages.length > 0) {
                // Show source path as a small header badge
                if (data.path) {
                    const badge = document.createElement('div');
                    badge.style.cssText = 'font-size:11px;color:#585b70;padding:4px 12px 8px;font-family:monospace;';
                    badge.textContent = data.path;
                    this.$sessionView.appendChild(badge);
                }
                const items = renderStructuredMessages(data.messages);
                for (const el of items) this.$sessionView.appendChild(el);
            } else if (data.source === 'db_only') {
                // session_id not yet assigned (agent just started) — show chat summary if available
                const chat = data.session && data.session.chat;
                if (chat) {
                    const el = document.createElement('div');
                    el.className = 'text-bubble assistant';
                    el.innerHTML = `<div class="bubble-body">${escapeHtml(chat)}</div>`;
                    this.$sessionView.appendChild(el);
                } else {
                    this.$sessionView.innerHTML = '<div style="color:#888;padding:16px;">会话启动中，暂无内容</div>';
                }
            } else if (data.source === 'not_found') {
                // JSONL file not found — show chat summary from DB if available
                const chat = data.session && data.session.chat;
                if (chat) {
                    const el = document.createElement('div');
                    el.className = 'text-bubble assistant';
                    el.innerHTML = `<div class="bubble-body">${escapeHtml(chat)}</div>`;
                    this.$sessionView.appendChild(el);
                } else {
                    this.$sessionView.innerHTML = '<div style="color:#888;padding:16px;">暂无内容（会话文件未找到）</div>';
                }
            } else {
                this.$sessionView.innerHTML = '<div style="color:#888;padding:16px;">暂无内容</div>';
            }

            this._scrollSessionViewToBottom();
        } catch (e) {
            this.$sessionView.innerHTML = `<div style="color:#f38ba8;padding:16px;">加载失败: ${escapeHtml(String(e))}</div>`;
        }
    }

    _renderSessionHeader() {
        // Render session header with: label, PID/duration, resume button, delete button
        const s = this._currentSessionData;
        if (!s) {
            this.$sessionViewTitle.innerHTML = '<span class="session-view-title-text">会话日志</span>';
            return;
        }

        const agentType = s.agent_type || s.agent || 'agent';
        const badgeClass = agentType === 'codex' ? 'badge-codex' : 'badge-claude';
        const roundIdx = s.round_index || s.round_number || 0;
        const stepIdx  = s.step_index || 0;
        const stepPart = (roundIdx > 0 && stepIdx > 0) ? `-${stepIdx}` : '';
        const timeStr = fmtTime(s.created_at || s.started_at);

        // Check for live status
        const liveStatus = this.sidebar.sessionStatus.get(s.id);
        let statusHtml = '';
        if (liveStatus) {
            const idleWarning = liveStatus.idle > 60 ? ' ⚠️' : '';
            statusHtml = `<span class="session-header-status" title="运行中">PID: ${liveStatus.pid} | ${liveStatus.duration}s | idle: ${liveStatus.idle}s${idleWarning}</span>`;
        }

        // Resume button
        const canResume = !!(s.session_id || s.cli_session_id);
        const resumeBtn = canResume
            ? `<button type="button" class="session-header-btn resume" title="继续此会话">继续</button>`
            : '';

        // Delete button
        const deleteBtn = `<button type="button" class="session-header-btn delete" title="删除此记录">删除</button>`;

        this.$sessionViewTitle.innerHTML = `
            <span class="session-agent-badge ${badgeClass}">${escapeHtml(agentType)}</span>
            <span class="session-view-title-text">Round ${roundIdx}${stepPart} · ${timeStr}</span>
            ${statusHtml}
            <span class="session-header-actions">
                ${resumeBtn}
                ${deleteBtn}
            </span>
        `;

        // Bind button events
        const resumeBtnEl = this.$sessionViewTitle.querySelector('.session-header-btn.resume');
        if (resumeBtnEl) {
            resumeBtnEl.addEventListener('click', (e) => {
                e.stopPropagation();
                this.sidebar._showResumeModal(s);
            });
        }

        const deleteBtnEl = this.$sessionViewTitle.querySelector('.session-header-btn.delete');
        if (deleteBtnEl) {
            deleteBtnEl.addEventListener('click', (e) => {
                e.stopPropagation();
                if (confirm(`删除此运行记录？\nRound ${roundIdx}${stepPart} · ${agentType}`)) {
                    this.ws.send({ type: 'delete_session', id: s.id });
                    this.showChatView();
                }
            });
        }
    }

    appendSessionLog(logMsg) {
        // Remove "no content" placeholder if present
        const placeholder = this.$sessionView.querySelector('div[style]');
        if (placeholder && placeholder.textContent.includes('暂无内容')) {
            this.$sessionView.innerHTML = '';
        }
        const entry = parseLogEntry(logMsg);
        if (!entry) return;

        // Streaming text consolidation:
        //
        // Claude emits many tiny text_delta (stream_event) chunks followed by
        // one final assistant event containing the full message text.
        //
        // - text_delta:  append to the last text-bubble (or create one)
        // - assistant:   replace the last text-bubble's content with the
        //                authoritative full text, avoiding duplication
        if (entry.kind === 'text') {
            const lastEl = this.$sessionView.lastElementChild;
            const lastBody = lastEl && lastEl.classList.contains('text-bubble')
                ? lastEl.querySelector('.bubble-body') : null;

            if (logMsg.event_type === 'stream_event') {
                // Incremental delta — append to existing bubble if available
                if (lastBody) {
                    lastBody.appendChild(document.createTextNode(entry.text));
                    this._scrollSessionViewToBottom();
                    return;
                }
            } else if (logMsg.event_type === 'assistant' && lastBody) {
                // Final complete message — replace accumulated delta text so
                // we don't double-print the same content
                lastBody.textContent = entry.text;
                this._scrollSessionViewToBottom();
                return;
            }
        }

        const items = renderLogEntries([logMsg]);
        for (const el of items) {
            this.$sessionView.appendChild(el);
        }
        this._scrollSessionViewToBottom();
    }

    _scrollSessionViewToBottom() {
        this.$sessionView.scrollTop = this.$sessionView.scrollHeight;
    }

    openSessionFromSidebar(session) {
        this._openSessionView(session.id, '', session);
    }

    showSessionView() {
        this.inSessionView = true;
        this.$main.classList.add('session-view');
        this.$sessionViewHeader.classList.remove('hidden');
    }

    showChatView() {
        this.inSessionView = false;
        this.viewingSessionId = null;
        this.$main.classList.remove('session-view');
        this.$sessionViewHeader.classList.add('hidden');
    }

    showInterventionBanner(message, timeout) {
        this.$interventionMessage.textContent = message;
        this.$countdown.textContent = timeout;
        this.$interventionBanner.classList.remove('hidden');
        this._startCountdown(timeout);
    }

    hideInterventionBanner() {
        this.$interventionBanner.classList.add('hidden');
        this._stopCountdown();
    }

    _startCountdown(seconds) {
        this._stopCountdown();
        let remaining = seconds;
        this._countdownTimer = setInterval(() => {
            remaining--;
            this.$countdown.textContent = remaining;
            if (remaining <= 0) this._stopCountdown();
        }, 1000);
    }

    _stopCountdown() {
        if (this._countdownTimer) {
            clearInterval(this._countdownTimer);
            this._countdownTimer = null;
        }
    }

    setConnectionStatus(connected) {
        this.$connStatus.className = 'connection-status ' + (connected ? 'connected' : 'disconnected');
        this.$connStatus.innerHTML = `<span class="status-dot"></span> ${connected ? '已连接' : '断开'}`;
    }

    setStatus(stateStr, roundNum, stepIndex, totalSteps, agent) {
        const stateMap = {
            idle: '空闲',
            agent_starting: '启动中',
            agent_running: '运行中',
            intervention: '等待介入',
            stopping: '停止中',
            error: '出错',
        };
        this.$status.textContent = stateMap[stateStr] || stateStr;

        // Build round/step display:
        // - Round 0: new conversation, not started yet
        // - Round 1-1, 1-2, 2-1, 2-2...: running, step is 1-based
        if (roundNum !== undefined && roundNum !== null) {
            let roundText;
            if (roundNum === 0) {
                // Not started yet
                roundText = 'Round 0';
            } else if (stepIndex !== undefined && stepIndex !== null) {
                // Running: step_index is 0-based internally, display as 1-based
                const stepDisplay = stepIndex + 1;
                roundText = `Round ${roundNum}-${stepDisplay}`;
                if (agent) {
                    roundText += ` (${agent})`;
                }
            } else {
                roundText = `Round ${roundNum}`;
            }
            this.$roundCounter.textContent = roundText;
        }
        this.currentState = stateStr;
        this._updateButtonStates();
    }

    setHasHistory(hasHistory) {
        this.hasHistory = hasHistory;
        this._updateButtonStates();
    }

    _updateButtonStates() {
        const s = this.currentState || 'idle';
        const isRunning = s === 'running';
        const canAct = s === 'idle' || s === 'error';

        // Label: "开始" only when there's no prior history and the system is truly idle
        this.$startBtn.textContent = (!this.hasHistory && s === 'idle') ? '开始' : '继续';
        this.$startBtn.disabled = isRunning;
        // Retry requires having a prior round to repeat
        this.$retryBtn.disabled = !canAct || !this.hasHistory;
        this.$stopBtn.disabled = !isRunning;
        this.$stopAfterBtn.disabled = !isRunning;
    }

    // Open session view immediately when a new agent session starts (before any logs arrive)
    openNewSessionView(sessionId, sessionData) {
        this._currentSessionData = sessionData;
        this._renderSessionHeader();
        this.$sessionView.innerHTML = '<div style="color:#888;padding:16px;">暂无内容</div>';
        this.viewingSessionId = sessionId;
        this._sessionPidEl = null;
        this.showSessionView();
    }

    showSessionPid(sessionId, pid) {
        if (this.viewingSessionId !== sessionId) return;
        // Re-render header to show PID
        this._renderSessionHeader();
    }

    _sendMessage() {
        const content = this.$input.value.trim();
        if (!content) return;
        this.ws.send({
            type: 'user_message',
            content,
            conversation_id: this.currentConvId
        });
        this.$input.value = '';
    }

    _scrollToBottom() {
        this.$messages.scrollTop = this.$messages.scrollHeight;
    }
}

// ============================================================
// App — wires everything together
// ============================================================

class App {
    constructor() {
        const host = location.hostname;
        const port = location.port || '8765';
        const wsUrl = `ws://${host}:${port}/ws`;

        this.ws = new WSClient(wsUrl, (msg) => this._onMessage(msg));
        this.ws.onStatusChange = (connected) => {
            this.chatUI.setConnectionStatus(connected);
        };

        this.chatUI = new ChatUI(this.ws);
        this.sidebar = new ConversationSidebar(
            this.ws,
            (id, name) => this._selectConversation(id, name),
            (session) => this.chatUI.openSessionFromSidebar(session)
        );
        // Allow ChatUI to access sidebar for session status
        this.chatUI.sidebar = this.sidebar;
        this.pipeline = new PipelinePanel(this.ws);

        // Check URL for initial conversation
        this._pendingConvId = this._getConvFromUrl();
    }

    _getConvFromUrl() {
        const params = new URLSearchParams(location.search);
        const id = params.get('conv');
        return id ? parseInt(id, 10) : null;
    }

    _setConvInUrl(convId) {
        const url = new URL(location.href);
        if (convId) {
            url.searchParams.set('conv', convId);
        } else {
            url.searchParams.delete('conv');
        }
        history.replaceState(null, '', url.toString());
    }

    _selectConversation(convId, convName) {
        this.sidebar.setActive(convId, convName);
        this.chatUI.setConversation(convId, convName);
        this._setConvInUrl(convId);
        this.ws.send({ type: 'select_conversation', id: convId });
    }

    _onMessage(msg) {
        switch (msg.type) {
            case 'conversations_list':
                // Store default templates if provided
                if (msg.default_prompt_template) {
                    this.pipeline.setDefaultTemplate(msg.default_prompt_template);
                }
                this.sidebar.setConversations(msg.conversations || []);
                // Decide which conversation to select
                const conversations = msg.conversations || [];
                if (conversations.length > 0) {
                    // Check URL first
                    const urlConvId = this._getConvFromUrl();
                    let targetConv = null;
                    if (urlConvId) {
                        targetConv = conversations.find(c => c.id == urlConvId);
                    }
                    // Fall back to first conversation if URL doesn't match or no URL
                    if (!targetConv) {
                        targetConv = conversations[0];
                    }
                    // Only switch if different from current
                    if (targetConv && targetConv.id != this.sidebar.activeConvId) {
                        this._selectConversation(targetConv.id, targetConv.name);
                    }
                }
                break;

            case 'all_sessions':
                // Legacy — sessions now come via conversation_selected
                break;

            case 'conversation_created':
                if (msg.conversation) {
                    this.sidebar.addConversation(msg.conversation);
                }
                break;

            case 'conversation_renamed':
                this.sidebar.updateConversation(msg.id, msg.name);
                break;

            case 'conversation_deleted':
                this.sidebar.removeConversation(msg.id);
                break;

            case 'conversation_selected': {
                const convName = msg.name || (this.sidebar.conversations.find(c => c.id == msg.id)?.name) || String(msg.id);
                // Update sidebar title, clear old sessions, close modal
                this.sidebar.setActive(msg.id, convName);
                this.chatUI.setConversation(msg.id, convName);
                this.chatUI.loadHistory(msg.history || []);
                // Set default templates from message (if provided)
                if (msg.default_prompt_template) {
                    this.pipeline.setDefaultTemplate(msg.default_prompt_template);
                }
                this.pipeline.load(msg.id, convName, msg.pipeline || [], msg.working_dir);
                // Load sessions for this conversation only
                const sessions = msg.sessions || [];
                this.sidebar.setSessions(sessions, msg.page || 0, msg.total_pages || 1);
                this.chatUI.setHasHistory(sessions.length > 0 || (msg.page || 0) > 0);
                // Update round/step display with current progress
                this.chatUI.setStatus('idle', msg.round_number, msg.step_index, msg.total_steps, null);
                // Update URL
                this._setConvInUrl(msg.id);
                break;
            }

            case 'pipeline_updated':
                break;

            case 'history':
                // Legacy — ignore (we use conversation_selected instead)
                break;

            case 'state':
                this.chatUI.setStatus(msg.state, msg.round_number, msg.step_index, msg.total_steps, msg.agent);
                break;

            case 'agent_status':
                this.chatUI.setStatus(msg.state, msg.round, msg.step_index, msg.total_steps, msg.agent);
                if (msg.state !== 'intervention') {
                    this.chatUI.hideInterventionBanner();
                }
                // When transitioning to idle/stopping/error, clear all live statuses
                if (['idle', 'stopping', 'error'].includes(msg.state)) {
                    this.sidebar.sessionStatus.clear();
                    this.sidebar._renderSessions();
                }
                // Refresh sessions from DB so killed/stopped sessions show as completed
                if (['idle', 'error'].includes(msg.state) && this.sidebar.activeConvId) {
                    this.ws.send({ type: 'get_sessions', conversation_id: this.sidebar.activeConvId, page: 0 });
                }
                break;

            case 'chat_message':
                if (!msg.conversation_id || msg.conversation_id == this.sidebar.activeConvId) {
                    this.chatUI.appendMessage(msg.role, msg.content, msg.session_id, msg.timestamp, msg.round_index, msg.step_index);
                }
                // Refresh session sidebar for this conversation when a session summary arrives
                if (msg.session_id && this.sidebar.activeConvId) {
                    this.sidebar.clearSessionStatus(msg.session_id);
                    this.ws.send({ type: 'get_sessions', conversation_id: this.sidebar.activeConvId, page: 0 });
                }
                break;

            case 'system':
                this.chatUI.appendMessage('system', msg.content, null, new Date().toISOString());
                break;

            case 'sessions_list':
                this.sidebar.setSessions(msg.sessions || [], msg.page, msg.total_pages);
                this.chatUI.setHasHistory((msg.sessions || []).length > 0 || (msg.page || 0) > 0);
                if (msg.history) {
                    this.chatUI.loadHistory(msg.history);
                }
                break;

            case 'session_created':
                // New session started — add to sidebar (don't auto-open)
                if (msg.session && msg.session.conversation_id == this.sidebar.activeConvId) {
                    this.sidebar.addSession(msg.session);
                    this.chatUI.setHasHistory(true);
                }
                break;

            case 'session_deleted':
                if (msg.conversation_id == this.sidebar.activeConvId) {
                    this.ws.send({ type: 'get_sessions', conversation_id: msg.conversation_id, page: this.sidebar.currentPage });
                }
                break;

            case 'session_pid':
                this.chatUI.showSessionPid(msg.session_id, msg.pid);
                break;

            case 'session_status':
                // Live process status update (PID, duration, idle)
                this.sidebar.updateSessionStatus(msg.session_id, {
                    pid: msg.pid,
                    duration: msg.duration,
                    idle: msg.idle
                });
                // Also update session view header if viewing this session
                if (this.chatUI.viewingSessionId === msg.session_id) {
                    this.chatUI._renderSessionHeader();
                }
                break;

            case 'session_log':
                // Real-time log entry — append if currently viewing that session
                if (this.chatUI.viewingSessionId === msg.session_id) {
                    this.chatUI.appendSessionLog(msg);
                }
                break;

            case 'session_chat_updated':
                if (this.sidebar.activeConvId) {
                    this.ws.send({ type: 'get_sessions', conversation_id: this.sidebar.activeConvId, page: this.sidebar.currentPage });
                }
                break;

            case 'intervention_window':
                this.chatUI.showInterventionBanner(msg.message, msg.timeout_seconds);
                break;

            case 'agent_output':
                // Raw streaming output — not shown in chat view
                break;

            default:
                break;
        }
    }
}

// Boot
const app = new App();

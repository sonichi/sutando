/**
 * Sutando Chat UI — clean full-page chat experience.
 *
 * Served at /chat by web-client.ts. Shares the same task-bridge backend
 * as the dashboard textbox: POST /task → poll /result/{task_id}.
 *
 * Differs from the dashboard at /:
 *   - Full-viewport chat (no 30vh clamp)
 *   - Markdown rendering (marked.js from CDN)
 *   - Message bubbles (user right, assistant left)
 *   - Persisted history in localStorage (survives refresh)
 *   - Auto-resizing textarea (Enter sends, Shift+Enter newline)
 *
 * No voice/avatar/dynamic-region — those live on the dashboard.
 */

export const CHAT_HTML = /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sutando — Chat</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<!-- DOMPurify — agent results come from external task channels (Discord,
     Telegram, voice, SMS) and aren't trusted input. marked@12 ships no
     sanitizer by default, so unwrapped innerHTML would execute embedded
     <script> / inline handlers. Sandbox the rendered HTML before insertion. -->
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.9/dist/purify.min.js"></script>
<style>
  /* Palette as tokens so a client can dress this page instead of inheriting one
     opinion. Dark stays the default look; ?theme=light and the OS preference
     both resolve through the same variables. Mirrors the --ui-* approach the
     other built-in surfaces use. */
  :root {
    --chat-bg: #0e0e14;
    --chat-raised: #14141e;
    --chat-line: #1e1e2a;
    --chat-line-soft: #2a2a3e;
    --chat-text: #e8e8ee;
    --chat-text-2: #d0d0e0;
    --chat-text-3: #707080;
    --chat-accent: #4ecca3;
    --chat-user-bubble: #1e2a44;
    --chat-user-border: #2a3a5a;
    --chat-user-avatar: #2a4060;
    --chat-user-avatar-fg: #d8e8f8;
    --chat-agent-avatar: #1e4028;
    --chat-code-bg: #14141e;
    --chat-heading: #f0f0f8;
    --chat-code-fg: #f8b878;
    --chat-quote: #a0a0b0;
    --chat-link: #6ea3ff;
    --chat-line-hover: #3a3a52;
    --chat-hint-code: #888888;
    --chat-caps-key: #66aaff;
  }
  :root.theme-light {
    --chat-bg: #ffffff;
    --chat-raised: #f7f7f9;
    --chat-line: #e4e4e9;
    --chat-line-soft: #ededf1;
    --chat-text: #1f1f25;
    --chat-text-2: #3f3f4a;
    --chat-text-3: #6b6b78;
    --chat-accent: #1a7f4b;
    --chat-user-bubble: #eaf0fb;
    --chat-user-border: #d3e0f5;
    --chat-user-avatar: #dbe7fa;
    --chat-user-avatar-fg: #23446e;
    --chat-agent-avatar: #dff0e6;
    --chat-code-bg: #f4f4f7;
    --chat-heading: #111118;
    --chat-code-fg: #8a3200;
    --chat-quote: #5a5a68;
    --chat-link: #1a56c4;
    --chat-line-hover: #c9c9d4;
    --chat-hint-code: #5a5a68;
    --chat-caps-key: #1a56c4;
  }
  /* No explicit choice: follow the host OS. An explicit ?theme= wins over it,
     which is why both stamps are classes on <html> rather than media-only. */
  @media (prefers-color-scheme: light) {
    :root:not(.theme-dark) {
      --chat-bg: #ffffff;
      --chat-raised: #f7f7f9;
      --chat-line: #e4e4e9;
      --chat-line-soft: #ededf1;
      --chat-text: #1f1f25;
      --chat-text-2: #3f3f4a;
      --chat-text-3: #6b6b78;
      --chat-accent: #1a7f4b;
      --chat-user-bubble: #eaf0fb;
      --chat-user-border: #d3e0f5;
      --chat-user-avatar: #dbe7fa;
      --chat-user-avatar-fg: #23446e;
      --chat-agent-avatar: #dff0e6;
      --chat-code-bg: #f4f4f7;
      --chat-heading: #111118;
      --chat-code-fg: #8a3200;
      --chat-quote: #5a5a68;
      --chat-link: #1a56c4;
      --chat-line-hover: #c9c9d4;
      --chat-hint-code: #5a5a68;
      --chat-caps-key: #1a56c4;
    }
  }
  /* Embedded in a host that draws its own title bar: one header, not two. */
  :root.embed .header { display: none; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
    background: var(--chat-bg); color: var(--chat-text);
    display: flex; flex-direction: column;
  }

  .header {
    padding: 12px 24px; border-bottom: 1px solid var(--chat-line);
    display: flex; align-items: center; gap: 12px; flex-shrink: 0;
  }
  .header .dot {
    width: 10px; height: 10px; border-radius: 50%; background: var(--chat-accent);
    box-shadow: 0 0 6px rgba(78, 204, 163, 0.5);
  }
  .header .title { font-size: 15px; font-weight: 600; }
  .header .subtitle {
    font-size: 12px; color: var(--chat-text-3); margin-left: auto; margin-right: 12px;
  }
  .header a {
    color: var(--chat-text-3); font-size: 12px; text-decoration: none;
    padding: 5px 12px; border-radius: 6px; border: 1px solid var(--chat-line-soft);
    transition: all 0.12s ease;
  }
  .header a:hover { color: var(--chat-text); border-color: var(--chat-line-hover); }
  .header .clear-btn {
    color: var(--chat-text-3); font-size: 12px; font-family: inherit;
    background: transparent; cursor: pointer;
    border: 1px solid var(--chat-line-soft); border-radius: 6px;
    padding: 5px 12px;
    transition: all 0.12s ease;
  }
  .header .clear-btn:hover { color: var(--chat-text); border-color: var(--chat-line-hover); }

  .chat {
    flex: 1; overflow-y: auto; padding: 32px 0 16px;
    display: flex; flex-direction: column; align-items: center;
  }
  .chat-inner {
    width: 100%; max-width: 760px; padding: 0 24px;
    /* Narrow docks: reclaim the gutter for text. */
    container-type: inline-size;
    display: flex; flex-direction: column; gap: 20px;
  }
  .empty {
    text-align: center; color: #555; font-size: 14px; padding: 80px 20px 40px;
  }
  .empty .logo { font-size: 32px; margin-bottom: 12px; }
  .empty .hint {
    font-size: 13px; color: #444; margin-top: 24px;
    line-height: 1.6;
  }
  .empty .hint code {
    background: var(--chat-raised); padding: 2px 6px; border-radius: 4px;
    color: var(--chat-hint-code); font-size: 12px;
  }
  .empty .caps {
    display: inline-grid; grid-template-columns: auto 1fr;
    gap: 3px 12px; margin-top: 28px; text-align: left;
  }
  .empty .caps dt { color: var(--chat-caps-key); font-size: 12px; white-space: nowrap; }
  .empty .caps dd { color: #444; font-size: 12px; margin: 0; }

  .msg { display: flex; gap: 12px; max-width: 100%; }

  /* Docked in a narrow column (a desktop side panel, a phone): the avatar and
     the wide gutter cost more than they earn once the text column is short. */
  @media (max-width: 560px) {
    .chat-inner { padding: 0 12px; }
    .msg { gap: 8px; }
    .msg .avatar { width: 22px; height: 22px; font-size: 10px; }
    .msg .bubble { padding: 10px 12px; font-size: 14px; max-width: calc(100% - 30px); }
    .header { padding: 10px 12px; }
    .input-inner { padding: 0 12px; }
  }
  .msg .avatar {
    width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700;
  }
  .msg.user { flex-direction: row-reverse; }
  .msg.user .avatar { background: var(--chat-user-avatar); color: var(--chat-user-avatar-fg); }
  .msg.assistant .avatar { background: var(--chat-agent-avatar); color: var(--chat-accent); }
  .msg .bubble {
    border-radius: 14px; padding: 12px 16px; line-height: 1.6;
    font-size: 15px; word-wrap: break-word; max-width: calc(100% - 40px);
  }
  .msg.user .bubble {
    background: var(--chat-user-bubble); border: 1px solid var(--chat-user-border);
  }
  .msg.assistant .bubble {
    background: var(--chat-raised); border: 1px solid var(--chat-line);
  }
  .msg.assistant .bubble.pending {
    color: var(--chat-text-3);
  }

  /* Markdown styles inside bubbles */
  .bubble h1 { font-size: 1.5em; margin: 0.6em 0 0.4em; font-weight: 700; }
  .bubble h2 { font-size: 1.25em; margin: 0.6em 0 0.4em; font-weight: 700; color: var(--chat-heading); }
  .bubble h3 { font-size: 1.1em; margin: 0.6em 0 0.3em; font-weight: 700; color: var(--chat-heading); }
  .bubble h4 { font-size: 1em; margin: 0.5em 0 0.3em; font-weight: 700; }
  .bubble p { margin: 0.5em 0; }
  .bubble p:first-child { margin-top: 0; }
  .bubble p:last-child { margin-bottom: 0; }
  .bubble ul, .bubble ol { margin: 0.5em 0; padding-left: 1.6em; }
  .bubble li { margin: 0.25em 0; }
  .bubble code {
    background: var(--chat-code-bg); padding: 2px 6px; border-radius: 4px;
    font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 0.85em;
    color: var(--chat-code-fg);
  }
  .bubble pre {
    background: var(--chat-code-bg); padding: 12px 14px; border-radius: 8px;
    overflow-x: auto; margin: 0.6em 0; border: 1px solid var(--chat-line);
  }
  .bubble pre code { background: none; padding: 0; color: var(--chat-text-2); font-size: 0.9em; }
  .bubble a { color: var(--chat-link); text-decoration: none; }
  .bubble a:hover { text-decoration: underline; }
  .bubble blockquote {
    border-left: 3px solid var(--chat-user-avatar); padding-left: 12px;
    margin: 0.6em 0; color: var(--chat-quote);
  }
  .bubble table {
    border-collapse: collapse; margin: 0.5em 0; width: 100%;
    font-size: 0.95em;
  }
  .bubble th, .bubble td {
    border: 1px solid var(--chat-line); padding: 6px 10px; text-align: left;
  }
  .bubble th { background: var(--chat-raised); }
  .bubble strong { font-weight: 700; color: var(--chat-heading); }
  .bubble em { color: var(--chat-text-2); }
  .bubble hr { border: 0; border-top: 1px solid var(--chat-line); margin: 1em 0; }

  .input-area {
    border-top: 1px solid var(--chat-line); padding: 16px 24px 20px;
    background: var(--chat-bg); flex-shrink: 0;
    display: flex; justify-content: center;
  }
  .input-inner {
    width: 100%; max-width: 760px;
    display: flex; gap: 8px; align-items: flex-end;
  }
  .input-textarea {
    flex: 1; background: var(--chat-raised); color: var(--chat-text);
    border: 1px solid var(--chat-line-soft); border-radius: 12px;
    padding: 13px 16px; font-size: 15px; line-height: 1.5;
    font-family: inherit; resize: none;
    min-height: 50px; max-height: 240px;
    outline: none;
    transition: border-color 0.12s ease;
  }
  .input-textarea:focus { border-color: var(--chat-accent); }
  .input-textarea::placeholder { color: var(--chat-text-3); }
  .send-btn {
    background: var(--chat-agent-avatar); color: var(--chat-accent);
    border: 1px solid #2a4a36; border-radius: 12px;
    padding: 0 20px; font-size: 14px; font-weight: 600;
    cursor: pointer; height: 50px;
    transition: background 0.15s ease;
    font-family: inherit;
  }
  .send-btn:hover:not(:disabled) { background: #2a503a; }
  .send-btn:disabled {
    background: #1a1a2a; color: #444; border-color: var(--chat-line-soft);
    cursor: not-allowed;
  }

  .chat::-webkit-scrollbar { width: 8px; }
  .chat::-webkit-scrollbar-track { background: transparent; }
  .chat::-webkit-scrollbar-thumb { background: var(--chat-line); border-radius: 4px; }
  .chat::-webkit-scrollbar-thumb:hover { background: var(--chat-line-soft); }

  .typing { display: inline-flex; gap: 4px; padding: 4px 0; align-items: center; }
  .typing span {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--chat-accent); opacity: 0.4;
    animation: typing 1.4s infinite ease-in-out;
  }
  .typing span:nth-child(2) { animation-delay: 0.2s; }
  .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes typing {
    0%, 60%, 100% { opacity: 0.3; transform: translateY(0); }
    30% { opacity: 1; transform: translateY(-4px); }
  }
</style>
</head>
<body>
  <div class="header">
    <div class="dot"></div>
    <div class="title">Sutando</div>
    <div class="subtitle" id="subtitle">core agent</div>
    <!-- Styling lives in the .header .clear-btn rule above. Per Chi #650:
         dual styling (inline + class) creates surprise on future edits. -->
    <button class="header clear-btn" id="clearBtn" style="margin-right:8px;">Clear</button>
    <a href="/" title="Open dashboard">Dashboard</a>
  </div>

  <div class="chat" id="chat">
    <div class="chat-inner" id="chatInner">
      <div class="empty" id="empty">
        <div class="logo">💬</div>
        <div>Start a conversation with Sutando.</div>
        <div class="hint">
          Tasks route through the same bridge as Telegram, Discord, and voice.<br>
          Press <code>Enter</code> to send, <code>Shift+Enter</code> for newline.
        </div>
        <dl class="caps">
          <dt>Voice + screen</dt><dd>control any Mac app by voice; read what's on screen</dd>
          <dt>Comms</dt><dd>phone, iMessage, Telegram, Discord</dd>
          <dt>Calendar + email</dt><dd>Gmail / Google Calendar; draft replies; book meetings</dd>
          <dt>Autonomous work</dt><dd>ships code, summarizes news, runs benchmarks</dd>
          <dt>Fleet</dt><dd>coordinates with other Sutandos under access tiers</dd>
          <dt>Skills</dt><dd><code>/list-skills</code> to see all installed</dd>
        </dl>
      </div>
    </div>
  </div>

  <div class="input-area">
    <div class="input-inner">
      <textarea
        class="input-textarea"
        id="input"
        rows="1"
        placeholder="Ask Sutando anything…"
        autofocus
      ></textarea>
      <button class="send-btn" id="sendBtn" onclick="sendMessage()">Send</button>
    </div>
  </div>

<script>
  // Preserve scheme so an https-served /chat doesn't downgrade to http.
  // Per Chi's PR #650 review: hardcoded http: breaks tailscale-funnel'd
  // deployments where the page is served over https — browser blocks the
  // mixed-content request and the chat just hangs.
  const apiBase = location.protocol + '//' + location.hostname + ':7843';
  const chatInner = document.getElementById('chatInner');
  const empty = document.getElementById('empty');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('sendBtn');
  const subtitle = document.getElementById('subtitle');
  const clearBtn = document.getElementById('clearBtn');

  // Configure marked: GitHub-flavored markdown, line breaks
  if (window.marked) {
    marked.setOptions({ breaks: true, gfm: true });
  }

  const HISTORY_KEY = 'sutando-chat-history-v1';
  const PENDING_KEY = 'sutando-chat-page-pending-v1';
  let history = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
  let pendingTasks = loadPendingTasks();
  const pendingPolls = {};

  function saveHistory() {
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-100))); } catch (e) {}
  }

  function loadPendingTasks() {
    try {
      const raw = localStorage.getItem(PENDING_KEY);
      if (!raw) return [];
      const cutoff = Date.now() - 30 * 60 * 1000;
      return JSON.parse(raw).filter(function(t) {
        return t && t.id && (t.createdAt || 0) >= cutoff;
      });
    } catch (e) { return []; }
  }

  function savePendingTasks() {
    try { localStorage.setItem(PENDING_KEY, JSON.stringify(pendingTasks.slice(-20))); } catch (e) {}
  }

  function rememberPendingTask(taskId, text) {
    pendingTasks = pendingTasks.filter(function(t) { return t.id !== taskId; });
    pendingTasks.push({ id: taskId, text: text || '', createdAt: Date.now() });
    savePendingTasks();
  }

  function forgetPendingTask(taskId) {
    pendingTasks = pendingTasks.filter(function(t) { return t.id !== taskId; });
    savePendingTasks();
    if (pendingPolls[taskId]) {
      clearInterval(pendingPolls[taskId]);
      delete pendingPolls[taskId];
    }
  }

  function renderMarkdown(text) {
    // Fall back to entity-escaped textContent if EITHER library failed to
    // load (CDN offline, ad blocker, etc.). marked alone would be unsafe —
    // without DOMPurify the innerHTML executes arbitrary HTML/JS from
    // untrusted task channels. Match the existing fallback's escape style.
    if (!window.marked || !window.DOMPurify) {
      return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    try { return DOMPurify.sanitize(marked.parse(text)); } catch (e) { return text; }
  }

  function appendMessage(role, content, save) {
    if (save === undefined) save = true;
    empty.style.display = 'none';
    const msg = document.createElement('div');
    msg.className = 'msg ' + role;

    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = role === 'user' ? 'You' : 'S';

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    if (role === 'assistant') {
      bubble.innerHTML = renderMarkdown(content);
    } else {
      bubble.textContent = content;
    }

    msg.appendChild(avatar);
    msg.appendChild(bubble);
    chatInner.appendChild(msg);
    scrollToBottom();

    if (save) {
      history.push({ role: role, content: content });
      saveHistory();
    }
    return bubble;
  }

  function appendPendingMessage(taskId) {
    if (taskId) {
      const existing = document.querySelector('[data-pending-task="' + taskId + '"]');
      if (existing) return existing;
    }
    empty.style.display = 'none';
    const pendingMsg = document.createElement('div');
    pendingMsg.className = 'msg assistant';
    if (taskId) pendingMsg.dataset.pendingTask = taskId;
    pendingMsg.innerHTML = '<div class="avatar">S</div><div class="bubble pending"><div class="typing"><span></span><span></span><span></span></div></div>';
    chatInner.appendChild(pendingMsg);
    scrollToBottom();
    return pendingMsg;
  }

  function completePendingTask(taskId, result, pendingMsg) {
    if (pendingMsg && pendingMsg.parentNode) pendingMsg.remove();
    appendMessage('assistant', result || '*(empty response)*');
    forgetPendingTask(taskId);
    sendBtn.disabled = false;
    input.focus();
  }

  function pollPendingTask(taskId, pendingMsg) {
    if (!taskId || pendingPolls[taskId]) return;
    const check = function() {
      fetch(apiBase + '/result/' + taskId)
        .then(function(r) { return r.json(); })
        .then(function(j) {
          if (j.status === 'completed') completePendingTask(taskId, j.result, pendingMsg);
        })
        .catch(function() {});
    };
    check();
    pendingPolls[taskId] = setInterval(check, 2000);
  }

  function resumePendingTasks() {
    pendingTasks = loadPendingTasks();
    savePendingTasks();
    pendingTasks.forEach(function(task) {
      if (!task || !task.id) return;
      const pendingMsg = appendPendingMessage(task.id);
      pollPendingTask(task.id, pendingMsg);
    });
  }

  function scrollToBottom() {
    const chat = document.getElementById('chat');
    requestAnimationFrame(() => { chat.scrollTop = chat.scrollHeight; });
  }

  function autoresize() {
    input.style.height = '50px';
    input.style.height = Math.min(input.scrollHeight, 240) + 'px';
  }

  function renderHistory() {
    if (history.length === 0) {
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';
    history.forEach(m => appendMessage(m.role, m.content, false));
  }

  input.addEventListener('input', autoresize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  clearBtn.addEventListener('click', () => {
    if (history.length === 0) return;
    if (!confirm('Clear conversation history? This only clears the local view; agent memory is unaffected.')) return;
    history = [];
    saveHistory();
    chatInner.innerHTML = '';
    chatInner.appendChild(empty);
    empty.style.display = 'block';
  });

  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    autoresize();
    sendBtn.disabled = true;

    appendMessage('user', text);

    const pendingMsg = appendPendingMessage();

    try {
      const resp = await fetch(apiBase + '/task', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from: 'web', task: text }),
      });
      const data = await resp.json();
      if (!data.ok) throw new Error('task creation failed');
      const taskId = data.task_id;
      pendingMsg.dataset.pendingTask = taskId;
      rememberPendingTask(taskId, text);
      pollPendingTask(taskId, pendingMsg);
    } catch (e) {
      pendingMsg.remove();
      appendMessage('assistant', '*(failed to reach agent API at \`' + apiBase + '\`. Make sure the bridge is running.)*');
      sendBtn.disabled = false;
    }
  }

  renderHistory();
  resumePendingTasks();

  // Live agent state via SSE (same channel as dashboard)
  try {
    const sse = new EventSource('/sse');
    sse.addEventListener('agent-state', (e) => {
      const states = {
        idle: 'idle',
        listening: '🎤 listening',
        speaking: '💬 speaking',
        working: '⚙️ working',
        seeing: '👁 looking',
      };
      subtitle.textContent = states[e.data] || (e.data || 'core agent');
    });
  } catch (e) {}

  input.focus();
</script>
</body>
</html>`;

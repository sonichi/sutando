/**
 * Web Audio Client for Sutando
 *
 * Usage:
 *   1. Start the voice agent:  pnpm tsx examples/hello_world/agent.ts
 *   2. Start this client:      pnpm tsx examples/web-client.ts
 *   3. Open http://localhost:8080 in your browser
 *   4. Click "Connect" and allow microphone access
 */

import { createServer } from 'node:http';

const HTTP_PORT = Number(process.env.CLIENT_PORT) || 8080;
const HTTP_HOST = process.env.CLIENT_HOST || '0.0.0.0'; // '0.0.0.0' binds to all interfaces for EC2
const WS_PORT = Number(process.env.PORT) || 9900;
const DEFAULT_WS_URL = `ws://localhost:${WS_PORT}`;

const HTML = /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sutando Web UI</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0a0a12; color: #c0c0d0;
    display: flex; flex-direction: column; align-items: center;
    min-height: 100vh; padding: 0;
  }
  /* Header */
  .header {
    width: 100%; padding: 16px 20px;
    display: flex; align-items: center; gap: 14px;
    background: #0e0e18; border-bottom: 1px solid #1a1a2e;
  }
  .header .avatar {
    width: 44px; height: 44px; border-radius: 50%;
    border: 2px solid #4ecca3; object-fit: cover; display: none;
  }
  .header .info { flex: 1; }
  .header h1 { color: #fff; font-size: 1.1em; font-weight: 500; }
  .header .meta { font-size: 11px; color: #555; display: flex; gap: 12px; align-items: center; margin-top: 2px; }
  .header .meta a { color: #555; text-decoration: none; border-bottom: 1px dotted #333; }
  .header .meta a:hover { color: #888; }
  .status-pill {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 500;
  }
  .status-pill.voice-on { background: #1a2e24; color: #4ecca3; }
  .status-pill.voice-off { background: #1a1a2e; color: #666; }
  .status-pill .dot {
    width: 6px; height: 6px; border-radius: 50%; background: #333;
  }
  .status-pill.voice-on .dot { background: #4ecca3; box-shadow: 0 0 4px #4ecca3; }
  .header .controls { display: flex; gap: 6px; }
  button {
    padding: 7px 14px; border-radius: 8px; border: none;
    font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.15s;
    white-space: nowrap;
  }
  .btn-voice {
    background: #1e5128; color: #fff; padding: 9px 20px; font-size: 13px;
    border: 1px solid #2a7a3a; border-radius: 10px;
    box-shadow: 0 0 12px rgba(78, 204, 163, 0.15);
  }
  .btn-voice:hover { background: #277334; box-shadow: 0 0 16px rgba(78, 204, 163, 0.25); }
  .btn-voice.active { background: #8b1a1a; border-color: #a52222; box-shadow: none; }
  .btn-voice.active:hover { background: #a52222; }
  .btn-mute { background: #2a2a3e; color: #888; }
  .btn-mute:hover { background: #3a3a4e; color: #fff; }
  .btn-mute.muted { background: #4a1a1a; color: #e94560; }
  .btn-subtle { background: transparent; color: #444; font-size: 11px; padding: 5px 8px; }
  .btn-subtle:hover { color: #888; }

  /* Main content */
  .main { width: 100%; max-width: 960px; flex: 1; display: flex; flex-direction: column; padding: 12px 24px; margin: 0 auto; }

  /* Conversation */
  #transcript {
    flex: 1; min-height: 200px; max-height: 50vh;
    background: #0e0e18; border-radius: 12px; padding: 14px 16px;
    overflow-y: auto; font-size: 14px; line-height: 1.7;
    margin-bottom: 10px;
  }
  .t-entry { margin-bottom: 6px; }
  .t-user { color: #7fb3e0; }
  .t-user::before { content: 'You: '; font-weight: 600; color: #5a9fd4; }
  .t-assistant { color: #a8d8b0; }
  .t-assistant::before { content: 'Sutando: '; font-weight: 600; color: #6dbe82; }
  .t-system { color: #666; font-size: 12px; }
  .t-interim { color: #7fb3e0; opacity: 0.5; font-size: 13px; }
  .t-interim::before { content: 'You: '; font-weight: 600; }

  /* Input bar */
  .input-bar {
    display: flex; gap: 8px; margin-bottom: 12px;
  }
  .input-bar input {
    flex: 1; padding: 10px 14px; border-radius: 10px;
    border: 1px solid #1e1e30; background: #0e0e18; color: #fff; font-size: 13px;
    outline: none;
  }
  .input-bar input:focus { border-color: #4ecca3; }
  .input-bar input::placeholder { color: #444; }
  .btn-send { background: #1a2e24; color: #4ecca3; border: 1px solid #2a4a36; }
  .btn-send:hover { background: #243e30; }

  /* Tasks */
  #tasks {
    background: #0e0e18; border-radius: 10px; padding: 8px 14px;
    margin-bottom: 10px; font-size: 12px;
  }
  #tasks:empty { display: none; }
  .task-item {
    display: flex; align-items: center; gap: 8px;
    padding: 5px 0; border-bottom: 1px solid #141420;
  }
  .task-item:last-child { border-bottom: none; }
  .task-status {
    width: 16px; height: 16px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 9px; flex-shrink: 0;
  }
  .task-status.working { background: #1e3a5f; color: #60a5fa; animation: pulse 1.5s infinite; }
  .task-status.done { background: #1e4028; color: #4ecca3; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  .task-text { color: #888; flex: 1; word-break: break-word; }
  .task-time { color: #444; font-size: 10px; }

  /* Questions */
  #questions {
    background: #1e1a12; border: 1px solid #2e2818; border-radius: 10px;
    padding: 10px 14px; margin-bottom: 10px; font-size: 12px; display: none;
  }

  /* Section labels */
  .section-label {
    font-size: 10px; color: #444; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 4px; margin-top: 4px;
  }

  /* Debug */
  #debug {
    background: #08080f; border-radius: 10px; padding: 10px 12px;
    max-height: 20vh; overflow-y: auto; font-size: 10px; line-height: 1.5;
    font-family: 'SF Mono', 'Fira Code', monospace;
  }
  .d-entry { color: #444; }
  .d-entry.warn { color: #f0ad4e; }
  .d-entry.err { color: #ef5350; }
  .d-entry.event { color: #9575cd; }
  .d-entry.audio { color: #4db6ac; }
  .btn-download {
    display: inline-block; margin-top: 6px; padding: 4px 10px;
    border-radius: 6px; border: 1px solid #1e1e30; background: #0e0e18;
    color: #555; font-size: 11px; cursor: pointer; text-decoration: none;
  }
  .btn-download:hover { background: #1a1a2e; color: #aaa; }

  /* Hidden URL input */
  #wsUrl { display: none; }
  .stats { font-size: 10px; color: #444; }

  /* Hero connect screen — shown when voice is disconnected */
  .hero {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 48px 20px 24px;
  }
  .hero .avatar-hero {
    width: 80px; height: 80px; border-radius: 50%;
    border: 3px solid #4ecca3; object-fit: cover; margin-bottom: 16px; display: none;
  }
  .hero h2 { color: #fff; font-size: 1.3em; font-weight: 500; margin-bottom: 4px; }
  .hero .tagline { color: #555; font-size: 13px; margin-bottom: 24px; }
  .btn-hero {
    background: #1e5128; color: #fff; padding: 14px 36px; font-size: 15px; font-weight: 600;
    border: 1px solid #2a7a3a; border-radius: 14px;
    box-shadow: 0 0 20px rgba(78, 204, 163, 0.2);
    cursor: pointer; transition: all 0.2s;
  }
  .btn-hero:hover { background: #277334; box-shadow: 0 0 28px rgba(78, 204, 163, 0.35); transform: scale(1.02); }
  .hero .or-text { color: #444; font-size: 12px; margin: 16px 0 8px; }

  /* Suggestion chips */
  .suggestions { display: flex; flex-wrap: wrap; gap: 8px; max-width: 480px; justify-content: center; margin-top: 20px; }
  .suggestion {
    padding: 8px 16px; border-radius: 20px; font-size: 12px;
    background: #12121e; border: 1px solid #1e1e30; color: #888;
    cursor: pointer; transition: all 0.15s;
  }
  .suggestion:hover { background: #1a1a2e; border-color: #4ecca3; color: #c0c0d0; }
  .suggestions-label { color: #333; font-size: 11px; margin-top: 24px; margin-bottom: 4px; }

  /* When voice is active, hide hero */
  body.voice-active .hero { display: none; }
  body.voice-active .main { display: flex; }
</style>
</head>
<body>

<div class="header">
  <img class="avatar" id="stand-avatar" src="http://localhost:7844/avatar">
  <div class="info">
    <h1 id="stand-name">Sutando</h1>
    <div class="meta">
      <span class="status-pill voice-off" id="voice-status"><span class="dot" id="dot"></span> <span id="status">Text only</span></span>
      <a href="http://localhost:7844" target="_blank">Dashboard</a>
      <span class="stats" id="stats"></span>
    </div>
  </div>
  <div class="controls">
    <button id="btn" class="btn-voice" onclick="toggle()" style="display:none">End Voice</button>
    <button id="btn-mute" class="btn-mute" onclick="toggleMute()" style="display:none">Mute</button>
    <button class="btn-subtle" onclick="saveDebug()">Debug</button>
  </div>
</div>
<input type="text" id="wsUrl" value="${DEFAULT_WS_URL}" />
<script>
fetch('http://localhost:7844/stand-identity').then(r=>r.json()).then(s=>{
  if(s.name){
    document.getElementById('stand-name').textContent='Sutando — '+s.name;
    document.getElementById('hero-name').textContent='Sutando — '+s.name;
  }
  if(s.avatarGenerated){
    document.getElementById('stand-avatar').style.display='block';
    document.getElementById('hero-avatar').style.display='block';
  }
}).catch(()=>{});
</script>

<div class="hero" id="hero">
  <img class="avatar-hero" id="hero-avatar" src="http://localhost:7844/avatar">
  <h2 id="hero-name">Sutando</h2>
  <p class="tagline">Voice, screen, and task control from one agent</p>
  <button class="btn-hero" onclick="toggle()">Start Voice</button>
  <p class="or-text">or type below</p>
  <p class="suggestions-label">Try saying or typing</p>
  <div class="suggestions">
    <span class="suggestion" onclick="trySuggestion(this)">"What's on my screen?"</span>
    <span class="suggestion" onclick="trySuggestion(this)">"What's on my calendar today?"</span>
    <span class="suggestion" onclick="trySuggestion(this)">"Introduce yourself"</span>
    <span class="suggestion" onclick="trySuggestion(this)">"Take a note: my first Sutando note"</span>
    <span class="suggestion" onclick="trySuggestion(this)">"Read my latest emails"</span>
    <span class="suggestion" onclick="trySuggestion(this)">"Open github.com"</span>
  </div>
</div>

<div class="main" id="main-area">

<div id="transcript">
  <div class="t-entry t-system">Ask Sutando anything.</div>
</div>

<div class="input-bar">
  <input type="text" id="textInput" placeholder="Type a message..." onkeydown="if(event.key==='Enter')sendText()" />
  <button class="btn-send" onclick="sendText()">Send</button>
</div>

<div id="questions"></div>
<div id="tasks-header" style="display:none;margin:8px 0 2px;font-size:11px;color:#555;display:flex;justify-content:space-between;align-items:center">
  <span>Tasks</span>
  <span style="cursor:pointer;color:#4a6a7a" onclick="toggleAllTasks()">collapse all</span>
</div>
<div id="tasks"></div>

<div class="section-label" style="cursor:pointer" onclick="$('debug').style.display=$('debug').style.display==='none'?'':'none'">Debug</div>
<div id="debug" style="display:none"></div>

</div>

<script>
// ─── Config ───────────────────────────────────────────────
let INPUT_RATE  = 16000;
let OUTPUT_RATE = 24000;
const CAPTURE_BUF = 2048;
const WS_PORT = ${WS_PORT};

// Auto-detect WebSocket URL from current hostname
function getDefaultWsUrl() {
  const hostname = window.location.hostname;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  // If HTTPS, use /ws path through nginx proxy; otherwise direct port
  if (window.location.protocol === 'https:') {
    return protocol + '//' + hostname + '/ws';
  }
  return protocol + '//' + hostname + ':' + WS_PORT;
}

// Set default WebSocket URL on page load + init Chrome STT
window.addEventListener('DOMContentLoaded', () => {
  const wsUrlInput = $('wsUrl');
  if (wsUrlInput && !wsUrlInput.value) {
    wsUrlInput.value = getDefaultWsUrl();
  }
  initChromeStt();
  // Auto-reconnect voice if it was connected before refresh
  try { if (sessionStorage.getItem('sutando-voice')) { setTimeout(() => toggle(), 500); } } catch {}
});

// ─── State ────────────────────────────────────────────────
let ws = null;
let audioCtx = null;
let micStream = null;
let processor = null;
let connected = false;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;
let nextPlayTime = 0;
let activeSources = [];
let playbackRate = 1.0;
let bytesSent = 0;
let bytesRecv = 0;
let audioChunksRecv = 0;
let playChunkCount = 0;
let statsTimer = null;
let muted = false;

// Chrome STT state — provides real-time interim display; server STT replaces with final
let recognition = null;

const debugLog = [];
const $ = (id) => document.getElementById(id);

// ─── Chrome STT (real-time interim display) ───────────────
function initChromeStt() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    dbg('Browser does not support SpeechRecognition — no interim transcripts available', 'warn');
    return;
  }
  recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = 'en-US';

  recognition.onresult = (event) => {
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      interim += event.results[i][0].transcript;
    }
    if (interim) showChromeSttInterim(interim);
  };

  recognition.onerror = (event) => {
    if (event.error !== 'no-speech') dbg('Chrome STT error: ' + event.error, 'warn');
  };

  recognition.onend = () => {
    if (connected) {
      try { recognition.start(); } catch {}
    }
  };
}

function showChromeSttInterim(text) {
  if (serverUserTextReceived) return;  // server text is authoritative — don't overwrite
  if (!currentUserEl) {
    currentUserEl = document.createElement('div');
    currentUserEl.className = 't-entry t-interim';
    $('transcript').appendChild(currentUserEl);
  }
  currentUserEl.textContent = text;
  $('transcript').scrollTop = $('transcript').scrollHeight;
}

function startChromeStt() {
  if (!recognition) return;
  try { recognition.start(); } catch {}
}

function stopChromeStt() {
  if (recognition) { try { recognition.stop(); } catch {} }
}

// ─── Transcript ───────────────────────────────────────────
let currentUserEl = null;
let currentAssistantEl = null;
let serverUserTextReceived = false;  // blocks Chrome STT overwrites after server sends

function handleTranscript(role, text, partial) {
  if (role === 'user') {
    dbg('[Server STT] ' + (partial ? 'partial' : 'FINAL') + ': ' + text);
    serverUserTextReceived = true;
    if (partial) {
      if (!currentUserEl) {
        currentUserEl = document.createElement('div');
        currentUserEl.className = 't-entry t-interim';
        $('transcript').appendChild(currentUserEl);
      }
      currentUserEl.textContent = text;
    } else {
      // Final transcript — update in-place for correct ordering
      if (!currentUserEl) {
        currentUserEl = document.createElement('div');
        $('transcript').appendChild(currentUserEl);
      }
      currentUserEl.className = 't-entry t-user';
      currentUserEl.textContent = text;
      currentUserEl = null;
    }
  } else {
    if (!currentAssistantEl) {
      currentAssistantEl = document.createElement('div');
      currentAssistantEl.className = 't-entry t-assistant';
      $('transcript').appendChild(currentAssistantEl);
    }
    currentAssistantEl.textContent = text;
    if (!partial) currentAssistantEl = null;
  }
  $('transcript').scrollTop = $('transcript').scrollHeight;
}

function addSystem(text) {
  const el = document.createElement('div');
  el.className = 't-entry t-system';
  el.textContent = text;
  $('transcript').appendChild(el);
  $('transcript').scrollTop = $('transcript').scrollHeight;
}

// ─── Debug log ────────────────────────────────────────────
function dbg(text, cls = '') {
  const ts = new Date().toISOString().slice(11, 23);
  const line = ts + '  ' + text;
  debugLog.push(line);
  const el = document.createElement('div');
  el.className = 'd-entry ' + cls;
  el.textContent = line;
  $('debug').appendChild(el);
  while ($('debug').children.length > 500) $('debug').removeChild($('debug').firstChild);
  $('debug').scrollTop = $('debug').scrollHeight;
}

function setStatus(text, state) {
  $('status').textContent = text;
  $('dot').className = 'dot' + (state === 'live' ? ' live' : state === 'error' ? ' error' : '');
}

// ─── Task list ────────────────────────────────────────────
// Expose on window so inline tools can access via Chrome AppleScript JS injection
const taskMap = window.taskMap = {};
function updateTask(taskId, status, text, result) {
  const existing = taskMap[taskId] || {};
  const wasDone = existing.status === 'done';
  taskMap[taskId] = { status, text: text || existing.text, time: new Date(), result: result || existing.result || '' };
  // Auto-expand the latest completed task (collapse others if user had collapsed)
  if (status === 'done' && !wasDone && (result || existing.result)) {
    if (userCollapsed) expandedTasks.clear(); // keep old ones collapsed
    expandedTasks.add(taskId);
    userCollapsed = false;
    setTimeout(() => { const el = document.getElementById('result-' + taskId); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }, 100);
  }
  renderTasks();
}
const expandedTasks = window.expandedTasks = new Set();
let userCollapsed = false; // user manually collapsed — suppress auto-expand
// Listen for external collapse/expand commands (from inline tools via AppleScript)
new MutationObserver(() => {
  if (document.body.dataset.taskAction === 'collapse') { expandedTasks.clear(); userCollapsed = true; renderTasks(); document.body.dataset.taskAction = ''; }
  if (document.body.dataset.taskAction === 'expand') { Object.keys(taskMap).forEach(id => { if (taskMap[id].result) expandedTasks.add(id); }); userCollapsed = false; renderTasks(); document.body.dataset.taskAction = ''; }
}).observe(document.body, { attributes: true, attributeFilter: ['data-task-action'] });
function toggleResult(taskId) {
  if (expandedTasks.has(taskId)) { expandedTasks.delete(taskId); } else { expandedTasks.add(taskId); userCollapsed = false; }
  const el = document.getElementById('result-' + taskId);
  if (el) el.style.display = expandedTasks.has(taskId) ? 'block' : 'none';
}
window.toggleAllTasks = toggleAllTasks;
function toggleAllTasks() {
  const hasExpanded = expandedTasks.size > 0;
  if (hasExpanded) { expandedTasks.clear(); userCollapsed = true; }
  else { Object.entries(taskMap).forEach(([id, t]) => { if (t.result) expandedTasks.add(id); }); userCollapsed = false; }
  renderTasks();
  const link = document.querySelector('#tasks-header span:last-child');
  if (link) link.textContent = hasExpanded ? 'expand all' : 'collapse all';
}
document.addEventListener('click', function(e) {
  const item = e.target.closest && e.target.closest('.task-item[data-taskid]');
  if (item) toggleResult(item.dataset.taskid);
});
function renderTasks() {
  const container = $('tasks');
  const entries = Object.entries(taskMap);
  const hdr = $('tasks-header');
  if (entries.length === 0) { container.innerHTML = ''; if (hdr) hdr.style.display = 'none'; return; }
  if (hdr) hdr.style.display = 'flex';
  const sorted = entries.sort((a, b) => b[1].time - a[1].time).slice(0, 8);
  container.innerHTML = sorted.map(([id, t]) => {
    const icons = { pending: '&#8987;', working: '&#9881;', done: '&#10003;', error: '&#10007;' };
    const ago = Math.round((Date.now() - t.time) / 1000);
    const timeStr = ago < 60 ? ago + 's ago' : Math.round(ago / 60) + 'm ago';
    const hasResult = t.result && t.status === 'done';
    const clickAttr = hasResult ? ' data-taskid="' + id + '" style="cursor:pointer"' : '';
    const isExpanded = expandedTasks.has(id);
    const resultDisplay = isExpanded ? 'block' : 'none';
    const resultHtml = hasResult ? '<div id="result-' + id + '" style="display:' + resultDisplay + ';padding:6px 26px;color:#8ab4c8;font-size:11px;white-space:pre-wrap;word-break:break-word;background:#0d1520;border-radius:6px;margin:4px 0 4px 26px">' + t.result.replace(/</g,'&lt;') + '</div>' : '';
    return '<div class="task-item"' + clickAttr + '>' +
      '<div class="task-status ' + t.status + '">' + (icons[t.status] || '?') + '</div>' +
      '<span class="task-text">' + (t.text || id) + (hasResult ? (isExpanded ? ' ▾' : ' ▸') : '') + '</span>' +
      '<span class="task-time">' + timeStr + '</span>' +
      '</div>' + resultHtml;
  }).join('');
}

// ─── Poll agent API for task status ───────────────────────
let taskPollTimer = null;
function startTaskPolling() {
  if (taskPollTimer) return;
  taskPollTimer = setInterval(async () => {
    try {
      const hostname = window.location.hostname;
      const resp = await fetch('http://' + hostname + ':7843/tasks/active');
      const data = await resp.json();
      // Replace taskMap with API data (preserve expanded state and WebSocket-delivered results)
      const apiTasks = new Set();
      for (const t of (data.tasks || [])) {
        apiTasks.add(t.id);
        const existing = taskMap[t.id] || {};
        // Auto-expand latest completed task (collapse others if user had collapsed)
        if (t.status === 'done' && existing.status !== 'done' && (t.result || existing.result)) {
          if (userCollapsed) expandedTasks.clear();
          expandedTasks.add(t.id);
          userCollapsed = false;
        }
        taskMap[t.id] = { status: t.status, text: t.text, time: new Date(t.time * 1000), result: t.result || existing.result || '' };
      }
      // Remove tasks no longer in API (stale)
      for (const id of Object.keys(taskMap)) {
        if (!apiTasks.has(id) && taskMap[id].status === 'working') {
          delete taskMap[id];
        }
      }
      renderTasks();
      // Update system status indicators
      const statusParts = [];
      if (data.claude === false) statusParts.push('<span style="color:#e94560">brain offline</span>');
      if (data.watcher === false) statusParts.push('<span style="color:#f0ad4e">watcher offline</span>');
      const sysEl = document.getElementById('sys-status');
      if (sysEl) sysEl.innerHTML = statusParts.length ? statusParts.join(' · ') : '';
      // Show pending questions
      const qEl = document.getElementById('questions');
      if (qEl && data.questions?.length) {
        qEl.innerHTML = data.questions.map(q =>
          '<div style="padding:8px 0;border-bottom:1px solid #1a1a2e;display:flex;align-items:center;gap:8px">' +
          '<span style="color:#f0ad4e;font-size:14px">?</span>' +
          '<span style="color:#ccc;font-size:12px"><b>' + q.id + '</b>: ' + q.text + '</span>' +
          '</div>'
        ).join('');
        qEl.style.display = '';
      } else if (qEl) { qEl.style.display = 'none'; }
    } catch {}
  }, 3000);
}
function stopTaskPolling() {
  if (taskPollTimer) { clearInterval(taskPollTimer); taskPollTimer = null; }
}

// Start polling on page load
startTaskPolling();

function updateStats() {
  $('stats').textContent =
    'Sent ' + fmtBytes(bytesSent) + ' / Recv ' + fmtBytes(bytesRecv) +
    ' (' + audioChunksRecv + ' chunks, ' + playChunkCount + ' played)';
}

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

function saveDebug() {
  const data = {
    timestamp: new Date().toISOString(),
    config: { INPUT_RATE, OUTPUT_RATE, CAPTURE_BUF },
    audioCtxState: audioCtx?.state ?? null,
    audioCtxSampleRate: audioCtx?.sampleRate ?? null,
    bytesSent, bytesRecv, audioChunksRecv, playChunkCount,
    log: debugLog,
  };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'voice-debug-' + Date.now() + '.json';
  a.click();
  URL.revokeObjectURL(a.href);
  dbg('Debug data saved');
}

// ─── PCM helpers ──────────────────────────────────────────
function downsample(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const len = Math.floor(input.length / ratio);
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    const pos = i * ratio;
    const idx = Math.floor(pos);
    const frac = pos - idx;
    out[i] = input[idx] * (1 - frac) + (input[idx + 1] || 0) * frac;
  }
  return out;
}

function float32ToInt16(f32) {
  const i16 = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    i16[i] = s < 0 ? (s * 0x8000) | 0 : (s * 0x7FFF) | 0;
  }
  return i16;
}

function int16ToFloat32(buf) {
  const view = new DataView(buf);
  const len = buf.byteLength / 2;
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    out[i] = view.getInt16(i * 2, true) / 32768;
  }
  return out;
}

// ─── Audio playback (gapless scheduling) ──────────────────
function playChunk(arrayBuf) {
  if (!audioCtx || audioCtx.state === 'closed') {
    try {
      audioCtx = new AudioContext();
      dbg('playChunk: created new AudioContext: ' + audioCtx.sampleRate + ' Hz');
    } catch (e) {
      dbg('playChunk: failed to create AudioContext: ' + e, 'err');
      return;
    }
  }
  if (audioCtx.state === 'suspended') {
    audioCtx.resume();
    dbg('playChunk: resumed suspended audioCtx');
  }

  const f32 = int16ToFloat32(arrayBuf);
  if (f32.length === 0) return;

  try {
    const audioBuf = audioCtx.createBuffer(1, f32.length, OUTPUT_RATE);
    audioBuf.getChannelData(0).set(f32);

    const src = audioCtx.createBufferSource();
    src.buffer = audioBuf;
    src.playbackRate.value = playbackRate;
    src.connect(audioCtx.destination);

    const now = audioCtx.currentTime;
    if (nextPlayTime < now) {
      nextPlayTime = now + 0.05;
    }
    src.start(nextPlayTime);
    nextPlayTime += audioBuf.duration / playbackRate;
    activeSources.push(src);
    src.onended = () => {
      const idx = activeSources.indexOf(src);
      if (idx >= 0) activeSources.splice(idx, 1);
    };
    playChunkCount++;

    if (playChunkCount <= 5) {
      dbg('Played chunk #' + playChunkCount + ': ' + f32.length + ' samples, scheduled at ' + nextPlayTime.toFixed(3) + 's (ctx.state=' + audioCtx.state + ')', 'audio');
    }
  } catch (err) {
    dbg('playChunk error: ' + err.message, 'err');
  }
}

// ─── Microphone capture ───────────────────────────────────
async function startMic() {
  // Check if getUserMedia is available (requires HTTPS or localhost)
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    const isLocalhost = window.location.hostname === 'localhost' || 
                       window.location.hostname === '127.0.0.1' ||
                       window.location.hostname === '[::1]';
    const isHttps = window.location.protocol === 'https:';
    
    if (!isLocalhost && !isHttps) {
      throw new Error('Microphone access requires HTTPS. Please access this page via HTTPS (https://your-domain.com) or use localhost. Modern browsers block getUserMedia on HTTP for security.');
    } else {
      throw new Error('Microphone access is not available in this browser. Please use a modern browser that supports getUserMedia.');
    }
  }

  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    }
  });

  const trackSettings = micStream.getAudioTracks()[0].getSettings();
  dbg('Mic stream: ' + (trackSettings.sampleRate || '?') + ' Hz, device=' + (trackSettings.deviceId || '?').slice(0, 8));

  // Reuse AudioContext created in toggle() on user gesture
  if (!audioCtx || audioCtx.state === 'closed') {
    audioCtx = new AudioContext();
    dbg('Created new AudioContext: ' + audioCtx.sampleRate + ' Hz');
  }
  dbg('AudioContext state=' + audioCtx.state + ' sampleRate=' + audioCtx.sampleRate);

  if (audioCtx.state === 'suspended') {
    await audioCtx.resume();
    dbg('AudioContext resumed');
  }

  const source = audioCtx.createMediaStreamSource(micStream);

  processor = audioCtx.createScriptProcessor(CAPTURE_BUF, 1, 1);
  let sendCount = 0;
  processor.onaudioprocess = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const raw = e.inputBuffer.getChannelData(0);
    const down = downsample(raw, audioCtx.sampleRate, INPUT_RATE);
    const pcm = float32ToInt16(down);
    ws.send(pcm.buffer);
    bytesSent += pcm.buffer.byteLength;
    sendCount++;
    if (sendCount <= 3) {
      dbg('Sent mic #' + sendCount + ': ' + pcm.buffer.byteLength + 'B (' + down.length + ' samples @ ' + INPUT_RATE + 'Hz)', 'audio');
    }
  };

  source.connect(processor);
  const silence = audioCtx.createGain();
  silence.gain.value = 0;
  processor.connect(silence);
  silence.connect(audioCtx.destination);

  dbg('Mic capture started');
  reconnectAttempts = 0;
  addSystem('Microphone active — speak now.');

  // Start Chrome STT for real-time interim display (server final replaces)
  startChromeStt();
}

function stopMic() {
  stopChromeStt();
  if (processor) { processor.disconnect(); processor = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  // Don't close audioCtx here — playback may still be draining
}

// ─── WebSocket ────────────────────────────────────────────
function connectWs() {
  const url = $('wsUrl').value.trim();
  if (!url) return;

  dbg('Connecting to ' + url);
  setStatus('Connecting...', '');

  ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';

  ws.onopen = async () => {
    dbg('WebSocket connected');
    setStatus('Starting mic...', 'live');
    try {
      await startMic();
      setStatus('Live — speak now', 'live');
      statsTimer = setInterval(updateStats, 500);
    } catch (err) {
      dbg('Mic error: ' + err.message, 'err');
      setStatus('Mic error', 'error');
      addSystem('Microphone access denied. Please allow and retry.');
      ws.close();
    }
  };

  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      bytesRecv += event.data.byteLength;
      audioChunksRecv++;
      if (audioChunksRecv <= 5) {
        dbg('Recv audio #' + audioChunksRecv + ': ' + event.data.byteLength + 'B', 'audio');
      }
      playChunk(event.data);
    } else {
      try {
        const msg = JSON.parse(event.data);
        dbg('Recv: ' + JSON.stringify(msg), 'event');

        if (msg.type === 'session.config' && msg.audioFormat) {
          INPUT_RATE = msg.audioFormat.inputSampleRate;
          OUTPUT_RATE = msg.audioFormat.outputSampleRate;
          dbg('Audio format configured: input=' + INPUT_RATE + 'Hz output=' + OUTPUT_RATE + 'Hz', 'event');
        } else if (msg.type === 'transcript') {
          handleTranscript(msg.role, msg.text, msg.partial !== false);
        } else if (msg.type === 'turn.end') {
          // Remove orphaned Chrome STT interim — if server never finalized it,
          // it's echo from the assistant's voice picked up by mic.
          if (currentUserEl && currentUserEl.classList.contains('t-interim')) {
            currentUserEl.remove();
          }
          currentUserEl = null;
          currentAssistantEl = null;
          serverUserTextReceived = false;
        } else if (msg.type === 'turn.interrupted') {
          for (const s of activeSources) {
            try { s.stop(); } catch {}
          }
          activeSources = [];
          nextPlayTime = 0;
          if (currentUserEl && currentUserEl.classList.contains('t-interim')) {
            currentUserEl.remove();
          }
          currentUserEl = null;
          currentAssistantEl = null;
          serverUserTextReceived = false;
        } else if (msg.type === 'gui.update') {
          const guiData = msg.payload?.data;
          if (guiData?.type === 'subprocess_log' && guiData.line) {
            dbg('subprocess  ' + guiData.line, 'audio');
          } else if (guiData?.type === 'image' && guiData.base64) {
            const imgEl = document.createElement('div');
            imgEl.className = 't-entry t-system';
            const img = document.createElement('img');
            const imgDataUrl = 'data:' + (guiData.mimeType || 'image/png') + ';base64,' + guiData.base64;
            img.src = imgDataUrl;
            img.alt = guiData.description || 'Generated image';
            img.style.maxWidth = '100%';
            img.style.borderRadius = '8px';
            img.style.marginTop = '8px';
            imgEl.appendChild(img);
            const dlLink = document.createElement('a');
            dlLink.className = 'btn-download';
            dlLink.href = imgDataUrl;
            const ext = (guiData.mimeType || 'image/png').split('/')[1] || 'png';
            dlLink.download = 'generated-image-' + Date.now() + '.' + ext;
            dlLink.textContent = 'Download image';
            imgEl.appendChild(dlLink);
            $('transcript').appendChild(imgEl);
            $('transcript').scrollTop = $('transcript').scrollHeight;
            dbg('Image received via gui.update: ' + (guiData.description || '').slice(0, 50), 'event');
          } else if (guiData?.type === 'video' && guiData.base64) {
            const vidEl = document.createElement('div');
            vidEl.className = 't-entry t-system';
            const vidDataUrl = 'data:' + (guiData.mimeType || 'video/mp4') + ';base64,' + guiData.base64;
            const video = document.createElement('video');
            video.src = vidDataUrl;
            video.controls = true;
            video.autoplay = true;
            video.muted = true;
            video.style.maxWidth = '100%';
            video.style.borderRadius = '8px';
            video.style.marginTop = '8px';
            if (guiData.description) {
              const caption = document.createElement('div');
              caption.style.fontSize = '12px';
              caption.style.color = '#888';
              caption.style.marginTop = '4px';
              caption.textContent = guiData.description;
              vidEl.appendChild(caption);
            }
            vidEl.appendChild(video);
            const dlLink = document.createElement('a');
            dlLink.className = 'btn-download';
            dlLink.href = vidDataUrl;
            const vidExt = (guiData.mimeType || 'video/mp4').split('/')[1] || 'mp4';
            dlLink.download = 'generated-video-' + Date.now() + '.' + vidExt;
            dlLink.textContent = 'Download video';
            vidEl.appendChild(dlLink);
            $('transcript').appendChild(vidEl);
            $('transcript').scrollTop = $('transcript').scrollHeight;
            dbg('Video received via gui.update: ' + (guiData.description || '').slice(0, 50), 'event');
          } else {
            addSystem('[gui] ' + JSON.stringify(guiData));
          }
        } else if (msg.type === 'gui.command') {
          if (msg.command === 'collapse_tasks') { collapseAllTasks(); }
          else if (msg.command === 'expand_tasks') { Object.keys(taskMap).forEach(id => { if (taskMap[id].result) expandedTasks.add(id); }); renderTasks(); }
        } else if (msg.type === 'gui.notification') {
          addSystem('[notification] ' + (msg.payload?.message || ''));
        } else if (msg.type === 'image') {
          const imgEl = document.createElement('div');
          imgEl.className = 't-entry t-system';
          const img = document.createElement('img');
          const legacyDataUrl = 'data:' + (msg.data.mimeType || 'image/png') + ';base64,' + msg.data.base64;
          img.src = legacyDataUrl;
          img.alt = msg.data.description || 'Generated image';
          img.style.maxWidth = '100%';
          img.style.borderRadius = '8px';
          img.style.marginTop = '8px';
          imgEl.appendChild(img);
          const dlLink2 = document.createElement('a');
          dlLink2.className = 'btn-download';
          dlLink2.href = legacyDataUrl;
          const ext2 = (msg.data.mimeType || 'image/png').split('/')[1] || 'png';
          dlLink2.download = 'generated-image-' + Date.now() + '.' + ext2;
          dlLink2.textContent = 'Download image';
          imgEl.appendChild(dlLink2);
          $('transcript').appendChild(imgEl);
          $('transcript').scrollTop = $('transcript').scrollHeight;
          dbg('Image received: ' + (msg.data.description || '').slice(0, 50), 'event');
        } else if (msg.type === 'speech_speed') {
          const speeds = { slow: 0.85, normal: 1.0, fast: 1.2 };
          playbackRate = speeds[msg.speed] || 1.0;
          addSystem('[speed] Speech speed set to ' + msg.speed + ' (' + playbackRate + 'x)');
        } else if (msg.type === 'session_end') {
          addSystem('Session ended by voice command.');
          dbg('session_end received — disconnecting', 'event');
          connected = false; // prevent auto-reconnect
          if (ws) { ws.close(); ws = null; }
          doCleanup();
        } else if (msg.type === 'task.status') {
          updateTask(msg.taskId, msg.status, msg.text, msg.result);
        } else if (msg.type === 'grounding') {
          const chunks = msg.payload?.groundingChunks;
          if (Array.isArray(chunks) && chunks.length > 0) {
            const sources = chunks.map(c => c.web?.title || c.web?.uri || '').filter(Boolean).join(', ');
            if (sources) addSystem('[sources] ' + sources);
          }
        }
      } catch {
        dbg('Bad JSON text frame', 'warn');
      }
    }
  };

  ws.onclose = (e) => {
    dbg('WS closed: code=' + e.code + ' reason=' + e.reason);
    // Server-initiated clean close (goodbye code 4000) or user clicked Disconnect
    const wasCleanDisconnect = !connected || e.code === 4000;
    if (wasCleanDisconnect) connected = false;
    doCleanup();
    if (wasCleanDisconnect) {
      addSystem('Disconnected.');
    } else {
      // Unexpected drop (Gemini timeout) — auto-reconnect with limit
      reconnectAttempts++;
      if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
        addSystem('Could not connect to the voice agent after ' + MAX_RECONNECT_ATTEMPTS + ' attempts.');
        addSystem('Check the terminal where you ran startup.sh — that is Sutando\'s core CLI. You can type commands there directly.');
        addSystem('To restart all services: bash src/restart.sh');
        setStatus('Disconnected', 'error');
        connected = false;
        reconnectAttempts = 0;
      } else {
        addSystem('Connection lost — reconnecting (' + reconnectAttempts + '/' + MAX_RECONNECT_ATTEMPTS + ')...');
        setStatus('Reconnecting...', 'error');
        setTimeout(() => {
          if (!connected) {
            dbg('Auto-reconnecting (attempt ' + reconnectAttempts + ')...');
            toggle();
          }
        }, 3000);
      }
    }
  };

  ws.onerror = () => {
    dbg('WS error', 'err');
    setStatus('Connection failed', 'error');
    addSystem('Connection error — is the agent server running?');
  };
}

function doCleanup() {
  stopMic();
  if (audioCtx && audioCtx.state !== 'closed') {
    // Close audio context immediately — don't use a delayed timeout
    // (a delayed null can race with reconnect and kill the new AudioContext)
    if (audioCtx) { try { audioCtx.close(); } catch {} audioCtx = null; }
  }
  setStatus('Text only', '');
  connected = false;
  muted = false;
  document.body.classList.remove('voice-active');
  $('hero').style.display = '';
  $('btn').style.display = 'none';
  $('btn-mute').style.display = 'none';
  $('voice-status').className = 'status-pill voice-off';
  try { sessionStorage.removeItem('sutando-voice'); } catch {}
  if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
  updateStats();
}

// ─── Mute toggle ──────────────────────────────────────────
function toggleMute() {
  if (!micStream) return;
  muted = !muted;
  micStream.getAudioTracks().forEach(t => { t.enabled = !muted; });
  const btn = document.getElementById('btn-mute');
  btn.textContent = muted ? 'Unmute' : 'Mute';
  btn.className = muted ? 'btn-mute muted' : 'btn-mute';
  addSystem(muted ? 'Microphone muted.' : 'Microphone unmuted.');
}

// ─── UI toggle (user gesture context!) ────────────────────
function toggle() {
  if (connected) {
    if (ws) { ws.close(); ws = null; }
    doCleanup();
  } else {
    // Create AudioContext HERE in the click handler so browsers allow playback
    // Use system default sample rate — it handles resampling from OUTPUT_RATE internally
    audioCtx = new AudioContext();
    dbg('AudioContext created on click: state=' + audioCtx.state + ' sampleRate=' + audioCtx.sampleRate);

    // Reset counters
    nextPlayTime = 0;
    bytesSent = 0;
    bytesRecv = 0;
    audioChunksRecv = 0;
    playChunkCount = 0;

    connected = true;
    muted = false;
    document.body.classList.add('voice-active');
    $('hero').style.display = 'none';
    $('btn').style.display = '';
    $('btn').textContent = 'End Voice';
    $('btn').className = 'btn-voice active';
    $('btn-mute').style.display = '';
    $('btn-mute').textContent = 'Mute';
    $('btn-mute').className = 'btn-mute';
    $('voice-status').className = 'status-pill voice-on';
    $('status').textContent = 'Voice active';
    try { sessionStorage.setItem('sutando-voice', '1'); } catch {}
    connectWs();
  }
}

// ─── Suggestion chips ─────────────────────────────────────
function trySuggestion(el) {
  const text = el.textContent.replace(/^"|"$/g, '');
  $('textInput').value = text;
  // If voice is connected, start voice first then it'll go through voice
  if (!connected) {
    // Send as text task
    sendText();
  } else {
    sendText();
  }
}

// ─── Text input ──────────────────────────────────────────
function sendText() {
  const input = $('textInput');
  const text = input.value.trim();
  if (!text) return;

  // Show typed text in the conversation
  currentUserEl = null;
  const el = document.createElement('div');
  el.className = 't-entry t-user';
  el.textContent = text;
  $('transcript').appendChild(el);
  $('transcript').scrollTop = $('transcript').scrollHeight;
  input.value = '';

  if (ws && ws.readyState === WebSocket.OPEN) {
    // Voice connected — send through voice agent
    ws.send(JSON.stringify({ type: 'text_input', text }));
    dbg('Sent text via voice: "' + text.slice(0, 50) + '"', 'event');
  } else {
    // Voice disconnected — route through task bridge (same as Telegram/Discord)
    const apiBase = 'http://' + location.hostname + ':7843';
    fetch(apiBase + '/task', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ from: 'web', task: text }) })
      .then(r => r.json())
      .then(d => {
        if (d.ok) {
          dbg('Sent text via task bridge: ' + d.task_id, 'event');
          // Poll for result
          const poll = setInterval(() => {
            fetch(apiBase + '/result/' + d.task_id).then(r => r.json()).then(r => {
              if (r.status === 'completed') {
                clearInterval(poll);
                const re = document.createElement('div');
                re.className = 't-entry t-assistant';
                re.textContent = r.result;
                $('transcript').appendChild(re);
                $('transcript').scrollTop = $('transcript').scrollHeight;
              }
            }).catch(() => {});
          }, 2000);
        }
      })
      .catch(() => {
        const err = document.createElement('div');
        err.className = 't-entry t-assistant';
        err.textContent = '(Failed to send — agent API not reachable)';
        $('transcript').appendChild(err);
      });
  }
}

</script>
</body>
</html>`;

const server = createServer((_req, res) => {
	res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
	res.end(HTML);
});

server.listen(HTTP_PORT, HTTP_HOST, () => {
	const serverUrl = HTTP_HOST === '0.0.0.0' 
		? `http://localhost:${HTTP_PORT} (or use your server's IP/DNS)`
		: `http://${HTTP_HOST}:${HTTP_PORT}`;
	console.log(`\n  Sutando — Web Client`);
	console.log(`  ────────────────────────────────`);
	console.log(`  Open in browser:  ${serverUrl}`);
	console.log(`  WebSocket URL:    Auto-detected from browser hostname`);
	console.log(`  WebSocket port:  ${WS_PORT}`);
	console.log(`\n  Press Ctrl+C to stop.\n`);
});

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
import { writeFileSync } from 'node:fs';

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
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-user-select: text; user-select: text; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0a0a12; color: #c0c0d0;
    display: flex; flex-direction: column; align-items: center;
    min-height: 100vh; padding: 0 0 60px 0;
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
  .main { width: 100%; max-width: 960px; flex: 1; display: flex; flex-direction: column; padding: 12px 24px 80px; margin: 0 auto; }

  /* Conversation */
  #transcript {
    min-height: 80px; max-height: 30vh;
    background: #0e0e18; border-radius: 12px; padding: 10px 14px;
    overflow-y: auto; font-size: 13px; line-height: 1.6;
    margin-bottom: 6px;
  }
  .t-entry { margin-bottom: 6px; position: relative; user-select: text; }
  .t-entry .copy-btn {
    display: none; position: absolute; right: 0; top: 0;
    background: #1e1e30; border: 1px solid #2a2a40; color: #666; font-size: 10px;
    padding: 2px 6px; border-radius: 4px; cursor: pointer;
  }
  .t-entry:hover .copy-btn { display: inline-block; }
  .t-entry .copy-btn:hover { color: #4ecca3; border-color: #4ecca3; }
  .t-user { color: #7fb3e0; }
  .t-user::before { content: 'You: '; font-weight: 600; color: #5a9fd4; }
  .t-assistant { color: #a8d8b0; }
  .t-assistant::before { content: 'Sutando: '; font-weight: 600; color: #6dbe82; }
  .t-system { color: #666; font-size: 12px; }
  .t-interim { color: #7fb3e0; opacity: 0.5; font-size: 13px; }
  .t-interim::before { content: 'You: '; font-weight: 600; }

  /* Input bar */
  #bottom-panel {
    position: fixed; bottom: 0; left: 0; right: 0; max-width: 960px; margin: 0 auto;
    background: #12121e; z-index: 10;
    border-top: 1px solid #1e1e30;
    padding: 8px 16px 12px;
  }
  .input-bar {
    display: flex; gap: 8px;
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

  /* Dynamic region */
  #dynamic-region { padding: 0 16px 8px; width: 100%; box-sizing: border-box; user-select: text; -webkit-user-select: text; }
  #dynamic-region:empty { display: none; }
  #core-status-bar { font-size: 11px; color: #555; }
  #core-status-bar:empty { display: none; }
  #core-status-bar .core-running { color: #4ecca3; }
  #core-status-bar .core-idle { color: #444; }
  #dynamic-region .dr-questions {
    background: linear-gradient(135deg, #1e1a12, #2a2218); border: 1px solid #f0ad4e44;
    border-radius: 10px; padding: 12px 16px; font-size: 13px; box-shadow: 0 0 12px #f0ad4e22;
  }
  #dynamic-region .dr-questions .q-title { color: #f0ad4e; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  #dynamic-region .dr-questions .q-item { color: #ddd; padding: 8px 0; border-bottom: 1px solid #2e281844; }
  #dynamic-region .dr-questions .q-item:last-child { border-bottom: none; }
  #dynamic-region .q-actions { margin-top: 8px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  #dynamic-region .q-btn {
    padding: 4px 14px; border-radius: 14px; font-size: 11px; cursor: pointer;
    border: 1px solid #2e2818; background: #1e1a12; color: #ccc; transition: all 0.15s;
  }
  #dynamic-region .q-btn:hover { background: #2e2818; border-color: #f0ad4e66; }
  #dynamic-region .q-btn.q-yes { border-color: #4ecca366; color: #4ecca3; }
  #dynamic-region .q-btn.q-yes:hover { background: #1e4028; }
  #dynamic-region .q-btn.q-no { border-color: #e9456066; color: #e94560; }
  #dynamic-region .q-btn.q-no:hover { background: #3a1520; }
  #dynamic-region .q-input {
    flex: 1; min-width: 120px; padding: 4px 10px; border-radius: 14px; font-size: 11px;
    border: 1px solid #2e2818; background: #12100a; color: #ccc; outline: none;
  }
  #dynamic-region .q-input:focus { border-color: #f0ad4e66; }
  #dynamic-region .dr-proactive { text-align: center; padding: 8px; font-size: 13px; color: #8899a6; }
  #dynamic-region .dr-chips { text-align: center; }
  #dynamic-region .dr-chips .suggestions-label { margin-bottom: 6px; }
  #dynamic-region .dr-chips .suggestion {
    display: inline-block; background: #1a1a2e; border: 1px solid #2a2a4e;
    border-radius: 16px; padding: 6px 14px; margin: 3px; font-size: 11px;
    color: #8899a6; cursor: pointer; transition: all 0.2s;
  }
  #dynamic-region .dr-chips .suggestion:hover { background: #2a2a4e; color: #ccc; border-color: #4a4a6e; }
  #dynamic-region .dr-media {
    background: #12121e; border: 1px solid #1e1e30; border-radius: 10px;
    padding: 12px 16px; text-align: center;
  }
  #dynamic-region .dr-media-title { color: #ccc; font-size: 14px; font-weight: 600; margin-bottom: 8px; }
  #dynamic-region .dr-media-caption { color: #666; font-size: 11px; margin-top: 6px; }
  #dynamic-region .dr-document {
    background: #12121e; border: 1px solid #1e1e30; border-radius: 10px; padding: 12px 16px;
  }
  #dynamic-region .dr-doc-body { color: #bbb; font-size: 13px; line-height: 1.5; white-space: pre-wrap; }

  /* Section labels */
  .section-label {
    font-size: 10px; color: #444; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 4px; margin-top: 4px;
  }

  /* Debug */
  #debug {
    background: #08080f; border-radius: 10px; padding: 10px 12px;
    max-height: 30vh; overflow-y: auto; font-size: 10px; line-height: 1.6;
    font-family: 'SF Mono', 'Fira Code', monospace;
    margin-bottom: 10px;
  }
  #debug-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 6px;
  }
  #debug-header .debug-actions { display: flex; gap: 8px; }
  #debug-header .debug-actions button {
    background: none; border: 1px solid #222; color: #555; font-size: 10px;
    padding: 2px 8px; border-radius: 4px; cursor: pointer;
  }
  #debug-header .debug-actions button:hover { color: #aaa; border-color: #444; }
  .d-entry { color: #555; padding: 1px 0; }
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
    transition: all 0.8s ease;
  }
  .hero h2 { color: #fff; font-size: 1.3em; font-weight: 500; margin-bottom: 4px; transition: all 0.6s ease; }
  .hero .tagline { color: #555; font-size: 13px; margin-bottom: 24px; transition: all 0.6s ease; }
  @keyframes avatar-glow {
    0% { box-shadow: 0 0 0 rgba(78,204,163,0); transform: scale(0.9); opacity: 0; }
    40% { box-shadow: 0 0 40px rgba(78,204,163,0.7); transform: scale(1.05); opacity: 1; }
    70% { box-shadow: 0 0 20px rgba(78,204,163,0.4); transform: scale(1); }
    100% { box-shadow: 0 0 15px rgba(78,204,163,0.25); transform: scale(1); opacity: 1; }
  }
  @keyframes fade-up { from { opacity: 0; transform: translateY(15px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes pulse-glow { 0%,100% { box-shadow: 0 0 12px rgba(78,204,163,0.2); } 50% { box-shadow: 0 0 20px rgba(78,204,163,0.35); } }
  .identity-reveal .avatar-hero { animation: avatar-glow 2s ease-out forwards, pulse-glow 3s ease-in-out 2.5s infinite; opacity: 1 !important; }
  .identity-reveal h2 { animation: fade-up 0.8s ease-out 0.8s both; }
  .identity-reveal .tagline { animation: fade-up 0.8s ease-out 1.2s both; }
  .btn-hero {
    background: #1e5128; color: #fff; padding: 14px 36px; font-size: 15px; font-weight: 600;
    border: 1px solid #2a7a3a; border-radius: 14px;
    box-shadow: 0 0 20px rgba(78, 204, 163, 0.2);
    cursor: pointer; transition: all 0.2s;
  }
  .btn-hero:hover { background: #277334; box-shadow: 0 0 28px rgba(78, 204, 163, 0.35); transform: scale(1.02); }
  /* When voice is active, hide hero */
  body.voice-active .hero { display: none; }
  body.voice-active .main { display: flex; }
  /* Toast notifications */
  .toast-container {
    position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%);
    z-index: 100; display: flex; flex-direction: column; gap: 6px; align-items: center;
  }
  .toast {
    background: #1a2e24; border: 1px solid #2a4a36; color: #c0c0d0;
    padding: 10px 16px; border-radius: 10px; font-size: 12px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    animation: toastIn 0.3s ease, toastOut 0.3s ease 3.7s forwards;
    max-width: 400px; text-align: center;
  }
  .toast .toast-label { color: #4ecca3; font-weight: 600; }
  @keyframes toastIn { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes toastOut { from { opacity: 1; } to { opacity: 0; transform: translateY(-8px); } }
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
  </div>
</div>
<input type="text" id="wsUrl" value="${DEFAULT_WS_URL}" />
<script>
fetch('http://localhost:7844/stand-identity').then(r=>r.json()).then(s=>{
  if(s.name){
    document.getElementById('stand-name').textContent='Sutando — '+s.name;
    document.getElementById('hero-name').textContent='Sutando — '+s.name;
  }
  if(s.nameOrigin){
    var t=document.querySelector('.tagline');
    if(t) t.textContent=s.nameOrigin.split(' — ')[1]||s.nameOrigin;
  }
  if(s.avatarGenerated){
    document.getElementById('stand-avatar').style.display='block';
    var ha=document.getElementById('hero-avatar');
    if(ha){ha.style.display='block';ha.style.opacity='0';}
  }
  if(s.name || s.avatarGenerated){
    var hero=document.getElementById('hero');
    if(hero){
      requestAnimationFrame(function(){
        hero.classList.add('identity-reveal');
        var ha2=document.getElementById('hero-avatar');
        if(ha2) ha2.style.opacity='1';
      });
    }
  }
}).catch(()=>{});
</script>

<div class="hero" id="hero">
  <img class="avatar-hero" id="hero-avatar" src="http://localhost:7844/avatar">
  <h2 id="hero-name">Sutando</h2>
  <p class="tagline">Summon your AI superpower</p>
  <button class="btn-hero" onclick="toggle()">Start Voice</button>
</div>

<div id="status-bar" style="text-align:center;font-size:11px;color:#556;letter-spacing:0.5px;padding:12px 16px">
  <kbd style="background:#1a1a2e;padding:2px 6px;border-radius:3px;border:1px solid #333;font-family:monospace;color:#8af">⌃C</kbd> drop context
  <span style="margin:0 6px;color:#333">|</span>
  <kbd style="background:#1a1a2e;padding:2px 6px;border-radius:3px;border:1px solid #333;font-family:monospace;color:#8af">⌃V</kbd> voice
  <span style="margin:0 6px;color:#333">|</span>
  <kbd style="background:#1a1a2e;padding:2px 6px;border-radius:3px;border:1px solid #333;font-family:monospace;color:#8af">⌃M</kbd> mute
  <span style="margin:0 6px;color:#333">|</span>
  <span id="core-status-bar" style="display:inline"></span>
</div>

<div id="dynamic-region"></div>

<div class="main" id="main-area">

<div class="toast-container" id="toast-container"></div>
<div id="bottom-panel">
<div id="transcript">
  <div class="t-entry t-system">Ask Sutando anything.</div>
</div>

<div class="input-bar">
  <input type="text" id="textInput" placeholder="Type a message..." onkeydown="if(event.key==='Enter')sendText()" />
  <button class="btn-send" onclick="sendText()">Send</button>
</div>
</div>

<div id="tasks-header" style="display:none"></div>
<div id="tasks" style="display:none"></div>

<div class="section-label" style="cursor:pointer" onclick="$('debug').style.display=$('debug').style.display==='none'?'':'none'">Debug</div>
<div id="debug" style="display:none">
  <div id="debug-header">
    <span style="color:#666;font-size:10px">Voice session log</span>
    <div class="debug-actions">
      <button onclick="$('debug').querySelectorAll('.d-entry').forEach(function(e){e.remove()});debugLog.length=0">Clear</button>
      <button onclick="saveDebug()">Export</button>
    </div>
  </div>
</div>

<div style="height:80px"></div>
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

// ─── Remote toggle via SSE ────────────────────────────────
(function initRemoteToggle() {
  const evtSource = new EventSource('/sse');
  evtSource.addEventListener('toggle-voice', () => toggle());
  evtSource.addEventListener('toggle-mute', () => toggleMute());
  evtSource.onerror = () => setTimeout(() => initRemoteToggle(), 5000);
})();

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

function addCopyBtn(el) {
  const btn = document.createElement('span');
  btn.className = 'copy-btn';
  btn.textContent = 'Copy';
  btn.onclick = function(e) {
    e.stopPropagation();
    navigator.clipboard.writeText(el.textContent.replace(/^(You: |Sutando: )/, '').replace(/Copy$/, '').trim());
    btn.textContent = 'Copied';
    setTimeout(function() { btn.textContent = 'Copy'; }, 1500);
  };
  el.appendChild(btn);
}

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
      addCopyBtn(currentUserEl);
      currentUserEl = null;
    }
  } else {
    if (!currentAssistantEl) {
      currentAssistantEl = document.createElement('div');
      currentAssistantEl.className = 't-entry t-assistant';
      $('transcript').appendChild(currentAssistantEl);
    }
    currentAssistantEl.textContent = text;
    if (!partial) { addCopyBtn(currentAssistantEl); currentAssistantEl = null; }
  }
  $('transcript').scrollTop = $('transcript').scrollHeight;
}

function addSystem(text, isHtml) {
  const el = document.createElement('div');
  el.className = 't-entry t-system';
  if (isHtml) { el.innerHTML = text; } else { el.textContent = text; }
  addCopyBtn(el);
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
  const isNew = !existing.status;
  const wasDone = existing.status === 'done';
  taskMap[taskId] = { status, text: text || existing.text, time: new Date(), result: result || existing.result || '' };
  // Auto-switch to tasks tab if new task arrives and user is on starter
  if (isNew && window._drActiveTab === 'starter') { switchDRTab('tasks'); }
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
  window._drTaskCount = entries.length;
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

// ─── Toast notifications ────────────────────────────────
function showToast(msg) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = msg;
  container.appendChild(el);
  setTimeout(() => { if (el.parentNode) el.remove(); }, 4000);
}
const knownTaskIds = new Set(Object.keys(taskMap));

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
        // Toast for new tasks
        if (!knownTaskIds.has(t.id)) {
          knownTaskIds.add(t.id);
          const snippet = (t.text || '').slice(0, 60);
          showToast('<span class="toast-label">Context received</span> ' + snippet);
        }
        // Toast for completed tasks
        if (t.status === 'done' && existing.status && existing.status !== 'done') {
          showToast('<span class="toast-label">Done</span> ' + (t.text || t.id).slice(0, 60));
        }
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
      // Update dynamic region with latest data
      window._drQuestions = data.questions || [];
      updateDynamicRegion();
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
      addSystem('Microphone access denied. Please allow mic in browser settings and retry.');
      connected = false;  // prevent auto-reconnect loop
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
    // Always reset connected here so subsequent toggle calls (from auto-
    // reconnect or the external SSE toggle path) take the open-new-ws
    // branch instead of seeing stale state. Without this, an unclean drop
    // (e.g. voice-agent restart) leaves the page in a wedged state where
    // ws=null but connected=true, requiring a hard reload to recover.
    connected = false;
    doCleanup();
    if (wasCleanDisconnect) {
      addSystem('Disconnected.');
    } else {
      // Unexpected drop (Gemini timeout, voice-agent restart) — auto-reconnect with limit
      reconnectAttempts++;
      if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
        addSystem('Still trying to connect. Common causes:');
        addSystem('1. GEMINI_API_KEY not set — edit .env and add your key from ai.google.dev');
        addSystem('2. Voice agent not running — run: bash src/startup.sh');
        addSystem('3. Port 9900 blocked — check: lsof -i :9900');
        addSystem('You can type commands below while reconnecting.');
        addSystem('<a href="https://discord.gg/uZHWXXmrCS" target="_blank" style="color:#5865F2">Ask for help on Discord</a> · <a href="https://github.com/sonichi/sutando/issues" target="_blank" style="color:#4ecca3">Report an issue</a> · <span style="color:#8899a6;cursor:pointer;text-decoration:underline" onclick="copyLogs()">Copy logs</span>', true);
        setStatus('Reconnecting...', 'error');
        reconnectAttempts = 0;  // reset counter and keep retrying
      } else {
        addSystem('Connection lost — reconnecting (' + reconnectAttempts + '/' + MAX_RECONNECT_ATTEMPTS + ')...');
        setStatus('Reconnecting...', 'error');
      }
      // Always retry — connected is now false, so toggle() will open a fresh ws
      setTimeout(() => {
        if (!connected) {
          dbg('Auto-reconnecting (attempt ' + reconnectAttempts + ')...');
          toggle();
        }
      }, 3000);
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
    // Create AudioContext if not already created (may exist from page load or prior toggle)
    if (!audioCtx || audioCtx.state === 'closed') {
      audioCtx = new AudioContext();
    } else if (audioCtx.state === 'suspended') {
      audioCtx.resume();
    }
    dbg('AudioContext: state=' + audioCtx.state + ' sampleRate=' + audioCtx.sampleRate);

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
window.toggle = toggle;

// ─── Suggestion chips ─────────────────────────────────────
function copyLogs() {
  var apiBase = 'http://' + location.hostname + ':7843';
  fetch(apiBase + '/logs/voice').then(function(r) { return r.json(); }).then(function(d) {
    var text = (d.lines || []).join(String.fromCharCode(10));
    navigator.clipboard.writeText(text).then(function() {
      addSystem('Logs copied to clipboard (last 30 lines). Paste in Discord or GitHub issue.');
    });
  }).catch(function() { addSystem('Could not fetch logs — is the agent API running?'); });
}

function answerQuestion(qid, answer) {
  if (!answer || !answer.trim()) return;
  const apiBase = 'http://' + location.hostname + ':7843';
  fetch(apiBase + '/answer', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: qid, answer: answer.trim()})
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      // Show answered state on the question briefly before removing
      var qItem = document.querySelector('[data-qid="' + qid + '"]');
      var qParent = qItem ? qItem.closest('.q-item') : null;
      if (qParent) {
        var actions = qParent.querySelector('.q-actions');
        if (actions) actions.innerHTML = '<span style="color:#4ecca3;font-size:12px">Answered: ' + esc(answer.trim()) + '</span>';
      }
      // Remove after brief delay so user sees confirmation
      setTimeout(function() {
        window._drQuestions = (window._drQuestions || []).filter(function(q) { return q.id !== qid; });
        updateDynamicRegion();
      }, 1500);
      // Show in transcript too
      var el = document.createElement('div');
      el.className = 't-entry t-system';
      el.textContent = 'Answered ' + qid + ': ' + answer.trim();
      document.getElementById('transcript').appendChild(el);
    } else {
      alert('Failed: ' + (d.error || 'unknown error'));
    }
  }).catch(() => { alert('Could not reach agent API'); });
}

function trackChipUsage(label) {
  try {
    var usage = JSON.parse(localStorage.getItem('sutando_chip_usage') || '{}');
    usage[label] = (usage[label] || 0) + 1;
    localStorage.setItem('sutando_chip_usage', JSON.stringify(usage));
  } catch(e) {}
}

function getChipUsage() {
  try { return JSON.parse(localStorage.getItem('sutando_chip_usage') || '{}'); } catch(e) { return {}; }
}

function trySuggestion(el) {
  // Extract only the quoted command (e.g. "summon" from '"summon" — description')
  const raw = el.textContent;
  const dashIdx = raw.indexOf(' — ');
  const cmd = dashIdx > 0 ? raw.slice(0, dashIdx) : raw;
  const text = cmd.replace(/[\u201C\u201D"]/g, '').trim();
  // Track usage
  trackChipUsage(text);
  // Handle special actions
  if (text === 'Show questions') { switchDRTab('questions'); return; }
  if (text === 'Notes') { showNotesInDR(); return; }
  $('textInput').value = text;
  sendText();
}

function showNotesInDR() { switchDRTab('notes'); }

function showNoteInDR(slug) { showNoteContent(slug); }

function toggleActivity() { switchDRTab('activity'); }
window.toggleActivity = toggleActivity;

// Expose notes functions to global scope for onclick handlers
window.showNotesInDR = showNotesInDR;
window.showNoteInDR = showNoteInDR;

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
                addCopyBtn(re);
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

// ─── Dynamic region: contextual generative UI ────────────
// Priority: dynamic-content.json > pending questions > proactive status > chips
// Supports: audio, image, video, document, html, and fallback chips
window._drQuestions = [];
window._drProactive = null;
window._drContent = null;
const API_BASE = 'http://' + window.location.hostname + ':7843';
function getSuggestionChips() {
  var h = new Date().getHours();
  var usage = getChipUsage();
  var chips = [];
  // Time-based
  if (h < 12) chips.push({label: 'Morning briefing'});
  else chips.push({label: 'What is on my calendar today?'});
  // Always useful
  chips.push({label: 'Check my email'});
  chips.push({label: 'What is on my screen?'});
  // Actions (work via text or voice)
  chips.push({label: 'Summon', desc: 'share screen on Zoom'});
  chips.push({label: 'Join my next meeting'});
  // Productivity
  chips.push({label: 'Take a note'});
  chips.push({label: 'Read my reminders'});
  chips.push({label: 'Show tasks'});
  chips.push({label: 'Show notes'});
  // Evening wind-down
  if (h >= 17) chips.push({label: 'What did I accomplish today?'});
  // Voice disconnect
  if (connected) chips.push({label: 'Bye', desc: 'disconnect voice'});
  else chips.push({label: 'Tutorial'});
  // Contextual: pending questions badge
  var qCount = (window._drQuestions || []).length;
  if (qCount > 0) chips.unshift({label: 'Show questions', desc: qCount + ' pending'});
  // Contextual chips from core agent (written each loop pass)
  var ctxChips = (window._contextualChips || []).slice().reverse();
  ctxChips.forEach(function(c) { chips.unshift(c); });
  // Sort static chips by usage frequency, keep contextual + time-based at top
  var contextCount = ctxChips.length + 1; // +1 for time-based chip
  var pinned = chips.slice(0, contextCount);
  var rest = chips.slice(contextCount);
  rest.sort(function(a, b) { return (usage[b.label] || 0) - (usage[a.label] || 0); });
  return pinned.concat(rest);
}

function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function renderDynamicContent(c) {
  const media = API_BASE + '/media/';
  const src = c.src || '';
  const fullSrc = src.startsWith('http') ? src : media + src;
  const title = c.title ? '<div class="dr-media-title">' + esc(c.title) + '</div>' : '';
  const caption = c.caption ? '<div class="dr-media-caption">' + esc(c.caption) + '</div>' : '';

  switch (c.type) {
    case 'audio':
      return '<div class="dr-media">' + title +
        '<audio controls autoplay style="width:100%"><source src="' + fullSrc + '"></audio>' +
        caption + '</div>';
    case 'image':
      return '<div class="dr-media">' + title +
        '<img src="' + fullSrc + '" style="max-width:100%;border-radius:8px">' +
        caption + '</div>';
    case 'video':
      if (src.includes('youtu')) {
        var vid = src.match(/(?:v=|youtu\\.be\\/)([\\w-]+)/);
        if (vid) return '<div class="dr-media">' + title +
          '<iframe width="100%" height="280" src="https://www.youtube.com/embed/' + vid[1] +
          '" frameborder="0" allowfullscreen style="border-radius:8px"></iframe>' +
          caption + '</div>';
      }
      return '<div class="dr-media">' + title +
        '<video controls autoplay style="max-width:100%;border-radius:8px"><source src="' + fullSrc + '"></video>' +
        caption + '</div>';
    case 'document':
      return '<div class="dr-document">' + title +
        '<div class="dr-doc-body">' + (c.content || '') + '</div>' +
        caption + '</div>';
    case 'html':
      return '<div class="dr-media">' + (c.content || '') + '</div>';
    default:
      return '<div class="dr-media">' + title + '<p>' + (c.content || JSON.stringify(c)) + '</p></div>';
  }
}

window._drActiveTab = window._drActiveTab || 'starter';
window._drTaskCount = 0;
window._drTabsRendered = false;

function switchDRTab(tab) {
  window._drActiveTab = tab;
  window._drLocalContent = true; // prevent poll from clearing content
  updateTabHighlights();
  renderTabContent();
}
window.switchDRTab = switchDRTab;

function ensureTabStructure() {
  var dr = document.getElementById('dynamic-region');
  if (!dr) return;
  if (!document.getElementById('dr-tabs')) {
    dr.innerHTML = '<div id="dr-tabs" style="display:flex;gap:2px;margin-bottom:8px"></div>' +
      '<div id="dr-content" style="min-width:100%;word-wrap:break-word;overflow-wrap:break-word;user-select:text;-webkit-user-select:text;cursor:text"></div>';
  }
  updateTabHighlights();
}

// Track last-seen counts per tab to detect new items
window._lastSeenCounts = window._lastSeenCounts || {};
function updateTabHighlights() {
  var tabsEl = document.getElementById('dr-tabs');
  if (!tabsEl) return;
  var active = window._drActiveTab;
  var questions = window._drQuestions || [];
  var taskCount = window._drTaskCount || 0;
  var noteCount = window._drNoteCount || 0;
  var seen = window._lastSeenCounts;
  // Mark current tab as seen
  if (active === 'tasks') seen.tasks = taskCount;
  if (active === 'questions') seen.questions = questions.length;
  if (active === 'notes') seen.notes = noteCount;
  var hasNewTasks = taskCount > (seen.tasks || 0);
  var hasNewQuestions = questions.length > (seen.questions || 0);
  var dot = '<span style="color:#4ecca3;font-size:8px;margin-left:2px">●</span>';
  var tabs = [
    {id:'starter', label:'Starter'},
    {id:'tasks', label:'Tasks' + (taskCount > 0 ? ' (' + taskCount + ')' : '') + (hasNewTasks ? dot : '')},
    {id:'notes', label:'Notes' + (noteCount > 0 ? ' (' + noteCount + ')' : '')},
    {id:'questions', label:'Questions' + (questions.length > 0 ? ' (' + questions.length + ')' : '') + (hasNewQuestions ? dot : '')},
    {id:'activity', label:'Activity'},
  ];
  tabsEl.style.display = 'flex';
  tabsEl.style.gap = '2px';
  tabsEl.innerHTML = tabs.map(function(t) {
    var isActive = t.id === active;
    var bg = isActive ? '#2a2a4e' : 'transparent';
    var fg = isActive ? '#ccc' : '#666';
    var border = isActive ? '#4a4a6e' : '#2a2a3e';
    if (t.id === 'questions' && questions.length > 0 && !isActive) fg = '#f0ad4e';
    if (t.id === 'tasks' && hasNewTasks && !isActive) fg = '#4ecca3';
    return '<span onclick="switchDRTab(&quot;' + t.id + '&quot;)" style="cursor:pointer;padding:4px 0;border-radius:12px;font-size:11px;border:1px solid ' + border + ';background:' + bg + ';color:' + fg + ';flex:1;text-align:center">' + t.label + '</span>';
  }).join('');
}

function renderTabContent() {
  var container = document.getElementById('dr-content');
  if (!container) return;
  var tab = window._drActiveTab;

  if (tab === 'starter') {
    container.innerHTML = '<div class="dr-chips">' +
      '<div class="suggestions-label" style="font-size:11px;color:#666;margin-bottom:4px">Try saying or typing</div>' +
      getSuggestionChips().map(function(c) {
        return '<span class="suggestion" onclick="trySuggestion(this)">' +
          c.label + (c.desc ? ' — ' + c.desc : '') + '</span>';
      }).join('') + '</div>';
    window._drLocalContent = false;

  } else if (tab === 'tasks') {
    // Render tasks directly from taskMap
    var entries = Object.entries(taskMap);
    if (entries.length === 0) {
      container.innerHTML = '<div style="color:#666;font-size:12px;text-align:center;padding:12px">No recent tasks</div>';
    } else {
      var sorted = entries.sort(function(a,b) { return b[1].time - a[1].time; }).slice(0, 10);
      var icons = { pending: '&#8987;', working: '&#9881;', done: '&#10003;', error: '&#10007;' };
      container.innerHTML = sorted.map(function(entry) {
        var id = entry[0], t = entry[1];
        var ago = Math.round((Date.now() - t.time) / 1000);
        var timeStr = ago < 60 ? ago + 's ago' : Math.round(ago / 60) + 'm ago';
        var hasResult = t.result && t.status === 'done';
        var isExpanded = expandedTasks.has(id);
        var resultHtml = hasResult ? '<div style="display:' + (isExpanded ? 'block' : 'none') + ';padding:6px 12px;color:#8ab4c8;font-size:11px;white-space:pre-wrap;word-break:break-word;overflow-wrap:break-word;background:#0d1520;border-radius:6px;margin:4px 0;max-width:100%;box-sizing:border-box">' + esc(t.result) + '</div>' : '';
        return '<div style="padding:4px 0;border-bottom:1px solid #1a2a3a;cursor:' + (hasResult ? 'pointer' : 'default') + '" onclick="if(this.nextElementSibling)this.nextElementSibling.style.display=this.nextElementSibling.style.display===&quot;none&quot;?&quot;block&quot;:&quot;none&quot;">' +
          '<span style="color:' + (t.status==='done' ? '#4ecca3' : t.status==='working' ? '#f0ad4e' : '#666') + ';font-size:12px">' + (icons[t.status] || '?') + '</span> ' +
          '<span style="font-size:12px;color:#ccc">' + esc(t.text || id) + '</span>' +
          '<span style="float:right;font-size:10px;color:#555">' + timeStr + '</span>' +
          '</div>' + resultHtml;
      }).join('');
    }
    window._drLocalContent = false;

  } else if (tab === 'notes') {
    var DASH = 'http://' + window.location.hostname + ':7844';
    fetch(DASH + '/notes').then(function(r){return r.json()}).then(function(notes) {
      var searchHtml = '<div style="margin-bottom:8px"><input id="note-search" type="text" placeholder="Search notes..." style="width:100%;padding:6px 10px;border-radius:8px;border:1px solid #1e1e30;background:#0e0e18;color:#ccc;font-size:12px;outline:none" oninput="filterNotes(this.value)"></div>';
      var html = '';
      window._allNotes = notes;
      notes.forEach(function(n) {
        html += '<div class="note-item" data-title="' + esc(n.title).toLowerCase() + '" data-slug="' + n.slug + '" style="padding:6px 0;border-bottom:1px solid #2a2a3e;display:flex;align-items:center">' +
          '<span style="color:#7c83ff;cursor:pointer;flex:1" onclick="showNoteContent(&quot;' + n.slug + '&quot;)">' + n.title + '</span>' +
          '<span style="color:#666;font-size:11px;margin-right:8px">' + new Date(n.modified*1000).toLocaleDateString() + '</span>' +
          '<span style="color:#e94560;font-size:11px;cursor:pointer;opacity:0.5" onclick="event.stopPropagation();deleteNoteFromUI(&quot;' + n.slug + '&quot;)">x</span></div>';
      });
      if (!html) html = '<div style="color:#666;font-size:12px;text-align:center;padding:12px">No notes</div>';
      container.innerHTML = searchHtml + html;
    });

  } else if (tab === 'questions') {
    var questions = window._drQuestions || [];
    if (questions.length === 0) {
      container.innerHTML = '<div style="color:#666;font-size:12px;text-align:center;padding:12px">No pending questions</div>';
    } else {
      container.innerHTML = '<div class="dr-questions">' +
        questions.map(function(q) {
          return '<div class="q-item"><b>' + esc(q.id) + '</b>: ' + esc(q.text) +
            (q.detail ? '<div style="color:#999;font-size:11px;margin-top:2px;white-space:pre-wrap">' + esc(q.detail) + '</div>' : '') +
            '<div class="q-actions">' +
            (q.options ? q.options.map(function(opt) {
              return '<button class="q-btn" data-qid="' + q.id + '" data-ans="' + esc(opt) + '" style="border-color:#4ecca366;color:#4ecca3">' + esc(opt) + '</button>';
            }).join('') :
            '<button class="q-btn q-yes" data-qid="' + q.id + '" data-ans="Yes">Yes</button>' +
            '<button class="q-btn q-no" data-qid="' + q.id + '" data-ans="No">No</button>') +
            '<input class="q-input" data-qid="' + q.id + '" placeholder="Or type a response...">' +
            '<button class="q-btn q-send" data-qid="' + q.id + '">Send</button>' +
            '</div></div>';
        }).join('') + '</div>';
    }

  } else if (tab === 'activity') {
    fetch(API_BASE + '/activity').then(function(r){return r.json()}).then(function(data) {
      var items = data.activity || [];
      if (items.length === 0) {
        container.innerHTML = '<div style="color:#666;font-size:12px;text-align:center;padding:12px">No recent activity</div>';
        return;
      }
      var html = '';
      items.forEach(function(item) {
        if (item.type === 'commit') {
          html += '<div style="padding:3px 0;font-size:12px"><span style="color:#555;font-family:monospace">' + item.hash + '</span> <span style="color:#7c83ff">' + esc(item.message) + '</span></div>';
        } else if (item.type === 'task') {
          html += '<div style="padding:3px 0;font-size:12px;color:#4ecca3">' + esc(item.preview) + '</div>';
        }
      });
      container.innerHTML = html;
    });
  }
}

function showNoteContent(slug) {
  var DASH = 'http://' + window.location.hostname + ':7844';
  var container = document.getElementById('dr-content');
  if (!container) return;
  fetch(DASH + '/notes/' + slug).then(function(r){return r.text()}).then(function(text) {
    // Notify the voice agent — raw markdown (before HTML transform) is what
    // Gemini wants to reason about. Fire-and-forget; voice agent may or may
    // not be connected.
    try {
      fetch('/note-viewing', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug: slug, content: text })
      }).catch(function(){});
    } catch (e) {}
    text = text.replace(new RegExp('^---[\\s\\S]*?---\\n'), '');
    text = text.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    text = text.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    text = text.replace(/^# (.+)$/gm, '<h1 style="font-size:16px">$1</h1>');
    var codeBlockRe = new RegExp(String.fromCharCode(96,96,96) + '([\\s\\S]*?)' + String.fromCharCode(96,96,96), 'g');
    text = text.replace(codeBlockRe, '<pre style="background:#1a1a2e;padding:8px;border-radius:4px;font-size:12px;overflow-x:auto"><code>$1</code></pre>');
    var inlineCodeRe = new RegExp(String.fromCharCode(96) + '([^' + String.fromCharCode(96) + ']+)' + String.fromCharCode(96), 'g');
    text = text.replace(inlineCodeRe, '<code style="background:#1a1a2e;padding:1px 4px;border-radius:2px">$1</code>');
    text = text.replace(/[*][*](.+?)[*][*]/g, '<strong>$1</strong>');
    text = text.replace(/^- (.+)$/gm, '<li>$1</li>');
    text = text.replace(new RegExp('\\n\\n', 'g'), '<br><br>');
    container.innerHTML = '<span class="suggestion" onclick="renderTabContent()" style="font-size:11px;cursor:pointer;margin-bottom:8px;display:inline-block">&larr; Back</span>' +
      '<div style="font-size:13px;line-height:1.5">' + text + '</div>';
  });
}
window.showNoteContent = showNoteContent;

function deleteNoteFromUI(slug) {
  var DASH = 'http://' + window.location.hostname + ':7844';
  fetch(DASH + '/notes/' + slug, {method: 'DELETE'}).then(function() {
    renderTabContent(); // refresh notes list
  });
}
window.deleteNoteFromUI = deleteNoteFromUI;

function filterNotes(query) {
  var items = document.querySelectorAll('.note-item');
  var q = query.toLowerCase();
  items.forEach(function(el) {
    var title = el.getAttribute('data-title') || '';
    var slug = el.getAttribute('data-slug') || '';
    el.style.display = (!q || title.indexOf(q) >= 0 || slug.indexOf(q) >= 0) ? 'flex' : 'none';
  });
}
window.filterNotes = filterNotes;

function updateDynamicRegion() {
  var dr = document.getElementById('dynamic-region');
  if (!dr) return;
  // Skip re-render if user is typing
  var activeInput = document.activeElement;
  if (activeInput && activeInput.classList && activeInput.classList.contains('q-input')) return;

  // If API pushed real content, handle it
  var content = window._drContent;
  if (content && content.type) {
    // View switch command from voice agent
    if (content.type === 'view' && content.view) {
      window._drContent = null;
      switchDRTab(content.view);
      return;
    }
    // Real media content (video, image, etc.) — show directly (no tabs)
    dr.innerHTML = renderDynamicContent(content);
    return;
  }

  // Ensure tab structure exists
  ensureTabStructure();

  // Auto-switch to questions tab if new questions arrive
  var questions = window._drQuestions || [];
  if (questions.length > 0 && window._drActiveTab === 'starter') {
    window._drActiveTab = 'questions';
    updateTabHighlights();
    renderTabContent();
    return;
  }

  // Update tab badges (task count, question count) without re-rendering content
  updateTabHighlights();

  // Only render content if not locally set (user clicked a tab)
  if (!window._drLocalContent) {
    renderTabContent();
  }
}

// Event delegation for question actions
document.addEventListener('click', function(e) {
  var btn = e.target.closest && e.target.closest('[data-qid]');
  if (!btn) return;
  var qid = btn.dataset.qid;
  if (btn.dataset.ans) {
    answerQuestion(qid, btn.dataset.ans);
  } else if (btn.classList.contains('q-send')) {
    var inp = document.querySelector('.q-input[data-qid="' + qid + '"]');
    if (inp && inp.value.trim()) answerQuestion(qid, inp.value.trim());
  }
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && e.target.classList && e.target.classList.contains('q-input')) {
    var qid = e.target.dataset.qid;
    if (e.target.value.trim()) answerQuestion(qid, e.target.value.trim());
  }
});

// Poll dynamic-content + core-status
(function pollDynamicContent() {
  setInterval(() => {
    Promise.all([
      fetch(API_BASE + '/dynamic-content').then(r => r.json()).catch(() => ({})),
      fetch(API_BASE + '/core-status').then(r => r.json()).catch(() => ({status:'idle'})),
      fetch('http://' + window.location.hostname + ':7844/notes').then(r => r.json()).catch(() => []),
      fetch(API_BASE + '/contextual-chips').then(r => r.json()).catch(() => ({chips:[]}))
    ]).then(([dc, loopData, notes, ctx]) => {
      window._contextualChips = (ctx && ctx.chips) || [];
      window._drNoteCount = Array.isArray(notes) ? notes.length : 0;
      // Only overwrite content if API has real content; preserve local content (e.g. notes browser)
      if (dc && dc.type) {
        window._drContent = dc;
        window._drLocalContent = false;
      } else if (!window._drLocalContent) {
        window._drContent = null;
      }
      if (loopData.status === 'running') {
        window._drProactive = loopData.step || 'Working...';
      } else {
        window._drProactive = null;
      }
      updateDynamicRegion();
      // Update persistent core status bar (clickable to expand activity)
      var csBar = document.getElementById('core-status-bar');
      if (csBar) {
        var statusText = loopData.status === 'running'
          ? '<span class="core-running">Core: ' + esc(loopData.step || 'working') + '</span>'
          : '<span class="core-idle">Core: idle</span>';
        var expandBtn = '';
        csBar.innerHTML = statusText + expandBtn;
      }
    });
  }, 3000);
})();

// Initial render
updateDynamicRegion();

</script>
</body>
</html>`;

// SSE clients for remote toggle
const sseClients: import('node:http').ServerResponse[] = [];

const server = createServer((req, res) => {
	const url = new URL(req.url || '/', `http://${req.headers.host}`);

	if (url.pathname === '/sse') {
		res.writeHead(200, {
			'Content-Type': 'text/event-stream',
			'Cache-Control': 'no-cache',
			'Connection': 'keep-alive',
			'Access-Control-Allow-Origin': '*',
		});
		res.write(':\n\n'); // heartbeat
		sseClients.push(res);
		req.on('close', () => {
			const idx = sseClients.indexOf(res);
			if (idx >= 0) sseClients.splice(idx, 1);
		});
		return;
	}

	if (url.pathname === '/toggle' || url.pathname === '/mute') {
		const event = url.pathname === '/toggle' ? 'toggle-voice' : 'toggle-mute';
		for (const client of sseClients) {
			client.write(`event: ${event}\ndata: 1\n\n`);
		}
		res.writeHead(200, { 'Content-Type': 'application/json' });
		res.end(JSON.stringify({ ok: true, event, clients: sseClients.length }));
		return;
	}

	// Note view event from the in-page note reader. Writes the current slug +
	// content to /tmp/sutando-note-viewing.json; the voice-agent's
	// startNoteViewingWatcher picks it up and injects into Gemini so the
	// assistant can answer questions about whatever the user is looking at.
	if (url.pathname === '/note-viewing' && req.method === 'POST') {
		const chunks: Buffer[] = [];
		req.on('data', (c: Buffer) => chunks.push(c));
		req.on('end', () => {
			try {
				const body = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
				if (!body.slug || typeof body.content !== 'string') {
					res.writeHead(400, { 'Content-Type': 'application/json' });
					res.end(JSON.stringify({ error: 'slug and content required' }));
					return;
				}
				const event = { slug: body.slug, content: body.content, ts: new Date().toISOString() };
				writeFileSync('/tmp/sutando-note-viewing.json', JSON.stringify(event));
				res.writeHead(200, { 'Content-Type': 'application/json' });
				res.end(JSON.stringify({ ok: true }));
			} catch (e) {
				res.writeHead(400, { 'Content-Type': 'application/json' });
				res.end(JSON.stringify({ error: e instanceof Error ? e.message : 'parse failed' }));
			}
		});
		return;
	}

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

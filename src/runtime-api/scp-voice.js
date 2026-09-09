/** scp-voice.js — drop-in toggle-live voice client for ANY web surface.
 *
 * The exact logic behind the web companion's voice card, packaged so the
 * desktop chat's "Talk to your agent" button (or any other UI) can mount it:
 *
 *   import { ScpVoice } from '.../scp-voice.js'   // or <script src>, window.ScpVoice
 *   const v = new ScpVoice({
 *     url: 'wss://<host>:8443/scp',      // the AGENT's endpoint
 *     token: '<device credential>',       // from pair.redeem ({agentId → profile})
 *     onState: (s) => ...,                // listening/thinking/speaking/interrupted
 *     onError: (msg) => ...,
 *   })
 *   await v.start()   // opens its own WS + voice session; talk naturally
 *   await v.stop()    // ends the session and releases the mic
 *
 * Full-duplex: getUserMedia echoCancellation keeps the mic deaf to the
 * speaker, so interruption is just talking over the reply. On the server's
 * "interrupted" state all scheduled reply audio is flushed instantly.
 * The client stays a dumb mic/speaker — session semantics live server-side.
 */
export class ScpVoice {
	constructor(opts) {
		this.opts = opts;
		this.ws = null;
		this.sid = null;
		this.ctx = null;
		this.stream = null;
		this.srcNode = null;
		this.proc = null;
		this.playCursor = 0;
		this.sources = [];
		this.live = false;
		this.nextId = 1;
		this.pending = new Map();
	}

	_rpc(method, params = {}) {
		return new Promise((res, rej) => {
			const id = this.nextId++;
			this.pending.set(id, { res, rej });
			this.ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
			setTimeout(() => {
				if (this.pending.delete(id)) rej(new Error(method + ' timeout'));
			}, 10000);
		});
	}

	_mfEncode(payload) {
		const out = new Uint8Array(4 + payload.byteLength);
		out[0] = 1; out[1] = 1;
		out[2] = (this.sid >> 8) & 0xff; out[3] = this.sid & 0xff;
		out.set(new Uint8Array(payload), 4);
		return out.buffer;
	}

	_flush() {
		for (const s of this.sources.splice(0)) { try { s.stop(); } catch (_e) { /* */ } }
		this.playCursor = 0;
	}

	_play24k(buf) {
		const i16 = new Int16Array(buf), f32 = new Float32Array(i16.length);
		for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000;
		const ab = this.ctx.createBuffer(1, f32.length, 24000);
		ab.getChannelData(0).set(f32);
		const src = this.ctx.createBufferSource();
		src.buffer = ab; src.connect(this.ctx.destination);
		const t = Math.max(this.ctx.currentTime, this.playCursor);
		src.start(t);
		this.playCursor = t + ab.duration;
		this.sources.push(src);
		src.onended = () => { this.sources = this.sources.filter((s) => s !== src); };
	}

	_down16k(f32, fromRate) {
		const ratio = fromRate / 16000, n = Math.floor(f32.length / ratio);
		const out = new Int16Array(n);
		for (let i = 0; i < n; i++) {
			const s = Math.max(-1, Math.min(1, f32[Math.floor(i * ratio)]));
			out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
		}
		return out;
	}

	async start() {
		if (this.live) return;
		if (!window.isSecureContext) throw new Error('voice needs a secure context (https/localhost)');
		await new Promise((res, rej) => {
			this.ws = new WebSocket(`${this.opts.url}?token=${encodeURIComponent(this.opts.token)}`);
			this.ws.binaryType = 'arraybuffer';
			this.ws.onopen = res;
			this.ws.onerror = () => rej(new Error('connection failed'));
			this.ws.onclose = () => { if (this.live) void this.stop(); };
			this.ws.onmessage = (ev) => this._onMessage(ev);
		});
		const r = await this._rpc('voice.open', {});
		this.sid = r.streamId;
		this.stream = await navigator.mediaDevices.getUserMedia({ audio: {
			echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
		this.ctx = new (window.AudioContext || window.webkitAudioContext)();
		await this.ctx.resume();
		this.srcNode = this.ctx.createMediaStreamSource(this.stream);
		this.proc = this.ctx.createScriptProcessor(4096, 1, 1);
		this.proc.onaudioprocess = (e) => {
			if (!this.live || this.sid == null || this.ws.readyState !== 1) return;
			this.ws.send(this._mfEncode(
				this._down16k(e.inputBuffer.getChannelData(0), this.ctx.sampleRate).buffer));
		};
		this.srcNode.connect(this.proc);
		this.proc.connect(this.ctx.destination);
		this.live = true;
		this.opts.onState?.('listening');
	}

	_onMessage(ev) {
		if (ev.data instanceof ArrayBuffer) {
			const b = new Uint8Array(ev.data);
			if (b.length > 4 && b[0] === 1 && ((b[2] << 8) | b[3]) === this.sid) {
				this._play24k(ev.data.slice(4));
			}
			return;
		}
		let m; try { m = JSON.parse(ev.data); } catch (_e) { return; }
		if (m.id && this.pending.has(m.id)) {
			const p = this.pending.get(m.id); this.pending.delete(m.id);
			m.error ? p.rej(new Error(m.error.message || 'error')) : p.res(m.result);
			return;
		}
		if (m.method === 'voice.state') {
			if (m.params?.state === 'interrupted') this._flush();
			this.opts.onState?.(m.params?.state || '');
		}
	}

	async stop() {
		this.live = false;
		this._flush();
		try {
			this.proc?.disconnect(); this.srcNode?.disconnect();
			this.stream?.getTracks().forEach((t) => t.stop());
		} catch (_e) { /* */ }
		this.proc = this.srcNode = this.stream = null;
		if (this.sid != null && this.ws?.readyState === 1) {
			try { await this._rpc('voice.close', { streamId: this.sid }); } catch (_e) { /* */ }
		}
		this.sid = null;
		try { this.ws?.close(); } catch (_e) { /* */ }
		this.ws = null;
		this.opts.onState?.('');
	}
}

if (typeof window !== 'undefined') window.ScpVoice = ScpVoice;

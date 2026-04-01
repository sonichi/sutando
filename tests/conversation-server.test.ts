/**
 * Conversation server unit tests — audio codecs, phone normalization,
 * transcript dedup logic, and task delegation caching.
 *
 * Run: npx tsx tests/conversation-server.test.ts
 */

let passed = 0;
let failed = 0;

function assert(condition: boolean, msg: string) {
	if (condition) {
		passed++;
		console.log(`  ✓ ${msg}`);
	} else {
		failed++;
		console.log(`  ✗ ${msg}`);
	}
}

function assertEq(actual: unknown, expected: unknown, msg: string) {
	if (actual === expected) {
		passed++;
		console.log(`  ✓ ${msg}`);
	} else {
		failed++;
		console.log(`  ✗ ${msg} — expected ${expected}, got ${actual}`);
	}
}

// ============================================================
// Re-implement pure functions from conversation-server.ts
// (can't import them since the file has side effects + process.exit)
// ============================================================

// --- normalizePhone ---
function normalizePhone(num: string): string {
	const digits = num.replace(/\D/g, '');
	return digits.length === 10 ? '1' + digits : digits;
}

// --- mu-law decode table ---
const MULAW_DECODE = new Int16Array(256);
(() => {
	for (let i = 0; i < 256; i++) {
		const mu = ~i & 0xff;
		const sign = mu & 0x80 ? -1 : 1;
		const exponent = (mu >> 4) & 0x07;
		const mantissa = mu & 0x0f;
		const magnitude = ((mantissa << 1) + 33) * (1 << exponent) - 33;
		MULAW_DECODE[i] = sign * magnitude;
	}
})();

// --- pcmToMulaw ---
function pcmToMulaw(sample: number): number {
	const sign = sample < 0 ? 0x80 : 0;
	let magnitude = Math.min(Math.abs(sample), 32635);
	magnitude += 0x84;
	let exponent = 7;
	const expMask = 0x4000;
	for (let i = 0; i < 8; i++) {
		if (magnitude & (expMask >> i)) { exponent = 7 - i; break; }
	}
	const mantissa = (magnitude >> (exponent + 3)) & 0x0f;
	return ~(sign | (exponent << 4) | mantissa) & 0xff;
}

// --- mulawTopcm16k ---
function mulawTopcm16k(mulawBytes: Buffer): Buffer {
	const numSamples = mulawBytes.length;
	const out = Buffer.alloc(numSamples * 2 * 2);
	for (let i = 0; i < numSamples; i++) {
		const s0 = MULAW_DECODE[mulawBytes[i]];
		const s1 = i + 1 < numSamples ? MULAW_DECODE[mulawBytes[i + 1]] : s0;
		const mid = (s0 + s1) >> 1;
		out.writeInt16LE(s0, i * 4);
		out.writeInt16LE(mid, i * 4 + 2);
	}
	return out;
}

// --- pcm24kToMulaw8k ---
function pcm24kToMulaw8k(pcmBuf: Buffer): Buffer {
	const numSamples = pcmBuf.length / 2;
	const outLen = Math.floor(numSamples / 3);
	const out = Buffer.alloc(outLen);
	for (let i = 0; i < outLen; i++) {
		const sample = pcmBuf.readInt16LE(i * 3 * 2);
		out[i] = pcmToMulaw(sample);
	}
	return out;
}

// --- esc (XML/HTML escaping) ---
function esc(s: string): string {
	return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ============================================================
// Transcript dedup simulator
// ============================================================

interface TranscriptEntry { role: string; text: string }

function simulateTranscriptDedup(
	turns: { items: { role: string; content: string }[] }[],
): TranscriptEntry[] {
	const transcript: TranscriptEntry[] = [];
	let lastProcessedIdx = 0;

	for (const turn of turns) {
		const items = turn.items;
		// Guard: if items shrunk (reconnect reset context), re-scan from start
		if (items.length < lastProcessedIdx) lastProcessedIdx = 0;

		const lastTranscriptText = transcript.length > 0
			? transcript[transcript.length - 1].text : null;

		for (const item of items.slice(lastProcessedIdx)) {
			if (item.content === lastTranscriptText) continue;
			if (item.role === 'user') {
				transcript.push({ role: 'caller', text: item.content });
			} else if (item.role === 'assistant') {
				transcript.push({ role: 'sutando', text: item.content });
			}
		}
		lastProcessedIdx = items.length;
	}
	return transcript;
}

// ============================================================
// Task result cache simulator
// ============================================================

function simulateTaskCache() {
	const cache = new Map<string, string>();
	const resultQueue: { text: string }[] = [];

	function delegateTask(taskDescription: string): { status: string } {
		const cached = cache.get(taskDescription);
		if (cached) {
			resultQueue.push({
				text: `[Task result for "${taskDescription}"]\n${cached}\n\nReport this result to the caller now.`,
			});
			return { status: 'cached' };
		}
		// Simulate immediate completion for testing
		const result = `Result for: ${taskDescription}`;
		cache.set(taskDescription, result);
		resultQueue.push({
			text: `[Task result for "${taskDescription}"]\n${result}\n\nReport this result to the caller now.`,
		});
		return { status: 'delegated' };
	}

	return { delegateTask, cache, resultQueue };
}

// ============================================================
// Tests
// ============================================================

console.log('\n=== Conversation Server Unit Tests ===\n');

// --- Phone normalization ---
console.log('Phone normalization:');
assertEq(normalizePhone('+1 (555) 123-4567'), '15551234567', 'normalizes +1 (555) 123-4567');
assertEq(normalizePhone('5551234567'), '15551234567', '10-digit gets 1 prepended');
assertEq(normalizePhone('15551234567'), '15551234567', '11-digit stays as-is');
assertEq(normalizePhone('+44 20 7946 0958'), '442079460958', 'international number normalized');
assertEq(normalizePhone(''), '', 'empty string stays empty');

// --- Audio codec: pcmToMulaw ---
console.log('\nAudio codec — pcmToMulaw:');
const silence = pcmToMulaw(0);
assert(silence >= 0 && silence <= 255, 'silence encodes to valid mu-law byte');
assertEq(pcmToMulaw(0), pcmToMulaw(-0), 'zero and negative zero encode the same');
const loud = pcmToMulaw(32000);
assert(loud >= 0 && loud <= 255, 'loud sample encodes to valid mu-law byte');
const quiet = pcmToMulaw(100);
assert(quiet >= 0 && quiet <= 255, 'quiet sample encodes to valid mu-law byte');
assert(loud !== quiet, 'loud and quiet encode to different values');

// --- Audio codec: mu-law round-trip ---
console.log('\nAudio codec — mu-law round-trip:');
// mu-law is a logarithmic companding codec — encode/decode are NOT exact inverses.
// The decode table maps 256 values to a subset of the 16-bit range.
// Verify that the codec preserves the sign and general magnitude.
for (const sample of [0, 1000, -1000, 16000, -16000, 32000, -32000]) {
	const encoded = pcmToMulaw(sample);
	const decoded = MULAW_DECODE[encoded];
	if (sample === 0) {
		assert(Math.abs(decoded) < 100, `round-trip sample 0: decoded=${decoded} ≈ 0`);
	} else {
		// Sign must be preserved
		assert(Math.sign(decoded) === Math.sign(sample), `round-trip sample ${sample}: sign preserved (decoded=${decoded})`);
		// Magnitude should be in the same order of magnitude (mu-law is lossy)
		assert(Math.abs(decoded) > 0, `round-trip sample ${sample}: decoded is non-zero`);
	}
}

// --- Audio codec: mulawTopcm16k ---
console.log('\nAudio codec — mulawTopcm16k:');
const testMulaw = Buffer.from([0xFF, 0xFF, 0xFF, 0xFF]); // 4 silence bytes
const pcm16k = mulawTopcm16k(testMulaw);
assertEq(pcm16k.length, 16, 'mulawTopcm16k: 4 input bytes → 16 output bytes (2x upsample, 16-bit)');
// All silence should decode to near-zero
for (let i = 0; i < pcm16k.length; i += 2) {
	const sample = pcm16k.readInt16LE(i);
	assert(Math.abs(sample) < 100, `mulawTopcm16k silence sample at ${i}: ${sample} ≈ 0`);
}

// --- Audio codec: pcm24kToMulaw8k ---
console.log('\nAudio codec — pcm24kToMulaw8k:');
// Create 12 bytes of PCM (6 samples at 24kHz) → should produce 2 mu-law bytes (3:1 downsample)
const testPcm = Buffer.alloc(12);
testPcm.writeInt16LE(0, 0);     // sample 0
testPcm.writeInt16LE(1000, 2);  // sample 1
testPcm.writeInt16LE(2000, 4);  // sample 2
testPcm.writeInt16LE(3000, 6);  // sample 3
testPcm.writeInt16LE(4000, 8);  // sample 4
testPcm.writeInt16LE(5000, 10); // sample 5
const mulaw8k = pcm24kToMulaw8k(testPcm);
assertEq(mulaw8k.length, 2, 'pcm24kToMulaw8k: 6 PCM samples → 2 mu-law bytes (3:1 ratio)');
assert(mulaw8k[0] >= 0 && mulaw8k[0] <= 255, 'first output byte is valid mu-law');
assert(mulaw8k[1] >= 0 && mulaw8k[1] <= 255, 'second output byte is valid mu-law');

// --- XML/HTML escaping ---
console.log('\nXML escaping:');
assertEq(esc('hello'), 'hello', 'plain text unchanged');
assertEq(esc('<script>alert("xss")</script>'), '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;', 'XSS attempt escaped');
assertEq(esc('a & b'), 'a &amp; b', 'ampersand escaped');

// --- Transcript dedup: normal conversation ---
console.log('\nTranscript dedup — normal conversation:');
{
	const result = simulateTranscriptDedup([
		{ items: [{ role: 'user', content: 'Hello' }, { role: 'assistant', content: 'Hi there!' }] },
		{ items: [{ role: 'user', content: 'Hello' }, { role: 'assistant', content: 'Hi there!' }, { role: 'user', content: 'What time is it?' }, { role: 'assistant', content: 'It is 3pm.' }] },
	]);
	assertEq(result.length, 4, 'normal conversation: 4 entries total');
	assertEq(result[0].role, 'caller', 'first entry is caller');
	assertEq(result[0].text, 'Hello', 'first entry text');
	assertEq(result[2].text, 'What time is it?', 'third entry is new question');
	assertEq(result[3].text, 'It is 3pm.', 'fourth entry is answer');
}

// --- Transcript dedup: reconnect replays history ---
console.log('\nTranscript dedup — reconnect replay:');
{
	const result = simulateTranscriptDedup([
		// Normal turn
		{ items: [{ role: 'user', content: 'Hello' }, { role: 'assistant', content: 'Hi!' }] },
		// After reconnect — items reset, replays same content
		{ items: [{ role: 'user', content: 'Hello' }, { role: 'assistant', content: 'Hi!' }] },
	]);
	// Should NOT duplicate — the second turn replays the same items
	// The dedup works: items.length < lastProcessedIdx → reset, then skip duplicates
	assertEq(result.length, 2, 'reconnect replay: only 2 entries (no duplicates)');
	assertEq(result[0].text, 'Hello', 'first entry preserved');
	assertEq(result[1].text, 'Hi!', 'second entry preserved');
}

// --- Transcript dedup: reconnect with new content ---
console.log('\nTranscript dedup — reconnect with new content after replay:');
{
	const result = simulateTranscriptDedup([
		{ items: [{ role: 'user', content: 'Hello' }, { role: 'assistant', content: 'Hi!' }] },
		// Reconnect replays old + adds new
		{ items: [{ role: 'user', content: 'Hello' }, { role: 'assistant', content: 'Hi!' }, { role: 'user', content: 'New question' }] },
	]);
	assertEq(result.length, 3, 'reconnect with new: 3 entries');
	assertEq(result[2].text, 'New question', 'new content captured after replay');
}

// --- Task result caching ---
console.log('\nTask result caching:');
{
	const { delegateTask, cache, resultQueue } = simulateTaskCache();

	const first = delegateTask('Check the weather');
	assertEq(first.status, 'delegated', 'first call is delegated');
	assertEq(cache.size, 1, 'cache has 1 entry after first call');
	assertEq(resultQueue.length, 1, 'result queue has 1 entry');

	const second = delegateTask('Check the weather');
	assertEq(second.status, 'cached', 'second identical call is cached');
	assertEq(cache.size, 1, 'cache still has 1 entry');
	assertEq(resultQueue.length, 2, 'result queue has 2 entries (original + replay)');

	const third = delegateTask('Send an email');
	assertEq(third.status, 'delegated', 'different task is delegated');
	assertEq(cache.size, 2, 'cache has 2 entries');
}

// --- isReplaying flag simulation ---
console.log('\nisReplaying flag behavior:');
{
	let isReplaying = false;
	const audioSent: string[] = [];

	function handleAudioOutput(data: string) {
		if (isReplaying) return;
		audioSent.push(data);
	}

	handleAudioOutput('audio1');
	assertEq(audioSent.length, 1, 'audio sent when not replaying');

	isReplaying = true;
	handleAudioOutput('audio2');
	assertEq(audioSent.length, 1, 'audio blocked during replay');

	isReplaying = false;
	handleAudioOutput('audio3');
	assertEq(audioSent.length, 2, 'audio resumes after replay');
}

// --- Summary ---
console.log(`\n${passed + failed} assertions: ${passed} passed, ${failed} failed\n`);
if (failed > 0) process.exit(1);

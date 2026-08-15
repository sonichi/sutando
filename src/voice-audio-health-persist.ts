// voice-audio-health-persist — worker_threads sqlite writer for the P7
// voice_audio_health table (D7.1; §D7.0b round-4 #3: node:sqlite is
// synchronous, so timer-scheduled writes on the voice event loop can still
// block audio behind disk latency or busy_timeout — the writes live in a
// worker thread instead, and the main thread only try-enqueues).
//
// One-slot mailbox: at most one row is in flight; while the worker is busy,
// tryEnqueue() returns false and the caller counts a skipped sample. Rows are
// written immediately on receipt (G-P7-14: an end-of-session flush loses
// crash evidence) — once the worker acks, the row is on disk.
//
// The worker is spawned from an inline source string (eval: true): the
// voice agent ships as a single esbuild artifact (scripts/build-bundle.mjs),
// where a sibling worker FILE would not resolve. The worker uses only node
// builtins, so the string needs no bundling.

import { Worker } from 'node:worker_threads';
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';
import type { HealthRow } from './voice-audio-health.js';

/** Hard row cap enforced after every insert (age prune: 30 days). */
export const HEALTH_MAX_ROWS = 5000;

// CJS worker source — `require` is available under eval:true workers.
const WORKER_SOURCE = `
const { parentPort, workerData } = require('node:worker_threads');
const { DatabaseSync } = require('node:sqlite');
let db = null;
function open() {
  if (db) return db;
  db = new DatabaseSync(workerData.dbPath);
  db.exec('PRAGMA journal_mode=WAL');
  db.exec('PRAGMA synchronous=NORMAL');
  db.exec('PRAGMA busy_timeout=2000');
  db.exec('CREATE TABLE IF NOT EXISTS voice_audio_health (' +
    'id INTEGER PRIMARY KEY AUTOINCREMENT,' +
    'ts_unix INTEGER NOT NULL,' +
    'session_id TEXT NOT NULL,' +
    'epoch INTEGER,' +
    'nonce TEXT,' +
    'reason TEXT NOT NULL,' +
    'payload_json TEXT NOT NULL)');
  // Pre-rename databases carry a "payload" column; CREATE IF NOT EXISTS
  // would leave them as-is and every insert would then fail forever.
  const cols = db.prepare('PRAGMA table_info(voice_audio_health)').all().map((c) => c.name);
  if (!cols.includes('payload_json') && cols.includes('payload')) {
    db.exec('ALTER TABLE voice_audio_health RENAME COLUMN payload TO payload_json');
  }
  db.exec('CREATE INDEX IF NOT EXISTS idx_vah_ts ON voice_audio_health(ts_unix)');
  db.exec('CREATE INDEX IF NOT EXISTS idx_vah_session ON voice_audio_health(session_id)');
  return db;
}
parentPort.on('message', (row) => {
  let ok = true;
  try {
    const d = open();
    d.prepare('INSERT INTO voice_audio_health (ts_unix, session_id, epoch, nonce, reason, payload_json) VALUES (?, ?, ?, ?, ?, ?)')
      .run(row.tsUnix, row.sessionId, row.epoch ?? null, row.nonce ?? null, row.reason, row.payload);
    // Row cap is PER SESSION (a busy new session must not evict another
    // session's evidence); the 30-day sweep is global.
    d.prepare('DELETE FROM voice_audio_health WHERE session_id = ? AND id NOT IN ' +
      '(SELECT id FROM voice_audio_health WHERE session_id = ? ORDER BY id DESC LIMIT ?)')
      .run(row.sessionId, row.sessionId, row.maxRows);
    d.prepare('DELETE FROM voice_audio_health WHERE ts_unix < ?')
      .run(Math.floor(Date.now() / 1000) - 30 * 24 * 3600);
  } catch (e) {
    ok = false;
  }
  parentPort.postMessage({ ok });
});
`;

export interface HealthPersistence {
  /** Non-blocking: false when the slot is occupied or the worker is down. */
  tryEnqueue(row: HealthRow): boolean;
  /** Test seam: resolves when the in-flight row (if any) has been acked —
   *  i.e. it is on disk. */
  drain(): Promise<void>;
  close(): Promise<void>;
  readonly broken: boolean;
  /** Rows the worker acked as failed (disk error) — accepted but not stored. */
  readonly failedWrites: number;
}

export function defaultHealthDbPath(): string {
  return process.env.SUTANDO_VOICE_HEALTH_DB || join(resolveWorkspace(), 'data', 'voice-audio-health.sqlite');
}

export function createHealthPersistence(opts: { dbPath?: string; maxRows?: number } = {}): HealthPersistence {
  const dbPath = opts.dbPath ?? defaultHealthDbPath();
  const maxRows = opts.maxRows ?? HEALTH_MAX_ROWS;
  try {
    mkdirSync(dirname(dbPath), { recursive: true });
  } catch {
    /* worker open will surface a real failure */
  }
  let inFlight = false;
  let broken = false;
  let failedWrites = 0;
  const waiters: Array<() => void> = [];
  const release = (): void => {
    inFlight = false;
    for (const w of waiters.splice(0)) w();
  };
  const worker = new Worker(WORKER_SOURCE, { eval: true, workerData: { dbPath } });
  // The persistence worker must never keep the voice process alive.
  worker.unref();
  worker.on('message', (m: { ok?: boolean }) => {
    if (m?.ok === false) failedWrites++;
    release();
  });
  worker.on('error', () => {
    broken = true;
    release();
  });
  worker.on('exit', (code) => {
    if (code !== 0) broken = true;
    release();
  });
  return {
    tryEnqueue(row: HealthRow): boolean {
      if (broken || inFlight) return false;
      inFlight = true;
      try {
        worker.postMessage({ ...row, maxRows });
      } catch {
        broken = true;
        release();
        return false;
      }
      return true;
    },
    drain(): Promise<void> {
      if (!inFlight) return Promise.resolve();
      return new Promise((res) => waiters.push(res));
    },
    async close(): Promise<void> {
      await worker.terminate();
      release();
    },
    get broken(): boolean {
      return broken;
    },
    get failedWrites(): number {
      return failedWrites;
    },
  };
}

// Mentra lane: pure logic (core.ts) + the server seam (server.ts imports,
// constructs, and wires against the REAL @mentra/sdk AppServer — the review
// round proved core-only testing missed a wrong SDK contract entirely).
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  ackText, buildTask, deliveryMode, gateTranscript, sessionSlug,
  shouldPollResults, taskId, resolveConfig,
} from '../skills/mentra-channel/scripts/core.ts';

const SPARROW_TID = /^[A-Za-z0-9._-]{1,64}$/;

describe('gateTranscript', () => {
  it('passes wake-phrase utterances and strips the phrase', () => {
    assert.equal(gateTranscript('hey sutando what is on my calendar', 'hey sutando').text,
      'what is on my calendar');
    assert.equal(gateTranscript('Hey, Sutando — remind me at 5', 'hey sutando').text,
      'remind me at 5');
    assert.equal(gateTranscript('HEY SUTANDO do the thing', 'hey sutando').text,
      'do the thing');
  });

  it('gates out everything else (ambient firehose stays out)', () => {
    assert.equal(gateTranscript('so I told her sutando could help', 'hey sutando').text, null);
    assert.equal(gateTranscript('random meeting chatter', 'hey sutando').text, null);
    assert.equal(gateTranscript('', 'hey sutando').text, null);
    assert.equal(gateTranscript('hey sutando', 'hey sutando').text, null); // phrase alone: no task
  });
});

describe('taskId — collision + restart resistance (review P1)', () => {
  it('distinct opaque session ids never share a slug', () => {
    // The sanitize-approach collision pair from the review:
    assert.notEqual(sessionSlug('sess:a/b'), sessionSlug('sess:a:b'));
    assert.notEqual(taskId('sess:a/b', 'n1', 1), taskId('sess:a:b', 'n1', 1));
  });

  it('a restarted process can never re-mint a previous id', () => {
    // Same session, same seq, different boot nonce (restart) → different id.
    assert.notEqual(taskId('sess-1', 'boot1', 1), taskId('sess-1', 'boot2', 1));
  });

  it('ids are deterministic per instance, bounded, in sparrow alphabet', () => {
    for (const sess of ['abc123', 'sess:with/colons', 'x'.repeat(500), '你好世界', '']) {
      const id1 = taskId(sess, 'n0', 7);
      assert.equal(id1, taskId(sess, 'n0', 7));            // retries reuse it
      assert.match(id1, SPARROW_TID, `bad id for ${JSON.stringify(sess)}: ${id1}`);
    }
    assert.notEqual(taskId('s1', 'n', 1), taskId('s1', 'n', 2));
  });

  it('task shape matches the lane contract', () => {
    const t = buildTask('do the thing', 'user-9', 'sess-1', 'bootA', 3);
    assert.equal(t.source, 'mentra');
    assert.equal(t.channel_id, 'sess-1');
    assert.match(t.id, SPARROW_TID);
  });
});

describe('delivery single-owner (review P1)', () => {
  it('defaults to room mode — broker fallback owns replies, no polling', () => {
    assert.equal(deliveryMode(undefined), 'room');
    assert.equal(deliveryMode(''), 'room');
    assert.equal(shouldPollResults('room'), false);
    assert.match(ackText('room'), /Mentra room/);
  });

  it('glasses mode is explicit opt-in and enables polling', () => {
    assert.equal(deliveryMode('glasses'), 'glasses');
    assert.equal(shouldPollResults('glasses'), true);
    assert.doesNotMatch(ackText('glasses'), /room/);
  });
});

describe('resolveConfig', () => {
  it('env overrides manifest; blank env falls through', () => {
    const cfg = resolveConfig(
      { MENTRA_WAKE_PHRASE: 'yo glasses', MENTRA_PORT: '  ' },
      { MENTRA_WAKE_PHRASE: 'hey sutando', MENTRA_PORT: '8093' },
    );
    assert.equal(cfg.MENTRA_WAKE_PHRASE, 'yo glasses');
    assert.equal(cfg.MENTRA_PORT, '8093');
  });
});

describe('server seam — real SDK contract', () => {
  it('module imports side-effect free; buildServer validates config', async () => {
    const mod = await import('../skills/mentra-channel/scripts/server.ts');
    assert.throws(
      () => mod.buildServer({ MENTRA_PACKAGE_NAME: '', MENTRA_API_KEY: '' }),
      /not configured/,
    );
  });

  it('constructs a real AppServer subclass with a session hook', async () => {
    const mod = await import('../skills/mentra-channel/scripts/server.ts');
    const srv = mod.buildServer({
      MENTRA_PACKAGE_NAME: 'sutando',
      MENTRA_API_KEY: 'test-key-not-real',
      MENTRA_BROKER_URL: 'http://127.0.0.1:9',
      MENTRA_BROKER_TOKEN: 'tok',
      MENTRA_DELIVERY: 'room',
    });
    const { AppServer } = await import('@mentra/sdk');
    assert.ok(srv instanceof AppServer);                   // real SDK base class
    assert.equal(typeof (srv as unknown as { onSession: unknown }).onSession, 'function');
    // no .start() — construction must not open sockets (pinned by this test
    // completing without leaking handles under --test-force-exit)
  });
});

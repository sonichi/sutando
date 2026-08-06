// Mentra lane pure logic (skills/mentra-channel/scripts/core.ts).
// The wake gate is the v1 trigger CONTRACT (DESIGN.md): only wake-phrase
// finals become tasks; ids obey sparrow's [A-Za-z0-9._-]{1,64}.
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { gateTranscript, safeTaskId, buildTask, resolveConfig } from '../skills/mentra-channel/scripts/core.ts';

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

describe('safeTaskId / buildTask', () => {
  it('ids are deterministic, bounded, and in sparrow alphabet for hostile session ids', () => {
    for (const sess of ['abc123', 'sess:with/colons', 'x'.repeat(200), '你好世界', '']) {
      const id1 = safeTaskId(sess, 7);
      const id2 = safeTaskId(sess, 7);
      assert.equal(id1, id2);
      assert.match(id1, SPARROW_TID, `bad id for session ${JSON.stringify(sess)}: ${id1}`);
    }
    assert.notEqual(safeTaskId('s1', 1), safeTaskId('s1', 2)); // seq disambiguates
  });

  it('task shape matches the lane contract', () => {
    const t = buildTask('do the thing', 'user-9', 'sess-1', 3);
    assert.equal(t.source, 'mentra');
    assert.equal(t.channel_id, 'sess-1');
    assert.equal(t.task, 'do the thing');
    assert.match(t.id, SPARROW_TID);
    assert.equal(t.interaction_type, 'message');
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

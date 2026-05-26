/**
 * Unit tests for the pure routing functions in src/conversation-store.ts.
 *
 * We only import the two exported pure helpers (sourceFromRole, kindFromRole)
 * to avoid importing DatabaseSync (which would open or create a real SQLite
 * file at init time). The module uses top-level `let db = null` and only
 * opens the DB on first write — so a bare import triggers no I/O as long as
 * we never call recordConversation / recordSession.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { sourceFromRole, kindFromRole } from '../src/conversation-store.js';

describe('sourceFromRole()', () => {
  it('phone-caller → phone', () => {
    assert.equal(sourceFromRole('phone-caller'), 'phone');
  });
  it('phone-agent → phone', () => {
    assert.equal(sourceFromRole('phone-agent'), 'phone');
  });
  it('discord-user → discord-voice', () => {
    assert.equal(sourceFromRole('discord-user'), 'discord-voice');
  });
  it('discord-agent → discord-voice', () => {
    assert.equal(sourceFromRole('discord-agent'), 'discord-voice');
  });
  it('user → voice', () => {
    assert.equal(sourceFromRole('user'), 'voice');
  });
  it('assistant → voice', () => {
    assert.equal(sourceFromRole('assistant'), 'voice');
  });
  it('empty string → voice', () => {
    assert.equal(sourceFromRole(''), 'voice');
  });
  it('SESSION_END → voice (unknown passthrough)', () => {
    assert.equal(sourceFromRole('SESSION_END'), 'voice');
  });
});

describe('kindFromRole()', () => {
  it('"user" → "user"', () => {
    assert.equal(kindFromRole('user'), 'user');
  });
  it('"phone-user" (ends with -user) → "user"', () => {
    assert.equal(kindFromRole('phone-user'), 'user');
  });
  it('"discord-user" → "user"', () => {
    assert.equal(kindFromRole('discord-user'), 'user');
  });
  it('"phone-caller" (ends with -caller) → "user"', () => {
    assert.equal(kindFromRole('phone-caller'), 'user');
  });
  it('"assistant" → "agent"', () => {
    assert.equal(kindFromRole('assistant'), 'agent');
  });
  it('"sutando" → "agent"', () => {
    assert.equal(kindFromRole('sutando'), 'agent');
  });
  it('"phone-agent" (ends with -agent) → "agent"', () => {
    assert.equal(kindFromRole('phone-agent'), 'agent');
  });
  it('"discord-assistant" (ends with -assistant) → "agent"', () => {
    assert.equal(kindFromRole('discord-assistant'), 'agent');
  });
  it('"discord-peer" → "peer"', () => {
    assert.equal(kindFromRole('discord-peer'), 'peer');
  });
  it('"SESSION_END" → "SESSION_END" (verbatim passthrough)', () => {
    assert.equal(kindFromRole('SESSION_END'), 'SESSION_END');
  });
  it('"core-agent" → "agent" (ends with -agent)', () => {
    assert.equal(kindFromRole('core-agent'), 'agent');
  });
  it('unknown custom event → verbatim passthrough', () => {
    assert.equal(kindFromRole('health.degraded'), 'health.degraded');
  });
});

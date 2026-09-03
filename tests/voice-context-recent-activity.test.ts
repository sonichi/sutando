import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { pickRecentActivity } from '../src/voice-context.js';

// Regression cover for #3573. Drives the real exported implementation — an
// in-test copy of the algorithm cannot disagree with the source about anything.

const TWO_SECTIONS = `# Build log

Standing status (preamble — the OLDEST text, and the part that most resembles
legitimate "recent activity"):

- **Streaming task watcher** alive
- **PREAMBLE-ITEM** should never be injected

## 2026-08-01 — oldest dated header

- **OLD-A** belongs to the oldest section
- **OLD-B** belongs to the oldest section

## 2026-08-28 — newest dated header

- **NEW-A** belongs to the newest section
- **NEW-B** belongs to the newest section
`;

// A level-two header the SELECTOR cannot match (a time sits between the date
// and the dash). Measured on a peer host: 92 of its headers carry this shape.
const WIDE_HEADER_AFTER = `# Build log

## 2026-08-24 — newest header the selector can match

- **MINE** belongs to the selected section

## 2026-08-28 15:15 MST — later header the selector does NOT match

- **NOT-MINE-1** belongs to a LATER section
- **NOT-MINE-2** belongs to a LATER section
`;

const items = (out: string[]) => out.filter(l => l.trim().startsWith('- **'));

describe('pickRecentActivity (#3573)', () => {
	it('selects the newest dated header, not the first', () => {
		const out = pickRecentActivity(TWO_SECTIONS);
		assert.ok(out[1].includes('2026-08-28'), `expected the newest header, got ${out[1]}`);
		assert.ok(!out[1].includes('2026-08-01'), 'selected the oldest header');
	});

	it('injects only that header own items', () => {
		const got = items(pickRecentActivity(TWO_SECTIONS)).map(l => l.trim());
		assert.deepEqual(got, ['- **NEW-A** belongs to the newest section',
			'- **NEW-B** belongs to the newest section']);
	});

	it('does not inject the file preamble or an older section', () => {
		const joined = pickRecentActivity(TWO_SECTIONS).join('\n');
		assert.ok(!joined.includes('PREAMBLE-ITEM'), 'leaked the preamble');
		assert.ok(!joined.includes('Streaming task watcher'), 'leaked the preamble');
		assert.ok(!joined.includes('OLD-A'), 'leaked an older section');
	});

	// The selector may be narrow; the DELIMITER may not. A delimiter narrower
	// than what can begin a new unit lets one section swallow the next.
	it('ends the section at any level-two header, not only selectable ones', () => {
		const out = pickRecentActivity(WIDE_HEADER_AFTER);
		const joined = out.join('\n');
		assert.ok(out[1].includes('2026-08-24'), `expected the 08-24 header, got ${out[1]}`);
		assert.ok(joined.includes('MINE'), 'dropped the selected section own item');
		assert.ok(!joined.includes('NOT-MINE-1'), 'swallowed a later section');
		assert.ok(!joined.includes('NOT-MINE-2'), 'swallowed a later section');
	});

	it('renders header-only when the newest section has no items', () => {
		const out = pickRecentActivity('# Build log\n\n- **PREAMBLE** x\n\n## 2026-08-28 — empty\n');
		assert.equal(items(out).length, 0, 'borrowed items from elsewhere');
		assert.ok(out[0] === 'RECENT ACTIVITY:' && out[1].includes('2026-08-28'),
			'block disappeared instead of rendering header-only');
	});

	it('caps the injected items at five', () => {
		const many = '## 2026-08-28 — many\n\n' +
			Array.from({ length: 9 }, (_, i) => `- **I${i}** item`).join('\n') + '\n';
		assert.equal(items(pickRecentActivity(many)).length, 5);
	});

	it('returns nothing when no dated header exists', () => {
		assert.deepEqual(pickRecentActivity('# Build log\n\n- **X** y\n'), []);
	});

	// CONTROL: without this every assertion above could pass on an empty result.
	it('CONTROL: a well-formed log yields a header and items', () => {
		const out = pickRecentActivity(TWO_SECTIONS);
		assert.ok(out.length > 2 && items(out).length > 0, 'probe produced nothing to assert on');
	});
});

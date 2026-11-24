/* Runs the helper library's own node:test suites (skills/commons-app-builder/lib) in a child
 * process and turns the summary into this repo's one-line verdict. Hermetic: a fake bridge only. */
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const LIB = join(dirname(fileURLToPath(import.meta.url)), '..', 'skills', 'commons-app-builder', 'lib');
const SUITES = ['test/commons-sdk.test.js', 'test/interleavings.test.js', 'examples/check.js'];

// A parent `node --test` marks its children through this variable; unset, the child reports TAP.
const env = { ...process.env };
delete env.NODE_TEST_CONTEXT;
const run = spawnSync(process.execPath, ['--test', '--test-reporter=tap', ...SUITES], { cwd: LIB, env, encoding: 'utf8' });
const out = (run.stdout || '') + (run.stderr || '');
const count = (label) => Number((out.match(new RegExp('^# ' + label + ' (\\d+)$', 'm')) || [])[1]);
const pass = count('pass'), fail = count('fail'), total = count('tests');

if (run.status !== 0 || !(pass > 0) || fail !== 0 || pass !== total) {
  console.log(out);
  console.log(`FAIL commons-app-builder lib: exit ${run.status}, ${pass} passed, ${fail} failed, ${total} run`);
  process.exit(1);
}
console.log(`all passed: commons-app-builder lib, ${pass} of ${total} tests in ${SUITES.length} suites`);

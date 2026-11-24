'use strict';
/* Copies the library and the shared styles into each example, between their markers.
 * Usage: node examples/inline.js (rewrite), or with --check (exit 1 when an example is out of date). */
const fs = require('node:fs');
const path = require('node:path');

const here = __dirname;
const parts = {
  'commons-sdk': fs.readFileSync(path.join(here, '..', 'commons-sdk.js'), 'utf8').trim(),
  'shared-css': fs.readFileSync(path.join(here, '_head.css'), 'utf8').trim(),
};
const PAGES = ['signup.html', 'board.html'];

function fill(html) {
  let out = html;
  for (const [name, text] of Object.entries(parts)) {
    const begin = '/* ' + name + ':begin */', end = '/* ' + name + ':end */';
    const from = out.indexOf(begin), to = out.indexOf(end);
    if (from < 0 || to < from) throw new Error('markers for ' + name + ' are missing');
    out = out.slice(0, from + begin.length) + '\n' + text + '\n' + out.slice(to);
  }
  return out;
}

function stale() {
  return PAGES.filter((page) => {
    const file = path.join(here, page), html = fs.readFileSync(file, 'utf8');
    return fill(html) !== html;
  });
}

if (require.main === module) {
  if (process.argv.includes('--check')) {
    const old = stale();
    if (old.length) { console.error('out of date: ' + old.join(', ')); process.exit(1); }
    console.log('examples are up to date');
  } else {
    for (const page of PAGES) {
      const file = path.join(here, page);
      fs.writeFileSync(file, fill(fs.readFileSync(file, 'utf8')));
      console.log('inlined into ' + page);
    }
  }
}

module.exports = { stale, PAGES };

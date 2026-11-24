'use strict';
/* Runs the two example pages without a browser: their own scripts, in a Node vm context, with a
 * small stand-in DOM and the fake room from test/. Usage: node --test examples/check.js */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createFakeRoom, createClock, settle } = require('../test/fake-room.js');
const { stale } = require('./inline.js');

/* Just enough DOM for the examples: ids, text, children, value, disabled, hidden, click. */
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.own = ''; this.value = ''; this.disabled = false; this.hidden = false; this.className = ''; this.onclick = null; }
  get textContent() { return this.own + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this.children = []; this.own = String(v); }
  append(...nodes) { for (const n of nodes) this.children.push(typeof n === 'string' ? Object.assign(new El('#text'), { own: n }) : n); }
  replaceChildren(...nodes) { this.children = []; this.own = ''; this.append(...nodes); }
  click() { if (!this.disabled && this.onclick) return this.onclick({ target: this }); }
  find(pred) { return this.children.flatMap((c) => (pred(c) ? [c] : []).concat(c.find(pred))); }
}

/** Opens an example page for one viewer. `artifact` may be undefined: a page outside a room. */
async function open(file, artifact) {
  const html = fs.readFileSync(path.join(__dirname, file), 'utf8');
  const byId = new Map();
  for (const m of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"/g)) byId.set(m[2], new El(m[1]));
  const ticks = [];
  const page = {
    document: { getElementById: (id) => byId.get(id), createElement: (tag) => new El(tag) },
    setInterval: (fn) => ticks.push(fn), console,
    // A short wait (a retry) passes at once; a long one (the limit on one bridge call) never fires here.
    setTimeout: (fn, ms) => { const t = { live: true }; if (!(ms >= 5000)) setImmediate(() => t.live && fn()); return t; },
    clearTimeout: (t) => { if (t) t.live = false; },
  };
  page.window = page;
  if (artifact) page.artifact = artifact;
  vm.createContext(page);
  for (const m of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) vm.runInContext(m[1], page, { filename: file });
  await settle();
  const $ = (id) => byId.get(id);
  return {
    $, tick: () => ticks.forEach((fn) => fn()),
    items: () => $('list').children.map((li) => li.textContent),
    button: (label, within) => (within || $('list')).find((n) => n.tagName === 'BUTTON' && n.textContent === label)[0],
    item: (text) => $('list').children.find((li) => li.textContent.includes(text)),
  };
}

const SIGNUP = [
  { id: 'n', name: 'Name', type: 'title' }, { id: 'o', name: 'Note', type: 'text' },
  { id: 's', name: 'State', type: 'select', options: [{ name: 'Going' }, { name: 'Cancelled' }] },
  { id: 'b', name: 'Created by', type: 'created_by' },
];
const BOARD = [
  { id: 't', name: 'Task', type: 'title' }, { id: 's', name: 'Status', type: 'select', options: [{ name: 'Open' }, { name: 'Done' }] },
  { id: 'c', name: 'Claim', type: 'text' }, { id: 'r', name: 'Result', type: 'text' },
];

const UNSURE = 'Not confirmed by the room. If someone else took it too, this page will say so.';

function room(name, props, opts) {
  const clock = createClock();
  const fake = createFakeRoom(Object.assign({ clock }, opts));
  const db = fake.addDatabase(name, props);
  return { clock, fake, db };
}

test('the examples carry the current library and styles', () => {
  assert.deepEqual(stale(), []);
});

test('the pages and the README build the DOM from text, never from an HTML string', () => {
  const html = /innerHTML|outerHTML|insertAdjacentHTML|document\.write|DOMParser|createContextualFragment/;
  for (const file of ['signup.html', 'board.html', '../README.md']) {
    assert.doesNotMatch(fs.readFileSync(path.join(__dirname, file), 'utf8'), html, file);
  }
  const readme = fs.readFileSync(path.join(__dirname, '..', 'README.md'), 'utf8');
  assert.match(readme, /textContent = r\.outcome/);
});

test('both pages say so when opened outside a room', async () => {
  for (const file of ['signup.html', 'board.html']) {
    const page = await open(file);
    assert.equal(page.$('status').textContent, 'Open this page in a room to use it.');
    assert.equal(page.$('status').hidden, false);
  }
});

test('both pages name a missing database', async () => {
  const { fake } = room('Something else', SIGNUP);
  const page = await open('board.html', fake.client({ id: 'u1', name: 'Ana' }).artifact);
  assert.match(page.$('status').textContent, /no database named "Board — Tasks"/);
});

test('sign-up: a person and an agent sign up, each sees the other live, and only the author can cancel', async () => {
  const { fake, db } = room('Sign-up — Entries', SIGNUP);
  const ana = await open('signup.html', fake.client({ id: 'u-ana', name: 'Ana' }).artifact);
  const bot = await open('signup.html', fake.client({ id: 'u-bot', name: 'Bot', kind: 'agent' }).artifact);
  assert.deepEqual(ana.items(), ['No one yet. Add your name above.']);
  assert.equal(ana.$('status').hidden, true);
  assert.equal(ana.$('name').value, 'Ana');

  ana.$('note').value = 'Bringing gloves';
  await ana.$('join').click(); await settle();
  assert.equal(ana.$('count').textContent, '1 signed up');
  assert.equal(bot.$('count').textContent, '1 signed up');
  assert.match(bot.items()[0], /^AnaBringing gloves$/);
  assert.equal(ana.$('note').value, '');
  assert.ok(ana.button('Cancel my sign-up'));
  assert.equal(bot.button('Cancel my sign-up'), undefined);

  await bot.$('join').click(); await settle();
  assert.equal(ana.$('count').textContent, '2 signed up');
  assert.match(ana.item('Bot').textContent, /^Botagent$/);
  assert.deepEqual(fake.rows(db).map((r) => [r.cells.Name, r.cells.State, r.cells['Created by'][0]]), [['Ana', 'Going', '@u-ana:fake'], ['Bot', 'Going', '@u-bot:fake']]);

  await ana.button('Cancel my sign-up').click(); await settle();
  assert.equal(bot.$('count').textContent, '1 signed up');
  assert.deepEqual(ana.items(), ['Botagent']);
  assert.equal(fake.rows(db)[0].cells.State, 'Cancelled');
  assert.equal(fake.rows(db).length, 2);

  ana.$('name').value = '  ';
  await ana.$('join').click();
  assert.equal(ana.$('status').textContent, 'Type a name first.');
});

test('sign-up: before the viewer allows the page it shows "?" and a hint, waits, then saves', async () => {
  const { fake, db } = room('Sign-up — Entries', SIGNUP);
  fake.serverAddRow(db, { Name: 'Earlier', State: 'Going' }, '@someone:fake');
  const client = fake.client({ id: 'u-ana', name: 'Ana', consent: 'unasked' });
  const ana = await open('signup.html', client.artifact);
  assert.equal(ana.items()[0], 'Earlier?');
  assert.equal(ana.$('hint').hidden, false);
  const saving = ana.$('join').click();
  await settle();
  assert.equal(ana.$('status').textContent, 'Allow this page to edit data in this room, and it will save.');
  assert.equal(ana.$('join').disabled, true);
  assert.equal(fake.rows(db).length, 1);
  client.answer(true);
  await saving; await settle();
  assert.equal(fake.rows(db).length, 2);
  assert.equal(ana.$('status').hidden, true);
  assert.equal(ana.$('hint').hidden, true);
  assert.equal(ana.$('join').disabled, false);
  assert.ok(ana.button('Cancel my sign-up', ana.item('Ana')));
  assert.equal(ana.button('Cancel my sign-up', ana.item('Earlier')), undefined);
});

test('sign-up: a refused write keeps what the person typed and shows the reason', async () => {
  const { fake } = room('Sign-up — Entries', SIGNUP);
  const client = fake.client({ id: 'u-ana', name: 'Ana' });
  const ana = await open('signup.html', client.artifact);
  ana.$('note').value = 'keep me';
  client.failNext = 'the room said no';
  await ana.$('join').click(); await settle();
  assert.equal(ana.$('status').textContent, 'Not saved: the room said no');
  assert.equal(ana.$('note').value, 'keep me');
  assert.equal(ana.$('join').disabled, false);
});

test('board: a person adds a task, an agent takes it, works and marks it done; both pages follow', async () => {
  const { fake, db } = room('Board — Tasks', BOARD);
  const ana = await open('board.html', fake.client({ id: 'u-ana', name: 'Ana' }).artifact);
  const bot = await open('board.html', fake.client({ id: 'u-bot', name: 'Bot', kind: 'agent' }).artifact);
  assert.deepEqual(ana.items(), ['No tasks yet. Add the first one above.']);

  ana.$('title').value = 'Draft the release note';
  await ana.$('add').click(); await settle();
  assert.deepEqual(bot.items(), ['Draft the release noteOpenTake it']);
  assert.equal(ana.$('title').value, '');

  await bot.button('Take it').click(); await settle();
  assert.equal(ana.items()[0], 'Draft the release noteBot (agent) is on it10 min left');
  assert.equal(ana.button('Take it'), undefined);
  assert.equal(bot.items()[0], 'Draft the release noteYou are on it10 min left' + UNSURE + 'Mark doneLet go');

  await bot.button('Mark done').click(); await settle();
  for (const page of [ana, bot]) assert.deepEqual(page.items(), ['Draft the release noteDoneDone by Bot (agent)']);
  assert.deepEqual([fake.rows(db)[0].cells.Status, fake.rows(db)[0].cells.Claim], ['Done', '']);
});

test('board: two take one task at once; a hold is shown as unsettled, and the one who lost it is told', async () => {
  const { fake, db } = room('Board — Tasks', BOARD);
  fake.serverAddRow(db, { Task: 'One', Status: 'Open' });
  const ana = await open('board.html', fake.client({ id: 'u-ana', name: 'Ana' }).artifact);
  const bot = await open('board.html', fake.client({ id: 'u-bot', name: 'Bot', kind: 'agent' }).artifact);
  await Promise.all([ana.button('Take it').click(), bot.button('Take it').click()]);
  await settle();
  assert.equal(ana.$('status').textContent, 'Bot (agent) took "One" at the same moment.');
  assert.equal(ana.items()[0], 'OneBot (agent) is on it10 min left');
  assert.equal(bot.$('status').hidden, true);
  assert.equal(bot.items()[0], 'OneYou are on it10 min left' + UNSURE + 'Mark doneLet go');
});

test('board, conditional writes: the room settles a tie at once, and a hold it confirmed carries no warning', async () => {
  const { fake, db } = room('Board — Tasks', BOARD, { versions: true, conditional: true });
  fake.serverAddRow(db, { Task: 'One', Status: 'Open' });
  const ana = await open('board.html', fake.client({ id: 'u-ana', name: 'Ana' }).artifact);
  const bot = await open('board.html', fake.client({ id: 'u-bot', name: 'Bot', kind: 'agent' }).artifact);
  await Promise.all([ana.button('Take it').click(), bot.button('Take it').click()]);
  await settle();
  assert.equal(ana.items()[0], 'OneYou are on it10 min leftMark doneLet go');
  assert.equal(bot.$('status').textContent, 'Ana got there first.');
  assert.equal(bot.items()[0], 'OneAna is on it10 min left');
});

test('board: a hold that is cleared rather than taken (onLost with no holder) is said plainly, and nothing breaks', async () => {
  const { fake, db } = room('Board — Tasks', BOARD);
  const id = fake.serverAddRow(db, { Task: 'One', Status: 'Open' });
  const ana = await open('board.html', fake.client({ id: 'u-ana', name: 'Ana' }).artifact);
  await ana.button('Take it').click(); await settle();
  fake.serverWrite(db, id, 'Claim', ''); await settle();
  assert.equal(ana.$('status').textContent, 'Your hold on "One" was cleared.');
  assert.deepEqual(ana.items(), ['OneOpenTake it']);
});

test('board: a claim held for an approval says so, and not that it failed', async () => {
  const { fake, db } = room('Board — Tasks', BOARD, { versions: true, conditional: true });
  fake.serverAddRow(db, { Task: 'One', Status: 'Open' });
  const client = fake.client({ id: 'u-ana', name: 'Ana' });
  const ana = await open('board.html', client.artifact);
  client.failNext = 'this write is waiting for approval';
  await ana.button('Take it').click(); await settle();
  assert.equal(ana.$('status').textContent, 'Waiting for an approval. "One" may show as yours later.');
  assert.deepEqual(ana.items(), ['OneOpenTake it']);
});

test('board: letting go, and a hold that runs out, both open the task again', async () => {
  const { fake, db, clock } = room('Board — Tasks', BOARD);
  fake.serverAddRow(db, { Task: 'One', Status: 'Open' });
  const ana = await open('board.html', fake.client({ id: 'u-ana', name: 'Ana' }).artifact);
  const bot = await open('board.html', fake.client({ id: 'u-bot', name: 'Bot', kind: 'agent' }).artifact);
  await ana.button('Take it').click(); await settle();
  await ana.button('Let go').click(); await settle();
  assert.deepEqual(bot.items(), ['OneOpenTake it']);
  await ana.button('Take it').click(); await settle();
  clock.advance(4 * 60 * 1000); bot.tick();
  assert.equal(bot.items()[0], 'OneAna is on it6 min left');
  clock.advance(6 * 60 * 1000); bot.tick(); ana.tick();
  assert.deepEqual(bot.items(), ['OneOpenTake it']);
  assert.deepEqual(ana.items(), ['OneOpenTake it']);
  await bot.button('Take it').click(); await settle();
  assert.equal(ana.items()[0], 'OneBot (agent) is on it10 min left');
});

test('board: an agent with no page claims through the row, and the page shows it as a player', async () => {
  const { fake, db, clock } = room('Board — Tasks', BOARD);
  const id = fake.serverAddRow(db, { Task: 'One', Status: 'Open' });
  const ana = await open('board.html', fake.client({ id: 'u-ana', name: 'Ana' }).artifact);
  fake.serverWrite(db, id, 'Claim', JSON.stringify({ by: 'runner', name: 'Runner', kind: 'agent', until: clock.now() + 120000 }));
  await settle();
  assert.equal(ana.items()[0], 'OneRunner (agent) is on it2 min left');
  fake.serverWrite(db, id, 'Status', 'Done');
  fake.serverWrite(db, id, 'Result', 'Shipped');
  await settle();
  assert.deepEqual(ana.items(), ['OneDoneShipped']);
});

test('board: an empty title and a refused add are shown, and the text is kept', async () => {
  const { fake } = room('Board — Tasks', BOARD);
  const client = fake.client({ id: 'u-ana', name: 'Ana' });
  const ana = await open('board.html', client.artifact);
  await ana.$('add').click();
  assert.equal(ana.$('status').textContent, 'Type a task first.');
  ana.$('title').value = 'Keep me';
  client.failNext = 'the room said no';
  await ana.$('add').click(); await settle();
  assert.equal(ana.$('status').textContent, 'Not added: the room said no');
  assert.equal(ana.$('title').value, 'Keep me');
});

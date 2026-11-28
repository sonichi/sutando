const fs = require('node:fs');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');

const source = fs.readFileSync(process.argv[2], 'utf8');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const agents = [
  { id: 'u' + 'a'.repeat(64), name: 'Example A', kind: 'sutando' },
  { id: 'u' + 'b'.repeat(64), name: '<Example B>', kind: 'spacer' },
];

function setup(host) {
  const errors = [];
  const dom = new JSDOM(source, {
    url: 'https://guide.example.test/',
    runScripts: 'dangerously',
    beforeParse(w) {
      w.HTMLElement.prototype.scrollIntoView = () => {};
      w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
      w.HTMLDialogElement.prototype.close = function () {
        this.open = false;
        this.dispatchEvent(new w.Event('close'));
      };
      w.addEventListener('error', event => errors.push(String(event.message)));
      if (host) w.artifact = host;
    },
  });
  return { dom, errors, d: dom.window.document };
}

(async () => {
  const plain = setup();
  assert.equal(plain.d.querySelector('.guide-ask').hidden, true);
  assert.equal(plain.d.querySelector('#guide-bridge-status').hidden, false);
  assert.deepEqual(plain.errors, []);
  plain.dom.window.close();

  let lists = 0;
  let selected = [];
  const sent = [];
  let result = { ok: false, reason: 'declined' };
  const sample = setup({
    agent: { list: async () => { lists++; return agents; } },
    sendToMyAgent: async request => { sent.push(request); return result; },
    ready: Promise.resolve(),
    me: { id: 'synthetic-viewer' },
    state: {
      get(key) { assert.equal(key, 'gs-done-synthetic-viewer'); return ['phone']; },
      async set(key, value) { assert.equal(key, 'gs-done-synthetic-viewer'); selected = [...value]; },
    },
  });
  const { dom, d } = sample;
  const $ = id => d.getElementById(id);
  const click = id => $(id).click();
  const touch = (id, type) => $(id).dispatchEvent(new dom.window.Event(type, { bubbles: true }));
  await tick();
  assert.equal(lists, 0);
  assert.equal(d.querySelector('input[value="mobile"]').checked, true);
  assert.equal($('checklist-progress').max, 14);
  d.querySelector('input[value="dm"]').click();
  await tick();
  assert.deepEqual(selected, ['dm', 'mobile']);

  const topicTabs = [...d.querySelectorAll('.tab-btn')];
  assert.equal(topicTabs.length, 5);
  for (const [index, topic] of topicTabs.entries()) {
    topic.click();
    assert.equal(d.querySelector('.tour-card.active').id, 'step-' + index);
    assert.equal(d.querySelectorAll('.tab-btn[aria-current="true"]').length, 1);
  }
  const workflow = d.querySelector('#sutando-workflow-tab-0');
  workflow.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  assert.equal($('sutando-workflow-tab-1').getAttribute('aria-selected'), 'true');
  assert.equal($('sutando-workflow-panel-0').hidden, true);
  topicTabs[0].click();
  const button = d.querySelector('#step-0 .guide-ask');
  assert.equal(button.hidden, false);
  button.click();
  assert.equal($('guide-draft').open, true);
  assert.equal($('draft-text').value, button.dataset.draft);
  assert.equal($('draft-send').disabled, true);
  assert.equal(lists, 0);
  click('draft-close');
  assert.equal(sent.length, 0);
  button.click();
  $('draft-text').value = 'My edited message.';
  touch('draft-text', 'input');
  click('draft-choose');
  await tick();
  assert.equal(lists, 1);
  assert.equal($('draft-agent').options[2].text, '<Example B>');
  $('draft-agent').value = agents[1].id;
  touch('draft-agent', 'change');
  click('draft-send');
  await tick();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].agentId, agents[1].id);
  assert.equal(sent[0].text, 'My edited message.');
  assert.match(sent[0].key, /^guide-[a-f0-9]{32}$/);
  assert.match($('draft-status').textContent, /Not sent/);
  assert.equal($('draft-text').value, 'My edited message.');
  result = { ok: false, reason: 'send outcome unknown' };
  click('draft-send');
  await tick();
  assert.equal(sent.length, 2);
  assert.equal($('draft-send').disabled, true);
  assert.match($('draft-status').textContent, /did not confirm delivery/);
  click('draft-send');
  assert.equal(sent.length, 2);
  click('draft-close');
  button.click();
  click('draft-choose');
  await tick();
  $('draft-agent').value = agents[0].id;
  touch('draft-agent', 'change');
  result = { ok: true, eventId: 'simulated' };
  click('draft-send');
  await tick();
  assert.equal(sent.length, 3);
  assert.equal($('guide-draft').open, false);
  assert.match($('guide-send-receipt').textContent, /Message sent to Example A/);
  assert.deepEqual(sample.errors, []);
  dom.window.close();
})().catch(error => { console.error(error); process.exitCode = 1; });

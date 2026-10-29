import { test, expect } from 'claude-code/testing'
import { Dedupe, messageFor, parseTemplate, takeEvents, takeLines, withContext } from '../hooks/register'

test('only complete event lines are taken; the partial tail is carried', () => {
  const r = takeLines('noise\nPR_EVENT a/b#1 closed author=x head=abc merged=true\nPR_EVENT a/b#2 synch')
  expect(r.events).toEqual(['PR_EVENT a/b#1 closed author=x head=abc merged=true'])
  expect(r.notes).toEqual([])
  expect(r.rest).toBe('PR_EVENT a/b#2 synch')
})

test('a line split across chunks is taken once it completes', () => {
  const first = takeLines('PR_EVENT a/b#2 synch')
  const second = takeLines(first.rest + 'ronize author=x head=def merged=false\n')
  expect(first.events).toEqual([])
  expect(second.events).toEqual(['PR_EVENT a/b#2 synchronize author=x head=def merged=false'])
})

test('notes are separated from events and never become prompts', () => {
  const r = takeLines('PR_WATCH_NOTE a/b: another forwarder holds this repo\nPR_EVENT a/b#3 opened author=x head=1 merged=false\n')
  expect(r.notes).toEqual(['a/b: another forwarder holds this repo'])
  expect(r.events).toEqual(['PR_EVENT a/b#3 opened author=x head=1 merged=false'])
})

test('a repeated line inside the window is dropped, and passes again after it', () => {
  const d = new Dedupe(60_000)
  expect(d.fresh('PR_EVENT a/b#1 closed', 1_000)).toBe(true)
  expect(d.fresh('PR_EVENT a/b#1 closed', 30_000)).toBe(false)
  expect(d.fresh('PR_EVENT a/b#2 closed', 30_000)).toBe(true)
  expect(d.fresh('PR_EVENT a/b#1 closed', 61_001)).toBe(true)
})

test('the prompt template fills {event}; with none yet the bare lines are sent', () => {
  expect(messageFor('see {event} now', ['PR_EVENT a/b#1 opened'])).toBe('see PR_EVENT a/b#1 opened now')
  expect(messageFor(undefined, ['PR_EVENT x', 'PR_EVENT y'])).toBe('PR_EVENT x\nPR_EVENT y')
  expect(messageFor('  ', ['PR_EVENT x'])).toBe('PR_EVENT x')
  expect(messageFor('no placeholder', ['PR_EVENT x'])).toBe('[pr-watch] PR_EVENT x\nno placeholder')
})

test('the runner template line is taken, the last one wins, and a bad one is ignored', () => {
  const r = takeLines('PR_WATCH_PROMPT_TEMPLATE "one {event}"\nPR_WATCH_PROMPT_TEMPLATE "two\\n{event}"\nPR_EVENT a/b#1 opened\n')
  expect(r.template).toBe('two\n{event}')
  expect(r.events).toEqual(['PR_EVENT a/b#1 opened'])
  expect(messageFor(r.template, r.events)).toBe('two\nPR_EVENT a/b#1 opened')
  expect(takeLines('PR_WATCH_PROMPT_TEMPLATE {not json\nPR_EVENT x\n').template).toBe(undefined)
  expect(takeLines('PR_EVENT x\n').template).toBe(undefined)
  expect(parseTemplate('PR_WATCH_PROMPT_TEMPLATE 42')).toBe(undefined)
})

test('events that waited are joined into one message', () => {
  expect(takeEvents([], 'x {event}')).toBe(null)
  expect(takeEvents(['PR_EVENT a/b#1 closed', 'PR_EVENT a/b#2 synchronize'], 'see:\n{event}\nend'))
    .toBe('see:\nPR_EVENT a/b#1 closed\nPR_EVENT a/b#2 synchronize\nend')
})

test('the message rides after the tool result, keeping context already there', () => {
  expect(withContext({ result: 'ok' }, 'ev')).toEqual({ result: 'ok', context: ['ev'] })
  expect(withContext({ result: 'ok', context: ['first'] }, 'ev')).toEqual({ result: 'ok', context: ['first', 'ev'] })
})

test('a denied call carries nothing, so the events wait for the next one', () => {
  expect(withContext({ deny: 'no' }, 'ev')).toBe(null)
  expect(withContext({ deny: undefined, result: 1 }, 'ev')).toEqual({ deny: undefined, result: 1, context: ['ev'] })
})

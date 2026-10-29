import { test, expect } from 'claude-code/testing'
import { Dedupe, DEFAULT_PROMPT, promptFor, takeLines } from '../hooks/register'

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

test('the prompt template fills {event}, and falls back to the default', () => {
  expect(promptFor('see {event} now', 'PR_EVENT a/b#1 opened')).toBe('see PR_EVENT a/b#1 opened now')
  expect(promptFor(undefined, 'PR_EVENT x')).toBe(DEFAULT_PROMPT.replace('{event}', 'PR_EVENT x'))
  expect(promptFor('no placeholder', 'PR_EVENT x')).toBe('[pr-watch] PR_EVENT x\nno placeholder')
})

#!/usr/bin/env python3
"""The notes viewer renders note text and titles as text, never as live HTML.

The JS under test is the /notes-ui page exactly as served (the Python string
constant, unescaped by ast), run under node with a minimal DOM stub.
"""
import ast
import json
import pathlib
import re
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent


def _notes_ui_script() -> str:
    tree = ast.parse((REPO / "src" / "dashboard.py").read_text())
    pages = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
             and isinstance(n.value, str) and "<title>Sutando Notes</title>" in n.value]
    assert len(pages) == 1, f"expected one notes-ui page, found {len(pages)}"
    m = re.search(r"<script>\n(.*?)</script>", pages[0], re.S)
    assert m, "notes-ui script not found"
    return m.group(1)


HOSTILE_TITLE = '<img src=x onerror="alert(1)">'
HOSTILE_SLUG = "a');alert(1)//"
MD_CASES = {
    "raw_html": '<img src=x onerror=alert(1)>\n\n<script>alert(2)</script>',
    "js_link": "[click](javascript:alert(1))",
    "tab_js_link": "[click](java\tscript:alert(1))",
    "data_link": "[click](data:text/html,<script>alert(1)</script>)",
    "quote_breakout": '[x](https://e.com/" onmouseover="alert(1))',
    "https_link": "[docs](https://example.com/?a=1&b=2)",
    "relative_link": "[other](other-note)",
    "mailto_link": "[mail](mailto:a@b.c)",
    "formatting": "# Head\n\n**bold** *em* `code`\n\n- item\n\n> quoted",
    "code_block": "```\n<b>not bold</b>\n```",
}


def _run() -> dict:
    harness = """
const NOTES = [{slug: %s, title: %s, modified: 0}];
const CASES = %s;
const log = {innerHTML: [], fetches: []};
function el(tag) {
  return {tag, children: [], listeners: {}, style: {}, textContent: '', _html: '',
    set innerHTML(v) { this._html = v; log.innerHTML.push([this.id || tag, v]); },
    get innerHTML() { return this._html; },
    replaceChildren(...c) { this.children = c; }, append(...c) { this.children.push(...c); },
    addEventListener(t, f) { this.listeners[t] = f; }};
}
const els = {};
for (const id of ['list', 'content', 'app', 'viewer']) { els[id] = el('div'); els[id].id = id; }
global.document = {getElementById: id => els[id], createElement: el};
global.fetch = async u => { log.fetches.push(u);
  return u === '/notes' ? {json: async () => NOTES} : {text: async () => 'body'}; };
(0, eval)(%s);
setTimeout(async () => {
  const li = els.list.children[0] || {children: []};
  const a = li.children[0] || {listeners: {}};
  if (a.listeners.click) { a.listeners.click({preventDefault() {}}); }
  await new Promise(r => setTimeout(r, 20));
  const rendered = {};
  for (const [k, v] of Object.entries(CASES)) rendered[k] = md(v);
  console.log(JSON.stringify({rendered, log, titleText: a.textContent || null}));
}, 20);
""" % (json.dumps(HOSTILE_SLUG), json.dumps(HOSTILE_TITLE), json.dumps(MD_CASES),
       json.dumps(_notes_ui_script()))
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-600:]
    return json.loads(out.stdout.strip().splitlines()[-1])


fails = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def main() -> None:
    if shutil.which("node") is None:
        print("dashboard-notes-render: SKIP (node unavailable)")
        return
    res = _run()
    r = res["rendered"]
    list_html = [v for k, v in res["log"]["innerHTML"] if k == "list"]
    check("a note title is set as text, not markup", res["titleText"] == HOSTILE_TITLE,
          f"got {res['titleText']!r}")
    check("the note list is never built from an HTML string", not list_html, f"{list_html}")
    check("opening a note fetches its slug URL-encoded",
          "/notes/a')%3Balert(1)%2F%2F" in res["log"]["fetches"], f"{res['log']['fetches']}")
    check("raw HTML in a note renders as escaped text",
          "<img" not in r["raw_html"] and "<script" not in r["raw_html"]
          and "&lt;img src=x onerror=alert(1)&gt;" in r["raw_html"], r["raw_html"])
    for k in ("js_link", "tab_js_link", "data_link"):
        check(f"{k}: a non-web link scheme renders as plain text", "<a " not in r[k], r[k])
    check("a quote in a link target cannot open a new attribute",
          'onmouseover="' not in r["quote_breakout"], r["quote_breakout"])
    check("https links still render",
          r["https_link"] == '<a href="https://example.com/?a=1&amp;b=2" '
          'rel="noopener noreferrer">docs</a>', r["https_link"])
    check("relative links still render", '<a href="other-note"' in r["relative_link"],
          r["relative_link"])
    check("mailto links still render", '<a href="mailto:a@b.c"' in r["mailto_link"],
          r["mailto_link"])
    f = r["formatting"]
    check("headings, emphasis, code, lists and quotes still render",
          all(t in f for t in ("<h1>Head</h1>", "<strong>bold</strong>", "<em>em</em>",
                               "<code>code</code>", "<li>item</li>",
                               "<blockquote>quoted</blockquote>")), f)
    check("HTML inside a code block is shown, not rendered",
          "&lt;b&gt;not bold&lt;/b&gt;" in r["code_block"] and "<pre><code>" in r["code_block"],
          r["code_block"])
    if fails:
        raise SystemExit(f"dashboard-notes-render: FAILED: {', '.join(fails)}")
    print("dashboard-notes-render: all passed")


if __name__ == "__main__":
    main()

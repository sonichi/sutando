#!/usr/bin/env python3
"""Browser-mode X (Twitter) — drives your real, logged-in Chrome.

No API key required. Uses AppleScript to control the actual Google Chrome app
(not a headless browser), so it reads and engages with X (like, reply) using
your existing logged-in session.

Requirements (macOS + Google Chrome):
  - Chrome > View > Developer > "Allow JavaScript from Apple Events" must be ON.
    (One-time toggle; without it, Chrome refuses `execute javascript`.)
  - You must be logged into x.com in Chrome.

Read commands:
  x-browser.py whoami                 # the logged-in account (name + @handle)
  x-browser.py home [--limit N]       # visible tweets on your home timeline
  x-browser.py read <tweet-id|url>    # a single tweet's text + author
  x-browser.py search "<query>"       # latest results for a search (--limit N)

Engagement commands (opt-in writes):
  x-browser.py like  <tweet-id|url>            # like a tweet
  x-browser.py reply <tweet-id|url> "<text>"   # post a reply

Engagement notes:
  - `like` is pure DOM (a synthetic click is honored by X) — reliable.
  - `reply` is a HYBRID: JS fills the composer, but the final SUBMIT needs a
    real OS keystroke (System Events Cmd+Return), because X ignores synthetic
    submit events. That means `reply` additionally requires:
      * Accessibility permission for the controlling process (System Events).
      * It briefly brings Chrome to the foreground and activates the x.com tab
        to land the keystroke. Don't run it while typing elsewhere.
  - Writes are public and post under your real handle — confirm intent first.
    For bulk/headless writes with no foregrounding, prefer the API path
    (x-post.py).
"""
from __future__ import annotations

import sys
import json
import time
import base64
import argparse
import subprocess

X_HOSTS = ("x.com", "twitter.com")


class BrowserError(RuntimeError):
    pass


def _osascript(script: str, timeout: int = 20) -> str:  # pragma: no cover - the one osascript seam; every caller is tested against a stub
    try:
        p = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise BrowserError("osascript timed out talking to Chrome")
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0:
        raise BrowserError(err or "osascript failed")
    return out


def _chrome_running() -> bool:  # pragma: no cover - pgrep against a live Chrome
    try:
        p = subprocess.run(["pgrep", "-x", "Google Chrome"],
                           capture_output=True, text=True)
        return p.returncode == 0
    except Exception:
        return False


_TARGET: dict | None = None  # {"win": id, "tab": id} — the ONE page every phase addresses


def _target_script(body: str) -> str:
    """AppleScript that binds theWin/theTab/tabIdx to the RECORDED ids.

    Addressing by id is what makes focus changes unable to retarget: a scan for
    the first matching URL picks a different page when windows reorder.
    """
    w, t = _TARGET["win"], _TARGET["tab"]
    return f'''
tell application "Google Chrome"
  set theWin to missing value
  set theTab to missing value
  set tabIdx to 0
  repeat with w in windows
    if (id of w) is {w} then
      set theWin to w
      set i to 0
      repeat with t in tabs of w
        set i to i + 1
        if (id of t) is {t} then
          set theTab to t
          set tabIdx to i
          exit repeat
        end if
      end repeat
      exit repeat
    end if
  end repeat
  if theTab is missing value then return "__TARGET_GONE__"
{body}
end tell
'''


def _record_target() -> None:
    """Resolve the first x.com tab ONCE and remember its window+tab ids."""
    global _TARGET
    res = _osascript('''
tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      set u to URL of t
      if u contains "x.com" or u contains "twitter.com" then
        return ((id of w) as text) & "," & ((id of t) as text)
      end if
    end repeat
  end repeat
  return "__NO_X_TAB__"
end tell
''')
    if res == "__NO_X_TAB__":
        raise BrowserError("no x.com tab is open in Chrome")
    win, _, tab = res.partition(",")
    _TARGET = {"win": int(win.strip()), "tab": int(tab.strip())}


def run_js(js: str, timeout: int = 20) -> str:
    """Run a JS snippet in the first x.com/twitter.com tab; return its value.

    JS is passed base64-encoded and eval'd inside the tab, so we never fight
    AppleScript/Python quote escaping. The snippet's final expression is the
    return value.
    """
    b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
    if _TARGET is None:
        _record_target()
    script = _target_script(
        f'  return execute theTab javascript "eval(atob(\'{b64}\'))"')
    res = _osascript(script, timeout=timeout)
    if res == "__TARGET_GONE__":
        raise BrowserError("the x.com tab this operation started on is gone — "
                           "refusing to retarget another tab")
    if res == "__NO_X_TAB__":
        raise BrowserError("no x.com tab is open in Chrome")
    if res.startswith("__JSERR__"):
        raise BrowserError("page JS error: " + res[len("__JSERR__"):])
    return res


def ensure_tab(url: str, settle: float = 4.0, max_wait: float = 15.0) -> None:
    """Point an x.com tab at `url` (reuse one if present, else open a new tab),
    then wait for document.readyState == 'complete' plus a short settle for the
    React SPA to render."""
    global _TARGET
    _TARGET = None  # a new operation: never inherit the previous page's identity
    if not _chrome_running():
        raise BrowserError("Google Chrome is not running")
    b64 = base64.b64encode(url.encode("utf-8")).decode("ascii")
    script = f'''
tell application "Google Chrome"
  if (count of windows) is 0 then make new window
  set theWin to missing value
  set theTab to missing value
  repeat with w in windows
    repeat with t in tabs of w
      set u to URL of t
      if u contains "x.com" or u contains "twitter.com" then
        set theWin to w
        set theTab to t
        exit repeat
      end if
    end repeat
    if theTab is not missing value then exit repeat
  end repeat
  set target to (do shell script "python3 -c \\"import base64,sys;sys.stdout.write(base64.b64decode('{b64}').decode())\\"")
  if theTab is missing value then
    set theWin to front window
    set theTab to make new tab at end of tabs of theWin with properties {{URL:target}}
  else
    set URL of theTab to target
  end if
  return ((id of theWin) as text) & "," & ((id of theTab) as text)
end tell
'''
    ids = _osascript(script)
    win, _, tab = ids.partition(",")
    try:
        _TARGET = {"win": int(win.strip()), "tab": int(tab.strip())}
    except ValueError:
        raise BrowserError(f"could not identify the x.com tab (got {ids!r})")
    # Poll for load completion.
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            state = run_js("document.readyState", timeout=10)
        except BrowserError:
            state = ""
        if state == "complete":
            break
        time.sleep(0.5)
    time.sleep(settle)


def _extract_tweets_js(limit: int) -> str:
    return (
        "(function(){try{var out=[];"
        "var arts=document.querySelectorAll('article[data-testid=\\\"tweet\\\"]');"
        "for(var i=0;i<arts.length&&out.length<" + str(limit) + ";i++){var a=arts[i];"
        "var u=a.querySelector('[data-testid=\\\"User-Name\\\"]');"
        "var tx=a.querySelector('[data-testid=\\\"tweetText\\\"]');"
        "var tm=a.querySelector('time');"
        "out.push({user:u?u.innerText.replace(/\\n/g,' '):'',"
        "text:tx?tx.innerText:'',time:tm?tm.getAttribute('datetime'):''});}"
        "return JSON.stringify(out);}catch(e){return '__JSERR__'+e.message;}})()"
    )


def cmd_whoami() -> int:
    ensure_tab("https://x.com/home")
    js = (
        "(function(){try{var b=document.querySelector('[data-testid=\\\"SideNav_AccountSwitcher_Button\\\"]');"
        "return JSON.stringify({account:b?b.innerText.replace(/\\n/g,' | '):'(not found — logged out?)'});"
        "}catch(e){return '__JSERR__'+e.message;}})()"
    )
    data = json.loads(run_js(js))
    print(data["account"])
    return 0


def cmd_home(limit: int) -> int:
    ensure_tab("https://x.com/home")
    tweets = json.loads(run_js(_extract_tweets_js(limit)))
    if not tweets:
        print("(no tweets visible — try scrolling or re-running)")
        return 0
    for t in tweets:
        print(f"{t['user']}\n  {t['text']}\n  [{t['time']}]\n")
    return 0


def cmd_read(ref: str) -> int:
    if ref.startswith("http"):
        url = ref
    else:
        url = f"https://x.com/i/web/status/{ref}"
    ensure_tab(url)
    js = (
        "(function(){try{var a=document.querySelector('article[data-testid=\\\"tweet\\\"]');"
        "if(!a)return JSON.stringify({error:'tweet not found'});"
        "var u=a.querySelector('[data-testid=\\\"User-Name\\\"]');"
        "var tx=a.querySelector('[data-testid=\\\"tweetText\\\"]');"
        "var tm=a.querySelector('time');"
        "return JSON.stringify({user:u?u.innerText.replace(/\\n/g,' '):'',"
        "text:tx?tx.innerText:'',time:tm?tm.getAttribute('datetime'):''});"
        "}catch(e){return '__JSERR__'+e.message;}})()"
    )
    data = json.loads(run_js(js))
    if data.get("error"):
        print(data["error"])
        return 1
    print(f"{data['user']}\n{data['text']}\n[{data['time']}]")
    return 0


def cmd_search(query: str, limit: int) -> int:
    from urllib.parse import quote
    url = f"https://x.com/search?q={quote(query)}&f=live"
    ensure_tab(url)
    tweets = json.loads(run_js(_extract_tweets_js(limit)))
    if not tweets:
        print("(no results visible)")
        return 0
    for t in tweets:
        print(f"{t['user']}\n  {t['text']}\n  [{t['time']}]\n")
    return 0


def _status_url(ref: str) -> str:
    return ref if ref.startswith("http") else f"https://x.com/i/web/status/{ref}"


def cmd_like(ref: str) -> int:
    ensure_tab(_status_url(ref), settle=5.0)
    click = (
        "(function(){var a=document.querySelector('article[data-testid=\\\"tweet\\\"]');"
        "if(!a)return JSON.stringify({error:'tweet not found'});"
        "if(a.querySelector('[data-testid=\\\"unlike\\\"]'))return JSON.stringify({already:true});"
        "var b=a.querySelector('[data-testid=\\\"like\\\"]');"
        "if(!b)return JSON.stringify({error:'no like button'});b.click();"
        "return JSON.stringify({clicked:true});})()"
    )
    r = json.loads(run_js(click))
    if r.get("error"):
        print(r["error"]); return 1
    if r.get("already"):
        print("already liked"); return 0
    time.sleep(1.5)
    ver = (
        "(function(){var a=document.querySelector('article[data-testid=\\\"tweet\\\"]');"
        "return JSON.stringify({liked: !!(a&&a.querySelector('[data-testid=\\\"unlike\\\"]'))});})()"
    )
    if json.loads(run_js(ver)).get("liked"):
        print("liked"); return 0
    print("like click sent but not confirmed"); return 1


def _os_submit_via_keystroke() -> None:  # pragma: no cover - sends real keystrokes to the frontmost app
    """Send a REAL Cmd+Return to Chrome to submit the focused composer.

    X ignores synthetic JS submit events (untrusted), so the post button can't
    be driven from inside the page. This brings Chrome forward, activates the
    x.com tab, and sends an OS-level keystroke via System Events (needs
    Accessibility permission)."""
    if _TARGET is None:
        raise BrowserError("no target tab recorded — refusing to submit blind")
    osa = _target_script(
        "  set index of theWin to 1\n"
        "  set active tab index of theWin to tabIdx\n"
        "  activate"
    ) + '''
delay 0.7
tell application "System Events"
  keystroke return using command down
end tell
'''
    _osascript(osa)


def cmd_reply(ref: str, text: str) -> int:
    ensure_tab(_status_url(ref), settle=5.0)
    run_js(
        "(function(){var a=document.querySelector('article[data-testid=\\\"tweet\\\"]');"
        "var b=a&&a.querySelector('[data-testid=\\\"reply\\\"]');if(b)b.click();return 'ok';})()"
    )
    time.sleep(2.5)
    tjson = json.dumps(text)
    fill = (
        "(function(){var ed=document.querySelector('[data-testid=\\\"tweetTextarea_0\\\"]');"
        "if(!ed)return 'noeditor';ed.focus();"
        "document.execCommand('selectAll',false,null);"
        "document.execCommand('delete',false,null);"
        "document.execCommand('insertText',false," + tjson + ");"
        "return ed.innerText;})()"
    )
    if run_js(fill) == "noeditor":
        raise BrowserError("reply composer did not open")
    time.sleep(0.5)
    _os_submit_via_keystroke()
    time.sleep(4.0)
    needle = json.dumps(text[:20])
    ver = (
        "(function(){var arts=document.querySelectorAll('article[data-testid=\\\"tweet\\\"]');"
        "var found=false;arts.forEach(function(a){var tx=a.querySelector('[data-testid=\\\"tweetText\\\"]');"
        "if(tx&&tx.innerText.indexOf(" + needle + ")>-1)found=true;});"
        "return JSON.stringify({posted:found});})()"
    )
    if json.loads(run_js(ver)).get("posted"):
        print("reply posted"); return 0
    print("reply submitted but not confirmed (check the tweet)"); return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Browser-mode X (real Chrome, no API key)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    p_home = sub.add_parser("home")
    p_home.add_argument("--limit", type=int, default=10)
    p_read = sub.add_parser("read")
    p_read.add_argument("ref", help="tweet id or full URL")
    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_like = sub.add_parser("like")
    p_like.add_argument("ref", help="tweet id or full URL")
    p_reply = sub.add_parser("reply")
    p_reply.add_argument("ref", help="tweet id or full URL")
    p_reply.add_argument("text", help="reply text (posts publicly under your handle)")
    args = ap.parse_args()

    try:
        if args.cmd == "whoami":
            return cmd_whoami()
        if args.cmd == "home":
            return cmd_home(args.limit)
        if args.cmd == "read":
            return cmd_read(args.ref)
        if args.cmd == "search":
            return cmd_search(args.query, args.limit)
        if args.cmd == "like":
            return cmd_like(args.ref)
        if args.cmd == "reply":
            return cmd_reply(args.ref, args.text)
    except BrowserError as e:
        msg = str(e)
        print(f"browser-mode error: {msg}", file=sys.stderr)
        if "Allow JavaScript from Apple Events" in msg or "execute" in msg.lower():
            print("hint: enable Chrome > View > Developer > "
                  "'Allow JavaScript from Apple Events'", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

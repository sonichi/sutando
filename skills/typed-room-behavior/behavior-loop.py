#!/usr/bin/env python3
"""typed-room-behavior — the per-type agent-behavior loop (Track 13's moat; owner directive 2026-07-12).

MANIFEST-DRIVEN: each room type's behavior is a DECLARATION (manifests/<type>.json, schema in
behavior-manifest.schema.json) — the local stand-in for the trusted platform registry. Per the
Track-13 operating contract the manifest is resolved by TYPE from the registry, never
fetched-and-obeyed from room-supplied content (injection-safe). A new room type = a new manifest,
not new code paths.

Each pass, for every registered typed room:
  1. LOAD   the room's data object (vault path from the manifest)
  2. ANALYZE with the type's attention primitive (module+function from the manifest)
  3. SPLIT  by autonomy tier (safe-reason set from the manifest):
       SAFE (additive)      -> AUTO-EXECUTE (e.g. enrich via the manifest's Hub tool binding)
       ADVANCE (state move) -> PROPOSE in the room's OWN verb grammar (verb templates from the
                               manifest) — the owner approves by replying with the verb; the type's
                               existing command handler applies it. No bespoke approval machinery.
  4. POST one concise room update only when something happened (per-room cooldown from manifest).

Autonomy: per-room `autonomy` field in the data object; default from manifest ("safe").
  propose = nothing auto; safe = additive auto, advance proposes. 'full' never auto-fires advance
  in v1 regardless (deliberate).

v1 executors: enrich-via-Hub-tool (first-party: endpoint+auth from our Neon skills table +
X-Sutando-User-Id metering). Registry v1 = state/typed-rooms.json; later a RegistryRoom.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
MANIFEST_DIR = os.path.join(_HERE, "manifests")
OWNER_USER_ID = "a2f858a2-d444-4bc9-9165-d432cb110ed0"  # qingyun@ag2.ai — metering header


def load_manifest(rtype, manifest_dir=None):
    path = os.path.join(manifest_dir or MANIFEST_DIR, f"{rtype}.json")
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def _load_module(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_REPO, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── pure logic (unit-tested; parameterized by manifest) ─────────────────────
def split_actions(items, autonomy, safe_reasons):
    """Split attention items into (auto, propose) per the autonomy tier.

    AUTO only if autonomy=='safe' AND every reason of the item is in the manifest's
    safe-reason set — any advance-class reason drags the whole item to propose (acting
    on half an item invites inconsistent state). autonomy=='propose' or any unknown
    value sends everything to propose (fail-safe). Empty-reason items never auto.
    """
    if autonomy != "safe":
        return [], list(items)
    safe = set(safe_reasons or [])
    auto, propose = [], []
    for it in items:
        reasons = it.get("reasons", [])
        (auto if (reasons and all(r in safe for r in reasons)) else propose).append(it)
    return auto, propose


def proposal_verb(item, verbs):
    """Resolve the manifest's verb template for the item's first matching reason.
    Templates use the room's own grammar with '{name}' interpolation."""
    name = item.get("name") or item.get("title") or item.get("id") or "?"
    for r in item.get("reasons", []):
        if r in verbs:
            return verbs[r].replace("{name}", str(name))
        for key, tpl in verbs.items():
            if key.endswith("*") and r.startswith(key[:-1]):
                return tpl.replace("{name}", str(name))
    return verbs.get("default", "`note {name}: ...`").replace("{name}", str(name))


def linkedin_url_from_notes(notes):
    for tok in (notes or "").replace("|", " ").split():
        if "linkedin.com/in/" in tok:
            return tok.strip(".,;")
    return None


def should_run_room(state, room, now, cooldown):
    if room not in state:
        return True  # never run → always eligible, regardless of cooldown arithmetic
    entry = state[room]
    # Two schema generations coexist: a bare epoch stamp (v1) and a dict with
    # last_run + proposed_fps (v2, the proposal-dedup upgrade). Read both.
    if isinstance(entry, dict):
        entry = entry.get("last_run")
    try:
        last = int(entry)
    except (TypeError, ValueError):
        return True  # corrupt stamp → fail-open toward running
    return (now - last) >= cooldown


def proposal_fp(item, verbs):
    """Stable fingerprint of a proposal AS POSTED: same entity + same reasons +
    same suggested verb → the owner has already seen exactly this ask."""
    nm = item.get("name") or item.get("title") or item.get("id") or "?"
    key = f"{nm}|{','.join(sorted(item.get('reasons', [])))}|{proposal_verb(item, verbs)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def filter_new_proposals(state, room, propose, verbs):
    """Drop proposals already posted in a prior pass; return (new, current_fps).

    Without this, an unanswered advance-tier proposal re-posts on EVERY
    cooldown expiry, forever — the loop's own reports showed identical
    proposed-counts across cycles (observed 2026-07-20). The stored set is
    replaced by the CURRENT analysis's fingerprints each pass, so an issue
    that resolves and later reappears legitimately re-proposes."""
    prior = state.get(room)
    prior_fps = set(prior.get("proposed_fps", [])) if isinstance(prior, dict) else set()
    current = [(it, proposal_fp(it, verbs)) for it in propose]
    new = [it for (it, fp) in current if fp not in prior_fps]
    return new, [fp for (_it, fp) in current]


# ── plumbing ─────────────────────────────────────────────────────────────────
def gateway_call(payload):
    tok = None
    for line in open(os.path.join(_REPO, ".env")):
        if line.startswith("AG2_REMOTE_TOKEN="):
            tok = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    base, secret = tok.split("|", 1)
    base = base.strip().strip("'").strip('"')
    req = urllib.request.Request(base.rstrip("/") + "/v1/room", data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {secret.strip()}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "sutando-core/1.0"})
    r = urllib.request.urlopen(req, timeout=30)
    return json.loads(r.read().decode() or "{}")


def hub_tool_call(slug, tool, arguments, timeout=290):
    """First-party MCP call: endpoint+auth from our Neon skills table + metering header."""
    neon = None
    for line in open(os.path.join(_REPO, ".env")):
        if line.startswith("NEON_DATABASE_URL="):
            neon = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    row = subprocess.run(["psql", neon, "-At", "-F", "\t", "-c",
                          f"SELECT mcp_endpoint_url, coalesce(mcp_auth_header,'') FROM skills WHERE slug='{slug}';"],
                         capture_output=True, text=True).stdout.strip()
    url, auth = row.split("\t")
    req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                       "params": {"name": tool, "arguments": arguments}}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          "X-Sutando-User-Id": OWNER_USER_ID})
    if ":" in auth.split(" ")[0] or auth.lower().startswith(("authorization:", "x-api-key:")):
        k, v = auth.split(":", 1)
        req.add_header(k.strip(), v.strip())
    elif auth:
        req.add_header("Authorization", auth if auth.lower().startswith("bearer") else f"Bearer {auth}")
    r = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(r.read().decode() or "{}")


# ── generic type engine (manifest-configured) ───────────────────────────────
class TypeEngine:
    """One engine per type, wired entirely from its manifest."""

    def __init__(self, manifest):
        self.m = manifest
        self.type_name = manifest["type"]
        self.mod = _load_module(manifest["analyze"]["module"], f"mod_{self.type_name}")
        self._analyze_fn = getattr(self.mod, manifest["analyze"]["function"])
        # save/widget helpers exist on piperoom's module; used only when present
        self._pipe_mod = self.mod if hasattr(self.mod, "save_pipeline") else None
        aux = _load_module("skills/piperoom/piperoom-command.py", "prc_tok")
        self.url, self.secret = aux.resolve_token(_REPO)

    def load(self, room):
        do = self.m["data_object"]
        if self._pipe_mod:
            return self._pipe_mod.load_pipeline(self.url, self.secret, room)
        import base64
        res = gateway_call({"op": "prep_get", "room_id": room,
                            "folder": do["folder"], "filename": do["filename"]})
        if res.get("content_b64"):
            return json.loads(base64.b64decode(res["content_b64"]).decode())
        if res.get("content"):
            return json.loads(res["content"])
        return None

    def analyze(self, data):
        return self._analyze_fn(data)

    def execute_auto(self, room, data, items, dry_run=False):
        """Run the manifest's safe-tier actions. v1 executor: enrich via Hub tool
        (additive: notes append + updated stamp), bounded by max_per_pass.

        Returns (results, unexecutable) — items whose preconditions fail (e.g. no
        linkedin_url) come back in `unexecutable` so the caller degrades them to
        proposals instead of dropping them silently."""
        results = []
        handled_ids = set()
        for action in self.m["tiers"]["safe"].get("actions", []):
            if action.get("name") != "enrich" or not action.get("tool"):
                continue
            cap = action.get("max_per_pass", 3)
            cands = []
            for it in items:  # scan ALL eligible items, cap after filtering
                if len(cands) >= cap:
                    break
                entry = next((d for d in data.get("deals", []) if d.get("id") == it.get("id")), None)
                if not entry:
                    continue
                if action.get("requires") == "linkedin_url_in_notes":
                    li = linkedin_url_from_notes(entry.get("notes"))
                    if not li:
                        continue
                    cands.append((entry, li))
                    handled_ids.add(it.get("id"))
                else:
                    cands.append((entry, None))
                    handled_ids.add(it.get("id"))
            if not cands:
                continue
            if dry_run:
                results += [f"would enrich: {e.get('name')}" for e, _ in cands]
                continue
            try:
                res = hub_tool_call(action["tool"]["hub_slug"], action["tool"]["tool"],
                                    {"leads": [{"name": e.get("name", ""), "linkedin_url": li}
                                               for e, li in cands if li]})
                text = "".join(c.get("text", "") for c in res.get("result", {}).get("content", []))
                stamp = time.strftime("%Y-%m-%d")
                for e, _ in cands:
                    e["notes"] = (e.get("notes", "") +
                                  f" | {stamp}: auto-enriched (behavior-loop; {action['tool']['hub_slug']} run submitted)")
                    e["updated"] = stamp
                if self._pipe_mod:
                    self._pipe_mod.save_pipeline(self.url, self.secret, room, data)
                    self._pipe_mod.restamp_widget(self.url, self.secret, room, data)
                results += [f"enrich submitted: {e.get('name')}" for e, _ in cands]
                if text:
                    results.append(f"{action['tool']['hub_slug']}: {text[:160]}")
            except Exception as ex:  # noqa: BLE001 — loop must not die on tool failure
                results.append(f"enrich failed ({type(ex).__name__}) — left for next pass")
        unexecutable = [it for it in items if it.get("id") not in handled_ids]
        return results, unexecutable


# ── the loop ─────────────────────────────────────────────────────────────────
def run(registry_path, state_path, dry_run=False, force=False, manifest_dir=None):
    rooms = json.load(open(registry_path))
    try:
        state = json.load(open(state_path))
    except (OSError, ValueError):
        state = {}
    now = int(time.time())
    engines = {}
    reports = []

    for rec in rooms:
        room, rtype = rec["room"], rec["type"]
        manifest = load_manifest(rtype, manifest_dir)
        if not manifest:
            reports.append((room, f"no manifest for type {rtype}"))
            continue
        cooldown = manifest.get("cooldown_s", 10800)
        if not force and not should_run_room(state, room, now, cooldown):
            reports.append((room, "cooldown — skipped"))
            continue
        try:
            eng = engines.setdefault(rtype, TypeEngine(manifest))
            data = eng.load(room)
        except Exception as ex:  # noqa: BLE001 — one room's failure must not kill the pass
            reports.append((room, f"load failed ({type(ex).__name__}) — skipped"))
            continue
        if not data:
            reports.append((room, "no data object"))
            continue
        autonomy = data.get("autonomy", manifest.get("autonomy_default", "safe"))
        items = eng.analyze(data)
        if not items:
            reports.append((room, "all clear — no post"))
            continue
        safe_reasons = manifest["tiers"]["safe"].get("reasons", [])
        verbs = manifest["tiers"]["advance"].get("verbs", {})
        auto, propose = split_actions(items, autonomy, safe_reasons)
        auto_results, unexecutable = (eng.execute_auto(room, data, auto, dry_run=dry_run)
                                      if auto else ([], []))
        propose = propose + unexecutable  # precondition-failed autos surface as proposals
        # Only NEW asks post; unanswered ones stay in current_fps and are held.
        new_propose, current_fps = filter_new_proposals(state, room, propose, verbs)
        if not auto_results and not new_propose:
            state[room] = {"last_run": now, "proposed_fps": current_fps}
            held = len(propose) - len(new_propose)
            reports.append((room, f"no post (nothing new; {held} unanswered proposal(s) held)"))
            continue

        lines = [f"**[core: qingyun-001]** 🔁 behavior-loop ({rtype} v{manifest['version']}, autonomy={autonomy}):"]
        if auto_results:
            lines.append("**Auto-executed (safe tier):**")
            lines += [f"  • {r}" for r in auto_results]
        if new_propose:
            lines.append("**Proposed — reply with the verb to apply:**")
            for it in new_propose[:6]:
                nm = it.get("name") or it.get("title") or it.get("id")
                lines.append(f"  • **{nm}** ({', '.join(it.get('reasons', []))}) → {proposal_verb(it, verbs)}")
        body = "\n".join(lines)
        if dry_run:
            reports.append((room, "DRY-RUN post:\n" + body))
        else:
            gateway_call({"op": "message", "room_id": room, "body": body})
            state[room] = {"last_run": now, "proposed_fps": current_fps}
            reports.append((room, f"posted (auto={len(auto_results)}, proposed={len(new_propose)} new of {len(propose)})"))

    if not dry_run:
        json.dump(state, open(state_path, "w"), indent=2)
    return reports


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Manifest-driven per-type agent-behavior loop.")
    ap.add_argument("--registry", default=os.path.join(_REPO, "workspace", "state", "typed-rooms.json"))
    ap.add_argument("--state", default=os.path.join(_REPO, "workspace", "state", "behavior-loop.json"))
    ap.add_argument("--manifests", default=None, help="override manifest dir (default: ./manifests)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore per-room cooldown")
    args = ap.parse_args(argv)
    for room, report in run(args.registry, args.state, dry_run=args.dry_run,
                            force=args.force, manifest_dir=args.manifests):
        print(f"[{room}] {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

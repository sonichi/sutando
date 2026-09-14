#!/usr/bin/env python3
"""Set up Superpower Station marketplace skills and cloud tools from the agent.

The Marketplace UI does three things for a signed-in owner: activate a cloud
tool, install a local skill, keep installed skills current. All three are cloud
API calls plus (for skills) a bundle landed in the core's skills dir. This CLI
does the same, so the owner can say "set up what this doc needs" instead of
clicking through the Marketplace.

  find <query> [--kind skill|cloud_tool]   search the catalog
  status                                   owned vs on-disk: missing / outdated / disabled
  install <slug...> [--yes]                install skills + activate cloud tools
  update [slug...] [--yes]                 re-fetch missing / outdated skills (no charge)
  uninstall <slug> [--yes]                 remove from the account and from disk

Nothing is written without --yes. Exit codes: 0 ok (for a plan: free, safe to
apply), 1 some items failed, 2 usage / not signed in, 3 plan spends credits or
removes something — confirm with the owner before re-running with --yes.

Cloud contract (agent-universe): install is POST /api/skills/{uuid}/install and
takes the UUID ONLY — a slug is resolved through the station catalog first.
Re-installing an owned item never charges again. A newly activated cloud tool
only reaches the core after a core restart (MCP tools are listed at startup).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))
import cloud_auth  # noqa: E402
import skill_install  # noqa: E402

from util_paths import claude_home_path  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CONFIRM = 0, 1, 2, 3
TIER_RANK = {"free": 0, "plus": 1, "pro": 2, "max": 3}
BUNDLE_TIMEOUT_S = 60
STATION_MCP_SERVER = "sutando-station"
RESTART_HINT = (
    "New cloud tools are active on your account, but I can only use them after a core "
    "restart: open Settings → Agent → Restart Core (also under Settings → Services, or the "
    "restart button in the Agent Console). Restarting ends my current session; your tasks "
    "and files are kept."
)


class Usage(Exception):
    """A problem the owner has to fix before anything can run (exit 2)."""


# --------------------------------------------------------------------------- context


class Context:
    """Everything a command needs: cloud session, install root, this agent's id.

    Tests build one directly with a fake `http` / `download`.
    """

    def __init__(
        self,
        base: str,
        token: str,
        dest_root: Path,
        agent_id: str | None,
        config_dir: Path | None = None,
        insecure_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self.base = base
        self.token = token
        self.dest_root = dest_root
        self.agent_id = agent_id
        self.config_dir = config_dir
        self.insecure_hosts = insecure_hosts
        self._catalog: list[dict] | None = None
        self._featured_connectors: list[dict] = []

    def http(self, method: str, path: str, body: Any = None) -> Any:
        return cloud_auth.cloud_request(
            self.base, self.token, method, path, body, insecure_hosts=self.insecure_hosts
        )

    def download(self, url: str) -> bytes:
        """Fetch a bundle. No bearer: the URL is presigned (or author-hosted),
        and integrity comes from the signingHash the authenticated API returned."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" and parsed.hostname not in self.insecure_hosts:
            raise ValueError(f"refusing non-https bundle URL: {url[:80]}")
        req = urllib.request.Request(url, headers={"User-Agent": "sutando-marketplace"})
        with urllib.request.urlopen(req, timeout=BUNDLE_TIMEOUT_S) as resp:
            data = resp.read(skill_install.MAX_BYTES + 1)
        if len(data) > skill_install.MAX_BYTES:
            raise ValueError(f"bundle exceeds safety limit of {skill_install.MAX_BYTES} bytes")
        return data

    # -- cloud reads, cached per run

    def catalog(self) -> list[dict]:
        if self._catalog is None:
            data = self.http("GET", "/api/station/catalog?kind=all") or {}
            self._catalog = [i for i in data.get("items") or [] if isinstance(i, dict)]
            self._featured_connectors = [
                i for i in data.get("featuredConnectors") or [] if isinstance(i, dict)
            ]
        return self._catalog

    def me(self) -> dict:
        return self.http("GET", "/api/me") or {}

    def inventory(self) -> dict:
        return self.http("GET", "/api/me/inventory") or {}


def build_context(args: argparse.Namespace) -> Context:
    base, token = cloud_auth.read_cloud_auth(_workspace())
    if not token:
        raise Usage(
            "Not signed in to Sutando Cloud. Sign in from the desktop app (it opens the "
            "Marketplace sign-in), then ask again."
        )
    dest = Path(args.dest_root).expanduser().resolve() if args.dest_root else claude_home_path("skills")
    return Context(base or cloud_auth.DEFAULT_CLOUD_ORIGIN, token, dest, _agent_id(), claude_home_path())


def _workspace() -> Path:
    try:
        from sutando_config import resolve_workspace  # noqa: PLC0415

        return Path(resolve_workspace())
    except Exception:  # noqa: BLE001 — no workspace config still has a Keychain session
        return REPO_ROOT / "workspace"


def _agent_id() -> str | None:
    """This agent's AG2 Space id, so an install also equips it. None when the
    install is not enrolled — the account-level install still works."""
    try:
        sys.path.insert(0, str(REPO_ROOT / "src" / "runtime-api"))
        import rundir  # noqa: PLC0415

        aid = rundir.agent_id()
        return None if aid == rundir.DEFAULT_ACTOR else aid
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- helpers


def credits_for(item: dict) -> int:
    """One-time credits an install spends. Metered cloud tools cost per call, not here."""
    price = item.get("price") or {}
    if price.get("model") == "one_time":
        try:
            return int(price.get("credits") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def price_label(item: dict) -> str:
    price = item.get("price") or {}
    model, credits = price.get("model"), price.get("credits") or 0
    if model == "one_time" and credits:
        return f"{credits} credits once"
    if model in ("per_call", "per_unit") and credits:
        return f"{credits} credits per {price.get('unitLabel') or 'call'}"
    return "free"


def tier_ok(required: str | None, plan: str | None) -> bool:
    return TIER_RANK.get(plan or "free", 0) >= TIER_RANK.get(required or "free", 0)


def explain_error(exc: cloud_auth.CloudError, plan: str | None = None) -> str:
    if exc.status == 401 or exc.code in ("unauthenticated", "no user"):
        return "your cloud session expired — sign in again from the desktop app"
    if exc.status == 402 or exc.code == "insufficient_credits":
        need = exc.body.get("required")
        return f"needs {need} credits — top up in the Marketplace" if need else "not enough credits — top up in the Marketplace"
    if exc.code == "tier_required":
        tier = exc.body.get("tierRequired") or "a higher plan"
        return f"requires the {tier} plan" + (f" (you're on {plan})" if plan else "")
    if exc.status == 404:
        return "not found in the marketplace"
    if exc.code == "network":
        return f"could not reach Sutando Cloud ({exc.detail})"
    return f"cloud error {exc.status or ''} {exc.code}".strip()


def local_state(dest_root: Path, slug: str) -> str:
    """absent | bundled (symlink into the engine) | disabled | present"""
    target = dest_root / slug
    if target.is_symlink():
        return "bundled"
    if not target.is_dir():
        return "absent"
    if (target / "SKILL.md").is_file():
        return "present"
    if (target / "SKILL.md.disabled").is_file():
        return "disabled"
    return "absent"


def applies_to_agent(row: dict, agent_id: str | None) -> bool:
    """Same scoping as the Marketplace reconcile: unassigned rows are account-wide."""
    agents = row.get("agents") or []
    return not agent_id or not agents or agent_id in agents


def emit(args: argparse.Namespace, payload: dict, text: str) -> None:
    print(json.dumps(payload, indent=2) if args.json else text)


# --------------------------------------------------------------------------- find


def cmd_find(ctx: Context, args: argparse.Namespace) -> int:
    kind = args.kind or "all"
    q = urllib.parse.quote(" ".join(args.query))
    data = ctx.http("GET", f"/api/station/catalog?kind={kind}&q={q}") or {}
    items = [i for i in data.get("items") or [] if isinstance(i, dict) and i.get("kind") != "connector"]
    rows = [
        {
            "slug": i.get("slug"),
            "name": i.get("name"),
            "kind": i.get("kind"),
            "price": price_label(i),
            "tier": i.get("tierRequired") or "free",
            "owned": bool(i.get("acquired")),
            "description": (i.get("description") or "")[:160],
        }
        for i in items
    ]
    lines = [f"{len(rows)} result(s)"] + [
        f"- {r['slug']} [{r['kind']}] {r['price']}, tier {r['tier']}"
        + (" — owned" if r["owned"] else "")
        + (f"\n    {r['description']}" if r["description"] else "")
        for r in rows
    ]
    emit(args, {"results": rows}, "\n".join(lines))
    return EXIT_OK


# --------------------------------------------------------------------------- install


def resolve(ctx: Context, slug: str) -> dict:
    """Catalog row for a slug, or a {'error': ...} row. Connectors can't be
    installed from here (they need a browser OAuth)."""
    slug = slug.strip().lower()
    for item in ctx.catalog():
        if (item.get("slug") or "").lower() == slug:
            return item
    for conn in ctx._featured_connectors:
        if (conn.get("slug") or conn.get("id") or "").lower() == slug:
            return {"slug": slug, "name": conn.get("name"), "kind": "connector"}
    return {"slug": slug, "error": "not_found"}


def build_plan(ctx: Context, slugs: list[str]) -> dict:
    me = ctx.me()
    plan_name = me.get("plan") or "free"
    wallet = me.get("walletCredits")
    items = []
    seen: set[str] = set()
    for raw in slugs:
        if raw.lower() in seen:
            continue
        seen.add(raw.lower())
        row = resolve(ctx, raw)
        slug = row.get("slug") or raw
        entry: dict[str, Any] = {"slug": slug, "name": row.get("name"), "kind": row.get("kind")}
        if row.get("error"):
            entry.update(action="skip", reason="not found in the marketplace — check the slug with `find`")
        elif row.get("kind") == "connector":
            entry.update(action="skip", reason="connector — connect it from the Marketplace (needs a browser sign-in)")
        elif row.get("comingSoon"):
            entry.update(action="skip", reason="coming soon — not installable yet")
        elif not tier_ok(row.get("tierRequired"), plan_name):
            entry.update(action="skip", reason=f"requires the {row.get('tierRequired')} plan (you're on {plan_name})")
        else:
            owned = bool(row.get("acquired"))
            entry["id"] = row.get("id")
            entry["price"] = price_label(row)
            entry["credits"] = 0 if owned else credits_for(row)
            if row.get("kind") == "cloud_tool":
                entry["action"] = "already_active" if owned else "activate"
            else:
                state = local_state(ctx.dest_root, slug)
                if state == "bundled":
                    entry.update(action="skip", reason="already bundled with Sutando")
                elif owned and state in ("present", "disabled"):
                    entry["action"] = "already_installed"
                    if state == "disabled":
                        entry["note"] = "installed but disabled — enable it in the Marketplace"
                else:
                    entry["action"] = "download" if owned else "install"
        items.append(entry)
    total = sum(i.get("credits", 0) for i in items)
    return {
        "plan": plan_name,
        "wallet_credits": wallet,
        "total_credits": total,
        "insufficient_credits": isinstance(wallet, int) and total > wallet,
        "items": items,
    }


def render_plan(plan: dict) -> str:
    labels = {
        "install": "install skill",
        "download": "download skill (already owned)",
        "activate": "activate cloud tool",
        "already_active": "already active",
        "already_installed": "already installed",
        "skip": "skip",
    }
    lines = []
    for i in plan["items"]:
        line = f"- {i['slug']}: {labels.get(i['action'], i['action'])}"
        if i.get("price") and i["action"] in ("install", "activate"):
            line += f" ({i['price']})"
        if i.get("reason"):
            line += f" — {i['reason']}"
        if i.get("note"):
            line += f" — {i['note']}"
        lines.append(line)
    lines.append(
        f"Credits this spends: {plan['total_credits']} (wallet: {plan['wallet_credits']}, plan: {plan['plan']})"
    )
    if plan["insufficient_credits"]:
        lines.append("Not enough credits for everything — paid items will fail until you top up.")
    return "\n".join(lines)


def install_item(ctx: Context, item: dict, allow_unsigned: bool, plan_name: str) -> dict:
    body = {"agentId": ctx.agent_id} if ctx.agent_id else None
    try:
        resp = ctx.http("POST", f"/api/skills/{item['id']}/install", body) or {}
    except cloud_auth.CloudError as exc:
        return {"slug": item["slug"], "status": "failed", "reason": explain_error(exc, plan_name)}
    newly = not resp.get("deduped", False)
    if item["kind"] == "cloud_tool":
        return {"slug": item["slug"], "status": "activated" if newly else "already_active", "restart": newly}
    return materialize(ctx, item, resp, allow_unsigned)


def materialize(ctx: Context, item: dict, resp: dict, allow_unsigned: bool) -> dict:
    slug = item["slug"]
    url, sha = resp.get("bundleUrl"), (resp.get("signingHash") or "").lower()
    if not url:
        return {"slug": slug, "status": "failed", "reason": "the marketplace has no downloadable bundle for this skill"}
    if not sha and not allow_unsigned:
        return {"slug": slug, "status": "failed", "reason": "bundle is unsigned (no signingHash); refusing to install it"}
    try:
        data = ctx.download(url)
        digest = hashlib.sha256(data).hexdigest()
        if sha and digest != sha:
            return {"slug": slug, "status": "failed", "reason": "bundle checksum mismatch — nothing was installed"}
        meta = {
            "schema_version": 1,
            "source": "marketplace",
            "slug": slug,
            "skill_id": item.get("id"),
            "version": resp.get("version"),
            "sha256": digest,
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        skill_install.atomic_install(
            slug, ctx.dest_root, lambda temp: skill_install.extract_bundle(data, temp), meta
        )
    except (ValueError, OSError, urllib.error.URLError) as exc:
        return {"slug": slug, "status": "failed", "reason": f"download/install failed: {exc}"}
    return {"slug": slug, "status": "installed", "version": resp.get("version")}


def run_install(ctx: Context, args: argparse.Namespace, slugs: list[str]) -> int:
    plan = build_plan(ctx, slugs)
    actionable = [i for i in plan["items"] if i["action"] in ("install", "download", "activate")]
    if not args.yes:
        needs_confirm = plan["total_credits"] > 0
        hint = (
            "Spends credits — confirm with the owner, then re-run with --yes."
            if needs_confirm
            else ("Free — safe to re-run with --yes." if actionable else "Nothing to do.")
        )
        emit(args, {"plan": plan, "confirm_required": needs_confirm}, render_plan(plan) + "\n" + hint)
        return EXIT_CONFIRM if needs_confirm else EXIT_OK

    results = []
    for item in plan["items"]:
        if item["action"] in ("install", "download", "activate"):
            results.append(install_item(ctx, item, args.allow_unsigned, plan["plan"]))
        elif item["action"] == "skip":
            results.append({"slug": item["slug"], "status": "skipped", "reason": item.get("reason")})
        else:
            results.append({"slug": item["slug"], "status": item["action"]})
    return report_results(args, results)


def report_results(args: argparse.Namespace, results: list[dict]) -> int:
    summary = {
        "installed": [r["slug"] for r in results if r["status"] == "installed"],
        "activated": [r["slug"] for r in results if r["status"] == "activated"],
        "already": [r["slug"] for r in results if r["status"] in ("already_active", "already_installed")],
        "skipped": [{"slug": r["slug"], "reason": r.get("reason")} for r in results if r["status"] == "skipped"],
        "failed": [{"slug": r["slug"], "reason": r.get("reason")} for r in results if r["status"] == "failed"],
        "restart_required": any(r.get("restart") for r in results),
    }
    lines = []
    if summary["installed"]:
        lines.append("Installed (usable now, no restart): " + ", ".join(summary["installed"]))
    if summary["activated"]:
        lines.append("Activated cloud tools: " + ", ".join(summary["activated"]))
    if summary["already"]:
        lines.append("Already set up: " + ", ".join(summary["already"]))
    for s in summary["skipped"]:
        lines.append(f"Skipped {s['slug']}: {s['reason']}")
    for f in summary["failed"]:
        lines.append(f"Failed {f['slug']}: {f['reason']}")
    if summary["restart_required"]:
        summary["restart_hint"] = RESTART_HINT
        lines.append("RESTART REQUIRED: " + RESTART_HINT)
    emit(args, summary, "\n".join(lines) or "Nothing to do.")
    return EXIT_FAILED if summary["failed"] else EXIT_OK


def cmd_install(ctx: Context, args: argparse.Namespace) -> int:
    return run_install(ctx, args, args.slugs)


# --------------------------------------------------------------------------- status / update


def collect_status(ctx: Context) -> dict:
    inv = ctx.inventory()
    skills = []
    for row in inv.get("installed") or []:
        if not isinstance(row, dict) or not applies_to_agent(row, ctx.agent_id):
            continue
        slug = row.get("slug") or ""
        try:
            skill_install.check_slug(slug)
        except ValueError:
            continue
        state = local_state(ctx.dest_root, slug)
        local_v = skill_install.local_version(ctx.dest_root / slug) if state in ("present", "disabled") else None
        cloud_v = row.get("version")
        if state == "bundled":
            status = "bundled"
        elif state == "absent":
            status = "missing"
        elif local_v and cloud_v and local_v != cloud_v:
            status = "outdated"
        elif state == "disabled":
            status = "disabled"
        else:
            status = "ok"
        skills.append({"slug": slug, "name": row.get("name"), "status": status, "local_version": local_v, "latest_version": cloud_v})
    tools = [
        {"slug": t.get("slug"), "name": t.get("name"), "calls_this_period": t.get("callsThisPeriod")}
        for t in inv.get("cloudTools") or []
        if isinstance(t, dict) and applies_to_agent(t, ctx.agent_id)
    ]
    connectors = [
        {"toolkit": c.get("toolkit"), "name": c.get("name"), "status": c.get("status")}
        for c in inv.get("connectors") or []
        if isinstance(c, dict)
    ]
    return {"skills": skills, "cloud_tools": tools, "connectors": connectors, "station_mcp_registered": station_mcp_registered(ctx)}


def station_mcp_registered(ctx: Context) -> bool | None:
    if not ctx.config_dir:
        return None
    try:
        cfg = json.loads((ctx.config_dir / ".claude.json").read_text())
        return STATION_MCP_SERVER in (cfg.get("mcpServers") or {})
    except (OSError, ValueError, AttributeError):
        return False


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    st = collect_status(ctx)
    me = ctx.me()
    st["plan"], st["wallet_credits"] = me.get("plan"), me.get("walletCredits")
    lines = [f"Plan: {st['plan']}, credits: {st['wallet_credits']}", "Skills:"]
    for s in st["skills"] or []:
        extra = ""
        if s["status"] == "outdated":
            extra = f" ({s['local_version']} → {s['latest_version']})"
        lines.append(f"- {s['slug']}: {s['status']}{extra}")
    if not st["skills"]:
        lines.append("- none owned")
    lines.append("Cloud tools: " + (", ".join(t["slug"] for t in st["cloud_tools"]) or "none active"))
    if st["connectors"]:
        lines.append("Connectors: " + ", ".join(f"{c['toolkit']} ({c['status']})" for c in st["connectors"]))
    pending = [s for s in st["skills"] if s["status"] in ("missing", "outdated")]
    if pending:
        lines.append(f"{len(pending)} skill(s) need updating — run `update --yes` (no charge).")
    if st["cloud_tools"] and st["station_mcp_registered"] is False:
        lines.append("Cloud tools aren't wired into the core yet — " + RESTART_HINT)
    emit(args, st, "\n".join(lines))
    return EXIT_OK


def cmd_update(ctx: Context, args: argparse.Namespace) -> int:
    st = collect_status(ctx)
    wanted = {s.lower() for s in args.slugs}
    pending = [
        s for s in st["skills"]
        if s["status"] in ("missing", "outdated") and (not wanted or s["slug"].lower() in wanted)
    ]
    if not pending:
        emit(args, {"pending": []}, "All owned skills are up to date.")
        return EXIT_OK
    if not args.yes:
        text = "\n".join(
            f"- {s['slug']}: {s['status']}" + (f" ({s['local_version']} → {s['latest_version']})" if s["status"] == "outdated" else "")
            for s in pending
        )
        emit(args, {"pending": pending}, text + "\nNo charge — safe to re-run with --yes.")
        return EXIT_OK
    plan_name = (ctx.me().get("plan")) or "free"
    results = []
    for s in pending:
        row = resolve(ctx, s["slug"])
        if row.get("error") or not row.get("id"):
            results.append({"slug": s["slug"], "status": "failed", "reason": "no longer in the marketplace"})
            continue
        item = {"slug": s["slug"], "id": row["id"], "kind": row.get("kind") or "skill"}
        results.append(install_item(ctx, item, args.allow_unsigned, plan_name))
    return report_results(args, results)


# --------------------------------------------------------------------------- uninstall


def cmd_uninstall(ctx: Context, args: argparse.Namespace) -> int:
    slug = args.slug.strip().lower()
    skill_install.check_slug(slug)
    inv = ctx.inventory()
    owned = None
    for kind, key in (("skill", "installed"), ("cloud_tool", "cloudTools")):
        for row in inv.get(key) or []:
            if isinstance(row, dict) and (row.get("slug") or "").lower() == slug:
                owned = {**row, "kind": kind}
    if not owned:
        emit(args, {"error": "not_owned"}, f"{slug} isn't installed on this account.")
        return EXIT_FAILED
    paid = int(owned.get("priceCredits") or 0)
    if not args.yes:
        text = f"Will remove {slug} ({owned['kind']}) from your account" + (" and this machine" if owned["kind"] == "skill" else "") + "."
        if paid:
            text += f" It cost {paid} credits; reinstalling charges again."
        emit(args, {"slug": slug, "kind": owned["kind"], "paid_credits": paid, "confirm_required": True}, text + "\nConfirm with the owner, then re-run with --yes.")
        return EXIT_CONFIRM
    try:
        ctx.http("POST", f"/api/skills/{urllib.parse.quote(slug)}/uninstall", {})
    except cloud_auth.CloudError as exc:
        emit(args, {"slug": slug, "status": "failed", "reason": explain_error(exc)}, f"Failed to uninstall {slug}: {explain_error(exc)}")
        return EXIT_FAILED
    removed_local = False
    if owned["kind"] == "skill" and local_state(ctx.dest_root, slug) in ("present", "disabled"):
        prov = skill_install.read_provenance(ctx.dest_root / slug)
        # Delete only marketplace dirs (ours, or the desktop's with no provenance);
        # a same-named trusted-capabilities install belongs to someone else.
        if prov.get("source") in (None, "marketplace"):
            removed_local = skill_install.remove_skill(slug, ctx.dest_root)
    text = f"Removed {slug} from your account" + (" and this machine." if removed_local else ".")
    if owned["kind"] == "cloud_tool":
        text += " Its tools disappear from the core after the next restart."
    emit(args, {"slug": slug, "status": "uninstalled", "removed_local": removed_local}, text)
    return EXIT_OK


# --------------------------------------------------------------------------- main


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--dest-root", help="install root (default: $CLAUDE_CONFIG_DIR/skills)")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("query", nargs="+")
    f.add_argument("--kind", choices=["skill", "cloud_tool"])
    sub.add_parser("status")
    i = sub.add_parser("install")
    i.add_argument("slugs", nargs="+")
    i.add_argument("--yes", action="store_true")
    i.add_argument("--allow-unsigned", action="store_true", help=argparse.SUPPRESS)
    u = sub.add_parser("update")
    u.add_argument("slugs", nargs="*")
    u.add_argument("--yes", action="store_true")
    u.add_argument("--allow-unsigned", action="store_true", help=argparse.SUPPRESS)
    r = sub.add_parser("uninstall")
    r.add_argument("slug")
    r.add_argument("--yes", action="store_true")
    for sp in (f, i, u, r, sub.choices["status"]):
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        sp.add_argument("--dest-root", default=argparse.SUPPRESS)
    return p


COMMANDS = {
    "find": cmd_find,
    "status": cmd_status,
    "install": cmd_install,
    "update": cmd_update,
    "uninstall": cmd_uninstall,
}


def main(argv: list[str] | None = None, ctx: Context | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        ctx = ctx or build_context(args)
        return COMMANDS[args.cmd](ctx, args)
    except Usage as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except cloud_auth.CloudError as exc:
        print(f"Sutando Cloud: {explain_error(exc)}", file=sys.stderr)
        return EXIT_USAGE if exc.status in (0, 401) else EXIT_FAILED
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())

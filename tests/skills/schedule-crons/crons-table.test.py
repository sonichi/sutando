#!/usr/bin/env python3
"""The Crons room database (skills/schedule-crons/scripts/crons_table.py): rows built from a
crons.json for every runner kind and owner, keyed per host so two hosts never clobber each other;
an entry gone from crons.json is marked Finished, never deleted; sync never writes Last ran (UTC)
or Last result, and skips the write when its rows' digest is unchanged; touch and status stamp one
row; an existing "Crons" database is adopted in place; an unreachable room is one line and exit 2.
No real room is touched: the room capability is a fake written to a temp workspace."""
import contextlib
import importlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "schedule-crons" / "scripts"))
ct = importlib.import_module("crons_table")
access = importlib.import_module("owner_room_access")

HOST, OTHER = "host-a", "host-b"
ROOM = "!ownerdm:test.invalid"
AGENT = "@agent:test.invalid"
WORKER = "d2571c90f75e4907af9f145b01b71c1f"

# A subset of the capability's room_database, to the DATABASE.md contract.
FAKE_DATABASE = textwrap.dedent('''
    import itertools, re
    UNFIT = object()
    _ids = itertools.count()
    DATE_RE = re.compile(r"^\\d{4}-\\d{2}-\\d{2}(T\\d{2}:\\d{2})?$")

    def key(*parts):
        return "|".join(parts)

    def new_id(prefix):
        return f"{prefix}x{next(_ids)}"

    def list_dbs(maps):
        out = [{"id": k, "name": v["name"], "order": v["order"]} for k, v in (maps.get("dbs") or {}).items()]
        return sorted(out, key=lambda x: (x["order"], x["id"]))

    def read_db(maps, db):
        pre = db + "|"
        def pick(name):
            out = [{**v, "id": k[len(pre):]} for k, v in (maps.get(name) or {}).items()
                   if k.startswith(pre) and "|" not in k[len(pre):]]
            return sorted(out, key=lambda x: (x["order"], x["id"]))
        cells = {k[len(pre):]: v for k, v in (maps.get("cells") or {}).items() if k.startswith(pre)}
        return {"id": db, "props": pick("props"), "rows": pick("rows"), "views": pick("views"), "cells": cells}

    def normalize(prop, v):
        if v is None or v == "":
            return None
        t = prop.get("type")
        if t in ("title", "text"):
            return v if isinstance(v, str) else UNFIT
        if t in ("select", "status"):
            return next((o["id"] for o in prop.get("options") or [] if v in (o["id"], o["name"])), UNFIT)
        if t == "date":
            return {"start": v} if isinstance(v, str) and DATE_RE.match(v) else UNFIT
        if t == "number":
            return v if isinstance(v, (int, float)) else UNFIT
        return UNFIT
''')
FAKE_CLI = ("URL_VARS = ()\nTOKEN_VARS = ()\n\n\ndef resolve_token(x):\n    return 'tok'\n\n\n"
            "def resolve_url(x):\n    return x or 'https://collab.test.invalid'\n")
# The client: one room's databases document, kept in the JSON file CRONS_FAKE_STATE names.
FAKE_CLIENT = textwrap.dedent('''
    import json, os
    from contextlib import asynccontextmanager
    from pathlib import Path
    MAPS = ("dbs", "props", "rows", "cells", "views")

    class FakeDoc:
        def __init__(self, path):
            self.path = path
            state = json.loads(Path(path).read_text()) if os.path.exists(path) else {}
            self.maps = {m: dict(state.get(m) or {}) for m in MAPS}

        @property
        def database(self):
            return {m: dict(v) for m, v in self.maps.items()}

        async def put_database(self, writes):
            assert set(writes) <= set(MAPS), writes
            for name, entries in writes.items():
                for k, v in entries.items():
                    if v is None:
                        self.maps[name].pop(k, None)
                    else:
                        self.maps[name][k] = v
            log = os.environ.get("CRONS_FAKE_WRITES")
            if log:
                with open(log, "a") as f:
                    f.write(json.dumps(writes) + "\\n")

        async def settle(self, seconds):
            with open(self.path, "w") as f:
                json.dump(self.maps, f)

    @asynccontextmanager
    async def _open(url, room, token, kind="markdown"):
        if os.environ.get("CRONS_FAKE_DOWN"):
            raise ConnectionError("collab service unreachable")
        assert kind == "db" and room == os.environ.get("CRONS_FAKE_ROOM", room)
        yield FakeDoc(os.environ["CRONS_FAKE_STATE"])
''')


def installed_client() -> "Path | None":
    """The room_commons_client.py an install on this machine carries, if any (CI has none)."""
    roots = [Path(os.environ[v]) / "skills" for v in ("CLAUDE_CONFIG_DIR",) if os.environ.get(v)]
    roots.append(Path.home() / ".claude" / "skills")
    for root in roots:
        p = root / "room-commons" / "scripts" / "room_commons_client.py"
        if p.is_file():
            return p
    return None


def exported_names(path: Path) -> set:
    """Module-level names a client file binds (defs and plain assignments), read with ast."""
    import ast
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


# The fake exports the open functions the installed client really exports; without one, only the
# name every released client exports (the newer ones add open_room_commons beside it).
REAL_CLIENT = installed_client()
FAKE_OPENERS = (tuple(n for n in ct.OPENERS if n in exported_names(REAL_CLIENT)) if REAL_CLIENT
                else ("open_room_collab",))
FAKE_CLIENT += "".join(f"\n{n} = _open\n" for n in FAKE_OPENERS)

ENTRIES = [
    {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
    {"name": "pr-practice", "cron": "13,43 * * * *", "owner": WORKER,
     "prompt": "Run the PR practice pass.\nSecond line is folded into the first."},
    {"name": "digest", "cron": "0 6 * * *", "launchd": True, "prompt": "x" * 400},
    {"name": "codex-job", "cron": "0 9 * * 1", "execution": "codex-task", "prompt": "weekly"},
    {"name": "codex-tz", "cron": "0 9 * * 1", "execution": "codex-task", "timezone": "Europe/Paris", "prompt": "p"},
    {"name": "watch", "monitor": {"command": "tail -f x", "description": "Draft watcher", "match": "x"}},
    {"name": "inbox", "loop": "dynamic", "prompt_skill": "inbox-score"},
    {"name": "parked", "cron": "0 0 * * *", "disabled": True, "prompt": "off"},
    {"cron": "* * * * *", "prompt": "unnamed is skipped"},
]


def fake_capability(ws: Path) -> Path:
    """The room-commons layout a current install has."""
    d = ws / "skills" / "room-commons" / "scripts"
    d.mkdir(parents=True)
    (d / "room_commons.py").write_text(FAKE_CLI)
    (d / "room_commons_client.py").write_text(FAKE_CLIENT)
    (d / "room_database.py").write_text(FAKE_DATABASE)
    return d


def load_fake_rd(scripts: Path):
    spec = importlib.util.spec_from_file_location("room_database", scripts / "room_database.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crons-table-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = self.tmp / "ws"
        self.scripts = fake_capability(self.ws)
        self.rd = load_fake_rd(self.scripts)
        (self.ws / "state").mkdir(parents=True)
        (self.ws / "state" / "owner-routing.json").write_text(json.dumps({"owner_dm": ROOM, "identity": AGENT}))
        self.state = self.tmp / "room.json"
        self.writes_log = self.tmp / "writes.jsonl"
        env = {k: v for k, v in os.environ.items()
               if k not in access.IDENTITY_VARS and not k.startswith(("CRONS_TABLE_", "CRONS_FAKE_"))}
        env.update({"CRONS_FAKE_STATE": str(self.state), "CRONS_FAKE_WRITES": str(self.writes_log)})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_crons(HOST, ENTRIES)

    def write_crons(self, host, entries):
        p = self.ws / "hosts" / host / "crons.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(entries))

    def run_cli(self, *argv, host=HOST):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ct.main(["--workspace", str(self.ws), "--host", host, *argv])
        return code, out.getvalue()

    def maps(self):
        return json.loads(self.state.read_text())

    def table(self, maps=None):
        """[{column name: displayed value}] for the Crons database, in row order."""
        maps = maps or self.maps()
        db = next(d["id"] for d in self.rd.list_dbs(maps) if d["name"] == "Crons")
        d = self.rd.read_db(maps, db)
        out = []
        for r in d["rows"]:
            row = {}
            for p in d["props"]:
                c = d["cells"].get(f"{r['id']}|{p['id']}")
                v = c["v"] if c else ""
                if p["type"] in ("select", "status"):
                    v = next((o["name"] for o in p["options"] if o["id"] == v), v)
                row[p["name"]] = v
            out.append(row)
        return out

    def row(self, name, host=HOST):
        hits = [r for r in self.table() if r["Cron"] == name and r["Host"] == host]
        self.assertEqual(len(hits), 1, hits)
        return hits[0]

    def writes(self):
        if not self.writes_log.exists():
            return []
        return [json.loads(x) for x in self.writes_log.read_text().splitlines()]


class BuildRows(unittest.TestCase):
    def test_each_runner_kind_owner_schedule_and_status(self):
        rows = ct.build_rows(ENTRIES, HOST, "hosts/host-a/crons.json", "America/Los_Angeles", "America/Denver")
        self.assertEqual(set(rows), {"main-loop", "pr-practice", "digest", "codex-job", "codex-tz", "watch",
                                     "inbox", "parked"})
        self.assertEqual({n: r["runner"] for n, r in rows.items()},
                         {"main-loop": "session", "pr-practice": "session", "digest": "launchd",
                          "codex-job": "codex-task", "codex-tz": "codex-task", "watch": "monitor",
                          "inbox": "dynamic-loop", "parked": "session"})
        self.assertEqual(rows["main-loop"]["owner"], "core")
        self.assertEqual(rows["pr-practice"]["owner"], WORKER)
        self.assertEqual(rows["main-loop"]["what"], "/proactive-loop")
        self.assertEqual(rows["pr-practice"]["what"],
                         "Run the PR practice pass. Second line is folded into the first.")
        self.assertEqual(len(rows["digest"]["what"]), ct.WHAT_MAX)
        self.assertTrue(rows["digest"]["what"].endswith("…"))
        self.assertEqual(rows["watch"]["what"], "Draft watcher")
        self.assertEqual((rows["watch"]["schedule"], rows["inbox"]["schedule"]), ("continuous", "dynamic"))
        self.assertEqual(rows["main-loop"]["timezone"], "America/Los_Angeles")
        self.assertEqual(rows["codex-job"]["timezone"], "America/Denver")
        self.assertEqual(rows["codex-tz"]["timezone"], "Europe/Paris")
        self.assertEqual(rows["parked"]["status"], "Paused")
        self.assertEqual(rows["main-loop"]["status"], "Active")
        self.assertEqual(ct.status_of({"name": "x-DISABLED-2026"}), "Paused")
        self.assertEqual(rows["main-loop"]["defined_in"], "hosts/host-a/crons.json")
        self.assertTrue(all("last_ran" not in r and "last_result" not in r for r in rows.values()))

    def test_digest_moves_with_rows_room_and_database(self):
        rows = ct.build_rows(ENTRIES, HOST, "d", "UTC")
        base = ct.rows_digest(rows, ROOM, "Crons")
        self.assertEqual(base, ct.rows_digest(json.loads(json.dumps(rows)), ROOM, "Crons"))
        self.assertNotEqual(base, ct.rows_digest(rows, "!other:test.invalid", "Crons"))
        self.assertNotEqual(base, ct.rows_digest(rows, ROOM, "Other"))
        rows["main-loop"]["schedule"] = "*/10 * * * *"
        self.assertNotEqual(base, ct.rows_digest(rows, ROOM, "Crons"))

    def test_codex_default_timezone_is_read_from_the_runner(self):
        self.assertEqual(ct.codex_default_timezone(), "America/Los_Angeles")


class Sync(Base):
    def test_sync_creates_the_database_and_one_row_per_entry(self):
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("synced 8 rows for host-a", out)
        names = [p["name"] for p in self.rd.read_db(self.maps(), "crons")["props"]]
        self.assertEqual(names, [c[1] for c in ct.COLUMNS])
        self.assertIn("Last ran (UTC)", names)
        r = self.row("pr-practice")
        self.assertEqual((r["Owner"], r["Runner"], r["Status"], r["Last ran (UTC)"], r["Last result"]),
                         (WORKER, "session", "Active", "", ""))
        self.assertEqual(self.row("parked")["Status"], "Paused")
        self.assertEqual(self.row("watch")["Runner"], "monitor")

    def test_unchanged_crons_json_does_not_write_again(self):
        self.assertEqual(self.run_cli("sync")[0], 0)
        n = len(self.writes())
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0)
        self.assertIn("unchanged (8 rows for host-a", out)
        self.assertEqual(len(self.writes()), n)
        os.environ["CRONS_FAKE_DOWN"] = "1"  # the no-op path never opens the room at all
        self.assertEqual(self.run_cli("sync")[0], 0)

    def test_force_resyncs_and_a_converged_room_writes_nothing(self):
        self.run_cli("sync")
        n = len(self.writes())
        code, out = self.run_cli("sync", "--force")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.writes()), n)

    def test_an_edit_is_written(self):
        self.run_cli("sync")
        edited = [dict(e) for e in ENTRIES]
        edited[0]["cron"] = "*/10 * * * *"
        self.write_crons(HOST, edited)
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.row("main-loop")["Schedule"], "*/10 * * * *")

    def test_two_hosts_with_the_same_name_keep_their_own_rows(self):
        self.write_crons(OTHER, [{"name": "main-loop", "cron": "*/30 * * * *", "prompt_skill": "proactive-loop"}])
        self.run_cli("sync")
        self.run_cli("sync", host=OTHER)
        self.assertEqual(self.row("main-loop")["Schedule"], "*/5 * * * *")
        self.assertEqual(self.row("main-loop", OTHER)["Schedule"], "*/30 * * * *")
        self.assertEqual(len(self.table()), 9)
        # host-b's crons.json losing everything finishes only host-b's rows
        self.write_crons(OTHER, [])
        self.run_cli("sync", host=OTHER)
        self.assertEqual(self.row("main-loop", OTHER)["Status"], "Finished")
        self.assertEqual(self.row("main-loop")["Status"], "Active")

    def test_a_removed_entry_is_finished_not_deleted_and_returns_active(self):
        self.run_cli("sync")
        self.write_crons(HOST, [e for e in ENTRIES if e.get("name") != "digest"])
        code, out = self.run_cli("sync")
        self.assertIn("1 finished", out)
        self.assertEqual(self.row("digest")["Status"], "Finished")
        self.assertEqual(len(self.table()), 8)
        self.write_crons(HOST, ENTRIES)
        self.run_cli("sync")
        self.assertEqual(self.row("digest")["Status"], "Active")

    def test_sync_preserves_last_ran_and_last_result(self):
        self.run_cli("sync")
        self.assertEqual(self.run_cli("touch", "main-loop", "posted 3 items")[0], 0)
        stamped = self.row("main-loop")
        edited = [dict(e) for e in ENTRIES]
        edited[0]["prompt_skill"] = "other-loop"
        self.write_crons(HOST, edited)
        self.run_cli("sync")
        r = self.row("main-loop")
        self.assertEqual(r["What"], "/other-loop")
        self.assertEqual((r["Last ran (UTC)"], r["Last result"]),
                         (stamped["Last ran (UTC)"], "posted 3 items"))

    def test_unreadable_crons_json_exits_1_and_writes_nothing(self):
        (self.ws / "hosts" / HOST / "crons.json").write_text("{not json")
        code, out = self.run_cli("sync")
        self.assertEqual(code, ct.EXIT_CONFIG)
        self.assertEqual(out.count("\n"), 1)
        self.assertFalse(self.state.exists())


class TouchAndStatus(Base):
    def test_touch_stamps_last_ran_and_result(self):
        self.run_cli("sync")
        with mock.patch.object(ct.time, "time", return_value=1791460800.0):  # 2026-10-08 12:00 UTC
            code, out = self.run_cli("touch", "pr-practice", "merged #5100\nand more")
        self.assertEqual(code, 0, out)
        r = self.row("pr-practice")
        self.assertEqual((r["Last ran (UTC)"], r["Last result"]), ("2026-10-08 12:00", "merged #5100 and more"))
        self.assertEqual(r["Owner"], WORKER)

    def test_touch_creates_a_missing_row_from_crons_json(self):
        code, out = self.run_cli("touch", "codex-job", "ok")
        self.assertEqual(code, 0, out)
        r = self.row("codex-job")
        self.assertEqual((r["Runner"], r["Schedule"], r["Last result"]), ("codex-task", "0 9 * * 1", "ok"))
        self.assertEqual(len(self.table()), 1)

    def test_touch_of_a_name_not_in_crons_json_still_records(self):
        code, out = self.run_cli("touch", "ad-hoc", "ran once")
        self.assertEqual(code, 0, out)
        r = self.row("ad-hoc")
        self.assertEqual((r["Last result"], r["Runner"]), ("ran once", ""))

    def test_status_sets_and_a_later_unchanged_sync_keeps_it(self):
        self.run_cli("sync")
        self.assertEqual(self.run_cli("status", "inbox", "paused")[0], 0)
        self.assertEqual(self.row("inbox")["Status"], "Paused")
        self.run_cli("sync")
        self.assertEqual(self.row("inbox")["Status"], "Paused")

    def test_a_status_set_by_hand_survives_a_sync_that_writes(self):
        self.run_cli("sync")
        self.run_cli("status", "inbox", "Paused")
        self.run_cli("status", "main-loop", "Finished")
        edited = [dict(e) for e in ENTRIES]
        edited[6]["prompt_skill"] = "inbox-score-v2"
        self.write_crons(HOST, edited)
        code, out = self.run_cli("sync")  # crons.json changed: the digest skip does not apply
        self.assertIn("synced 8 rows", out)
        self.assertEqual((self.row("inbox")["Status"], self.row("inbox")["What"]), ("Paused", "/inbox-score-v2"))
        self.assertEqual(self.row("main-loop")["Status"], "Active")  # Finished with its entry present: revived
        self.run_cli("status", "inbox", "Paused")
        self.run_cli("sync", "--force")
        self.assertEqual(self.row("inbox")["Status"], "Paused")

    def test_disabling_an_entry_reaches_its_existing_row_and_reenabling_clears_it(self):
        self.run_cli("sync")
        edited = [dict(e) for e in ENTRIES]
        edited[0]["disabled"] = True
        self.write_crons(HOST, edited)
        self.run_cli("sync")
        self.assertEqual(self.row("main-loop")["Status"], "Paused")
        self.write_crons(HOST, ENTRIES)
        self.run_cli("sync")
        self.assertEqual(self.row("main-loop")["Status"], "Active")

    def test_a_hand_set_status_survives_when_its_crons_json_status_did_not_move(self):
        self.run_cli("sync")
        self.run_cli("status", "main-loop", "Paused")
        edited = [dict(e) for e in ENTRIES]
        edited[1]["disabled"] = True  # another entry's status moves; main-loop's does not
        self.write_crons(HOST, edited)
        self.run_cli("sync")
        self.assertEqual((self.row("main-loop")["Status"], self.row("pr-practice")["Status"]), ("Paused", "Paused"))
        (self.ws / "hosts" / HOST / ct.STAMP).unlink()  # no record of the last sync: nothing counts as moved
        self.run_cli("status", "pr-practice", "Active")
        self.run_cli("sync")
        self.assertEqual(self.row("pr-practice")["Status"], "Active")

    def test_touch_does_not_reset_status(self):
        self.run_cli("sync")
        self.run_cli("status", "inbox", "Paused")
        self.run_cli("touch", "inbox", "skipped")
        self.assertEqual(self.row("inbox")["Status"], "Paused")

    def test_status_refuses_an_unknown_value(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("status", "inbox", "Gone")


class Adopt(Base):
    def test_an_existing_crons_database_is_adopted_and_extended(self):
        # A database named "crons" with its own title, a subset of the columns and a Status lacking Paused.
        maps = {"dbs": {"dB1": {"name": "crons", "order": 1024, "created": 1, "by": AGENT}},
                "props": {"dB1|name": {"name": "Cron", "type": "title", "order": 1024},
                          "dB1|st": {"name": "Status", "type": "select", "order": 2048,
                                     "options": [{"id": "a", "name": "Active", "color": "green"}]},
                          "dB1|lr": {"name": "Last ran (UTC)", "type": "date", "order": 3072}},
                "rows": {"dB1|r1": {"order": 1024, "created": 1, "by": AGENT}},
                "cells": {"dB1|r1|name": {"v": "parked", "updated": 1, "by": AGENT}},
                "views": {"dB1|v1": {"name": "Table", "layout": "table", "order": 1024}}}
        self.state.write_text(json.dumps(maps))
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        got = self.maps()
        self.assertEqual(list(got["dbs"]), ["dB1"])
        props = self.rd.read_db(got, "dB1")["props"]
        self.assertEqual([p["name"] for p in props][:3], ["Cron", "Status", "Last ran (UTC)"])
        self.assertEqual({p["name"] for p in props}, {c[1] for c in ct.COLUMNS})
        self.assertIn("Paused", [o["name"] for o in next(p for p in props if p["name"] == "Status")["options"]])
        self.assertEqual(len(self.rd.read_db(got, "dB1")["rows"]), 8)  # the host-less row was adopted
        cells = got["cells"]
        self.assertEqual(cells["dB1|r1|" + next(p["id"] for p in props if p["name"] == "Host")]["v"], HOST)
        self.run_cli("touch", "parked", "skipped")
        lr = self.maps()["cells"]["dB1|r1|lr"]["v"]
        self.assertRegex(lr["start"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")  # a date column takes the date shape

    def test_hand_typed_cells_on_an_adopted_row_survive(self):
        maps = {"dbs": {"dB1": {"name": "Crons", "order": 1024, "created": 1, "by": AGENT}},
                "props": {"dB1|name": {"name": "Cron", "type": "title", "order": 1024},
                          "dB1|sch": {"name": "Schedule", "type": "text", "order": 2048},
                          "dB1|own": {"name": "Owner", "type": "text", "order": 3072},
                          "dB1|st": {"name": "Status", "type": "text", "order": 4096}},
                "rows": {"dB1|r1": {"order": 1024, "created": 1, "by": AGENT}},
                "cells": {"dB1|r1|name": {"v": "Main-Loop", "updated": 1, "by": AGENT},
                          "dB1|r1|sch": {"v": "every 10m (GTM room)", "updated": 1, "by": AGENT},
                          "dB1|r1|own": {"v": "Bassil", "updated": 1, "by": AGENT},
                          "dB1|r1|st": {"v": "Paused", "updated": 1, "by": AGENT}}}
        self.state.write_text(json.dumps(maps))
        self.assertEqual(self.run_cli("sync")[0], 0)
        self.run_cli("sync", "--force")
        cells = self.maps()["cells"]
        self.assertEqual([cells[f"dB1|r1|{p}"]["v"] for p in ("name", "sch", "own", "st")],
                         ["Main-Loop", "every 10m (GTM room)", "Bassil", "Paused"])
        r = next(x for x in self.table() if x["Cron"] == "Main-Loop")
        self.assertEqual((r["Host"], r["Runner"], r["What"]), (HOST, "session", "/proactive-loop"))  # empty: filled
        self.assertEqual(len(self.table()), 8)  # adopted, not duplicated

    def test_two_databases_with_the_name_refuse(self):
        maps = {"dbs": {"a": {"name": "Crons", "order": 1, "created": 1, "by": AGENT},
                        "b": {"name": "Crons", "order": 2, "created": 1, "by": AGENT}}}
        self.state.write_text(json.dumps(maps))
        code, out = self.run_cli("sync")
        self.assertEqual(code, ct.EXIT_UNAVAILABLE)
        self.assertIn("2 databases are named 'Crons'", out)


class FailOpen(Base):
    def test_room_unreachable_is_one_line_exit_2_and_no_stamp(self):
        os.environ["CRONS_FAKE_DOWN"] = "1"
        for argv in (("sync",), ("touch", "main-loop", "x"), ("status", "main-loop", "Paused")):
            code, out = self.run_cli(*argv)
            self.assertEqual(code, ct.EXIT_UNAVAILABLE, argv)
            self.assertEqual(out.count("\n"), 1, out)
            self.assertIn("ConnectionError", out)
        self.assertFalse((self.ws / "hosts" / HOST / ct.STAMP).exists())
        del os.environ["CRONS_FAKE_DOWN"]
        self.assertIn("synced 8 rows", self.run_cli("sync")[1])  # a failed sync does not count as synced

    def test_no_capability_installed(self):
        shutil.rmtree(self.ws / "skills")
        with mock.patch.object(ct, "REPO", self.tmp):
            code, out = self.run_cli("sync")
        self.assertEqual(code, ct.EXIT_UNAVAILABLE)
        self.assertIn("no room capability installed", out)

    def test_no_room_and_no_identity(self):
        (self.ws / "state" / "owner-routing.json").write_text(json.dumps({"identity": AGENT}))
        code, out = self.run_cli("sync")
        self.assertEqual(code, ct.EXIT_UNAVAILABLE)
        self.assertIn("no room", out)
        (self.ws / "state" / "owner-routing.json").write_text(json.dumps({"owner_dm": ROOM}))
        code, out = self.run_cli("touch", "x", "y")
        self.assertEqual(code, ct.EXIT_UNAVAILABLE)
        self.assertIn("no agent identity", out)

    def test_configured_room_wins_over_the_owner_dm(self):
        os.environ["CRONS_TABLE_ROOM"] = "!shared:test.invalid"
        os.environ["CRONS_FAKE_ROOM"] = "!shared:test.invalid"
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("!shared:test.invalid", out)
        code, out = self.run_cli("--room", "!cli:test.invalid", "sync")
        self.assertEqual(code, ct.EXIT_UNAVAILABLE)  # the fake only serves the shared room


class Layouts(unittest.TestCase):
    def _load(self, exports):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "room-commons" / "scripts"
            d.mkdir(parents=True)
            (d / "room_commons.py").write_text(FAKE_CLI)
            (d / "room_commons_client.py").write_text("".join(f"def {n}(*a, **k):\n    return {n!r}\n" for n in exports))
            (d / "room_database.py").write_text(FAKE_DATABASE)
            saved = {m: sys.modules.pop(m) for m in ("room_commons", "room_commons_client", "room_database")
                     if m in sys.modules}
            try:
                return ct.load_capability(d)[1]()
            finally:
                for m in ("room_commons", "room_commons_client", "room_database"):
                    sys.modules.pop(m, None)
                sys.modules.update(saved)
                sys.path.remove(str(d))

    def test_the_open_function_falls_back_to_the_pre_rename_name(self):
        self.assertEqual(self._load(["open_room_collab"]), "open_room_collab")
        self.assertEqual(self._load(["open_room_commons"]), "open_room_commons")
        self.assertEqual(self._load(["open_room_collab", "open_room_commons"]), "open_room_commons")
        with self.assertRaisesRegex(ImportError, "exports none of open_room_commons, open_room_collab"):
            self._load(["open_something_else"])

    @unittest.skipUnless(REAL_CLIENT, "no room-commons install on this machine")
    def test_the_installed_client_exports_an_open_function_this_script_accepts(self):
        names = exported_names(REAL_CLIENT)
        self.assertTrue(set(ct.OPENERS) & names, f"{REAL_CLIENT} exports none of {ct.OPENERS}")
        self.assertTrue(set(FAKE_OPENERS) <= names)

    def test_room_commons_is_preferred_and_the_room_collab_shim_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as t:
            ws = Path(t)
            (ws / "state").mkdir()
            (ws / "state" / "owner-routing.json").write_text(json.dumps({"owner_dm": ROOM, "identity": AGENT}))
            shim = ws / "skills" / "room-collab" / "scripts"
            shim.mkdir(parents=True)
            for m in ("room_collab.py", "room_collab_client.py"):
                (shim / m).write_text("")
            with mock.patch.object(ct, "REPO", ws / "none"):
                self.assertEqual(ct.target(ws, None, {})[0], shim)
                canon = fake_capability(ws)
                self.assertEqual(ct.target(ws, None, {})[0], canon)


class Delegation(unittest.TestCase):
    """Both room-database adapters reach the room through owner_room_access, not a copy of it."""

    def test_crons_table_and_pending_questions_share_the_access_helpers(self):
        sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))
        pq = importlib.import_module("pending_questions_room_db")
        for name in ("capability_scripts", "owner_routing", "owner_dm", "agent_identity"):
            self.assertIs(getattr(ct, name), getattr(access, name), name)
            self.assertIs(getattr(pq, name), getattr(access, name), name)
        self.assertIs(ct.resolve_credentials, access.resolve_credentials)
        self.assertIs(pq._credentials, access.resolve_credentials)
        self.assertIs(pq.channel_credentials, access.channel_credentials)

    def test_capability_scripts_takes_the_first_root_and_name_with_every_module(self):
        with tempfile.TemporaryDirectory() as t:
            a, b = Path(t) / "a", Path(t) / "b"
            (a / "alias" / "scripts").mkdir(parents=True)
            (a / "alias" / "scripts" / "m1.py").write_text("")
            for name in ("canon", "alias"):
                (b / name / "scripts").mkdir(parents=True)
                for m in ("m1.py", "m2.py"):
                    (b / name / "scripts" / m).write_text("")
            self.assertEqual(access.capability_scripts((a, b), ("canon", "alias"), ("m1.py", "m2.py")),
                             b / "canon" / "scripts")
            self.assertIsNone(access.capability_scripts((a,), ("canon", "alias"), ("m1.py", "m2.py")))

    def test_agent_identity_prefers_the_environment(self):
        routing = {"identity": "@routing:test.invalid"}
        self.assertEqual(access.agent_identity(routing, {}), "@routing:test.invalid")
        self.assertEqual(access.agent_identity(routing, {"AG2_MATRIX_USER_ID": " @env:test.invalid "}),
                         "@env:test.invalid")
        self.assertEqual(access.owner_dm({"owner_dm": " !r:x "}), "!r:x")


if __name__ == "__main__":
    unittest.main()

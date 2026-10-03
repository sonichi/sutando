"""Every extra-content producer crosses the shared guard before its output edge."""
from __future__ import annotations

import ast
import builtins
import contextlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ROOM = "!room:example.invalid"
PEER = "@peer:example.invalid"
BASE = "https://gateway.example.invalid"
CARD = {"space.ag2.card": {"items": [{"space.ag2.internal": 1}]}}
BAD = {"wrapper": {"space.ag2.card": {"v": 1}}}
REFUSAL = "contract refusal"
COPIES = {
    "skills/agent-room-ops/room_message.py",
    "skills/task-progress/scripts/room_message.py",
    "packages/ag2-sparrow/ag2_sparrow/room_message.py",
}
PRODUCERS = {
    "skills/agent-room-ops/room_ops.py",
    "skills/agent-room-ops/say.py",
    "skills/agent-room-ops/mention.py",
    "skills/task-progress/scripts/notify.py",
    "skills/connect-apps/scripts/connectors.py",
    "src/hitl/projector.py",
    "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py",
}


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}


@contextlib.contextmanager
def policy_probe(adapter, reason=None):
    owner = adapter.room_message
    validator = mock.Mock(wraps=owner.extra_content_problem)
    if reason is not None:
        validator.return_value = reason
    with mock.patch.object(owner, "extra_content_problem", validator):
        yield validator


class DelegationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.sandbox.cleanup)
        cls.env = {
            "CLAUDE_CONFIG_DIR": cls.sandbox.name,
            "ROOM_OPS_GATE": str(Path(cls.sandbox.name) / "room-gate.json"),
            "AGENT_CONNECT_TASK_DIR": str(Path(cls.sandbox.name) / "tasks"),
            "AGENT_CONNECT_RESULT_DIR": str(Path(cls.sandbox.name) / "results"),
            "AGENT_CONNECT_STATE_DIR": str(Path(cls.sandbox.name) / "state"),
            "REMOTE_TASK_URL": BASE, "REMOTE_TASK_TOKEN": "synthetic-token",
            "GATEWAY_URL": BASE, "GATEWAY_TOKEN": "synthetic-token",
            "SUTANDO_TELEMETRY": "0",
        }
        cls.paths = list(sys.path)
        cls.addClassCleanup(lambda: sys.path.__setitem__(slice(None), cls.paths))
        for relative in ("src", "skills/agent-room-ops", "packages/ag2-sparrow"):
            sys.path.insert(0, str(ROOT / relative))
        with mock.patch.dict(os.environ, cls.env, clear=True), \
                mock.patch.object(Path, "home", return_value=Path(cls.sandbox.name)), \
                mock.patch.object(socket, "getaddrinfo", socket.getaddrinfo), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("import network")):
            cls.gateway = importlib.import_module("_gateway")
            cls.say = importlib.import_module("say")
            cls.mention = importlib.import_module("mention")
            cls.notify = load("room_contract_notify", "skills/task-progress/scripts/notify.py")
            cls.projector = importlib.import_module("hitl.projector")
            cls.manager = importlib.import_module("hitl.manager")
            cls.schema = importlib.import_module("hitl.schema")
            cls.bridge = importlib.import_module("ag2_sparrow.remote_gateway_bridge")

    def setUp(self):
        env = mock.patch.dict(os.environ, self.env, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.sent = []
        boundary = mock.patch.object(urllib.request, "urlopen", side_effect=self.network_boundary)
        self.network = boundary.start()
        self.addCleanup(boundary.stop)

    def network_boundary(self, request, **kwargs):
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        self.sent.append((request.get_method(), request.full_url, payload))
        return Response(b'{"ok": true, "event_id": "$event"}')

    def edge(self, name, payload, path="/v1/room", method="POST"):
        if name == "room-ops":
            return self.gateway.http_json(method, BASE + path, {}, payload)
        if name == "notify":
            return self.notify._post(BASE + path, payload, {})
        return self.bridge._req(method, path, payload)

    def test_real_request_edges_reject_malformed_message_and_edit_before_urlopen(self):
        for edge in ("room-ops", "notify", "sparrow"):
            for operation in ("message", "edit"):
                for extra in (BAD, {"body": "wrapper"}, None, []):
                    with self.subTest(edge=edge, operation=operation, extra=extra):
                        self.sent.clear()
                        payload = {"op": operation, "room_id": ROOM, "body": "hello", "extra_content": extra}
                        if edge == "notify":
                            error = io.StringIO()
                            with contextlib.redirect_stderr(error):
                                self.assertFalse(self.edge(edge, payload))
                            self.assertIn("extra_content", error.getvalue())
                        else:
                            with self.assertRaisesRegex(ValueError, "extra_content"):
                                self.edge(edge, payload)
                        self.assertEqual(self.sent, [])

    def test_valid_wire_payload_is_unchanged_at_all_request_edges(self):
        for name, adapter in (("room-ops", self.gateway), ("notify", self.notify), ("sparrow", self.bridge)):
            for operation in ("message", "edit"):
                with self.subTest(edge=name, operation=operation):
                    payload = {"op": operation, "room_id": ROOM, "body": "hello", "extra_content": CARD,
                               "event_id": "$prior", "dedupe_key": "contract", "reply_to": "$reply"}
                    with policy_probe(adapter) as checked:
                        self.edge(name, payload)
                    checked.assert_called_once_with(CARD)
                    self.assertEqual(self.sent[-1], ("POST", BASE + "/v1/room", payload))

    def test_unrelated_routes_and_operations_keep_their_existing_payloads(self):
        cases = (("/v1/room", "create"), ("/v1/other", "message"), ("/v1/rooms", "edit"))
        for name in ("room-ops", "notify", "sparrow"):
            for path, operation in cases:
                with self.subTest(edge=name, path=path, operation=operation):
                    payload = {"op": operation, "extra_content": BAD}
                    self.edge(name, payload, path=path)
                    self.assertEqual(self.sent[-1], ("POST", BASE + path, payload))
        for name in ("room-ops", "sparrow"):
            payload = {"op": "message", "extra_content": BAD}
            self.edge(name, payload, method="GET")
            self.assertEqual(self.sent[-1], ("GET", BASE + "/v1/room", payload))

    def test_query_string_does_not_bypass_the_room_path_guard(self):
        for name in ("room-ops", "notify", "sparrow"):
            with self.subTest(edge=name):
                self.sent.clear()
                payload = {"op": "message", "extra_content": BAD}
                if name == "notify":
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertFalse(self.edge(name, payload, path="/v1/room?contract=1"))
                else:
                    with self.assertRaises(ValueError):
                        self.edge(name, payload, path="/v1/room?contract=1")
                self.assertEqual(self.sent, [])

    def test_configured_gateway_prefix_preserves_cards_and_still_guards_them(self):
        for name in ("room-ops", "notify"):
            with self.subTest(edge=name):
                path = "/relay/v1/room?contract=1"
                payload = {"op": "message", "extra_content": CARD}
                self.edge(name, payload, path=path)
                self.assertEqual(self.sent[-1], ("POST", BASE + path, payload))
                self.sent.clear()
                payload["extra_content"] = BAD
                if name == "notify":
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertFalse(self.edge(name, payload, path=path))
                else:
                    with self.assertRaises(ValueError):
                        self.edge(name, payload, path=path)
                self.assertEqual(self.sent, [])

    def test_say_preserves_cards_worker_and_relations_through_shared_owner(self):
        os.environ["SUTANDO_WORKER_ID"] = "worker-contract"
        with policy_probe(self.gateway) as checked:
            result = self.say.say("hello", ROOM, PEER, gate=None, extra_content=CARD, reply_to="$reply")
        self.assertTrue(result["ok"])
        extra = {"space.ag2.worker": {"id": "worker-contract"}, **CARD}
        checked.assert_called_once_with(extra)
        self.assertEqual(self.sent[-1][2], {"op": "message", "room_id": ROOM, "body": "hello",
                                          "reply_to": "$reply", "extra_content": extra})

    def test_say_rejects_user_extras_before_any_network(self):
        result = self.say.say("hello", ROOM, PEER, gate=None, extra_content=BAD)
        self.assertFalse(result["ok"])
        self.assertIn("extra_content", result["reason"])
        self.assertEqual(self.sent, [])

    def test_mention_preserves_worker_and_mentions_through_shared_owner(self):
        os.environ["SUTANDO_WORKER_ID"] = "worker-contract"
        with policy_probe(self.gateway) as checked:
            result = self.mention.mention(PEER, "hello", ROOM, PEER, gate=None, agents=[])
        self.assertTrue(result["ok"])
        extra = {"space.ag2.worker": {"id": "worker-contract"}}
        checked.assert_called_once_with(extra)
        self.assertEqual(self.sent[-1][2], {"op": "message", "room_id": ROOM, "body": PEER + " — hello",
                                          "mentions": [PEER], "extra_content": extra})

    def test_say_and_mention_cannot_skip_a_shared_owner_refusal(self):
        os.environ["SUTANDO_WORKER_ID"] = "worker-contract"
        for sender in (lambda: self.say.say("hello", ROOM, PEER, gate=None),
                       lambda: self.mention.mention(PEER, "hello", ROOM, PEER, gate=None, agents=[])):
            with self.subTest(sender=sender):
                with policy_probe(self.gateway, REFUSAL), self.assertRaisesRegex(ValueError, REFUSAL):
                    sender()
                self.assertEqual(self.sent, [])

    def test_notify_preserves_worker_through_shared_owner(self):
        os.environ["SUTANDO_WORKER_ID"] = "worker-contract"
        with policy_probe(self.notify) as checked:
            self.assertTrue(self.notify.send_remote_gateway("test", ROOM, "hello"))
        extra = {"space.ag2.worker": {"id": "worker-contract"}}
        checked.assert_called_once_with(extra)
        self.assertEqual(self.sent[-1][2], {"op": "message", "room_id": ROOM, "body": "hello",
                                          "extra_content": extra})

    def test_notify_reports_shared_refusal_without_network(self):
        os.environ["SUTANDO_WORKER_ID"] = "worker-contract"
        error = io.StringIO()
        with policy_probe(self.notify, REFUSAL), contextlib.redirect_stderr(error):
            self.assertFalse(self.notify.send_remote_gateway("test", ROOM, "hello"))
        self.assertIn(REFUSAL, error.getvalue())
        self.assertEqual(self.sent, [])

    def new_requirement(self, directory):
        manager = self.manager.HitlManager(self.manager.HitlStore(Path(directory)))
        requirement = manager.create(self.schema.HumanRequirement(kind="auth", runtime="claude",
                                                                  message="Sign in", guard="contract"))
        return manager, requirement

    def test_projector_valid_create_and_edit_delegate_and_preserve_wire(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, requirement = self.new_requirement(directory)
            sent = []
            sender = lambda payload: (sent.append(payload), {"ok": True, "event_id": "$created"})[1]
            created = {"op": "message", "room_id": ROOM, "body": self.projector.fallback_body(requirement),
                       "dedupe_key": f"hitl:{requirement.id}:1",
                       "extra_content": {self.schema.WIRE_FIELD: requirement.to_wire()}}
            with policy_probe(self.projector) as checked:
                self.projector.project(manager, sender, ROOM)
                manager.resolve(requirement.id)
                resolved = manager.get(requirement.id)
                edited = {"op": "edit", "event_id": "$created", "room_id": ROOM,
                          "body": self.projector.fallback_body(resolved),
                          "dedupe_key": f"hitl:{requirement.id}:2",
                          "extra_content": {self.schema.WIRE_FIELD: resolved.to_wire()}}
                self.projector.project(manager, sender, ROOM)
            self.assertEqual(checked.call_count, 2)
            self.assertEqual(sent, [created, edited])

    def test_projector_refusal_precedes_sender_and_preserves_revision_for_retry(self):
        for operation in ("message", "edit"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                manager, requirement = self.new_requirement(directory)
                if operation == "edit":
                    self.projector.project(manager, lambda payload: {"event_id": "$created"}, ROOM)
                    manager.resolve(requirement.id)
                sender = mock.Mock()
                with policy_probe(self.projector, REFUSAL), self.assertRaisesRegex(ValueError, REFUSAL):
                    self.projector.project(manager, sender, ROOM)
                sender.assert_not_called()
                self.assertTrue(manager.needs_projection(requirement.id))

    @unittest.skipIf(os.name == "nt", "connectors imports Unix fcntl; requires a Unix host")
    def test_connector_builder_preserves_card_and_delegates_refusal(self):
        connectors = load("room_contract_connectors", "skills/connect-apps/scripts/connectors.py")
        wait = {"reply_to": "$reply", "toolkits": [{"slug": "calendar", "name": "Calendar"}]}
        with policy_probe(connectors) as checked:
            payload = connectors.card_message(wait, PEER, "task-contract", "hello", True)
        extra = {"space.ag2.connector": {"version": 1, "for": PEER,
                                         "toolkits": [{"slug": "calendar"}], "mode": "switch"}}
        checked.assert_called_once_with(extra)
        self.assertEqual(payload, {"body": "hello\n\n" + connectors.card_outro(["Calendar"], True),
                                   "extra_content": extra, "reply_to": "$reply",
                                   "operation_id": "task-contract:switch-card"})
        with policy_probe(connectors, REFUSAL), self.assertRaisesRegex(ValueError, REFUSAL):
            connectors.card_message(wait, PEER, "task-contract", "hello", True)
        self.assertEqual(self.sent, [])

    def test_sparrow_review_uses_guard_and_preserves_a2ui_card(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            record = {"status": "pending_dm", "review_id": "wr_0123456789abcdef",
                      "context": {"channel_id": ROOM}, "withheld_body": "candidate"}
            path.write_text(json.dumps(record), encoding="utf-8")
            messages = self.bridge._review_messages(record)
            with mock.patch.object(self.bridge, "_gateway_owner", return_value=PEER), \
                    mock.patch.object(self.bridge, "_owner_review_dm", return_value=ROOM), \
                    mock.patch.object(self.bridge, "_atomic_private_json") as persist, \
                    policy_probe(self.bridge) as checked:
                self.assertTrue(self.bridge._route_withheld_review(path))
            extra = {"space.ag2.a2ui": self.bridge._review_buttons(record["review_id"])}
            checked.assert_called_once_with(extra)
            self.assertEqual(len(self.sent), len(messages))
            for index, message in enumerate(messages, 1):
                expected = {"op": "message", "room_id": ROOM, "body": message,
                            "mentions": [PEER] if index == len(messages) else [],
                            "dedupe_key": f"withheld-review:{record['review_id']}:{index}"}
                if index == len(messages):
                    expected["extra_content"] = extra
                self.assertEqual(self.sent[index - 1][2], expected)
            self.assertEqual(persist.call_args.args[0], path)
            self.assertEqual(persist.call_args.args[1]["status"], "awaiting_owner")

    def test_sparrow_review_refusal_prevents_card_post_and_durable_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            record = {"status": "pending_dm", "review_id": "wr_0123456789abcdef",
                      "context": {"channel_id": ROOM}, "withheld_body": "candidate"}
            path.write_text(json.dumps(record), encoding="utf-8")
            with mock.patch.object(self.bridge, "_gateway_owner", return_value=PEER), \
                    mock.patch.object(self.bridge, "_owner_review_dm", return_value=ROOM), \
                    policy_probe(self.bridge, REFUSAL), self.assertRaisesRegex(ValueError, REFUSAL):
                self.bridge._route_withheld_review(path)
            self.assertTrue(self.sent)
            self.assertTrue(all("extra_content" not in payload for _, _, payload in self.sent))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)


def constructs_extra_content(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            if any(isinstance(key, ast.Constant) and key.value == "extra_content" for key in node.keys):
                return True
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if isinstance(node.slice, ast.Constant) and node.slice.value == "extra_content":
                return True
        if isinstance(node, ast.Call) and any(key.arg == "extra_content" for key in node.keywords):
            return True
    return False


class OwnershipTests(unittest.TestCase):
    def test_inventory_recognizes_dict_assignment_and_keyword_constructors(self):
        for source in ('payload = {"extra_content": card}', 'payload["extra_content"] = card',
                       'payload = dict(extra_content=card)'):
            with self.subTest(source=source):
                self.assertTrue(constructs_extra_content(ast.parse(source)))
        self.assertFalse(constructs_extra_content(ast.parse('value = payload.get("extra_content")')))

    def test_every_production_constructor_is_registered_and_policy_is_not_copied(self):
        producers, owners = set(), set()
        for directory in ("src", "skills", "packages", "scripts", "tools", "shared"):
            for path in (ROOT / directory).rglob("*.py"):
                if any(part in ("tests", "test", "node_modules", ".venv", "__pycache__") for part in path.parts):
                    continue
                if path.name.startswith("test_") or path.name.endswith(".test.py"):
                    continue
                source = path.read_text(encoding="utf-8-sig")
                if "extra_content" not in source:
                    continue
                tree = ast.parse(source, filename=str(path))
                relative = path.relative_to(ROOT).as_posix()
                if constructs_extra_content(tree):
                    producers.add(relative)
                if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name == "extra_content_problem" for node in ast.walk(tree)):
                    owners.add(relative)
        self.assertEqual(producers, PRODUCERS, "new producer needs a shared-owner delegation contract")
        self.assertEqual(owners, {"src/room_message.py", *COPIES})

    def test_generated_distributions_match_canonical_and_remain_registered(self):
        generator = load("room_contract_sync", "tools/sync_room_message.py")
        self.assertEqual(set(generator.TARGETS), COPIES)
        expected = (ROOT / "src/room_message.py").read_bytes()
        for relative in COPIES:
            self.assertEqual((ROOT / relative).read_bytes(), expected, relative)
        result = subprocess.run([sys.executable, str(ROOT / "tools/sync_room_message.py"), "--check"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("3 room-message distributions", result.stdout)

    def test_generation_detects_and_repairs_corrupted_and_missing_distributions(self):
        generator = load("room_contract_sync_repair", "tools/sync_room_message.py")
        expected = (ROOT / "src/room_message.py").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "canonical.py"
            source.write_bytes(expected)
            targets = ("first/room_message.py", "second/room_message.py")
            for relative in targets:
                (root / relative).parent.mkdir()
            (root / targets[0]).write_bytes(b"corrupted contract\n")
            with mock.patch.multiple(generator, REPO=root, SOURCE=source, TARGETS=targets):
                errors = io.StringIO()
                with mock.patch.object(sys, "argv", ["sync_room_message.py", "--check"]), \
                        contextlib.redirect_stderr(errors):
                    self.assertEqual(generator.main(), 1)
                for relative in targets:
                    self.assertIn(relative, errors.getvalue())
                with mock.patch.object(sys, "argv", ["sync_room_message.py"]), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(generator.main(), 0)
                for relative in targets:
                    self.assertEqual((root / relative).read_bytes(), expected)
                with mock.patch.object(sys, "argv", ["sync_room_message.py", "--check"]), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(generator.main(), 0)
                self.assertIn("2 room-message distributions", output.getvalue())

    def test_import_fallback_loads_generated_siblings(self):
        real_import = builtins.__import__
        for name, relative in (
                ("notify", "skills/task-progress/scripts/notify.py"),
                ("gateway", "skills/agent-room-ops/_gateway.py")):
            with self.subTest(name=name):
                attempted = []

                def first_import_missing(module_name, *args, **kwargs):
                    if module_name == "room_message":
                        attempted.append(module_name)
                        if len(attempted) == 1:
                            raise ModuleNotFoundError("No module named 'room_message'", name=module_name)
                    return real_import(module_name, *args, **kwargs)

                with tempfile.TemporaryDirectory() as directory, \
                        mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": directory, "SUTANDO_TELEMETRY": "0"}), \
                        mock.patch.dict(sys.modules), mock.patch.object(sys, "path", list(sys.path)), \
                        mock.patch.object(builtins, "__import__", side_effect=first_import_missing), \
                        mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("import network")):
                    sys.modules.pop("room_message", None)
                    module = load("room_contract_" + name + "_fallback", relative)
                    self.assertEqual(attempted, ["room_message", "room_message"])
                    self.assertEqual(Path(module.room_message.__file__).resolve().parent,
                                     (ROOT / relative).resolve().parent)
                    with self.assertRaisesRegex(ValueError, "extra_content"):
                        module.room_message.room_message_payload({"extra_content": BAD})

    def test_skills_load_generated_policy_without_a_core_checkout(self):
        probe = """
import contextlib, importlib.util, io, pathlib, sys
from unittest import mock
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('standalone_contract', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert pathlib.Path(module.room_message.__file__).resolve().parent == path.resolve().parent
payload = {'op': 'message', 'extra_content': {'wrapper': {'space.ag2.card': {}}}}
with mock.patch('urllib.request.urlopen', side_effect=AssertionError('network reached')):
    if path.name == 'notify.py':
        with contextlib.redirect_stderr(io.StringIO()) as error:
            assert module._post('https://gateway.example.invalid/v1/room', payload, {}) is False
        assert 'extra_content' in error.getvalue()
    else:
        try:
            module.http_json('POST', 'https://gateway.example.invalid/v1/room', {}, payload)
        except ValueError as error:
            assert 'extra_content' in str(error)
        else:
            raise AssertionError('malformed payload accepted')
"""
        with tempfile.TemporaryDirectory() as directory:
            env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
            env.update(CLAUDE_CONFIG_DIR=directory, SUTANDO_TELEMETRY="0")
            for relative in ("skills/agent-room-ops/_gateway.py", "skills/task-progress/scripts/notify.py"):
                with self.subTest(relative=relative):
                    source = ROOT / relative
                    target = Path(directory) / "isolated" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    shutil.copyfile(source.parent / "room_message.py", target.parent / "room_message.py")
                    result = subprocess.run([sys.executable, "-I", "-c", probe, str(target)],
                                            cwd=directory, capture_output=True, text=True,
                                            env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

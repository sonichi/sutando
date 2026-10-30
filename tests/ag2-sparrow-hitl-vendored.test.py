#!/usr/bin/env python3
"""The HITL decision domain is vendored into ag2_sparrow.hitl byte-identical (sync_from_src) and
imports with no shim and no sys.path help: measured from an unrelated cwd in an isolated
interpreter with only the package directory on the path."""
import ast
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "packages" / "ag2-sparrow"
HITL = ("__init__.py", "schema.py", "host.py", "manager.py", "projector.py", "renotify.py", "replies.py")


class VendoredHitl(unittest.TestCase):
    def test_package_projector_guards_cards_and_notices_without_a_top_level_policy(self):
        code = textwrap.dedent('''
            import sys, tempfile
            from pathlib import Path
            from unittest import mock
            sys.path.insert(0, sys.argv[1])
            sys.modules['room_message'] = None
            from ag2_sparrow import room_message
            from ag2_sparrow.hitl import projector
            from ag2_sparrow.hitl.manager import HitlManager, HitlStore
            from ag2_sparrow.hitl.schema import HumanRequirement
            assert projector.room_message is room_message
            for notice in ('reminder', 'recovery', 'failed'):
                with tempfile.TemporaryDirectory() as directory:
                    manager = HitlManager(HitlStore(Path(directory)))
                    req = manager.create(HumanRequirement(kind='auth', runtime='claude',
                                                         message='Sign in', guard='contract'))
                    sender = mock.Mock(return_value={'event_id': '$created'})
                    with mock.patch.object(projector, 'WIRE_FIELD', 'body'):
                        try:
                            projector.project(manager, sender, '!room:example.invalid', clock=lambda: 100)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError('invalid card accepted')
                    sender.assert_not_called()
                    assert manager.needs_projection(req.id)
                    if notice == 'failed':
                        manager.refuse_projection(req.id, req.revision, 'HTTP 400', 1, lambda n: {})
                    else:
                        projector.project(manager, sender, '!room:example.invalid', clock=lambda: 100)
                    if notice == 'recovery':
                        manager.resolve(req.id)
                        manager.record_projection(req.id, manager.get(req.id).revision, None)
                    before = manager.store.projection(req.id)
                    sender.reset_mock()
                    with mock.patch.object(projector, 'NOTICE_FIELD', 'body'):
                        try:
                            projector.project(manager, sender, '!room:example.invalid', clock=lambda: 2000)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError('invalid notice accepted')
                    sender.assert_not_called()
                    assert manager.store.projection(req.id) == before
                    projector.project(manager, sender, '!room:example.invalid', clock=lambda: 2000)
                    sender.assert_called_once()
                    assert manager.store.projection(req.id) != before
            print('OK')
        ''')
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, '-I', '-c', code, str(PKG)],
                                    cwd=cwd, capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'OK'), result.stderr)

    def test_every_hitl_module_is_vendored_byte_identical(self):
        sync = (PKG / "tools" / "sync_from_src.py").read_text()
        for name in HITL:
            with self.subTest(module=name):
                self.assertIn('"src/hitl/%s": "hitl/%s"' % (name, name), sync, "mapped in sync_from_src")
                self.assertEqual((REPO / "src" / "hitl" / name).read_bytes(),
                                 (PKG / "ag2_sparrow" / "hitl" / name).read_bytes())

    def test_the_vendored_copy_imports_from_an_unrelated_cwd_with_only_the_package_on_the_path(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import ag2_sparrow.hitl.projector, ag2_sparrow.hitl.replies, ag2_sparrow.hitl.manager as m\n"
                "assert 'workspace_default' not in sys.modules, 'the resolver is lazy'\n"
                "assert 'file_lock' not in sys.modules and 'local_task_protocol' not in sys.modules, "
                "'siblings resolve inside the package, not top-level'\n"
                "import tempfile; from pathlib import Path\n"
                "d = Path(tempfile.mkdtemp())\n"
                "from ag2_sparrow.hitl.schema import ActionReply, HumanRequirement, Action\n"
                "mgr = m.HitlManager(m.HitlStore(m.default_store(d)))\n"
                "assert 'workspace_default' not in sys.modules, 'a given root never imports the resolver'\n"
                "req = mgr.create(HumanRequirement(kind='choice', runtime='claude-local', message='stall', guard='g',"
                " room='!dm:x', actions=[Action(id='stop', kind='stop', label='Stop')]))\n"
                "a = mgr.apply_action(ActionReply(hitl_id=req.id, expected_revision=1, action_id='stop', guard='g',"
                " room='!dm:x'), patch=lambda r: r.subject.update({'claude-local': {'effect_phase': 'STOPPING'}}))\n"
                "assert a.id == 'stop' and mgr.get(req.id).subject['claude-local']['effect_phase'] == 'STOPPING'\n"
                "print('OK')\n" % str(PKG))
        with tempfile.TemporaryDirectory() as cwd:
            r = subprocess.run([sys.executable, "-I", "-c", code], cwd=cwd, capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "OK"), r.stderr)

    def test_src_hitl_imports_siblings_relative_first_and_resolves_the_workspace_lazily(self):
        for name in ("manager.py", "replies.py"):
            tree = ast.parse((REPO / "src" / "hitl" / name).read_text())
            top = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
            self.assertFalse([n.module for n in top if n.module == "workspace_default"],
                             "%s: workspace_default is imported only inside default_store/actions_dir" % name)
            for node in top:
                if node.module in ("file_lock", "local_task_protocol"):
                    self.fail("%s imports %s top-level without the package-relative try first" % (name, node.module))


if __name__ == "__main__":
    unittest.main()

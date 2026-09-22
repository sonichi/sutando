import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('core_metadata', Path(__file__).resolve().parents[1] / 'src/core_metadata.py')
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)


class CoreMetadataTest(unittest.TestCase):
    def test_config_and_override(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'CLAUDE_CONFIG_DIR': tmp, 'CODEX_HOME': tmp, 'SUTANDO_CORE_MODEL': '',
        }):
            root = Path(tmp)
            self.assertIsNone(metadata.configured_model('claude'))
            (root / 'settings.json').write_text('{"model":"opus"}')
            self.assertEqual(metadata.configured_model('claude'), 'opus')
            (root / 'config.toml').write_text('model = "test-codex-model"\n')
            self.assertEqual(metadata.configured_model('codex'), 'test-codex-model')
            os.environ['SUTANDO_CORE_MODEL'] = 'override'
            metadata.record(root, 'codex', 'sutando-core')
            record = json.loads((root / 'state/core-runtime.json').read_text())
            self.assertEqual(record['runtime'], 'codex')
            self.assertEqual(record['model'], 'override')
            os.environ['SUTANDO_CORE_MODEL'] = ''
            (root / 'settings.json').write_text('[]')
            self.assertIsNone(metadata.configured_model('claude'))


if __name__ == '__main__':
    unittest.main()

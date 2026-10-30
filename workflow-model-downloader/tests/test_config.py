import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('workflow_download_config', Path(__file__).resolve().parents[1] / 'config.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = Path(self.tmp.name)
        self.config = self.plugin / 'config.json'

    def test_absolute_root_and_windows_utf8_bom(self):
        expected = self.plugin / '模型库'
        self.config.write_text(json.dumps({'central_model_root': str(expected)}), encoding='utf-8-sig')
        self.assertEqual(module.load_model_root(self.plugin), expected.resolve())

    def test_invalid_config_does_not_silently_fall_back(self):
        values = [[], {'central_model_root': ''}, {'central_model_root': 3},
                  {'central_model_root': 'relative/models'}, {'typo_model_root': str(self.plugin)}]
        for value in values:
            with self.subTest(value=value):
                self.config.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    module.load_model_root(self.plugin)
        self.config.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(ValueError):
            module.load_model_root(self.plugin)

    def test_default_keeps_windows_install_compatible(self):
        if os.name == 'nt':
            self.assertEqual(module.load_model_root(self.plugin), Path('D:/ai_models/image_models').resolve())
        else:
            with self.assertRaises(ValueError):
                module.load_model_root(self.plugin)


if __name__ == '__main__':
    unittest.main(verbosity=2)

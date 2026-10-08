import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from sagel import create_app


class EnvironmentConfigTests(unittest.TestCase):
    def test_environment_config_and_explicit_override(self):
        with tempfile.TemporaryDirectory(prefix='test-environment-', dir=Path(__file__).resolve().parents[1]) as folder:
            config = {'TESTING': True, 'DATABASE': str(Path(folder) / 'test.db')}
            with patch.dict(os.environ, {'SAGEL_SECRET_KEY': 'test-only-environment-key',
                                         'SMTP_HOST': 'smtp.example.test',
                                         'SMTP_PASSWORD': 'test-only-password'}):
                app = create_app(config)
                self.assertEqual(app.secret_key, 'test-only-environment-key')
                self.assertEqual(app.config['SAGEL_SMTP']['host'], 'smtp.example.test')
                self.assertEqual(app.config['SAGEL_SMTP']['senha'], 'test-only-password')
                override = create_app({**config, 'SECRET_KEY': 'explicit-test-key',
                                       'SAGEL_SMTP': {'host': 'override.example.test'}})
                self.assertEqual(override.secret_key, 'explicit-test-key')
                self.assertEqual(override.config['SAGEL_SMTP'], {'host': 'override.example.test'})

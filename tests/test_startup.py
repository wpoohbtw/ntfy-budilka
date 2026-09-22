import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ndabudilka.__main__ import SafeLibraryLogs, instance_lock
from ndabudilka.config import Config, ROOT


class StartupTests(unittest.TestCase):
    def env(self):
        return {"BOT_TOKEN": "123456:dummy_test_token", "ADMIN_ID": "123", "TELEGRAM_API_ID": "456",
                "TELEGRAM_API_HASH": "a" * 32, "TELETHON_STRING_SESSION": "",
                "NTFY_ALLOW_PRIVATE_HOSTS": "false", "NTFY_ALLOW_HTTP": "false"}

    def test_required_config_and_relative_paths(self):
        with patch.dict(os.environ, self.env(), clear=True), patch("ndabudilka.config.load_dotenv"):
            config = Config.load()
            self.assertEqual(config.admin_id, 123)
            self.assertEqual(config.session_path, ROOT / "data/watcher.session")
            self.assertNotIn("dummy_test_token", repr(config))

    def test_missing_setting_has_safe_error(self):
        with patch.dict(os.environ, {}, clear=True), patch("ndabudilka.config.load_dotenv"):
            with self.assertRaisesRegex(ValueError, "BOT_TOKEN"):
                Config.load()

    def test_invalid_admin_id_and_boolean(self):
        for changed in ({"ADMIN_ID": "-1"}, {"NTFY_ALLOW_HTTP": "perhaps"}):
            with patch.dict(os.environ, self.env() | changed, clear=True), patch("ndabudilka.config.load_dotenv"):
                with self.assertRaises(ValueError):
                    Config.load()

    def test_process_lock_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.lock"
            with instance_lock(path):
                with self.assertRaises(ValueError):
                    with instance_lock(path):
                        pass
            with instance_lock(path):
                pass

    def test_offline_check_does_not_open_or_change_session(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "test.session"
            session.write_bytes(b"offline-test-file")
            env = dict(os.environ) | self.env() | {"TELETHON_SESSION": str(session), "PYTHONUTF8": "1"}
            result = subprocess.run([sys.executable, "-m", "ndabudilka", "--check"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Авторизация и сеть не проверялись", result.stdout)
            self.assertEqual(session.read_bytes(), b"offline-test-file")

    def test_library_logs_do_not_expose_payload_or_secrets(self):
        record = logging.LogRecord("aiogram.event", logging.ERROR, "x", 1,
                                   "request with secret %s", ("token-value",), None)
        SafeLibraryLogs().filter(record)
        self.assertNotIn("token-value", record.getMessage())
        self.assertNotIn("request with secret", record.getMessage())

    def test_console_and_rotating_text_log_are_configured_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            script = (
                "import logging,sys\n"
                "from pathlib import Path\n"
                "from logging.handlers import RotatingFileHandler\n"
                "from ndabudilka.__main__ import configure_logging\n"
                "configure_logging(Path(sys.argv[1]))\n"
                "root=logging.getLogger()\n"
                "file_handler=next(h for h in root.handlers if isinstance(h,RotatingFileHandler))\n"
                "assert file_handler.maxBytes == 5*1024*1024\n"
                "assert file_handler.backupCount == 3\n"
                "logger=logging.getLogger('ndabudilka.test')\n"
                "logger.info('Отправка уведомления: источник=%r приоритет=%s','Signals',5)\n"
                "try:\n"
                "    raise AttributeError('private-post-and-token')\n"
                "except AttributeError:\n"
                "    logger.error('Ошибка подтверждения канала',exc_info=True)\n"
            )
            env = dict(os.environ) | {"PYTHONUTF8": "1"}
            result = subprocess.run([sys.executable, "-c", script, directory], cwd=ROOT, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            file_text = (Path(directory) / "bot.log").read_text(encoding="utf-8")
            for output in (result.stderr, file_text):
                self.assertIn("Отправка уведомления: источник='Signals' приоритет=5", output)
                self.assertIn("Ошибка подтверждения канала: AttributeError", output)
                self.assertNotIn("private-post-and-token", output)

    def test_offline_check_allows_missing_session_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "new" / "watcher.session"
            env = dict(os.environ) | self.env() | {"TELETHON_SESSION": str(session), "PYTHONUTF8": "1"}
            result = subprocess.run([sys.executable, "-m", "ndabudilka", "--check"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("будет предложена авторизация", result.stdout)
            self.assertFalse(session.parent.exists())

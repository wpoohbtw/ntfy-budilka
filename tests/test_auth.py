from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import (FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError,
                             PhoneCodeInvalidError, SessionPasswordNeededError)

from ndabudilka.__main__ import make_client
from ndabudilka.auth import ensure_authorized, read_secret
from ndabudilka.config import Config


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = SimpleNamespace(
            is_user_authorized=AsyncMock(return_value=False),
            send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="dummy-hash")),
            sign_in=AsyncMock(),
        )
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("ndabudilka.auth.sys.stdin", Mock(isatty=Mock(return_value=True))))
        self.phone = self.stack.enter_context(patch("builtins.input", return_value="+12345678900"))
        self.secret = self.stack.enter_context(patch("ndabudilka.auth.read_secret", return_value="12345"))
        self.output = self.stack.enter_context(patch("builtins.print"))

    async def test_authorized_session_never_prompts(self):
        self.client.is_user_authorized.return_value = True
        await ensure_authorized(self.client)
        self.phone.assert_not_called()
        self.secret.assert_not_called()
        self.client.send_code_request.assert_not_awaited()

    async def test_login_without_2fa(self):
        await ensure_authorized(self.client)
        self.client.send_code_request.assert_awaited_once_with("+12345678900")
        self.client.sign_in.assert_awaited_once_with(phone="+12345678900", code="12345", phone_code_hash="dummy-hash")
        self.assertEqual(self.secret.call_count, 1)

    async def test_login_with_2fa_preserves_password_spaces(self):
        self.client.sign_in.side_effect = [SessionPasswordNeededError(None), None]
        self.secret.side_effect = ["12345", " dummy password "]
        await ensure_authorized(self.client)
        self.client.sign_in.assert_awaited_with(password=" dummy password ")
        output = str(self.output.call_args_list)
        self.assertNotIn("dummy password", output)
        self.assertNotIn("12345", output)
        self.assertNotIn("dummy-hash", output)

    async def test_invalid_code_then_success_does_not_resend_code(self):
        self.client.sign_in.side_effect = [PhoneCodeInvalidError(None), None]
        self.secret.side_effect = ["11111", "22222"]
        await ensure_authorized(self.client)
        self.assertEqual(self.client.sign_in.await_count, 2)
        self.client.send_code_request.assert_awaited_once()

    async def test_invalid_password_can_be_retried(self):
        self.client.sign_in.side_effect = [SessionPasswordNeededError(None), PasswordHashInvalidError(None), None]
        self.secret.side_effect = ["12345", "wrong", "correct"]
        await ensure_authorized(self.client)
        self.client.sign_in.assert_awaited_with(password="correct")

    async def test_password_attempts_are_bounded(self):
        self.client.sign_in.side_effect = [SessionPasswordNeededError(None)] + [PasswordHashInvalidError(None)] * 3
        self.secret.side_effect = ["12345", "wrong", "wrong", "wrong"]
        with self.assertRaisesRegex(ValueError, "три неверных пароля"):
            await ensure_authorized(self.client)
        self.assertEqual(self.client.sign_in.await_count, 4)

    async def test_code_attempts_are_bounded(self):
        self.client.sign_in.side_effect = PhoneCodeInvalidError(None)
        with self.assertRaisesRegex(ValueError, "три неверных кода"):
            await ensure_authorized(self.client)
        self.assertEqual(self.client.sign_in.await_count, 3)

    async def test_expired_code_stops_with_restart_instruction(self):
        self.client.sign_in.side_effect = PhoneCodeExpiredError(None)
        with self.assertRaisesRegex(ValueError, "Код истёк"):
            await ensure_authorized(self.client)
        self.client.sign_in.assert_awaited_once()

    async def test_flood_wait_is_reported_without_automatic_retry(self):
        self.client.send_code_request.side_effect = FloodWaitError(None, capture=45)
        with self.assertRaisesRegex(ValueError, "45 сек"):
            await ensure_authorized(self.client)
        self.client.send_code_request.assert_awaited_once()

    async def test_bad_phone_and_bot_token_not_sent_to_telegram(self):
        self.phone.side_effect = ["123:bot-token", "nonsense", "+1 (234) 567-8900"]
        await ensure_authorized(self.client)
        self.client.send_code_request.assert_awaited_once_with("+12345678900")

    async def test_cancelled_or_closed_input_does_not_send_code(self):
        for answer in ("", EOFError()):
            with self.subTest(answer=type(answer).__name__):
                self.phone.side_effect = [answer]
                with self.assertRaises(ValueError):
                    await ensure_authorized(self.client)
        self.client.send_code_request.assert_not_awaited()

    async def test_unauthorized_string_session_does_not_login_without_persistence(self):
        with self.assertRaisesRegex(ValueError, "Очистите"):
            await ensure_authorized(self.client, string_session=True)
        self.phone.assert_not_called()

    async def test_noninteractive_launch_exits_without_prompt(self):
        with patch("ndabudilka.auth.sys.stdin", Mock(isatty=Mock(return_value=False))):
            with self.assertRaisesRegex(ValueError, "интерактивном терминале"):
                await ensure_authorized(self.client)
        self.phone.assert_not_called()

    async def test_make_client_creates_missing_session_directory_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new" / "watcher"
            config = Config("1:dummy", 1, 123, "a" * 32, path)
            client = make_client(config)
            try:
                self.assertTrue(path.with_suffix(".session").is_file())
                self.assertFalse(client.is_connected())
            finally:
                client.session.close()


class SecretInputTests(unittest.TestCase):
    def test_hidden_input_uses_getpass(self):
        with patch("ndabudilka.auth.getpass.getpass", return_value=" password ") as getpass:
            self.assertEqual(read_secret("Пароль: "), " password ")
            getpass.assert_called_once_with("Пароль: ")

    def test_echo_fallback_is_rejected(self):
        import getpass
        import warnings

        def fallback(prompt):
            warnings.warn("Cannot hide input", getpass.GetPassWarning)
            self.fail("Input must not continue with echo enabled")

        with patch("ndabudilka.auth.getpass.getpass", side_effect=fallback):
            with self.assertRaisesRegex(ValueError, "скрытый ввод"):
                read_secret("Пароль: ")

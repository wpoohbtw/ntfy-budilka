import unittest

from aiohttp import web

from ndabudilka.notifications import DeliveryError, Notification
from ndabudilka.notifications.ntfy import NtfySender, check_host, parse_destination, validate_token


class DestinationTests(unittest.TestCase):
    def test_bare_topic_uses_ntfy_sh(self):
        self.assertEqual(parse_destination("  my_topic-123  "),
                         {"server": "https://ntfy.sh", "topic": "my_topic-123", "token": ""})
        for invalid in ("", "тема", "two words", "docs", "a" * 129):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_destination(invalid)

    def test_url_and_path_prefix(self):
        self.assertEqual(parse_destination("https://notify.example.com/ntfy/signals")['server'], "https://notify.example.com/ntfy")
        self.assertEqual(parse_destination("https://ntfy.sh/signals/")["topic"], "signals")

    def test_rejects_invalid_urls(self):
        for url in ("http://ntfy.sh/topic", "https://name:pass@ntfy.sh/topic", "https://ntfy.sh/",
                    "https://ntfy.sh/topic?token=secret", "https://ntfy.sh/topic#x", "https://ntfy.sh/два",
                    "https://ntfy.sh:99999/topic", "https://ntfy.sh/a b", "https://ntfy.sh/docs",
                    "https://ntfy.sh/" + "a/" * 600 + "topic"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_destination(url)

    def test_private_addresses_rejected_by_default(self):
        for host in ("localhost", "127.0.0.1", "10.1.1.1", "192.168.1.1", "169.254.169.254", "::1"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                check_host(host, False)
        self.assertEqual(parse_destination("http://127.0.0.1:8080/topic", True, True)["topic"], "topic")

    def test_token_no_header_injection(self):
        self.assertEqual(validate_token("tk_example123"), "tk_example123")
        with self.assertRaises(ValueError):
            validate_token("tk_example\r\nInjected: yes")


class NtfyHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.response_status = 200
        self.reply = {"event": "message", "id": "local-test"}
        self.headers = {}

        async def handler(request):
            self.requests.append((request.path, dict(request.headers), await request.json()))
            return web.json_response(self.reply, status=self.response_status, headers=self.headers)

        app = web.Application()
        app.router.add_post("/", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.server = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        self.sender = NtfySender(allow_private=True, allow_http=True)
        self.destination = {"server": self.server, "topic": "test-only", "token": "tk_example123"}
        self.notification = Notification("Шорт", "Сообщение 🚀", 5, "https://t.me/c/123/10")

    async def asyncTearDown(self):
        await self.sender.close()
        await self.runner.cleanup()

    async def test_json_priority_auth_and_unicode(self):
        await self.sender.send(self.destination, self.notification)
        path, headers, payload = self.requests[0]
        self.assertEqual(path, "/")
        self.assertEqual(headers["Authorization"], "Bearer tk_example123")
        self.assertEqual(payload["priority"], 5)
        self.assertEqual(payload["message"], "Сообщение 🚀")
        self.assertEqual(payload["topic"], "test-only")
        self.assertEqual(payload["click"], self.notification.link)

    async def test_auth_failure_is_permanent_and_safe(self):
        self.response_status = 403
        self.reply = {"error": "secret-server-response"}
        with self.assertRaises(DeliveryError) as caught:
            await self.sender.send(self.destination, self.notification)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("secret-server-response", str(caught.exception))
        self.assertNotIn("tk_example123", str(caught.exception))

    async def test_rate_limit_has_retry_delay(self):
        self.response_status = 429
        self.headers = {"Retry-After": "120"}
        with self.assertRaises(DeliveryError) as caught:
            await self.sender.send(self.destination, self.notification)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 120)

    async def test_server_failure_is_retryable(self):
        self.response_status = 503
        with self.assertRaises(DeliveryError) as caught:
            await self.sender.send(self.destination, self.notification)
        self.assertTrue(caught.exception.retryable)

    async def test_redirect_never_followed(self):
        self.response_status = 307
        self.headers = {"Location": self.server + "/unexpected"}
        with self.assertRaises(DeliveryError):
            await self.sender.send(self.destination, self.notification)
        self.assertEqual(len(self.requests), 1)

    async def test_false_success_rejected(self):
        self.reply = {"success": True}
        with self.assertRaises(DeliveryError):
            await self.sender.send(self.destination, self.notification)

    async def test_long_multibyte_message_fits_payload(self):
        await self.sender.send(self.destination, Notification("Я" * 300, "Я🚀" * 3000, 2))
        payload = self.requests[0][2]
        self.assertLessEqual(len(payload["message"].encode()), 3000)
        self.assertLessEqual(len(payload["title"].encode()), 200)

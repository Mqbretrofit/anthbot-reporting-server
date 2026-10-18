from __future__ import annotations

import unittest
from unittest.mock import patch

from entrypoint import VoiceDownloadTimingMiddleware


class VoiceDownloadTimingMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _run_request(self, path: str, *, query: bytes = b"") -> tuple:
        sent = []

        async def application(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"123", "more_body": True})
            await send({"type": "http.response.body", "body": b"4567", "more_body": False})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query,
            "headers": [(b"range", b"bytes=0-9")],
        }
        with patch("entrypoint._VOICE_DOWNLOAD_LOG.info") as log:
            await VoiceDownloadTimingMiddleware(application)(scope, receive, send)
        return sent, log.call_args

    async def test_paid_download_logs_bytes_without_token(self):
        messages, call = await self._run_request(
            "/api/anthbot/store/voice-packs/test-pack/download",
            query=b"token=secret-must-not-be-logged",
        )
        self.assertEqual(len(messages), 3)
        self.assertIsNotNone(call)
        self.assertEqual(call.args[1], "paid_community")
        self.assertEqual(call.args[2], 200)
        self.assertEqual(call.args[3], 7)
        self.assertTrue(call.args[-2])  # range
        self.assertTrue(call.args[-1])  # completed
        self.assertNotIn("secret-must-not-be-logged", str(call))

    async def test_public_download_logs_separately(self):
        _, call = await self._run_request("/voice-packs/example.pack")
        self.assertEqual(call.args[1], "public_cached")
        self.assertEqual(call.args[3], 7)

    async def test_other_routes_are_not_logged(self):
        _, call = await self._run_request("/health")
        self.assertIsNone(call)


if __name__ == "__main__":
    unittest.main()

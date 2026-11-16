"""Offline FETCH response regressions using synthetic messages only."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bridge_plugin as plugin


class ReadFetchTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, data):
        client = MagicMock()
        client.select.return_value = ("OK", [b"1"])
        client.uid.return_value = ("OK", data)
        async def call(op):
            return op(client)
        server = plugin.create_server({
            "username": "owner@example.test", "imap_port": 1143, "smtp_port": 1025,
            "cert_path": "/tmp/test-only-bridge-cert.pem", "mode": "read-only",
        })
        reader = server._tool_manager.get_tool("proton_read_email").fn
        with patch.object(plugin.MailAccess, "prepare", new_callable=AsyncMock), \
             patch.object(plugin.upstream, "_imap_call", side_effect=call):
            result = await reader(ctx=SimpleNamespace(info=AsyncMock()), uid="42", mailbox="INBOX")
        client.select.assert_called_once_with('"INBOX"', readonly=True)
        client.uid.assert_called_once_with("FETCH", "42", "(BODY.PEEK[])")
        return result

    async def test_missing_literal_is_failure_not_empty_email(self):
        for data in [[b"1 (UID 42 FLAGS ())"], [b")"], [(b"1 (UID 42)", None)]]:
            with self.subTest(data=data):
                result = await self.read(data)
                self.assertFalse(result["ok"])
                self.assertNotIn("data", result)

    async def test_unsolicited_flags_before_literal_do_not_hide_body(self):
        raw = b"Subject: Synthetic subject\r\nFrom: sender@example.test\r\n\r\nSynthetic body"
        result = await self.read([b"9 (FLAGS (\\Seen))", (f"1 (UID 42 BODY[] {{{len(raw)}}}".encode(), raw), b")"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["subject"], "Synthetic subject")
        self.assertIn("Synthetic body", result["data"]["body_text"])

    async def test_literal_first_and_empty_message_remain_valid(self):
        for raw in [b"Subject: Control\r\n\r\nbody", b""]:
            with self.subTest(raw=raw):
                result = await self.read([(f"1 (UID 42 BODY[] {{{len(raw)}}}".encode(), raw), b")"])
                self.assertTrue(result["ok"])

"""Offline behavior checks; no Proton credentials or live mail are used."""
from __future__ import annotations

import json
import smtplib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bridge_plugin as plugin


CONFIG = {
    "username": "owner@example.test", "imap_port": 1143, "smtp_port": 1025,
    "cert_path": "/tmp/test-only-bridge-cert.pem", "mode": "read-only",
}


def fn(server, name):
    return server._tool_manager.get_tool(name).fn


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_setup_starts_and_does_not_touch_keychain_or_network(self):
        with patch.object(plugin, "load_config", return_value=None), \
             patch.object(plugin.subprocess, "run") as command, \
             patch.object(plugin.upstream._imap_pool, "connect") as connect:
            server = plugin.create_server()
            tools = await server.list_tools()
            result = await fn(server, "proton_connection_status")()
        self.assertEqual(len(tools), 5)
        self.assertFalse(result["configured"])
        self.assertEqual(result["connection"], "not_tested")
        command.assert_not_called()
        connect.assert_not_called()

    async def test_bad_config_is_status_not_startup_crash(self):
        with patch.object(plugin, "load_config", side_effect=plugin.ConfigError("invalid")):
            server = plugin.create_server()
        result = await fn(server, "proton_connection_status")()
        self.assertEqual(result["configuration"], "invalid")
        self.assertFalse(result["configured"])

    async def test_real_tool_registry_enforces_modes(self):
        base = {"proton_connection_status", "proton_list_folders", "proton_list_recent", "proton_search_emails", "proton_read_email"}
        for mode, expected in [("read-only", base), ("drafts", base | {"proton_create_draft"}),
                               ("send", base | {"proton_create_draft", "proton_send_email"})]:
            server = plugin.create_server({**CONFIG, "mode": mode})
            tools = await server.list_tools()
            self.assertEqual({tool.name for tool in tools}, expected)
            read = next(tool for tool in tools if tool.name == "proton_read_email")
            self.assertNotIn("mark_seen", read.inputSchema["properties"])
            self.assertNotIn("include_html", read.inputSchema["properties"])

    async def test_read_is_forced_to_peek_without_html(self):
        server = plugin.create_server(CONFIG)
        with patch.object(plugin.MailAccess, "prepare", new_callable=AsyncMock), \
             patch.object(plugin.upstream, "proton_read_email", new_callable=AsyncMock, return_value='{"uid":"42"}') as reader:
            result = await fn(server, "proton_read_email")(ctx=SimpleNamespace(), uid="42", mailbox="INBOX")
        self.assertTrue(result["ok"])
        args = reader.await_args.args[0]
        self.assertFalse(args.mark_seen)
        self.assertFalse(args.include_html)

    async def test_invalid_uids_and_header_injections_fail_before_mail_access(self):
        server = plugin.create_server(CONFIG)
        with patch.object(plugin.MailAccess, "prepare", new_callable=AsyncMock) as prepare:
            for uid in ["0", "-1", "1:*", "1,2", "4294967296", "1\r\nSTORE", "01"]:
                with self.subTest(uid=repr(uid)), self.assertRaises(ValueError):
                    await fn(server, "proton_read_email")(ctx=SimpleNamespace(), uid=uid)
            with self.assertRaises(ValueError):
                await fn(server, "proton_list_recent")(ctx=SimpleNamespace(), mailbox="INBOX\r\nDELETE INBOX")
            with self.assertRaises(ValueError):
                await fn(server, "proton_search_emails")(ctx=SimpleNamespace(), subject="hello\r\nSTORE")
            with self.assertRaises(ValueError):
                await fn(server, "proton_search_emails")(ctx=SimpleNamespace(), since="2026-02-30")
            with self.assertRaises(ValueError):
                await fn(server, "proton_list_recent")(ctx=SimpleNamespace(), limit=100000)
            prepare.assert_not_called()

    async def test_chinese_search_uses_utf8_and_readonly_selection(self):
        client = MagicMock()
        client.uid.return_value = ("OK", [b"1 2"])
        client.select.return_value = ("OK", [b"2"])

        async def call(operation):
            return operation(client)

        params = plugin.upstream.SearchEmailsInput(subject="發票", mailbox="INBOX", limit=20)
        with patch.object(plugin.upstream, "_imap_call", side_effect=call), \
             patch.object(plugin.upstream, "_fetch_headers", return_value=[{"uid":"2"}]):
            result = json.loads(await plugin.search_messages(params, SimpleNamespace()))
        client.select.assert_called_once_with('"INBOX"', readonly=True)
        args = client.uid.call_args.args
        self.assertEqual(args[:3], ("SEARCH", "CHARSET", "UTF-8"))
        self.assertIn('"發票"'.encode("utf-8"), args)
        self.assertEqual(result["total_matched"], 2)

    async def test_raw_errors_are_not_returned(self):
        server = plugin.create_server(CONFIG)
        with patch.object(plugin.MailAccess, "prepare", new_callable=AsyncMock), \
             patch.object(plugin.upstream, "proton_list_folders", new_callable=AsyncMock, return_value="Error: sensitive local detail"):
            result = await fn(server, "proton_list_folders")(ctx=SimpleNamespace())
        self.assertFalse(result["ok"])
        self.assertNotIn("sensitive local detail", json.dumps(result))

    async def test_mode_does_not_authorize_a_send(self):
        server = plugin.create_server({**CONFIG, "mode": "send"})
        with patch.object(plugin.MailAccess, "prepare", new_callable=AsyncMock) as prepare:
            result = await fn(server, "proton_send_email")(to=["alice@example.test"], subject="Test", body_text="Test", user_authorized=False)
        self.assertEqual(result["code"], "not_authorized")
        prepare.assert_not_called()

    async def test_partial_smtp_and_failed_quit_are_reported_without_retry(self):
        server = plugin.create_server({**CONFIG, "mode": "send"})
        smtp = MagicMock()
        smtp.send_message.return_value = {"bob@example.test": (550, b"not accepted")}
        smtp.quit.side_effect = OSError("already disconnected")
        smtp.close.side_effect = OSError("already closed")
        with patch.object(plugin.MailAccess, "prepare", new_callable=AsyncMock), \
             patch.object(plugin.MailAccess, "password", return_value="test-only-password"), \
             patch.object(plugin.upstream, "_ssl_context", return_value=MagicMock()), \
             patch.object(plugin.smtplib, "SMTP", return_value=smtp):
            result = await fn(server, "proton_send_email")(
                to=["alice@example.test", "bob@example.test"], subject="Test", body_text="Test", user_authorized=True)
        self.assertEqual(result["status"], "partial_recipients")
        self.assertEqual(result["accepted_recipients"], ["alice@example.test"])
        self.assertEqual(result["refused_recipients"], ["bob@example.test"])
        smtp.send_message.assert_called_once()

    async def test_ambiguous_send_is_never_automatically_repeated(self):
        server = plugin.create_server({**CONFIG, "mode": "send"})
        smtp = MagicMock()
        smtp.send_message.side_effect = smtplib.SMTPServerDisconnected("test disconnect during DATA")
        with patch.object(plugin.MailAccess, "prepare", new_callable=AsyncMock), \
             patch.object(plugin.MailAccess, "password", return_value="test-only-password"), \
             patch.object(plugin.upstream, "_ssl_context", return_value=MagicMock()), \
             patch.object(plugin.smtplib, "SMTP", return_value=smtp):
            result = await fn(server, "proton_send_email")(to=["alice@example.test"], subject="Test", body_text="Test", user_authorized=True)
        self.assertEqual(result["code"], "send_outcome_unknown")
        smtp.send_message.assert_called_once()

    async def test_ambiguous_draft_append_is_never_repeated(self):
        server = plugin.create_server({**CONFIG, "mode": "drafts"})
        client = MagicMock()
        client.append.side_effect = OSError("test disconnect after APPEND")
        with patch.object(plugin.MailAccess, "prepare", new_callable=AsyncMock), \
             patch.object(plugin.upstream._imap_pool, "get", return_value=client), \
             patch.object(plugin.upstream, "_find_mailbox", return_value="Drafts"):
            result = await fn(server, "proton_create_draft")(to=["alice@example.test"], subject="Test", body_text="Test", user_authorized=True)
        self.assertEqual(result["code"], "draft_outcome_unknown")
        client.append.assert_called_once()

    def test_recipient_and_reply_header_injection_validation(self):
        for address in ["alice@example.test,bob@example.test", "Alice <alice@example.test>", "alice@example.test\r\nBcc: attacker@example.test"]:
            with self.subTest(address=repr(address)), self.assertRaises(ValueError):
                plugin.valid_recipients([address], required=True)
        with self.assertRaises(ValueError):
            plugin.validate_message(["alice@example.test"], "Hello\r\nBcc: attacker@example.test", "Hello", None, None)
        with self.assertRaises(ValueError):
            plugin.validate_message(["alice@example.test"], "Hello", "Hello", None, None, "<one@example.test> <two@example.test>")


if __name__ == "__main__":
    unittest.main()

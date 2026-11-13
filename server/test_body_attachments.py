"""Synthetic MIME regressions; no real mail, credentials, or network access."""
from __future__ import annotations

import email
import json
import unittest
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bridge_plugin as plugin


def text_part(text, subtype="plain"):
    part = EmailMessage()
    part.set_content(text, subtype=subtype)
    return part


def attached_message(*, disposition="attachment", filename="nested.eml"):
    nested = text_part("ATTACHMENT_ONLY_SENTINEL")
    nested["Subject"] = "Synthetic attached email"
    nested.add_alternative("<p>ATTACHED_HTML_SENTINEL</p>", subtype="html")
    part = EmailMessage()
    part.set_type("message/rfc822")
    part.set_payload([nested])
    if disposition:
        if filename:
            part.add_header("Content-Disposition", disposition, filename=filename)
        else:
            part.add_header("Content-Disposition", disposition)
    elif filename:
        part.set_param("name", filename)
    return part


def mixed_message(*parts):
    msg = EmailMessage()
    msg["From"] = "sender@example.test"
    msg["To"] = "owner@example.test"
    msg["Subject"] = "Synthetic MIME fixture"
    msg.make_mixed()
    for part in parts:
        msg.attach(part)
    return msg


def parsed(msg):
    # Exercise the same compat32 parser used after the real BODY.PEEK response.
    return email.message_from_bytes(msg.as_bytes())


class BodyAttachmentTests(unittest.TestCase):
    def test_attached_eml_is_metadata_only_with_or_without_filename(self):
        for filename in ("nested.eml", None):
            with self.subTest(filename=filename):
                msg = parsed(mixed_message(
                    text_part("VISIBLE_BODY"), attached_message(filename=filename)))
                plain, html, attachments = plugin.upstream._extract_body(msg)
                self.assertEqual(plain.strip(), "VISIBLE_BODY")
                self.assertEqual(html, "")
                self.assertEqual(attachments, [{
                    "filename": filename or "unnamed",
                    "content_type": "message/rfc822", "size_bytes": None,
                }])

    def test_named_inline_eml_keeps_existing_filename_attachment_rule(self):
        for disposition in ("inline", None):
            with self.subTest(disposition=disposition):
                msg = parsed(mixed_message(
                    text_part("VISIBLE_BODY"), attached_message(disposition=disposition)))
                plain, html, attachments = plugin.upstream._extract_body(msg)
                self.assertEqual(plain.strip(), "VISIBLE_BODY")
                self.assertEqual(html, "")
                self.assertEqual(attachments, [{
                    "filename": "nested.eml", "content_type": "message/rfc822",
                    "size_bytes": None,
                }])

    def test_multipart_attachment_prunes_the_whole_subtree(self):
        attachment = mixed_message(text_part("ATTACHMENT_ONLY_SENTINEL"), attached_message())
        attachment.add_header("Content-Disposition", "attachment", filename="bundle.mime")
        msg = parsed(mixed_message(text_part("VISIBLE_BODY"), attachment))
        plain, html, attachments = plugin.upstream._extract_body(msg)
        self.assertEqual(plain.strip(), "VISIBLE_BODY")
        self.assertEqual(html, "")
        self.assertEqual(attachments, [{
            "filename": "bundle.mime", "content_type": "multipart/mixed",
            "size_bytes": None,
        }])

    def test_root_attachment_is_not_exposed_as_body(self):
        plain, html, attachments = plugin.upstream._extract_body(parsed(attached_message()))
        self.assertEqual((plain, html), ("", ""))
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "nested.eml")

    def test_normal_alternatives_order_and_leaf_attachment_sizes_are_preserved(self):
        alternative = text_part("FIRST")
        alternative.add_alternative("<p>HTML_BODY</p>", subtype="html")
        binary = EmailMessage()
        binary.set_content(b"\x00\xff\x01", maintype="application", subtype="octet-stream")
        binary.add_header("Content-Disposition", "attachment", filename="bytes.bin")
        empty = text_part("")
        empty.set_payload("")
        empty.add_header("Content-Disposition", "attachment", filename="empty.txt")
        msg = parsed(mixed_message(alternative, attached_message(), binary, empty, text_part("LAST")))
        plain, html, attachments = plugin.upstream._extract_body(msg)
        self.assertEqual(plain, "FIRST\n\n\nLAST\n")
        self.assertEqual(html, "<p>HTML_BODY</p>\n")
        self.assertEqual(attachments, [
            {"filename": "nested.eml", "content_type": "message/rfc822", "size_bytes": None},
            {"filename": "bytes.bin", "content_type": "application/octet-stream", "size_bytes": 3},
            {"filename": "empty.txt", "content_type": "text/plain", "size_bytes": 0},
        ])

    def test_unnamed_inline_forward_retains_existing_body_behavior(self):
        msg = parsed(mixed_message(
            text_part("VISIBLE_BODY"), attached_message(disposition="inline", filename=None)))
        plain, html, attachments = plugin.upstream._extract_body(msg)
        self.assertIn("VISIBLE_BODY", plain)
        self.assertIn("ATTACHMENT_ONLY_SENTINEL", plain)
        self.assertIn("ATTACHED_HTML_SENTINEL", html)
        self.assertEqual(attachments, [])

    def test_related_and_named_inline_text_keep_existing_body_order(self):
        related = EmailMessage()
        related.make_related()
        related.attach(text_part("<p>HTML_BODY</p>", subtype="html"))
        image = EmailMessage()
        image.set_content(b"synthetic image", maintype="image", subtype="png")
        image.add_header("Content-Disposition", "inline", filename="image.png")
        related.attach(image)
        inline_text = text_part("INLINE_TEXT")
        inline_text.add_header("Content-Disposition", "inline", filename="inline.txt")
        msg = parsed(mixed_message(text_part("FIRST"), related, inline_text, text_part("LAST")))
        plain, html, attachments = plugin.upstream._extract_body(msg)
        self.assertEqual(plain, "FIRST\n\n\nINLINE_TEXT\n\n\nLAST\n")
        self.assertEqual(html, "<p>HTML_BODY</p>\n")
        self.assertEqual(attachments, [{
            "filename": "image.png", "content_type": "image/png", "size_bytes": 15,
        }])


class ReadAttachmentContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_read_tool_does_not_return_attached_message_content(self):
        config = {"username": "owner@example.test", "imap_port": 1143,
                  "smtp_port": 1025, "cert_path": "/tmp/test-only-cert.pem",
                  "mode": "read-only"}
        server = plugin.create_server(config)
        read = server._tool_manager.get_tool("proton_read_email").fn
        raw = mixed_message(text_part("VISIBLE_BODY"), attached_message()).as_bytes()
        client = MagicMock()
        client.select.return_value = ("OK", [b"1"])
        client.uid.return_value = ("OK", [(b"1 (BODY[] {123}", raw), b")"])

        async def call(operation):
            return operation(client)

        with patch.object(plugin.MailAccess, "prepare", new_callable=AsyncMock), \
             patch.object(plugin.upstream, "_imap_call", side_effect=call), \
             patch.object(plugin.upstream._imap_pool, "connect") as connect, \
             patch.object(plugin.subprocess, "run") as command:
            result = await read(ctx=SimpleNamespace(info=AsyncMock()), uid="42", mailbox="INBOX")
        self.assertTrue(result["ok"], result)
        serialized = json.dumps(result)
        self.assertIn("VISIBLE_BODY", serialized)
        self.assertNotIn("ATTACHMENT_ONLY_SENTINEL", serialized)
        self.assertNotIn("ATTACHED_HTML_SENTINEL", serialized)
        self.assertIn("nested.eml", serialized)
        self.assertNotIn("body_html", serialized)
        client.select.assert_called_once_with('"INBOX"', readonly=True)
        client.uid.assert_called_once_with("FETCH", "42", "(BODY.PEEK[])")
        connect.assert_not_called()
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()

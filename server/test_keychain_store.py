"""Offline secret-input regressions. No real Keychain or credentials are used."""
import getpass
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keychain_store as store


def result(stdout="", code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


class KeychainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fake keychain ")
        self.addCleanup(self.temp.cleanup)
        self.keychain = Path(self.temp.name) / "login.keychain-db"
        self.keychain.touch()
        self.platform = patch.object(store.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.tty = patch.object(store.sys.stdin, "isatty", return_value=True)
        self.tty.start()
        self.addCleanup(self.tty.stop)

    def test_long_secret_roundtrip_is_exact_without_secret_in_argv(self):
        secret = "test-" + "x" * 200 + "'\\\""
        with patch.object(store.getpass, "getpass", side_effect=[secret, secret]), \
             patch.object(store.subprocess, "run", side_effect=[
                 result(shlex.quote(str(self.keychain))), result(), result(secret + "\n")
             ]) as run:
            self.assertEqual(store.prompt_and_store("test_service", "person+test@example.invalid", "Secret"), secret)
        write = run.call_args_list[1]
        self.assertEqual(write.args[0], ["/usr/bin/security", "-i"])
        self.assertNotIn(secret, write.args[0])
        self.assertNotIn(secret, write.kwargs["input"])
        self.assertIn("-X " + secret.encode().hex(), write.kwargs["input"])
        self.assertIn('"' + str(self.keychain) + '"', write.kwargs["input"])
        self.assertTrue(write.kwargs["capture_output"])
        self.assertEqual(run.call_args_list[-1].args[0][-1], str(self.keychain))

    def test_mismatch_or_invalid_input_never_writes(self):
        for values in [("one", "two"), ("", ""), ("line\nline", "line\nline"), ("x" * 513,) * 2]:
            with self.subTest(values_length=tuple(map(len, values))), \
                 patch.object(store.getpass, "getpass", side_effect=values), \
                 patch.object(store, "default_keychain", return_value=self.keychain), \
                 patch.object(store.subprocess, "run") as run, self.assertRaises(store.SecretInputError):
                store.prompt_and_store("test_service", "test_account", "Secret")
            run.assert_not_called()

    def test_validator_rejects_before_writing(self):
        with patch.object(store.getpass, "getpass", side_effect=["not-a-key", "not-a-key"]), \
             patch.object(store, "default_keychain", return_value=self.keychain), \
             patch.object(store.subprocess, "run") as run, self.assertRaises(store.SecretInputError):
            store.prompt_and_store("s", "a", "Secret", validator=lambda value: value.startswith("sk-"))
        run.assert_not_called()

    def test_readback_detects_truncation_even_with_success_exit(self):
        secret = "x" * 200
        with patch.object(store.getpass, "getpass", side_effect=[secret, secret]), \
             patch.object(store, "default_keychain", return_value=self.keychain), \
             patch.object(store.subprocess, "run", side_effect=[result(), result(secret[:128])]), \
             self.assertRaisesRegex(store.SecretInputError, "complete saved credential"):
            store.prompt_and_store("s", "a", "Secret")

    def test_missing_default_keychain_fails_before_prompt_or_write(self):
        with patch.object(store.subprocess, "run", return_value=result(str(self.keychain) + ".missing")), \
             patch.object(store.getpass, "getpass") as prompt, self.assertRaises(store.SecretInputError):
            store.prompt_and_store("s", "a", "Secret")
        prompt.assert_not_called()

    def test_non_tty_and_echo_fallback_are_rejected(self):
        with patch.object(store.sys.stdin, "isatty", return_value=False), \
             patch.object(store.subprocess, "run") as run, self.assertRaises(store.SecretInputError):
            store.prompt_and_store("s", "a", "Secret")
        run.assert_not_called()
        with patch.object(store, "default_keychain", return_value=self.keychain), \
             patch.object(store.getpass, "getpass", side_effect=getpass.GetPassWarning), \
             patch.object(store.subprocess, "run") as run, self.assertRaises(store.SecretInputError):
            store.prompt_and_store("s", "a", "Secret")
        run.assert_not_called()

    def test_timeout_error_does_not_echo_subprocess_data(self):
        secret = "synthetic-secret"
        with patch.object(store, "default_keychain", return_value=self.keychain), \
             patch.object(store.getpass, "getpass", side_effect=[secret, secret]), \
             patch.object(store.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 30, output=secret)), \
             self.assertRaises(store.SecretInputError) as error:
            store.prompt_and_store("s", "a", "Secret")
        self.assertNotIn(secret, str(error.exception))


if __name__ == "__main__":
    unittest.main()

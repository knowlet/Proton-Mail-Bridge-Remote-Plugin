"""Offline checks for configuration and setup; never accesses a real mailbox."""
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import configuration

spec = importlib.util.spec_from_file_location("proton_local_setup", ROOT / "setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "config.json"
        self.valid = {"username": "person@example.invalid", "cert_path": str(self.directory / "cert.pem")}

    def save(self, data=None, mode=0o600):
        self.path.write_text(json.dumps(self.valid if data is None else data), encoding="utf-8")
        self.path.chmod(mode)

    def test_missing_config_has_setup_state(self):
        self.assertIsNone(configuration.load_config(self.path))

    def test_defaults_are_read_only_and_standard_ports(self):
        self.save()
        config = configuration.load_config(self.path)
        self.assertEqual(config["mode"], "read-only")
        self.assertEqual((config["imap_port"], config["smtp_port"]), (1143, 1025))

    def test_only_explicit_known_modes(self):
        for mode in configuration.MODES:
            with self.subTest(mode=mode):
                self.assertEqual(configuration.validate_config(dict(self.valid, mode=mode))["mode"], mode)
        for bad in ["full", "READ_ONLY", "", None, True, 0, [], {}]:
            with self.subTest(bad=bad), self.assertRaises(configuration.ConfigError):
                configuration.validate_config(dict(self.valid, mode=bad))

    def test_credentials_and_host_override_are_rejected(self):
        for key in ["password", "PROTON_BRIDGE_PASS", "host", "smtp_host", "tls_policy"]:
            with self.subTest(key=key), self.assertRaises(configuration.ConfigError):
                configuration.validate_config(dict(self.valid, **{key: "secret-or-nonlocal"}))

    def test_json_root_must_be_object(self):
        for bad in [None, [], "config", 1]:
            with self.subTest(bad=bad), self.assertRaises(configuration.ConfigError):
                configuration.validate_config(bad)

    def test_port_types_and_range(self):
        for key in ["imap_port", "smtp_port"]:
            for bad in [True, False, 0, -1, 65536, "1143", 1143.0, None]:
                with self.subTest(key=key, bad=bad), self.assertRaises(configuration.ConfigError):
                    configuration.validate_config(dict(self.valid, **{key: bad}))

    def test_username_cannot_contain_control_or_space(self):
        for bad in ["", "name", "first last@example.invalid", "p@example.invalid\r\nNOOP", "p\x00@example.invalid",
                    "Person <person@example.invalid>", "<person@example.invalid>", '"person"@example.invalid',
                    "人@example.invalid"]:
            with self.subTest(bad=bad), self.assertRaises(configuration.ConfigError):
                configuration.validate_config(dict(self.valid, username=bad))

    def test_certificate_path_must_be_absolute_and_clean(self):
        for bad in ["relative.pem", "~/cert.pem", "/tmp/a\x00b", "", None]:
            with self.subTest(bad=bad), self.assertRaises(configuration.ConfigError):
                configuration.validate_config(dict(self.valid, cert_path=bad))

    def test_readable_by_others_config_is_rejected(self):
        for mode in [0o644, 0o640, 0o604, 0o666]:
            self.save(mode=mode)
            with self.subTest(mode=mode), self.assertRaises(configuration.ConfigError):
                configuration.load_config(self.path)

    def test_config_symlink_is_rejected(self):
        self.save()
        link = self.directory / "linked.json"
        link.symlink_to(self.path)
        with self.assertRaises(configuration.ConfigError):
            configuration.load_config(link)

    def test_symlink_directory_is_rejected(self):
        real = self.directory / "real"
        real.mkdir(mode=0o700)
        (real / "config.json").write_text(json.dumps(self.valid))
        (real / "config.json").chmod(0o600)
        link = self.directory / "linked"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(configuration.ConfigError):
            configuration.load_config(link / "config.json")

    def test_other_writable_directory_is_rejected(self):
        self.save()
        self.directory.chmod(0o770)
        with self.assertRaises(configuration.ConfigError):
            configuration.load_config(self.path)

    def test_wrong_owner_is_rejected(self):
        self.save()
        with patch.object(configuration.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(configuration.ConfigError):
                configuration.load_config(self.path)

    def test_bad_json_and_oversize_are_rejected(self):
        for content in ["{broken", " " * 16_385]:
            self.path.write_text(content)
            self.path.chmod(0o600)
            with self.subTest(length=len(content)), self.assertRaises(configuration.ConfigError):
                configuration.load_config(self.path)

    def test_relative_configuration_environment_is_rejected(self):
        with patch.dict(os.environ, {"PROTON_MAIL_BRIDGE_CONFIG": "relative.json"}):
            with self.assertRaises(configuration.ConfigError):
                configuration.config_path()

    def test_private_write_is_owner_only_and_replaces_atomically(self):
        target = self.directory / "managed" / "config.json"
        setup.private_write(target, "first")
        setup.private_write(target, "second")
        self.assertEqual(target.read_text(), "second")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["config.json"])

    def test_private_write_refuses_symlink_target(self):
        target = self.directory / "original"
        target.write_text("keep")
        link = self.directory / "link"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            setup.private_write(link, "overwrite")
        self.assertEqual(target.read_text(), "keep")

    def test_private_directory_refuses_symlink(self):
        target = self.directory / "real"
        target.mkdir()
        link = self.directory / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            setup.private_directory(link)

    def test_failed_reconfiguration_preserves_trusted_cert_and_cleans_stage(self):
        target_cert = self.directory / "bridge-cert.pem"
        old_cert = "previous trusted certificate\n"
        target_cert.write_text(old_cert, encoding="ascii")
        target_cert.chmod(0o600)
        self.save(dict(self.valid, cert_path=str(target_cert), mode="read-only"))
        old_config = self.path.read_bytes()
        exported = self.directory / "new-export.pem"
        new_cert = "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n"
        exported.write_text(new_cert, encoding="ascii")
        observed_staging = []

        def fail_verification(candidate_config):
            candidate = Path(candidate_config["cert_path"])
            observed_staging.append(candidate)
            self.assertNotEqual(candidate, target_cert)
            self.assertEqual(candidate.read_text(encoding="ascii"), new_cert)
            self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)
            self.assertEqual(target_cert.read_text(encoding="ascii"), old_cert)
            self.assertEqual(self.path.read_bytes(), old_config)
            raise OSError("simulated Bridge connection failure")

        with (
            patch.dict(os.environ, {"PROTON_MAIL_BRIDGE_CONFIG": str(self.path)}),
            patch.object(setup.sys, "platform", "darwin"),
            patch.object(setup.sys, "argv", ["setup.py"]),
            patch.object(setup.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=["", "", "", str(exported), "replace"]),
            patch.object(setup, "install_runtime") as install,
            patch.object(setup, "prompt_and_store") as secure_prompt,
            patch.object(setup.ssl, "PEM_cert_to_DER_cert", return_value=b"candidate"),
            patch.object(setup.ssl, "SSLContext"),
            patch.object(setup, "verify_connection", side_effect=fail_verification) as verify,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(OSError):
                setup.main()
        install.assert_called_once()
        verify.assert_called_once()
        secure_prompt.assert_called_once()
        self.assertEqual(secure_prompt.call_args.args[:2], ("proton_mail_bridge_plugin", self.valid["username"]))
        self.assertEqual(target_cert.read_text(encoding="ascii"), old_cert)
        self.assertEqual(self.path.read_bytes(), old_config)
        self.assertTrue(observed_staging)
        self.assertFalse(observed_staging[0].exists())
        self.assertEqual(list(self.directory.glob(".bridge-cert-check-*")), [])

    def test_help_works_without_macos_or_secrets(self):
        result = subprocess.run([sys.executable, str(ROOT / "setup.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check", result.stdout)
        self.assertIn("read-only", result.stdout)

    @unittest.skipIf(sys.platform == "darwin", "This verifies non-Mac refusal")
    def test_non_macos_setup_refuses_before_mutation(self):
        result = subprocess.run([sys.executable, str(ROOT / "setup.py")], capture_output=True, text=True,
                                env=dict(os.environ, PROTON_MAIL_BRIDGE_CONFIG=str(self.path)))
        self.assertEqual(result.returncode, 2)
        self.assertIn("macOS Keychain", result.stderr)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

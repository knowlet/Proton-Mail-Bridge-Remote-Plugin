"""Offline deployment contract tests. No launchd, Keychain or network changes."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import deploy_mini as deploy
import run as launcher

TUNNEL = "tunnel_" + "a" * 32
SECRET = "sk-test-runtime-key-never-in-arguments-1234567890"


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mini deployment ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.settings = {"tunnel_id": TUNNEL, "tunnel_client": "/opt/homebrew/opt/tunnel-client/bin/tunnel-client",
                         "python": "/private/a path/bin/python", "package_root": str(ROOT),
                         "bridge_config": str(self.home / "bridge config.json")}

    def save_settings(self):
        p = deploy.paths()
        deploy.private_dir(p["base"])
        deploy.atomic_write(p["settings"], json.dumps(self.settings))
        return p

    def install_context(self, native_run):
        context = ExitStack()
        for target, options in [
            ("deploy_mini.require_mac", {}),
            ("deploy_mini.executable", {"side_effect":lambda path:path}),
            ("deploy_mini.find_tunnel_client", {"return_value":self.settings["tunnel_client"]}),
            ("deploy_mini.ensure_key", {"return_value":SECRET}),
            ("deploy_mini.run", {"side_effect":native_run}),
            ("configuration.load_config", {"return_value":{"mode":"read-only"}}),
            ("configuration.config_path", {"return_value":Path(self.settings["bridge_config"])}),
            ("run.runtime_path", {"return_value":Path(self.settings["python"]).parent.parent}),
            ("setup.verify_connection", {}),
        ]:
            context.enter_context(patch(target, **options))
        return context

    def native_profile(self, args, *, secret=False):
        if len(args) > 1 and args[1] == "init":
            candidate = Path(args[args.index("--profile-dir") + 1]) / (deploy.PROFILE + ".yaml")
            candidate.write_text("config_version: 1\ncontrol_plane:\n  api_keyref: env:CONTROL_PLANE_API_KEY\n"
                                 + ("unexpected: " + SECRET if secret else ""))

    def test_id_validation_rejects_secret_url_and_malformed_ids(self):
        self.assertEqual(deploy.checked_id(TUNNEL), TUNNEL)
        self.assertEqual(deploy.checked_id("tunnel_ab12_" + "b" * 32), "tunnel_ab12_" + "b" * 32)
        for value in [SECRET, "https://api.openai.com/" + TUNNEL, "tunnel_ABC", TUNNEL + "\n", TUNNEL + ";bad"]:
            with self.subTest(value=value), self.assertRaises(deploy.DeploymentError):
                deploy.checked_id(value)

    def test_runtime_key_uses_shared_secure_store(self):
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(deploy, "prompt_and_store", return_value=SECRET) as store:
            self.assertEqual(deploy.ensure_key(TUNNEL, replace=True), SECRET)
        args = store.call_args.args
        self.assertEqual(args[:2], (deploy.KEYCHAIN_SERVICE, TUNNEL))
        validator = store.call_args.kwargs["validator"]
        self.assertTrue(validator("sk-" + "x" * 200))
        self.assertFalse(validator("sk-" + "x" * 200 + "\n"))
        self.assertFalse(validator("invalid"))

    def test_runtime_key_safe_store_failure_is_reported(self):
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(deploy, "prompt_and_store", side_effect=deploy.SecretInputError("safe failure")), \
             self.assertRaisesRegex(deploy.DeploymentError, "safe failure"):
            deploy.ensure_key(TUNNEL, replace=True)

    def test_plist_contains_paths_and_never_credentials(self):
        data = deploy.plist_data(self.settings)
        encoded = plistlib.dumps(data)
        self.assertNotIn(SECRET.encode(), encoded)
        self.assertNotIn(b"CONTROL_PLANE_API_KEY", encoded)
        self.assertEqual(data["ProgramArguments"], [self.settings["python"], str(ROOT / "deploy_mini.py"), "serve"])
        self.assertTrue(data["KeepAlive"])
        self.assertTrue(data["RunAtLoad"])

    def test_runtime_env_drops_admin_and_unrelated_configuration(self):
        with patch.dict(os.environ, {"OPENAI_ADMIN_KEY": "admin", "OPENAI_API_KEY": "other",
                                     "MCP_COMMAND": "untrusted-command", "CONTROL_PLANE_BASE_URL": "https://other.invalid"}):
            env = deploy.runtime_environment(self.settings, SECRET)
        self.assertEqual(env["CONTROL_PLANE_API_KEY"], SECRET)
        self.assertEqual(env["PROTON_MAIL_BRIDGE_CONFIG"], self.settings["bridge_config"])
        for key in ["OPENAI_ADMIN_KEY", "OPENAI_API_KEY", "MCP_COMMAND", "CONTROL_PLANE_BASE_URL"]:
            self.assertNotIn(key, env)

    def test_stable_homebrew_opt_path_is_preserved(self):
        cellar = self.home / "Cellar/tunnel-client/1.0/bin"
        cellar.mkdir(parents=True)
        binary = cellar / "tunnel-client"
        binary.write_text("test fixture")
        binary.chmod(0o700)
        opt = self.home / "opt/tunnel-client"
        opt.parent.mkdir(parents=True)
        opt.symlink_to(cellar.parent, target_is_directory=True)
        with patch.object(deploy.shutil, "which", return_value="/opt/homebrew/bin/brew"), \
             patch.object(deploy, "run", return_value=SimpleResult(stdout=str(opt) + "\n")):
            self.assertEqual(deploy.find_tunnel_client(), str(opt / "bin/tunnel-client"))

    def test_private_config_rejects_symlinks_and_broad_permissions(self):
        p = self.save_settings()
        p["settings"].chmod(0o644)
        with self.assertRaises(deploy.DeploymentError):
            deploy.load_settings()
        p["settings"].unlink()
        target = self.home / "other.json"
        target.write_text(json.dumps(self.settings))
        p["settings"].symlink_to(target)
        with self.assertRaises(deploy.DeploymentError):
            deploy.load_settings()

    def test_status_uses_native_health_with_pid_and_poll_gate_without_keychain(self):
        p = self.save_settings()
        deploy.atomic_write(p["health"], "http://127.0.0.1:43210\n")
        response = {"result":"fail", "process":{"pid":4321,"running":True},
                    "healthz":{"ok":True}, "readyz":{"ok":True}, "control_plane_poll":{"ok":False}}
        with patch.object(deploy, "service_state", return_value={"loaded":True, "pid":4321}), \
             patch.object(deploy, "run", return_value=SimpleResult(stdout=json.dumps(response))) as command, \
             patch.object(deploy, "keychain_key") as keychain:
            status = deploy.status_data()
        self.assertTrue(status["healthy"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["control_plane_poll_seen"])
        self.assertFalse(status["remote_mail_verified"])
        self.assertEqual(status["tunnel_id"], TUNNEL)
        self.assertIn("--require-control-plane-poll", command.call_args.args[0])
        self.assertIn("--pid", command.call_args.args[0])
        keychain.assert_not_called()

    def test_stale_process_cannot_be_reported_healthy_from_reused_health_port(self):
        p = self.save_settings()
        deploy.atomic_write(p["health"], "http://127.0.0.1:43210\n")
        response = {"result":"fail", "process":{"pid":4321,"running":False},
                    "healthz":{"ok":True}, "readyz":{"ok":True}, "control_plane_poll":{"ok":True}}
        with patch.object(deploy, "service_state", return_value={"loaded":True, "pid":4321}), \
             patch.object(deploy, "run", return_value=SimpleResult(returncode=2, stdout=json.dumps(response))):
            status = deploy.status_data()
        self.assertFalse(status["process_running"])
        self.assertFalse(status["healthy"])
        self.assertFalse(status["ready"])

    def test_status_will_not_probe_nonlocal_or_credentialed_health_url(self):
        p = self.save_settings()
        for value in ["https://attacker.invalid", "http://127.0.0.1:9@attacker.invalid", "http://127.0.0.1:8080/?secret=1"]:
            deploy.atomic_write(p["health"], value)
            with patch.object(deploy, "run") as command:
                data = deploy.health_status(self.settings, {"loaded":True, "pid":4321})
            self.assertFalse(data["healthy"])
            command.assert_not_called()

    def test_serve_passes_key_only_in_env_and_execs_foreground_native_client(self):
        p = self.save_settings()
        deploy.atomic_write(p["profile"], "config_version: 1\n")
        with patch.object(deploy, "require_mac"), patch.object(deploy, "executable", side_effect=lambda x:x), \
             patch.object(deploy, "keychain_key", return_value=SECRET), \
             patch.object(deploy.os, "chdir"), patch.object(deploy.os, "umask"), \
             patch.object(deploy.os, "execve") as execute:
            deploy.serve()
        binary, argv, env = execute.call_args.args
        self.assertEqual(binary, self.settings["tunnel_client"])
        self.assertEqual(argv[1], "run")
        self.assertNotIn("--mcp.stdio-failure-policy=exit", argv)
        self.assertNotIn(SECRET, argv)
        self.assertEqual(env["CONTROL_PLANE_API_KEY"], SECRET)

    def test_stop_waits_for_launchd_to_remove_service(self):
        with patch.object(deploy, "service_state", side_effect=[
            {"loaded":True,"pid":None}, {"loaded":True,"pid":None}, {"loaded":False,"pid":None}
        ]), patch.object(deploy, "run") as command, patch.object(deploy.time, "sleep") as pause:
            deploy.stop_service()
        self.assertEqual(command.call_args.args[0][1], "bootout")
        pause.assert_called_once_with(0.1)

    def test_service_state_rejects_unexpected_launchctl_error(self):
        with patch.object(deploy, "run", return_value=SimpleResult(returncode=1, stderr="unexpected error")), \
             self.assertRaisesRegex(deploy.DeploymentError, "Could not verify"):
            deploy.service_state()

    def test_stop_fails_if_launchd_never_removes_service(self):
        with patch.object(deploy, "service_state", return_value={"loaded":True,"pid":None}), \
             patch.object(deploy, "run"), patch.object(deploy.time, "monotonic", side_effect=[0, 11]), \
             self.assertRaisesRegex(deploy.DeploymentError, "did not stop"):
            deploy.stop_service()

    def test_stop_waits_for_old_process_before_returning(self):
        with patch.object(deploy, "service_state", side_effect=[{"loaded":True,"pid":123}, {"loaded":False,"pid":None}]), \
             patch.object(deploy, "run") as command, \
             patch.object(deploy, "process_alive", side_effect=[True,False]) as alive, \
             patch.object(deploy.time, "sleep") as pause:
            deploy.stop_service()
        self.assertEqual(command.call_args.args[0][1], "bootout")
        self.assertEqual(alive.call_count, 2)
        pause.assert_called_once_with(0.1)

    def test_stop_disables_next_login_before_unloading(self):
        events = []
        with patch.object(sys, "argv", ["deploy_mini.py", "stop"]), \
             patch.object(deploy, "require_mac"), patch("builtins.print"), \
             patch.object(deploy, "run", side_effect=lambda argv:events.append(argv[1])), \
             patch.object(deploy, "stop_service", side_effect=lambda:events.append("stop")):
            self.assertEqual(deploy.main(), 0)
        self.assertEqual(events, ["disable", "stop"])

    def test_failed_profile_generation_or_doctor_preserves_previous_deployment(self):
        p = self.save_settings()
        p["plist"].parent.mkdir(parents=True)
        deploy.atomic_write(p["plist"], plistlib.dumps({"Comment":deploy.MARKER}))
        deploy.atomic_write(p["profile"], "old valid profile\n")
        previous = {name:p[name].read_bytes() for name in ("settings", "profile", "plist")}
        args = SimpleNamespace(tunnel_id=TUNNEL, tunnel_client=None, replace_key=False)
        for failure in ("init", "doctor", "secret_in_profile"):
            def native_run(argv, **kwargs):
                if argv[1] == failure:
                    raise deploy.DeploymentError("simulated native failure")
                self.native_profile(argv, secret=failure == "secret_in_profile")
                return SimpleResult()
            with self.subTest(failure=failure), self.install_context(native_run), \
                 patch.object(deploy, "stop_service") as stop, self.assertRaises(deploy.DeploymentError):
                deploy.install(args)
            stop.assert_not_called()
            for name, content in previous.items():
                self.assertEqual(p[name].read_bytes(), content)
            self.assertFalse(p["handoff"].exists())

    def test_install_rejects_malformed_key_before_creating_profile(self):
        args = SimpleNamespace(tunnel_id=TUNNEL, tunnel_client=None, replace_key=False)
        with self.install_context(lambda *args, **kwargs: SimpleResult()), \
             patch.object(deploy, "ensure_key", return_value="x"), \
             patch.object(deploy, "stop_service") as stop, \
             self.assertRaisesRegex(deploy.DeploymentError, "--replace-key"):
            deploy.install(args)
        stop.assert_not_called()
        self.assertFalse(deploy.paths()["profile"].exists())

    def test_install_artifacts_are_private_and_contain_no_runtime_key(self):
        commands = []
        def native_run(argv, **kwargs):
            commands.append(argv)
            self.assertNotIn(SECRET, argv)
            self.native_profile(argv)
            return SimpleResult()
        args = SimpleNamespace(tunnel_id=TUNNEL, tunnel_client=None, replace_key=False)
        status = {"installed":True, "tunnel_id":TUNNEL, "remote_mail_verified":False}
        with self.install_context(native_run), patch.object(deploy, "stop_service"), \
             patch.object(deploy, "status_data", return_value=status), patch("builtins.print"):
            self.assertEqual(deploy.install(args), 0)
        p = deploy.paths()
        for name in ("settings", "profile", "plist", "handoff", "log"):
            self.assertNotIn(SECRET.encode(), p[name].read_bytes())
            self.assertEqual(p[name].stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(p["handoff"].read_text())["tunnel_id"], TUNNEL)
        native_init = next(argv for argv in commands if argv[1] == "init")
        import shlex
        self.assertEqual(shlex.split(native_init[native_init.index("--mcp-command") + 1]),
                         [self.settings["python"], str(ROOT / "run.py")])
        launch_commands = [argv[1] for argv in commands if argv[0] == "/bin/launchctl"]
        self.assertEqual(launch_commands, ["enable", "bootstrap"])

    def test_mail_launcher_removes_inherited_tunnel_secrets(self):
        runtime = self.home / "runtime"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin/python").touch()
        with patch.object(launcher, "runtime_path", return_value=runtime), \
             patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": SECRET, "OPENAI_ADMIN_KEY": "admin", "OPENAI_API_KEY": "other"}), \
             patch.object(launcher.os, "execve") as execute:
            launcher.main()
        child_env = execute.call_args.args[2]
        for key in ["CONTROL_PLANE_API_KEY", "OPENAI_ADMIN_KEY", "OPENAI_API_KEY"]:
            self.assertNotIn(key, child_env)


class SimpleResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


if __name__ == "__main__":
    unittest.main()

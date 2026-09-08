import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import codex_runner


class RunnerEnvironmentTests(unittest.TestCase):
    def test_child_keeps_supported_connection_settings_without_parent_secrets(self):
        settings = {
            "CODEX_HOME": str(Path.cwd() / "synthetic-home"),
            "CODEX_CA_CERTIFICATE": str(Path.cwd() / "synthetic-ca.pem"),
            "SSL_CERT_FILE": str(Path.cwd() / "synthetic-fallback.pem"),
        }
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            value = "localhost,127.0.0.1" if name == "NO_PROXY" else "http://proxy.invalid:8080"
            settings[name] = settings[name.lower()] = value
        secret_name = "SYNTHETIC_PARENT_SECRET"
        names = [*settings, secret_name]
        child = (
            "import json, os; from pathlib import Path; "
            "Path('last-message.md').write_text(json.dumps("
            "{key: os.environ.get(key) for key in " + repr(names) + "}"
            "), encoding='utf-8')"
        )
        with (
            mock.patch.dict("os.environ", {**settings, secret_name: "must-not-cross"}),
            mock.patch.object(codex_runner, "find_codex", return_value=sys.executable),
            mock.patch.object(codex_runner, "_exec_argv", return_value=["-c", child]),
        ):
            output, error = codex_runner.run_exec(
                "synthetic prompt", sandbox="read-only", timeout=10,
            )
        self.assertIsNone(error)
        observed = json.loads(output)
        self.assertEqual(observed, {**settings, secret_name: None})

    def test_relative_home_and_ca_paths_keep_the_calling_directory(self):
        names = ("CODEX_HOME", "CODEX_CA_CERTIFICATE", "SSL_CERT_FILE")
        with mock.patch.dict("os.environ", {name: "relative-setting" for name in names}):
            environment = codex_runner._child_environment()
        for name in names:
            self.assertEqual(environment.get(name), str(Path.cwd() / "relative-setting"))


if __name__ == "__main__":
    unittest.main()

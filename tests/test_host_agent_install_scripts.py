import importlib.util
import os
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "apps/api/app.py"
SPEC = importlib.util.spec_from_file_location("argus_app_install_scripts", APP_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load module from {APP_PATH}")
argus_app = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("argus_app_install_scripts", argus_app)
SPEC.loader.exec_module(argus_app)


class HostAgentInstallScriptTests(unittest.TestCase):
    def test_shell_installer_installs_system_argus_command(self) -> None:
        with mock.patch.dict(os.environ, {"ARGUS_PUBLIC_BASE_URL": "https://argus.example.com"}, clear=True):
            script = argus_app._render_host_agent_install_sh("argus-host-enroll-v1.tid.secret")

        self.assertIn('linux:x86_64|linux:amd64)', script)
        self.assertIn('ARGUS_LINK_PATH="${ARGUS_LINK_PATH:-/usr/local/bin/argus}"', script)
        self.assertIn('ln -sf "$BIN_PATH" "$ARGUS_LINK_PATH"', script)
        self.assertIn('exec "$ARGUS_CMD" connect --gateway "$ARGUS_GATEWAY_BASE"', script)

    def test_powershell_installer_adds_argus_bin_to_user_path(self) -> None:
        with mock.patch.dict(os.environ, {"ARGUS_PUBLIC_BASE_URL": "https://argus.example.com"}, clear=True):
            script = argus_app._render_host_agent_install_ps1("argus-host-enroll-v1.tid.secret")

        self.assertIn('[Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")', script)
        self.assertIn('$env:Path = "$BinDir;$env:Path"', script)
        self.assertIn("& $BinPath connect --gateway $GatewayBaseUrl", script)


if __name__ == "__main__":
    unittest.main()

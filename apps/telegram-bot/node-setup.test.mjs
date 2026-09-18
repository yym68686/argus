import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { buildNodeInstallAndConnectCommand } from "./index.mjs";

const trickyToken = "token ' with $dollar $(touch INJECTED) `tick` ; & end";
const wsUrl = `wss://example.com/nodes/ws?token=${encodeURIComponent(trickyToken)}`;

for (const failure of [false, true]) {
  test(`POSIX install-and-connect ${failure ? "stops after download failure" : "uses the fresh install and preserves literal arguments"}`, () => {
    const dir = mkdtempSync(join(tmpdir(), "argus-setup-"));
    try {
      const binDir = join(dir, "bin with spaces");
      // The mock installer accepts the same installation directory as Argus.
      const installer = `mkdir -p "$ARGUS_BIN_DIR"\ncat > "$ARGUS_BIN_DIR/argus" <<'EOF'\n#!/bin/sh\nprintf '%s\\n' "$@" > "$ARGUS_TEST_OUTPUT"\nEOF\nchmod +x "$ARGUS_BIN_DIR/argus"\n`;
      const script = join(dir, "installer.sh");
      writeFileSync(script, installer);
      writeFileSync(join(dir, "curl"), failure ? "#!/bin/sh\nexit 22\n" : "#!/bin/sh\ncat \"$ARGUS_TEST_INSTALLER\"\n", { mode: 0o755 });
      const output = join(dir, "args");
      const result = spawnSync("bash", ["-c", buildNodeInstallAndConnectCommand(wsUrl)], {
        cwd: dir, encoding: "utf8",
        env: { ...process.env, PATH: `${dir}:/usr/bin:/bin`, ARGUS_BIN_DIR: binDir, ARGUS_TEST_OUTPUT: output, ARGUS_TEST_INSTALLER: script }
      });
      assert.equal(result.status === 0, !failure, result.stderr);
      assert.equal(existsSync(output), !failure);
      if (!failure) assert.deepEqual(readFileSync(output, "utf8").trimEnd().split("\n"), ["--gateway", "https://example.com", "--token", trickyToken]);
      assert.equal(existsSync(join(dir, "INJECTED")), false);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
}

const pwsh = process.env.ARGUS_TEST_PWSH || "pwsh";
const hasPowerShell = spawnSync(pwsh, ["-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"], { encoding: "utf8" }).status === 0;
for (const failure of [false, "download", "health"]) {
  test(`PowerShell install-and-connect ${failure ? `stops on ${failure} failure` : "returns from installer and preserves literal arguments"}`, { skip: !hasPowerShell }, () => {
    const dir = mkdtempSync(join(tmpdir(), "argus-ps-setup-"));
    try {
      const output = join(dir, "args.json");
      const harness = join(dir, "harness.ps1");
      const command = buildNodeInstallAndConnectCommand(wsUrl, "windows");
      // No network or real installation: execute the real installer with an
      // in-memory drive and mock native binary, retaining its control flow.
      writeFileSync(harness, `
$ErrorActionPreference = 'Stop'
function Invoke-RestMethod {
  if ($env:ARGUS_TEST_DOWNLOAD_FAILURE -eq '1') { throw 'Download failed' }
  Get-Content -Raw -LiteralPath $env:ARGUS_TEST_INSTALLER
}
function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing)
  if ($OutFile) { Set-Content -LiteralPath $OutFile -Value 'test fixture' }
  else { [pscustomobject]@{Content='0.0.test'} }
}
# Alias the exact exe path so the real installer's help check is controlled.
function Test-ArgusHealth { $global:LASTEXITCODE = [int]$env:ARGUS_TEST_EXIT }
$healthPath = Join-Path $env:ARGUS_BIN_DIR 'argus.exe'
Set-Alias -Name $healthPath -Value Test-ArgusHealth
function argus { ConvertTo-Json -InputObject @($args) -Compress | Set-Content -LiteralPath $env:ARGUS_TEST_OUTPUT }
${command}
`);
      const result = spawnSync(pwsh, ["-NoProfile", "-NonInteractive", "-File", harness], {
        cwd: dir, encoding: "utf8",
        env: { ...process.env, PROCESSOR_ARCHITECTURE: "AMD64", ARGUS_BIN_DIR: join(dir, "bin with spaces"), ARGUS_VERSION: "0.0.test", ARGUS_TEST_INSTALLER: resolve(import.meta.dirname, "../../scripts/install-argus.ps1"), ARGUS_TEST_EXIT: failure === "health" ? "7" : "0", ARGUS_TEST_DOWNLOAD_FAILURE: failure === "download" ? "1" : "0", ARGUS_TEST_OUTPUT: output }
      });
      assert.equal(result.status === 0, !failure, result.stderr);
      assert.equal(existsSync(output), !failure);
      if (!failure) assert.deepEqual(JSON.parse(readFileSync(output, "utf8")), ["--gateway", "https://example.com", "--token", trickyToken]);
      assert.equal(existsSync(join(dir, "INJECTED")), false);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
}

test("install-and-connect preserves custom URL paths and rejects empty URLs", () => {
  const custom = "wss://example.com/custom/ws?token=abc&session=123";
  assert.match(buildNodeInstallAndConnectCommand(custom), /--url/);
  assert.ok(buildNodeInstallAndConnectCommand(custom, "windows").includes(`'${custom}'`));
  assert.equal(buildNodeInstallAndConnectCommand(""), null);
  assert.equal(buildNodeInstallAndConnectCommand("", "windows"), null);
});

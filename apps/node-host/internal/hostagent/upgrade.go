package hostagent

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

func CurrentBinaryPath() (string, error) {
	exePath, err := os.Executable()
	if err != nil {
		return "", err
	}
	exePath = strings.TrimSpace(exePath)
	if exePath == "" {
		return "", fmt.Errorf("unable to resolve current executable")
	}
	resolved, err := filepath.EvalSymlinks(exePath)
	if err == nil && strings.TrimSpace(resolved) != "" {
		return resolved, nil
	}
	return exePath, nil
}

func CurrentDownloadTarget() (string, error) {
	switch runtime.GOOS {
	case "darwin":
		switch runtime.GOARCH {
		case "arm64":
			return "darwin-arm64", nil
		case "amd64":
			return "darwin-amd64", nil
		}
	case "windows":
		switch runtime.GOARCH {
		case "arm64":
			return "windows-arm64", nil
		case "amd64":
			return "windows-amd64", nil
		}
	case "linux":
		switch runtime.GOARCH {
		case "arm64":
			return "linux-arm64", nil
		case "amd64":
			return "linux-amd64", nil
		}
	}
	return "", fmt.Errorf("unsupported platform: %s/%s", runtime.GOOS, runtime.GOARCH)
}

func DownloadURL(gatewayBaseURL string) (string, error) {
	base := strings.TrimRight(strings.TrimSpace(gatewayBaseURL), "/")
	if base == "" {
		return "", fmt.Errorf("missing gateway base URL")
	}
	parsed, err := url.Parse(base)
	if err != nil {
		return "", err
	}
	target, err := CurrentDownloadTarget()
	if err != nil {
		return "", err
	}
	parsed.Path = strings.TrimRight(parsed.Path, "/") + "/host-agent/download/" + target
	parsed.RawQuery = ""
	return parsed.String(), nil
}

func UpgradeBinary(gatewayBaseURL string) (string, error) {
	downloadURL, err := DownloadURL(gatewayBaseURL)
	if err != nil {
		return "", err
	}
	exePath, err := CurrentBinaryPath()
	if err != nil {
		return "", err
	}
	tmpPath, mode, err := downloadToSiblingTemp(downloadURL, exePath)
	if err != nil {
		return "", err
	}
	if runtime.GOOS == "windows" {
		if err := scheduleWindowsReplace(exePath, tmpPath); err != nil {
			_ = os.Remove(tmpPath)
			return "", err
		}
		return exePath, nil
	}
	if err := os.Chmod(tmpPath, mode); err != nil {
		_ = os.Remove(tmpPath)
		return "", err
	}
	if err := os.Rename(tmpPath, exePath); err != nil {
		_ = os.Remove(tmpPath)
		return "", err
	}
	return exePath, nil
}

func downloadToSiblingTemp(downloadURL string, exePath string) (string, os.FileMode, error) {
	info, err := os.Stat(exePath)
	if err != nil {
		return "", 0, err
	}
	mode := info.Mode() & os.ModePerm
	if mode == 0 {
		mode = 0o755
	}
	dir := filepath.Dir(exePath)
	pattern := filepath.Base(exePath) + ".upgrade-*"
	if runtime.GOOS == "windows" && !strings.HasSuffix(strings.ToLower(pattern), ".exe") {
		pattern += ".exe"
	}
	tmpFile, err := os.CreateTemp(dir, pattern)
	if err != nil {
		return "", 0, err
	}
	tmpPath := tmpFile.Name()
	success := false
	defer func() {
		_ = tmpFile.Close()
		if !success {
			_ = os.Remove(tmpPath)
		}
	}()

	client := &http.Client{Timeout: 2 * time.Minute}
	resp, err := client.Get(downloadURL)
	if err != nil {
		return "", 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", 0, fmt.Errorf("download failed with status %d", resp.StatusCode)
	}
	if _, err := io.Copy(tmpFile, resp.Body); err != nil {
		return "", 0, err
	}
	if err := tmpFile.Sync(); err != nil {
		return "", 0, err
	}
	if err := tmpFile.Close(); err != nil {
		return "", 0, err
	}
	success = true
	return tmpPath, mode, nil
}

func scheduleWindowsReplace(targetPath string, tmpPath string) error {
	helperPath, err := writeWindowsUpgradeHelper(targetPath, tmpPath)
	if err != nil {
		return err
	}
	shell, err := findWindowsShell()
	if err != nil {
		_ = os.Remove(helperPath)
		return err
	}
	cmd := exec.Command(shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helperPath)
	if err := cmd.Start(); err != nil {
		_ = os.Remove(helperPath)
		return err
	}
	return nil
}

func writeWindowsUpgradeHelper(targetPath string, tmpPath string) (string, error) {
	scriptFile, err := os.CreateTemp("", "argus-upgrade-*.ps1")
	if err != nil {
		return "", err
	}
	scriptPath := scriptFile.Name()
	if err := scriptFile.Close(); err != nil {
		_ = os.Remove(scriptPath)
		return "", err
	}
	content := fmt.Sprintf(`$ErrorActionPreference = "Stop"
$TargetPath = '%s'
$TempPath = '%s'
for ($i = 0; $i -lt 80; $i++) {
  Start-Sleep -Milliseconds 250
  try {
    Copy-Item -Force $TempPath $TargetPath
    Remove-Item -Force $TempPath -ErrorAction SilentlyContinue
    Remove-Item -Force $PSCommandPath -ErrorAction SilentlyContinue
    exit 0
  } catch {
    if ($i -ge 79) { throw }
  }
}
`, escapePowerShellString(targetPath), escapePowerShellString(tmpPath))
	if err := os.WriteFile(scriptPath, []byte(content), 0o600); err != nil {
		_ = os.Remove(scriptPath)
		return "", err
	}
	return scriptPath, nil
}

func escapePowerShellString(raw string) string {
	return strings.ReplaceAll(raw, "'", "''")
}

func findWindowsShell() (string, error) {
	for _, candidate := range []string{"powershell.exe", "pwsh.exe"} {
		if path, err := exec.LookPath(candidate); err == nil && strings.TrimSpace(path) != "" {
			return path, nil
		}
	}
	return "", fmt.Errorf("PowerShell is not available")
}

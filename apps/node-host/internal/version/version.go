package version

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
)

const fallbackVersion = "0.1.1"

var (
	currentVersion string
	loadOnce       sync.Once
)

func Current() string {
	loadOnce.Do(func() {
		currentVersion = detect()
	})
	return currentVersion
}

func UserAgent() string {
	return "argus/" + Current()
}

func detect() string {
	if v := normalize(os.Getenv("ARGUS_VERSION")); v != "" {
		return v
	}
	if exe, err := os.Executable(); err == nil {
		if v := searchForVersion(filepath.Dir(exe), 6); v != "" {
			return v
		}
	}
	if wd, err := os.Getwd(); err == nil {
		if v := searchForVersion(wd, 6); v != "" {
			return v
		}
	}
	return fallbackVersion
}

func searchForVersion(start string, maxParents int) string {
	dir := filepath.Clean(start)
	for i := 0; i <= maxParents; i++ {
		if v := readVersionFile(filepath.Join(dir, "VERSION")); v != "" {
			return v
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return ""
}

func readVersionFile(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return normalize(string(raw))
}

func normalize(raw string) string {
	return strings.TrimSpace(raw)
}

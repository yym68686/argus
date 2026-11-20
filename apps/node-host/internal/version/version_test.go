package version

import "testing"

func TestDetectBuildVersion(t *testing.T) {
	previous := buildVersion
	t.Cleanup(func() { buildVersion = previous })
	buildVersion = "9.8.7"
	t.Setenv("ARGUS_VERSION", "")
	if got := detect(); got != "9.8.7" {
		t.Fatalf("detect() = %q, want embedded version", got)
	}
	t.Setenv("ARGUS_VERSION", "1.2.3")
	if got := detect(); got != "1.2.3" {
		t.Fatalf("detect() = %q, want explicit override", got)
	}
}

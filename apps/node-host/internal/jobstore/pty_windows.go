//go:build windows

package jobstore

import (
	"fmt"
	"os"
	"os/exec"
)

func startPTYCommand(argv []string, cwd *string, env map[string]string, cols int, rows int) (*exec.Cmd, *os.File, error) {
	return nil, nil, fmt.Errorf("pty is not supported on windows nodes")
}

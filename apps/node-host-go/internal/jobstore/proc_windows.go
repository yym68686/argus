//go:build windows

package jobstore

import (
	"os"
)

func sendTerminate(p *os.Process) error {
	if p == nil {
		return nil
	}
	return p.Signal(os.Interrupt)
}

func exitInfo(ps *os.ProcessState, waitErr error) (exitCode *int, signal *string) {
	if ps == nil {
		return nil, nil
	}
	c := ps.ExitCode()
	if c >= 0 {
		return &c, nil
	}
	return nil, nil
}

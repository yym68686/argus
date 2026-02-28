//go:build !windows

package jobstore

import (
	"fmt"
	"os"
	"syscall"
)

func sendTerminate(p *os.Process) error {
	if p == nil {
		return nil
	}
	return p.Signal(syscall.SIGTERM)
}

func exitInfo(ps *os.ProcessState, waitErr error) (exitCode *int, signal *string) {
	if ps == nil {
		return nil, nil
	}
	if st, ok := ps.Sys().(syscall.WaitStatus); ok {
		if st.Signaled() {
			s := signalName(st.Signal())
			return nil, &s
		}
		if st.Exited() {
			c := st.ExitStatus()
			return &c, nil
		}
	}
	c := ps.ExitCode()
	if c >= 0 {
		return &c, nil
	}
	if waitErr != nil {
		s := fmt.Sprintf("SIG(%s)", waitErr.Error())
		return nil, &s
	}
	return nil, nil
}

func signalName(sig syscall.Signal) string {
	switch sig {
	case syscall.SIGKILL:
		return "SIGKILL"
	case syscall.SIGTERM:
		return "SIGTERM"
	case syscall.SIGINT:
		return "SIGINT"
	case syscall.SIGQUIT:
		return "SIGQUIT"
	case syscall.SIGHUP:
		return "SIGHUP"
	case syscall.SIGABRT:
		return "SIGABRT"
	case syscall.SIGPIPE:
		return "SIGPIPE"
	default:
		return fmt.Sprintf("SIG%d", int(sig))
	}
}

//go:build linux || darwin

package jobstore

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"unsafe"
)

type ptyPair struct {
	master *os.File
	slave  *os.File
}

type winsize struct {
	Row    uint16
	Col    uint16
	Xpixel uint16
	Ypixel uint16
}

func startPTYCommand(argv []string, cwd *string, env map[string]string, cols int, rows int) (*exec.Cmd, *os.File, error) {
	if len(argv) == 0 {
		return nil, nil, fmt.Errorf("pty argv required")
	}

	pair, err := openPTYPair(cols, rows)
	if err != nil {
		return nil, nil, err
	}

	cmd := exec.Command(argv[0], argv[1:]...)
	if cwd != nil && strings.TrimSpace(*cwd) != "" {
		cmd.Dir = strings.TrimSpace(*cwd)
	}
	if env != nil {
		merged := os.Environ()
		for k, v := range env {
			if strings.TrimSpace(k) == "" {
				continue
			}
			merged = append(merged, fmt.Sprintf("%s=%s", k, v))
		}
		cmd.Env = merged
	}

	cmd.Stdin = pair.slave
	cmd.Stdout = pair.slave
	cmd.Stderr = pair.slave
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setsid:  true,
		Setctty: true,
		Ctty:    0,
	}

	if err := cmd.Start(); err != nil {
		_ = pair.master.Close()
		_ = pair.slave.Close()
		return nil, nil, err
	}
	_ = pair.slave.Close()
	return cmd, pair.master, nil
}

func ioctlNoArg(fd int, req uintptr) error {
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), req, 0)
	if errno != 0 {
		return errno
	}
	return nil
}

func setPTYWinsize(fd int, cols int, rows int) error {
	if cols <= 0 {
		cols = 120
	}
	if rows <= 0 {
		rows = 30
	}
	ws := winsize{Col: uint16(cols), Row: uint16(rows)}
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), uintptr(syscall.TIOCSWINSZ), uintptr(unsafe.Pointer(&ws)))
	if errno != 0 {
		return errno
	}
	return nil
}

//go:build darwin

package jobstore

import (
	"bytes"
	"fmt"
	"os"
	"syscall"
	"unsafe"
)

func openPTYPair(cols int, rows int) (*ptyPair, error) {
	mfd, err := syscall.Open("/dev/ptmx", syscall.O_RDWR|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	closeMaster := true
	defer func() {
		if closeMaster {
			_ = syscall.Close(mfd)
		}
	}()

	if err := ioctlNoArg(mfd, uintptr(syscall.TIOCPTYGRANT)); err != nil {
		return nil, err
	}
	if err := ioctlNoArg(mfd, uintptr(syscall.TIOCPTYUNLK)); err != nil {
		return nil, err
	}

	nameBuf := make([]byte, 128)
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(mfd), uintptr(syscall.TIOCPTYGNAME), uintptr(unsafe.Pointer(&nameBuf[0])))
	if errno != 0 {
		return nil, errno
	}
	nameBuf = nameBuf[:bytes.IndexByte(nameBuf, 0)]
	if len(nameBuf) == 0 {
		return nil, fmt.Errorf("pty slave name missing")
	}

	slaveName := string(nameBuf)
	sfd, err := syscall.Open(slaveName, syscall.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		return nil, err
	}
	if err := setPTYWinsize(sfd, cols, rows); err != nil {
		_ = syscall.Close(sfd)
		return nil, err
	}

	closeMaster = false
	return &ptyPair{
		master: os.NewFile(uintptr(mfd), "/dev/ptmx"),
		slave:  os.NewFile(uintptr(sfd), slaveName),
	}, nil
}

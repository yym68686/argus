//go:build linux

package jobstore

import (
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

	unlock := int32(0)
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(mfd), uintptr(syscall.TIOCSPTLCK), uintptr(unsafe.Pointer(&unlock)))
	if errno != 0 {
		return nil, errno
	}

	var num uint32
	_, _, errno = syscall.Syscall(syscall.SYS_IOCTL, uintptr(mfd), uintptr(syscall.TIOCGPTN), uintptr(unsafe.Pointer(&num)))
	if errno != 0 {
		return nil, errno
	}

	slaveName := fmt.Sprintf("/dev/pts/%d", num)
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

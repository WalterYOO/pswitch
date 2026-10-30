//go:build darwin

package config

import "syscall"

// maxFilesPerProc 是硬上限为 unlimited 时真正的内核上限。
func maxFilesPerProc() uint64 {
	if v, err := syscall.SysctlUint32("kern.maxfilesperproc"); err == nil && v > 0 {
		return uint64(v)
	}
	return fallbackHardFD
}

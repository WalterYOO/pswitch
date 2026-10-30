//go:build !darwin

package config

// maxFilesPerProc 在非 macOS 上没有对应的内核参数，用兜底值。
func maxFilesPerProc() uint64 {
	return fallbackHardFD
}

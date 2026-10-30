//go:build unix

package config

import (
	"syscall"
	"testing"
)

func TestRaiseFdLimitRaisesSoftLimit(t *testing.T) {
	var orig syscall.Rlimit
	if err := syscall.Getrlimit(syscall.RLIMIT_NOFILE, &orig); err != nil {
		t.Skip("拿不到 rlimit:", err)
	}
	defer func() { _ = syscall.Setrlimit(syscall.RLIMIT_NOFILE, &orig) }()

	// 复现 launchd 默认给的 256 软上限
	low := syscall.Rlimit{Cur: 256, Max: orig.Max}
	if err := syscall.Setrlimit(syscall.RLIMIT_NOFILE, &low); err != nil {
		t.Skip("无法下调软上限:", err)
	}
	if target := DesiredFdLimit(); target <= 256 {
		t.Fatalf("目标值应大于 256，得到 %d", target)
	}
	_, after, changed := RaiseFdLimit()
	if !changed || after <= 256 {
		t.Fatalf("软上限没有抬起来: after=%d changed=%v", after, changed)
	}
}

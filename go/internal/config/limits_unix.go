//go:build unix

package config

import (
	"math"
	"syscall"
)

// 拿不到硬上限时的兜底值，仅用于把硬上限换算成目标值。
const fallbackHardFD = 1 << 16

// DesiredFdLimit 返回 fd 软上限的目标值：硬上限的一半。
func DesiredFdLimit() uint64 {
	var lim syscall.Rlimit
	if err := syscall.Getrlimit(syscall.RLIMIT_NOFILE, &lim); err != nil {
		return 0
	}
	hard := lim.Max
	if hard >= math.MaxInt64 { // RLIM_INFINITY
		hard = maxFilesPerProc()
	}
	return hard / 2
}

// RaiseFdLimit 把 fd 软上限抬到硬上限的一半；真有变化时返回 (旧值, 新值, true)。
//
// launchd 给守护进程的默认软上限只有 256，浏览器连接稍一多就会撞顶：accept 返回
// EMFILE，而 macOS 的 getaddrinfo 在 fd 耗尽后会退化成 EAI_NONAME，direct 模式
// 于是把所有域名请求都回成 502（纯 IP 目标反而正常）。
func RaiseFdLimit() (before, after uint64, changed bool) {
	var lim syscall.Rlimit
	if err := syscall.Getrlimit(syscall.RLIMIT_NOFILE, &lim); err != nil {
		return 0, 0, false
	}
	before = lim.Cur
	target := DesiredFdLimit()
	if target <= before {
		return before, before, false
	}
	lim.Cur = target
	if err := syscall.Setrlimit(syscall.RLIMIT_NOFILE, &lim); err != nil {
		return before, before, false
	}
	return before, target, true
}

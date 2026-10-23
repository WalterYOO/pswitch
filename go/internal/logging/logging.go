// Package logging 提供与 Python 版一致的滚动日志。
package logging

import (
	"fmt"
	"os"
	"sync"
	"time"

	"pswitch/internal/config"
)

const (
	maxBytes   = 1_000_000
	backups    = 3
	timeLayout = "2006-01-02 15:04:05"
)

// Logger 是并发安全、按大小轮转的文件日志。
type Logger struct {
	mu      sync.Mutex
	path    string
	file    *os.File
	size    int64
	debug   bool
	console bool
}

// Setup 打开日志文件；foreground 时同时输出到 stderr。
func Setup(foreground, debug bool) (*Logger, error) {
	if err := os.MkdirAll(config.Home(), 0o755); err != nil {
		return nil, err
	}
	l := &Logger{path: config.LogPath(), debug: debug, console: foreground}
	if err := l.open(); err != nil {
		return nil, err
	}
	return l, nil
}

func (l *Logger) open() error {
	f, err := os.OpenFile(l.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	l.file = f
	if st, err := f.Stat(); err == nil {
		l.size = st.Size()
	}
	return nil
}

// Close 关闭日志文件。
func (l *Logger) Close() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file != nil {
		_ = l.file.Close()
		l.file = nil
	}
}

// rotateLocked 在超过大小上限时轮转 pswitch.log → .1 → .2 → .3。
func (l *Logger) rotateLocked() {
	if l.file != nil {
		_ = l.file.Close()
		l.file = nil
	}
	_ = os.Remove(fmt.Sprintf("%s.%d", l.path, backups))
	for i := backups - 1; i >= 1; i-- {
		old := fmt.Sprintf("%s.%d", l.path, i)
		if _, err := os.Stat(old); err == nil {
			_ = os.Rename(old, fmt.Sprintf("%s.%d", l.path, i+1))
		}
	}
	_ = os.Rename(l.path, l.path+".1")
	l.size = 0
	_ = l.open()
}

func (l *Logger) logf(level, format string, args ...any) {
	if l == nil {
		return
	}
	line := fmt.Sprintf("%s %-7s %s\n",
		time.Now().Format(timeLayout), level, fmt.Sprintf(format, args...))
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file == nil {
		_ = l.open()
	}
	if l.file == nil {
		return
	}
	if l.size+int64(len(line)) > maxBytes {
		l.rotateLocked()
	}
	if l.file != nil {
		if n, err := l.file.WriteString(line); err == nil {
			l.size += int64(n)
		}
	}
	if l.console {
		_, _ = os.Stderr.WriteString(line)
	}
}

// Infof 记录 INFO 日志。
func (l *Logger) Infof(format string, args ...any) { l.logf("INFO", format, args...) }

// Warnf 记录 WARNING 日志。
func (l *Logger) Warnf(format string, args ...any) { l.logf("WARNING", format, args...) }

// Errorf 记录 ERROR 日志。
func (l *Logger) Errorf(format string, args ...any) { l.logf("ERROR", format, args...) }

// Debugf 仅在 --debug 时记录。
func (l *Logger) Debugf(format string, args ...any) {
	if l != nil && l.debug {
		l.logf("DEBUG", format, args...)
	}
}

// IsDebug 返回是否开启调试日志。
func (l *Logger) IsDebug() bool { return l != nil && l.debug }

// Package launchd 提供 macOS 开机自启支持。
package launchd

import (
	"fmt"
	"html"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"runtime"

	"pswitch/internal/config"
)

// Label 返回 launchd 作业名。
func Label() string {
	name := "unknown"
	if u, err := user.Current(); err == nil && u.Username != "" {
		name = u.Username
	}
	return "com." + name + ".pswitch"
}

// PlistPath 返回 plist 路径。
func PlistPath() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		home = "."
	}
	return filepath.Join(home, "Library", "LaunchAgents", Label()+".plist")
}

// Installed 判断 plist 是否存在（仅 macOS）。
func Installed() bool {
	if runtime.GOOS != "darwin" {
		return false
	}
	_, err := os.Stat(PlistPath())
	return err == nil
}

func plistText(exe string) string {
	log := html.EscapeString(config.LogPath())
	return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array>
    <string>%s</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict>
</plist>
`, html.EscapeString(Label()), html.EscapeString(exe), log, log)
}

// WritePlist 写入 plist，ProgramArguments 指向当前可执行文件。
func WritePlist(exe string) error {
	path := PlistPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, []byte(plistText(exe)), 0o644)
}

func launchctl(args ...string) (string, error) {
	out, err := exec.Command("launchctl", args...).CombinedOutput()
	return string(out), err
}

// Bootstrap 把 plist 交给 launchd。
func Bootstrap() (string, error) {
	return launchctl("bootstrap", fmt.Sprintf("gui/%d", os.Getuid()), PlistPath())
}

// Bootout 从 launchd 摘除作业。
func Bootout() (string, error) {
	return launchctl("bootout", fmt.Sprintf("gui/%d", os.Getuid()), PlistPath())
}

// RemovePlist 删除 plist。
func RemovePlist() {
	_ = os.Remove(PlistPath())
}

package completion

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNormalize(t *testing.T) {
	ok := map[string]string{
		"zsh":                    "zsh",
		"/bin/zsh":               "zsh",
		"-zsh":                   "zsh",
		"bash":                   "bash",
		"/opt/homebrew/bin/fish": "fish",
	}
	for input, want := range ok {
		got, matched := normalize(input)
		if !matched || got != want {
			t.Errorf("normalize(%q) = %q, %v；期望 %q", input, got, matched, want)
		}
	}
	for _, bad := range []string{"", "ksh", "/bin/sh"} {
		if got, matched := normalize(bad); matched {
			t.Errorf("normalize(%q) 不应匹配，得到 %q", bad, got)
		}
	}
}

func TestScriptsListEveryCommand(t *testing.T) {
	// serve 是隐藏的调试命令，不进入补全
	commands := []string{
		"start", "stop", "restart", "status", "use", "list",
		"add", "remove", "priority", "enable", "disable", "env", "test",
		"logs", "install", "uninstall", "system", "lan", "completion", "version",
	}
	for _, shell := range shells {
		for _, command := range commands {
			if !strings.Contains(scripts[shell], command) {
				t.Errorf("%s 补全脚本缺少命令 %q", shell, command)
			}
		}
	}
}

func TestSetBlockIsIdempotent(t *testing.T) {
	dir := t.TempDir()
	rc := filepath.Join(dir, ".zshrc")
	if err := os.WriteFile(rc, []byte("export FOO=1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	blk := block("zsh", filepath.Join(dir, "completions", "_pswitch"))

	changed, err := setBlock(rc, blk)
	if err != nil || !changed {
		t.Fatalf("首次写入应改动: changed=%v err=%v", changed, err)
	}
	changed, err = setBlock(rc, blk)
	if err != nil || changed {
		t.Fatalf("重复写入不应改动: changed=%v err=%v", changed, err)
	}

	data, err := os.ReadFile(rc)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Count(string(data), markBegin); got != 1 {
		t.Errorf("区块应只有 1 个，实际 %d", got)
	}
	if !strings.Contains(string(data), "export FOO=1") {
		t.Error("原有内容被破坏")
	}

	removed, err := removeBlock(rc)
	if err != nil || !removed {
		t.Fatalf("应能移除区块: removed=%v err=%v", removed, err)
	}
	data, _ = os.ReadFile(rc)
	if strings.TrimSpace(string(data)) != "export FOO=1" {
		t.Errorf("移除后内容 = %q", string(data))
	}
}

func TestInstallZsh(t *testing.T) {
	home := t.TempDir()
	ps := filepath.Join(home, ".config", "proxy_switch")
	t.Setenv("HOME", home)
	t.Setenv("PSWITCH_HOME", ps)

	if code := installShell("zsh"); code != 0 {
		t.Fatalf("installShell 退出码 = %d", code)
	}
	script := filepath.Join(ps, "completions", "_pswitch")
	if _, err := os.Stat(script); err != nil {
		t.Fatalf("补全脚本未写入: %v", err)
	}
	rc, err := os.ReadFile(filepath.Join(home, ".zshrc"))
	if err != nil {
		t.Fatalf("未更新 .zshrc: %v", err)
	}
	if !strings.Contains(string(rc), markBegin) || !strings.Contains(string(rc), "fpath=(") {
		t.Errorf(".zshrc 内容不符合预期:\n%s", rc)
	}

	if !uninstallShell("zsh") {
		t.Fatal("uninstallShell 应返回 true")
	}
	if _, err := os.Stat(script); !os.IsNotExist(err) {
		t.Error("补全脚本应已删除")
	}
	rc, _ = os.ReadFile(filepath.Join(home, ".zshrc"))
	if strings.Contains(string(rc), markBegin) {
		t.Error(".zshrc 中的区块应已移除")
	}
}

func TestInstallFishUsesXDG(t *testing.T) {
	home := t.TempDir()
	xdg := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", xdg)
	t.Setenv("PSWITCH_HOME", filepath.Join(home, "ps"))

	if code := installShell("fish"); code != 0 {
		t.Fatalf("installShell 退出码 = %d", code)
	}
	if _, err := os.Stat(filepath.Join(xdg, "fish", "completions", "pswitch.fish")); err != nil {
		t.Fatalf("fish 补全未写入 XDG 目录: %v", err)
	}
}

package cli

import (
	"fmt"
	"os"
	"strings"
	"syscall"

	"pswitch/internal/config"
	"pswitch/internal/server"
)

// cmdLan 管理 LAN 监听（127.0.0.1 ↔ 0.0.0.0）与允许来源。
func cmdLan(args []string) int {
	if len(args) == 0 {
		lanUsage()
		return 2
	}
	action, rest := args[0], args[1:]
	switch action {
	case "on":
		return lanOn(rest)
	case "off":
		if code, ok := requireNoArgs(rest, "lan off"); !ok {
			return code
		}
		return lanOff()
	case "allow":
		return lanAllow(rest)
	case "deny":
		return lanDeny(rest)
	case "status":
		if code, ok := requireNoArgs(rest, "lan status"); !ok {
			return code
		}
		return lanStatus()
	default:
		fmt.Fprintf(os.Stderr, "未知的 lan 动作: %s\n\n", action)
		lanUsage()
		return 2
	}
}

func lanUsage() {
	fmt.Fprint(os.Stderr, `usage: pswitch lan <on|off|allow|status> [...]

管理 LAN 监听与允许来源（本机回环始终允许）：

  lan on [IP|网段...]        切到 0.0.0.0，可同时覆盖允许来源
  lan off                    切回 127.0.0.1
  lan allow [IP|网段...]     查看/覆盖允许来源
  lan deny [IP|网段...]      查看/覆盖拒绝来源（优先于允许来源）
  lan allow|deny --clear     清空对应列表
  lan status                 查看当前监听与来源列表
`)
}

func lanOn(entries []string) int {
	cfg := config.Load()
	host, port, err := config.SplitHostPort(cfg.Listen)
	if err != nil {
		fmt.Println(err)
		return 1
	}
	if len(entries) > 0 {
		normalized, err := config.NormalizeCIDRs(entries)
		if err != nil {
			fmt.Println(err)
			return 1
		}
		cfg.AllowCIDRs = normalized
	} else if len(cfg.AllowCIDRs) == 0 {
		fmt.Println("为安全起见，开启 LAN 监听前请指定允许来源，可给任意多个 IP 或网段，例如:")
		fmt.Println("  pswitch lan on 192.168.3.0/24")
		fmt.Println("  pswitch lan on 192.168.3.40 10.0.0.8")
		return 1
	}

	if code := applyListen(cfg, config.JoinHostPort("0.0.0.0", port)); code != 0 {
		return code
	}
	if host == "0.0.0.0" {
		fmt.Printf("LAN 监听已开启: 0.0.0.0:%d\n", port)
	} else {
		fmt.Printf("已开启 LAN 监听: 0.0.0.0:%d\n", port)
	}
	fmt.Printf("允许来源: %s（本机始终允许，其余来源拒绝）\n", strings.Join(cfg.AllowCIDRs, ", "))
	return 0
}

func lanOff() int {
	cfg := config.Load()
	host, port, err := config.SplitHostPort(cfg.Listen)
	if err != nil {
		fmt.Println(err)
		return 1
	}
	if config.IsLoopbackHost(host) {
		fmt.Printf("当前已只监听本机: %s\n", cfg.Listen)
		return 0
	}
	if code := applyListen(cfg, config.JoinHostPort("127.0.0.1", port)); code != 0 {
		return code
	}
	fmt.Printf("已关闭 LAN 监听: 127.0.0.1:%d（仅本机可连接）\n", port)
	return 0
}

func lanAllow(args []string) int { return lanSetList(args, true) }

func lanDeny(args []string) int { return lanSetList(args, false) }

func lanSetList(args []string, isAllow bool) int {
	clear := false
	entries := make([]string, 0, len(args))
	for _, a := range args {
		switch {
		case a == "--clear":
			clear = true
		case strings.HasPrefix(a, "-"):
			fmt.Fprintf(os.Stderr, "未知参数: %s\n", a)
			return 2
		default:
			entries = append(entries, a)
		}
	}
	if clear && len(entries) > 0 {
		fmt.Println("--clear 不能和 IP / 网段一起使用")
		return 2
	}

	title, suffix, empty := "拒绝来源", "（优先级高于允许来源）", "未配置"
	if isAllow {
		title, suffix, empty = "允许来源", "（本机始终允许）", "未配置（仅本机可连接）"
	}

	cfg := config.Load()
	if !clear && len(entries) == 0 {
		if isAllow {
			return printList(title, cfg.AllowCIDRs, empty)
		}
		return printList(title, cfg.DenyCIDRs, empty)
	}

	normalized := []string{}
	if !clear {
		var err error
		normalized, err = config.NormalizeCIDRs(entries)
		if err != nil {
			fmt.Println(err)
			return 1
		}
	}
	if isAllow {
		cfg.AllowCIDRs = normalized
	} else {
		cfg.DenyCIDRs = normalized
	}
	if err := config.Save(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "保存配置失败: %v\n", err)
		return 1
	}
	signalDaemon(syscall.SIGHUP)

	if len(normalized) == 0 {
		fmt.Printf("已清空%s\n", title)
	} else {
		fmt.Printf("%s: %s%s\n", title, strings.Join(normalized, ", "), suffix)
	}
	host, _, err := config.SplitHostPort(cfg.Listen)
	if err != nil {
		return 0
	}
	if config.IsLoopbackHost(host) {
		fmt.Println("当前只监听本机，这项设置会在 pswitch lan on 后生效")
	} else if isAllow && len(normalized) == 0 {
		fmt.Println("注意：当前监听 LAN 地址，允许来源为空，只有本机能连接")
	}
	return 0
}

func lanStatus() int {
	cfg := config.Load()
	host, _, err := config.SplitHostPort(cfg.Listen)
	if err != nil {
		fmt.Println(err)
		return 1
	}
	fmt.Println("pswitch lan")
	fmt.Printf("  监听      %s  （%s）\n", cfg.Listen, lanMode(host))
	fmt.Printf("  允许来源  %s\n", config.AllowText(cfg.AllowCIDRs))
	fmt.Printf("  拒绝来源  %s\n", listText(cfg.DenyCIDRs, "无"))
	if pid := server.DaemonRunning(); pid != 0 {
		fmt.Printf("  守护进程  运行中 (pid %d)\n", pid)
	} else {
		fmt.Println("  守护进程  未运行")
	}
	return 0
}

func printList(title string, entries []string, empty string) int {
	if len(entries) == 0 {
		fmt.Printf("%s: %s\n", title, empty)
		return 0
	}
	fmt.Printf("%s:\n", title)
	for _, entry := range entries {
		fmt.Println("  " + entry)
	}
	return 0
}

func listText(entries []string, empty string) string {
	if len(entries) == 0 {
		return empty
	}
	return strings.Join(entries, ", ")
}

func lanMode(host string) string {
	if config.IsLoopbackHost(host) {
		return "仅本机"
	}
	if host == "0.0.0.0" || host == "::" {
		return "LAN 已开启"
	}
	return "自定义地址"
}

// applyListen 写入配置；监听地址变化且守护进程在跑时重启以生效。
func applyListen(cfg config.Config, newListen string) int {
	oldListen := cfg.Listen
	cfg.Listen = newListen
	if err := config.Save(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "保存配置失败: %v\n", err)
		return 1
	}
	if newListen == oldListen {
		signalDaemon(syscall.SIGHUP)
		return 0
	}
	if server.DaemonRunning() != 0 {
		fmt.Println("监听地址已变更，正在重启守护进程…")
		cmdStop(nil)
		return cmdStart(nil)
	}
	return 0
}

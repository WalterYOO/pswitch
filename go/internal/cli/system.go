package cli

import (
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"strings"
	"time"

	"pswitch/internal/config"
	"pswitch/internal/launchd"
	"pswitch/internal/server"
)

func cmdInstall(args []string) int {
	if code, ok := requireNoArgs(args, "install"); !ok {
		return code
	}
	cfg := config.Load()
	if cfg.Mode != config.ModeDirect {
		if !requireUpstreams(cfg, "安装开机自启") {
			return 1
		}
		if !requireEnabledUpstreams(cfg, "安装开机自启") {
			return 1
		}
	}
	if runtime.GOOS != "darwin" {
		fmt.Println("仅支持 macOS 的 launchd")
		return 1
	}
	exe, err := os.Executable()
	if err != nil {
		fmt.Printf("找不到可执行文件: %v\n", err)
		return 1
	}
	if err := launchd.WritePlist(exe); err != nil {
		fmt.Printf("写入 plist 失败: %v\n", err)
		return 1
	}
	_, _ = launchd.Bootout()
	out, err := launchd.Bootstrap()
	if err != nil && !strings.Contains(out, "already bootstrapped") {
		fmt.Printf("launchctl 失败: %s\n", strings.TrimSpace(out))
		return 1
	}

	host, port, splitErr := config.SplitHostPort(cfg.Listen)
	if splitErr != nil {
		fmt.Println(splitErr)
		return 1
	}
	for i := 0; i < 100; i++ {
		if pid := server.DaemonRunning(); pid != 0 &&
			server.PortOpen(host, port, 400*time.Millisecond) {
			fmt.Printf("已安装开机自启并启动 (pid %d)，监听 %s:%d\n", pid, host, port)
			fmt.Printf("plist: %s\n", launchd.PlistPath())
			fmt.Println("停止并禁用: pswitch uninstall")
			return 0
		}
		time.Sleep(100 * time.Millisecond)
	}
	fmt.Printf("已安装开机自启: %s（进程尚未就绪，看日志 %s）\n",
		launchd.PlistPath(), config.LogPath())
	fmt.Println("停止并禁用: pswitch uninstall")
	return 0
}

func cmdUninstall(args []string) int {
	_, _ = launchd.Bootout()
	launchd.RemovePlist()
	cmdStop(nil)
	fmt.Println("已卸载开机自启")
	return 0
}

var reService = regexp.MustCompile(`^\((?:\*)?\d+\)\s+(.+)$`)

// activeNetworkService 按默认路由找到当前活动的网络服务名。
func activeNetworkService() string {
	out, err := exec.Command("route", "-n", "get", "default").Output()
	if err != nil {
		return ""
	}
	iface := ""
	for _, line := range strings.Split(string(out), "\n") {
		if i := strings.Index(line, "interface:"); i >= 0 {
			iface = strings.TrimSpace(line[i+len("interface:"):])
			break
		}
	}
	if iface == "" {
		return ""
	}

	order, err := exec.Command("networksetup", "-listnetworkserviceorder").Output()
	if err != nil {
		return ""
	}
	svc := ""
	for _, line := range strings.Split(string(order), "\n") {
		if m := reService.FindStringSubmatch(strings.TrimSpace(line)); m != nil {
			svc = strings.TrimSpace(m[1])
			continue
		}
		if svc != "" && strings.Contains(line, "Device:") {
			dev := strings.SplitN(line, "Device:", 2)[1]
			dev = strings.TrimSpace(strings.SplitN(dev, ")", 2)[0])
			if dev == iface {
				return svc
			}
		}
	}
	return ""
}

func cmdSystem(args []string) int {
	if len(args) != 1 || (args[0] != "on" && args[0] != "off" && args[0] != "status") {
		fmt.Fprintln(os.Stderr, "用法: pswitch system on|off|status")
		return 2
	}
	action := args[0]
	cfg := config.Load()
	if cfg.Mode != config.ModeDirect && !requireUpstreams(cfg, "修改系统代理") {
		return 1
	}
	if action == "on" && server.DaemonRunning() == 0 {
		fmt.Println("守护进程未运行，先 pswitch start 或 pswitch install")
		return 1
	}
	if action == "on" && cfg.Mode != config.ModeDirect &&
		!requireEnabledUpstreams(cfg, "开启系统代理") {
		return 1
	}
	svc := activeNetworkService()
	if svc == "" {
		fmt.Println("找不到当前活动的网络服务")
		return 1
	}
	host, port, err := config.SplitHostPort(cfg.Listen)
	if err != nil {
		fmt.Println(err)
		return 1
	}
	if host == "0.0.0.0" {
		host = "127.0.0.1"
	}
	portStr := fmt.Sprintf("%d", port)

	run := func(args ...string) {
		_ = exec.Command("networksetup", args...).Run()
	}

	if action == "on" {
		run("-setwebproxy", svc, host, portStr)
		run("-setsecurewebproxy", svc, host, portStr)
		run("-setsocksfirewallproxy", svc, host, portStr)
		run("-setproxybypassdomains", svc, "127.0.0.1", "localhost")
		fmt.Printf("系统代理（%s）已指向 %s:%s\n", svc, host, portStr)
		return 0
	}
	if action == "off" {
		run("-setwebproxystate", svc, "off")
		run("-setsecurewebproxystate", svc, "off")
		run("-setsocksfirewallproxystate", svc, "off")
		fmt.Printf("系统代理（%s）已关闭\n", svc)
		return 0
	}

	for _, item := range []struct{ kind, flag string }{
		{"HTTP", "-getwebproxy"},
		{"HTTPS", "-getsecurewebproxy"},
		{"SOCKS", "-getsocksfirewallproxy"},
	} {
		out, _ := exec.Command("networksetup", item.flag, svc).Output()
		data := map[string]string{}
		for _, line := range strings.Split(string(out), "\n") {
			if k, v, ok := strings.Cut(line, ":"); ok {
				data[strings.TrimSpace(k)] = strings.TrimSpace(v)
			}
		}
		state := "关"
		if data["Enabled"] == "Yes" {
			state = "开"
		}
		fmt.Printf("  %-6s%s  %s:%s\n", item.kind, state, data["Server"], data["Port"])
	}
	return 0
}

// Package cli 实现 pswitch 命令行接口，命令与输出与 Python 版保持一致。
package cli

import (
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"

	"pswitch/internal/config"
	"pswitch/internal/launchd"
	"pswitch/internal/server"
	"pswitch/internal/version"
)

// Main 是命令入口，返回进程退出码。
func Main(argv []string) int {
	if len(argv) == 0 {
		usage()
		return 2
	}

	switch argv[0] {
	case "-V", "--version", "version":
		fmt.Printf("pswitch %s\n", version.Version)
		return 0
	case "-h", "--help", "help":
		usage()
		return 0
	}

	cmd, args := argv[0], argv[1:]

	if cmd == "serve" {
		fs := newFlagSet("serve")
		foreground := fs.Bool("foreground", false, "")
		debug := fs.Bool("debug", false, "")
		if code, ok := parseFlags(fs, args); !ok {
			return code
		}
		return server.Serve(*foreground, *debug)
	}

	if cmd == "completion" {
		// 只是装补全，不该顺手创建配置文件
		return cmdCompletion(args)
	}

	created, err := config.Ensure()
	if err != nil {
		fmt.Fprintf(os.Stderr, "创建配置目录失败: %v\n", err)
		return 1
	}
	if created {
		fmt.Printf("已创建配置文件: %s（还没有上游，用 pswitch add 添加）\n", config.ConfigPath())
	}

	switch cmd {
	case "start":
		return cmdStart(args)
	case "stop":
		return cmdStop(args)
	case "restart":
		cmdStop(args)
		return cmdStart(args)
	case "status":
		return cmdStatus(args)
	case "use":
		return cmdUse(args)
	case "list":
		return cmdList(args)
	case "add":
		return cmdAdd(args)
	case "remove":
		return cmdRemove(args)
	case "priority":
		return cmdPriority(args)
	case "enable":
		return cmdEnable(args)
	case "disable":
		return cmdDisable(args)
	case "env":
		return cmdEnv(args)
	case "test":
		return cmdTest(args)
	case "logs":
		return cmdLogs(args)
	case "install":
		return cmdInstall(args)
	case "uninstall":
		return cmdUninstall(args)
	case "system":
		return cmdSystem(args)
	default:
		fmt.Fprintf(os.Stderr, "未知命令: %s\n\n", cmd)
		usage()
		return 2
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `usage: pswitch <命令> [参数]

一个本地端口，多个上游代理。

命令:
  start / stop / restart   管理守护进程
  status                   查看状态（含实时可达性）
  use <名字|auto|direct>   切换出口
  list / add <名> <addr> / remove <名>
                           管理上游列表
  enable / disable <名>    临时启用/禁用上游
  priority [<名> [位置]]   查看/调整 auto 选路顺序
  env                      打印代理环境变量
  test                     经本地端口做真实请求
  logs [-n 行数]           查看最近日志
  install / uninstall      安装/卸载 launchd 开机自启
  system on|off|status     管理 macOS 系统代理
  system use <名字|host:port>
                           把系统代理直接指向某个上游（不经过 pswitch）
  completion [zsh|bash|fish]
                           安装 shell 补全（默认按 $SHELL 识别）
  version                  查看版本号
  serve                    前台运行转发进程（调试用）
`)
}

func newFlagSet(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	return fs
}

func parseFlags(fs *flag.FlagSet, args []string) (int, bool) {
	if err := fs.Parse(args); err != nil {
		fmt.Fprintf(os.Stderr, "%v\n", err)
		return 2, false
	}
	return 0, true
}

func requireNoArgs(args []string, name string) (int, bool) {
	if len(args) != 0 {
		fmt.Fprintf(os.Stderr, "%s 不接受额外参数: %v\n", name, args)
		return 2, false
	}
	return 0, true
}

// --------------------------------------------------------------------------
// 小工具
// --------------------------------------------------------------------------

func signalDaemon(sig os.Signal) { server.SignalDaemon(sig) }

func requireUpstreams(cfg config.Config, action string) bool {
	if config.HasUpstreams(&cfg) {
		return true
	}
	fmt.Printf("还没有配置任何上游，拒绝%s。\n", action)
	fmt.Println("先添加一个，例如:")
	fmt.Println("  pswitch add local 127.0.0.1:17890")
	fmt.Println(config.ConfigHint())
	return false
}

func requireEnabledUpstreams(cfg config.Config, action string) bool {
	if len(config.Enabled(&cfg)) > 0 {
		return true
	}
	fmt.Printf("所有上游都被禁用了，拒绝%s。\n", action)
	fmt.Println("先启用一个，例如: pswitch enable <名字>")
	fmt.Println(config.ConfigHint())
	return false
}

func enabledNames(cfg config.Config) string {
	ups := config.Enabled(&cfg)
	if len(ups) == 0 {
		return "无（都被禁用了）"
	}
	names := make([]string, len(ups))
	for i, u := range ups {
		names[i] = u.Name
	}
	return strings.Join(names, ", ")
}

func allNames(cfg config.Config) string {
	if len(cfg.Upstreams) == 0 {
		return "无"
	}
	names := make([]string, len(cfg.Upstreams))
	for i, u := range cfg.Upstreams {
		names[i] = u.Name
	}
	return strings.Join(names, ", ")
}

func saveAndReload(cfg config.Config) int {
	if err := config.Save(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "保存配置失败: %v\n", err)
		return 1
	}
	signalDaemon(syscall.SIGHUP)
	return 0
}

// --------------------------------------------------------------------------
// 进程管理
// --------------------------------------------------------------------------

func cmdStart(args []string) int {
	if code, ok := requireNoArgs(args, "start"); !ok {
		return code
	}
	cfg := config.Load()
	if cfg.Mode != config.ModeDirect {
		if !requireUpstreams(cfg, "启动") {
			return 1
		}
		if !requireEnabledUpstreams(cfg, "启动") {
			return 1
		}
	}
	if pid := server.DaemonRunning(); pid != 0 {
		fmt.Printf("已经在运行 (pid %d)\n", pid)
		return 0
	}
	host, port, err := config.SplitHostPort(cfg.Listen)
	if err != nil {
		fmt.Println(err)
		return 1
	}

	if launchd.Installed() {
		out, err := launchd.Bootstrap()
		if err != nil && !strings.Contains(out, "already bootstrapped") {
			fmt.Printf("launchctl 启动失败: %s\n", strings.TrimSpace(out))
			return 1
		}
	} else {
		exe, err := os.Executable()
		if err != nil {
			fmt.Printf("找不到可执行文件: %v\n", err)
			return 1
		}
		out, err := os.OpenFile(config.LogPath(), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			fmt.Printf("打开日志失败: %v\n", err)
			return 1
		}
		cmd := exec.Command(exe, "serve")
		cmd.Stdin = nil
		cmd.Stdout = out
		cmd.Stderr = out
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
		if err := cmd.Start(); err != nil {
			_ = out.Close()
			fmt.Printf("启动失败: %v\n", err)
			return 1
		}
		_ = cmd.Process.Release()
		_ = out.Close()
	}

	for i := 0; i < 60; i++ {
		time.Sleep(100 * time.Millisecond)
		if pid := server.DaemonRunning(); pid != 0 && server.PortOpen(host, port, 400*time.Millisecond) {
			fmt.Printf("已启动 (pid %d)，监听 %s:%d\n", pid, host, port)
			return 0
		}
	}
	fmt.Printf("启动失败或超时，看日志: %s\n", config.LogPath())
	return 1
}

func cmdStop(args []string) int {
	if code, ok := requireNoArgs(args, "stop"); !ok {
		return code
	}
	pid := server.DaemonRunning()
	if launchd.Installed() {
		// 先摘掉 launchd 作业，否则 KeepAlive 会立刻把它拉起来。
		_, _ = launchd.Bootout()
	}
	if pid == 0 {
		fmt.Println("没有在运行")
		return 0
	}
	var proc *os.Process
	if p, err := os.FindProcess(pid); err == nil {
		proc = p
	}
	for i := 0; i < 50; i++ {
		if !server.PIDAlive(pid) {
			fmt.Println("已停止")
			return 0
		}
		if proc != nil {
			_ = proc.Signal(syscall.SIGTERM)
		}
		time.Sleep(100 * time.Millisecond)
	}
	if proc != nil {
		if err := proc.Signal(syscall.SIGKILL); err != nil {
			fmt.Println("进程已退出")
		} else {
			fmt.Println("已强制停止")
		}
	}
	return 0
}

// --------------------------------------------------------------------------
// 上游管理
// --------------------------------------------------------------------------

func cmdUse(args []string) int {
	if len(args) != 1 {
		fmt.Fprintln(os.Stderr, "用法: pswitch use <名字|auto|direct>")
		return 2
	}
	key := args[0]
	cfg := config.Load()

	if key == "direct" {
		cfg.Mode = config.ModeDirect
		if code := saveAndReload(cfg); code != 0 {
			return code
		}
		fmt.Println("已切换为 direct：所有请求直接连目标，不经过任何上游")
		return 0
	}
	if !config.HasUpstreams(&cfg) {
		fmt.Println("还没有配置任何上游。先 pswitch add <名字> <host:port>")
		return 1
	}
	if key == "auto" {
		cfg.Mode = config.ModeAuto
		if code := saveAndReload(cfg); code != 0 {
			return code
		}
		fmt.Println("已切换为 auto：按顺序自动选择第一个可用的上游")
		return 0
	}
	u := config.FindUpstream(&cfg, key)
	if u == nil {
		fmt.Printf("没有这个上游: %s\n可选: %s, auto, direct\n", key, enabledNames(cfg))
		return 1
	}
	if !u.Enabled {
		fmt.Printf("%s 已被禁用，先 pswitch enable %s\n", u.Name, u.Name)
		return 1
	}
	cfg.Mode = config.ModeManual
	name := u.Name
	cfg.Active = &name
	if code := saveAndReload(cfg); code != 0 {
		return code
	}
	fmt.Printf("已切换: %s -> %s\n", u.Name, u.Addr)
	return 0
}

func cmdList(args []string) int {
	if code, ok := requireNoArgs(args, "list"); !ok {
		return code
	}
	cfg := config.Load()
	if !config.HasUpstreams(&cfg) {
		fmt.Println("还没有配置任何上游。用 pswitch add <名字> <host:port> 添加。")
		return 0
	}
	st := config.ReadState()
	active := ""
	if st.Active != nil {
		active = *st.Active
	}
	for i, u := range cfg.Upstreams {
		mark := " "
		if u.Name == active {
			mark = "*"
		}
		tag := ""
		if !u.Enabled {
			tag = "  已禁用"
		}
		fmt.Printf(" %s %d. %-12s %s%s\n", mark, i+1, u.Name, u.Addr, tag)
	}
	return 0
}

func cmdAdd(args []string) int {
	if len(args) != 2 {
		fmt.Fprintln(os.Stderr, "用法: pswitch add <名字> <host:port>")
		return 2
	}
	name, addr := args[0], args[1]
	if _, _, err := config.SplitHostPort(addr); err != nil {
		fmt.Println(err)
		return 1
	}
	cfg := config.Load()
	if config.FindUpstream(&cfg, name) != nil {
		fmt.Printf("名字已存在: %s\n", name)
		return 1
	}
	cfg.Upstreams = append(cfg.Upstreams, config.Upstream{Name: name, Addr: addr, Enabled: true})
	if cfg.Active == nil || *cfg.Active == "" {
		n := name
		cfg.Active = &n
	}
	if code := saveAndReload(cfg); code != 0 {
		return code
	}
	fmt.Printf("已添加: %s -> %s\n", name, addr)
	if len(cfg.Upstreams) == 1 {
		fmt.Println("现在可以启动了: pswitch start")
	}
	return 0
}

func cmdRemove(args []string) int {
	if len(args) != 1 {
		fmt.Fprintln(os.Stderr, "用法: pswitch remove <名字>")
		return 2
	}
	name := args[0]
	cfg := config.Load()
	before := len(cfg.Upstreams)
	out := cfg.Upstreams[:0]
	for _, u := range cfg.Upstreams {
		if u.Name != name {
			out = append(out, u)
		}
	}
	cfg.Upstreams = out
	if len(cfg.Upstreams) == before {
		fmt.Printf("没有这个上游: %s\n", name)
		return 1
	}
	if cfg.Active != nil && *cfg.Active == name {
		cfg.Mode = config.ModeAuto
		remaining := config.Enabled(&cfg)
		if len(remaining) == 0 {
			remaining = cfg.Upstreams
		}
		if len(remaining) > 0 {
			n := remaining[0].Name
			cfg.Active = &n
		} else {
			cfg.Active = nil
		}
	}
	if code := saveAndReload(cfg); code != 0 {
		return code
	}
	fmt.Printf("已删除: %s\n", name)
	return 0
}

func cmdPriority(args []string) int {
	fs := newFlagSet("priority")
	if code, ok := parseFlags(fs, args); !ok {
		return code
	}
	pos := fs.Args()
	cfg := config.Load()
	ups := cfg.Upstreams
	if len(ups) == 0 {
		fmt.Println("还没有配置任何上游。用 pswitch add <名字> <host:port> 添加。")
		return 0
	}

	if len(pos) == 0 {
		fmt.Println("优先级顺序（auto 从上到下取第一个可用的，禁用的跳过）：")
		for i, u := range ups {
			tag := ""
			if !u.Enabled {
				tag = "  已禁用"
			}
			line := fmt.Sprintf(" %d. %-12s %-24s%s", i+1, u.Name, u.Addr, tag)
			fmt.Println(strings.TrimRight(line, " "))
		}
		return 0
	}

	name := pos[0]
	cur := -1
	var target config.Upstream
	for i := range ups {
		if ups[i].Name == name || ups[i].Addr == name {
			cur = i
			target = ups[i]
			break
		}
	}
	if cur < 0 {
		fmt.Printf("没有这个上游: %s\n可选: %s\n", name, allNames(cfg))
		return 1
	}
	if len(pos) == 1 {
		fmt.Printf("%s 当前优先级 %d/%d\n", target.Name, cur+1, len(ups))
		return 0
	}
	want, err := strconv.Atoi(pos[1])
	if err != nil || want < 1 || want > len(ups) {
		fmt.Printf("位置要在 1..%d 之间\n", len(ups))
		return 1
	}
	if want-1 == cur {
		fmt.Printf("%s 已经在第 %d 位\n", target.Name, want)
		return 0
	}

	res := make([]config.Upstream, 0, len(ups))
	res = append(res, ups[:cur]...)
	res = append(res, ups[cur+1:]...)
	res = append(res, config.Upstream{})
	copy(res[want:], res[want-1:])
	res[want-1] = target
	cfg.Upstreams = res

	if code := saveAndReload(cfg); code != 0 {
		return code
	}
	names := make([]string, len(res))
	for i, u := range res {
		names[i] = u.Name
	}
	fmt.Printf("已把 %s 排到第 %d 位: %s\n", target.Name, want, strings.Join(names, " > "))
	if !target.Enabled {
		fmt.Printf("注意：它当前是禁用状态，顺序不参与选路，先 pswitch enable %s\n", target.Name)
	}
	return 0
}

func setEnabled(args []string, enabled bool) int {
	word := "禁用"
	action := "disable"
	if enabled {
		word = "启用"
		action = "enable"
	}
	if len(args) != 1 {
		fmt.Fprintf(os.Stderr, "用法: pswitch %s <名字>\n", action)
		return 2
	}
	name := args[0]
	cfg := config.Load()
	u := config.FindUpstream(&cfg, name)
	if u == nil {
		fmt.Printf("没有这个上游: %s\n可选: %s\n", name, allNames(cfg))
		return 1
	}
	if u.Enabled == enabled {
		fmt.Printf("%s 已经是%s状态\n", u.Name, word)
		return 0
	}
	u.Enabled = enabled
	displayName, displayAddr := u.Name, u.Addr
	if code := saveAndReload(cfg); code != 0 {
		return code
	}
	if enabled {
		fmt.Printf("已启用: %s -> %s\n", displayName, displayAddr)
		return 0
	}
	fmt.Printf("已禁用: %s -> %s（配置保留，不参与 use / auto / 故障转移）\n", displayName, displayAddr)
	if len(config.Enabled(&cfg)) == 0 {
		if cfg.Mode == config.ModeDirect {
			fmt.Println("警告：所有上游都已禁用；direct 模式不受影响，切回 auto/manual 前需启用一个。")
		} else {
			fmt.Println("警告：所有上游都已禁用，转发会全部失败。启用一个: pswitch enable <名字>")
		}
	} else if cfg.Mode == config.ModeManual && cfg.Active != nil && *cfg.Active == displayName {
		fmt.Println("注意：它正是当前 manual 固定的上游，守护进程会暂时改用其它可用上游。")
	}
	return 0
}

func cmdEnable(args []string) int  { return setEnabled(args, true) }
func cmdDisable(args []string) int { return setEnabled(args, false) }

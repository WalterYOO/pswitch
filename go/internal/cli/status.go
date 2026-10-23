package cli

import (
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"pswitch/internal/config"
	"pswitch/internal/health"
	"pswitch/internal/server"
)

func liveProbe(cfg config.Config) []config.Health {
	ups := config.Enabled(&cfg)
	out := make([]config.Health, len(ups))
	timeout := time.Duration(config.Seconds(cfg.ProbeTimeout, 3) * float64(time.Second))
	var wg sync.WaitGroup
	for i := range ups {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			out[i] = health.ProbeFull(ups[i].Addr, cfg.ProbeURL, timeout)
		}(i)
	}
	wg.Wait()
	return out
}

// autoPick 先 HTTP 通，再 TCP 通，最后退回第一个。
func autoPick(ups []config.Upstream, probes []config.Health) string {
	for _, key := range [2]string{"http", "tcp"} {
		for i := range ups {
			if i >= len(probes) {
				break
			}
			h := probes[i]
			if (key == "http" && h.HTTP) || (key == "tcp" && h.TCP) {
				return ups[i].Name
			}
		}
	}
	if len(ups) > 0 {
		return ups[0].Name
	}
	return ""
}

func cmdStatus(args []string) int {
	if code, ok := requireNoArgs(args, "status"); !ok {
		return code
	}
	cfg := config.Load()
	st := config.ReadState()
	pid := server.DaemonRunning()
	host, port, err := config.SplitHostPort(cfg.Listen)
	if err != nil {
		fmt.Println(err)
		return 1
	}
	listening := pid != 0 && server.PortOpen(host, port, 400*time.Millisecond)

	fmt.Println("pswitch")
	fmt.Printf("  配置文件  %s\n", config.ConfigPath())
	if pid != 0 {
		fmt.Printf("  守护进程  运行中 (pid %d)\n", pid)
	} else {
		fmt.Println("  守护进程  未运行  → pswitch start")
	}
	conn := "不可连接"
	if listening {
		conn = "可连接"
	}
	fmt.Printf("  监听      %s  %s\n", cfg.Listen, conn)

	mode := cfg.Mode
	direct := mode == config.ModeDirect

	if !config.HasUpstreams(&cfg) {
		if direct {
			fmt.Println("  模式      direct 全部直连")
			fmt.Println("  当前出口  direct（直接连接目标，不使用上游）")
		}
		fmt.Println("  上游      未配置  → pswitch add <名字> <host:port>")
		return 0
	}

	enabled := config.Enabled(&cfg)
	probes := liveProbe(cfg)
	healthByName := make(map[string]config.Health, len(enabled))
	for i, u := range enabled {
		healthByName[u.Name] = probes[i]
	}

	active := ""
	if direct {
		fmt.Println("  模式      direct 全部直连")
		fmt.Println("  当前出口  direct（直接连接目标，不使用上游）")
	} else {
		interval := cfg.HealthCheckInterval
		if interval < 2 {
			interval = 2
		}
		threshold := 3 * interval
		if threshold < 15 {
			threshold = 15
		}
		fresh := pid != 0 && st.Updated > 0 &&
			time.Since(time.Unix(0, int64(st.Updated*1e9))) <
				time.Duration(threshold*float64(time.Second))
		if fresh && st.Mode == mode && st.Active != nil {
			if _, ok := healthByName[*st.Active]; ok {
				active = *st.Active
			}
		}
		if active == "" && mode == config.ModeManual && cfg.Active != nil {
			if _, ok := healthByName[*cfg.Active]; ok {
				active = *cfg.Active
			}
		}
		if active == "" {
			active = autoPick(enabled, probes)
		}

		label := "auto 自动选择"
		if mode == config.ModeManual {
			label = "manual 固定"
		}
		fmt.Printf("  模式      %s\n", label)
		if len(enabled) == 0 {
			fmt.Println("  当前上游  -（所有上游都已禁用）")
		} else {
			display := active
			if display == "" {
				display = "-"
			}
			fmt.Printf("  当前上游  %s\n", display)
			if mode == config.ModeManual && cfg.Active != nil {
				if _, ok := healthByName[*cfg.Active]; !ok {
					fmt.Printf("  说明      manual 目标 %s 已禁用或不存在，按 auto 顺序选择\n", *cfg.Active)
				}
			}
		}
	}

	fmt.Println("  上游列表")
	for _, u := range cfg.Upstreams {
		mark := " "
		if u.Name == active {
			mark = "*"
		}
		if !u.Enabled {
			fmt.Printf("   %s %-12s %-24s 已禁用\n", mark, u.Name, u.Addr)
			continue
		}
		h := healthByName[u.Name]
		var state string
		switch {
		case h.HTTP:
			ms := 0
			if h.MS != nil {
				ms = *h.MS
			}
			state = fmt.Sprintf("OK        %3dms", ms)
		case h.TCP:
			state = "TCP only"
		default:
			state = "DOWN"
		}
		fmt.Printf("   %s %-12s %-24s %s\n", mark, u.Name, u.Addr, state)
	}
	return 0
}

func cmdEnv(args []string) int {
	if code, ok := requireNoArgs(args, "env"); !ok {
		return code
	}
	cfg := config.Load()
	proxy := fmt.Sprintf("http://%s", cfg.Listen)
	socks := fmt.Sprintf("socks5://%s", cfg.Listen)
	for _, line := range []string{
		fmt.Sprintf(`export http_proxy="%s"`, proxy),
		fmt.Sprintf(`export https_proxy="%s"`, proxy),
		fmt.Sprintf(`export HTTP_PROXY="%s"`, proxy),
		fmt.Sprintf(`export HTTPS_PROXY="%s"`, proxy),
		fmt.Sprintf(`export all_proxy="%s"`, socks),
		fmt.Sprintf(`export ALL_PROXY="%s"`, socks),
		`export no_proxy="127.0.0.1,localhost"`,
		`export NO_PROXY="127.0.0.1,localhost"`,
	} {
		fmt.Println(line)
	}
	return 0
}

func cmdTest(args []string) int {
	fs := newFlagSet("test")
	timeout := fs.Float64("timeout", 8, "")
	if code, ok := parseFlags(fs, args); !ok {
		return code
	}
	cfg := config.Load()
	if cfg.Mode != config.ModeDirect && !requireUpstreams(cfg, "测试") {
		return 1
	}
	host, port, err := config.SplitHostPort(cfg.Listen)
	if err != nil {
		fmt.Println(err)
		return 1
	}
	proxyURL, _ := url.Parse(fmt.Sprintf("http://%s:%d", host, port))
	client := &http.Client{
		Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)},
		Timeout:   time.Duration(*timeout * float64(time.Second)),
	}

	okAll := true
	for _, u := range []string{
		"http://connectivitycheck.gstatic.com/generate_204",
		"https://www.gstatic.com/generate_204",
	} {
		t0 := time.Now()
		resp, err := client.Get(u)
		if err != nil {
			okAll = false
			fmt.Printf("FAIL %s  %v\n", u, err)
			continue
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
		fmt.Printf("OK   %s  %d  %dms\n", u, resp.StatusCode, time.Since(t0).Milliseconds())
	}

	st := config.ReadState()
	if st.Active != nil {
		addr := ""
		if st.ActiveAddr != nil {
			addr = *st.ActiveAddr
		}
		fmt.Printf("实际出口: %s (%s)\n", *st.Active, addr)
	}
	if okAll {
		return 0
	}
	return 1
}

func cmdLogs(args []string) int {
	fs := newFlagSet("logs")
	var short, long int
	fs.IntVar(&short, "n", 30, "")
	fs.IntVar(&long, "lines", 30, "")
	if code, ok := parseFlags(fs, args); !ok {
		return code
	}
	count := 30
	fs.Visit(func(f *flag.Flag) {
		switch f.Name {
		case "n":
			count = short
		case "lines":
			count = long
		}
	})

	data, err := os.ReadFile(config.LogPath())
	if err != nil {
		fmt.Printf("暂无日志: %s\n", config.LogPath())
		return 0
	}
	lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	if len(lines) == 1 && lines[0] == "" {
		lines = nil
	}
	start := len(lines) - count
	if start < 0 {
		start = 0
	}
	for _, line := range lines[start:] {
		fmt.Println(line)
	}
	fmt.Printf("\n(日志文件: %s，实时跟踪: tail -f %s)\n", config.LogPath(), config.LogPath())
	return 0
}

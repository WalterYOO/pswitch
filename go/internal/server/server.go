// Package server 实现 TCP 转发守护进程：一个本地端口，多个上游。
//
// 与 Python 版的行为一一对应：
//   - 启动前完成首轮探活，避免刚启动时选错上游；
//   - auto / manual / direct 三种模式；
//   - 当前上游连不上时按健康顺序临时借用其它上游（failover）；
//   - SIGHUP 或配置文件 mtime 变化时热重载。
package server

import (
	"errors"
	"fmt"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"pswitch/internal/config"
	"pswitch/internal/direct"
	"pswitch/internal/health"
	"pswitch/internal/logging"
	"pswitch/internal/relay"
)

// Server 是转发守护进程。
type Server struct {
	log *logging.Logger

	mu          sync.RWMutex
	cfg         config.Config
	health      map[string]config.Health
	active      *config.Upstream
	allow       []*net.IPNet
	deny        []*net.IPNet
	configMtime time.Time
	ln          net.Listener

	stop     chan struct{}
	stopOnce sync.Once
}

// New 创建服务器。
func New(cfg config.Config, log *logging.Logger) *Server {
	return &Server{
		log:         log,
		cfg:         cfg,
		health:      map[string]config.Health{},
		allow:       config.ParseCIDRs(cfg.AllowCIDRs),
		deny:        config.ParseCIDRs(cfg.DenyCIDRs),
		configMtime: ConfigMtime(),
		stop:        make(chan struct{}),
	}
}

// Run 监听端口并开始转发，直到 Stop 被调用。
func (s *Server) Run() error {
	// launchd 下的 fd 软上限默认只有 256，先抬到硬上限的一半再干活
	if before, after, changed := config.RaiseFdLimit(); changed {
		s.log.Infof("fd 软上限: %d -> %d", before, after)
	}
	s.checkAll()

	host, port, err := config.SplitHostPort(s.cfg.Listen)
	if err != nil {
		return err
	}
	ln, err := net.Listen("tcp", net.JoinHostPort(host, strconv.Itoa(port)))
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.ln = ln
	mode := s.cfg.Mode
	activeName := ""
	if s.active != nil {
		activeName = s.active.Name
	}
	allowCIDRs := append([]string(nil), s.cfg.AllowCIDRs...)
	denyCIDRs := append([]string(nil), s.cfg.DenyCIDRs...)
	s.mu.Unlock()

	if err := os.WriteFile(config.PIDPath(), []byte(strconv.Itoa(os.Getpid())), 0o644); err != nil {
		_ = ln.Close()
		return err
	}
	defer func() {
		_ = os.Remove(config.PIDPath())
		s.log.Infof("pswitch 已停止")
	}()

	if mode == config.ModeDirect {
		s.log.Infof("pswitch 已启动，监听 %s:%d，模式 direct（全部直连）", host, port)
	} else {
		s.log.Infof("pswitch 已启动，监听 %s:%d，模式 %s，当前上游 %s",
			host, port, mode, activeName)
	}
	if !config.IsLoopbackHost(host) {
		s.log.Infof("允许来源（LAN）: %s", config.AllowText(allowCIDRs))
		if len(denyCIDRs) > 0 {
			s.log.Infof("拒绝来源（LAN）: %s", strings.Join(denyCIDRs, ", "))
		}
	}

	go s.healthLoop()

	var delay time.Duration
	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-s.stop:
				return nil
			default:
			}
			var ne net.Error
			if errors.As(err, &ne) && ne.Temporary() {
				if delay == 0 {
					delay = 5 * time.Millisecond
				} else {
					delay *= 2
				}
				if delay > time.Second {
					delay = time.Second
				}
				s.log.Warnf("接受连接出错: %v，%v 后重试", err, delay)
				time.Sleep(delay)
				continue
			}
			return err
		}
		delay = 0
		go s.handle(conn)
	}
}

// Stop 停止监听；已建立的连接由进程退出自然结束。
func (s *Server) Stop() {
	s.stopOnce.Do(func() {
		close(s.stop)
		s.mu.RLock()
		ln := s.ln
		s.mu.RUnlock()
		if ln != nil {
			_ = ln.Close()
		}
	})
}

// --------------------------------------------------------------------------
// 连接处理
// --------------------------------------------------------------------------

func (s *Server) handle(c net.Conn) {
	peer := ""
	if a := c.RemoteAddr(); a != nil {
		peer = a.String()
	}
	relay.Tune(c)
	defer c.Close()

	s.mu.RLock()
	cfg := s.cfg
	active := s.active
	hasActive := active != nil
	var activeCopy config.Upstream
	if hasActive {
		activeCopy = *active
	}
	allow := s.allow
	deny := s.deny
	candidates := s.candidatesLocked(active)
	s.mu.RUnlock()

	if ip := remoteIP(c.RemoteAddr()); ip != nil && !config.PeerAllowed(allow, deny, ip) {
		s.log.Warnf("拒绝来自 %s 的连接：不在允许来源列表中", peer)
		return
	}

	if cfg.Mode == config.ModeDirect {
		direct.Handle(c, &cfg, s.log)
		return
	}
	if !hasActive {
		return
	}

	timeout := time.Duration(config.Seconds(cfg.ConnectTimeout, 3) * float64(time.Second))
	for _, cand := range candidates {
		up, err := net.DialTimeout("tcp", cand.Addr, timeout)
		if err != nil {
			s.log.Warnf("连接上游 %s (%s) 失败: %v", cand.Name, cand.Addr, err)
			continue
		}
		relay.Tune(up)
		if cand.Name != activeCopy.Name {
			s.log.Warnf("上游 %s 不可用，临时改走 %s", activeCopy.Name, cand.Name)
		} else {
			s.log.Debugf("%s -> %s (%s)", peer, cand.Name, cand.Addr)
		}
		relay.Relay(c, up)
		return
	}
	s.log.Errorf("所有上游都连不上，断开客户端 %s", peer)
}

// candidatesLocked 返回依次尝试的上游：当前上游优先，然后是健康的其它上游。
func (s *Server) candidatesLocked(active *config.Upstream) []config.Upstream {
	if active == nil {
		return nil
	}
	out := []config.Upstream{*active}
	if !s.cfg.Failover {
		return out
	}
	ups := config.Enabled(&s.cfg)
	for _, key := range [2]string{"http", "tcp"} {
		for _, u := range ups {
			if u.Name == active.Name || containsName(out, u.Name) {
				continue
			}
			h := s.health[u.Name]
			if (key == "http" && h.HTTP) || (key == "tcp" && h.TCP) {
				out = append(out, u)
			}
		}
	}
	return out
}

func containsName(ups []config.Upstream, name string) bool {
	for _, u := range ups {
		if u.Name == name {
			return true
		}
	}
	return false
}

// --------------------------------------------------------------------------
// 选路与探活
// --------------------------------------------------------------------------

func (s *Server) pickLocked() *config.Upstream {
	if s.cfg.Mode == config.ModeDirect {
		return nil
	}
	ups := config.Enabled(&s.cfg)
	if len(ups) == 0 {
		return nil
	}
	if s.cfg.Mode == config.ModeManual && s.cfg.Active != nil {
		for i := range ups {
			if ups[i].Name == *s.cfg.Active {
				return &ups[i]
			}
		}
	}
	for _, key := range [2]string{"http", "tcp"} {
		for i := range ups {
			h := s.health[ups[i].Name]
			if (key == "http" && h.HTTP) || (key == "tcp" && h.TCP) {
				return &ups[i]
			}
		}
	}
	return &ups[0]
}

// applySelectionLocked 重新选路，返回 (mode, 旧名字, 新名字, 新地址)。
func (s *Server) applySelectionLocked() (mode, oldName, newName, newAddr string) {
	mode = s.cfg.Mode
	if s.active != nil {
		oldName = s.active.Name
	}
	if s.cfg.Mode == config.ModeDirect {
		s.active = nil
		return mode, oldName, "", ""
	}
	s.active = s.pickLocked()
	if s.active != nil {
		newName, newAddr = s.active.Name, s.active.Addr
	}
	return mode, oldName, newName, newAddr
}

func (s *Server) logSelection(mode, oldName, newName, newAddr string) {
	if mode == config.ModeDirect {
		if oldName != "" {
			s.log.Infof("当前模式: direct（不走任何上游，全部直连）")
		}
		return
	}
	if newName == oldName {
		return
	}
	if newName != "" {
		s.log.Infof("当前上游: %s -> %s (%s)", orNone(oldName), newName, newAddr)
	} else {
		s.log.Warnf("当前上游: %s -> 无", orNone(oldName))
	}
}

// checkAll 并发探活全部启用中的上游，整体替换健康表。
func (s *Server) checkAll() {
	s.mu.RLock()
	cfg := s.cfg
	s.mu.RUnlock()

	if cfg.Mode == config.ModeDirect {
		s.mu.Lock()
		s.health = map[string]config.Health{}
		mode, oldName, newName, newAddr := s.applySelectionLocked()
		s.mu.Unlock()
		s.logSelection(mode, oldName, newName, newAddr)
		s.writeState()
		return
	}

	ups := config.Enabled(&cfg)
	timeout := time.Duration(config.Seconds(cfg.ProbeTimeout, 3) * float64(time.Second))
	results := make([]config.Health, len(ups))
	var wg sync.WaitGroup
	for i := range ups {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i] = health.ProbeFull(ups[i].Addr, cfg.ProbeURL, timeout)
		}(i)
	}
	wg.Wait()

	hm := make(map[string]config.Health, len(ups))
	for i, u := range ups {
		hm[u.Name] = results[i]
	}

	s.mu.Lock()
	s.health = hm
	mode, oldName, newName, newAddr := s.applySelectionLocked()
	s.mu.Unlock()

	s.logSelection(mode, oldName, newName, newAddr)
	s.writeState()
}

func (s *Server) healthLoop() {
	for {
		timer := time.NewTimer(s.interval())
		select {
		case <-s.stop:
			timer.Stop()
			return
		case <-timer.C:
		}
		s.checkAll()
		if mt := ConfigMtime(); !mt.Equal(s.getConfigMtime()) {
			s.Reload()
		}
	}
}

func (s *Server) interval() time.Duration {
	s.mu.RLock()
	v := s.cfg.HealthCheckInterval
	s.mu.RUnlock()
	if v < 2 {
		v = 2
	}
	return time.Duration(v * float64(time.Second))
}

func (s *Server) getConfigMtime() time.Time {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.configMtime
}

// ConfigMtime 返回配置文件的修改时间。
func ConfigMtime() time.Time {
	st, err := os.Stat(config.ConfigPath())
	if err != nil {
		return time.Time{}
	}
	return st.ModTime()
}

// Reload 重新读取配置；监听地址变化需要重启。
func (s *Server) Reload() {
	cfg := config.Load()

	s.mu.Lock()
	if cfg.Listen != s.cfg.Listen {
		s.log.Warnf("监听地址变化需要 restart 才生效，暂沿用 %s", s.cfg.Listen)
		cfg.Listen = s.cfg.Listen
	}
	s.cfg = cfg
	s.allow = config.ParseCIDRs(cfg.AllowCIDRs)
	s.deny = config.ParseCIDRs(cfg.DenyCIDRs)
	s.configMtime = ConfigMtime()
	needProbe := cfg.Mode != config.ModeDirect && len(s.health) == 0
	s.mu.Unlock()

	s.log.Infof("配置已重载: mode=%s active=%s", cfg.Mode, deref(cfg.Active))

	if needProbe {
		// 从 direct 切回 auto/manual 时没有健康数据，先探活再选路。
		s.checkAll()
		return
	}
	s.mu.Lock()
	mode, oldName, newName, newAddr := s.applySelectionLocked()
	s.mu.Unlock()
	s.logSelection(mode, oldName, newName, newAddr)
	s.writeState()
}

func (s *Server) writeState() {
	s.mu.RLock()
	cfg := s.cfg
	var name, addr *string
	if s.active != nil {
		n, a := s.active.Name, s.active.Addr
		name, addr = &n, &a
	}
	hm := make(map[string]config.Health, len(s.health))
	for k, v := range s.health {
		hm[k] = v
	}
	s.mu.RUnlock()

	st := config.State{
		PID:        os.Getpid(),
		Listen:     cfg.Listen,
		Mode:       cfg.Mode,
		Manual:     cfg.Active,
		Active:     name,
		ActiveAddr: addr,
		Health:     hm,
		Updated:    float64(time.Now().UnixNano()) / 1e9,
	}
	if err := config.WriteJSON(config.StatePath(), st); err != nil {
		s.log.Warnf("写 state 失败: %v", err)
	}
}

func deref(s *string) string {
	if s == nil {
		return "None"
	}
	return *s
}

func orNone(s string) string {
	if s == "" {
		return "None"
	}
	return s
}

// --------------------------------------------------------------------------
// 进程 / 端口辅助
// --------------------------------------------------------------------------

// remoteIP 从 net.Addr 提取来源 IP。
func remoteIP(addr net.Addr) net.IP {
	if addr == nil {
		return nil
	}
	if a, ok := addr.(*net.TCPAddr); ok {
		return a.IP
	}
	host, _, err := net.SplitHostPort(addr.String())
	if err != nil {
		return nil
	}
	return net.ParseIP(host)
}

// ReadPID 读取 PID 文件。
func ReadPID() int {
	data, err := os.ReadFile(config.PIDPath())
	if err != nil {
		return 0
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		return 0
	}
	return pid
}

// PIDAlive 检查进程是否存在。
func PIDAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	p, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return p.Signal(syscall.Signal(0)) == nil
}

// DaemonRunning 返回运行中的守护进程 PID，未运行返回 0。
func DaemonRunning() int {
	pid := ReadPID()
	if PIDAlive(pid) {
		return pid
	}
	return 0
}

// PortOpen 检查端口是否可连接。
func PortOpen(host string, port int, timeout time.Duration) bool {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort(host, strconv.Itoa(port)), timeout)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

// SignalDaemon 向守护进程发送信号。
func SignalDaemon(sig os.Signal) {
	pid := DaemonRunning()
	if pid == 0 {
		return
	}
	if p, err := os.FindProcess(pid); err == nil {
		_ = p.Signal(sig)
	}
}

// Serve 是 serve 子命令的入口，返回进程退出码。
func Serve(foreground, debug bool) int {
	logger, err := logging.Setup(foreground, debug)
	if err != nil {
		fmt.Fprintf(os.Stderr, "打开日志失败: %v\n", err)
		return 1
	}
	defer logger.Close()

	cfg := config.Load()
	if cfg.Mode != config.ModeDirect && len(config.Enabled(&cfg)) == 0 {
		if len(cfg.Upstreams) > 0 {
			logger.Errorf("所有上游都被禁用，拒绝启动")
			fmt.Fprintf(os.Stderr,
				"所有上游都被禁用了，没有可用上游，拒绝启动。\n"+
					"先启用一个，例如: pswitch enable <名字>\n%s\n",
				config.ConfigHint())
		} else {
			logger.Errorf("没有配置任何上游，拒绝启动")
			fmt.Fprintf(os.Stderr,
				"没有配置任何上游，拒绝启动。\n"+
					"先添加一个，例如: pswitch add local 127.0.0.1:17890\n%s\n",
				config.ConfigHint())
		}
		return 2
	}
	if pid := DaemonRunning(); pid != 0 {
		logger.Errorf("已有实例在运行 (pid %d)", pid)
		return 1
	}

	srv := New(cfg, logger)
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP)
	defer signal.Stop(sigs)
	go func() {
		for sig := range sigs {
			if sig == syscall.SIGHUP {
				srv.Reload()
				continue
			}
			srv.Stop()
			return
		}
	}()

	if err := srv.Run(); err != nil {
		logger.Errorf("运行出错: %v", err)
		return 1
	}
	return 0
}

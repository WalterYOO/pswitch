// Package config 负责配置文件、运行时状态与路径约定。
//
// 目录、文件名与 JSON 字段与 Python 版完全一致，因此两版可以共用同一个
// PSWITCH_HOME，CLI 与守护进程可以互操作。
package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Upstream 是一个上游代理。
type Upstream struct {
	Name    string `json:"name"`
	Addr    string `json:"addr"`
	Enabled bool   `json:"enabled"`
}

// UnmarshalJSON 让缺失的 enabled 字段默认为 true（与旧配置兼容）。
func (u *Upstream) UnmarshalJSON(data []byte) error {
	var raw struct {
		Name    string `json:"name"`
		Addr    string `json:"addr"`
		Enabled *bool  `json:"enabled"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	u.Name = raw.Name
	u.Addr = raw.Addr
	u.Enabled = raw.Enabled == nil || *raw.Enabled
	return nil
}

// Health 是单个上游的探活结果。
type Health struct {
	TCP  bool `json:"tcp"`
	HTTP bool `json:"http"`
	MS   *int `json:"ms"`
}

// Config 与 Python 版的 config.json 一一对应。
type Config struct {
	Listen              string  `json:"listen"`
	Mode                string  `json:"mode"`
	Active              *string `json:"active"`
	HealthCheckInterval float64 `json:"health_check_interval"`
	ConnectTimeout      float64 `json:"connect_timeout"`
	Failover            bool    `json:"failover"`
	ProbeURL            string  `json:"probe_url"`
	ProbeTimeout        float64 `json:"probe_timeout"`
	// AllowCIDRs 是监听非本机地址时允许的来源（IP 或网段）。
	// 为空表示只接受本机连接；回环地址始终允许。
	AllowCIDRs []string `json:"allow_cidrs"`
	// DenyCIDRs 是拒绝来源（IP 或网段），优先级高于 AllowCIDRs；回环同样不受限。
	DenyCIDRs []string   `json:"deny_cidrs"`
	Upstreams []Upstream `json:"upstreams"`

	// extra 保留未知字段，保存时原样写回（与 Python 的 dict.update 行为一致）。
	extra map[string]json.RawMessage
}

// Default 返回内置默认配置（不含任何上游）。
func Default() Config {
	return Config{
		Listen:              "127.0.0.1:17000",
		Mode:                "auto",
		Active:              nil,
		HealthCheckInterval: 10,
		ConnectTimeout:      3,
		Failover:            true,
		ProbeURL:            "http://connectivitycheck.gstatic.com/generate_204",
		ProbeTimeout:        3,
		AllowCIDRs:          []string{},
		DenyCIDRs:           []string{},
		Upstreams:           []Upstream{},
	}
}

var knownKeys = map[string]struct{}{
	"listen": {}, "mode": {}, "active": {}, "health_check_interval": {},
	"connect_timeout": {}, "failover": {}, "probe_url": {}, "probe_timeout": {},
	"allow_cidrs": {}, "deny_cidrs": {}, "upstreams": {},
}

// UnmarshalJSON 在默认值之上合并文件内容，并保留未知字段。
func (c *Config) UnmarshalJSON(data []byte) error {
	type plain Config
	base := plain(Default())
	if err := json.Unmarshal(data, &base); err != nil {
		return err
	}
	var all map[string]json.RawMessage
	if err := json.Unmarshal(data, &all); err != nil {
		return err
	}
	extra := make(map[string]json.RawMessage)
	for k, v := range all {
		if _, ok := knownKeys[k]; !ok {
			extra[k] = v
		}
	}
	*c = Config(base)
	c.extra = extra
	c.normalize()
	return nil
}

// MarshalJSON 把已知字段和保留的未知字段一起输出。
func (c Config) MarshalJSON() ([]byte, error) {
	type plain Config
	raw, err := json.Marshal(plain(c))
	if err != nil {
		return nil, err
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, err
	}
	for k, v := range c.extra {
		if _, ok := m[k]; !ok {
			m[k] = v
		}
	}
	return json.Marshal(m)
}

func (c *Config) normalize() {
	out := make([]Upstream, 0, len(c.Upstreams))
	for _, u := range c.Upstreams {
		if u.Name != "" && u.Addr != "" {
			out = append(out, u)
		}
	}
	c.Upstreams = out
	if c.AllowCIDRs == nil {
		c.AllowCIDRs = []string{}
	}
	if c.DenyCIDRs == nil {
		c.DenyCIDRs = []string{}
	}
}

// Home 返回配置目录，可用 PSWITCH_HOME 覆盖。
func Home() string {
	if v := os.Getenv("PSWITCH_HOME"); v != "" {
		return v
	}
	h, err := os.UserHomeDir()
	if err != nil || h == "" {
		h = "."
	}
	return filepath.Join(h, ".config", "proxy_switch")
}

func ConfigPath() string { return filepath.Join(Home(), "config.json") }
func StatePath() string  { return filepath.Join(Home(), "state.json") }
func PIDPath() string    { return filepath.Join(Home(), "pswitch.pid") }
func LogPath() string    { return filepath.Join(Home(), "pswitch.log") }

// ConfigHint 返回给用户看的配置文件位置。
func ConfigHint() string { return "配置文件: " + ConfigPath() }

// Ensure 在配置不存在时创建一份默认配置，返回是否新建。
func Ensure() (bool, error) {
	if err := os.MkdirAll(Home(), 0o755); err != nil {
		return false, err
	}
	if _, err := os.Stat(ConfigPath()); err == nil {
		return false, nil
	}
	if err := Save(Default()); err != nil {
		return false, err
	}
	return true, nil
}

// Load 读取配置；文件损坏或缺失时退回默认值（与 Python 版一致）。
func Load() Config {
	_, _ = Ensure()
	data, err := os.ReadFile(ConfigPath())
	if err != nil {
		return Default()
	}
	var c Config
	if err := json.Unmarshal(data, &c); err != nil {
		return Default()
	}
	return c
}

// Save 原子写入配置。
func Save(c Config) error {
	return WriteJSON(ConfigPath(), c)
}

// WriteJSON 先写临时文件再 rename，避免读者看到半截内容。
func WriteJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := MarshalPretty(v)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// MarshalPretty 输出两空格缩进、不转义 HTML 的 JSON。
func MarshalPretty(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// ReadJSON 读取 JSON；出错时返回 false，不报错（与 Python 版一致）。
func ReadJSON(path string, v any) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	return json.Unmarshal(data, v) == nil
}

// State 是守护进程写出的运行时状态。
type State struct {
	PID        int               `json:"pid"`
	Listen     string            `json:"listen"`
	Mode       string            `json:"mode"`
	Manual     *string           `json:"manual"`
	Active     *string           `json:"active"`
	ActiveAddr *string           `json:"active_addr"`
	Health     map[string]Health `json:"health"`
	Updated    float64           `json:"updated"`
}

// ReadState 读取 state.json，失败时返回零值。
func ReadState() State {
	var st State
	_ = ReadJSON(StatePath(), &st)
	if st.Health == nil {
		st.Health = map[string]Health{}
	}
	return st
}

// FindUpstream 按名字或地址查找上游。
func FindUpstream(c *Config, key string) *Upstream {
	for i := range c.Upstreams {
		if c.Upstreams[i].Name == key || c.Upstreams[i].Addr == key {
			return &c.Upstreams[i]
		}
	}
	return nil
}

// HasUpstreams 判断是否配置了任意上游（含被禁用的）。
func HasUpstreams(c *Config) bool { return len(c.Upstreams) > 0 }

// Enabled 返回参与选路的上游（按配置顺序）。
func Enabled(c *Config) []Upstream {
	out := make([]Upstream, 0, len(c.Upstreams))
	for _, u := range c.Upstreams {
		if u.Enabled {
			out = append(out, u)
		}
	}
	return out
}

// Mode 常量。
const (
	ModeAuto   = "auto"
	ModeManual = "manual"
	ModeDirect = "direct"
)

// Seconds 把配置里的秒数转成时长；非法值退回 def。
func Seconds(v float64, def float64) float64 {
	if v <= 0 {
		return def
	}
	return v
}

// SplitHostPort 解析配置里的 host:port，端口必须是数字。
func SplitHostPort(s string) (string, int, error) {
	i := strings.LastIndex(s, ":")
	if i <= 0 || i == len(s)-1 {
		return "", 0, fmt.Errorf("地址格式应为 host:port，收到 %q", s)
	}
	host, portStr := s[:i], s[i+1:]
	port, err := strconv.Atoi(portStr)
	if err != nil || port < 0 || port > 65535 {
		return "", 0, fmt.Errorf("地址格式应为 host:port，收到 %q", s)
	}
	return host, port, nil
}

// SplitHostPortDefault 解析 host:port，支持 [IPv6]:port；缺端口时用 def。
func SplitHostPortDefault(s string, def *int) (string, int, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return "", 0, fmt.Errorf("空地址")
	}
	if strings.HasPrefix(s, "[") {
		end := strings.Index(s, "]")
		if end < 0 {
			return "", 0, fmt.Errorf("地址格式错误: %q", s)
		}
		host := s[1:end]
		rest := s[end+1:]
		if strings.HasPrefix(rest, ":") {
			if p, err := strconv.Atoi(rest[1:]); err == nil {
				return host, p, nil
			}
		}
		if rest != "" {
			return "", 0, fmt.Errorf("地址格式错误: %q", s)
		}
		if def == nil {
			return "", 0, fmt.Errorf("缺少端口: %q", s)
		}
		return host, *def, nil
	}
	if i := strings.LastIndex(s, ":"); i > 0 && i < len(s)-1 {
		if p, err := strconv.Atoi(s[i+1:]); err == nil {
			return s[:i], p, nil
		}
	}
	if def == nil {
		return "", 0, fmt.Errorf("缺少端口: %q", s)
	}
	return s, *def, nil
}

// JoinHostPort 统一拼接 host:port（IPv6 请传带方括号的 host）。
func JoinHostPort(host string, port int) string {
	return host + ":" + strconv.Itoa(port)
}

// --------------------------------------------------------------------------
// LAN 监听 / 允许来源
// --------------------------------------------------------------------------

const cidrExample = "示例: 192.168.3.40 / 192.168.3.0/24"

// NormalizeCIDR 把 IP 或网段规范成 "ip/prefix"：192.168.3.40 -> 192.168.3.40/32。
func NormalizeCIDR(entry string) (string, error) {
	text := strings.TrimSpace(entry)
	if text == "" {
		return "", fmt.Errorf("空的 IP / 网段")
	}
	if strings.Contains(text, "/") {
		_, network, err := net.ParseCIDR(text)
		if err != nil {
			return "", fmt.Errorf("不是合法的 IP 或网段: %s（%s）", text, cidrExample)
		}
		return network.String(), nil
	}
	ip := net.ParseIP(text)
	if ip == nil {
		return "", fmt.Errorf("不是合法的 IP 或网段: %s（%s）", text, cidrExample)
	}
	if ip4 := ip.To4(); ip4 != nil {
		return ip4.String() + "/32", nil
	}
	return ip.String() + "/128", nil
}

// NormalizeCIDRs 规范化一组条目并去重，保持输入顺序。
func NormalizeCIDRs(entries []string) ([]string, error) {
	out := make([]string, 0, len(entries))
	seen := make(map[string]bool, len(entries))
	for _, entry := range entries {
		network, err := NormalizeCIDR(entry)
		if err != nil {
			return nil, err
		}
		if !seen[network] {
			seen[network] = true
			out = append(out, network)
		}
	}
	return out, nil
}

// ParseCIDRs 解析来源列表；非法项忽略（手改配置时容错）。
func ParseCIDRs(entries []string) []*net.IPNet {
	out := make([]*net.IPNet, 0, len(entries))
	for _, entry := range entries {
		network, err := NormalizeCIDR(entry)
		if err != nil {
			continue
		}
		if _, parsed, err := net.ParseCIDR(network); err == nil {
			out = append(out, parsed)
		}
	}
	return out
}

// PeerAllowed 判断来源 IP 是否允许：回环始终允许，deny 优先于 allow。
func PeerAllowed(allow, deny []*net.IPNet, ip net.IP) bool {
	if ip == nil {
		return false
	}
	if ip.IsLoopback() {
		return true
	}
	for _, network := range deny {
		if network != nil && network.Contains(ip) {
			return false
		}
	}
	for _, network := range allow {
		if network != nil && network.Contains(ip) {
			return true
		}
	}
	return false
}

// IsLoopbackHost 判断监听地址是否只面向本机。
func IsLoopbackHost(host string) bool {
	if host == "" || host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// AllowText 返回给用户看的允许来源说明。
func AllowText(entries []string) string {
	if len(entries) == 0 {
		return "无（仅本机可连接）"
	}
	return strings.Join(entries, ", ")
}

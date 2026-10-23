// Package health 实现上游探活：先做一次真实 HTTP 代理请求，失败再退回 TCP 探测。
package health

import (
	"bytes"
	"net"
	"net/url"
	"strconv"
	"strings"
	"time"

	"pswitch/internal/config"
)

const maxHeadBytes = 64 * 1024

// ProbeFull 与 Python 版 probe_full 对齐：
//   - HTTP 探测成功 → {tcp:true, http:true, ms}
//   - 否则 TCP 探测成功 → {tcp:true, http:false, ms}
//   - 都失败 → {tcp:false, http:false, ms:null}
func ProbeFull(addr, probeURL string, timeout time.Duration) config.Health {
	if ok, ms := probeHTTP(addr, probeURL, timeout); ok {
		return config.Health{TCP: true, HTTP: true, MS: &ms}
	}
	if ok, ms := probeTCP(addr, time.Second); ok {
		return config.Health{TCP: true, HTTP: false, MS: &ms}
	}
	return config.Health{TCP: false, HTTP: false, MS: nil}
}

// probeHTTP 用 HTTP 代理协议真实请求一次探活地址。
func probeHTTP(addr, probeURL string, timeout time.Duration) (bool, int) {
	u, err := url.Parse(probeURL)
	if err != nil || u.Scheme == "" || u.Hostname() == "" {
		return false, 0
	}
	hostHdr := u.Hostname()
	if p := u.Port(); p != "" {
		hostHdr += ":" + p
	}
	path := u.EscapedPath()
	if path == "" {
		path = "/"
	}
	if u.RawQuery != "" {
		path += "?" + u.RawQuery
	}
	req := "GET " + u.Scheme + "://" + hostHdr + path + " HTTP/1.1\r\n" +
		"Host: " + hostHdr + "\r\n" +
		"User-Agent: pswitch-health\r\n" +
		"Accept: */*\r\n" +
		"Connection: close\r\n\r\n"

	start := time.Now()
	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return false, 0
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))
	if _, err := conn.Write([]byte(req)); err != nil {
		return false, 0
	}
	head, ok := readHead(conn)
	if !ok {
		return false, 0
	}
	code, ok := statusCode(head)
	ms := int(time.Since(start).Milliseconds())
	if !ok {
		return false, ms
	}
	return code == 200 || code == 204, ms
}

// probeTCP 只验证 TCP 可达。
func probeTCP(addr string, timeout time.Duration) (bool, int) {
	start := time.Now()
	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return false, 0
	}
	_ = conn.Close()
	return true, int(time.Since(start).Milliseconds())
}

// readHead 读到 \r\n\r\n 为止。
func readHead(conn net.Conn) ([]byte, bool) {
	buf := make([]byte, 0, 512)
	tmp := make([]byte, 512)
	for len(buf) < maxHeadBytes {
		n, err := conn.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
			if i := bytes.Index(buf, []byte("\r\n\r\n")); i >= 0 {
				return buf[:i], true
			}
		}
		if err != nil {
			return nil, false
		}
	}
	return nil, false
}

// statusCode 解析状态行里的三位状态码。
func statusCode(head []byte) (int, bool) {
	line := string(head)
	if i := strings.Index(line, "\r\n"); i >= 0 {
		line = line[:i]
	}
	parts := strings.Fields(line)
	if len(parts) < 2 || !strings.HasPrefix(parts[0], "HTTP/") {
		return 0, false
	}
	code, err := strconv.Atoi(parts[1])
	if err != nil || code < 100 || code > 999 {
		return 0, false
	}
	return code, true
}

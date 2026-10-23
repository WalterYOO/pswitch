// Package direct 实现 direct 模式：pswitch 自己充当 HTTP / SOCKS5 代理，直连目标。
//
// 支持范围与 Python 版一致：
//   - HTTP CONNECT（HTTPS 及任意隧道协议）
//   - 普通 HTTP 代理请求：首请求行改写为 origin-form，并强制 Connection: close
//   - SOCKS5 CONNECT，无认证；BIND / UDP ASSOCIATE 明确拒绝
//   - SOCKS4 不支持，记录日志后断开
package direct

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"strconv"
	"strings"
	"syscall"
	"time"

	"pswitch/internal/config"
	"pswitch/internal/logging"
	"pswitch/internal/relay"
)

const (
	handshakeTimeout = 10 * time.Second
	maxHeadBytes     = 64 * 1024
)

type badRequest struct{ msg string }

func (e *badRequest) Error() string { return e.msg }

func badf(format string, args ...any) error {
	return &badRequest{msg: fmt.Sprintf(format, args...)}
}

type header struct {
	Name  string
	Value string
}

// Handle 处理 direct 模式下的单条客户端连接。
func Handle(client net.Conn, cfg *config.Config, log *logging.Logger) {
	peer := ""
	if a := client.RemoteAddr(); a != nil {
		peer = a.String()
	}
	timeout := time.Duration(config.Seconds(cfg.ConnectTimeout, 3) * float64(time.Second))

	_ = client.SetReadDeadline(time.Now().Add(handshakeTimeout))
	br := bufio.NewReaderSize(client, 32*1024)
	first, err := br.ReadByte()
	if err != nil {
		_ = client.Close()
		return
	}
	_ = client.SetReadDeadline(time.Time{})

	switch first {
	case 0x05:
		handleSOCKS5(client, br, timeout, log, peer)
	case 0x04:
		log.Warnf("%s 使用 SOCKS4，direct 模式不支持", peer)
	default:
		handleHTTP(client, br, first, timeout, log, peer)
	}
	_ = client.Close()
}

// --------------------------------------------------------------------------
// HTTP 代理
// --------------------------------------------------------------------------

func handleHTTP(client net.Conn, br *bufio.Reader, first byte, timeout time.Duration,
	log *logging.Logger, peer string) {

	_ = client.SetReadDeadline(time.Now().Add(handshakeTimeout))
	head, err := readHead(br)
	_ = client.SetReadDeadline(time.Time{})
	if err != nil {
		log.Debugf("%s direct HTTP 请求无效: %v", peer, err)
		sendHTTPError(client, 400, "Bad Request")
		return
	}
	raw := append([]byte{first}, head...)
	method, target, version, headers, err := parseHead(raw)
	if err != nil {
		log.Debugf("%s direct HTTP 请求无效: %v", peer, err)
		sendHTTPError(client, 400, "Bad Request")
		return
	}

	if strings.EqualFold(method, "CONNECT") {
		host, port, err := config.SplitHostPortDefault(target, nil)
		if err != nil {
			log.Debugf("%s direct HTTP 请求无效: %v", peer, err)
			sendHTTPError(client, 400, "Bad Request")
			return
		}
		up, err := net.DialTimeout("tcp", net.JoinHostPort(host, strconv.Itoa(port)), timeout)
		if err != nil {
			log.Warnf("direct CONNECT %s:%d 失败: %v", host, port, err)
			sendHTTPError(client, 502, "Bad Gateway")
			return
		}
		relay.Tune(up)
		if _, err := client.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n")); err != nil {
			_ = up.Close()
			return
		}
		log.Debugf("%s -> direct CONNECT %s:%d", peer, host, port)
		relay.RelayReader(br, client, up)
		return
	}

	host, port, requestLine, err := originForm(method, target, version, headers)
	if err != nil {
		log.Debugf("%s direct HTTP 请求无效: %v", peer, err)
		sendHTTPError(client, 400, "Bad Request")
		return
	}

	// 逐跳头不发给源站；普通请求强制短连接，避免同一连接上切换目标。
	// 带 Upgrade 的请求（ws:// 等）保留连接语义。
	upgrade := strings.Contains(strings.ToLower(headerValue(headers, "connection")), "upgrade")
	drop := map[string]bool{
		"proxy-connection":    true,
		"proxy-authorization": true,
		"keep-alive":          true,
	}
	if !upgrade {
		drop["connection"] = true
	}
	lines := []string{requestLine}
	for _, h := range headers {
		if !drop[strings.ToLower(h.Name)] {
			lines = append(lines, h.Name+": "+h.Value)
		}
	}
	if headerValue(headers, "host") == "" {
		hostHdr := host
		if port != 80 {
			hostHdr = net.JoinHostPort(host, strconv.Itoa(port))
		}
		lines = append(lines, "Host: "+hostHdr)
	}
	if !upgrade {
		lines = append(lines, "Connection: close")
	}
	request := strings.Join(lines, "\r\n") + "\r\n\r\n"

	up, err := net.DialTimeout("tcp", net.JoinHostPort(host, strconv.Itoa(port)), timeout)
	if err != nil {
		log.Warnf("direct %s %s:%d 失败: %v", method, host, port, err)
		sendHTTPError(client, 502, "Bad Gateway")
		return
	}
	relay.Tune(up)
	if _, err := up.Write([]byte(request)); err != nil {
		_ = up.Close()
		sendHTTPError(client, 502, "Bad Gateway")
		return
	}
	log.Debugf("%s -> direct %s %s:%d", peer, method, host, port)
	relay.RelayReader(br, client, up)
}

// readHead 读到 \r\n\r\n，返回不含终止符的内容。
func readHead(br *bufio.Reader) ([]byte, error) {
	var buf []byte
	for {
		line, err := br.ReadBytes('\n')
		if err != nil {
			return nil, err
		}
		buf = append(buf, line...)
		if len(buf) > maxHeadBytes {
			return nil, badf("请求头过长")
		}
		if bytes.HasSuffix(buf, []byte("\r\n\r\n")) {
			return buf[:len(buf)-4], nil
		}
	}
}

func parseHead(raw []byte) (method, target, version string, headers []header, err error) {
	lines := strings.Split(string(raw), "\r\n")
	parts := strings.Split(lines[0], " ")
	if len(parts) != 3 || !strings.HasPrefix(parts[2], "HTTP/") {
		return "", "", "", nil, badf("请求行无法解析: %q", lines[0])
	}
	for _, line := range lines[1:] {
		if line == "" {
			continue
		}
		name, value, ok := strings.Cut(line, ":")
		if !ok {
			return "", "", "", nil, badf("请求头无法解析: %q", line)
		}
		headers = append(headers, header{
			Name:  strings.TrimSpace(name),
			Value: strings.TrimSpace(value),
		})
	}
	return parts[0], parts[1], parts[2], headers, nil
}

func headerValue(headers []header, key string) string {
	for _, h := range headers {
		if strings.EqualFold(h.Name, key) {
			return h.Value
		}
	}
	return ""
}

func originForm(method, target, version string, headers []header) (string, int, string, error) {
	switch {
	case strings.HasPrefix(target, "http://"):
		u, err := url.Parse(target)
		if err != nil || u.Hostname() == "" {
			return "", 0, "", badf("URL 缺少主机名: %q", target)
		}
		port := 80
		if p := u.Port(); p != "" {
			n, err := strconv.Atoi(p)
			if err != nil {
				return "", 0, "", badf("端口无效: %q", p)
			}
			port = n
		}
		path := u.EscapedPath()
		if path == "" {
			path = "/"
		}
		if u.RawQuery != "" {
			path += "?" + u.RawQuery
		}
		return u.Hostname(), port, method + " " + path + " " + version, nil
	case strings.HasPrefix(target, "https://"):
		return "", 0, "", badf("https 请求必须使用 CONNECT")
	case strings.HasPrefix(target, "/") || target == "*":
		hv := headerValue(headers, "host")
		if hv == "" {
			return "", 0, "", badf("请求缺少 Host 头")
		}
		def := 80
		host, port, err := config.SplitHostPortDefault(hv, &def)
		if err != nil {
			return "", 0, "", err
		}
		return host, port, method + " " + target + " " + version, nil
	default:
		return "", 0, "", badf("无法识别的请求目标: %q", target)
	}
}

func sendHTTPError(w io.Writer, code int, reason string) {
	_, _ = w.Write([]byte(fmt.Sprintf(
		"HTTP/1.1 %d %s\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
		code, reason)))
}

// --------------------------------------------------------------------------
// SOCKS5
// --------------------------------------------------------------------------

func handleSOCKS5(client net.Conn, br *bufio.Reader, timeout time.Duration,
	log *logging.Logger, peer string) {

	_ = client.SetReadDeadline(time.Now().Add(handshakeTimeout))

	n, err := br.ReadByte()
	if err != nil {
		return
	}
	methods := make([]byte, int(n))
	if _, err := io.ReadFull(br, methods); err != nil {
		return
	}
	if !bytes.Contains(methods, []byte{0x00}) {
		_, _ = client.Write([]byte{0x05, 0xff}) // 没有可接受的认证方式
		return
	}
	if _, err := client.Write([]byte{0x05, 0x00}); err != nil { // 无认证
		return
	}

	hdr := make([]byte, 4)
	if _, err := io.ReadFull(br, hdr); err != nil {
		return
	}
	if hdr[0] != 5 {
		log.Debugf("%s direct SOCKS5 版本错误: %d", peer, hdr[0])
		return
	}
	cmd, atyp := hdr[1], hdr[3]

	var host string
	switch atyp {
	case 0x01:
		b := make([]byte, 4)
		if _, err := io.ReadFull(br, b); err != nil {
			return
		}
		host = net.IP(b).String()
	case 0x03:
		size, err := br.ReadByte()
		if err != nil {
			return
		}
		b := make([]byte, int(size))
		if _, err := io.ReadFull(br, b); err != nil {
			return
		}
		for _, c := range b {
			if c >= 0x80 {
				log.Debugf("%s direct SOCKS5 域名不是 ASCII", peer)
				writeReply(client, 0x01, nil)
				return
			}
		}
		host = string(b)
	case 0x04:
		b := make([]byte, 16)
		if _, err := io.ReadFull(br, b); err != nil {
			return
		}
		host = net.IP(b).String()
	default:
		writeReply(client, 0x08, nil) // address type not supported
		return
	}

	pb := make([]byte, 2)
	if _, err := io.ReadFull(br, pb); err != nil {
		return
	}
	port := int(binary.BigEndian.Uint16(pb))

	if cmd != 0x01 { // 只支持 CONNECT
		writeReply(client, 0x07, nil) // command not supported
		return
	}

	up, err := net.DialTimeout("tcp", net.JoinHostPort(host, strconv.Itoa(port)), timeout)
	if err != nil {
		log.Warnf("direct SOCKS5 %s:%d 失败: %v", host, port, err)
		writeReply(client, socksErr(err), nil)
		return
	}
	relay.Tune(up)
	writeReply(client, 0x00, up.LocalAddr())
	log.Debugf("%s -> direct SOCKS5 %s:%d", peer, host, port)
	_ = client.SetReadDeadline(time.Time{})
	relay.RelayReader(br, client, up)
}

func writeReply(w io.Writer, rep byte, addr net.Addr) {
	ip := net.IPv4zero.To4()
	port := 0
	if tcp, ok := addr.(*net.TCPAddr); ok {
		ip = tcp.IP
		port = tcp.Port
	}
	var b []byte
	if v4 := ip.To4(); v4 != nil {
		b = append([]byte{0x05, rep, 0x00, 0x01}, v4...)
	} else {
		b = append([]byte{0x05, rep, 0x00, 0x04}, ip.To16()...)
	}
	b = append(b, byte(port>>8), byte(port))
	_, _ = w.Write(b)
}

func socksErr(err error) byte {
	var dnsErr *net.DNSError
	var ne net.Error
	switch {
	case errors.Is(err, syscall.ECONNREFUSED):
		return 0x05 // connection refused
	case errors.As(err, &dnsErr):
		return 0x04 // host unreachable
	case errors.As(err, &ne) && ne.Timeout():
		return 0x06 // TTL expired
	default:
		return 0x01 // general failure
	}
}

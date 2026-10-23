package direct

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"pswitch/internal/config"
	"pswitch/internal/logging"
)

func startEcho(t *testing.T) net.Listener {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				_, _ = io.Copy(c, c)
			}(c)
		}
	}()
	return ln
}

func startDirect(t *testing.T) string {
	t.Helper()
	t.Setenv("PSWITCH_HOME", t.TempDir())
	log, err := logging.Setup(false, true)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(log.Close)

	cfg := config.Default()
	cfg.ConnectTimeout = 2
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go Handle(c, &cfg, log)
		}
	}()
	return ln.Addr().String()
}

func dialDirect(t *testing.T, addr string) (net.Conn, *bufio.Reader) {
	t.Helper()
	c, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = c.Close() })
	return c, bufio.NewReader(c)
}

func echoRoundTrip(t *testing.T, conn net.Conn, r *bufio.Reader, payload string) {
	t.Helper()
	if _, err := conn.Write([]byte(payload)); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, len(payload))
	if _, err := io.ReadFull(r, buf); err != nil {
		t.Fatal(err)
	}
	if string(buf) != payload {
		t.Fatalf("echo = %q, want %q", buf, payload)
	}
}

func TestHTTPConnectTunnel(t *testing.T) {
	echo := startEcho(t)
	directAddr := startDirect(t)
	conn, r := dialDirect(t, directAddr)

	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n",
		echo.Addr().String(), echo.Addr().String())
	status, err := r.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if status != "HTTP/1.1 200 Connection Established\r\n" {
		t.Fatalf("status = %q", status)
	}
	if _, err := r.ReadString('\n'); err != nil { // 空行
		t.Fatal(err)
	}
	echoRoundTrip(t, conn, r, "hello-connect")
}

func TestPlainHTTPProxy(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/hello" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = io.WriteString(w, "ok")
	}))
	defer srv.Close()
	host, port, _ := config.SplitHostPort(srv.Listener.Addr().String())

	directAddr := startDirect(t)
	conn, r := dialDirect(t, directAddr)
	target := net.JoinHostPort(host, strconv.Itoa(port))
	req := fmt.Sprintf("GET http://%s/hello?x=1 HTTP/1.1\r\nHost: %s\r\nProxy-Connection: keep-alive\r\n\r\n",
		target, target)
	if _, err := conn.Write([]byte(req)); err != nil {
		t.Fatal(err)
	}
	resp, err := http.ReadResponse(r, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 || string(body) != "ok" {
		t.Fatalf("status=%d body=%q", resp.StatusCode, body)
	}
	if !resp.Close {
		t.Fatal("plain proxy request should force Connection: close")
	}
}

func TestSOCKS5Connect(t *testing.T) {
	echo := startEcho(t)
	directAddr := startDirect(t)
	conn, r := dialDirect(t, directAddr)

	if _, err := conn.Write([]byte{0x05, 0x01, 0x00}); err != nil {
		t.Fatal(err)
	}
	reply := make([]byte, 2)
	if _, err := io.ReadFull(r, reply); err != nil {
		t.Fatal(err)
	}
	if reply[0] != 0x05 || reply[1] != 0x00 {
		t.Fatalf("method reply = %v", reply)
	}

	host, port, _ := config.SplitHostPort(echo.Addr().String())
	ip := net.ParseIP(host).To4()
	req := []byte{0x05, 0x01, 0x00, 0x01}
	req = append(req, ip...)
	req = binary.BigEndian.AppendUint16(req, uint16(port))
	if _, err := conn.Write(req); err != nil {
		t.Fatal(err)
	}

	head := make([]byte, 4)
	if _, err := io.ReadFull(r, head); err != nil {
		t.Fatal(err)
	}
	if head[0] != 0x05 || head[1] != 0x00 || head[3] != 0x01 {
		t.Fatalf("connect reply head = %v", head)
	}
	rest := make([]byte, 6) // IPv4 + port
	if _, err := io.ReadFull(r, rest); err != nil {
		t.Fatal(err)
	}
	echoRoundTrip(t, conn, r, "hello-socks5")
}

func TestSOCKS5RejectsBind(t *testing.T) {
	directAddr := startDirect(t)
	conn, r := dialDirect(t, directAddr)

	_, _ = conn.Write([]byte{0x05, 0x01, 0x00})
	_, _ = io.ReadFull(r, make([]byte, 2))
	// BIND 命令，地址 127.0.0.1:1
	req := []byte{0x05, 0x02, 0x00, 0x01, 127, 0, 0, 1, 0, 1}
	_, _ = conn.Write(req)

	head := make([]byte, 4)
	if _, err := io.ReadFull(r, head); err != nil {
		t.Fatal(err)
	}
	if head[1] != 0x07 { // command not supported
		t.Fatalf("BIND reply = %v", head)
	}
}

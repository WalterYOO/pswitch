// Package relay 负责双向字节搬运。
//
// 设计目标：单连接零多余分配、支持半关闭、两个方向各用一条 goroutine。
// 64 KiB 缓冲区来自 sync.Pool，连接结束后归还。
package relay

import (
	"io"
	"net"
	"sync"
	"time"
)

const bufferSize = 64 * 1024

var pool = sync.Pool{
	New: func() any {
		b := make([]byte, bufferSize)
		return &b
	},
}

// Tune 为转发连接设置低延迟选项：关闭 Nagle、开启 keepalive。
func Tune(c net.Conn) {
	if tcp, ok := c.(*net.TCPConn); ok {
		_ = tcp.SetNoDelay(true)
		_ = tcp.SetKeepAlive(true)
		_ = tcp.SetKeepAlivePeriod(30 * time.Second)
	}
}

// Copy 用一个池化缓冲区把 src 拷到 dst。
func Copy(dst io.Writer, src io.Reader) error {
	bufp := pool.Get().(*[]byte)
	defer pool.Put(bufp)
	_, err := io.CopyBuffer(dst, src, *bufp)
	return err
}

// halfClose 在支持 CloseWrite 的连接上发送 FIN，让对端知道本方向结束。
func halfClose(c net.Conn) {
	if cw, ok := c.(interface{ CloseWrite() error }); ok {
		_ = cw.CloseWrite()
		return
	}
	_ = c.Close()
}

// Relay 在两条 TCP 连接之间双向转发，直到两个方向都结束。
func Relay(a, b net.Conn) {
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		_ = Copy(b, a)
		halfClose(b)
	}()
	go func() {
		defer wg.Done()
		_ = Copy(a, b)
		halfClose(a)
	}()
	wg.Wait()
	_ = a.Close()
	_ = b.Close()
}

// RelayReader 用于 direct 模式：客户端侧已经套了 bufio.Reader，
// 其中可能残留握手后立即到达的字节，必须从同一个 reader 继续读。
func RelayReader(clientSrc io.Reader, client, upstream net.Conn) {
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		_ = Copy(upstream, clientSrc)
		halfClose(upstream)
	}()
	go func() {
		defer wg.Done()
		_ = Copy(client, upstream)
		halfClose(client)
	}()
	wg.Wait()
	_ = client.Close()
	_ = upstream.Close()
}

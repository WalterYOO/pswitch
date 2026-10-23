// pswitch 是代理聚合器的 Go 实现：一个本地端口，多个上游代理。
package main

import (
	"os"

	"pswitch/internal/cli"
)

func main() {
	os.Exit(cli.Main(os.Args[1:]))
}

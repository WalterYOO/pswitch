package cli

import (
	"fmt"
	"strings"

	"pswitch/internal/completion"
)

func cmdCompletion(args []string) int {
	var shell string
	var printOnly, uninstall bool

	for _, arg := range args {
		switch arg {
		case "-h", "--help", "help":
			completionUsage()
			return 0
		case "-p", "--print":
			printOnly = true
		case "--uninstall":
			uninstall = true
		default:
			if strings.HasPrefix(arg, "-") {
				fmt.Printf("未知选项: %s\n", arg)
				return 2
			}
			if shell != "" {
				fmt.Printf("completion 只接受一个 shell 参数，收到多余: %s\n", arg)
				return 2
			}
			shell = arg
		}
	}

	return completion.Run(shell, printOnly, uninstall)
}

func completionUsage() {
	fmt.Print(`usage: pswitch completion [zsh|bash|fish] [--print] [--uninstall]

安装 shell 补全。省略 shell 时按 $SHELL 自动识别。

  --print       只把补全脚本打印到标准输出
  --uninstall   移除已安装的补全（省略 shell 时清理全部）
`)
}

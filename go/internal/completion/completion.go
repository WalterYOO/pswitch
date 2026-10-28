// Package completion 安装 pswitch 的 shell 补全（zsh / bash / fish）。
//
// 三个实现（Python / Go / Rust）生成同一份脚本，只依赖 `pswitch list`
// 的稳定输出，因此 `pswitch use <TAB>` 在不同实现下行为一致。
package completion

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"pswitch/internal/config"
)

const (
	markBegin = "# >>> pswitch completion >>>"
	markEnd   = "# <<< pswitch completion <<<"
)

var shells = []string{"zsh", "bash", "fish"}

const zshScript = `#compdef pswitch
# pswitch 的 zsh 补全，由 ` + "`pswitch completion zsh`" + ` 生成。

_pswitch_upstreams() {
  local -a names
  names=("${(@f)$(pswitch list 2>/dev/null | awk '$1 == "*" {print $3; next} $1 ~ /^[0-9]+\.$/ {print $2}')}")
  names=(${names:#})
  (( ${#names} )) || return 1
  _describe -t upstreams '上游' names
}

_pswitch() {
  local -a commands
  commands=(
    'start:启动转发守护进程'
    'stop:停止守护进程'
    'restart:重启守护进程'
    'status:查看状态（含实时可达性）'
    'use:切换出口（名字 | auto | direct）'
    'list:列出上游'
    'add:添加上游 <名字> <host:port>'
    'remove:删除上游 <名字>'
    'priority:查看/调整 auto 选路顺序'
    'enable:启用上游 <名字>'
    'disable:禁用上游 <名字>'
    'env:打印应设置的代理环境变量'
    'test:经本地端口做一次真实请求'
    'logs:查看最近日志'
    'install:安装为 launchd 开机自启'
    'uninstall:卸载开机自启'
    'system:管理 macOS 系统代理指向'
    'completion:安装 shell 补全'
    'version:查看版本号'
  )

  _arguments -C \
    '(-h --help)'{-h,--help}'[显示帮助]' \
    '(-V --version)'{-V,--version}'[查看版本号]' \
    '1:命令:->command' \
    '*::参数:->args'

  case $state in
    command)
      _describe -t commands 'pswitch 命令' commands
      ;;
    args)
      case $words[1] in
        use)
          _pswitch_upstreams
          _values '特殊目标' auto direct
          ;;
        remove|enable|disable)
          _pswitch_upstreams
          ;;
        priority)
          if (( CURRENT == 2 )); then
            _pswitch_upstreams
          elif (( CURRENT == 3 )); then
            _values '位置' 1 2 3 4 5 6 7 8 9 10
          fi
          ;;
        test)
          _arguments '--timeout=[超时秒数]:秒数:'
          ;;
        logs)
          _arguments '(-n --lines)'{-n,--lines}'=[行数]:行数:'
          ;;
        system)
          if (( CURRENT == 2 )); then
            _values '动作' on off status use
          elif [[ $words[2] == use ]] && (( CURRENT == 3 )); then
            _pswitch_upstreams
          fi
          ;;
        completion)
          _values 'shell' zsh bash fish
          _arguments '--print[只打印补全脚本]' '--uninstall[移除已安装的补全]'
          ;;
      esac
      ;;
  esac
}

_pswitch "$@"
`

const bashScript = `# pswitch 的 bash 补全，由 ` + "`pswitch completion bash`" + ` 生成。
# 需要在本文件，或在 bash 启动文件里 source 本文件。

_pswitch_upstreams() {
    pswitch list 2>/dev/null | awk '$1 == "*" {print $3; next} $1 ~ /^[0-9]+\.$/ {print $2}'
}

_pswitch() {
    local cur cmd i names
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"

    cmd=""
    for ((i = 1; i < COMP_CWORD; i++)); do
        case "${COMP_WORDS[i]}" in
            -*) ;;
            *)
                cmd="${COMP_WORDS[i]}"
                break
                ;;
        esac
    done

    if [[ -z "$cmd" ]]; then
        COMPREPLY=($(compgen -W "start stop restart status use list add remove priority enable disable env test logs install uninstall system completion version" -- "$cur"))
        return
    fi

    case "$cmd" in
        use)
            names="$(_pswitch_upstreams)"
            COMPREPLY=($(compgen -W "auto direct $names" -- "$cur"))
            ;;
        remove|enable|disable)
            names="$(_pswitch_upstreams)"
            COMPREPLY=($(compgen -W "$names" -- "$cur"))
            ;;
        priority)
            if [[ $COMP_CWORD -eq 2 ]]; then
                names="$(_pswitch_upstreams)"
                COMPREPLY=($(compgen -W "$names" -- "$cur"))
            elif [[ $COMP_CWORD -eq 3 ]]; then
                COMPREPLY=($(compgen -W "1 2 3 4 5 6 7 8 9 10" -- "$cur"))
            fi
            ;;
        system)
            if [[ $COMP_CWORD -eq 2 ]]; then
                COMPREPLY=($(compgen -W "on off status use" -- "$cur"))
            elif [[ $COMP_CWORD -eq 3 && ${COMP_WORDS[2]} == use ]]; then
                names="$(_pswitch_upstreams)"
                COMPREPLY=($(compgen -W "$names" -- "$cur"))
            fi
            ;;
        test)
            COMPREPLY=($(compgen -W "--timeout" -- "$cur"))
            ;;
        logs)
            COMPREPLY=($(compgen -W "-n --lines" -- "$cur"))
            ;;
        completion)
            COMPREPLY=($(compgen -W "zsh bash fish --print --uninstall" -- "$cur"))
            ;;
    esac
}

complete -F _pswitch pswitch
`

const fishScript = `# pswitch 的 fish 补全，由 ` + "`pswitch completion fish`" + ` 生成。

function __pswitch_upstreams
    pswitch list 2>/dev/null | awk '$1 == "*" {print $3; next} $1 ~ /^[0-9]+\.$/ {print $2}'
end

complete -c pswitch -f

# 全局选项
complete -c pswitch -s h -l help -d '显示帮助'
complete -c pswitch -s V -l version -d '查看版本号'

# 子命令
complete -c pswitch -n '__fish_use_subcommand' -a start -d '启动转发守护进程'
complete -c pswitch -n '__fish_use_subcommand' -a stop -d '停止守护进程'
complete -c pswitch -n '__fish_use_subcommand' -a restart -d '重启守护进程'
complete -c pswitch -n '__fish_use_subcommand' -a status -d '查看状态（含实时可达性）'
complete -c pswitch -n '__fish_use_subcommand' -a use -d '切换出口（名字 | auto | direct）'
complete -c pswitch -n '__fish_use_subcommand' -a list -d '列出上游'
complete -c pswitch -n '__fish_use_subcommand' -a add -d '添加上游 <名字> <host:port>'
complete -c pswitch -n '__fish_use_subcommand' -a remove -d '删除上游 <名字>'
complete -c pswitch -n '__fish_use_subcommand' -a priority -d '查看/调整 auto 选路顺序'
complete -c pswitch -n '__fish_use_subcommand' -a enable -d '启用上游 <名字>'
complete -c pswitch -n '__fish_use_subcommand' -a disable -d '禁用上游 <名字>'
complete -c pswitch -n '__fish_use_subcommand' -a env -d '打印应设置的代理环境变量'
complete -c pswitch -n '__fish_use_subcommand' -a test -d '经本地端口做一次真实请求'
complete -c pswitch -n '__fish_use_subcommand' -a logs -d '查看最近日志'
complete -c pswitch -n '__fish_use_subcommand' -a install -d '安装为 launchd 开机自启'
complete -c pswitch -n '__fish_use_subcommand' -a uninstall -d '卸载开机自启'
complete -c pswitch -n '__fish_use_subcommand' -a system -d '管理 macOS 系统代理指向'
complete -c pswitch -n '__fish_use_subcommand' -a completion -d '安装 shell 补全'
complete -c pswitch -n '__fish_use_subcommand' -a version -d '查看版本号'

# 参数补全
complete -c pswitch -n '__fish_seen_subcommand_from use' -a '(__pswitch_upstreams)' -d '上游'
complete -c pswitch -n '__fish_seen_subcommand_from use' -a 'auto direct' -d '出口'
complete -c pswitch -n '__fish_seen_subcommand_from remove enable disable' -a '(__pswitch_upstreams)' -d '上游'
complete -c pswitch -n '__fish_seen_subcommand_from priority; and test (count (commandline -opc)) -eq 2' -a '(__pswitch_upstreams)' -d '上游'
complete -c pswitch -n '__fish_seen_subcommand_from priority; and test (count (commandline -opc)) -eq 3' -a '1 2 3 4 5 6 7 8 9 10' -d '位置'
complete -c pswitch -n '__fish_seen_subcommand_from test' -l timeout -d '超时秒数' -r
complete -c pswitch -n '__fish_seen_subcommand_from logs' -s n -l lines -d '行数' -r
complete -c pswitch -n '__fish_seen_subcommand_from system; and test (count (commandline -opc)) -eq 2' -a 'on off status use' -d '动作'
complete -c pswitch -n '__fish_seen_subcommand_from system; and test (count (commandline -opc)) -eq 3; and test (commandline -opc)[3] = use' -a '(__pswitch_upstreams)' -d '上游'
complete -c pswitch -n '__fish_seen_subcommand_from completion' -a 'zsh bash fish' -d 'shell'
complete -c pswitch -n '__fish_seen_subcommand_from completion' -l print -d '只打印补全脚本'
complete -c pswitch -n '__fish_seen_subcommand_from completion' -l uninstall -d '移除已安装的补全'
`

var scripts = map[string]string{
	"zsh":  zshScript,
	"bash": bashScript,
	"fish": fishScript,
}

// Run 是 completion 子命令的入口，返回退出码。
func Run(shell string, printOnly, uninstall bool) int {
	if printOnly && uninstall {
		fmt.Println("--print 与 --uninstall 不能同时使用")
		return 2
	}

	explicit, ok := normalize(shell)
	if shell != "" && !ok {
		fmt.Printf("不支持的 shell: %s\n", shell)
		fmt.Println("可选: zsh / bash / fish")
		return 2
	}

	if printOnly {
		target := explicit
		if target == "" {
			target = detectShell()
		}
		if target == "" {
			fmt.Println("无法从 $SHELL 识别目标，请显式指定: zsh | bash | fish")
			return 2
		}
		fmt.Print(scripts[target])
		return 0
	}

	if uninstall {
		targets := shells
		if explicit != "" {
			targets = []string{explicit}
		}
		removed := false
		for _, s := range targets {
			if uninstallShell(s) {
				removed = true
			}
		}
		if !removed {
			fmt.Println("没有找到已安装的补全")
		}
		return 0
	}

	target := explicit
	if target == "" {
		target = detectShell()
	}
	if target == "" {
		fmt.Println("无法从 $SHELL 识别目标，请显式指定: zsh | bash | fish")
		return 2
	}
	return installShell(target)
}

// --------------------------------------------------------------------------
// 路径
// --------------------------------------------------------------------------

// normalize 把 "/bin/zsh"、"-zsh" 之类的输入归一成 zsh / bash / fish。
func normalize(shell string) (string, bool) {
	name := filepath.Base(strings.TrimPrefix(shell, "-"))
	for _, s := range shells {
		if name == s {
			return s, true
		}
	}
	return "", false
}

func detectShell() string {
	name, ok := normalize(os.Getenv("SHELL"))
	if !ok {
		return ""
	}
	return name
}

func realHome() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return "."
	}
	return home
}

func fishConfigDir() string {
	if v := os.Getenv("XDG_CONFIG_HOME"); v != "" {
		return filepath.Join(v, "fish")
	}
	return filepath.Join(realHome(), ".config", "fish")
}

func scriptPath(shell string) string {
	switch shell {
	case "fish":
		return filepath.Join(fishConfigDir(), "completions", "pswitch.fish")
	case "zsh":
		return filepath.Join(config.Home(), "completions", "_pswitch")
	default:
		return filepath.Join(config.Home(), "completions", "pswitch.bash")
	}
}

func rcPath(shell string) string {
	home := realHome()
	if shell == "zsh" {
		return filepath.Join(home, ".zshrc")
	}
	for _, name := range []string{".bashrc", ".bash_profile"} {
		path := filepath.Join(home, name)
		if _, err := os.Stat(path); err == nil {
			return path
		}
	}
	// macOS 的 Terminal 默认启动登录 shell，读的是 .bash_profile
	if runtime.GOOS == "darwin" {
		return filepath.Join(home, ".bash_profile")
	}
	return filepath.Join(home, ".bashrc")
}

// --------------------------------------------------------------------------
// 启动文件里的补全区块
// --------------------------------------------------------------------------

func quote(path string) string {
	return "'" + strings.ReplaceAll(path, "'", `'\''`) + "'"
}

func block(shell, path string) string {
	var body string
	if shell == "zsh" {
		body = "fpath=(" + quote(filepath.Dir(path)) + " $fpath)\nautoload -Uz compinit && compinit -i"
	} else {
		quoted := quote(path)
		body = "[ -f " + quoted + " ] && . " + quoted
	}
	return markBegin + "\n" + body + "\n" + markEnd + "\n"
}

func stripBlock(text string) string {
	var out strings.Builder
	skipping := false
	for _, line := range strings.SplitAfter(text, "\n") {
		if line == "" {
			continue
		}
		trimmed := strings.TrimSpace(line)
		if trimmed == markBegin {
			skipping = true
			continue
		}
		if skipping {
			if trimmed == markEnd {
				skipping = false
			}
			continue
		}
		out.WriteString(line)
	}
	return out.String()
}

// setBlock 写入或更新 rc 里的补全区块，返回是否改动。
func setBlock(rc, block string) (bool, error) {
	data, err := os.ReadFile(rc)
	if err != nil && !os.IsNotExist(err) {
		return false, err
	}
	old := string(data)

	updated := strings.TrimRight(stripBlock(old), "\n")
	if updated != "" {
		updated += "\n\n"
	}
	updated += block
	if updated == old {
		return false, nil
	}
	if err := os.MkdirAll(filepath.Dir(rc), 0o755); err != nil {
		return false, err
	}
	return true, os.WriteFile(rc, []byte(updated), 0o644)
}

func removeBlock(rc string) (bool, error) {
	data, err := os.ReadFile(rc)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, err
	}
	old := string(data)
	updated := strings.TrimRight(stripBlock(old), "\n")
	if updated != "" {
		updated += "\n"
	}
	if updated == old {
		return false, nil
	}
	return true, os.WriteFile(rc, []byte(updated), 0o644)
}

// --------------------------------------------------------------------------
// 安装 / 卸载
// --------------------------------------------------------------------------

func installShell(shell string) int {
	path := scriptPath(shell)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		fmt.Printf("创建目录失败: %v\n", err)
		return 1
	}
	current, err := os.ReadFile(path)
	if err != nil || string(current) != scripts[shell] {
		if err := os.WriteFile(path, []byte(scripts[shell]), 0o644); err != nil {
			fmt.Printf("写入失败: %v\n", err)
			return 1
		}
		fmt.Printf("已写入: %s\n", path)
	} else {
		fmt.Printf("已是最新: %s\n", path)
	}

	if shell == "fish" {
		fmt.Println("fish 会自动加载该目录，重开终端生效。")
		return 0
	}

	rc := rcPath(shell)
	updated, err := setBlock(rc, block(shell, path))
	if err != nil {
		fmt.Printf("更新 %s 失败: %v\n", rc, err)
		return 1
	}
	if updated {
		fmt.Printf("已更新: %s（pswitch completion 区块）\n", rc)
	} else {
		fmt.Printf("已配置: %s（补全区块已存在）\n", rc)
	}
	if shell == "zsh" {
		fmt.Println("重开终端生效，也可以执行 exec zsh。")
	} else {
		fmt.Println("重开终端生效，也可以执行 exec bash。")
	}
	return 0
}

func uninstallShell(shell string) bool {
	removed := false
	path := scriptPath(shell)
	if _, err := os.Stat(path); err == nil {
		if err := os.Remove(path); err == nil {
			fmt.Printf("已移除: %s\n", path)
			removed = true
		}
	}
	if shell == "fish" {
		return removed
	}
	rc := rcPath(shell)
	if updated, err := removeBlock(rc); err == nil && updated {
		fmt.Printf("已更新: %s（移除补全区块）\n", rc)
		removed = true
	}
	return removed
}

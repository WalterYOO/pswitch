"""安装 pswitch 的 shell 补全（zsh / bash / fish）。

补全脚本只依赖 `pswitch list` 的稳定输出，不区分 Python / Go / Rust 实现：
三个版本生成同一份脚本，`pswitch use <TAB>` 都能补出当前配置里的上游名字。
"""

import os
import sys
from pathlib import Path

from .config import HOME as CONFIG_HOME

SHELLS = ("zsh", "bash", "fish")

MARK_BEGIN = "# >>> pswitch completion >>>"
MARK_END = "# <<< pswitch completion <<<"

ZSH_SCRIPT = r"""#compdef pswitch
# pswitch 的 zsh 补全，由 `pswitch completion zsh` 生成。

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
    'lan:管理 LAN 监听与允许来源'
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
        lan)
          if (( CURRENT == 2 )); then
            _values '动作' on off allow deny status
          elif [[ $words[2] == allow ]] && (( CURRENT == 3 )); then
            _values '选项' --clear
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
"""

BASH_SCRIPT = r"""# pswitch 的 bash 补全，由 `pswitch completion bash` 生成。
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
        COMPREPLY=($(compgen -W "start stop restart status use list add remove priority enable disable env test logs install uninstall system lan completion version" -- "$cur"))
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
        lan)
            if [[ $COMP_CWORD -eq 2 ]]; then
                COMPREPLY=($(compgen -W "on off allow deny status" -- "$cur"))
            elif [[ $COMP_CWORD -eq 3 && ${COMP_WORDS[2]} == allow ]]; then
                COMPREPLY=($(compgen -W "--clear" -- "$cur"))
            fi
            ;;
        completion)
            COMPREPLY=($(compgen -W "zsh bash fish --print --uninstall" -- "$cur"))
            ;;
    esac
}

complete -F _pswitch pswitch
"""

FISH_SCRIPT = r"""# pswitch 的 fish 补全，由 `pswitch completion fish` 生成。

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
complete -c pswitch -n '__fish_use_subcommand' -a lan -d '管理 LAN 监听与允许来源'
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
complete -c pswitch -n '__fish_seen_subcommand_from lan; and test (count (commandline -opc)) -eq 2' -a 'on off allow deny status' -d '动作'
complete -c pswitch -n '__fish_seen_subcommand_from lan; and test (count (commandline -opc)) -eq 3; and test (commandline -opc)[3] = allow' -l clear -d '清空允许来源'
complete -c pswitch -n '__fish_seen_subcommand_from completion' -a 'zsh bash fish' -d 'shell'
complete -c pswitch -n '__fish_seen_subcommand_from completion' -l print -d '只打印补全脚本'
complete -c pswitch -n '__fish_seen_subcommand_from completion' -l uninstall -d '移除已安装的补全'
"""

SCRIPTS = {"zsh": ZSH_SCRIPT, "bash": BASH_SCRIPT, "fish": FISH_SCRIPT}


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def normalize_shell(value):
    """把 "/bin/zsh"、"-zsh" 之类的输入归一成 zsh / bash / fish。"""
    if not value:
        return None
    name = os.path.basename(value).lstrip("-")
    return name if name in SHELLS else None


def detect_shell():
    return normalize_shell(os.environ.get("SHELL"))


def _fish_config_dir():
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "fish"
    return Path.home() / ".config" / "fish"


def script_path(shell):
    if shell == "fish":
        return _fish_config_dir() / "completions" / "pswitch.fish"
    name = "_pswitch" if shell == "zsh" else "pswitch.bash"
    return CONFIG_HOME / "completions" / name


def rc_path(shell):
    home = Path.home()
    if shell == "zsh":
        return home / ".zshrc"
    for name in (".bashrc", ".bash_profile"):
        path = home / name
        if path.exists():
            return path
    # macOS 的 Terminal 默认启动登录 shell，读的是 .bash_profile
    return home / (".bash_profile" if sys.platform == "darwin" else ".bashrc")


# --------------------------------------------------------------------------
# 启动文件里的补全区块
# --------------------------------------------------------------------------

def _read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        return ""


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="surrogateescape")


def _quote(path):
    return "'" + str(path).replace("'", "'\\''") + "'"


def _block(shell, path):
    if shell == "zsh":
        body = (f"fpath=({_quote(path.parent)} $fpath)\n"
                "autoload -Uz compinit && compinit -i")
    else:
        quoted = _quote(path)
        body = f"[ -f {quoted} ] && . {quoted}"
    return f"{MARK_BEGIN}\n{body}\n{MARK_END}\n"


def _strip_block(text):
    lines, skipping = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == MARK_BEGIN:
            skipping = True
            continue
        if skipping:
            if stripped == MARK_END:
                skipping = False
            continue
        lines.append(line)
    return "".join(lines)


def _set_block(rc, block):
    """写入或更新 rc 里的补全区块，返回是否改动。"""
    old = _read_text(rc)
    new = _strip_block(old).rstrip("\n")
    if new:
        new += "\n\n"
    new += block
    if new == old:
        return False
    _write_text(rc, new)
    return True


def _remove_block(rc):
    old = _read_text(rc)
    new = _strip_block(old).rstrip("\n")
    if new:
        new += "\n"
    if new == old:
        return False
    _write_text(rc, new)
    return True


# --------------------------------------------------------------------------
# 安装 / 卸载
# --------------------------------------------------------------------------

def install(shell):
    script = SCRIPTS[shell]
    path = script_path(shell)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _read_text(path) != script:
        _write_text(path, script)
        print(f"已写入: {path}")
    else:
        print(f"已是最新: {path}")

    if shell == "fish":
        print("fish 会自动加载该目录，重开终端生效。")
        return 0

    rc = rc_path(shell)
    if _set_block(rc, _block(shell, path)):
        print(f"已更新: {rc}（pswitch completion 区块）")
    else:
        print(f"已配置: {rc}（补全区块已存在）")
    reload = "exec zsh" if shell == "zsh" else "exec bash"
    print(f"重开终端生效，也可以执行 {reload}。")
    return 0


def uninstall(shell):
    removed = False
    path = script_path(shell)
    if path.exists():
        path.unlink()
        print(f"已移除: {path}")
        removed = True
    if shell != "fish":
        rc = rc_path(shell)
        if _remove_block(rc):
            print(f"已更新: {rc}（移除补全区块）")
            removed = True
    return removed


def run(shell=None, print_only=False, uninstall_mode=False):
    """completion 子命令的入口，返回退出码。"""
    if print_only and uninstall_mode:
        print("--print 与 --uninstall 不能同时使用")
        return 2

    explicit = normalize_shell(shell)
    if shell and not explicit:
        print(f"不支持的 shell: {shell}")
        print("可选: zsh / bash / fish")
        return 2

    if print_only:
        target = explicit or detect_shell()
        if not target:
            print("无法从 $SHELL 识别目标，请显式指定: zsh | bash | fish")
            return 2
        sys.stdout.write(SCRIPTS[target])
        return 0

    if uninstall_mode:
        targets = [explicit] if explicit else list(SHELLS)
        removed = False
        for target in targets:
            removed = uninstall(target) or removed
        if not removed:
            print("没有找到已安装的补全")
        return 0

    target = explicit or detect_shell()
    if not target:
        print("无法从 $SHELL 识别目标，请显式指定: zsh | bash | fish")
        return 2
    return install(target)

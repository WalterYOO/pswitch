//! 安装 pswitch 的 shell 补全（zsh / bash / fish）。
//!
//! 三个实现（Python / Go / Rust）生成同一份脚本，只依赖 `pswitch list`
//! 的稳定输出，因此 `pswitch use <TAB>` 在不同实现下行为一致。

use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use crate::config;

const SHELLS: [&str; 3] = ["zsh", "bash", "fish"];
const MARK_BEGIN: &str = "# >>> pswitch completion >>>";
const MARK_END: &str = "# <<< pswitch completion <<<";

const ZSH_SCRIPT: &str = r#"#compdef pswitch
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
"#;

const BASH_SCRIPT: &str = r#"# pswitch 的 bash 补全，由 `pswitch completion bash` 生成。
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
"#;

const FISH_SCRIPT: &str = r#"# pswitch 的 fish 补全，由 `pswitch completion fish` 生成。

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
"#;

// --------------------------------------------------------------------------
// 入口
// --------------------------------------------------------------------------

/// `completion` 子命令的入口，返回退出码。
pub fn run(shell: Option<&str>, print_only: bool, uninstall: bool) -> i32 {
    if print_only && uninstall {
        println!("--print 与 --uninstall 不能同时使用");
        return 2;
    }

    let explicit = match shell {
        Some(value) => match normalize(value) {
            Some(name) => Some(name),
            None => {
                println!("不支持的 shell: {value}");
                println!("可选: zsh / bash / fish");
                return 2;
            }
        },
        None => None,
    };

    if print_only {
        return match explicit.or_else(detect_shell) {
            Some(name) => {
                print!("{}", script(name));
                0
            }
            None => {
                println!("无法从 $SHELL 识别目标，请显式指定: zsh | bash | fish");
                2
            }
        };
    }

    if uninstall {
        let removed = match explicit {
            Some(name) => uninstall_shell(name),
            None => SHELLS
                .iter()
                .fold(false, |acc, name| uninstall_shell(name) || acc),
        };
        if !removed {
            println!("没有找到已安装的补全");
        }
        return 0;
    }

    match explicit.or_else(detect_shell) {
        Some(name) => install_shell(name),
        None => {
            println!("无法从 $SHELL 识别目标，请显式指定: zsh | bash | fish");
            2
        }
    }
}

// --------------------------------------------------------------------------
// 路径
// --------------------------------------------------------------------------

/// 把 "/bin/zsh"、"-zsh" 之类的输入归一成 zsh / bash / fish。
fn normalize(shell: &str) -> Option<&'static str> {
    let name = shell.trim_start_matches('-');
    let name = Path::new(name)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or(name);
    SHELLS.iter().copied().find(|s| *s == name)
}

fn detect_shell() -> Option<&'static str> {
    std::env::var("SHELL").ok().and_then(|s| normalize(&s))
}

fn real_home() -> PathBuf {
    match std::env::var("HOME") {
        Ok(home) if !home.is_empty() => PathBuf::from(home),
        _ => PathBuf::from("."),
    }
}

fn fish_config_dir() -> PathBuf {
    match std::env::var("XDG_CONFIG_HOME") {
        Ok(dir) if !dir.is_empty() => PathBuf::from(dir).join("fish"),
        _ => real_home().join(".config").join("fish"),
    }
}

fn script_path(shell: &str) -> PathBuf {
    match shell {
        "fish" => fish_config_dir().join("completions").join("pswitch.fish"),
        "zsh" => config::home().join("completions").join("_pswitch"),
        _ => config::home().join("completions").join("pswitch.bash"),
    }
}

fn rc_path(shell: &str) -> PathBuf {
    let home = real_home();
    if shell == "zsh" {
        return home.join(".zshrc");
    }
    for name in [".bashrc", ".bash_profile"] {
        if home.join(name).exists() {
            return home.join(name);
        }
    }
    // macOS 的 Terminal 默认启动登录 shell，读的是 .bash_profile
    if cfg!(target_os = "macos") {
        home.join(".bash_profile")
    } else {
        home.join(".bashrc")
    }
}

fn script(shell: &str) -> &'static str {
    match shell {
        "zsh" => ZSH_SCRIPT,
        "bash" => BASH_SCRIPT,
        _ => FISH_SCRIPT,
    }
}

// --------------------------------------------------------------------------
// 启动文件里的补全区块
// --------------------------------------------------------------------------

fn quote(path: &Path) -> String {
    format!("'{}'", path.display().to_string().replace('\'', "'\\''"))
}

fn block(shell: &str, path: &Path) -> String {
    let body = if shell == "zsh" {
        let dir = path.parent().unwrap_or_else(|| Path::new("."));
        format!(
            "fpath=({} $fpath)\nautoload -Uz compinit && compinit -i",
            quote(dir)
        )
    } else {
        let quoted = quote(path);
        format!("[ -f {quoted} ] && . {quoted}")
    };
    format!("{MARK_BEGIN}\n{body}\n{MARK_END}\n")
}

fn strip_block(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut skipping = false;
    for line in text.split_inclusive('\n') {
        let trimmed = line.trim();
        if trimmed == MARK_BEGIN {
            skipping = true;
            continue;
        }
        if skipping {
            if trimmed == MARK_END {
                skipping = false;
            }
            continue;
        }
        out.push_str(line);
    }
    out
}

fn read_rc(rc: &Path) -> Result<String, String> {
    match fs::read(rc) {
        Ok(bytes) => {
            String::from_utf8(bytes).map_err(|_| format!("{} 不是 UTF-8，拒绝修改", rc.display()))
        }
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(String::new()),
        Err(e) => Err(format!("{}: {e}", rc.display())),
    }
}

/// 写入或更新 rc 里的补全区块，返回是否改动。
fn set_block(rc: &Path, block: &str) -> Result<bool, String> {
    let old = read_rc(rc)?;
    let mut updated = strip_block(&old).trim_end_matches('\n').to_string();
    if !updated.is_empty() {
        updated.push_str("\n\n");
    }
    updated.push_str(block);
    if updated == old {
        return Ok(false);
    }
    if let Some(parent) = rc.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("{}: {e}", parent.display()))?;
    }
    fs::write(rc, updated).map_err(|e| format!("{}: {e}", rc.display()))?;
    Ok(true)
}

fn remove_block(rc: &Path) -> bool {
    let old = match read_rc(rc) {
        Ok(text) => text,
        Err(_) => return false,
    };
    let mut updated = strip_block(&old).trim_end_matches('\n').to_string();
    if !updated.is_empty() {
        updated.push('\n');
    }
    if updated == old {
        return false;
    }
    fs::write(rc, updated).is_ok()
}

// --------------------------------------------------------------------------
// 安装 / 卸载
// --------------------------------------------------------------------------

fn install_shell(shell: &str) -> i32 {
    let path = script_path(shell);
    if let Some(parent) = path.parent() {
        if let Err(e) = fs::create_dir_all(parent) {
            println!("创建目录失败: {e}");
            return 1;
        }
    }
    let current = fs::read_to_string(&path).unwrap_or_default();
    if current != script(shell) {
        if let Err(e) = fs::write(&path, script(shell)) {
            println!("写入失败: {e}");
            return 1;
        }
        println!("已写入: {}", path.display());
    } else {
        println!("已是最新: {}", path.display());
    }

    if shell == "fish" {
        println!("fish 会自动加载该目录，重开终端生效。");
        return 0;
    }

    let rc = rc_path(shell);
    match set_block(&rc, &block(shell, &path)) {
        Ok(true) => println!("已更新: {}（pswitch completion 区块）", rc.display()),
        Ok(false) => println!("已配置: {}（补全区块已存在）", rc.display()),
        Err(e) => {
            println!("更新 {} 失败: {e}", rc.display());
            return 1;
        }
    }
    if shell == "zsh" {
        println!("重开终端生效，也可以执行 exec zsh。");
    } else {
        println!("重开终端生效，也可以执行 exec bash。");
    }
    0
}

fn uninstall_shell(shell: &str) -> bool {
    let mut removed = false;
    let path = script_path(shell);
    if path.exists() && fs::remove_file(&path).is_ok() {
        println!("已移除: {}", path.display());
        removed = true;
    }
    if shell != "fish" {
        let rc = rc_path(shell);
        if remove_block(&rc) {
            println!("已更新: {}（移除补全区块）", rc.display());
            removed = true;
        }
    }
    removed
}

# Changelog

本文件记录 pswitch 的显著变更。三份实现（Python / Go / Rust）共用同一版本号。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

0.1.5 及更早版本没有独立的变更文件，可查看 git 历史与
[tags](https://github.com/WalterYOO/pswitch/tags)。

## [0.1.8] - 2026-10-29

三份实现（Python / Go / Rust）统一发布 0.1.8。

### 新增

- **`pswitch lan on|off|allow|status`**（三版）：在 `127.0.0.1` 与 `0.0.0.0`
  之间切换监听。开启 LAN 监听前必须指定允许来源（任意多个 IP 或 CIDR，例如
  `pswitch lan on 192.168.3.0/24 192.168.3.40`）；`pswitch lan allow` 可随时
  查看/覆盖，`--clear` 清空。回环地址始终允许，列表为空时只有本机可连接。
  切换监听地址会自动重启守护进程。
- **来源白名单**：配置新增 `allow_cidrs`。守护进程在 accept 后按来源 IP 校验，
  不在列表中的连接直接断开并记录日志；白名单变更随配置热重载生效。
- **来源拒绝列表**：配置新增 `deny_cidrs`，用 `pswitch lan deny ...` 维护；
  优先级高于 `allow_cidrs`，命中即断开。回环地址不受 allow / deny 限制。

### 变更

- `pswitch status` 在监听 `0.0.0.0` 时显示允许来源，并用 `127.0.0.1` 做端口探测。
- `pswitch env` / `pswitch test` 在监听 `0.0.0.0` 时改用 `127.0.0.1` 访问本地端口。

## [0.1.7] - 2026-10-28

三份实现（Python / Go / Rust）统一发布 0.1.7。

### 新增

- **`pswitch completion [zsh|bash|fish]`**（三版）：安装 shell 补全，
  默认按 `$SHELL` 识别；`--print` 只把脚本打印到标准输出，`--uninstall`
  移除。zsh / bash 会在启动文件里维护可重复执行的
  `# >>> pswitch completion >>>` 区块，fish 写入
  `$XDG_CONFIG_HOME/fish/completions/`。补全内容包含子命令、选项和
  当前配置里的上游名字，三份实现共用同一份脚本。

## [0.1.6] - 2026-10-24

三份实现（Python / Go / Rust）统一发布 0.1.6。

### 新增

- **Go 实现**（`go/`）：与 Python 版功能对齐的独立二进制，覆盖转发、
  `auto` 选路与故障转移、`direct` 模式、上游管理、日志轮转、launchd 与
  macOS 系统代理；面向高吞吐、低延迟场景。
- **Rust 实现**（`rust/`）：基于 tokio 的等价实现，配置、状态、日志与
  CLI 行为与 Python 版一致；新建连接延迟与转发吞吐明显优于 Python 版。
- **`pswitch system use <名字|host:port>`**（三版）：把 macOS 系统代理
  直接指向指定上游，绕过本地转发端口；禁用的上游会被拒绝，
  `pswitch system on` 可切回本地端口。
- **可重复基准**（`bench/`）：新增延迟（`micro` / `loaded` / `failover`）
  与转发吞吐（`throughput`）用例，以及 `ab` 宏基准，结果写入
  `bench/results/<时间戳>/SUMMARY.md`。
- **`bench.py --impl python|rust|go` 与 `--binary PATH`**：同一套用例、
  端口与统计口径可以测任意一份实现。
- **`bench/COMPARISON.md`**：Python / Go / Rust 三实现同机横向对比数据与结论。

### 变更

- 三份实现的版本号统一为 0.1.6；Go 版此前自报 0.2.0。
- 根 README 的基准入口改为 `--impl`，`PSWITCH_BIN` 环境变量被移除。

### 修复

- `bench.py` 生成的「复现」命令补上 `--offset` 与 `--upstream`，
  按提示重跑不会换端口，也不会因缺少参数直接失败。
- `bench.py` 把连接被 RST 视为快速失败，不再误判为挂住。

### 文档

- 根 README 增加 Go / Rust 版的构建与安装说明，以及 `system use`
  绕过本地转发时的注意事项。
- `go/README.md`、`rust/README.md` 补充功能对齐表、设计要点与各自基准数据。
- `bench/README.md` 说明如何用 `--impl` / `--binary` 比较不同实现。

[0.1.6]: https://github.com/WalterYOO/pswitch/releases/tag/0.1.6

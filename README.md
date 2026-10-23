# pswitch

把多个上游代理聚合到一个固定本地端口（默认 `127.0.0.1:17000`）。
客户端只认这个端口，切换上游只需一条命令，不再改环境变量。

```
                  ┌──────────────────────┐
 curl/git/npm ───▶│ 127.0.0.1:17000      │
 浏览器/GUI   ───▶│ pswitch (TCP 转发)    │───▶ upstream A
                  │                      │───▶ upstream B
                  └──────────────────────┘───▶ upstream C
```

## 安装

```bash
uv tool install pswitch
# 或者
pip install pswitch
```

升级：

```bash
uv tool upgrade pswitch
# 或者
pip install -U pswitch
```

从源码安装（开发用）：

```bash
git clone https://github.com/WalterYOO/pswitch.git
uv tool install ./pswitch
```

安装后可执行文件位于 `~/.local/bin/pswitch`，确认该目录在 `PATH` 中：

```bash
command -v pswitch
```

### Rust 版（高吞吐 / 低延迟）

仓库内还有一份功能等价的 Rust 实现，配置与状态文件格式完全兼容：

```bash
cargo build --release --manifest-path rust/Cargo.toml
install -m 755 rust/target/release/pswitch ~/.local/bin/pswitch
```

构建方式、设计要点和与 Python 版的基准对照见 [`rust/README.md`](rust/README.md)。

## 首次运行

代码里**不内置任何上游**。第一次执行任意命令时会自动创建配置文件，
其中 `upstreams` 为空：

```bash
pswitch status
# 已创建配置文件: ~/.config/proxy_switch/config.json（还没有上游，用 pswitch add 添加）
```

添加上游后才能启动：

```bash
pswitch add local  127.0.0.1:17890
pswitch add office 192.168.212.21:17090
pswitch start
```

**没有配置任何上游时，`pswitch start` 和 `pswitch install` 会拒绝执行并返回退出码 1。**

## 日常使用

```bash
pswitch status          # 当前走哪个、各上游实时探活
pswitch use office      # 固定走某一个
pswitch use auto        # 自动：按优先级顺序选第一个真正能通的（推荐）
pswitch use direct      # 全部直连：不经过任何上游
pswitch priority        # 查看优先级顺序
pswitch priority office 1   # 把 office 提到最高优先级
pswitch list            # 列出全部上游
pswitch test            # 经本地端口发真实请求验证
pswitch env             # 打印代理环境变量
pswitch disable office  # 临时不用它（如内网代理在当前网络不可达），配置保留
pswitch enable office   # 恢复
pswitch version         # 查看当前版本号
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `start` / `stop` / `restart` | 管理守护进程 |
| `status` | 进程、监听、当前上游、各上游实时探活结果 |
| `use <名字\|auto\|direct>` | 切换出口（auto = 自动选择；direct = 不经上游直接连目标） |
| `list` / `add <名> <host:port>` / `remove <名>` | 管理上游列表 |
| `enable <名>` / `disable <名>` | 临时启用/禁用某个上游：配置保留，禁用后不参与 `use`、auto 选路和故障转移 |
| `priority [<名> [位置]]` | 查看优先级顺序；给出名字和位置则把该上游移到该位置（1 = 最高） |
| `env` | 打印代理环境变量 |
| `test` | 经本地端口请求 gstatic 的 204 接口 |
| `logs [-n 行数]` | 查看日志（`~/.config/proxy_switch/pswitch.log`） |
| `install` / `uninstall` | 安装/卸载 launchd 开机自启 |
| `system on\|off\|status` | 把 macOS 系统代理指向/取消指向本地端口 |
| `version`（或 `-V` / `--version`） | 查看版本号 |

## 配置文件

`~/.config/proxy_switch/config.json`（`PSWITCH_HOME` 可覆盖目录）：

```json
{
  "listen": "127.0.0.1:17000",
  "mode": "auto",
  "active": null,
  "health_check_interval": 10,
  "connect_timeout": 3,
  "failover": true,
  "probe_url": "http://connectivitycheck.gstatic.com/generate_204",
  "probe_timeout": 3,
  "upstreams": []
}
```

- 直接编辑也没关系，守护进程会检测到文件变化并自动重载。
- 改 `listen` 需要 `pswitch restart` 才生效。
- `mode: auto` 时按 `upstreams` 顺序取第一个探活通过的；`mode: manual` 时固定 `active`。
- `mode: direct` 时完全不使用上游：pswitch 自己解析 HTTP CONNECT / 普通 HTTP / SOCKS5，
  直接连接目标。此时没有配置上游也能 `pswitch start`，`use direct` 与 `use auto|名字`
  可随时互切，客户端的环境变量和系统代理指向都不用改。
- **`upstreams` 的数组顺序就是优先级**：先比探活等级（HTTP 通 > 只通 TCP），同级按顺序取第一个，
  不看延迟。`pswitch add` 追加到末尾（最低），`pswitch priority <名> <位置>` 调整，
  `list` / `priority` 里的编号就是选路顺序。
- 每个上游可带 `"enabled": false`（`pswitch disable <名>` 会写入）。禁用的上游保留在配置里，
  但探活、auto 选路、故障转移都会跳过它；如果 `mode: manual` 的 `active` 正好被禁用，
  选路会当作没命中，退回 auto 顺序。`pswitch enable <名>` 即可恢复。
- 全部上游都被禁用时，`pswitch start` 会拒绝启动，系统代理也不会被打开。
- 探活地址可以换成内网地址，避免依赖外网：例如
  `"probe_url": "http://your-intranet/health"`（要求返回 200/204）。

## 与 shell 环境变量的关系

如果环境变量由 `zsh-osx-autoproxy` 之类的插件从 **macOS 系统代理设置**导出，
让系统代理指向本地端口即可，新终端会自动跟随：

```bash
pswitch install        # 开机自启，挂了自动拉起
pswitch system on      # 系统代理 -> 127.0.0.1:17000
```

GUI 应用也一起生效。恢复直连或换回原来的代理：`pswitch system off`。

如果只想影响命令行，把 `pswitch env` 的输出追加到 `~/.zshrc` 末尾
（放在 oh-my-zsh 加载之后，才能覆盖插件设置）。

## 工作方式

- 在 TCP 层透明转发，同一个本地端口既能当 HTTP 代理，也能当 SOCKS5 代理。
- 每 `health_check_interval` 秒对每个上游做一次真实 HTTP 探活；只通 TCP 但转发失败
  （例如返回 502）的上游会被标记为 `TCP only`，自动选路时排在后面。
- 当前上游临时连不上时，单条连接会自动借用其它可用上游。
- direct 模式下 pswitch 自己充当代理：支持 HTTP CONNECT、普通 HTTP（首个请求改写为
  origin-form 并强制 `Connection: close`）和无认证 SOCKS5 CONNECT；BIND / UDP ASSOCIATE
  和 SOCKS4 不支持，会明确拒绝。

## 注意事项

- 系统代理指向本地端口后，`pswitch` 停掉就等于断网。建议先 `install` 再 `system on`。
- 手动 `use <名字>` 的语义是“钉死”；要自动避开坏上游就用 `use auto`。
- `no_proxy` 里保留 `127.0.0.1,localhost`，否则访问本地服务可能被绕一圈。
- 从源码目录执行 `python -m pswitch` 时，注意不要与旧版的 `pswitch.py` 同名冲突；
  正常使用安装后的 `pswitch` 命令即可。

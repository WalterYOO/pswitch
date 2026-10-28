# pswitch（Rust 实现）

与仓库根目录的 Python 版功能对齐，目标是**更高吞吐、更低延迟**。
配置、状态、日志文件格式完全兼容，可以直接替换 Python 版的可执行文件。

## 构建与安装

```bash
cargo build --release --manifest-path rust/Cargo.toml
# 二进制: rust/target/release/pswitch

install -m 755 rust/target/release/pswitch ~/.local/bin/pswitch
```

要求 Rust 1.82+（用到了 `Option::is_none_or`）；在 macOS（launchd / networksetup）
上开发与测试。

## 功能对齐

| 能力 | 说明 |
| --- | --- |
| `start` / `stop` / `restart` / `serve` | 后台守护进程，macOS 有 launchd 时走 launchd |
| `status` / `list` / `add` / `remove` | 上游管理，实时探活 |
| `use <名字\|auto\|direct>` | manual / auto / direct 三种模式 |
| `priority` / `enable` / `disable` | 优先级与临时禁用 |
| `auto` 选路 + `failover` | HTTP 探活优先、TCP 兜底；当前上游连不上时借用其它健康上游 |
| `direct` 模式 | 自带 HTTP CONNECT / 普通 HTTP / SOCKS5（无认证）代理 |
| 配置热重载 | 每条 CLI 命令发 SIGHUP，守护进程立即重载并重新选路 |
| `test` / `env` / `logs` | 真实请求验证、环境变量、日志 |
| `install` / `uninstall` / `system on\|off\|status` | macOS 开机自启 / 系统代理 |
| `system use <名字\|host:port>` | 系统代理直接指向某个上游（不经过 pswitch） |
| `completion [zsh\|bash\|fish]` | 安装 shell 补全（`--print` / `--uninstall`） |

配置路径与 Python 版相同：`$PSWITCH_HOME`，缺省 `~/.config/proxy_switch/`。
`config.json`、`state.json`、`pswitch.pid`、`pswitch.log` 可直接复用。

## 性能设计

- tokio 多线程运行时，每连接一个 task；`copy_bidirectional_with_sizes`
  使用 64 KiB 双向缓冲，减少 loopback 上的 syscall 次数。
- 客户端与上游 socket 都设置 `TCP_NODELAY`，交互式小包不额外增加延迟。
- 路由表是一份不可变快照（`Arc<Route>`），每连接只读一次；健康检查与配置
  重载在后台 task 里更新，不阻塞转发热路径。
- 握手阶段多读出的字节（例如 CONNECT 后紧跟的 TLS ClientHello）原样转交，
  不丢包。
- 所有上游都不可用时先 `shutdown` 再关闭，客户端看到干净的 EOF 而不是 RST。

## 基准结果

同一台机器（macOS / Apple Silicon 14 核）、同一轮 bench，端口全部在 171xx / 1808x
（未触碰生产 `127.0.0.1:17000`）。两种实现各跑一次完整 `all`：

```bash
cargo build --release --manifest-path rust/Cargo.toml
python bench/bench.py all --impl rust   --offset 100
python bench/bench.py all --impl python --offset 100
```

| 指标 | Python | Rust | 变化 |
| --- | ---: | ---: | ---: |
| T1 稳态 1 KiB p50 / p99 (µs) | 94.6 / 122.5 | 55.6 / 74.4 | −41% / −39% |
| T1 稳态 64 KiB p50 / p99 (µs) | 133.1 / 167.6 | 91.0 / 142.6 | −32% / −15% |
| T1 新建连接首包 p50 (µs) | 318.0 | 186.4 | −41% |
| T2 两级转发首包 p50 (µs) | 628.0 | 268.0 | −57% |
| T3 direct 模式首包 p50 (µs) | 379.4 | 197.4 | −48% |
| 并发 1：req/s（p50 µs） | 9,096（98.7） | 15,004（56.1） | +65% |
| 并发 8：req/s（p50 µs） | 54,056（125.3） | 39,622（161.2） | −27% |
| 并发 64：req/s（p50 µs） | 31,884（1,656） | 29,373（1,792） | −8% |
| 转发吞吐 send 1 流 (Gbps) | 30.90 | 52.27 | +69% |
| 转发吞吐 send 4 流 (Gbps) | 38.56 | 56.53 | +47% |
| 转发吞吐 recv 1 流 (Gbps) | 27.47 | 53.74 | +96% |
| 转发吞吐 recv 4 流 (Gbps) | 36.55 | 57.43 | +57% |
| ab keep-alive 并发 1 (req/s) | 10,081 | 17,479 | +73% |
| ab keep-alive 并发 64 (req/s) | 69,049 | 81,175 | +18% |
| 上游拒绝切换 p50 (µs) | 519 | 297 | −43% |
| 全部上游不可用 EOF p50 (µs) | 388 | 809 | +109% |

结论：

- **吞吐提升约 1.5–2 倍**。单流从 ~30 Gbps 升到 52–54 Gbps，多流约
  56–57 Gbps。
- **新建连接延迟降低约 40–60%**，日常切换、短连接场景受益最直接。
- **稳态 1 KiB / 64 KiB 往返延迟降低 30–40%**；1 字节小包的稳态 RTT
  两者都在 3.5 µs 左右（已低于测量噪声）。
- **并发 8 是 Python 唯一占优的场景**：tokio 每连接一个 task，8 条连接
  分散在多个 worker 上，缓存局部性不如 Python 的单线程事件循环；
  并发 64 时两者都撞上客户端线程瓶颈，差距回到个位数。
- 全部上游不可用时 Rust 多花几百 µs 做 shutdown（Python 直接 close），
  换来的是客户端拿到 EOF 而不是 RST，绝对时间仍在亚毫秒级。

> 测量注意：本机同时有另一个 worktree 在跑 go 版 bench，早期几轮结果被
> iperf3 抢占 CPU 严重污染（T0 基线一度偏到 638 µs）。上表取自两次连续的
> 干净 `all` 运行，基线恢复到 ~3.5 µs（1 B）。跑基准前先确认
> `pgrep -af 'bench/bench.py|iperf3'` 为空。

## 基准口径

详细定义见 [`bench/PLAN.md`](../bench/PLAN.md) 与
[`bench/THROUGHPUT-PLAN.md`](../bench/THROUGHPUT-PLAN.md)。要点：

- 每个 pswitch 实例使用独立 `PSWITCH_HOME` 和 171xx 端口，目标端在 1808x；
- 健康检查周期设为 3600 s，避免探活污染尾延迟；
- 结果写入 `bench/results/<时间戳>/SUMMARY.md`，不提交 git。

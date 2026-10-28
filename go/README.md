# pswitch（Go 实现）

功能与 Python 版完全对齐的 Go 重写：一个本地端口，多个上游代理，随时切换。
配置文件、`state.json`、PID 文件、日志格式与 CLI 命令都与 Python 版一致，
两版可以共用同一个 `PSWITCH_HOME`，也可以直接替换二进制。

## 构建

```bash
cd go
go build -o bin/pswitch ./cmd/pswitch
./bin/pswitch version
```

要求 Go 1.22+，无第三方依赖。

## 功能对齐

| 能力 | 状态 |
| --- | --- |
| `start/stop/restart/status/serve` 守护进程管理 | ✅ |
| `add/remove/list/priority/enable/disable` 上游管理 | ✅ |
| `use <名字\|auto\|direct>` 三种模式 | ✅ |
| `auto` 按优先级 + 探活等级（HTTP > TCP）选路 | ✅ |
| 当前上游连不上时故障转移到其它健康上游 | ✅ |
| HTTP 探活（真实代理请求）+ TCP 回退 | ✅ |
| 配置文件自动发现变化 + `SIGHUP` 热重载 | ✅ |
| `direct` 模式：HTTP CONNECT、普通 HTTP、SOCKS5 | ✅ |
| `env/test/logs/version` | ✅ |
| macOS launchd `install/uninstall` | ✅ |
| macOS `system on/off/status` 系统代理 | ✅ |
| macOS `system use <名字\|host:port>` 直连上游 | ✅ |
| `completion [zsh\|bash\|fish]` 安装 shell 补全 | ✅ |
| 日志按 1 MB × 3 轮转 | ✅ |

行为细节与 Python 版保持一致，例如：

- `upstreams` 数组顺序就是优先级，同级只按顺序、不看延迟；
- 禁用的上游保留配置，但探活、选路、故障转移全部跳过；
- `direct` 模式不需要配置任何上游即可启动；
- 普通 HTTP 代理请求会改写为 origin-form 并强制 `Connection: close`；
- SOCKS5 只支持 CONNECT，BIND / UDP ASSOCIATE 明确拒绝。

## 性能设计

- **goroutine-per-connection**：每条连接两个 goroutine 各负责一个方向，
  多连接天然吃满多核；Python 版是单事件循环。
- **64 KiB 池化缓冲区**：`sync.Pool` 复用，连接建立后零额外分配。
- **半关闭语义**：一个方向 EOF 时 `CloseWrite`，避免截断响应。
- **`TCP_NODELAY` + keepalive**：客户端与上游两侧都关闭 Nagle，降低小包延迟。
- **无锁热路径**：连接建立时一次性读取配置/健康快照，转发期间不碰锁。
- **探活并发**：所有上游同时探测，互不阻塞。

## 基准结果（2026-10-23，Apple Silicon 14 核，同机同轮对比）

延迟（`bench.py micro`，µs）：

| 指标 | Go | Python |
| --- | ---: | ---: |
| T1 稳态 1 B p50 | 4.8 | 3.2 |
| T1 稳态 1 B p99 | 11.9 | — |
| T1 新建连接首包 p50 | 216.6 | 318 |

转发吞吐（`bench.py throughput`，iperf3，Gbps）：

| 方向 | 流数 | Go | Python | 提升 |
| --- | ---: | ---: | ---: | ---: |
| send | 1 | 45.21 | 29.57 | +53% |
| send | 4 | 46.98 | 36.44 | +29% |
| recv | 1 | 46.62 | 30.88 | +51% |
| recv | 4 | 48.27 | 37.72 | +28% |

- 四流时同轮 iperf3 基线约 48–55 Gbps，Go 版保持率 95% 左右，已接近测量床上限。
- Go 版 CPU 128–167%（多核并行），Python 版 82–99%（单核饱和）。
- 额外实验：把拷贝缓冲从 64 KiB 提到 256 KiB、socket 缓冲提到 1 MiB，
  单流提升在噪声范围内、四流反而波动更大，因此保留 64 KiB 以控制每连接内存。

## 用 bench 测它

先构建 `go/bin/pswitch`，然后在仓库根目录：

```bash
python bench/bench.py all --impl go
```

不加 `--impl` 时跑 Python 版；各实现使用完全相同的用例、端口和统计口径，
结果目录里的 `meta.json` 会记录 `impl` 与 `binary`。

## 测试

```bash
cd go
go test ./...
go vet ./...
```

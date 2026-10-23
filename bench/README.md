# pswitch 延迟基准

这套脚本按 [`PLAN.md`](PLAN.md)（延迟）和
[`THROUGHPUT-PLAN.md`](THROUGHPUT-PLAN.md)（转发吞吐）的口径测量 pswitch，
结果可跨次运行对比。

## 一条命令

```bash
python bench/bench.py all
```

跑完后看：

```
bench/results/<YYYYMMDD-HHMMSS>/SUMMARY.md
```

结果目录里有 `meta.json`、各用例 JSON、`SUMMARY.md` 和每个测试实例的配置/日志。
每次运行都会新建目录，不会覆盖历史结果。

## 隔离保证

- 每个 pswitch 实例使用独立的临时 `PSWITCH_HOME`，监听 `171xx` 端口；
  目标端监听 `1808x`。**不会使用 17000，也不会调用 `pswitch start/stop` 或 launchd。**
- 测试子进程会清空代理环境变量，避免测试流量绕到生产代理。
- 周期健康检查被关掉（`health_check_interval=3600`），避免每 10 秒的探活污染尾延迟。
- 运行结束后按启动逆序清理进程；可用 `lsof -iTCP:17100-17105` 确认没有残留。

`--offset 100` 可把所有测试端口整体后移，端口冲突时使用。

## 用例

| 命令 | 测什么 | 默认耗时 |
| --- | --- | --- |
| `python bench/bench.py micro` | L1 稳态 RTT + L2 新建连接延迟，四条拓扑 | ~30 s |
| `python bench/bench.py loaded` | L3 背景灌包下的探测延迟 + 并发 RTT | ~35 s |
| `python bench/bench.py throughput` | T1–T4 单流/多流/双向转发吞吐与 CPU 效率 | ~100 s |
| `python bench/bench.py macro` | ab keep-alive 吞吐与并发（毫秒精度） | ~10 s |
| `python bench/bench.py failover` | L4 上游拒绝/黑洞/全挂时的停顿 | ~10 s |
| `python bench/bench.py e2e --upstream HOST:PORT` | 真实上游的配对差值 | 取决于网络 |
| `python bench/bench.py all` | 前五项；带 `--upstream` 时再加 e2e | ~3 min |

常用参数：

```bash
# 更稳的微基准（5 轮）
python bench/bench.py micro --rounds 5 --samples 2000

# 更高并发，找出排队拐点
python bench/bench.py loaded --concurrent 1,8,32,128 --duration 10

# 转发吞吐：iperf3 优先，缺失时自动回退 Python 生成器
python bench/bench.py throughput
python bench/bench.py throughput --throughput-directions send,recv,bidir \
    --throughput-streams 1,4,8
python bench/bench.py throughput --throughput-generator python --throughput-duration 3

# 更高压力下的 HTTP 吞吐
python bench/bench.py macro --requests 20000 --concurrency 1,64,256

# 按生产的 3 秒超时测故障切换
python bench/bench.py failover --connect-timeout 3 --failover-samples 3

# CI 模式：任一判定为 CHECK 时退出码 1
python bench/bench.py all --strict

# 保留测试进程排查
python bench/bench.py micro --no-cleanup
```

## 四条拓扑

```
T0  client ──────────────────────────────▶ target:18080
T1  client ──▶ pswitch(forward):17100 ───▶ target:18080
T2  client ──▶ pswitch(forward):17102 ──▶ pswitch(direct):17101 ──▶ target:18080
T3  client ──▶ pswitch(direct):17101 ───▶ target:18080
```

- `T1 - T0`：转发模式的纯开销。
- `T2 - T3`：多一跳转发相对 direct 的差异。
- `T3 - T0`：direct 模式多出的协议解析与目标拨号。
- `fresh total` 是“新建连接 + 首包”的端到端时间，包含 pswitch 拨上游的成本。

## 基线结果

2026-10-23，macOS 26.6.2 / Apple Silicon 14 核 / Python 3.14.0 / pswitch 0.1.5。
完整结果保存在当次运行的 `bench/results/<时间戳>/SUMMARY.md`（结果目录不提交 git）。

### 微基准（µs）

| 拓扑 | 1 B steady p50/p99 | 1 KiB steady p50/p99 | 64 KiB steady p50/p99 | 1 B fresh p50 |
| --- | --- | --- | --- | --- |
| T0 baseline | 3.6 / 5.3 | 36.0 / 56.3 | 61.8 / 74.0 | 131.0 |
| T1 forward | 3.2 / 5.3 | 93.3 / 154.5 | 131.5 / 159.5 | 317.9 |
| T2 forward→direct | 3.6 / 8.7 | 152.8 / 196.0 | 217.9 / 254.9 | 639.4 |
| T3 direct | 3.2 / 5.0 | 92.0 / 124.1 | 130.6 / 157.6 | 380.2 |

解读：

- 1 B 稳态 RTT 的差值在测量噪声内；1 KiB / 64 KiB 每跳约增加 **55–70 µs**。
- 新建连接比直连多约 **190 µs**（T1），direct 模式多约 **250 µs**（T3）。
- 上游如果是 30 ms 的公网代理，这些开销占比不到 1%；真正值得关注的是新建连接数
  和故障切换。

### 加载与并发

| 场景 | 结果 |
| --- | --- |
| 0 条背景流 | 探测 p50 668 µs / p99 2669 µs（主要是空闲唤醒开销，见下文） |
| 1 条背景流 | 30.3 Gbps，pswitch CPU 83%，探测 p50 215 µs / p99 343 µs |
| 2 条背景流 | 36.0 Gbps，pswitch CPU 95%，探测 p50 306 µs / p99 503 µs |
| 并发 1（持续请求） | 9,620 req/s，p50 94 µs / p99 121 µs |
| 并发 8 | 54,964 req/s，p50 126 µs / p99 213 µs |
| 并发 64 | 31,192 req/s，p50 1733 µs / p99 5200 µs |

并发 64 是明显的拐点：单线程事件循环饱和，吞吐回落、p99 上到毫秒级。
这是排队，不是丢包或错误（错误数为 0）。

### 转发吞吐（iperf3）

| 方向 | 流数 | 直连基线 Gbps | 经 pswitch Gbps | 保持率 | pswitch CPU | Gbps/核 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| send | 1 | 133.79 | 29.94 | 22% | 83% | 35.9 |
| send | 4 | 51.18 | 39.91 | 78% | 99% | 40.1 |
| recv | 1 | 147.51 | 32.45 | 22% | 82% | 39.5 |
| recv | 4 | 51.56 | 35.87 | 70% | 96% | 37.4 |

解读：

- pswitch 单流约 **30 Gbps**，四流约 **36–40 Gbps**，此时单核已接近 100%。
- 基线是内核 loopback（单流 130–148 Gbps），所以单流保持率只有约 22%；
  这不是判定项。对 1–10 Gbps 的网络，pswitch 仍有充足余量。
- 四流基线反而降到 51 Gbps，是 iperf3 自身多流开销，不能与单流基线直接比较。
- 如果要突破 40 Gbps，优先做多进程 + `SO_REUSEPORT`，而不是直接换语言。

### 宏基准（ab -k，HTTP keep-alive）

| 并发 | 直连目标 | 经 pswitch |
| ---: | ---: | ---: |
| 1 | 26,560 req/s | 10,269 req/s |
| 64 | 123,309 req/s | 75,282 req/s |

ab 的分位数只有 1 ms 精度，本地结果全是 `0.00`，所以这张表只看吞吐。
微秒级尾延迟看上面的并发 RTT 表。

### 故障切换（connect_timeout = 1 s）

| 场景 | 首包/EOF p50 |
| --- | ---: |
| 当前上游拒绝连接，另有健康上游 | 544 µs |
| 当前上游黑洞，另有健康上游 | 1.0036 s |
| 全部上游不可用 | 549 µs |

黑洞场景精确落在 `connect_timeout` 上；拒绝连接几乎立即切换。
生产默认超时是 3 s，用 `--connect-timeout 3` 可以测真实配置。

## 结果怎么读

- **看分位数，不看均值**。每次运行都带 `n`；微基准默认每档 1000 个样本、3 轮。
- **只比同机结果**。`meta.json` 记录了主机、平台、CPU、Python、git commit。
- **判定只作提示**。默认 `CHECK` 不影响退出码；CI 里用 `--strict`。
- **e2e 只看配对差值**。公网 TTFB 的方差远大于 pswitch 开销，样本少于 30 对没有意义；
  失败样本不参与差值统计。
- **可重复性验收**：连续跑两次 `all`，同一指标 p50 差异应在 20% 以内。
  本次两次运行的 T1 1 B p50 为 3.2 / 3.3 µs，新建连接为 318 / 315 µs，
  背景吞吐为 30.25 / 30.43 Gbps，均在 20% 内；
  吞吐测试两次单流 send 为 29.94 / 29.64 Gbps，四流 send 为 39.91 / 38.38 Gbps。

### 两个容易误读的点

1. **空闲探测反而更慢**：探测流每 10 ms 才发一次，进程从空闲被唤醒本身就要几百 µs；
   同机直连目标端也能复现。背景流量让事件循环保持热态，所以“有负载”的探测延迟
   比“无负载”低。要看持续请求下的真实延迟，看并发 RTT 表里并发 = 1 的那行。
2. **ab 的 0.00 ms**：ab 只输出整数毫秒，亚毫秒全部显示 0；这不是延迟为零。

## 依赖

- Python ≥ 3.9，只用标准库。
- `iperf3` 用于转发吞吐（首选）；缺失时自动回退到内置 Python 生成器。
- `ab`（macOS 自带）用于宏基准；缺失时自动跳过。
- `curl` 用于 e2e；缺失时自动跳过。
- 黑洞测试默认连 `10.255.255.1:17090`；不同网络下行为可能不同，
  可用 `--blackhole HOST:PORT` 换一个会丢 SYN 的地址。

## 扩展

新增一个场景时：

1. 先在 `PLAN.md` 里写清度量定义、采样量和判定阈值，并提升方案版本；
2. 在 `bench.py` 里加一个 `run_<场景>`，复用 `open_conn` / `summarize` /
   `Env.start_instance`；
3. 把结果写进 `results` 字典并在 `build_summary` 里加一节；
4. 跑两次确认可重复，再把基线数字补到本文件。

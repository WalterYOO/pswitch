# pswitch 转发吞吐基准方案

- 方案版本：v1
- 状态：已实现并验证（2026-10-23）
- 适用范围：pswitch **转发模式**的本地吞吐上限与 CPU 效率
- 不适用范围：延迟（见 [`PLAN.md`](PLAN.md)）；真实上游/公网带宽评估

本文件与 `PLAN.md` 并列：延迟看 `PLAN.md`，吞吐看本文件。
修改测量方法、端口约定或判定阈值时，先改本文件并提升版本号。

---

## 1. 目标

回答三个问题：

1. pswitch 单条连接能跑多快（上行 / 下行）？
2. 多条连接并行时能不能继续扩展，还是单核先饱和？
3. 达到这个吞吐时，pswitch 的 CPU 成本是多少（Gbps / 核）？

### 非目标

- 不测公网或真实上游的带宽；那由上游决定，不是 pswitch 的能力边界。
- 不测 direct 模式作为通用 TCP 转发器的吞吐：direct 模式是 HTTP/SOCKS5 代理，
  只接受代理协议，不能直接承载 iperf3 的裸 TCP 流。
  （需要时可走 CONNECT 隧道单独测，见第 9 节。）
- 不测连接建立速率（churn）；如需，用 `PLAN.md` 的 L2 指标扩展。

---

## 2. 度量定义

所有吞吐单位 **Gbps**（`bits_per_second / 1e9`），CPU 单位 **单核百分比**。

### T1 单流单向吞吐

- 一条 TCP 连接，持续传输 `duration` 秒；分别测：
  - `send`：客户端 → pswitch → 目标端；
  - `recv`：目标端 → pswitch → 客户端。
- 报告：吞吐、重传数、pswitch CPU 占比。

### T2 多流聚合吞吐

- 同一方向上 `P` 条并行连接，`P = 1, 4`（可扩展）。
- 报告：总吞吐、单流平均吞吐、pswitch CPU、扩展比
  `throughput(P) / throughput(1)`。

### T3 双向吞吐（可选）

- 单条连接上同时双向传输（iperf3 `--bidir`）。
- 报告：两个方向各自吞吐与合计。
- Python 回退实现无法做单连接真双工，退化为两条连接同时收发，结果标注
  `bidir(2 conns)`，不与 iperf3 的 `bidir` 直接比较。

### T4 CPU 效率

- `pswitch_cpu_pct`：测试期间 pswitch 进程的 CPU 占单核百分比。
- `gbps_per_core = 吞吐 / (pswitch_cpu_pct / 100)`。
- `retention = 经 pswitch 吞吐 / 同参数直连基线吞吐`。
- 目的：判断还有多少余量，以及瓶颈是 pswitch 还是测试端。

---

## 3. 隔离与复现约束（硬规则）

1. 独立 `PSWITCH_HOME`，监听 `17106+offset`；iperf3 服务端用 `18082+offset`；
   目标端 `18080+offset`。**绝不使用 17000**。
2. 测试子进程清空代理环境变量。
3. 测试实例 `health_check_interval=3600`、`mode=manual`、`active=<被测上游>`。
4. 每个参数组合跑 `rounds` 轮（默认 2），奇偶轮交换 baseline / pswitch 顺序，
   取每轮吞吐的中位数。
5. iperf3 必须带 `-O 2`：丢弃前 2 秒预热，避免 TCP 慢启动和 CPU 升频污染结果。
6. 同一轮内 baseline 与 pswitch 使用完全相同的 `-t/-O/-P/-w` 参数。
7. 只比较同机、同轮次的结果；`meta.json` 记录机器与 git commit。

---

## 4. 测试床

### 4.1 首选：iperf3

选择 iperf3 的原因：它是 C 实现，能产生远高于 Python 客户端的负载，
可以把“测试客户端先饱和”从测量里排除。

```
B0  iperf3 client ──────────────────────────────▶ iperf3 server:18082
B1  iperf3 client ──▶ pswitch(forward):17106 ───▶ iperf3 server:18082
```

- B0 是基线；B1 是经 pswitch。
- pswitch 转发模式是字节透明管道，iperf3 的裸 TCP 流可以直接穿过。
- pswitch 的上游配置为 `127.0.0.1:18082`；健康探活会失败但仍标记 TCP 可达，
  manual 模式下不影响选路。

### 4.2 回退：内置 Python 生成器

没有 iperf3 时使用，复用 `target.py` 的 raw 协议：

| 首字节 | 行为 | 用途 |
| --- | --- | --- |
| `S` | 读走并丢弃 | 上行吞吐 |
| `D` | 持续下发 64 KiB 块，直到客户端断开 | 下行吞吐 |

- 上行：客户端连接后发 `S`，持续 `sendall` 64 KiB。
- 下行：客户端连接后发 `D`，持续 `recv` 并丢弃。
- 只能做相对比较：同一台机器上 Python 客户端和 pswitch 会争 CPU，
  绝对数字通常低于 iperf3；报告中标注 `generator: python`。

### 4.3 端口

| 用途 | 端口 |
| --- | ---: |
| pswitch throughput 实例 | 17106 + offset |
| iperf3 server | 18082 + offset |
| raw 目标端（回退用） | 18080 + offset |

---

## 5. 用例矩阵

| 维度 | 取值 | 默认 |
| --- | --- | --- |
| 方向 | send / recv / bidir | send, recv |
| 并行流 | 1 / 4 | 1, 4 |
| 轮数 | 每组合 | 2 |
| 单轮时长 | 秒 | 6 |
| 预热丢弃 | 秒 | 2 |
| 生成器 | iperf3 / python / auto | auto |

`auto`：有 iperf3 就用 iperf3，否则回退 Python。

默认组合数：2 方向 × 2 流数 × 2 路径 × 2 轮 × 6 秒 ≈ 96 秒。

---

## 6. 执行流程

```
1. 预检端口、iperf3/目标端可用性
2. 启动 iperf3 server（或 raw 目标端）
3. 启动 throughput pswitch 实例
4. 对每个 (方向, 流数)：
     for round in rounds:
       按奇偶顺序跑 baseline 与 pswitch
       每个 arm 记录吞吐、重传、pswitch CPU
5. 每个组合取中位数，计算 retention / gbps_per_core
6. 写 throughput.json + SUMMARY.md 对应章节
7. 逆序清理进程
```

---

## 7. 统计规范

- 每个组合、每个 arm 至少 2 轮；报告每轮明细与中位数。
- `retention` 用中位数计算：`median(pswitch_gbps) / median(baseline_gbps)`。
- CPU 占比用 `ps -o time=` 的累计 CPU 时间差除以墙钟时长。
- iperf3 解析 `end.sum_received.bits_per_second` 作为接收端有效吞吐；
  `bidir` 把正向与反向的接收吞吐相加。
- 失败（iperf3 非零退出、JSON 解析失败）保留错误信息，不参与中位数。

---

## 8. 结果格式

目录同 `PLAN.md`：`bench/results/<YYYYMMDD-HHMMSS>/throughput.json`。

```json
{
  "plan_version": "v1",
  "generator": "iperf3",
  "params": {"duration_s": 6, "omit_s": 2, "rounds": 2},
  "cases": [
    {
      "direction": "send",
      "streams": 1,
      "baseline": {"gbps": 42.1, "retransmits": 0},
      "pswitch": {"gbps": 31.5, "retransmits": 120, "cpu_pct": 88.0,
                  "gbps_per_core": 35.8},
      "retention": 0.748
    }
  ]
}
```

合成值仅示意字段；实际数字以运行结果为准。

---

## 9. 判定阈值

| 项 | 阈值 | 理由 |
| --- | --- | --- |
| 单流 send | ≥ 10 Gbps | 超过常见 10GbE 线速需求 |
| 四流 send | ≥ 20 Gbps | 单核应有明显扩展 |
| 保持率 retention | 记录，不设硬阈值 | 基线是内核 loopback，可能远高于用户态实现 |
| pswitch CPU | 记录，不设硬阈值 | 用于判断余量 |

保持率不作为判定项的原因：内核 loopback 单流可达 100+ Gbps，
而单核 Python 用户态代理实测约 30–40 Gbps；拿两者做比例会得出误导性的“不达标”。
真正有意义的指标是绝对吞吐和 CPU 余量。

阈值只用于提示；`--strict` 时未达标返回 1。
不同机器的阈值需要重新标定，不能照搬。

### direct 模式的吞吐

direct 模式只接受代理协议。需要测时：

1. 用 `socat` 建 CONNECT 隧道：
   `socat TCP-LISTEN:18100,fork PROXY:127.0.0.1:target:5201,proxyport=17101`
2. iperf3 客户端连 `127.0.0.1:18100`。

这条路径包含 socat 自身开销，只用于相对比较，不纳入默认用例。

---

## 10. 误差源

| 误差源 | 表现 | 排除方法 |
| --- | --- | --- |
| 测试客户端先饱和 | pswitch CPU 不高但吞吐上不去 | 用 iperf3；同时看 `retention` |
| iperf3 server 饱和 | baseline 也上不去 | 对照 baseline；必要时把 server 放另一台机器 |
| TCP 慢启动 | 前 1–2 秒偏低 | `-O 2` |
| CPU 降频 | 后跑的 arm 偏低 | 奇偶轮交换顺序 |
| loopback 共享 CPU | 所有路径一起变慢 | 记录 pswitch 与 iperf3 CPU |
| 小窗口 | 高 BDP 下上不去 | 固定 `-w 1M`，两个 arm 一致 |
| 重传 | 吞吐抖动 | 记录 `retransmits` |
| 后台流量 | 结果偏低 | 测试前确认无大流量任务 |

---

## 11. 落地清单

| 文件 | 作用 | 状态 |
| --- | --- | --- |
| `bench/THROUGHPUT-PLAN.md` | 本方案 | 已完成 |
| `bench/bench.py` | 新增 `throughput` 子命令与汇总 | 已完成并验证 |
| `bench/target.py` | 新增 `D` 下行模式（Python 回退用） | 已完成并验证 |
| `bench/README.md` | 补充吞吐用法与基线 | 已完成 |
| 根 `README.md` | 性能测试入口改为“延迟与吞吐” | 已完成 |

验证记录：2026-10-23，默认参数连续运行两次：单流 send 29.9 / 29.6 Gbps，
四流 send 39.9 / 38.4 Gbps，单流 recv 32.5 / 31.6 Gbps，
四流 recv 35.9 / 38.4 Gbps；pswitch CPU 82–99%，两次差异均 ≤ 20%。
bidir 单流 39.5 Gbps、CPU 99%。Python 回退生成器同样可用（单流约 31 Gbps）。

验收标准：

- `python bench/bench.py throughput` 一条命令跑完，无残留进程和监听端口。
- `throughput.json` 含 generator、params、每个组合的 baseline/pswitch/retention。
- 连续跑两次，同一组合的吞吐中位数差异 ≤ 20%。
- 生产实例 127.0.0.1:17000 全程不受影响。

# pswitch 延迟基准方案

- 方案版本：v1
- 状态：已实现并验证（2026-10-23）
- 适用范围：pswitch 转发模式与 direct 模式的**延迟**测量
- 不适用范围：吞吐测量（另见此前的转发基准）；真实业务链路的 SLA 评估

本文件是延迟测试的唯一权威口径。脚本、README、结果判定都必须以本文件为准；
修改测量方法、端口约定或判定阈值时，先改本文件并提升版本号。

---

## 1. 目标

用同一套可复现流程回答四个问题：

1. pswitch 在已建连接上转发一个包，额外引入多少延迟？
2. 每新建一条连接，pswitch 额外引入多少延迟？
3. 有背景流量、并发升高时，尾延迟如何变化？拐点在哪里？
4. 当前上游不可用时，客户端要等多久才能恢复？

每次运行必须产出：机器可读的 JSON + 人可读的 `SUMMARY.md`，并且能回答“这次结果和
上次相比变了没有”。

### 非目标

- 不测吞吐上限（已有单独基准）。
- 不评价上游代理自身质量；上游在本地用 test double 代替。
- 不做公网 SLA 断言；端到端测试只做配对差值，不做绝对值结论。

---

## 2. 度量定义

所有时间单位默认 **微秒（µs）**；端到端与 ab 结果用 **毫秒（ms）**。
每个指标必须同时报告样本数 `n`、p50、p90、p99、max；只报均值视为无效结果。

### L1 稳态转发 RTT

- 定义：在一条已建立的连接上，客户端发送固定 payload，收到完整回显的时间。
- 起点：`sendall()` 调用前；终点：读满等长回显后。
- 采样：单连接连续 N 次，丢弃前 W 次预热；默认 N=1000，W=50。
- payload：1 B、1 KiB、64 KiB 三档。
- 目的：隔离 pswitch 自身的事件循环调度、管道读写开销。

### L2 新建连接延迟

拆成三段，分别报告：

| 段 | 起点 | 终点 |
| --- | --- | --- |
| connect | 发起 TCP 连接 | 连接建立 |
| setup | 发出代理协议请求 | 收到成功响应（仅 CONNECT 类路径） |
| first-payload | 发出首个 payload | 收到回显 |

`total = connect + setup + first-payload`。

- 每次样本使用**全新连接**，关闭时置 `SO_LINGER=on, linger=0` 发 RST，
  避免 TIME_WAIT 耗尽本地端口。
- 默认每档 100 个样本。
- 目的：覆盖 pswitch 接受连接、拨号上游、代理握手的全部一次性成本。

### L3 加载下的尾延迟

- 背景流：N 条连接持续灌 64 KiB 块，目标端丢弃。
- 探测流：独立连接每 10 ms 发 8 B 并等回显。
- 背景流档位：0、1、2（可选扩展）。
- 同时记录：背景吞吐（Gbps）、探测 p50/p90/p99、pswitch 进程 CPU 占比。
- **并发 RTT**：K 条连接（默认 1/8/64）同时做顺序 echo，持续请求、不留空闲间隔；
  报告总 req/s 与 p50/p90/p99。这是观察排队拐点的主指标。
- 目的：观察单线程 asyncio 在排队时的尾延迟拐点。

### L4 故障切换停顿

| 场景 | 预期 |
| --- | --- |
| 当前上游拒绝连接，另有健康上游 | 快速切到健康上游，接近 L2 |
| 当前上游黑洞（SYN 无响应），另有健康上游 | 约等于 `connect_timeout` 后切换 |
| 全部上游不可用 | 客户端在 `connect_timeout` 内看到 EOF，不悬挂 |

- 每个场景默认 5 个样本；黑洞场景耗时较长，可按需减少。
- 目的：量化用户感知到的“卡住”时间。

---

## 3. 隔离与复现约束（硬规则）

违反以下任一条，结果不得与其他运行对比。

1. **不碰生产实例**：所有测试 pswitch 使用独立 `PSWITCH_HOME`，监听
   `17100+offset`；目标端使用 `18080+offset`。绝不使用 17000，也不经过
   launchd、`pswitch start/stop` 或系统代理设置。
2. **不继承代理环境**：测试子进程清除 `http_proxy`、`https_proxy`、
   `all_proxy` 及其大写形式；`NO_PROXY` 固定为 `127.0.0.1,localhost`。
   端到端 curl 测试则把 `NO_PROXY` 置空，确保 `-x` 生效。
3. **关闭周期探活**：测试实例固定 `health_check_interval=3600`、
   `mode=manual`、`active=<被测上游>`，避免 10 秒一次的探活污染尾延迟。
   启动时的一次性探活保留。
4. **固定客户端**：同一进程、同一 socket 选项（`TCP_NODELAY`）、
   `time.perf_counter_ns()` 计时；不在测量循环内做日志、格式化或内存分配。
5. **预热与轮次**：每轮丢弃 W 个预热样本；默认 3 轮，奇偶轮反转拓扑顺序，
   用“每轮分位数的中位数”作为最终值，不混池。
6. **新建连接用 RST 关闭**：见 L2。
7. **结果不可跨机器直接比较**：`meta.json` 必须记录主机、平台、CPU 核数、
   Python 版本、git commit；缺少任一项的结果不得作为基线。

---

## 4. 测试床

### 4.1 test double 协议（raw 模式）

目标端按首个字节分流：

| 首字节 | 行为 | 用途 |
| --- | --- | --- |
| `E` | 把其后所有字节原样回显 | L1 / L2 / L3 探测 |
| `S` | 读走并丢弃其后所有字节 | L3 背景流 |
| `G` / `H` | 读完 HTTP 头，回 `204` 并关闭 | pswitch 健康探活 |

选 `E`/`S` 的原因：pswitch 转发模式是字节透明管道，不需要目标端理解代理协议；
这样 L1 测量里不含任何 HTTP 解析成本。

### 4.2 拓扑

```
T0  client ──────────────────────────────▶ target:18080
T1  client ──▶ pswitch(forward):17100 ───▶ target:18080
T2  client ──▶ pswitch(forward):17102 ──▶ pswitch(direct):17101 ──▶ target:18080
T3  client ──▶ pswitch(direct):17101 ───▶ target:18080
```

| 拓扑 | 隔离出的量 |
| --- | --- |
| T0 | 客户端与目标端自身的开销（基线） |
| T1 | pswitch 盲转开销 + 一次本地 TCP 中转 |
| T2 | 真实代理语义下的转发开销（forward 后接 direct） |
| T3 | direct 模式的协议解析 + 目标拨号开销 |

T1 的差值是“pswitch 自身成本”的主要证据；T2/T3 用于覆盖 direct 模式。
T2 必须让 forward 实例的上游指向 direct 实例，因为转发模式的上游必须自己会讲代理协议。

### 4.3 HTTP 目标端（macro 用）

最小 keep-alive HTTP 服务：`GET/HEAD` 返回 `200` + 3 字节正文；
除非请求声明 `Connection: close`，否则保持连接。

---

## 5. 用例矩阵

| 用例 | 目的 | 默认参数 | 输出 |
| --- | --- | --- | --- |
| micro | L1 + L2 | 3 轮 × 1000 样本 × 3 档 payload；每档 100 个新建连接 | `micro.json` |
| loaded | L3 | 每档 8 s；背景流 0/1/2；探测间隔 10 ms；并发 1/8/64 | `loaded.json` |
| macro | 并发下的分位数 | ab `-k`；并发 1/64；每轮 10000 请求 | `macro.json` |
| failover | L4 | 3 场景 × 5 样本；`connect_timeout=1s` | `failover.json` |
| e2e | 真实上游配对差值 | 30 对；A/B/A/B 交替 | `e2e.json` |
| all | 上述全部（e2e 需显式给上游） | 见上 | `SUMMARY.md` |

### 端到端测试的特殊规则

- 只比较**同一时刻的配对差值**：`经 pswitch − 直连上游`，A/B 顺序逐次交换。
- 公网目标的绝对值方差远大于 pswitch 开销，禁止用绝对值下结论。
- 失败样本（curl 非零退出）不参与差值统计，但必须在 `pairs` 中保留。

---

## 6. 执行流程

```
1. 预检：端口可用性、Python 版本、git 状态、ab/curl 是否存在
2. 建床：启动 raw target、direct 实例、forward 实例、chain 实例
3. micro → 写 micro.json
4. loaded → 写 loaded.json
5. macro（按需启动 http target + http-forward 实例）→ 写 macro.json
6. failover（每场景独立实例，用完即停）→ 写 failover.json
7. e2e（可选，按需实例）→ 写 e2e.json
8. 汇总 → 写 SUMMARY.md
9. 清理：按启动逆序发送 SIGTERM，5 s 未退出则 SIGKILL
```

任一步失败不终止整个运行：记录 `error.txt` 并继续后续用例；
但该用例的结果必须在汇总中标记为缺失。

---

## 7. 统计规范

- 分位数算法：线性插值，`k = (n-1) * p / 100`。
- 多轮聚合：对每轮的同名分位数取中位数；不把多轮原始样本混在一起。
- 样本量下限：L1 每档 ≥ 500；L2 每档 ≥ 50；L4 每场景 ≥ 3。
- 背景负载下丢弃前 0.5 s 探测样本。
- 所有结果必须带 `n`；`n=0` 时输出 `null` 而不是 0。
- ab 是闭环压测，只能比较“同一并发下两条路径的差异”，不能当作开环延迟分布。

---

## 8. 结果格式

目录：`bench/results/<YYYYMMDD-HHMMSS>/`（本地生成，不提交 git）

```
meta.json          环境与参数
micro.json         L1/L2 明细与聚合
loaded.json        L3（levels = 背景流探测；concurrency = 并发 RTT）
macro.json         ab 明细
failover.json      L4
e2e.json           配对差值（可选）
SUMMARY.md         人可读汇总与判定
error.txt          仅失败时存在
env/<实例名>/      config.json、stdout.log
```

`meta.json` 必填字段：

```json
{
  "plan_version": "v1",
  "started_at": "2026-10-23T22:15:00",
  "host": "synthetic-host",
  "platform": "Darwin 25.6.0",
  "machine": "arm64",
  "python": "3.14.0",
  "cpu_count": 14,
  "pswitch_version": "0.1.5",
  "git_commit": "885ed25",
  "argv": ["all"],
  "ports": {"forward": 17100, "direct": 17101}
}
```

`micro.json` 聚合键的命名规则：

```
<拓扑>|<payload>.<steady|fresh>.<rtt|connect|setup|payload|total>.<p50|p90|p99|mean|max>
```

示例（合成值）：

```
"T1_forward|1.steady.rtt.p50": 123.4
"T1_forward|1.fresh.total.p99": 900.2
```

---

## 9. 判定阈值

| 项 | 阈值 | 理由 |
| --- | --- | --- |
| T1 稳态 1 B p50 | ≤ 500 µs | loopback 单跳的合理上界 |
| T1 稳态 1 B p99 | ≤ 2000 µs | 排除偶发调度尖峰 |
| T1 新建连接首包 p50 | ≤ 2000 µs | 含上游拨号的一次性成本 |
| 加载下探测 p99 | ≤ 5000 µs | 两路背景流下仍应可用 |
| 黑洞切换耗时 | 0.5–1.5 × `connect_timeout` | 切换应受配置约束，而非玄学 |

判定只用于提示，默认不影响退出码；`--strict` 时任一 `CHECK` 返回 1。
阈值调整必须提升方案版本并重跑基线。

---

## 10. 已知误差源与排除方法

| 误差源 | 表现 | 排除方法 |
| --- | --- | --- |
| Nagle + delayed ACK | p99 出现约 40 ms 台阶 | 客户端设 `TCP_NODELAY`；确认服务端 transport 也设了 |
| 周期健康检查 | 每 10 s 一个尖峰 | 测试实例设 `health_check_interval=3600` |
| CPU 降频/热漂移 | 后跑的拓扑系统性变慢 | 奇偶轮反转顺序；插电；多轮取中位数 |
| loopback 共享 CPU | 并发升高后所有路径一起变慢 | 记录 pswitch CPU；必要时把目标端放到另一台机器 |
| Python 客户端自身开销 | 绝对 RTT 偏高 | 只用于同一运行内的相对比较；客户端开销在四条拓扑中相同 |
| `curl` 进程启动 | 单次 5–15 ms 噪声 | 端到端只用配对差值，不看单次绝对值 |
| 代理环境变量 | 测试流量绕到生产代理 | 子进程清空代理变量 |
| TIME_WAIT 耗尽 | 高频建连中后期失败 | 新建连接用 RST 关闭；控制样本量 |
| DNS | 偶发解析尖峰 | 全流程只用 `127.0.0.1` |

---

## 11. 方案变更流程

1. 修改本文件并提升“方案版本”。
2. 同步修改脚本中的 `plan_version`、默认参数和判定阈值。
3. 重跑一次完整基线，把 `SUMMARY.md` 路径记录到实现 PR/提交说明中。
4. 旧结果目录保留但标注版本；不同版本的阈值不可跨版本比较。

---

## 12. 落地清单

| 文件 | 作用 | 状态 |
| --- | --- | --- |
| `bench/PLAN.md` | 本方案，唯一权威口径 | 已完成 |
| `bench/target.py` | raw/http test double | 已完成并验证 |
| `bench/bench.py` | 统一驱动与汇总 | 已完成并验证 |
| `bench/README.md` | 使用说明与基线结果 | 已完成 |
| 根 `README.md` | 增加“性能测试”入口 | 已完成 |
| `.gitignore` | 忽略 `bench/results/` | 已完成 |

验证记录：2026-10-23 连续运行两次 `python bench/bench.py all`，关键指标差异均在
20% 以内（T1 1 B steady p50 3.2 / 3.3 µs，新建连接 318 / 315 µs，
背景吞吐 30.25 / 30.43 Gbps）。

实现完成的验收标准：

- `python bench/bench.py all` 一条命令跑完，退出后无残留进程和监听端口。
- 生产实例 127.0.0.1:17000 全程不受影响。
- 结果目录包含 `meta.json` 与 `SUMMARY.md`，且 `meta.json` 字段齐全。
- 连续跑两次，同一指标的 p50 差异应在 20% 以内（同机、同负载）。

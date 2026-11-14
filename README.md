# Notebook 复算服务（recalc-service）

临床统计员交稿前的复算工具：上传分析 notebook、原始 CSV 和稿件结果 JSON，
服务在**独立目录 + 全新 Python 内核**中顺序重跑全部代码单元（忽略 notebook
自带的缓存输出），只比较本次生成的 `result.json` 中的三个效应量字段，给出
一致 / 不一致 / 失败的结论。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs （Swagger UI，可直接上传文件试跑）
```

`POST /batches` 需要三个文件（`samples/` 下有成组样例，见下文）：

| 表单字段      | 内容                                   |
| ------------- | -------------------------------------- |
| `notebook`    | `analysis.ipynb`，待重跑的分析 notebook |
| `observations`| `observations.csv`，原始随访数据        |
| `expected`    | 稿件结果 JSON（含 `estimate`/`ci_low`/`ci_high`） |

提交后轮询 `GET /batches/{batch_id}` 直到 `status` 不再是 `running`，
然后可下载执行后的 notebook 和比较报告。

## API

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/batches` | 提交复算批；满额返回 **429**；成功返回 202 与批次状态 |
| GET  | `/batches` | 全部批次与槽位占用（`slots.active` / `slots.max_parallel`） |
| GET  | `/batches/{id}` | 批次状态与结论（`report` 内嵌比较报告） |
| GET  | `/batches/{id}/notebook` | 下载本次执行后的 notebook（执行中 409；超时被强杀无文件 404） |
| GET  | `/batches/{id}/report` | 下载比较报告 JSON（执行中 409） |

批次状态：`running` → `match`（一致）/ `mismatch`（不一致）/ `failed`（失败）。
**状态只存内存**，服务重启即清空。

## 判定规则

* 只比较本次运行生成的 `result.json` 中的 `estimate`、`ci_low`、`ci_high`；
  其余字段（如 `p_value`）一律忽略。
* 三个字段均须为**有限数**（NaN/Inf/非数值不行），且与稿件值的**绝对误差 ≤ 1e-6**
  才判 `match`；任一不符判 `mismatch`。
* 以下情况判 `failed`（`report.error.kind` 区分）：
  * `timeout` —— 超过 60 秒硬上限（含 kernel 尚未退出的残留计算子进程）；
  * `execution_error` —— 任一代码单元报错或内核异常；
  * `missing_result` —— 未生成 `result.json` 或其不是合法 JSON 对象；
  * `invalid_result` —— 三个字段不全是有限数。

## 并发、超时与进程清理

* 最多 **2** 批并行；满额时新提交立即收到 429，请稍后重试（不排队）。
* 每批硬上限 **60 秒**（墙钟，自 runner 启动起算）。
* 每个批次 = 一个 runner 子进程（独立进程组）+ 一个全新 Jupyter 内核
  （jupyter_client 会再给内核一个独立进程组，runner 把内核 pid 写入
  `kernel.pid` 供服务追踪）+ notebook 自行启动的计算子进程（落在内核进程组）。
* **只有等 runner、内核和本批全部计算子进程都退出后**，服务才公布结论并释放
  并发槽；另一批不受影响。超时则对两个进程组发 `SIGKILL`。
* 批次只**定稿一次**：失败批次目录里之后出现的任何迟到写入（如 detached
  子进程补写的 `result.json`）都不会改变已公布的结论。
* compose 里 `init: true` 用于回收被孤儿化的孙进程，避免僵尸堆积。

环境变量（compose 中可改）：`RECALC_TIMEOUT_SEC`（默认 60）、
`RECALC_MAX_PARALLEL`（默认 2）、`RECALC_WORK_DIR`（默认 /tmp/recalc_batches）。

## 样例（samples/）

虚构的 12 周随访研究（收缩压，治疗组 vs 对照组，各 24 人）；
`expected.json` 由同一份 CSV 实算生成。在 Swagger 的 `POST /batches`
里分别上传同一目录下的三个文件即可：

| 目录 | 预期结论 | 演示点 |
| ---- | ---- | ---- |
| `samples/ok` | `match` | 正常复算一致 |
| `samples/stale_cache` | `match` | notebook 自带旧缓存输出（estimate=999.0），重跑时被忽略 |
| `samples/error` | `failed` / `execution_error` | 第 3 个单元抛异常 |
| `samples/slow` | `failed` / `timeout` | notebook 启动 sleep 120 的计算子进程并自身睡眠，60s 被强杀且子进程一并清理 |
| `samples/late_write` | `failed` / `execution_error` | detached 子进程 3 秒后补写一份**本来一致**的 result.json，失败结论不变 |

curl 示例：

```bash
curl -F "notebook=@samples/ok/analysis.ipynb" \
     -F "observations=@samples/ok/observations.csv" \
     -F "expected=@samples/ok/expected.json" \
     http://localhost:8000/batches
# => {"batch_id": "...", "status": "running", ...}
curl http://localhost:8000/batches/<batch_id>
curl -OJ http://localhost:8000/batches/<batch_id>/notebook
curl -OJ http://localhost:8000/batches/<batch_id>/report
```

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt httpx
python scripts/make_samples.py     # 重新生成 samples/（确定性）
uvicorn app.main:app --port 8000   # 启动服务
python tests/smoke_test.py         # 端到端冒烟测试（自动起临时服务，26 项检查）
```

## 安全说明

notebook 会以服务进程权限**执行任意代码**。本服务面向受信内网/本机使用，
未内置鉴权与资源配额（CPU/内存/网络）；如需暴露到更大范围，请自行加
反向代理鉴权、seccomp/只读根fs/网络隔离等加固措施。

## 已知限制

* 若 notebook 主动用 `setsid`/双重 fork 把子进程**守护化**，该进程会逃出
  进程组追踪（`samples/late_write` 即此情形）：结论的不可变性不受影响，
  但服务无法保证该逃逸进程已退出。
* 批次状态仅内存保存；执行产物（notebook、报告、日志）写在
  `RECALC_WORK_DIR` 下，服务重启时清空。

# 样例批次

虚构的 12 周随访研究：`observations.csv` 为 48 名患者（治疗组/对照组各 24）
的基线与 12 周收缩压；`analysis.ipynb` 计算"相对基线变化"的组间均值差及
Welch 95% CI 并写出 `result.json`；`expected.json` 为稿件申报值（由同一份
CSV 实算生成，含会被忽略的额外字段 `p_value`、`source`）。

在 Swagger UI（`/docs`）的 `POST /batches` 中上传同一目录下的三个文件，
或参考根 README 的 curl 示例。

| 目录 | 预期结论 | 说明 |
| ---- | ---- | ---- |
| `ok/` | `match` | 正常复算一致 |
| `stale_cache/` | `match` | notebook 内含旧缓存输出（estimate=999.0），重跑忽略缓存 |
| `error/` | `failed` / `execution_error` | 单元格抛 `ValueError` |
| `slow/` | `failed` / `timeout` | 派生 sleep 120 的计算子进程并自身睡眠，触发 60s 硬上限 |
| `late_write/` | `failed` / `execution_error` | detached 子进程迟到 3 秒补写一致的 result.json，结论不变 |

重新生成（确定性，固定随机种子）：

```bash
python scripts/make_samples.py
```

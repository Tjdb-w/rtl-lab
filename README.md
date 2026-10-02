# RTL Lab

硬件仿真与 RTL 验证框架：编译 Verilog 设计、运行仿真测试台，收集断言与覆盖率结果并生成可复现的验证报告。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。底层使用 Icarus Verilog（`iverilog` / `vvp`）。

## 用法

### 单次运行

```bash
rtl-lab run design1.v [design2.v ...] \
    --tb tb.v --top tb --duration 100ns \
    [--workdir DIR] [--seed N] [--report report.json]
```

编译一次、以 `+SEED=<seed>` 执行一次仿真，生成 schema v1 报告。

### 多随机种子回归

```bash
rtl-lab regress design1.v [design2.v ...] \
    --tb tb.v --top tb --duration 100ns \
    --seeds 1,2,3 \
    [--workdir DIR] [--report report.json]
```

源文件、测试台、顶层、时长、工作目录与可选 `--report` 与 `run` 相同；
校验一次、编译一次，然后按 `--seeds` 给出的顺序对每个种子以
`+SEED=<seed>` 依次仿真。

`--seeds` 接受逗号分隔的非负整数（允许空白与前导零），空值、负数、
非整数或重复项均为输入错误。

回归报告为 schema v2：`seed` 扩展为 `seeds`，新增 `runs`（按种子顺序，
每个含 `seed`、`status`、`diagnostics`、`assertions`、`coverage`）与
`failed_seeds`；断言与覆盖率按名称首次出现顺序跨种子汇总，覆盖率含
总命中 `hits` 与命中种子数 `hit_runs`。路径与工具位置沿用同一脱敏规则。

Python 入口：

```python
from rtl_lab import regress, RegressConfig

report = regress(RegressConfig(
    sources=["design.v"], testbench="tb.v", top="tb",
    duration="100ns", seeds=[1, 2, 3],
))
```

## 退出码

- 0：全部种子仿真正常结束且断言通过；
- 2：输入校验失败（不生成报告）；
- 3：编译失败（生成 `compile_failed` 报告，回归报告无 `runs`）；
- 4：未找到 `iverilog` / `vvp`（不生成报告）；
- 5：仿真进程非零退出（生成 `simulation_failed` 报告，回归保留已有种子结果并停止后续种子）；
- 6：断言失败（生成 `assertion_failed` 报告，回归继续执行剩余种子）。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。

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

### 统一验证

```bash
rtl-lab verify design1.v [design2.v ...] \
    --run-id ID --duration 100ns \
    --tb FILE[@TOP[@NAME]] ... [--cover NAME ...] \
    [--coverage-threshold R] [--skip NAME ...] [--optional NAME ...] \
    [--tb-seed NAME=SEED ...] [--jobs N] [--baseline PATH] \
    [--workdir DIR] [--report report.json]
```

每个测试台在工作目录下的独立子目录中独立编译、独立仿真；`--jobs N`
仅在测试台之间并发，结果始终按 `--tb` 的选择顺序收集。默认产出
schema v3 报告；加 `--baseline PATH` 时与上次 verify 报告按 name 对比
（testbenches/assertions/coverage 的既有字段）并追加 `comparison`，
为 schema v4，有差异则结论 failed。

### 多种子矩阵验证

```bash
rtl-lab verify design1.v [design2.v ...] \
    --run-id ID --duration 100ns \
    --tb FILE[@TOP[@NAME]] ... --seeds 1,2,3 \
    [--jobs N] [--baseline PATH] [--workdir DIR] [--report report.json]
```

`--seeds` 与 regress 语义一致：逗号分隔的非负十进制整数（允许前导零、
允许项间空白），空值、负数、非整数或重复项均为输入错误；且不可与
`--tb-seed` 并用。非法输入退出 2 且不生成或覆盖报告。

给出 `--seeds` 后：

- 每个未跳过测试台只编译一次，`--jobs` 仍只并行测试台，同一测试台的
  种子按列表顺序串行以 `+SEED=<seed>` 仿真；
- 产出 schema v5 报告；测试台条目保留既有字段与汇总语义，并在末尾新增
  `seeds` 与 `runs`，`runs` 每项含 `seed`、`status`、`reason`、
  `diagnostics`、`assertions`、`coverage`、`simulate`；
- 全部种子通过该台才 `passed`；某种子进程非零退出记
  `simulation_failed` 并停止该台后续种子；断言失败记 `assertion_failed`
  但继续后续种子；正常结束却缺少断言或覆盖率统计记
  `incomplete_statistics`；编译失败时 `runs` 为空、原因为
  `compilation_failed`；跳过的测试台带 `seeds` 但 `runs` 为空；
- 断言按名取跨种子终态并累加 `fail_count`；覆盖率按名跨种子累加
  `hits` 后再跨台合并（`hit_testbenches` 去重），达标率仍为命中点数
  除以总点数。

测试台失败、断言失败、覆盖率未达标、跳过必测项或基线差异任一成立，
总体 `result` 即为 `failed`，否则 `passed`。`--seeds --baseline` 仍为
schema v5（`comparison` 追加在最后）；v3/v4/v5 报告均可作为基线。

Python 入口：

```python
from rtl_lab import verify, VerifyConfig, TestSpec, CoverageConfig

report = verify(VerifyConfig(
    sources=["design.v"],
    testbenches=[TestSpec(testbench="tb.v")],
    run_id="id", duration="100ns",
    coverage=CoverageConfig(threshold=1.0),
    seeds=[1, 2, 3],   # 省略或为 None 时为传统单种子 schema v3
))
```

## 报告版本

- schema v1：`run` 单次报告；
- schema v2：`regress` 多种子回归报告；
- schema v3：`verify` 统一验证报告；
- schema v4：`verify --baseline` 单种子对比报告（v3 字段加 `comparison`）；
- schema v5：`verify --seeds` 多种子矩阵报告（条目加 `seeds`/`runs`；
  再加 `--baseline` 时追加 `comparison`，版本仍为 5）。

## 退出码

- 0：全部种子仿真正常结束且断言通过；
- 2：输入校验失败（不生成报告）；
- 3：编译失败（生成 `compile_failed` 报告，回归报告无 `runs`）；
- 4：未找到 `iverilog` / `vvp`（不生成报告）；
- 5：仿真进程非零退出（生成 `simulation_failed` 报告，回归保留已有种子结果并停止后续种子）；
- 6：断言失败（生成 `assertion_failed` 报告，回归继续执行剩余种子）；
- 7：verify 总体结论 failed 或基线对比存在差异（编译、仿真或断言失败也生成 schema v5 报告）；
- 8：verify 前置条件失败（命名冲突、无可执行测试台、无任何覆盖率结果等，不生成或覆盖报告）。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。

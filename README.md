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

### 多测试台统一验证

```bash
rtl-lab verify design1.v [design2.v ...] \
    --run-id ID --duration 100ns \
    --tb FILE[@TOP[@NAME]] ... [--cover NAME ...] \
    [--coverage-threshold R] [--skip NAME ...] [--optional NAME ...] \
    [--tb-seed NAME=SEED ...] [--jobs N] \
    [--baseline report.json] \
    [--workdir DIR] [--report report.json]
```

各测试台独立编译、独立仿真（产物置于工作目录下的独立子目录），可经
`--jobs` 并发，但结果始终按 `--tb` 选择顺序收集。报告为 schema v3：
含 `run_id`、`format: rtl-lab-verification`、总体 `result`、`config`、
`compilation`、逐台 `testbenches`、跨台 `assertions` 与 `coverage`
（覆盖率点含 `status`/`hits`/`hit_testbenches`）及各自汇总。任一测试台
失败、断言失败、覆盖率未达标或跳过必测项时 `result` 为 `failed`，但仍
生成完整报告。

#### 基线对比（schema v4）

指定 `--baseline PATH`（对应 `VerifyConfig.baseline_path`，PATH 指向上次
`verify` 生成的 JSON 报告）时，命令先生成当前结果，再把测试台、断言、
覆盖率分别按 `name` 与基线对齐；执行顺序、种子、产物目录与字段语义均不
改变。报告升级为 schema v4，在 schema v3 全部字段之后追加 `comparison`：

```json
{ "baseline": "<脱敏后的基线路径>", "passed": true, "mismatches": [ ... ] }
```

每类记录分别报告基线缺失（`*.missing`）、当前新增（`*.added`）与同名
记录的字段差异；每条差异固定为 `kind`、`name`、`expected`、`actual` 四
字段：缺失时 `expected` 为基线完整记录、`actual` 为 `null`，新增时
`expected` 为 `null`、`actual` 为当前完整记录，字段差异时二者为对应
标量。逐字段比较的范围为：

- 测试台：`status`、`reason`；
- 断言：终态 `status`、`fail_count`；
- 覆盖率：`status`、`hits`、`hit_testbenches`。

差异按类别（testbench → assertion → coverage）与 name 的 Unicode 码点
排序，同名的多个字段差异分别保留、不合并。存在任一差异时 `result` 为
`failed`、退出码 7，命令行末尾汇总差异条数；无差异时按当前检查决定
`result` 与退出码。基线不存在、不可读、非合法 JSON、不是 rtl-lab 验证
报告或版本不受支持时，按输入错误处理（退出码 2），不生成或覆盖报告；
测试台失败、断言失败、覆盖率未达标或仿真异常退出时仍生成完整报告与
comparison，且比较不会中止其余测试台。

## 退出码

- 0：全部种子仿真正常结束且断言通过；verify 为总体结论 `passed`；
- 2：输入校验失败（不生成报告），含 verify 基线非法；
- 3：编译失败（生成 `compile_failed` 报告，回归报告无 `runs`）；
- 4：未找到 `iverilog` / `vvp`（不生成报告）；
- 5：仿真进程非零退出（生成 `simulation_failed` 报告，回归保留已有种子结果并停止后续种子）；
- 6：断言失败（生成 `assertion_failed` 报告，回归继续执行剩余种子）；
- 7：verify 总体结论 `failed`（仍生成完整报告）；带基线时存在任一基线差异同样为 7；
- 8：verify 前置条件失败（`run_id` 为空、报告输出位置不可写、结果归属/命名冲突、无可执行测试台或无任何覆盖率结果；不生成或覆盖报告）。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。

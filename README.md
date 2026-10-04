# RTL Lab

硬件仿真与 RTL 验证框架：编译 Verilog 设计、运行仿真测试台，收集断言与覆盖率结果并生成可复现的验证报告。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。底层使用 Icarus Verilog（`iverilog` / `vvp`）。

## 用法

### 单次运行

```bash
rtl-lab run design1.v [design2.v ...] \
    --tb tb.v --top tb --duration 100ns \
    [--workdir DIR] [--seed N] [--timeout SECONDS] [--report report.json] \
    [--incdir DIR ...] [--define NAME[=VALUE] ...] \
    [--parameter PATH=VALUE ...]
```

编译一次、以 `+SEED=<seed>` 执行一次仿真，生成 schema v1 报告。

### 多随机种子回归

```bash
rtl-lab regress design1.v [design2.v ...] \
    --tb tb.v --top tb --duration 100ns \
    --seeds 1,2,3 \
    [--workdir DIR] [--timeout SECONDS] [--report report.json] \
    [--incdir DIR ...] [--define NAME[=VALUE] ...] \
    [--parameter PATH=VALUE ...]
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

### 进程超时

三个命令均可选 `--timeout SECONDS`（Python 入口为配置对象的
`timeout`）：对每次独立启动的 `iverilog` / `vvp` 进程分别施加墙钟
超时，计时从进程启动到自然结束，与仿真时长无关。只接受正十进制整数
秒；省略则不限制，零、负数、小数、非数字或空值均为输入错误（退出 2，
不生成或覆盖报告）。

`run` 的编译与仿真分别计时；`regress` 编译一次后按种子分别计时；
`verify` 每个测试台独立编译、每个种子独立仿真，各次调用独立计时，
`--jobs` 只并行测试台，不合并或平均各次调用的预算。达到上限时终止
该次进程及其残留子进程，保留已捕获的 stdout，并在该阶段 stderr
末尾追加稳定标记 `RTL_LAB_PROCESS_TIMEOUT`（对应 diagnostics 同样
包含该标记）。

超时归入既有阶段失败，不新增报告版本：编译超时等同编译失败
（`run`/`regress` 退出 3；`verify` 该测试台 `compilation_failed`，
多种子时 `runs` 为空）；仿真超时等同仿真非零退出（`run`/`regress`
退出 5；`verify` 当前种子 `simulation_failed` 并停止该台后续种子，
`regress` 保留此前种子结果后停止）。

### include 目录与宏定义

三个命令均可重复给出 `--incdir DIR` 与 `--define NAME[=VALUE]`
（Python 入口为配置对象的 `include_dirs` 与 `defines` 列表）：编译时
按给出顺序把每个目录转换为 iverilog 的 `-I` 参数、每个定义转换为
`-D` 参数，随源文件、顶层、测试台与看门狗一起用于每次编译。`run` 编译
一次，`regress` 编译一次后复用于全部种子，`verify` 仍按测试台独立
编译。省略时编译命令与既有行为完全一致。

`--define` 接受 `NAME` 或 `NAME=VALUE`：`NAME` 只能由 ASCII 字母、
数字和下划线组成且首字符不能是数字；`VALUE` 可为空（空值仍按已定义
处理）并可含等号。相对目录按启动命令时的当前目录解析。输入在启动
iverilog 前一次性校验：`--incdir` 为空、路径不存在或不是目录，
`--define` 为空、名称非法或同一 `NAME` 重复，均为输入错误（退出 2，
不生成或覆盖报告）；目录与定义都不得包含换行、回车或 NUL。

新参数只出现在实际编译命令及报告记录中（沿用既有路径脱敏），报告
字段、schema 版本与退出码不变；include 文件缺失或宏导致编译错误时
沿用既有编译失败结果。

### 设计参数覆盖

三个命令均可重复给出 `--parameter PATH=VALUE`（Python 入口为配置对象
的 `parameters` 列表，按输入顺序保存）：编译时按给出顺序把每项转换为
iverilog 的 `-P` 参数，随源文件、顶层、测试台与看门狗一起用于每次
编译，从而用同一套源码与测试台验证不同编译期参数组合。`run` 编译一次，
`regress` 编译一次后复用于全部种子，`verify` 仍按测试台独立编译。
省略时编译命令与既有行为完全一致。

`PATH` 为从所选顶层开始的点分参数层级名（如 `top.U_DUT.WIDTH`）：
只能由 ASCII 字母、数字、下划线和点组成，首末字符不能是点，不得出现
连续点，每一层不能以数字开头；同一次执行中同一 `PATH` 不可重复。
`VALUE` 可为空、可含等号与空白，但不得包含换行、回车或 NUL。所有
参数输入在启动 iverilog 前一次性校验：空 `PATH`、非法 `PATH`、重复
`PATH` 或 `VALUE` 含换行/回车/NUL 均为输入错误（退出 2，不生成或
覆盖报告）；参数无法被设计接受或导致预处理、elaboration 失败时沿用
既有编译失败结果（退出 3，生成 `compile_failed` 报告）。

参数只出现在实际编译命令及报告记录中（沿用既有路径脱敏），报告
字段、字段顺序与 schema 版本不变；相同输入顺序、工作目录与工具环境
下参数顺序与报告内容一致（仅 `generated_at` 可变化）。

### 统一验证

```bash
rtl-lab verify design1.v [design2.v ...] \
    --run-id ID --duration 100ns \
    --tb FILE[@TOP[@NAME]] ... [--cover NAME ...] \
    [--coverage-threshold R] [--skip NAME ...] [--optional NAME ...] \
    [--tb-seed NAME=SEED ...] [--jobs N] [--baseline PATH] \
    [--timeout SECONDS] [--workdir DIR] [--report report.json] \
    [--incdir DIR ...] [--define NAME[=VALUE] ...] \
    [--parameter PATH=VALUE ...]
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

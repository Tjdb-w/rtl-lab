"""load_verify_manifest 单元测试：清单解析、校验与相对路径解析。"""

import json
import os

import pytest

from rtl_lab import InputError, VerifyConfig, load_verify_manifest, verify


def _write_manifest(tmp_path, data, name="manifest.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _base_manifest(**overrides):
    data = {
        "schema_version": 1,
        "run_id": "rid",
        "duration": "100ns",
        "sources": ["rtl/d1.v", "rtl/d2.v"],
        "testbenches": [{"testbench": "tb/tb_pass.v"}],
        "coverage": {"threshold": 0.5, "points": ["c1", "c2"]},
    }
    data.update(overrides)
    return data


def test_loads_equivalent_config(tmp_path):
    path = _write_manifest(tmp_path, _base_manifest())
    config = load_verify_manifest(str(path))
    assert isinstance(config, VerifyConfig)
    assert config.run_id == "rid"
    assert config.duration == "100ns"
    # 相对路径以清单目录解析为绝对路径，数组顺序保持。
    assert config.sources == [
        os.path.join(str(tmp_path), "rtl", "d1.v"),
        os.path.join(str(tmp_path), "rtl", "d2.v"),
    ]
    assert len(config.testbenches) == 1
    spec = config.testbenches[0]
    assert spec.testbench == os.path.join(str(tmp_path), "tb", "tb_pass.v")
    # 省略 top/name 时取文件 basename，required/seed 取默认值。
    assert spec.top == "tb_pass"
    assert spec.name == "tb_pass"
    assert spec.required is True
    assert spec.skip is False
    assert spec.seed == 0
    assert config.coverage.threshold == 0.5
    assert config.coverage.points == ["c1", "c2"]
    # workdir 省略时默认 "."，同样按清单目录解析。
    assert config.workdir == str(tmp_path)
    assert config.jobs == 1
    assert config.seeds == []
    assert config.timeout is None
    assert config.report_path is None
    assert config.baseline_path is None


def test_optional_fields_and_testbench_options(tmp_path):
    data = _base_manifest(
        testbenches=[
            {"testbench": "tb/a.v", "top": "top_a", "name": "alpha",
             "required": False, "skip": True, "seed": 7},
            {"testbench": "tb/b.v"},
        ],
        compile={
            "include_dirs": ["inc", "inc2"],
            "defines": ["WIDTH=8", "DEBUG"],
            "parameters": ["top.U_DUT.WIDTH=8"],
        },
        seeds=[1, 2, 3],
        jobs=2,
        timeout=60,
        workdir="build/verify",
    )
    # 多种子矩阵下逐测试台种子必须全为 0，这里 seeds 与 seed=7 冲突，
    # 但冲突由 verify 校验而非加载；先去掉逐台种子再测加载。
    data["testbenches"][0].pop("seed")
    path = _write_manifest(tmp_path, data)
    config = load_verify_manifest(str(path))
    alpha, beta = config.testbenches
    assert (alpha.top, alpha.name) == ("top_a", "alpha")
    assert alpha.required is False
    assert alpha.skip is True
    assert beta.required is True
    assert config.include_dirs == [
        os.path.join(str(tmp_path), "inc"),
        os.path.join(str(tmp_path), "inc2"),
    ]
    assert config.defines == ["WIDTH=8", "DEBUG"]
    assert config.parameters == ["top.U_DUT.WIDTH=8"]
    assert config.seeds == [1, 2, 3]
    assert config.jobs == 2
    assert config.timeout == 60
    assert config.workdir == os.path.join(str(tmp_path), "build", "verify")


def test_testbench_seed_passthrough(tmp_path):
    data = _base_manifest(
        testbenches=[{"testbench": "tb/a.v", "seed": 7}],
    )
    path = _write_manifest(tmp_path, data)
    config = load_verify_manifest(str(path))
    assert config.testbenches[0].seed == 7


def test_absolute_paths_kept(tmp_path):
    data = _base_manifest(sources=["/abs/d.v"], workdir="/abs/work")
    path = _write_manifest(tmp_path, data)
    config = load_verify_manifest(str(path))
    assert config.sources == ["/abs/d.v"]
    assert config.workdir == "/abs/work"


def test_load_does_not_require_files_to_exist(tmp_path):
    # 加载只构造配置：源文件/测试台是否存在属于 verify 阶段校验。
    path = _write_manifest(tmp_path, _base_manifest())
    config = load_verify_manifest(str(path))
    assert not os.path.exists(config.sources[0])


@pytest.mark.parametrize("bad_version", [0, 2, "1", 1.0, True, None])
def test_bad_schema_version_exit_input_error(tmp_path, bad_version):
    data = _base_manifest()
    if bad_version is None:
        del data["schema_version"]
    else:
        data["schema_version"] = bad_version
    path = _write_manifest(tmp_path, data)
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("key", ["schema", "report", "baseline", "unknown"])
def test_unknown_top_level_key(tmp_path, key):
    path = _write_manifest(tmp_path, _base_manifest(**{key: "x"}))
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("key", ["schema_version", "run_id", "duration",
                                 "sources", "testbenches", "coverage"])
def test_missing_required_key(tmp_path, key):
    if key == "schema_version":
        pytest.skip("schema_version 缺失按版本错误处理，另有测试覆盖")
    data = _base_manifest()
    del data[key]
    path = _write_manifest(tmp_path, data)
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("mutate", [
    lambda d: d["testbenches"][0].update(foo=1),
    lambda d: d["coverage"].update(foo=1),
    lambda d: d.update(compile={"foo": 1}),
])
def test_unknown_nested_key(tmp_path, mutate):
    data = _base_manifest()
    mutate(data)
    path = _write_manifest(tmp_path, data)
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(sources="rtl/d.v"),
    lambda d: d.update(sources=[""]),
    lambda d: d.update(sources=[3]),
    lambda d: d.update(testbenches={"testbench": "tb/a.v"}),
    lambda d: d.update(testbenches=[{"top": "t"}]),
    lambda d: d.update(testbenches=[{"testbench": ""}]),
    lambda d: d.update(testbenches=[{"testbench": "tb/a.v", "seed": True}]),
    lambda d: d.update(testbenches=[{"testbench": "tb/a.v", "seed": "7"}]),
    lambda d: d.update(testbenches=[{"testbench": "tb/a.v", "required": 1}]),
    lambda d: d.update(testbenches=[{"testbench": "tb/a.v", "skip": "no"}]),
    lambda d: d.update(testbenches=[{"testbench": "tb/a.v", "top": ""}]),
    lambda d: d.update(coverage=[]),
    lambda d: d.update(coverage={"points": "c1"}),
    lambda d: d.update(coverage={"threshold": 1.5}),
    lambda d: d.update(coverage={"threshold": "0.5"}),
    lambda d: d.update(compile=[]),
    lambda d: d.update(compile={"include_dirs": "inc"}),
    lambda d: d.update(compile={"defines": [3]}),
    lambda d: d.update(workdir=""),
    lambda d: d.update(workdir=3),
    lambda d: d.update(seeds="1,-1"),
    lambda d: d.update(seeds={"a": 1}),
    lambda d: d.update(timeout="1.5"),
    lambda d: d.update(timeout=0),
])
def test_invalid_fields_raise_input_error(tmp_path, mutate):
    data = _base_manifest()
    mutate(data)
    path = _write_manifest(tmp_path, data)
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_missing_file_raises_input_error(tmp_path):
    with pytest.raises(InputError):
        load_verify_manifest(str(tmp_path / "nope.json"))


def test_not_json_raises_input_error(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("not json {", encoding="utf-8")
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_non_object_json_raises_input_error(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_non_utf8_raises_input_error(tmp_path):
    path = tmp_path / "m.json"
    path.write_bytes(b"\xff\xfe{\x00}")
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_jobs_validation_deferred_to_config(tmp_path):
    # jobs 非法沿用 VerifyConfig 既有 ValueError 语义（退出码 8）。
    path = _write_manifest(tmp_path, _base_manifest(jobs=0))
    with pytest.raises(ValueError):
        load_verify_manifest(str(path))


def test_run_id_validation_deferred_to_verify(tmp_path):
    # run_id 为空沿用 verify 既有 ValueError 语义（退出码 8）；
    # 加载本身不校验 run_id。
    path = _write_manifest(tmp_path, _base_manifest(run_id="  "))
    config = load_verify_manifest(str(path))
    with pytest.raises(ValueError):
        verify(config)


def test_seeds_matrix_conflicts_with_tb_seed(tmp_path):
    # 顶层 seeds 与测试台 seed 并用：沿用 verify 既有 InputError（退出 2）。
    src = tmp_path / "d.v"
    src.write_text("module d; endmodule\n")
    tb = tmp_path / "tb.v"
    tb.write_text("module tb; endmodule\n")
    data = _base_manifest(
        sources=["d.v"],
        testbenches=[{"testbench": "tb.v", "seed": 7}],
        seeds=[1, 2],
    )
    path = _write_manifest(tmp_path, data)
    config = load_verify_manifest(str(path))
    with pytest.raises(InputError):
        verify(config)

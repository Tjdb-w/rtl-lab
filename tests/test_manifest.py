"""load_verify_manifest 单元测试：结构校验、路径解析与配置等价构造。

本文件只构造 VerifyConfig，不执行编译或仿真，无需 Icarus Verilog。
"""

import json

import pytest

from rtl_lab import (
    CoverageConfig,
    InputError,
    TestSpec,
    VerifyConfig,
    load_verify_manifest,
)
from rtl_lab.manifest import MANIFEST_SCHEMA_VERSION


def _write_manifest(tmp_path, data, name="manifest.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _minimal(**overrides):
    data = {
        "schema_version": 1,
        "run_id": "rid",
        "duration": "100ns",
        "sources": ["d.v"],
        "testbenches": [{"testbench": "tb.v"}],
        "coverage": {"threshold": 1.0, "points": ["c1"]},
    }
    data.update(overrides)
    return data


def test_minimal_manifest_builds_equivalent_config(tmp_path):
    path = _write_manifest(tmp_path, _minimal())
    config = load_verify_manifest(str(path))
    assert isinstance(config, VerifyConfig)
    assert config.run_id == "rid"
    assert config.duration == "100ns"
    # 相对路径按清单所在目录解析。
    assert config.sources == [str(tmp_path / "d.v")]
    assert config.testbenches == [TestSpec(testbench=str(tmp_path / "tb.v"))]
    assert config.coverage == CoverageConfig(threshold=1.0, points=["c1"])
    # 缺省字段与逐项参数等价。
    assert config.workdir == "."
    assert config.jobs == 1
    assert config.seeds == []
    assert config.timeout is None
    assert config.include_dirs == []
    assert config.defines == []
    assert config.parameters == []
    assert config.report_path is None
    assert config.baseline_path is None


def test_manifest_schema_version_constant():
    assert MANIFEST_SCHEMA_VERSION == 1


def test_relative_paths_resolve_against_manifest_dir(tmp_path):
    sub = tmp_path / "proj"
    sub.mkdir()
    path = _write_manifest(sub, _minimal(
        sources=["src/d.v"],
        testbenches=[{"testbench": "tb/tb.v"}],
        compile={"include_dirs": ["inc"], "defines": ["A=1"],
                 "parameters": ["top.W=8"]},
        workdir="work",
    ))
    config = load_verify_manifest(str(path))
    assert config.sources == [str(sub / "src" / "d.v")]
    assert config.testbenches[0].testbench == str(sub / "tb" / "tb.v")
    assert config.include_dirs == [str(sub / "inc")]
    assert config.workdir == str(sub / "work")
    # defines/parameters 不是路径，原样保留。
    assert config.defines == ["A=1"]
    assert config.parameters == ["top.W=8"]


def test_absolute_paths_kept_as_is(tmp_path):
    sub = tmp_path / "proj"
    sub.mkdir()
    src = tmp_path / "elsewhere" / "d.v"
    path = _write_manifest(sub, _minimal(sources=[str(src)]))
    config = load_verify_manifest(str(path))
    assert config.sources == [str(src)]


def test_manifest_in_cwd_leaves_relative_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _write_manifest(tmp_path, _minimal(), name="m.json")
    config = load_verify_manifest("m.json")
    assert config.sources == ["d.v"]


def test_full_manifest_fields(tmp_path):
    path = _write_manifest(tmp_path, _minimal(
        testbenches=[
            {"testbench": "a.v", "top": "ta", "name": "alpha",
             "required": False, "skip": True, "seed": 7},
            {"testbench": "b.v"},
        ],
        seeds=[1, 2, 3],
        jobs=4,
        timeout=30,
    ))
    config = load_verify_manifest(str(path))
    first, second = config.testbenches
    assert (first.top, first.name) == ("ta", "alpha")
    assert first.required is False and first.skip is True and first.seed == 7
    assert second.required is True and second.skip is False and second.seed == 0
    assert config.seeds == [1, 2, 3]
    assert config.jobs == 4
    assert config.timeout == 30


def test_array_order_preserved(tmp_path):
    path = _write_manifest(tmp_path, _minimal(
        sources=["c.v", "a.v", "b.v"],
        testbenches=[{"testbench": "z.v"}, {"testbench": "y.v"}],
        coverage={"threshold": 0.5, "points": ["p2", "p1"]},
    ))
    config = load_verify_manifest(str(path))
    assert config.sources == [str(tmp_path / n) for n in ("c.v", "a.v", "b.v")]
    assert [t.testbench for t in config.testbenches] == [
        str(tmp_path / "z.v"), str(tmp_path / "y.v"),
    ]
    assert config.coverage.points == ["p2", "p1"]


def test_load_does_not_require_files_to_exist(tmp_path):
    # 文件存在性属于 verify 阶段校验；load 只构造配置。
    path = _write_manifest(tmp_path, _minimal(sources=["nope.v"]))
    config = load_verify_manifest(str(path))
    assert config.sources == [str(tmp_path / "nope.v")]


def test_missing_file_raises_input_error(tmp_path):
    with pytest.raises(InputError):
        load_verify_manifest(str(tmp_path / "absent.json"))


def test_invalid_json_raises_input_error(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_non_utf8_raises_input_error(tmp_path):
    path = tmp_path / "m.json"
    path.write_bytes(b'{"schema_version": 1, "x": "\xff\xfe"}')
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("payload", ["[1, 2]", '"text"', "42", "null", "true"])
def test_non_object_json_raises_input_error(tmp_path, payload):
    path = tmp_path / "m.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("version", [0, 2, -1, "1", 1.0, True, None])
def test_bad_schema_version_raises_input_error(tmp_path, version):
    data = _minimal()
    if version is None:
        del data["schema_version"]
    else:
        data["schema_version"] = version
    path = _write_manifest(tmp_path, data)
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_unknown_top_level_key_raises_input_error(tmp_path):
    path = _write_manifest(tmp_path, _minimal(extra=1))
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("section,key", [
    ("testbenches", "bogus"),
    ("coverage", "bogus"),
    ("compile", "bogus"),
])
def test_unknown_nested_key_raises_input_error(tmp_path, section, key):
    data = _minimal(compile={"include_dirs": []})
    if section == "testbenches":
        data["testbenches"][0][key] = 1
    else:
        data[section][key] = 1
    path = _write_manifest(tmp_path, data)
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("missing", [
    "run_id", "duration", "sources", "testbenches", "coverage",
])
def test_missing_required_key_raises_input_error(tmp_path, missing):
    data = _minimal()
    del data[missing]
    path = _write_manifest(tmp_path, data)
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_testbench_entry_requires_testbench_key(tmp_path):
    path = _write_manifest(tmp_path, _minimal(testbenches=[{"top": "t"}]))
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("field,value", [
    ("run_id", 1),
    ("duration", 100),
    ("sources", "d.v"),
    ("sources", [1]),
    ("testbenches", {"testbench": "tb.v"}),
    ("testbenches", ["tb.v"]),
    ("coverage", []),
    ("compile", []),
    ("seeds", "1,2"),
    ("jobs", "2"),
    ("jobs", True),
    ("workdir", 3),
])
def test_field_type_errors_raise_input_error(tmp_path, field, value):
    path = _write_manifest(tmp_path, _minimal(**{field: value}))
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("item", [
    {"testbench": 1},
    {"testbench": "tb.v", "top": 1},
    {"testbench": "tb.v", "name": 1},
    {"testbench": "tb.v", "required": 1},
    {"testbench": "tb.v", "skip": "no"},
    {"testbench": "tb.v", "seed": 1.5},
    {"testbench": "tb.v", "seed": True},
])
def test_testbench_field_type_errors_raise_input_error(tmp_path, item):
    path = _write_manifest(tmp_path, _minimal(testbenches=[item]))
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


@pytest.mark.parametrize("compile_value", [
    {"include_dirs": "inc"},
    {"defines": [1]},
    {"parameters": "top.W=8"},
])
def test_compile_field_type_errors_raise_input_error(tmp_path, compile_value):
    path = _write_manifest(tmp_path, _minimal(compile=compile_value))
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_bad_threshold_raises_input_error(tmp_path):
    path = _write_manifest(
        tmp_path, _minimal(coverage={"threshold": 2.0, "points": []})
    )
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_bad_seeds_raise_input_error(tmp_path):
    path = _write_manifest(tmp_path, _minimal(seeds=[1, 1]))
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_bad_timeout_raises_input_error(tmp_path):
    path = _write_manifest(tmp_path, _minimal(timeout=0))
    with pytest.raises(InputError):
        load_verify_manifest(str(path))


def test_bad_jobs_value_raises_value_error(tmp_path):
    # 与逐项参数一致：jobs 取值非法沿用 ValueError（退出码 8）。
    path = _write_manifest(tmp_path, _minimal(jobs=0))
    with pytest.raises(ValueError):
        load_verify_manifest(str(path))


def test_seeds_matrix_with_tb_seed_rejected_at_verify(tmp_path):
    # 多种子矩阵与测试台 seed 互斥沿用 verify 的既有校验（InputError）。
    from rtl_lab import verify

    (tmp_path / "d.v").write_text("module d; endmodule\n")
    (tmp_path / "tb.v").write_text("module tb; endmodule\n")
    path = _write_manifest(tmp_path, _minimal(
        seeds=[1, 2],
        testbenches=[{"testbench": "tb.v", "seed": 3}],
    ))
    config = load_verify_manifest(str(path))
    with pytest.raises(InputError):
        verify(config)

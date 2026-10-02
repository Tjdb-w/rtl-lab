"""verify 编排辅助（子目录名规划）单测，不调用仿真器。"""

from rtl_lab.verification import TestSpec, _plan_tb_dirs


def _spec(name):
    return TestSpec(testbench=f"{name}.v", name=name, top=name)


def test_plain_names_kept():
    specs = [_spec("alpha"), _spec("beta")]
    dirs = _plan_tb_dirs(specs)
    assert dirs == {"alpha": "alpha", "beta": "beta"}


def test_collision_gets_distinct_stable_suffixes():
    # "a b" 与 "a_b" 清洗后都变成 a_b，须消歧且稳定。
    specs = [_spec("a b"), _spec("a_b")]
    dirs = _plan_tb_dirs(specs)
    assert dirs["a b"] != dirs["a_b"]
    assert dirs["a b"].startswith("a_b_")
    # 同一输入再规划一次，结果完全一致（与机器、调用次序无关）。
    assert _plan_tb_dirs(specs) == dirs
    assert _plan_tb_dirs(list(reversed(specs))) == dirs


def test_non_colliding_unsafely_named_stays_clean():
    specs = [_spec("a b"), _spec("c.d")]
    dirs = _plan_tb_dirs(specs)
    assert dirs == {"a b": "a_b", "c.d": "c.d"}

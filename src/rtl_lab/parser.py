"""解析测试台标准输出中的单行结果。

支持三类记录（均为单行，名称为去除首尾空白后非空的单个 token）：

- ``ASSERT PASS name``
- ``ASSERT FAIL name message``（message 为 name 之后的全部文本，可为空）
- ``COVER name``

断言状态取最后一次结果；失败次数按 FAIL 记录累加；
覆盖率点按命中次数累加。所有映射均按名称首次出现的顺序保存。
"""

import re

#: 三类记录的标记前缀。
ASSERT_PREFIX = "ASSERT PASS "
ASSERT_FAIL_PREFIX = "ASSERT FAIL "
COVER_PREFIX = "COVER "

#: 名称为首个非空白 token；FAIL 的 message 为名称之后的全部文本。
_PASS_RE = re.compile(r"^ASSERT\s+PASS\s+(\S+)\s*$")
_FAIL_RE = re.compile(r"^ASSERT\s+FAIL\s+(\S+)(?:\s+(.*?))?\s*$")
_COVER_RE = re.compile(r"^COVER\s+(\S+)\s*$")


class ResultCollector:
    """按行收集断言与覆盖率结果。"""

    def __init__(self):
        #: name -> {"status": "passed"/"failed", "fail_count": int}
        self.assertions = {}
        #: name -> 命中次数
        self.coverage = {}

    def feed_line(self, line):
        """处理一行（不含行尾换行）。无法识别的行将被忽略。

        标记与名称之间允许任意空白（空格或制表符）。

        :returns: 识别出的记录元组（仅供调试），未识别返回 None。
        """
        fail = _FAIL_RE.match(line)
        if fail:
            name = fail.group(1)
            message = fail.group(2) or ""
            entry = self.assertions.setdefault(
                name, {"status": "failed", "fail_count": 0}
            )
            entry["status"] = "failed"
            entry["fail_count"] += 1
            return ("assert_fail", name, message)

        pas = _PASS_RE.match(line)
        if pas:
            name = pas.group(1)
            entry = self.assertions.setdefault(
                name, {"status": "passed", "fail_count": 0}
            )
            entry["status"] = "passed"
            return ("assert_pass", name)

        cov = _COVER_RE.match(line)
        if cov:
            name = cov.group(1)
            self.coverage[name] = self.coverage.get(name, 0) + 1
            return ("cover", name)

        return None

    def feed_text(self, text):
        """处理整段文本，按 \\n / \\r\\n 分行。"""
        for raw in text.splitlines():
            self.feed_line(raw)

    @property
    def has_failure(self):
        """是否出现过且最终仍处于失败状态的断言。"""
        return any(a["status"] == "failed" for a in self.assertions.values())

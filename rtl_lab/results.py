"""Parse ASSERT/COVER result lines emitted by a testbench.

Recognised single-line forms (leading/trailing whitespace is ignored, and
unrecognised lines are ordinary simulator output):

    ASSERT PASS <name>
    ASSERT FAIL <name> [<message>]
    COVER <name>

The name is the first whitespace-delimited token after the verdict (for
ASSERT FAIL, the remainder is the free-form failure message). A result
whose name is empty after stripping whitespace is ignored.
"""

from collections import OrderedDict


class AssertionRecord:
    __slots__ = ("name", "status", "fail_count")

    def __init__(self, name):
        self.name = name
        self.status = "pass"
        self.fail_count = 0

    def to_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "fail_count": self.fail_count,
        }


class CoverageRecord:
    __slots__ = ("name", "hits")

    def __init__(self, name):
        self.name = name
        self.hits = 0

    def to_dict(self):
        return {"name": self.name, "hits": self.hits}


class ResultCollector:
    """Collects assertion/coverage results in first-seen order.

    An assertion's status is taken from its *last* matching result line;
    ``fail_count`` accumulates every FAIL line seen for that name.
    """

    def __init__(self):
        self._assertions = OrderedDict()
        self._coverage = OrderedDict()

    def feed_line(self, line):
        if line.endswith("\n"):
            line = line[:-1]
        stripped = line.strip()
        if not stripped:
            return

        # Names are single whitespace-delimited tokens; the rest of a FAIL
        # line is the free-form message (currently not reported).
        tokens = stripped.split()
        kind = tokens[0]
        if kind == "COVER":
            if len(tokens) != 2:
                return
            name = tokens[1]
            record = self._coverage.get(name)
            if record is None:
                record = CoverageRecord(name)
                self._coverage[name] = record
            record.hits += 1
            return

        if kind != "ASSERT" or len(tokens) < 3:
            return

        verdict = tokens[1]
        if verdict not in ("PASS", "FAIL"):
            return
        name = tokens[2]
        if not name:
            return

        record = self._assertions.get(name)
        if record is None:
            record = AssertionRecord(name)
            self._assertions[name] = record
        record.status = "fail" if verdict == "FAIL" else "pass"
        if verdict == "FAIL":
            record.fail_count += 1

    def feed_text(self, text):
        for line in text.splitlines():
            self.feed_line(line)

    @property
    def assertions(self):
        return [rec.to_dict() for rec in self._assertions.values()]

    @property
    def coverage(self):
        return [rec.to_dict() for rec in self._coverage.values()]

    @property
    def failed_assertions(self):
        return [name for name, rec in self._assertions.items() if rec.status == "fail"]

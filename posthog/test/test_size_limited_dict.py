import unittest

from parameterized import parameterized

from posthog import utils


class TestSizeLimitedDict(unittest.TestCase):
    @parameterized.expand([(10, 100), (5, 20), (20, 200)])
    def test_size_limited_dict(self, size: int, iterations: int) -> None:
        values = utils.SizeLimitedDict(size, lambda _: -1)

        for i in range(iterations):
            values[i] = i

            assert values[i] == i
            # Capacity is a ceiling, not a reset point: the dict fills up and then
            # stays full instead of collapsing back to one entry.
            assert len(values) == min(i + 1, size)

            # The most recent `size` keys survive; only what fell off the front is gone.
            for recent in range(max(0, i - size + 1), i + 1):
                assert values.get(recent) == recent
            if i >= size:
                self.assertIsNone(values.get(i - size))

    @parameterized.expand([(10, 100), (5, 20), (20, 200)])
    def test_size_limited_dict_evicts_incrementally(
        self, size: int, iterations: int
    ) -> None:
        """Passing capacity must drop one oldest entry, not clear the whole mapping."""
        values = utils.SizeLimitedDict(size, lambda _: -1)

        for i in range(size):
            values[i] = i

        for i in range(size, iterations):
            values[i] = i

            assert len(values) == size
            # Exactly one entry was evicted per insertion, so everything but the
            # oldest key is still deduped.
            self.assertIsNone(values.get(i - size))
            assert values.get(i - size + 1) == i - size + 1

    def test_size_limited_dict_overwrite_at_capacity_does_not_evict(self) -> None:
        values = utils.SizeLimitedDict(3, lambda _: -1)
        for i in range(3):
            values[i] = i

        values[0] = "updated"

        assert len(values) == 3
        assert values[0] == "updated"
        assert values[1] == 1
        assert values[2] == 2

    def test_size_limited_dict_forwards_defaultdict_args_and_kwargs(self) -> None:
        values = utils.SizeLimitedDict(
            3, lambda: "missing", {"existing": "value"}, other="item"
        )

        assert values["missing"] == "missing"
        assert values["existing"] == "value"
        assert values["other"] == "item"
        assert values.max_size == 3

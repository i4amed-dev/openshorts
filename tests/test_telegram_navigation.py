"""Pagination must clamp instead of raising — a stale or forged page number in
callback_data (double-tap at a boundary, an edited request) must never crash
a handler — and must be idempotent at both edges.
"""
from __future__ import annotations

from telegram_bot import navigation


class TestPaginate:
    def test_first_page(self):
        items, page, total = navigation.paginate(list(range(12)), 0, 5)
        assert items == [0, 1, 2, 3, 4]
        assert (page, total) == (0, 3)

    def test_last_partial_page(self):
        items, page, total = navigation.paginate(list(range(12)), 2, 5)
        assert items == [10, 11]
        assert (page, total) == (2, 3)

    def test_negative_page_clamps_to_zero(self):
        items, page, _total = navigation.paginate(list(range(12)), -5, 5)
        assert page == 0
        assert items == [0, 1, 2, 3, 4]

    def test_page_far_past_the_end_clamps_to_the_last_page(self):
        items, page, total = navigation.paginate(list(range(12)), 999, 5)
        assert page == total - 1 == 2
        assert items == [10, 11]

    def test_empty_list_never_raises(self):
        items, page, total = navigation.paginate([], 0, 5)
        assert items == []
        assert (page, total) == (0, 1)


class TestPaginationRow:
    def test_first_page_has_no_previous_button(self):
        row = navigation.pagination_row("candidates", "list", 0, 3)
        labels = [b.text for b in row]
        assert "◀" not in labels
        assert "▶" in labels

    def test_last_page_has_no_next_button(self):
        row = navigation.pagination_row("candidates", "list", 2, 3)
        labels = [b.text for b in row]
        assert "▶" not in labels
        assert "◀" in labels

    def test_single_page_has_neither(self):
        row = navigation.pagination_row("candidates", "list", 0, 1)
        labels = [b.text for b in row]
        assert labels == ["1/1"]

    def test_extra_args_travel_with_the_page_number(self):
        row = navigation.pagination_row("clips", "list", 1, 3, "job-42")
        prev_button = row[0]
        assert prev_button.callback_data == "clips:list:0:job-42"

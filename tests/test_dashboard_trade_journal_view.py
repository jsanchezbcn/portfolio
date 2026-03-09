"""tests/test_dashboard_trade_journal_view.py — unit tests for dashboard journal helpers."""
from __future__ import annotations

from dashboard.components.trade_journal_view import _build_display_rows, _filter_rows


def test_build_display_rows_formats_note_fields():
    rows = [
        {
            "created_at": "2026-03-06T12:34:56+00:00",
            "title": "Gamma hedge",
            "tags": ["hedge", "gamma"],
            "body": "Reduce short gamma before payrolls.",
        }
    ]

    display = _build_display_rows(rows)

    assert display == [
        {
            "Created": "2026-03-06 12:34:56",
            "Title": "Gamma hedge",
            "Tags": "hedge, gamma",
            "Body": "Reduce short gamma before payrolls.",
        }
    ]


def test_filter_rows_applies_date_bounds():
    rows = [
        {"created_at": "2026-03-01T00:00:00+00:00", "title": "old"},
        {"created_at": "2026-03-06T00:00:00+00:00", "title": "new"},
    ]

    filtered = _filter_rows(rows, start_dt="2026-03-05T00:00:00+00:00", end_dt=None)

    assert [row["title"] for row in filtered] == ["new"]

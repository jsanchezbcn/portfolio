"""desktop/tests/test_journal_tab.py — UI tests for the Postgres-backed journal notes pane."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from desktop.ui.journal_tab import _StrategyJournalPane


class _FakeJournalStore:
    def __init__(self):
        self.list_journal_notes = AsyncMock(return_value=[
            {
                "created_at": "2026-03-06T12:00:00+00:00",
                "title": "Gamma hedge note",
                "tags": ["hedge", "gamma"],
                "body": "Reduce short gamma into CPI print.",
            }
        ])
        self.create_journal_note = AsyncMock(return_value="note-1")


@pytest.mark.asyncio
async def test_strategy_journal_refresh_renders_notes(qtbot):
    store = _FakeJournalStore()
    pane = _StrategyJournalPane(engine=None, journal_store=store)
    qtbot.addWidget(pane)

    await pane._async_refresh()

    assert pane._table.rowCount() == 1
    title_item = pane._table.item(0, 1)
    tags_item = pane._table.item(0, 2)
    assert title_item is not None
    assert tags_item is not None
    assert title_item.text() == "Gamma hedge note"
    assert "hedge" in tags_item.text()
    store.list_journal_notes.assert_awaited_once()


@pytest.mark.asyncio
async def test_strategy_journal_save_note_clears_editor_and_refreshes(qtbot):
    store = _FakeJournalStore()
    pane = _StrategyJournalPane(engine=None, journal_store=store)
    qtbot.addWidget(pane)

    pane._txt_title.setText("Post-close review")
    pane._txt_tags.setText("review, closing")
    pane._txt_body.setPlainText("Document the adjustment and follow-up plan.")

    await pane._async_save_note()

    store.create_journal_note.assert_awaited_once_with(
        account_id=None,
        title="Post-close review",
        body="Document the adjustment and follow-up plan.",
        tags=["review", "closing"],
    )
    assert pane._txt_title.text() == ""
    assert pane._txt_tags.text() == ""
    assert pane._txt_body.toPlainText() == ""
    assert pane._table.rowCount() == 1


@pytest.mark.asyncio
async def test_strategy_journal_requires_title(qtbot):
    store = _FakeJournalStore()
    pane = _StrategyJournalPane(engine=None, journal_store=store)
    qtbot.addWidget(pane)

    pane._txt_body.setPlainText("Body without title")
    await pane._async_save_note()

    assert "Title is required" in pane._lbl_status.text()
    store.create_journal_note.assert_not_awaited()

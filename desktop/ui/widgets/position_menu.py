from __future__ import annotations

from PySide6.QtWidgets import QMenu, QTableView, QWidget


class PositionContextMenu(QMenu):
    """Context menu for portfolio rows and grouped trades."""

    def __init__(self, parent: QWidget | None, payload: dict):
        super().__init__(parent)
        self._payload = payload
        self._actions_by_name = {}
        for action_name in self.action_names_for_payload(payload):
            self._actions_by_name[action_name] = self.addAction(action_name)

    @staticmethod
    def action_names_for_payload(payload: dict) -> list[str]:
        legs = list(payload.get("legs") or [])
        has_option = any(str(getattr(leg, "sec_type", "")).upper() in {"OPT", "FOP"} for leg in legs)
        names = ["Buy", "Sell"]
        if has_option:
            names.append("Roll")
        return names

    def exec_for_table(self, table: QTableView, point) -> str | None:
        selected = self.exec(table.viewport().mapToGlobal(point))
        if selected is None:
            return None
        return selected.text().upper()
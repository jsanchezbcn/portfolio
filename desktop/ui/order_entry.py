"""desktop/ui/order_entry.py — Multi-leg order entry panel.

Layout (top → bottom):
  Add Leg form  (symbol | sectype | exch | qty | action | strike | right | expiry)  [+ Add Leg]
  Staged Legs table  (#, Sym, SecType, Action, Qty, Strike, Right, Expiry, Bid/Ask)
  [Refresh Prices]  [Remove]  [Clear All]
  Rationale text
  Bid $X.XX ──[slider]── Ask $Y.YY   Mid $Z.ZZ
  Limit: [spinbox]  Step: [combo]  Type: [LIMIT|MARKET]
  [WhatIf]  [SUBMIT]
  result label

Pre-fill via prefill_from_chain() or prefill_from_legs() or add_chain_leg().
"""
from __future__ import annotations

import asyncio
from datetime import date
from math import gcd
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QPushButton, QLabel, QLineEdit, QComboBox, QSpinBox,
    QDoubleSpinBox, QTextEdit, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QSlider,
)
from PySide6.QtCore import Qt, Signal, Slot, QTimer

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine

# Minimum price ticks by instrument type
_TICK = {"FOP": 0.05, "OPT": 0.01, "STK": 0.01, "FUT": 0.25}
_FUTURES_TICKS = {"ES": 0.25, "MES": 0.25, "NQ": 0.25, "MNQ": 0.25, "RTY": 0.10, "YM": 1.0}
_DEFAULT_TICK = 0.05


def _tick_for_legs(legs: list[dict]) -> float:
    ticks = []
    for lg in legs:
        sec = str(lg.get("sec_type") or "OPT").upper()
        sym = str(lg.get("symbol") or "").upper()
        ticks.append(_FUTURES_TICKS.get(sym, 0.25) if sec == "FUT" else _TICK.get(sec, _DEFAULT_TICK))
    return min(ticks) if ticks else _DEFAULT_TICK


def _net_prices(legs: list[dict], bid_ask: list[dict]) -> tuple:
    """Net (lo, hi, mid) for a combo using IB price convention (BUY=you pay, SELL=you receive)."""
    def _ratios(src_legs: list[dict]) -> list[int]:
        if not src_legs:
            return []
        qtys: list[int] = []
        for leg in src_legs:
            try:
                qtys.append(max(1, int(float(leg.get("qty", 1)))))
            except Exception:
                qtys.append(1)
        base = qtys[0]
        for q in qtys[1:]:
            base = gcd(base, q)
        base = max(1, base)
        return [max(1, q // base) for q in qtys]

    combo_lo = combo_hi = 0.0
    any_price = False
    for leg, ba, ratio in zip(legs, bid_ask, _ratios(legs)):
        sign = 1.0 if str(leg.get("action", "BUY")).upper() == "BUY" else -1.0
        bid, ask = ba.get("bid"), ba.get("ask")
        if bid is None and ask is None:
            continue
        any_price = True
        b_raw = bid if bid is not None else ask
        a_raw = ask if ask is not None else bid
        if b_raw is None or a_raw is None:
            continue
        b = float(b_raw)
        a = float(a_raw)
        combo_lo += sign * float(ratio) * min(b, a)
        combo_hi += sign * float(ratio) * max(b, a)
    if not any_price:
        return None, None, None
    lo, hi = min(combo_lo, combo_hi), max(combo_lo, combo_hi)
    return round(lo, 3), round(hi, 3), round((lo + hi) / 2.0, 3)


class OrderEntryPanel(QWidget):
    """Multi-leg order ticket with real-time bid/ask display and price slider."""

    order_submitted = Signal(dict)

    def __init__(self, engine: "IBEngine", parent=None):
        super().__init__(parent)
        self._engine = engine
        self._staged_legs: list[dict] = []
        self._bid_ask: list[dict] = []
        self._current_tick: float = _DEFAULT_TICK
        self._slider_busy: bool = False
        self._refresh_running: bool = False  # guard: no concurrent auto-refreshes
        # Auto-refresh bid/ask every 5 seconds while legs are staged
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(5_000)
        self._auto_refresh_timer.timeout.connect(self._on_auto_refresh)
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(5)

        # Add-Leg form
        form_box = QGroupBox("Add Leg")
        form_box.setMaximumHeight(155)
        h = QHBoxLayout(form_box)
        col1 = QFormLayout(); col1.setSpacing(3)
        self._txt_symbol = QLineEdit(); self._txt_symbol.setPlaceholderText("ES, SPY…"); self._txt_symbol.setMaximumWidth(80)
        col1.addRow("Symbol:", self._txt_symbol)
        self._cmb_sec_type = QComboBox(); self._cmb_sec_type.addItems(["FOP","OPT","STK","FUT"])
        col1.addRow("SecType:", self._cmb_sec_type)
        self._cmb_exchange = QComboBox(); self._cmb_exchange.addItems(["CME","SMART","CBOE","GLOBEX"])
        col1.addRow("Exchange:", self._cmb_exchange)
        h.addLayout(col1)

        col2 = QFormLayout(); col2.setSpacing(3)
        self._spn_qty = QSpinBox(); self._spn_qty.setRange(1, 999); self._spn_qty.setValue(1)
        col2.addRow("Qty:", self._spn_qty)
        self._cmb_action = QComboBox(); self._cmb_action.addItems(["BUY","SELL"])
        col2.addRow("Action:", self._cmb_action)
        h.addLayout(col2)

        col3 = QFormLayout(); col3.setSpacing(3)
        self._spn_strike = QDoubleSpinBox(); self._spn_strike.setRange(0, 99999); self._spn_strike.setDecimals(0); self._spn_strike.setSingleStep(5)
        col3.addRow("Strike:", self._spn_strike)
        self._cmb_right = QComboBox(); self._cmb_right.addItems(["C","P"])
        col3.addRow("Right:", self._cmb_right)
        self._txt_expiry = QLineEdit(); self._txt_expiry.setPlaceholderText("YYYYMMDD"); self._txt_expiry.setMaximumWidth(90)
        col3.addRow("Expiry:", self._txt_expiry)
        h.addLayout(col3)

        btn_col = QVBoxLayout(); btn_col.addStretch()
        self._btn_add_leg = QPushButton("＋ Add Leg")
        self._btn_add_leg.setFixedWidth(90)
        self._btn_add_leg.setStyleSheet("background:#27ae60;color:white;padding:5px;")
        btn_col.addWidget(self._btn_add_leg); h.addLayout(btn_col)
        root.addWidget(form_box)

        # Staged-legs table
        self._tbl_legs = QTableWidget(0, 9)
        self._tbl_legs.setHorizontalHeaderLabels(["#","Symbol","SecType","Action","Qty","Strike","Right","Expiry","Bid / Ask"])
        self._tbl_legs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._tbl_legs.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self._tbl_legs.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tbl_legs.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._tbl_legs.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._tbl_legs.setMaximumHeight(155)
        self._tbl_legs.setVisible(False)
        root.addWidget(self._tbl_legs)

        tbl_row = QHBoxLayout()
        self._btn_refresh_ba = QPushButton("🔄 Refresh Bid/Ask"); self._btn_refresh_ba.setVisible(False); tbl_row.addWidget(self._btn_refresh_ba)
        self._btn_remove_leg = QPushButton("🗑 Remove"); self._btn_remove_leg.setVisible(False); tbl_row.addWidget(self._btn_remove_leg)
        self._btn_clear = QPushButton("🧹 Clear All"); self._btn_clear.setVisible(False); tbl_row.addWidget(self._btn_clear)
        tbl_row.addStretch(); root.addLayout(tbl_row)

        # Rationale
        self._txt_rationale = QTextEdit(); self._txt_rationale.setPlaceholderText("Why this trade…"); self._txt_rationale.setMaximumHeight(52)
        root.addWidget(self._txt_rationale)

        # Price controls
        price_box = QGroupBox("Price"); pv = QVBoxLayout(price_box)
        ba_row = QHBoxLayout()
        ba_row.addWidget(QLabel("Bid:"))
        self._lbl_bid = QLabel("—"); self._lbl_bid.setStyleSheet("color:#e74c3c;font-weight:bold;min-width:60px;"); ba_row.addWidget(self._lbl_bid)
        ba_row.addStretch()
        ba_row.addWidget(QLabel("Mid:"))
        self._lbl_mid = QLabel("—"); self._lbl_mid.setStyleSheet("color:#f39c12;font-weight:bold;min-width:60px;"); ba_row.addWidget(self._lbl_mid)
        ba_row.addStretch()
        ba_row.addWidget(QLabel("Ask:"))
        self._lbl_ask = QLabel("—"); self._lbl_ask.setStyleSheet("color:#27ae60;font-weight:bold;min-width:60px;"); ba_row.addWidget(self._lbl_ask)
        pv.addLayout(ba_row)
        self._slider = QSlider(Qt.Orientation.Horizontal); self._slider.setRange(0, 1000); self._slider.setValue(500); self._slider.setEnabled(False); pv.addWidget(self._slider)
        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Limit:"))
        self._spn_limit = QDoubleSpinBox(); self._spn_limit.setRange(-99999, 99999); self._spn_limit.setDecimals(2); self._spn_limit.setSingleStep(0.05)
        ctrl_row.addWidget(self._spn_limit)
        ctrl_row.addWidget(QLabel("Step:"))
        self._cmb_step = QComboBox(); self._cmb_step.addItems(["0.01","0.05","0.10","0.25","1.00"]); self._cmb_step.setCurrentText("0.05"); self._cmb_step.setMaximumWidth(65)
        ctrl_row.addWidget(self._cmb_step)
        ctrl_row.addWidget(QLabel("Type:"))
        self._cmb_order_type = QComboBox(); self._cmb_order_type.addItems(["LIMIT","MARKET","PRICE_DISCOVERY"]); ctrl_row.addWidget(self._cmb_order_type)
        ctrl_row.addStretch(); pv.addLayout(ctrl_row); root.addWidget(price_box)

        # Buttons
        btn_row = QHBoxLayout()
        self._btn_whatif = QPushButton("🔍 WhatIf Simulate"); self._btn_whatif.setStyleSheet("background:#3498db;color:white;padding:8px;"); self._btn_whatif.setEnabled(False); btn_row.addWidget(self._btn_whatif)
        self._btn_submit = QPushButton("🚨 SUBMIT LIVE ORDER"); self._btn_submit.setStyleSheet("background:#e74c3c;color:white;padding:8px;font-weight:bold;"); self._btn_submit.setEnabled(False); btn_row.addWidget(self._btn_submit)
        root.addLayout(btn_row)

        self._lbl_result = QLabel(""); self._lbl_result.setWordWrap(True); self._lbl_result.setStyleSheet("padding:5px;"); root.addWidget(self._lbl_result)
        root.addStretch()

    def _connect_signals(self) -> None:
        self._btn_add_leg.clicked.connect(self._on_add_leg)
        self._btn_remove_leg.clicked.connect(self._on_remove_leg)
        self._btn_clear.clicked.connect(self._on_clear_legs)
        self._btn_refresh_ba.clicked.connect(self._on_refresh_prices)
        self._btn_whatif.clicked.connect(self._on_whatif)
        self._btn_submit.clicked.connect(self._on_submit)
        self._cmb_order_type.currentTextChanged.connect(self._on_order_type_changed)
        self._cmb_sec_type.currentTextChanged.connect(self._on_sec_type_changed)
        self._slider.valueChanged.connect(self._on_slider_changed)
        self._spn_limit.valueChanged.connect(self._on_spinbox_changed)
        self._cmb_step.currentTextChanged.connect(self._on_step_changed)
        self._engine.connected.connect(self._on_connected)
        self._engine.disconnected.connect(self._on_disconnected)
        self._engine.connected.connect(self._auto_refresh_timer.start)
        self._engine.disconnected.connect(self._auto_refresh_timer.stop)

    # public pre-fill

    def prefill_from_chain(self, chain_row) -> None:
        """Stage a single leg from a chain-tab double-click."""
        self._staged_legs.clear(); self._bid_ask.clear()
        sec_type = "FOP" if chain_row.underlying in ("ES","MES","NQ","MNQ") else "OPT"
        exchange = "CME" if sec_type == "FOP" else "SMART"
        self._stage_leg(
            symbol=chain_row.underlying, sec_type=sec_type, exchange=exchange,
            action=self._cmb_action.currentText(), qty=1,
            strike=chain_row.strike, right=chain_row.right,
            expiry=(chain_row.expiry or "")[:8], conid=chain_row.conid or 0,
            bid=chain_row.bid, ask=chain_row.ask,
        )

    def add_chain_leg(self, chain_row, action: str) -> None:
        """Append one leg from the chain leg-cart (BUY or SELL)."""
        sec_type = "FOP" if chain_row.underlying in ("ES","MES","NQ","MNQ") else "OPT"
        exchange = "CME" if sec_type == "FOP" else "SMART"
        self._stage_leg(
            symbol=chain_row.underlying, sec_type=sec_type, exchange=exchange,
            action=action, qty=1,
            strike=chain_row.strike, right=chain_row.right,
            expiry=(chain_row.expiry or "")[:8], conid=chain_row.conid or 0,
            bid=chain_row.bid, ask=chain_row.ask,
        )

    def prefill_from_legs(self, legs: list[dict], *, rationale: str = "", source: str = "AI") -> None:
        """Stage one or more legs (e.g. from AI suggestion)."""
        if not legs:
            return
        self._staged_legs.clear(); self._bid_ask.clear()
        for leg in legs:
            sym = str(leg.get("symbol") or "").upper()
            sec_type = str(leg.get("sec_type") or "OPT").upper()
            if sec_type not in {"FOP","OPT","STK","FUT"}:
                sec_type = "FOP" if sym in {"ES","MES","NQ","MNQ"} else "OPT"
            exchange = str(leg.get("exchange") or ("CME" if sec_type in {"FOP","FUT"} else "SMART")).upper()
            try: qty = max(1, int(float(leg.get("qty") or leg.get("quantity") or 1)))
            except Exception: qty = 1
            try: strike = float(leg.get("strike") or 0)
            except Exception: strike = 0.0
            right = str(leg.get("right") or "C").upper()
            right = "P" if right.startswith("P") else "C"
            expiry_raw = leg.get("expiry")
            expiry = expiry_raw.strftime("%Y%m%d") if isinstance(expiry_raw, date) else str(expiry_raw or "").strip()
            self._stage_leg(
                symbol=sym, sec_type=sec_type, exchange=exchange,
                action=str(leg.get("action") or "BUY").upper(),
                qty=qty, strike=strike, right=right, expiry=expiry,
                conid=int(leg.get("conid") or 0),
            )
        if rationale:
            self._txt_rationale.setPlainText(rationale)
        self._lbl_result.setText(f"✅ {len(legs)} leg(s) staged from {source}. Click 🔄 Refresh Bid/Ask then submit.")
        self._lbl_result.setStyleSheet("background:#d4edda;color:#155724;padding:5px;")
        asyncio.get_event_loop().create_task(self._async_refresh_prices())

    # internal leg management

    def _stage_leg(self, *, symbol, sec_type, exchange, action, qty, strike, right, expiry, conid=0, bid=None, ask=None) -> None:
        leg = dict(symbol=symbol, sec_type=sec_type, exchange=exchange, action=action, qty=qty, strike=strike, right=right, expiry=expiry, conid=conid)
        self._staged_legs.append(leg)
        mid = round((bid + ask) / 2.0, 2) if (bid is not None and ask is not None) else (bid or ask)
        self._bid_ask.append({"bid": bid, "ask": ask, "mid": mid})
        self._render_table(); self._update_price_controls()

    @Slot()
    def _on_add_leg(self) -> None:
        sym = self._txt_symbol.text().strip().upper()
        if not sym: return
        right = self._cmb_right.currentText().upper()
        self._stage_leg(
            symbol=sym, sec_type=self._cmb_sec_type.currentText(), exchange=self._cmb_exchange.currentText(),
            action=self._cmb_action.currentText(), qty=self._spn_qty.value(),
            strike=self._spn_strike.value(), right="P" if right.startswith("P") else "C",
            expiry=self._txt_expiry.text().strip(),
        )

    @Slot()
    def _on_remove_leg(self) -> None:
        row = self._tbl_legs.currentRow()
        if 0 <= row < len(self._staged_legs):
            self._staged_legs.pop(row); self._bid_ask.pop(row)
            self._render_table(); self._update_price_controls()

    @Slot()
    def _on_clear_legs(self) -> None:
        self._staged_legs.clear(); self._bid_ask.clear()
        self._render_table(); self._update_price_controls(); self._lbl_result.setText("")

    @Slot()
    def _on_refresh_prices(self) -> None:
        asyncio.get_event_loop().create_task(self._async_refresh_prices())

    @Slot()
    def _on_auto_refresh(self) -> None:
        """Called by 5-second timer — refresh bid/ask silently if legs are staged."""
        if not self._staged_legs or self._refresh_running:
            return
        asyncio.get_event_loop().create_task(self._async_refresh_prices(silent=True))

    async def _async_refresh_prices(self, *, silent: bool = False) -> None:
        if not self._staged_legs or self._refresh_running:
            return
        self._refresh_running = True
        self._btn_refresh_ba.setEnabled(False)
        if not silent:
            self._lbl_result.setText("⏳ Fetching bid/ask…")
        try:
            self._bid_ask = await self._engine.get_bid_ask_for_legs(self._staged_legs)
        except Exception as exc:
            if not silent:
                self._lbl_result.setText(f"❌ Price fetch failed: {exc}")
        finally:
            self._btn_refresh_ba.setEnabled(True)
            self._refresh_running = False
        self._render_table()
        self._update_price_controls()
        if not silent and all(ba.get("bid") is None and ba.get("ask") is None for ba in self._bid_ask):
            self._lbl_result.setText("⚠ No bid/ask received — market may be closed")

    def _render_table(self) -> None:
        has = bool(self._staged_legs)
        self._tbl_legs.setVisible(has); self._btn_refresh_ba.setVisible(has)
        self._btn_remove_leg.setVisible(has); self._btn_clear.setVisible(has)
        self._tbl_legs.setRowCount(len(self._staged_legs))
        for i, lg in enumerate(self._staged_legs):
            ba = self._bid_ask[i] if i < len(self._bid_ask) else {}
            bid, ask = ba.get("bid"), ba.get("ask")
            ba_text = (f"{bid:.2f} / {ask:.2f}" if bid is not None and ask is not None
                       else f"{bid:.2f} / —" if bid is not None else f"— / {ask:.2f}" if ask is not None else "—")
            cells = [str(i+1), lg["symbol"], lg["sec_type"], lg["action"], str(lg["qty"]),
                     f"{lg['strike']:.0f}" if lg.get("strike") else "—",
                     {"C":"Call","P":"Put"}.get(lg.get("right",""), lg.get("right","—")),
                     lg.get("expiry") or "—", ba_text]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text); item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 3: item.setForeground(Qt.GlobalColor.green if lg["action"]=="BUY" else Qt.GlobalColor.red)
                if col == 8 and bid is not None: item.setForeground(Qt.GlobalColor.cyan)
                self._tbl_legs.setItem(i, col, item)

    def _update_price_controls(self) -> None:
        if not self._staged_legs:
            for lbl in (self._lbl_bid, self._lbl_mid, self._lbl_ask): lbl.setText("—")
            self._slider.setEnabled(False); return
        self._current_tick = _tick_for_legs(self._staged_legs)
        try: self._cmb_step.setCurrentText(f"{self._current_tick:.2f}")
        except Exception: pass
        self._spn_limit.setSingleStep(self._current_tick)
        lo, hi, mid = _net_prices(self._staged_legs, self._bid_ask)
        if lo is None:
            for lbl in (self._lbl_bid, self._lbl_mid, self._lbl_ask): lbl.setText("—")
            self._slider.setEnabled(False); return
        self._lbl_bid.setText(f"${lo:+.2f}"); self._lbl_mid.setText(f"${mid:+.2f}"); self._lbl_ask.setText(f"${hi:+.2f}")
        tick = self._current_tick
        lo_t, hi_t, mid_t = round(lo/tick), round(hi/tick), round(mid/tick)
        self._slider_busy = True
        if lo_t < hi_t:
            self._slider.setRange(lo_t, hi_t); self._slider.setValue(mid_t); self._slider.setEnabled(True)
        else:
            self._slider.setRange(lo_t - 20, lo_t + 20); self._slider.setValue(lo_t); self._slider.setEnabled(True)
        self._slider_busy = False
        self._spn_limit.setValue(round(self._slider.value() * tick, 2))

    @Slot()
    def _on_slider_changed(self) -> None:
        if self._slider_busy: return
        price = round(self._slider.value() * self._current_tick, 2)
        self._slider_busy = True; self._spn_limit.setValue(price); self._slider_busy = False

    @Slot()
    def _on_spinbox_changed(self) -> None:
        if self._slider_busy or self._current_tick <= 0: return
        t = round(self._spn_limit.value() / self._current_tick)
        self._slider_busy = True; self._slider.setValue(int(t)); self._slider_busy = False

    @Slot(str)
    def _on_step_changed(self, text: str) -> None:
        try:
            tick = float(text)
            if tick > 0: self._current_tick = tick; self._spn_limit.setSingleStep(tick)
        except ValueError: pass

    @Slot(str)
    def _on_order_type_changed(self, order_type: str) -> None:
        is_limit = order_type in {"LIMIT", "PRICE_DISCOVERY"}
        self._spn_limit.setEnabled(is_limit); self._slider.setEnabled(is_limit and bool(self._staged_legs))

    @Slot(str)
    def _on_sec_type_changed(self, sec_type: str) -> None:
        if sec_type == "FOP": self._cmb_exchange.setCurrentText("CME")
        elif sec_type in ("OPT","STK"): self._cmb_exchange.setCurrentText("SMART")

    @Slot()
    def _on_connected(self) -> None: self._btn_whatif.setEnabled(True); self._btn_submit.setEnabled(True)

    @Slot()
    def _on_disconnected(self) -> None: self._btn_whatif.setEnabled(False); self._btn_submit.setEnabled(False)

    @Slot()
    def _on_whatif(self) -> None:
        legs = self._staged_legs or [self._build_leg()]
        if not legs: return
        self._lbl_result.setText("⏳ Running WhatIf simulation…"); self._btn_whatif.setEnabled(False)
        asyncio.get_event_loop().create_task(self._async_whatif(legs))

    async def _async_whatif(self, legs: list[dict]) -> None:
        try:
            selected_order_type = self._cmb_order_type.currentText()
            engine_order_type = "LIMIT" if selected_order_type == "PRICE_DISCOVERY" else selected_order_type
            result = await self._engine.whatif_order(
                legs,
                order_type=engine_order_type,
                limit_price=self._spn_limit.value() if selected_order_type in {"LIMIT", "PRICE_DISCOVERY"} else None,
            )
            if result.get("error"):
                self._lbl_result.setText(f"❌ {result['error']}"); self._lbl_result.setStyleSheet("background:#f8d7da;color:#721c24;padding:6px;")
            else:
                init, maint = result.get("init_margin"), result.get("maint_margin")
                self._lbl_result.setText(
                    f"✅ WhatIf OK — Init Margin: ${init:,.0f}  |  Maint: ${maint:,.0f}"
                    if (init is not None and maint is not None) else f"✅ WhatIf: {result}"
                )
                self._lbl_result.setStyleSheet("background:#d4edda;color:#155724;padding:6px;")
        except Exception as exc:
            self._lbl_result.setText(f"❌ Error: {exc}"); self._lbl_result.setStyleSheet("background:#f8d7da;color:#721c24;padding:6px;")
        finally:
            self._btn_whatif.setEnabled(True)

    @Slot()
    def _on_submit(self) -> None:
        """Show a synchronous confirmation dialog first (avoids qasync re-entrancy
        crash), then launch the async WhatIf + submit chain.
        """
        legs = self._staged_legs or [self._build_leg()]
        if not legs:
            return

        # Build summary for dialog
        summary_lines = [
            f"  {lg['action']} {lg['qty']} {lg['symbol']} "
            f"{lg.get('strike', '')}{lg.get('right', '')} exp:{lg.get('expiry', '')}"
            for lg in legs
        ]
        summary = "\n".join(summary_lines)

        reply = QMessageBox.warning(
            self,
            "⚠ Confirm Live Order",
            f"LIVE order to IBKR:\n\n{summary}\n\n"
            f"Limit: {self._spn_limit.value():.2f}  "
            f"Type: {self._cmb_order_type.currentText()}\n\n"
            "A WhatIf risk check will run automatically before submitting.\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._lbl_result.setText("⏳ Running WhatIf risk check…")
        self._btn_submit.setEnabled(False)
        asyncio.get_event_loop().create_task(self._async_submit(legs))

    async def _async_submit(self, legs: list[dict]) -> None:
        """Run WhatIf, show margin result in label, then submit."""
        selected_order_type = self._cmb_order_type.currentText()
        engine_order_type = "LIMIT" if selected_order_type == "PRICE_DISCOVERY" else selected_order_type

        # Step 1: WhatIf for margin visibility
        try:
            wi = await self._engine.whatif_order(
                legs,
                order_type=engine_order_type,
                limit_price=self._spn_limit.value() if selected_order_type in {"LIMIT", "PRICE_DISCOVERY"} else None,
            )
            if wi.get("error"):
                self._lbl_result.setText(f"⚠ WhatIf: {wi['error']} — submitting anyway…")
                self._lbl_result.setStyleSheet("background:#fff3cd;color:#856404;padding:6px;")
            elif wi.get("init_margin") is not None:
                init, maint = wi["init_margin"], wi.get("maint_margin", 0)
                self._lbl_result.setText(
                    f"⏳ Margin impact  Init: ${init:+,.0f}  Maint: ${maint:+,.0f} — submitting…"
                )
                self._lbl_result.setStyleSheet("background:#d1ecf1;color:#0c5460;padding:6px;")
        except Exception as exc:
            self._lbl_result.setText(f"⚠ WhatIf unavailable ({exc}) — submitting…")
            self._lbl_result.setStyleSheet("background:#fff3cd;color:#856404;padding:6px;")

        if selected_order_type == "PRICE_DISCOVERY":
            await self._async_submit_price_discovery(legs)
            self._btn_submit.setEnabled(True)
            return

        # Step 2: Submit
        try:
            result = await self._engine.place_order(
                legs,
                order_type=selected_order_type,
                limit_price=self._spn_limit.value() if selected_order_type in {"LIMIT", "PRICE_DISCOVERY"} else None,
                source="manual",
                rationale=self._txt_rationale.toPlainText(),
            )
            if result.get("error"):
                self._lbl_result.setText(f"❌ {result['error']}"); self._lbl_result.setStyleSheet("background:#f8d7da;color:#721c24;padding:6px;")
            else:
                oid = str(result.get("order_id","?"))[:12]
                self._lbl_result.setText(f"✅ {result.get('status','submitted')} — ID: {oid}  Avg: ${result.get('avg_price',0):.2f}")
                self._lbl_result.setStyleSheet("background:#d4edda;color:#155724;padding:6px;")
                self.order_submitted.emit(result)
                self._staged_legs.clear(); self._bid_ask.clear(); self._render_table()
        except Exception as exc:
            self._lbl_result.setText(f"❌ Error: {exc}"); self._lbl_result.setStyleSheet("background:#f8d7da;color:#721c24;padding:6px;")
        finally:
            self._btn_submit.setEnabled(True)

    async def _async_submit_price_discovery(self, legs: list[dict]) -> None:
        """Submit as stepped LIMIT retries until execution or max attempts."""
        base_price = float(self._spn_limit.value())
        tick = max(0.01, float(self._current_tick or 0.05))
        max_attempts = 8

        first_action = str((legs[0] if legs else {}).get("action") or "BUY").upper()
        direction = 1.0 if first_action == "BUY" else -1.0

        last_result: dict | None = None
        for attempt in range(max_attempts):
            price = round(base_price + direction * tick * attempt, 2)
            self._lbl_result.setText(
                f"⏳ Price discovery {attempt + 1}/{max_attempts} — trying limit {price:.2f}"
            )
            self._lbl_result.setStyleSheet("background:#d1ecf1;color:#0c5460;padding:6px;")

            result = await self._engine.place_order(
                legs,
                order_type="LIMIT",
                limit_price=price,
                source="price_discovery",
                rationale=self._txt_rationale.toPlainText(),
            )
            last_result = result
            status = str(result.get("status") or "").lower()
            if "filled" in status or "submitted" in status or "pending" in status:
                oid = str(result.get("order_id", "?"))[:12]
                self._lbl_result.setText(
                    f"✅ Price discovery {result.get('status', 'submitted')} — ID: {oid}  Avg: ${result.get('avg_price', 0):.2f}"
                )
                self._lbl_result.setStyleSheet("background:#d4edda;color:#155724;padding:6px;")
                self.order_submitted.emit(result)
                self._staged_legs.clear(); self._bid_ask.clear(); self._render_table()
                return

        if last_result and last_result.get("error"):
            self._lbl_result.setText(f"❌ Price discovery failed: {last_result['error']}")
            self._lbl_result.setStyleSheet("background:#f8d7da;color:#721c24;padding:6px;")
        else:
            self._lbl_result.setText("❌ Price discovery exhausted attempts without execution")
            self._lbl_result.setStyleSheet("background:#f8d7da;color:#721c24;padding:6px;")

    def _build_leg(self) -> dict:
        right = self._cmb_right.currentText().upper()
        return {"symbol": self._txt_symbol.text().strip().upper(), "sec_type": self._cmb_sec_type.currentText(),
                "exchange": self._cmb_exchange.currentText(), "action": self._cmb_action.currentText(),
                "qty": self._spn_qty.value(), "strike": self._spn_strike.value(),
                "right": "P" if right.startswith("P") else "C", "expiry": self._txt_expiry.text().strip()}

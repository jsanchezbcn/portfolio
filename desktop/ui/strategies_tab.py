"""desktop/ui/strategies_tab.py — Agent-managed strategy monitor tab.

Initial scope:
  1. Strategy controls (strategy, underlying, stop-loss, take-profit)
  2. Taleb gamma warning state for 0-7 DTE options exposure
  3. Sebastian theta/vega ratio state with target-band evaluation
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Iterable

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QComboBox, QLineEdit, QDoubleSpinBox, QPushButton,
)

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine, PositionRow


@dataclass
class GammaWarningState:
    gamma_0_7d_abs: float
    threshold: float
    breached: bool
    message: str


@dataclass
class ThetaVegaState:
    theta_total: float
    vega_total: float
    ratio: float
    lower_band: float
    upper_band: float
    zone: str
    message: str


def _parse_expiry(expiry: str | None) -> date | None:
    if not expiry:
        return None
    value = str(expiry).strip()
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _is_option_like(row: "PositionRow") -> bool:
    return str(getattr(row, "sec_type", "")).upper() in {"OPT", "FOP"}


def calculate_taleb_gamma_warning(
    rows: Iterable["PositionRow"],
    *,
    gamma_threshold: float,
    as_of: date | None = None,
) -> GammaWarningState:
    today = as_of or date.today()
    gamma_abs_sum = 0.0

    for row in rows:
        if not _is_option_like(row):
            continue
        expiry_date = _parse_expiry(getattr(row, "expiry", None))
        if expiry_date is None:
            continue
        dte = (expiry_date - today).days
        if not (0 <= dte <= 7):
            continue
        gamma = float(getattr(row, "gamma", 0.0) or 0.0)
        gamma_abs_sum += abs(gamma)

    breached = gamma_abs_sum > float(gamma_threshold)
    if breached:
        message = (
            f"Taleb warning: high near-expiry gamma risk "
            f"(|Γ|0-7D={gamma_abs_sum:.2f} > {gamma_threshold:.2f})."
        )
    else:
        message = (
            f"Taleb state normal: near-expiry gamma within limit "
            f"(|Γ|0-7D={gamma_abs_sum:.2f} ≤ {gamma_threshold:.2f})."
        )
    return GammaWarningState(
        gamma_0_7d_abs=gamma_abs_sum,
        threshold=float(gamma_threshold),
        breached=breached,
        message=message,
    )


def calculate_theta_vega_state(
    rows: Iterable["PositionRow"],
    *,
    lower_band: float = -0.35,
    upper_band: float = -0.10,
) -> ThetaVegaState:
    theta_total = 0.0
    vega_total = 0.0

    for row in rows:
        if not _is_option_like(row):
            continue
        theta_total += float(getattr(row, "theta", 0.0) or 0.0)
        vega_total += float(getattr(row, "vega", 0.0) or 0.0)

    ratio = (theta_total / vega_total) if vega_total != 0 else 0.0
    inside = float(lower_band) <= ratio <= float(upper_band)
    zone = "inside" if inside else "outside"
    if inside:
        message = (
            f"Sebastian state inside target band "
            f"(Θ/V={ratio:+.3f}, target {lower_band:+.2f}..{upper_band:+.2f})."
        )
    else:
        message = (
            f"Sebastian warning: Θ/V outside target band "
            f"(Θ/V={ratio:+.3f}, target {lower_band:+.2f}..{upper_band:+.2f})."
        )
    return ThetaVegaState(
        theta_total=theta_total,
        vega_total=vega_total,
        ratio=ratio,
        lower_band=float(lower_band),
        upper_band=float(upper_band),
        zone=zone,
        message=message,
    )


class StrategiesTab(QWidget):
    """Initial strategies workspace with risk overlays for 0DTE workflows."""

    def __init__(self, engine: "IBEngine", parent=None):
        super().__init__(parent)
        self._engine = engine
        self._is_running = False
        self._positions: list[PositionRow] = []
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        cfg_box = QGroupBox("Strategy Configuration")
        cfg_layout = QVBoxLayout(cfg_box)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Strategy:"))
        self._cmb_strategy = QComboBox()
        self._cmb_strategy.addItems([
            "0DTE Short Strangle",
            "0DTE Iron Condor",
        ])
        row1.addWidget(self._cmb_strategy)

        row1.addWidget(QLabel("Underlying:"))
        self._txt_underlying = QLineEdit("ES")
        self._txt_underlying.setMaximumWidth(120)
        row1.addWidget(self._txt_underlying)

        row1.addWidget(QLabel("Stop-Loss %:"))
        self._spn_stop_loss = QDoubleSpinBox()
        self._spn_stop_loss.setRange(0.1, 100.0)
        self._spn_stop_loss.setDecimals(1)
        self._spn_stop_loss.setValue(35.0)
        row1.addWidget(self._spn_stop_loss)

        row1.addWidget(QLabel("Take-Profit %:"))
        self._spn_take_profit = QDoubleSpinBox()
        self._spn_take_profit.setRange(0.1, 100.0)
        self._spn_take_profit.setDecimals(1)
        self._spn_take_profit.setValue(50.0)
        row1.addWidget(self._spn_take_profit)
        row1.addStretch()
        cfg_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Gamma Threshold (|Γ| 0-7D):"))
        self._spn_gamma_threshold = QDoubleSpinBox()
        self._spn_gamma_threshold.setRange(0.01, 5000.0)
        self._spn_gamma_threshold.setDecimals(2)
        self._spn_gamma_threshold.setValue(25.0)
        row2.addWidget(self._spn_gamma_threshold)

        self._btn_start = QPushButton("▶ Start Monitoring")
        self._btn_stop = QPushButton("⏹ Stop")
        self._btn_stop.setEnabled(False)
        self._btn_refresh = QPushButton("🔄 Refresh Strategy View")
        row2.addWidget(self._btn_start)
        row2.addWidget(self._btn_stop)
        row2.addWidget(self._btn_refresh)
        row2.addStretch()
        cfg_layout.addLayout(row2)

        self._lbl_run_state = QLabel("Status: Idle")
        self._lbl_run_state.setStyleSheet("color: #888;")
        cfg_layout.addWidget(self._lbl_run_state)

        layout.addWidget(cfg_box)

        risk_box = QGroupBox("Strategy Risk Summary")
        risk_layout = QVBoxLayout(risk_box)
        self._lbl_gamma_state = QLabel("Taleb gamma warning: waiting for positions")
        self._lbl_theta_vega_state = QLabel("Sebastian theta/vega state: waiting for positions")
        self._lbl_gamma_state.setWordWrap(True)
        self._lbl_theta_vega_state.setWordWrap(True)
        risk_layout.addWidget(self._lbl_gamma_state)
        risk_layout.addWidget(self._lbl_theta_vega_state)
        layout.addWidget(risk_box)

        layout.addStretch()

    def _connect_signals(self) -> None:
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_refresh.clicked.connect(self._on_refresh)
        self._engine.positions_updated.connect(self._on_positions_updated)

    @Slot()
    def _on_start(self) -> None:
        self._is_running = True
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._lbl_run_state.setText(
            f"Status: Monitoring {self._cmb_strategy.currentText()} "
            f"on {self._txt_underlying.text().strip().upper()} "
            f"(SL {self._spn_stop_loss.value():.1f}% / TP {self._spn_take_profit.value():.1f}%)"
        )
        self._update_risk_labels()

    @Slot()
    def _on_stop(self) -> None:
        self._is_running = False
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._lbl_run_state.setText("Status: Idle")

    @Slot()
    def _on_refresh(self) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(self._engine.refresh_positions())

    @Slot(list)
    def _on_positions_updated(self, rows: list["PositionRow"]) -> None:
        self._positions = list(rows)
        self._update_risk_labels()

    def _update_risk_labels(self) -> None:
        gamma_state = calculate_taleb_gamma_warning(
            self._positions,
            gamma_threshold=self._spn_gamma_threshold.value(),
        )
        theta_vega_state = calculate_theta_vega_state(self._positions)

        gamma_color = "#e74c3c" if gamma_state.breached else "#27ae60"
        tv_color = "#27ae60" if theta_vega_state.zone == "inside" else "#e74c3c"

        self._lbl_gamma_state.setText(
            f"<b>Taleb Gamma Warning:</b> "
            f"<span style='color:{gamma_color}'>{gamma_state.message}</span>"
        )
        self._lbl_theta_vega_state.setText(
            f"<b>Sebastian Θ/V State:</b> "
            f"<span style='color:{tv_color}'>{theta_vega_state.message}</span>"
        )

"""desktop/ui/risk_tab.py — Portfolio Risk & Greek Aggregates tab.

Shows:
  1. Risk metrics cards (total delta, gamma, theta, vega, SPX delta…)
  2. Exposure breakdown (gross, net, theta/vega ratio)
  3. Risk limits loaded from risk_matrix.yaml — current value vs effective limit
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QTableView, QHeaderView, QPushButton,
)
from PySide6.QtCore import Qt, Slot

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine, PortfolioRiskSummary

_RISK_MATRIX_PATH = Path(__file__).resolve().parents[2] / "config" / "risk_matrix.yaml"
_DEFAULT_REGIME = "neutral_volatility"


def _load_risk_limits(nlv: float) -> list[tuple[str, float, str]]:
    """Return list of (limit_name, effective_value, direction) from risk_matrix.yaml.

    direction is 'min' (must be >= threshold) or 'max' (must be <= threshold).
    Uses `neutral_volatility` regime as the default.  Falls back gracefully.
    """
    try:
        data = yaml.safe_load(_RISK_MATRIX_PATH.read_text())
        regime = (data.get("regimes") or {}).get(_DEFAULT_REGIME) or {}
        limits = regime.get("limits") or {}
        result = []
        _nlv = float(nlv) if nlv else 0.0

        def pct(key: str) -> float | None:
            v = limits.get(key)
            return float(v) if v is not None else None

        def apply_nlv(pct_key: str, abs_key: str) -> float | None:
            p = pct(pct_key)
            if p is not None and _nlv > 0:
                return p * _nlv
            a = limits.get(abs_key)
            return float(a) if a is not None else None

        theta_lim  = apply_nlv("min_daily_theta_pct_nlv",   "legacy_min_daily_theta")
        vega_lim   = apply_nlv("max_negative_vega_pct_nlv", "legacy_max_negative_vega")
        delta_lim  = apply_nlv("max_spx_delta_pct_nlv",     "legacy_max_beta_delta")
        gamma_lim  = apply_nlv("max_gamma_pct_nlv",         "legacy_max_gamma")

        if theta_lim  is not None: result.append(("Daily Theta (min)",  theta_lim,  "min"))
        if vega_lim   is not None: result.append(("Short Vega (max)",   vega_lim,   "max"))
        if delta_lim  is not None: result.append(("SPX Δ (max |abs|)",  delta_lim,  "absmax"))
        if gamma_lim  is not None: result.append(("Gamma (max)",        gamma_lim,  "max"))
        return result
    except Exception:
        return []


class RiskTab(QWidget):
    """Risk overview tab showing aggregated Greeks and exposure metrics."""

    def __init__(self, engine: IBEngine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._risk_rules_loaded = False
        self._setup_ui()
        self._connect_signals()


    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Risk Metrics Cards ────────────────────────────────────────────
        greeks_box = QGroupBox("Portfolio Greeks")
        greeks_layout = QHBoxLayout(greeks_box)

        self._lbl_spx_delta = self._metric_card("SPX Δ", "—")
        self._lbl_delta = self._metric_card("Total Δ", "—")
        self._lbl_gamma = self._metric_card("Total Γ", "—")
        self._lbl_theta = self._metric_card("Total Θ", "—")
        self._lbl_vega = self._metric_card("Total V", "—")
        self._lbl_tv_ratio = self._metric_card("Θ/V Ratio", "—")

        for w in (self._lbl_spx_delta, self._lbl_delta, self._lbl_gamma,
                  self._lbl_theta, self._lbl_vega, self._lbl_tv_ratio):
            greeks_layout.addWidget(w)

        layout.addWidget(greeks_box)

        # ── Exposure Cards ────────────────────────────────────────────────
        exposure_box = QGroupBox("Exposure")
        exposure_layout = QHBoxLayout(exposure_box)

        self._lbl_positions = self._metric_card("Positions", "0")
        self._lbl_options = self._metric_card("Options", "0")
        self._lbl_stocks = self._metric_card("Stocks", "0")
        self._lbl_gross = self._metric_card("Gross Exposure", "$0")
        self._lbl_net = self._metric_card("Net Exposure", "$0")
        self._lbl_total_value = self._metric_card("Total Value", "$0")

        for w in (self._lbl_positions, self._lbl_options, self._lbl_stocks,
                  self._lbl_gross, self._lbl_net, self._lbl_total_value):
            exposure_layout.addWidget(w)

        layout.addWidget(exposure_box)

        # ── Risk Rules Status ─────────────────────────────────────────────
        rules_box = QGroupBox("Risk Limits (from risk_matrix.yaml)")
        rules_layout = QVBoxLayout(rules_box)
        self._lbl_rules_status = QLabel("Connect to IBKR to load risk rules…")
        self._lbl_rules_status.setWordWrap(True)
        self._lbl_rules_status.setStyleSheet("padding: 12px; font-size: 13px;")
        rules_layout.addWidget(self._lbl_rules_status)
        layout.addWidget(rules_box)

        # ── Refresh button ────────────────────────────────────────────────
        btn_layout = QHBoxLayout()
        self._btn_refresh = QPushButton("🔄 Refresh Risk Metrics")
        self._btn_refresh.setFixedHeight(32)
        btn_layout.addWidget(self._btn_refresh)
        btn_layout.addStretch()
        self._lbl_status = QLabel("Waiting for data…")
        self._lbl_status.setStyleSheet("color: #888;")
        btn_layout.addWidget(self._lbl_status)
        layout.addLayout(btn_layout)

        layout.addStretch()

    def _metric_card(self, title: str, value: str) -> QLabel:
        lbl = QLabel(f"<small>{title}</small><br/><b>{value}</b>")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setMinimumWidth(120)
        lbl.setStyleSheet(
            "QLabel { background: #2d2d2d; color: white; padding: 10px; "
            "border-radius: 6px; font-size: 13px; }"
        )
        return lbl

    def _update_card(self, lbl: QLabel, title: str, value: str, color: str = "white") -> None:
        lbl.setText(f"<small>{title}</small><br/><b style='color:{color}'>{value}</b>")

    def _connect_signals(self) -> None:
        self._btn_refresh.clicked.connect(self._on_refresh)
        self._engine.risk_updated.connect(self._on_risk_updated)

    @Slot()
    def _on_refresh(self) -> None:
        self._lbl_status.setText("Refreshing…")
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        try:
            await self._engine.refresh_positions()
            self._lbl_status.setText("✅ Updated")
        except Exception as exc:
            self._lbl_status.setText(f"❌ {exc}")

    @Slot(object)
    def _on_risk_updated(self, risk: PortfolioRiskSummary) -> None:
        # Greeks
        delta_color = "#e74c3c" if abs(risk.total_spx_delta) > 100 else "#27ae60"
        self._update_card(self._lbl_spx_delta, "SPX Δ", f"{risk.total_spx_delta:+.2f}", delta_color)
        self._update_card(self._lbl_delta, "Total Δ", f"{risk.total_delta:+.2f}")
        self._update_card(self._lbl_gamma, "Total Γ", f"{risk.total_gamma:+.4f}")

        theta_color = "#27ae60" if risk.total_theta > 0 else "#e74c3c"
        self._update_card(self._lbl_theta, "Total Θ", f"{risk.total_theta:+.2f}", theta_color)
        self._update_card(self._lbl_vega, "Total V", f"{risk.total_vega:+.2f}")

        tv_color = "#27ae60" if risk.theta_vega_ratio > 0 else "#e74c3c"
        self._update_card(self._lbl_tv_ratio, "Θ/V Ratio", f"{risk.theta_vega_ratio:+.3f}", tv_color)

        # Exposure
        self._update_card(self._lbl_positions, "Positions", str(risk.total_positions))
        self._update_card(self._lbl_options, "Options", str(risk.options_count))
        self._update_card(self._lbl_stocks, "Stocks", str(risk.stocks_count))
        self._update_card(self._lbl_gross, "Gross Exposure", f"${risk.gross_exposure:,.0f}")
        self._update_card(self._lbl_net, "Net Exposure", f"${risk.net_exposure:+,.0f}")
        self._update_card(self._lbl_total_value, "Total Value", f"${risk.total_value:+,.0f}")

        self._lbl_status.setText(f"✅ {risk.total_positions} positions analyzed")

        # Risk limits (load once; refresh on each update with current greek values)
        self._refresh_risk_rules(risk)

    def _refresh_risk_rules(self, risk: PortfolioRiskSummary) -> None:
        """Update the Risk Limits panel using risk_matrix.yaml + current NLV."""
        try:
            account = self._engine.account_snapshot()
            nlv = float(getattr(account, "net_liquidation", 0) or 0) if account else 0
            limits = _load_risk_limits(nlv)
            if not limits:
                self._lbl_rules_status.setText(
                    "⚠ Could not parse risk_matrix.yaml"
                )
                return

            nlv_str = f"${nlv:,.0f}" if nlv else "NLV unavailable"
            current = {
                "Daily Theta (min)":  risk.total_theta,
                "Short Vega (max)":   risk.total_vega,
                "SPX Δ (max |abs|)":  abs(risk.total_spx_delta),
                "Gamma (max)":        risk.total_gamma,
            }

            lines = [
                f"<b>Regime: {_DEFAULT_REGIME.replace('_', ' ').title()}</b>"
                f"  &nbsp; NLV: {nlv_str}",
                "<hr style='border:1px solid #444;'/>",
            ]
            for name, limit, direction in limits:
                cur = current.get(name, 0)
                if cur is None:
                    cur = 0.0
                if direction == "min":
                    breached = float(cur) < float(limit)
                elif direction == "absmax":
                    breached = abs(float(cur)) > abs(float(limit))
                else:
                    breached = float(cur) < float(limit)  # 'max' for negatives
                icon  = "🔴" if breached else "🟢"
                color = "#e74c3c" if breached else "#27ae60"
                lines.append(
                    f"{icon} <b>{name}</b>: "
                    f"<span style='color:{color}'>{cur:+.2f}</span>"
                    f" &nbsp;/&nbsp; limit {limit:+.2f}"
                )

            self._lbl_rules_status.setText(
                "<div style='font-family:Menlo, Monaco, Courier New; font-size:12px; line-height:1.6'>"
                + "<br/>".join(lines)
                + "</div>"
            )
        except Exception as exc:
            self._lbl_rules_status.setText(f"⚠ Error loading risk rules: {exc}")


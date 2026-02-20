"""dashboard/components/ai_suggestions.py — AI trade suggestion cards (T052-T053).

Displays:
1. Breach alert banner (always shown when violations > 0; degraded banner when AI unavailable)
2. Up to 3 suggestion cards with projected Greeks improvement, theta cost, rationale
3. "Use This Trade" button → populates order builder via session_state
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

import streamlit as st

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public entry point (T056)
# ---------------------------------------------------------------------------

def render_ai_suggestions(
    *,
    violations: list[dict[str, Any]],
    portfolio_greeks: Any,          # PortfolioGreeks or None
    vix: float,
    regime: str,
) -> None:
    """Render the AI suggestions panel in `dashboard/app.py`.

    Args:
        violations:       Output of `portfolio_tools.check_risk_limits()`.
        portfolio_greeks: Current aggregate Greeks (PortfolioGreeks or None).
        vix:              Current VIX value.
        regime:           Active regime name string.
    """
    st.subheader("🤖 AI Risk Analyst")

    if not violations:
        st.success("✅ No risk breaches detected — portfolio within all limits.")
        # Still allow on-demand suggestion if user wants
        if not st.button("🔮 Generate Optional AI Ideas", key="ai_suggest_optional"):
            return
        breach = None
    else:
        _render_breach_banners(violations)
        breach = _violations_to_breach(violations, regime=regime, vix=vix)

    # --- Load or refresh suggestions ---
    _trigger_suggestion_refresh(
        portfolio_greeks=portfolio_greeks,
        vix=vix,
        regime=regime,
        breach=breach,
    )

    suggestions: list[Any] = st.session_state.get("ai_suggestions", [])
    is_loading: bool = st.session_state.get("ai_suggestions_loading", False)
    error_msg: str = st.session_state.get("ai_suggestions_error", "")

    if is_loading:
        with st.spinner("⏳ AI analyst generating trade suggestions…"):
            st.info("Suggestions will appear after the current analysis completes.")
        return

    if error_msg:
        st.warning(f"⚠️ AI unavailable — {error_msg}")
        st.caption("The breach banner above still shows active violations. Manual action required.")
        return

    if not suggestions:
        st.info("ℹ️ AI unavailable — no suggestions generated.")
        return

    st.markdown(f"**{len(suggestions)} remediation idea(s) suggested:**")
    for i, suggestion in enumerate(suggestions):
        _render_suggestion_card(suggestion, card_index=i)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _render_breach_banners(violations: list[dict[str, Any]]) -> None:
    """Display one alert line per violation."""
    for v in violations:
        metric = v.get("metric", "Unknown")
        current = v.get("current", 0)
        limit = v.get("limit", 0)
        message = v.get("message", "")
        st.error(
            f"🚨 **BREACH — {metric}**: current={current:.2f}, limit={limit:.2f} — {message}"
        )


def _violations_to_breach(
    violations: list[dict[str, Any]],
    *,
    regime: str,
    vix: float,
) -> Any:
    """Convert first violation dict into a RiskBreach dataclass."""
    from models.order import RiskBreach
    v = violations[0]
    return RiskBreach(
        breach_type=v.get("metric", "unknown").lower().replace(" ", "_"),
        threshold_value=float(v.get("limit", 0)),
        actual_value=float(v.get("current", 0)),
        regime=regime,
        vix=vix,
    )


def _trigger_suggestion_refresh(
    *,
    portfolio_greeks: Any,
    vix: float,
    regime: str,
    breach: Any,
) -> None:
    """Start a background thread to call suggest_trades() if not already running.

    Uses a refresh key derived from breach/vix to avoid re-triggering the same
    analysis on every Streamlit rerun.
    """
    from models.order import PortfolioGreeks

    breach_key = (
        f"{getattr(breach, 'breach_type', 'none')}|{vix:.1f}|{regime}"
    )
    last_key = st.session_state.get("ai_suggestions_breach_key", "")
    already_loading = st.session_state.get("ai_suggestions_loading", False)

    if breach_key == last_key and not already_loading:
        return  # Already have results for this breach; nothing to do

    if already_loading:
        return  # Thread already running

    # Kick off background analysis
    st.session_state["ai_suggestions_loading"] = True
    st.session_state["ai_suggestions_breach_key"] = breach_key
    st.session_state["ai_suggestions_error"] = ""

    greeks: PortfolioGreeks = portfolio_greeks if isinstance(portfolio_greeks, PortfolioGreeks) else PortfolioGreeks()

    def _run_suggest() -> None:
        try:
            from agents.llm_risk_auditor import LLMRiskAuditor
            from database.local_store import LocalStore

            store = LocalStore()
            auditor = LLMRiskAuditor(db=store)
            result = asyncio.run(
                auditor.suggest_trades(
                    portfolio_greeks=greeks,
                    vix=vix,
                    regime=regime,
                    breach=breach,
                    theta_budget=abs(greeks.theta) * 1.5 if greeks.theta else 500.0,
                )
            )
            st.session_state["ai_suggestions"] = result
        except Exception as exc:
            LOGGER.warning("AI suggestions background thread error: %s", exc)
            st.session_state["ai_suggestions_error"] = str(exc)[:120]
        finally:
            st.session_state["ai_suggestions_loading"] = False

    thread = threading.Thread(target=_run_suggest, daemon=True)
    thread.start()


def _render_suggestion_card(suggestion: Any, *, card_index: int) -> None:
    """Render one AI suggestion as a Streamlit expander card (T053)."""
    legs_summary = _legs_to_summary(getattr(suggestion, "legs", []))
    delta_change = getattr(suggestion, "projected_delta_change", 0.0)
    theta_cost = getattr(suggestion, "projected_theta_cost", 0.0)
    rationale = getattr(suggestion, "rationale", "")
    suggestion_id = getattr(suggestion, "suggestion_id", "")

    card_label = f"💡 Idea {card_index + 1}: {legs_summary}"

    with st.expander(card_label, expanded=card_index == 0):
        col_a, col_b = st.columns(2)
        col_a.metric(
            "Projected Δ Change",
            f"{delta_change:+.2f}",
            help="Estimated SPX beta-delta reduction after trade",
        )
        theta_label = f"${abs(theta_cost):.0f}/d {'earned' if theta_cost < 0 else 'spent'}"
        col_b.metric(
            "Est. Θ Impact",
            theta_label,
            help="Positive = consumes theta budget; negative = earns theta",
        )

        st.markdown(f"**Rationale:** {rationale}")
        st.caption(f"Suggestion ID: `{suggestion_id}`")

        if st.button(f"Use This Trade", key=f"use_suggestion_{card_index}_{suggestion_id[:8]}"):
            _prefill_order_builder(suggestion)
            st.success("✅ Order builder pre-filled. Scroll up to review and submit.")


def _legs_to_summary(legs: list[Any]) -> str:
    """Produce a short text summary of legs for the card header."""
    if not legs:
        return "No legs"
    parts = []
    for leg in legs[:2]:  # cap at 2 legs for header
        action = getattr(leg, "action", None)
        symbol = getattr(leg, "symbol", "?")
        qty = getattr(leg, "quantity", 1)
        action_str = action.value if hasattr(action, "value") else str(action)
        parts.append(f"{action_str} {qty}x {symbol}")
    suffix = f" +{len(legs) - 2} more" if len(legs) > 2 else ""
    return ", ".join(parts) + suffix


def _prefill_order_builder(suggestion: Any) -> None:
    """Store suggestion data in session_state for the order builder to pick up (T054)."""
    st.session_state["ai_prefill_legs"] = getattr(suggestion, "legs", [])
    st.session_state["ai_suggestion_id"] = getattr(suggestion, "suggestion_id", "")
    st.session_state["ai_rationale"] = getattr(suggestion, "rationale", "")

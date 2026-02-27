"""dashboard/components/risk_compliance_view.py — Risk compliance + trade suggestions.

Renders:
  1. Risk compliance status (violations table)
  2. For each violation, a capital-efficient trade suggestion with a "Create Trade" button
  3. User can add a journal rationale when creating the trade
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
import streamlit as st

LOGGER = logging.getLogger(__name__)

# ── Commission schedule ───────────────────────────────────────────────────────
COMM = {"ES": 1.40, "MES": 0.47, "DEFAULT": 0.65}


def render_risk_compliance(
    violations: list[dict],
    summary: dict,
    regime: Any,
    positions: list,
    adapter: Any = None,
    account_id: str = "",
    ibkr_summary: dict | None = None,
    vix_data: dict | None = None,
    macro_data: dict | None = None,
    prefill_order_fn: Any = None,
) -> None:
    """Render Risk Compliance section with capital-efficient trade suggestions."""
    st.subheader("🛡️ Risk Compliance")

    if not violations:
        st.success("✅ All regime limits are currently satisfied.")
    else:
        st.error(f"⚠️ {len(violations)} risk limit(s) violated:")
        viol_df = pd.DataFrame(violations)
        st.dataframe(viol_df, use_container_width=True, hide_index=True)

    # ── Gamma Risk by DTE (compact) ───────────────────────────────────────
    if positions:
        from agent_tools.portfolio_tools import PortfolioTools
        _pt = PortfolioTools()
        gamma_by_dte = _pt.get_gamma_risk_by_dte(positions)
        bucket_order = ["0-7", "8-30", "31-60", "60+"]
        g_cols = st.columns(4)
        for i, bucket in enumerate(bucket_order):
            val = float(gamma_by_dte.get(bucket, 0.0))
            if bucket == "0-7" and abs(val) > 5.0:
                g_cols[i].metric(f"Γ {bucket}d", f"{val:.3f}", delta="⚠ High", delta_color="inverse")
            else:
                g_cols[i].metric(f"Γ {bucket}d", f"{val:.3f}")

    # ── Trade suggestions to fix violations ───────────────────────────────
    if violations:
        st.markdown("---")
        st.markdown("### 💡 Suggested Risk-Improving Trades")
        st.caption(
            "Each suggestion aims for the most capital-efficient fix. "
            "Click **Create Trade** to pre-fill the Order Builder, then add your rationale."
        )

        for idx, v in enumerate(violations):
            metric = v.get("metric", "")
            current = float(v.get("current", 0))
            limit_val = float(v.get("limit", 0))
            suggestion = _suggest_trade_for_violation(
                metric=metric,
                current=current,
                limit_val=limit_val,
                summary=summary,
                positions=positions,
            )
            if suggestion:
                with st.container(border=True):
                    cols = st.columns([4, 1])
                    with cols[0]:
                        st.markdown(f"**{metric}**: current `{current:.2f}` vs limit `{limit_val:.2f}`")
                        st.markdown(f"**Suggestion:** {suggestion['description']}")
                        if suggestion.get("legs"):
                            _leg_strs = []
                            for lg in suggestion["legs"]:
                                _leg_strs.append(
                                    f"{lg['action']} {lg.get('qty', 1)}x "
                                    f"{lg.get('symbol', '?')} {lg.get('right', '')} {lg.get('strike', '')} "
                                    f"exp {lg.get('expiry', '?')}"
                                )
                            st.caption("Legs: " + " | ".join(_leg_strs))
                            comm = _estimate_commission(suggestion["legs"])
                            st.caption(f"Est. commission: ${comm:.2f}")
                    with cols[1]:
                        rationale = st.text_input(
                            "Rationale",
                            value=suggestion.get("rationale", ""),
                            key=f"risk_fix_rationale_{idx}",
                            placeholder="Why this trade…",
                        )
                        if st.button(
                            "🚀 Create Trade",
                            key=f"risk_fix_create_{idx}",
                            use_container_width=True,
                            type="primary",
                        ):
                            _stage_risk_trade(
                                suggestion["legs"],
                                rationale=rationale or suggestion.get("rationale", "Risk fix"),
                                prefill_order_fn=prefill_order_fn,
                            )
                            st.rerun()
            else:
                st.caption(f"No automatic suggestion available for **{metric}** violation.")

    # ── Theta/Vega ratio summary ──────────────────────────────────────────
    tv_ratio = float(summary.get("theta_vega_ratio", 0))
    tv_zone = summary.get("theta_vega_zone", "neutral")
    zone_emoji = {"green": "🟢", "neutral": "🟡", "red": "🔴"}.get(tv_zone, "⚪")
    st.caption(
        f"{zone_emoji} Theta/Vega ratio: **{tv_ratio:.3f}** "
        f"(target: 0.25–0.40, Sebastian framework)"
    )


def _suggest_trade_for_violation(
    metric: str,
    current: float,
    limit_val: float,
    summary: dict,
    positions: list,
) -> Optional[dict]:
    """Generate a capital-efficient trade suggestion for a specific violation.

    Strategy selection prioritises capital efficiency:
    - For SPX Delta: use /MES (cheapest margin) to neutralise
    - For Vega: sell a short-dated strangle (highest vega/margin)
    - For Theta: buy protective puts (cheapest theta reduction)
    - For Gamma: sell far-dated options (gamma near zero, minimal margin impact)
    """
    from datetime import date as _d

    # Default expiry: ~30 DTE for efficiency
    target_expiry = (_d.today() + timedelta(days=30)).isoformat()
    short_expiry = (_d.today() + timedelta(days=7)).isoformat()

    metric_lower = metric.lower()
    option_positions = [
        p for p in positions
        if getattr(p, "instrument_type", None) is not None
        and getattr(p.instrument_type, "name", "") == "OPTION"
        and abs(float(getattr(p, "quantity", 0.0) or 0.0)) > 0
    ]

    def _close_leg_for_option(pos: Any, qty: int = 1) -> dict[str, Any]:
        _is_short = float(getattr(pos, "quantity", 0.0) or 0.0) < 0
        _action = "BUY" if _is_short else "SELL"
        return {
            "action": _action,
            "symbol": getattr(pos, "underlying", None) or getattr(pos, "symbol", ""),
            "qty": max(1, int(qty)),
            "instrument_type": "Option",
            "strike": float(getattr(pos, "strike", 0.0) or 0.0),
            "right": str(getattr(pos, "option_type", "") or "").upper(),
            "expiry": getattr(pos, "expiration", None),
            "conid": getattr(pos, "broker_id", None),
        }

    if "spx delta" in metric_lower or "beta_delta" in metric_lower:
        # Use /MES to delta hedge — cheapest margin per delta unit
        excess = current - limit_val if current > 0 else current + limit_val
        # /MES delta ≈ 5 per contract
        contracts = max(1, int(abs(excess) / 5))
        action = "SELL" if current > 0 else "BUY"
        return {
            "description": (
                f"{action} {contracts}x /MES to reduce SPX delta from {current:.1f} toward {limit_val:.1f}. "
                f"/MES is most capital-efficient ($0.47 comm, ~$800 margin vs $4,000 for /ES)."
            ),
            "legs": [{
                "action": action,
                "symbol": "MES",
                "qty": contracts,
                "instrument_type": "Future",
                "strike": None,
                "right": None,
                "expiry": None,
            }],
            "rationale": f"Delta hedge: {action} {contracts}x /MES to reduce SPX delta exposure",
        }

    if "vega" in metric_lower:
        # Prefer closing largest absolute vega contributor among options.
        if option_positions:
            pos = max(option_positions, key=lambda p: abs(float(getattr(p, "vega", 0.0) or 0.0)))
            lg = _close_leg_for_option(pos, qty=1)
            return {
                "description": (
                    "Close one high-vega option leg to reduce vega exposure immediately. "
                    "This keeps execution capital-efficient and contract-specific."
                ),
                "legs": [lg],
                "rationale": "Reduce vega by closing a high-vega option leg",
            }
        return {
            "description": (
                "Consider selling a short-dated (7-14 DTE) strangle or iron condor "
                "to reduce vega exposure. Short-dated options have highest vega decay "
                "per dollar of margin required."
            ),
            "legs": [],
            "rationale": "Reduce vega exposure via short-dated premium selling",
        }

    if "theta" in metric_lower:
        if option_positions:
            pos = max(option_positions, key=lambda p: abs(float(getattr(p, "theta", 0.0) or 0.0)))
            lg = _close_leg_for_option(pos, qty=1)
            return {
                "description": (
                    "Close one high-theta option leg to reduce immediate time-decay exposure."
                ),
                "legs": [lg],
                "rationale": "Reduce theta decay by trimming highest-theta leg",
            }
        return {
            "description": (
                "Buy protective options or close short positions to reduce theta. "
                "Target far-dated positions (60+ DTE) to reduce theta with minimal vega impact."
            ),
            "legs": [],
            "rationale": "Reduce theta decay exposure",
        }

    if "gamma" in metric_lower:
        if option_positions:
            pos = min(
                option_positions,
                key=lambda p: float(getattr(p, "days_to_expiration", 9999) or 9999),
            )
            lg = _close_leg_for_option(pos, qty=1)
            return {
                "description": (
                    "Close one near-expiry option leg to reduce concentrated gamma risk "
                    "in the front bucket."
                ),
                "legs": [lg],
                "rationale": "Reduce front-dated gamma by closing nearest-expiry leg",
            }
        return {
            "description": (
                "Close or roll near-expiry (0-7 DTE) option positions. "
                "Gamma concentrates near expiration — rolling to 30+ DTE "
                "dramatically reduces gamma risk."
            ),
            "legs": [],
            "rationale": "Reduce concentrated gamma risk near expiration",
        }

    return None


def _estimate_commission(legs: list[dict]) -> float:
    """Estimate round-trip commission for a set of legs."""
    total = 0.0
    for leg in legs:
        qty = int(leg.get("qty", 1))
        sym = str(leg.get("symbol", "")).upper()
        if sym in COMM:
            total += COMM[sym] * qty
        else:
            total += COMM["DEFAULT"] * qty
    return total


def _stage_risk_trade(legs: list[dict], rationale: str, prefill_order_fn: Any = None) -> None:
    """Pre-fill Order Builder from a risk-improvement suggestion."""
    normalized = []
    for lg in legs:
        normalized.append({
            "action": lg.get("action", "BUY"),
            "symbol": lg.get("symbol", ""),
            "qty": int(lg.get("qty", 1)),
            "instrument_type": lg.get("instrument_type", "Option"),
            "strike": lg.get("strike"),
            "right": lg.get("right"),
            "expiry": lg.get("expiry"),
            # Preserve conid so the Order Builder can skip TWS qualification
            "conid": lg.get("conid") or lg.get("conId") or lg.get("broker_id"),
        })

    if callable(prefill_order_fn):
        prefill_order_fn(
            legs=normalized,
            source_label="risk compliance suggestion",
            rationale=rationale,
        )
        return

    st.session_state["ob_prefill_data"] = {
        "leg_count": len(normalized),
        "legs": normalized,
        "rationale": rationale,
        "reset_approved": True,
    }
    st.session_state["ob_force_expand"] = True
    st.toast(f"📋 Risk trade pre-filled — {rationale[:50]}", icon="🛡️")

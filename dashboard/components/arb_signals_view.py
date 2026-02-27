"""dashboard/components/arb_signals_view.py — Arbitrage signals sorted by fill probability.

Sorts signals by:
  1. Tightness of bid/ask spread (proxy for fill probability)
  2. Net edge AFTER commissions
  3. Confidence score

Also shows a "Create Trade" button for each signal.
"""
from __future__ import annotations

import ast
import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import streamlit as st

LOGGER = logging.getLogger(__name__)

# Commission schedule (per contract, round-trip)
COMM = {
    "ES": 1.40,
    "MES": 0.47,
    "NQ": 1.40,
    "MNQ": 0.47,
    "SPX": 0.65,
    "SPY": 0.65,
    "QQQ": 0.65,
    "DEFAULT_OPTION": 0.65,
    "DEFAULT_STOCK": 0.005,
}


def _estimate_commission_for_legs(legs: list[dict]) -> float:
    """Estimate total commission for a set of legs."""
    total = 0.0
    for lg in legs:
        qty = max(1, int(float(lg.get("quantity", lg.get("qty", 1)) or 1)))
        sym = str(lg.get("symbol", "")).upper()
        if sym in COMM:
            total += COMM[sym] * qty
        else:
            total += COMM["DEFAULT_OPTION"] * qty
    return total


def _fill_probability_score(legs: list[dict]) -> float:
    """Estimate fill probability from bid/ask spread tightness.

    Score 0–1 where tighter spreads → higher score.
    For each leg, compute spread / mid.  Average across legs.
    A spread/mid < 5% → score ~0.95; >20% → score ~0.30.
    """
    if not legs:
        return 0.5  # neutral

    ratios = []
    for lg in legs:
        bid = lg.get("bid")
        ask = lg.get("ask")
        if bid is not None and ask is not None:
            try:
                bid_f, ask_f = float(bid), float(ask)
                mid = (bid_f + ask_f) / 2.0
                if mid > 0:
                    ratios.append((ask_f - bid_f) / mid)
                else:
                    ratios.append(1.0)  # can't price → assume wide
            except (ValueError, TypeError):
                ratios.append(1.0)
        else:
            ratios.append(0.5)  # no quote → neutral

    avg_ratio = sum(ratios) / len(ratios) if ratios else 0.5
    # Map: 0% spread → 1.0, 30% spread → 0.0 (linear)
    score = max(0.0, min(1.0, 1.0 - (avg_ratio / 0.30)))
    return score


def render_arb_signals(
    signals: list[dict],
    build_order_legs_fn: Any,
    with_live_quotes_fn: Any,
    prefill_order_fn: Any,
    estimate_combo_quote_fn: Any,
    market_data_svc: Any = None,
) -> None:
    """Render arbitrage signals, sorted by fill probability × net edge after commissions.

    Parameters
    ----------
    signals : list[dict]
        Raw signal dicts from DB (id, signal_type, confidence, net_value, legs_json, detected_at).
    build_order_legs_fn : callable
        Function to convert signal dict → list of order-leg dicts.
    with_live_quotes_fn : callable
        Function to enrich legs with live bid/ask/mid.
    prefill_order_fn : callable
        Function to stage legs into Order Builder.
    estimate_combo_quote_fn : callable
        Function to compute combo bid/ask/mid/spread from legs.
    market_data_svc : optional
        MarketDataService instance for live quote enrichment.
    """
    st.subheader("📊 Arbitrage Signals")

    if not signals:
        st.info("No active arbitrage signals.")
        return

    # Global underlying override
    arb_underlying = st.text_input(
        "Default underlying symbol",
        value="SPX",
        key="arb_signal_underlying_v2",
    )

    # ── Enrich each signal with live quotes, commissions, fill score ──
    enriched: list[dict] = []
    for sig_i, sig in enumerate(signals):
        sig_id = str(sig.get("id", sig_i))
        sig_type = str(sig.get("signal_type", "unknown"))
        confidence = float(sig.get("confidence", 0) or 0)
        net_val = float(sig.get("net_value", 0) or 0)
        detected = str(sig.get("detected_at", ""))[:16]
        legs_raw = sig.get("legs_json", {})

        if isinstance(legs_raw, str):
            try:
                legs_raw = ast.literal_eval(legs_raw)
            except Exception:
                legs_raw = {}

        # Build legs
        draft_sig = dict(sig)
        if isinstance(draft_sig.get("legs_json"), str):
            try:
                draft_sig["legs_json"] = ast.literal_eval(draft_sig["legs_json"])
            except Exception:
                pass

        legs = build_order_legs_fn(draft_sig, default_underlying=arb_underlying) or []
        legs = with_live_quotes_fn(legs, market_data_svc)

        # Calculate fill probability and commission-adjusted edge
        fill_score = _fill_probability_score(legs)
        commission = _estimate_commission_for_legs(legs)
        net_edge_after_comm = net_val - commission

        # Composite ranking score: fill_prob * net_edge_after_comm * confidence
        # Higher = better trade
        rank_score = fill_score * max(0, net_edge_after_comm) * max(0.01, confidence)

        # Combo quote
        combo_quote = estimate_combo_quote_fn(legs) if legs else {}

        enriched.append({
            "sig_i": sig_i,
            "sig_id": sig_id,
            "sig_type": sig_type,
            "confidence": confidence,
            "net_value": net_val,
            "net_edge_after_comm": net_edge_after_comm,
            "fill_score": fill_score,
            "commission": commission,
            "rank_score": rank_score,
            "detected": detected,
            "legs_raw": legs_raw,
            "legs": legs,
            "combo_quote": combo_quote,
            "raw_sig": sig,
        })

    # ── Sort by rank_score descending ─────────────────────────────────
    enriched.sort(key=lambda x: x["rank_score"], reverse=True)

    # ── Summary table ─────────────────────────────────────────────────
    summary_rows = []
    for e in enriched:
        summary_rows.append({
            "Type": e["sig_type"].replace("_", " ").title(),
            "Fill Prob": f"{e['fill_score']:.0%}",
            "Gross Edge": f"${e['net_value']:.2f}",
            "Commission": f"${e['commission']:.2f}",
            "Net Edge": f"${e['net_edge_after_comm']:.2f}",
            "Confidence": f"{e['confidence']:.0%}",
            "Rank": f"{e['rank_score']:.2f}",
            "Detected": e["detected"],
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
    st.caption(
        f"{len(enriched)} signal(s) sorted by fill probability × net edge after commissions. "
        "Commissions: /ES=$1.40, /MES=$0.47, options=$0.65."
    )

    # ── Per-signal cards ──────────────────────────────────────────────
    for e in enriched:
        sig_id = e["sig_id"]
        sig_i = e["sig_i"]
        sig_type = e["sig_type"]
        legs = e["legs"]
        combo_quote = e["combo_quote"]

        # Build description parts
        desc_parts: list[str] = []
        if isinstance(e["legs_raw"], dict):
            for k in ("strike", "strikes", "expiry", "direction"):
                if k in e["legs_raw"]:
                    desc_parts.append(f"{k.title()}: {e['legs_raw'][k]}")

        sig_reason = (
            f"{sig_type.replace('_', ' ').title()} arbitrage — "
            f"confidence {e['confidence']:.0%}, "
            f"net edge ${e['net_edge_after_comm']:.2f} (after ${e['commission']:.2f} comm). "
            + (", ".join(desc_parts) if desc_parts else "")
        )

        with st.container(border=True):
            sc = st.columns([1, 5])
            with sc[0]:
                if st.button(
                    "🚀 Create\nOrder",
                    key=f"arb_create_{sig_id}_{sig_i}_v2",
                    use_container_width=True,
                    help="Pre-fill Order Builder with this signal's legs",
                ):
                    if legs and prefill_order_fn(
                        legs=legs,
                        source_label=f"arb signal {sig_id[:8]} ({sig_type})",
                        rationale=sig_reason,
                    ):
                        st.rerun()
                    else:
                        st.warning("Signal could not be converted to order legs.")

                # Fill probability indicator
                fill_emoji = "🟢" if e["fill_score"] > 0.7 else ("🟡" if e["fill_score"] > 0.4 else "🔴")
                st.caption(f"{fill_emoji} Fill: {e['fill_score']:.0%}")

            with sc[1]:
                st.markdown(
                    f"**{sig_type.replace('_', ' ').title()}** &nbsp; "
                    f"Fill: `{e['fill_score']:.0%}` &nbsp; "
                    f"Net edge: `${e['net_edge_after_comm']:.2f}` &nbsp; "
                    f"Conf: `{e['confidence']:.0%}` &nbsp; _{e['detected']}_"
                )
                st.caption(f"Commission: ${e['commission']:.2f} | Gross: ${e['net_value']:.2f}")

                if combo_quote.get("combo_bid") is not None:
                    st.caption(
                        f"Quote — Bid {float(combo_quote['combo_bid']):.2f} | "
                        f"Ask {float(combo_quote['combo_ask']):.2f} | "
                        f"Mid {float(combo_quote['combo_mid']):.2f} | "
                        f"Spread {float(combo_quote['combo_spread']):.2f} "
                        f"({combo_quote['quoted_legs']}/{combo_quote['total_legs']} legs quoted)"
                    )

                if legs:
                    leg_parts = [
                        f"{lg.get('action', '?')} {lg.get('right', '?')}{lg.get('strike', '?')}"
                        for lg in legs if lg.get("strike")
                    ]
                    if leg_parts:
                        st.caption("Legs: " + " | ".join(leg_parts))

    st.caption("Draft creation pre-fills Order Builder. Submission requires simulation + explicit approval.")

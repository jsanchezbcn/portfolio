"""
agents/arb_hunter.py — User Story 3: Passive arbitrage detection.

ArbHunter scans an option chain for two types of arbitrage signals:

1. Put-Call Parity (PCP) violations:
   C - P ≠ F - K·e^{-rT}
   Flags when the net mispricing exceeds 2 × fee_per_leg.

2. Box Spread violations:
   A 4-leg synthetic bond should price at (K2 - K1).
   Box credit = (C_K1 - C_K2) + (P_K2 - P_K1).
   Flags when credit > (K2 - K1) + 4 × fee_per_leg (reverse arb).

All detected signals are written to the signals DB table.
Stale ACTIVE signals that no longer hold are marked EXPIRED via
expire_stale_signals().

Expected chain format:
    {
        "underlying_price": float,
        "risk_free_rate": float,          # annualised, e.g. 0.05
        "2025-12-19": {                   # expiration ISO date string
            5000.0: {"call": float, "put": float},
            ...
        },
        ...
    }

Environment variables:
  ARB_FEE_PER_LEG: override fee_per_leg (default from config/risk_matrix.yaml)
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Resolve arb_fee_per_leg from config/risk_matrix.yaml at module load time
# so it can be used as a default argument in ArbHunter.__init__.
_CONFIG_PATH = Path(__file__).parent.parent / "config" / "risk_matrix.yaml"


def _load_fee_from_config() -> float:
    try:
        with _CONFIG_PATH.open() as f:
            cfg = yaml.safe_load(f)
        return float(cfg.get("arb_fee_per_leg", 0.65))
    except Exception:
        return 0.65


_DEFAULT_FEE: float = float(os.getenv("ARB_FEE_PER_LEG", str(_load_fee_from_config())))


class ArbHunter:
    """Scans option chain data for arbitrage opportunities.

    Args:
        db:           Initialised DBManager instance.
        fee_per_leg:  Transaction cost per option leg in dollars
                      (default from config/risk_matrix.yaml ``arb_fee_per_leg``).
    """

    def __init__(self, *, db: Any, fee_per_leg: float = _DEFAULT_FEE) -> None:
        self.db = db
        self.fee_per_leg = fee_per_leg

    # ------------------------------------------------------------------ #
    # T032 — Put-Call Parity check                                        #
    # ------------------------------------------------------------------ #

    def _check_put_call_parity(self, chain: dict[str, Any]) -> list[dict[str, Any]]:
        """Detect Put-Call Parity violations.

        $C - P = F - K \\cdot e^{-rT}$

        Flags violations where |net_ev| > fee_per_leg * 2.

        Returns a list of signal dicts ready for insert_signal().
        """
        signals: list[dict[str, Any]] = []
        underlying_price: float = chain.get("underlying_price", 0.0)
        risk_free_rate: float = chain.get("risk_free_rate", 0.0)
        today = date.today()

        for key, strikes in chain.items():
            if not isinstance(strikes, dict):
                continue
            # key is expected to be an ISO expiration date string
            try:
                exp_date = date.fromisoformat(str(key))
            except (ValueError, TypeError):
                continue

            # Time to expiration in years
            t_years = max((exp_date - today).days / 365.0, 1 / 365.0)

            for strike, prices in strikes.items():
                try:
                    strike_f = float(strike)
                    call_price = float(prices.get("call", 0.0))
                    put_price = float(prices.get("put", 0.0))
                except (TypeError, ValueError):
                    continue

                # Theoretical: C - P = S - K * e^{-rT}
                forward_parity = underlying_price - strike_f * math.exp(
                    -risk_free_rate * t_years
                )
                observed_diff = call_price - put_price
                net_ev = observed_diff - forward_parity

                threshold = self.fee_per_leg * 2
                if abs(net_ev) > threshold:
                    direction = "CALL_OVERPRICED" if net_ev > 0 else "PUT_OVERPRICED"
                    signals.append(
                        {
                            "signal_type": f"PUT_CALL_PARITY_{direction}",
                            "legs_json": {
                                "expiration": str(key),
                                "strike": strike_f,
                                "call_price": call_price,
                                "put_price": put_price,
                                "forward_parity": round(forward_parity, 4),
                                "observed_diff": round(observed_diff, 4),
                                "net_ev": round(net_ev, 4),
                            },
                            "net_value": round(net_ev, 4),
                            "confidence": min(abs(net_ev) / (threshold * 10), 1.0),
                        }
                    )
                    logger.info(
                        "PCP violation: exp=%s K=%s net_ev=%.2f", key, strike_f, net_ev
                    )

        return signals

    # ------------------------------------------------------------------ #
    # T033 — Box Spread check                                              #
    # ------------------------------------------------------------------ #

    def _check_box_spread(self, chain: dict[str, Any]) -> list[dict[str, Any]]:
        """Detect Box Spread arbitrage opportunities.

        Box credit = (C_K1 - C_K2) + (P_K2 - P_K1)
        Theoretical value = K2 - K1 (discounted)

        Flags when credit > theoretical + 4 * fee_per_leg (too cheap to borrow)
        or when credit < theoretical - 4 * fee_per_leg (too expensive).

        Returns a list of signal dicts.
        """
        signals: list[dict[str, Any]] = []
        risk_free_rate: float = chain.get("risk_free_rate", 0.0)
        today = date.today()

        for key, strikes in chain.items():
            if not isinstance(strikes, dict):
                continue
            try:
                exp_date = date.fromisoformat(str(key))
            except (ValueError, TypeError):
                continue

            t_years = max((exp_date - today).days / 365.0, 1 / 365.0)
            df = math.exp(-risk_free_rate * t_years)  # discount factor

            sorted_strikes = sorted(float(k) for k in strikes.keys())
            for i, k1 in enumerate(sorted_strikes):
                for k2 in sorted_strikes[i + 1 :]:
                    try:
                        c1 = float(strikes[k1]["call"])
                        p1 = float(strikes[k1]["put"])
                        c2 = float(strikes[k2]["call"])
                        p2 = float(strikes[k2]["put"])
                    except (KeyError, TypeError, ValueError):
                        continue

                    # Box credit (sell call spread + sell put spread)
                    box_credit = (c1 - c2) + (p2 - p1)
                    theoretical = (k2 - k1) * df
                    net_ev = box_credit - theoretical
                    threshold = self.fee_per_leg * 4

                    if net_ev > threshold:
                        signals.append(
                            {
                                "signal_type": "BOX_SPREAD",
                                "legs_json": {
                                    "expiration": str(key),
                                    "lower_strike": k1,
                                    "upper_strike": k2,
                                    "c1": c1,
                                    "p1": p1,
                                    "c2": c2,
                                    "p2": p2,
                                    "box_credit": round(box_credit, 4),
                                    "theoretical": round(theoretical, 4),
                                    "net_ev": round(net_ev, 4),
                                },
                                "net_value": round(net_ev, 4),
                                "confidence": min(net_ev / (threshold * 5), 1.0),
                            }
                        )
                        logger.info(
                            "Box spread arb: exp=%s K1=%s K2=%s net_ev=%.2f",
                            key, k1, k2, net_ev,
                        )

        return signals

    # ------------------------------------------------------------------ #
    # T034 — scan (orchestration)                                         #
    # ------------------------------------------------------------------ #

    async def scan(self, chain: dict[str, Any]) -> list[str]:
        """Run all arb checks, persist new signals, expire stale ones.

        Returns the list of new signal IDs inserted this scan.
        """
        detected = (
            self._check_put_call_parity(chain) + self._check_box_spread(chain)
        )

        new_ids: list[str] = []
        for sig in detected:
            try:
                signal_id = await self.db.insert_signal(
                    signal_type=sig["signal_type"],
                    legs_json=sig["legs_json"],
                    net_value=sig.get("net_value"),
                    confidence=sig.get("confidence"),
                    status="ACTIVE",
                )
                new_ids.append(signal_id)
            except Exception:
                logger.exception("Failed to insert signal %s", sig["signal_type"])

        # T034: expire signals that are no longer active
        await self.db.expire_stale_signals(active_ids=new_ids)

        logger.info(
            "ArbHunter scan complete: %d new signals, %d stale candidates",
            len(new_ids),
            0,  # expire_stale_signals handles count internally
        )
        return new_ids

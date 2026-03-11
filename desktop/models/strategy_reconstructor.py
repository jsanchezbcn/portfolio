from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Iterable, Sequence


@dataclass(slots=True)
class StrategyGroup:
    association_id: str
    strategy_name: str
    underlying: str
    legs: list[Any]
    matched_by: str
    expiry_label: str
    strategy_family: str | None = None
    net_delta: float | None = None
    net_gamma: float | None = None
    net_theta: float | None = None
    net_vega: float | None = None
    net_spx_delta: float | None = None
    net_mkt_value: float = 0.0
    net_upnl: float = 0.0
    net_rpnl: float = 0.0

    def __post_init__(self) -> None:
        self._aggregate()

    def _aggregate(self) -> None:
        self.net_delta = self._sum_attr("delta")
        self.net_gamma = self._sum_attr("gamma")
        self.net_theta = self._sum_attr("theta")
        self.net_vega = self._sum_attr("vega")
        self.net_spx_delta = self._sum_attr("spx_delta")
        self.net_mkt_value = self._sum_attr("market_value") or 0.0
        self.net_upnl = self._sum_attr("unrealized_pnl") or 0.0
        self.net_rpnl = self._sum_attr("realized_pnl") or 0.0

    def _sum_attr(self, name: str) -> float | None:
        total = 0.0
        seen = False
        for leg in self.legs:
            value = getattr(leg, name, None)
            if value is None:
                continue
            total += float(value)
            seen = True
        return total if seen else None

    @property
    def leg_ids(self) -> list[int]:
        return [int(getattr(leg, "conid", 0) or 0) for leg in self.legs]

    def to_record(self) -> dict[str, Any]:
        return {
            "association_id": self.association_id,
            "strategy_name": self.strategy_name,
            "strategy_family": self.strategy_family,
            "underlying": self.underlying,
            "matched_by": self.matched_by,
            "expiry_label": self.expiry_label,
            "leg_ids": self.leg_ids,
            "net_delta": self.net_delta,
            "net_gamma": self.net_gamma,
            "net_theta": self.net_theta,
            "net_vega": self.net_vega,
            "net_spx_delta": self.net_spx_delta,
            "market_value": self.net_mkt_value,
            "unrealized_pnl": self.net_upnl,
            "realized_pnl": self.net_rpnl,
        }


@dataclass(slots=True)
class _VerticalCandidate:
    legs: tuple[Any, Any]
    strategy_name: str
    matched_by: str
    underlying: str
    expiry: str | None
    right: str
    lower_strike: float
    higher_strike: float
    quantity: float
    short_strike: float
    long_strike: float


class StrategyReconstructor:
    """Re-associate raw positions into human-friendly options strategies."""

    def __init__(self, *, account_id: str | None = None) -> None:
        self.account_id = (account_id or "").strip()

    def reconstruct(self, positions: Sequence[Any]) -> list[StrategyGroup]:
        by_underlying: dict[str, list[Any]] = defaultdict(list)
        for row in positions:
            by_underlying[_underlying(row)].append(row)

        groups: list[StrategyGroup] = []
        for underlying in sorted(by_underlying):
            groups.extend(self._reconstruct_underlying(underlying, by_underlying[underlying]))
        return sorted(groups, key=_group_sort_key)

    def to_records(self, positions: Sequence[Any]) -> list[dict[str, Any]]:
        return [group.to_record() for group in self.reconstruct(positions)]

    def _reconstruct_underlying(self, underlying: str, rows: Sequence[Any]) -> list[StrategyGroup]:
        groups: list[StrategyGroup] = []
        used: set[int] = set()

        bag_rows = [row for row in rows if _sec_type(row) == "BAG"]
        stock_rows = [row for row in rows if _sec_type(row) == "STK"]
        future_rows = [row for row in rows if _sec_type(row) == "FUT"]
        option_rows = [row for row in rows if _sec_type(row) in {"OPT", "FOP"}]

        for bag in bag_rows:
            groups.append(self._build_group(
                underlying=underlying,
                strategy_name=_combo_strategy_name(bag),
                legs=[bag],
                matched_by="bag_combo",
                strategy_family="combo",
            ))
            used.add(id(bag))

        for stock in stock_rows:
            if id(stock) in used:
                continue
            collar = self._find_collar(stock, option_rows, used)
            if collar:
                groups.append(self._build_group(
                    underlying=underlying,
                    strategy_name="Collar",
                    legs=[stock, *collar],
                    matched_by="collar",
                    strategy_family="stock_combo",
                ))
                used.add(id(stock))
                used.update(id(leg) for leg in collar)
                continue

            covered = self._find_stock_covered_structure(stock, option_rows, used)
            if covered:
                strategy_name, option_leg, matched_by = covered
                groups.append(self._build_group(
                    underlying=underlying,
                    strategy_name=strategy_name,
                    legs=[stock, option_leg],
                    matched_by=matched_by,
                    strategy_family="stock_combo",
                ))
                used.add(id(stock))
                used.add(id(option_leg))

        by_expiry: dict[str, list[Any]] = defaultdict(list)
        for leg in option_rows:
            if id(leg) in used:
                continue
            by_expiry[_expiry(leg) or ""].append(leg)

        for expiry, expiry_legs in sorted(by_expiry.items(), key=lambda item: item[0] or ""):
            expiry_groups = self._extract_same_expiry_groups(
                underlying=underlying,
                expiry=expiry,
                legs=expiry_legs,
                used=used,
            )
            groups.extend(expiry_groups)

        cross_expiry_groups = self._extract_cross_expiry_groups(
            underlying=underlying,
            option_rows=option_rows,
            used=used,
        )
        groups.extend(cross_expiry_groups)

        for leg in option_rows:
            if id(leg) in used:
                continue
            groups.append(self._build_group(
                underlying=underlying,
                strategy_name="Single Leg / Naked",
                legs=[leg],
                matched_by="single_leg",
                strategy_family="single",
            ))
            used.add(id(leg))

        for stock in stock_rows:
            if id(stock) in used:
                continue
            groups.append(self._build_group(
                underlying=underlying,
                strategy_name="Stock",
                legs=[stock],
                matched_by="single_stock",
                strategy_family="stock",
            ))
            used.add(id(stock))

        for fut in future_rows:
            if id(fut) in used:
                continue
            groups.append(self._build_group(
                underlying=underlying,
                strategy_name="Future",
                legs=[fut],
                matched_by="single_future",
                strategy_family="future",
            ))
            used.add(id(fut))

        return groups

    def _extract_same_expiry_groups(
        self,
        *,
        underlying: str,
        expiry: str | None,
        legs: Sequence[Any],
        used: set[int],
    ) -> list[StrategyGroup]:
        groups: list[StrategyGroup] = []

        butterflies = self._extract_butterflies(underlying=underlying, legs=legs, used=used)
        groups.extend(butterflies)

        verticals = self._extract_vertical_candidates(underlying=underlying, expiry=expiry, legs=legs, used=used)

        condor_groups, consumed_vertical_ids = self._pair_iron_structures(underlying=underlying, verticals=verticals)
        groups.extend(condor_groups)
        for group in condor_groups:
            used.update(id(leg) for leg in group.legs)
        verticals = [candidate for candidate in verticals if id(candidate) not in consumed_vertical_ids]

        jade_groups, consumed_vertical_ids = self._pair_jade_lizards(
            underlying=underlying,
            expiry=expiry,
            verticals=verticals,
            legs=legs,
            used=used,
        )
        groups.extend(jade_groups)
        for group in jade_groups:
            used.update(id(leg) for leg in group.legs)
        verticals = [candidate for candidate in verticals if id(candidate) not in consumed_vertical_ids]

        for candidate in verticals:
            groups.append(self._build_group(
                underlying=underlying,
                strategy_name=candidate.strategy_name,
                legs=list(candidate.legs),
                matched_by=candidate.matched_by,
                strategy_family="vertical",
            ))
            used.update(id(leg) for leg in candidate.legs)

        same_expiry_pairs = self._extract_same_expiry_pairs(underlying=underlying, legs=legs, used=used)
        groups.extend(same_expiry_pairs)

        return groups

    def _extract_butterflies(self, *, underlying: str, legs: Sequence[Any], used: set[int]) -> list[StrategyGroup]:
        groups: list[StrategyGroup] = []
        by_right: dict[str, list[Any]] = defaultdict(list)
        for leg in legs:
            if id(leg) in used:
                continue
            by_right[_right(leg)].append(leg)

        for right, right_legs in by_right.items():
            ordered = sorted(right_legs, key=lambda leg: (_strike(leg), abs(_quantity(leg))))
            strikes = [_strike(leg) for leg in ordered]
            if len(ordered) < 3 or any(strike is None for strike in strikes):
                continue
            local_used: set[int] = set()
            for combo in combinations(ordered, 3):
                if any(id(leg) in local_used or id(leg) in used for leg in combo):
                    continue
                sorted_combo = sorted(combo, key=_strike_sort_key)
                a, b, c = sorted_combo
                if not _same_expiry(sorted_combo):
                    continue
                if not _is_butterfly_ratio(sorted_combo):
                    continue
                strike_a, strike_b, strike_c = (_strike(a), _strike(b), _strike(c))
                if strike_a is None or strike_b is None or strike_c is None:
                    continue
                if abs((strike_b - strike_a) - (strike_c - strike_b)) > 1e-9:
                    continue
                strategy_name = f"{_right_label(right)} Butterfly".strip()
                groups.append(self._build_group(
                    underlying=underlying,
                    strategy_name=strategy_name or "Butterfly",
                    legs=sorted_combo,
                    matched_by="butterfly",
                    strategy_family="butterfly",
                ))
                local_used.update(id(leg) for leg in sorted_combo)
                used.update(id(leg) for leg in sorted_combo)
        return groups

    def _extract_vertical_candidates(
        self,
        *,
        underlying: str,
        expiry: str | None,
        legs: Sequence[Any],
        used: set[int],
    ) -> list[_VerticalCandidate]:
        candidates: list[_VerticalCandidate] = []
        by_right: dict[str, list[Any]] = defaultdict(list)
        for leg in legs:
            if id(leg) in used:
                continue
            by_right[_right(leg)].append(leg)

        for right, right_legs in by_right.items():
            ordered = sorted(
                [leg for leg in right_legs if _strike(leg) is not None],
                key=lambda leg: (_strike(leg), abs(_quantity(leg))),
            )
            local_used: set[int] = set()
            for idx, leg in enumerate(ordered):
                if id(leg) in local_used or id(leg) in used:
                    continue
                best: _VerticalCandidate | None = None
                for other in ordered[idx + 1:]:
                    if id(other) in local_used or id(other) in used:
                        continue
                    if not _is_balanced_pair(leg, other):
                        continue
                    strategy_name = _vertical_strategy_name(leg, other)
                    if not strategy_name:
                        continue
                    low_leg, high_leg = sorted((leg, other), key=_strike_sort_key)
                    low_strike = _strike(low_leg)
                    high_strike = _strike(high_leg)
                    if low_strike is None or high_strike is None:
                        continue
                    short_leg = low_leg if _quantity(low_leg) < 0 else high_leg if _quantity(high_leg) < 0 else None
                    long_leg = low_leg if _quantity(low_leg) > 0 else high_leg if _quantity(high_leg) > 0 else None
                    if short_leg is None or long_leg is None:
                        continue
                    candidate = _VerticalCandidate(
                        legs=(leg, other),
                        strategy_name=strategy_name,
                        matched_by="vertical_spread",
                        underlying=underlying,
                        expiry=expiry,
                        right=right,
                        lower_strike=low_strike,
                        higher_strike=high_strike,
                        quantity=abs(_quantity(leg)),
                        short_strike=_strike(short_leg) or 0.0,
                        long_strike=_strike(long_leg) or 0.0,
                    )
                    if best is None or (candidate.higher_strike - candidate.lower_strike) < (best.higher_strike - best.lower_strike):
                        best = candidate
                if best is not None:
                    candidates.append(best)
                    local_used.update(id(leg) for leg in best.legs)
        return candidates

    def _pair_iron_structures(
        self,
        *,
        underlying: str,
        verticals: Sequence[_VerticalCandidate],
    ) -> tuple[list[StrategyGroup], set[int]]:
        groups: list[StrategyGroup] = []
        consumed: set[int] = set()
        put_spreads = [candidate for candidate in verticals if candidate.right == "P"]
        call_spreads = [candidate for candidate in verticals if candidate.right == "C"]

        for put_spread in put_spreads:
            if id(put_spread) in consumed:
                continue
            if not put_spread.short_strike > put_spread.long_strike:
                continue
            best_call: _VerticalCandidate | None = None
            for call_spread in call_spreads:
                if id(call_spread) in consumed:
                    continue
                if call_spread.expiry != put_spread.expiry:
                    continue
                if abs(call_spread.quantity - put_spread.quantity) > 1e-9:
                    continue
                if not call_spread.short_strike < call_spread.long_strike:
                    continue
                if put_spread.higher_strike > call_spread.lower_strike:
                    continue
                best_call = call_spread
                break
            if best_call is None:
                continue
            legs = [*put_spread.legs, *best_call.legs]
            shared_middle = abs(put_spread.higher_strike - best_call.lower_strike) < 1e-9
            groups.append(self._build_group(
                underlying=underlying,
                strategy_name="Iron Butterfly" if shared_middle else "Iron Condor",
                legs=legs,
                matched_by="iron_butterfly" if shared_middle else "iron_condor",
                strategy_family="iron",
            ))
            consumed.add(id(put_spread))
            consumed.add(id(best_call))
        return groups, consumed

    def _pair_jade_lizards(
        self,
        *,
        underlying: str,
        expiry: str | None,
        verticals: Sequence[_VerticalCandidate],
        legs: Sequence[Any],
        used: set[int],
    ) -> tuple[list[StrategyGroup], set[int]]:
        groups: list[StrategyGroup] = []
        consumed_verticals: set[int] = set()
        short_puts = [
            leg for leg in legs
            if id(leg) not in used and _right(leg) == "P" and _quantity(leg) < 0 and (_expiry(leg) or "") == (expiry or "")
        ]

        for vertical in verticals:
            if id(vertical) in consumed_verticals:
                continue
            if vertical.right != "C" or vertical.strategy_name != "Bear Call Spread":
                continue
            best_put = next(
                (
                    leg for leg in short_puts
                    if id(leg) not in used
                    and abs(abs(_quantity(leg)) - vertical.quantity) < 1e-9
                    and (_strike(leg) or 0.0) < vertical.lower_strike
                ),
                None,
            )
            if best_put is None:
                continue
            groups.append(self._build_group(
                underlying=underlying,
                strategy_name="Jade Lizard",
                legs=[best_put, *vertical.legs],
                matched_by="jade_lizard",
                strategy_family="jade_lizard",
            ))
            used.add(id(best_put))
            consumed_verticals.add(id(vertical))
        return groups, consumed_verticals

    def _extract_same_expiry_pairs(self, *, underlying: str, legs: Sequence[Any], used: set[int]) -> list[StrategyGroup]:
        groups: list[StrategyGroup] = []
        by_expiry: dict[str, list[Any]] = defaultdict(list)
        for leg in legs:
            if id(leg) in used:
                continue
            by_expiry[_expiry(leg) or ""].append(leg)

        for expiry, expiry_legs in sorted(by_expiry.items()):
            calls = sorted([leg for leg in expiry_legs if _right(leg) == "C"], key=_strike_sort_key)
            puts = sorted([leg for leg in expiry_legs if _right(leg) == "P"], key=_strike_sort_key)
            local_used: set[int] = set()
            for call in calls:
                if id(call) in used or id(call) in local_used:
                    continue
                match = next(
                    (
                        put for put in puts
                        if id(put) not in used
                        and id(put) not in local_used
                        and _same_sign(call, put)
                        and _same_abs_quantity(call, put)
                    ),
                    None,
                )
                if match is None:
                    continue
                same_strike = abs((_strike(call) or 0.0) - (_strike(match) or 0.0)) < 1e-9
                side = "Short" if _quantity(call) < 0 else "Long"
                strategy_name = f"{side} {'Straddle' if same_strike else 'Strangle'}"
                groups.append(self._build_group(
                    underlying=underlying,
                    strategy_name=strategy_name,
                    legs=[call, match],
                    matched_by="straddle" if same_strike else "strangle",
                    strategy_family="volatility",
                ))
                local_used.add(id(call))
                local_used.add(id(match))
                used.add(id(call))
                used.add(id(match))
        return groups

    def _extract_cross_expiry_groups(
        self,
        *,
        underlying: str,
        option_rows: Sequence[Any],
        used: set[int],
    ) -> list[StrategyGroup]:
        groups: list[StrategyGroup] = []
        by_right: dict[str, list[Any]] = defaultdict(list)
        for leg in option_rows:
            if id(leg) in used:
                continue
            by_right[_right(leg)].append(leg)

        for right, right_legs in by_right.items():
            by_strike: dict[float, list[Any]] = defaultdict(list)
            for leg in right_legs:
                strike = _strike(leg)
                if strike is None:
                    continue
                by_strike[strike].append(leg)

            for strike, strike_legs in sorted(by_strike.items()):
                ordered = sorted(strike_legs, key=lambda leg: (_expiry_sort_key(_expiry(leg)), abs(_quantity(leg))))
                local_used: set[int] = set()
                for idx, leg in enumerate(ordered):
                    if id(leg) in used or id(leg) in local_used:
                        continue
                    match = next(
                        (
                            other for other in ordered[idx + 1:]
                            if id(other) not in used
                            and id(other) not in local_used
                            and _is_balanced_pair(leg, other)
                            and _expiry(leg) != _expiry(other)
                        ),
                        None,
                    )
                    if match is None:
                        continue
                    groups.append(self._build_group(
                        underlying=underlying,
                        strategy_name="Calendar Spread",
                        legs=[leg, match],
                        matched_by="calendar_spread",
                        strategy_family="calendar",
                    ))
                    local_used.add(id(leg))
                    local_used.add(id(match))
                    used.add(id(leg))
                    used.add(id(match))

            remaining = [leg for leg in right_legs if id(leg) not in used]
            ordered = sorted(remaining, key=lambda leg: (_expiry_sort_key(_expiry(leg)), _strike(leg) or 0.0))
            local_used = set()
            for idx, leg in enumerate(ordered):
                if id(leg) in used or id(leg) in local_used:
                    continue
                best = next(
                    (
                        other for other in ordered[idx + 1:]
                        if id(other) not in used
                        and id(other) not in local_used
                        and _is_balanced_pair(leg, other)
                        and _expiry(leg) != _expiry(other)
                        and _strike(leg) != _strike(other)
                    ),
                    None,
                )
                if best is None:
                    continue
                groups.append(self._build_group(
                    underlying=underlying,
                    strategy_name="Diagonal Spread",
                    legs=[leg, best],
                    matched_by="diagonal_spread",
                    strategy_family="diagonal",
                ))
                local_used.add(id(leg))
                local_used.add(id(best))
                used.add(id(leg))
                used.add(id(best))

        return groups

    def _find_collar(self, stock: Any, option_rows: Sequence[Any], used: set[int]) -> list[Any] | None:
        if _quantity(stock) <= 0:
            return None
        long_puts = [
            leg for leg in option_rows
            if id(leg) not in used and _right(leg) == "P" and _quantity(leg) > 0 and _stock_covers_option(stock, leg)
        ]
        short_calls = [
            leg for leg in option_rows
            if id(leg) not in used and _right(leg) == "C" and _quantity(leg) < 0 and _stock_covers_option(stock, leg)
        ]
        for put in sorted(long_puts, key=lambda leg: (_expiry_sort_key(_expiry(leg)), _strike(leg) or 0.0)):
            match = next(
                (
                    call for call in short_calls
                    if _expiry(call) == _expiry(put)
                    and _same_abs_quantity(call, put)
                    and (_strike(put) or 0.0) < (_strike(call) or 0.0)
                ),
                None,
            )
            if match is not None:
                return [put, match]
        return None

    def _find_stock_covered_structure(
        self,
        stock: Any,
        option_rows: Sequence[Any],
        used: set[int],
    ) -> tuple[str, Any, str] | None:
        if _quantity(stock) > 0:
            short_calls = [
                leg for leg in option_rows
                if id(leg) not in used and _right(leg) == "C" and _quantity(leg) < 0 and _stock_covers_option(stock, leg)
            ]
            short_calls.sort(key=lambda leg: (_expiry_sort_key(_expiry(leg)), _strike(leg) or 0.0))
            for call in short_calls:
                if (_strike(call) or 0.0) >= float(getattr(stock, "avg_cost", 0.0) or 0.0):
                    return ("Covered Call", call, "covered_call")
            protective_puts = [
                leg for leg in option_rows
                if id(leg) not in used and _right(leg) == "P" and _quantity(leg) > 0 and _stock_covers_option(stock, leg)
            ]
            protective_puts.sort(key=lambda leg: (_expiry_sort_key(_expiry(leg)), -(_strike(leg) or 0.0)))
            if protective_puts:
                return ("Protective Put", protective_puts[0], "protective_put")
        elif _quantity(stock) < 0:
            short_puts = [
                leg for leg in option_rows
                if id(leg) not in used and _right(leg) == "P" and _quantity(leg) < 0 and _stock_covers_option(stock, leg)
            ]
            short_puts.sort(key=lambda leg: (_expiry_sort_key(_expiry(leg)), _strike(leg) or 0.0))
            if short_puts:
                return ("Covered Put", short_puts[0], "covered_put")
        return None

    def _build_group(
        self,
        *,
        underlying: str,
        strategy_name: str,
        legs: Sequence[Any],
        matched_by: str,
        strategy_family: str | None,
    ) -> StrategyGroup:
        normalized_legs = sorted(list(legs), key=_leg_sort_key)
        association_id = _association_id(
            account_id=self.account_id,
            underlying=underlying,
            strategy_name=strategy_name,
            legs=normalized_legs,
        )
        return StrategyGroup(
            association_id=association_id,
            strategy_name=strategy_name,
            underlying=underlying,
            legs=normalized_legs,
            matched_by=matched_by,
            expiry_label=_expiry_label(normalized_legs),
            strategy_family=strategy_family,
        )


def reconstruct_strategy_groups(positions: Sequence[Any], *, account_id: str | None = None) -> list[StrategyGroup]:
    return StrategyReconstructor(account_id=account_id).reconstruct(positions)


def _group_sort_key(group: StrategyGroup) -> tuple[Any, ...]:
    return (_expiry_sort_key(_group_first_expiry(group)), group.underlying, group.strategy_name, group.association_id)


def _group_first_expiry(group: StrategyGroup) -> str | None:
    expiries = sorted(exp for exp in (_expiry(leg) for leg in group.legs) if exp)
    return expiries[0] if expiries else None


def _leg_sort_key(leg: Any) -> tuple[Any, ...]:
    return (
        _expiry_sort_key(_expiry(leg)),
        _right(leg),
        _strike(leg) if _strike(leg) is not None else float("inf"),
        -abs(_quantity(leg)),
        _sec_type(leg),
        str(getattr(leg, "symbol", "") or ""),
    )


def _association_id(*, account_id: str, underlying: str, strategy_name: str, legs: Iterable[Any]) -> str:
    payload = "|".join(
        f"{getattr(leg, 'conid', 0)}:{_quantity(leg):.8f}:{_expiry(leg) or ''}:{_strike(leg) if _strike(leg) is not None else ''}:{_right(leg)}:{_sec_type(leg)}"
        for leg in legs
    )
    seed = f"{account_id}|{underlying}|{strategy_name}|{payload}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]


def _combo_strategy_name(leg: Any) -> str:
    combo = str(getattr(leg, "combo_description", "") or "").strip()
    return combo or "Broker Combo"


def _underlying(leg: Any) -> str:
    value = getattr(leg, "underlying", None) or getattr(leg, "symbol", "") or "UNKNOWN"
    return str(value).upper()


def _sec_type(leg: Any) -> str:
    return str(getattr(leg, "sec_type", "") or "").upper()


def _right(leg: Any) -> str:
    return str(getattr(leg, "right", "") or "").upper()


def _strike(leg: Any) -> float | None:
    value = getattr(leg, "strike", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strike_sort_key(leg: Any) -> float:
    strike = _strike(leg)
    return strike if strike is not None else float("inf")


def _quantity(leg: Any) -> float:
    try:
        return float(getattr(leg, "quantity", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _expiry(leg: Any) -> str | None:
    value = getattr(leg, "expiry", None)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    digits = text.replace("-", "")
    return digits[:8] if len(digits) >= 8 else text


def _expiry_sort_key(expiry: str | None) -> tuple[int, str]:
    return (0, expiry) if expiry else (1, "99999999")


def _same_expiry(legs: Sequence[Any]) -> bool:
    expiries = {_expiry(leg) for leg in legs}
    return len(expiries) == 1


def _same_sign(left: Any, right: Any) -> bool:
    return _quantity(left) * _quantity(right) > 0


def _same_abs_quantity(left: Any, right: Any) -> bool:
    return abs(abs(_quantity(left)) - abs(_quantity(right))) < 1e-9


def _is_balanced_pair(left: Any, right: Any) -> bool:
    return _same_abs_quantity(left, right) and (_quantity(left) * _quantity(right) < 0)


def _is_butterfly_ratio(legs: Sequence[Any]) -> bool:
    ordered = sorted(legs, key=_strike_sort_key)
    qtys = [abs(_quantity(leg)) for leg in ordered]
    if min(qtys) <= 0:
        return False
    smallest = min(qtys)
    normalized = [round(qty / smallest, 6) for qty in qtys]
    signs = [1 if _quantity(leg) > 0 else -1 for leg in ordered]
    return (normalized == [1.0, 2.0, 1.0] and signs in ([1, -1, 1], [-1, 1, -1]))


def _vertical_strategy_name(left: Any, right: Any) -> str | None:
    if _right(left) != _right(right) or _expiry(left) != _expiry(right) or not _is_balanced_pair(left, right):
        return None
    low_leg, high_leg = sorted((left, right), key=_strike_sort_key)
    low_qty = _quantity(low_leg)
    high_qty = _quantity(high_leg)
    if _right(low_leg) == "C":
        if low_qty > 0 and high_qty < 0:
            return "Bull Call Spread"
        if low_qty < 0 and high_qty > 0:
            return "Bear Call Spread"
    if _right(low_leg) == "P":
        if low_qty > 0 and high_qty < 0:
            return "Bear Put Spread"
        if low_qty < 0 and high_qty > 0:
            return "Bull Put Spread"
    return None


def _expiry_label(legs: Sequence[Any]) -> str:
    expiries = sorted(exp for exp in (_expiry(leg) for leg in legs) if exp)
    if not expiries:
        return "—"
    if len(set(expiries)) == 1:
        return _fmt_exp(expiries[0])
    return f"{_fmt_exp(expiries[0])}–{_fmt_exp(expiries[-1])}"


def _fmt_exp(expiry: str) -> str:
    import calendar

    digits = (expiry or "").replace("-", "")
    if len(digits) < 8 or not digits[:8].isdigit():
        return expiry
    year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
    return f"{calendar.month_abbr[month]}-{day:02d}-{str(year)[2:]}"


def _right_label(right: str) -> str:
    return {"C": "Call", "P": "Put"}.get((right or "").upper(), "")


def _stock_covers_option(stock: Any, option: Any) -> bool:
    return abs(_quantity(stock)) >= abs(_quantity(option)) * 100.0

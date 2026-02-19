"""One-shot debug script to test full Greeks fetch for account U19664833."""
import asyncio
import sys
import logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, '.')

from ibkr_portfolio_client import IBKRClient
from adapters.ibkr_adapter import IBKRAdapter


async def main():
    client = IBKRClient()
    adapter = IBKRAdapter(client)
    adapter.force_refresh_on_miss = True
    positions = await adapter.fetch_positions("U19664833")
    positions = await adapter.fetch_greeks(positions)

    opts = [p for p in positions if hasattr(p, 'instrument_type') and str(p.instrument_type).endswith("OPTION")]
    print(f"Options: {len(opts)}")
    total_theta = total_vega = total_gamma = 0.0
    missing = []
    for p in opts:
        src = getattr(p, 'greeks_source', 'none')
        th = float(getattr(p, 'theta', 0) or 0)
        ve = float(getattr(p, 'vega', 0) or 0)
        ga = float(getattr(p, 'gamma', 0) or 0)
        de = float(getattr(p, 'delta', 0) or 0)
        qty = float(getattr(p, 'quantity', 1) or 1)
        total_theta += th * qty
        total_vega += ve * qty
        total_gamma += ga * qty
        sym_short = (p.symbol or '')[:40]
        print(f"  {sym_short:<40}  src={src:<20}  d={de:.3f}  th={th:.3f}")
        if th == 0 and src not in ('ibkr_native', 'skipped'):
            missing.append({
                'symbol': p.symbol,
                'src': src,
                'und': p.underlying,
                'exp': str(p.expiration),
                'strike': p.strike,
                'type': p.option_type,
            })

    print()
    print(f"Totals: Theta={total_theta:.2f}  Vega={total_vega:.2f}  Gamma={total_gamma:.4f}")

    if missing:
        print(f"\nStill 0 Greeks ({len(missing)} options):")
        for m in missing:
            print(f"  sym={m['symbol']!r}  src={m['src']}  und={m['und']}  exp={m['exp']}  strike={m['strike']}  type={m['type']}")
    else:
        print("\nAll options have Greeks!")

    # Also check Tastytrade /MES chain expiry dates
    print("\nTop /MES expirations from Tastytrade cache:")
    cache = client.options_cache
    mes_entries = {k: v for k, v in cache._cache.items() if '/MES' in k or 'MES' in k}
    seen_expiries = set()
    for key, entry in list(mes_entries.items())[:20]:
        exp = entry.data.expiration if hasattr(entry, 'data') else None
        if exp and exp not in seen_expiries:
            seen_expiries.add(exp)
            print(f"  {key[:60]}  expiry={exp}")


if __name__ == '__main__':
    asyncio.run(main())

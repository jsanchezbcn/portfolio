"""Quick test to verify options chain fetch works from command line."""
import asyncio
import os
import sys

sys.path.insert(0, ".")

# Load .env manually
with open(".env") as _f:
    for _line in _f:
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _k = _k.strip()
            _v = _v.strip().strip("\"'")
            if "  #" in _v:
                _v = _v[: _v.index("  #")].strip()
            os.environ[_k] = _v

print("IB_API_MODE:", os.environ.get("IB_API_MODE"))
print("IB_CHAIN_CLIENT_ID:", os.environ.get("IB_CHAIN_CLIENT_ID"))
print("IB_SOCKET_PORT:", os.environ.get("IB_SOCKET_PORT"))


async def test():
    from adapters.ibkr_adapter import IBKRAdapter  # noqa: PLC0415

    adapter = IBKRAdapter()

    print("\n--- Testing fetch_option_expirations_tws for SPX (3-15 DTE) ---")
    rows = await adapter.fetch_option_expirations_tws("SPX", dte_min=3, dte_max=15)
    print(f"Got {len(rows)} expirations")
    if not rows:
        print("ERROR: No expirations returned!")
        return

    # Pick a ~7D expiry
    target = next((r for r in rows if 5 <= int(r.get("dte", 0)) <= 10), rows[0])
    exp = str(target["expiry"])
    print(f"Testing chain for expiry: {exp} ({target['dte']}D)")

    print(f"\n--- Testing fetch_option_chain_matrix_tws for SPX {exp} ---")
    chain = await adapter.fetch_option_chain_matrix_tws(
        "SPX", exp, atm_price=5900.0, strikes_each_side=3
    )
    print(f"Got {len(chain)} rows")
    if chain:
        print("Sample rows:")
        for row in chain[:4]:
            print(f"  {row['right']} {row['strike']:.0f}: bid={row['bid']:.2f} ask={row['ask']:.2f} mid={row['mid']:.2f} delta={row['delta']:.3f}")
    else:
        print("ERROR: Empty chain returned!")


asyncio.run(test())

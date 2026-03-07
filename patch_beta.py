import re

file_path = "/Users/jsanchez/PycharmProjects/portfolioIBKR/desktop/engine/ib_engine.py"

with open(file_path, "r") as f:
    text = f.read()

beta_func = """
    async def _fetch_dynamic_beta(self, symbol: str) -> None:
        if not symbol or symbol.upper() in self._symbol_betas:
            return
        # default to config until we prove we got a valid one
        self._symbol_betas[symbol.upper()] = self._beta_default
        try:
            from bs4 import BeautifulSoup
            from ib_async import Stock
            stock = Stock(symbol, "SMART", "USD")
            xml_data = await asyncio.wait_for(
                self._ib.reqFundamentalDataAsync(stock, "ReportSnapshot"),
                timeout=5
            )
            if xml_data:
                soup = BeautifulSoup(xml_data, "xml")
                beta_tag = soup.find("Ratio", {"FieldName": "BETA"})
                if beta_tag and beta_tag.text:
                    self._symbol_betas[symbol.upper()]                    self._sy       ept Ex                    self._symbol_betas[symbol.upper()]                    self._sy    {ex                    self._symbf,     ol: str) -> float:
"""

text = ttext = ttext = ttext = ttext = ttext = ttext = ttext =  float:text = ttext = ttext = ttext = ttext = ttext = ttext = ttext xytext = ttext = ttlftext = ttext = ttext = ttext = ttext = ttext = ttext = ts
                                                               s if            secType in ("STK", "OPT", "FOP", "FUT")}
        await asyncio.gather(*[self._fetch_dynamic_beta(s) for s in unique_stocks])

        # ── Step 1: Request portfolio PnL to get unrealized/realized PnL per contract
"""

text = text.replace("""        spx_proxy_price = await self._spx_proxy_price_async()

        # ── Step 1: Request portfolio PnL to get unrealized/realized PnL per contract""", refresh_code.strip("\n"))

# Now replace the part where OPT spx_delta is calculated
old_delta_logic = """
            if c.secType == "STK":
                ref_price = (
                    float(mkt_price or 0.0)
                    or float(pos.avgCost or 0.0)
                    or float(getattr(c, "lastTradePrice", 0.0) or 0.0)
                )
                spx_delta = self._compute_spx_weighted_delta(
                    symbol=c.symbol,
                    quantity=float(pos.position),
                    price=ref_price,
                    underlying_delta=1.0,
                    multiplier=1.0,
                    spx_proxy_price=spx_proxy                    spx_proxy_price=spx_proxy                    spx_proxy_price=slta = float(pos.position)
            elif c.secType == "FUT":
                # Futures have delta                # Futures have delta                # Futures have delta           ble for index futures; unknown symbols get 0 (non-SPX).
                qty = float(pos.position)
                raw_mult = float(c.multiplier or 1)
                spx_mult = _FUT_SPX_MULTIPLIERS.get(c.symbol, 0.0)
                delta                 delta       de                delta                                       delta                 delta     e                delta                 delta       de                d_d              ""
            if c.secType == "STK":
                ref_price =                 ref_price =      rice                ref_price =                 gCost                 ref_        or float(getattr(c, "lastTradePrice", 0.0) or 0.0)
                )
                sp                sp                sp                sp                sp                sp                sp                sp                s        pr                sp                 un                sp                sp                sp                sp                sp                sp                sp                sp                s        pr                    sp                sp          elif c.secType == "FUT":
                qty = f                qty = f                qty = f                qtyor                qty = f                qty = f                qty = f                qtyor                qty = f                qty = f                qty = f                qtyor                qty = f                qty = f                       qty = fice to get t                qty = f                qty = f                qty = f                qtyor                qty = f                qty = f                qty = f                qtyor           sition)
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 _proxy_price,
                    )
                else:
                    spx_delta = delta
            else:
                spx_delta = delta
"""
text = text.replace(old_delta_logic, "\n" + new_delta_logic + "\n")

with open(file_path, "w") as f:
    f.write(text)

print("Patched!")

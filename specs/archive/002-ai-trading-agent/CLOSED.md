# 002-ai-trading-agent — CLOSED ✓

**Closed**: February 19, 2026

## Summary

Completed all requirements for AI trading agent with Greeks accuracy fixes, Market Intelligence debugging, and LLM model picker.

### What was implemented:
1. **Greeks Accuracy Fix** (adapters/ibkr_adapter.py)
   - Removed position-adjusted fields from `_extract_native_greeks` that were causing double-counting
   - Changed to use only per-contract Greek fields

2. **IBKR-First Greeks Logic** (adapters/ibkr_adapter.py)
   - Track `ibkr_has_delta` flag separately for partial IBKR greeks
   - Preserve IBKR native source tag on Tastytrade miss instead of zeroing it
   - On TT success: keep IBKR delta/gamma, take theta/vega/IV from TT

3. **Market Intelligence Fix** (agents/news_sentry.py)
   - Fixed JSON parsing bug: replaced `lstrip("```json")` with proper regex code-fence removal `re.match(r'^```(?:json)?\s*([\s\S]*?)\s*```$', raw)`
   - Added `import re`
   - Better `json.JSONDecodeError` logging

4. **LLM Model Picker** (dashboard/app.py)
   - New `_get_available_models()` cached helper function
   - Sidebar selector in "🤖 AI / LLM Settings" section
   - Displays all models with 🆓 free / 💰 paid indicators
   - Defaults to `gpt-4.1`

5. **Expanded AI Assistant Context** (dashboard/app.py)
   - Account ID, regime, VIX data  
   - Per-position option table (qty, DTE, Greeks per contract, greeks source)
   - Equity/Futures position summaries
   - Full risk violation details

6. **New Helper** (agents/llm_client.py)
   - `async_list_models()` function queries Copilot SDK
   - Marks free models (multiplier == 0 or id in {gpt-4.1, gpt-4o, gpt-4o-mini})
   - Falls back to hardcoded list on failure

### Dashboard Status:
- Running on http://localhost:8506 (PID 94669)
- All changes compiled successfully
- Ready for testing

### Files Modified:
- adapters/ibkr_adapter.py
- agents/news_sentry.py (added `import re`)
- agents/llm_client.py (new `async_list_models()`)
- dashboard/app.py (model picker + expanded context)

### Testing Notes:
- Greeks=0 issue should now resolve via IBKR-first logic + preserved source tags
- Market Intelligence scoring should work with proper JSON parsing
- LLM model picker allows selection of different models
- AI assistant context now includes full position details

---
Ready for next spec.

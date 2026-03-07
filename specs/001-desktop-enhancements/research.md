# Phase 0 Research: Desktop Client Enhancements

## Decisions

### 1. Copilot profile persistence

- **Decision**: Store the active profile in `desktop/config/prefs.json` and expose it through a small preferences helper module.
- **Rationale**: The selection must survive restarts without storing secrets in the preferences file.
- **Alternatives considered**: Storing the token directly in preferences was rejected because tokens already belong in `.env`.

### 2. Token routing strategy

- **Decision**: The selected profile (`personal` or `work`) maps to `.env` variables `GITHUB_COPILOT_TOKEN_PERSONAL` and `GITHUB_COPILOT_TOKEN_WORK`. Runtime switching updates active environment markers that `agents/llm_client.py` reads for every LLM call.
- **Rationale**: This centralizes token selection and ensures desktop and agent-originated LLM calls all respect the chosen profile.
- **Alternatives considered**: Per-widget token handling was rejected because it would fragment behavior across the app.

### 3. Favorite quote refresh cadence

- **Decision**: Use a one-second `QTimer` in the Market Data tab to request refreshed snapshots for persisted favorites.
- **Rationale**: The existing tab already uses timer-driven refresh; shortening the interval is low-risk and easy to test.
- **Alternatives considered**: Full streaming for favorites was deferred because the current engine already exposes reliable snapshot fetching.

### 4. Options-chain streaming path

- **Decision**: Use existing IBEngine live chain tickers for the active chain first, and only fall back to a full chain refresh when live ticker data is unavailable.
- **Rationale**: This resolves the previous ambiguity around streaming and keeps the implementation consistent with current engine behavior.
- **Alternatives considered**: Adding a new external streaming adapter for this feature was rejected because the desktop app already has an IB-native chain streaming path.

### 5. Expiry-change clearing

- **Decision**: Clear the chain model immediately on expiry change before the async fetch starts, and cancel existing chain streaming subscriptions.
- **Rationale**: This prevents stale rows from lingering while network work continues in the background.
- **Alternatives considered**: Waiting for the fetch to finish before clearing was rejected because it is exactly the stale-data bug the user reported.

### 6. Position action menus

- **Decision**: Add a reusable `PositionContextMenu` widget and emit staged order payloads from the Portfolio tab to the Main Window.
- **Rationale**: This preserves separation between the portfolio tables and the order-entry dock.
- **Alternatives considered**: Direct Order Entry mutation inside the table view was rejected because it would tightly couple the tab to the dock.

### 7. Strategy risk overlays

- **Decision**: Even before full autonomous execution is built, the Strategies tab must show Taleb gamma warnings and Sebastian theta/vega ratio state.
- **Rationale**: These are constitution requirements, not optional polish.
- **Alternatives considered**: Deferring risk overlays to a later milestone was rejected because it would leave the strategy work inconsistent with project rules.

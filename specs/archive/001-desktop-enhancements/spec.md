# Feature Specification: Desktop Client Enhancements

**Feature Branch**: `001-desktop-enhancements`  
**Created**: March 3, 2026  
**Status**: Draft  
**Input**: User description: "Improve the desktop client to better mimic IBKR workflows, add position action menus, refresh and stream option-chain data correctly, support Personal/Work Copilot tokens, improve the trades view with GPT-5 mini and tool access, add favorite market symbols with 1-second updates, and add a strategies tab for agent-managed trades such as 0DTE with stop loss and profit taking."

## User Scenarios & Testing _(mandatory)_

### User Story 1 - Actionable Position Menus (Priority: P1)

Traders need to act on positions directly from the portfolio tables without manually rebuilding orders in the order-entry dock.

**Why this priority**: This is core trading workflow parity with IBKR and reduces order-entry mistakes.

**Independent Test**: Open either the Raw or Trades portfolio view, invoke the position menu, choose an action, and confirm the Order Entry panel is populated correctly.

**Acceptance Scenarios**:

1. **Given** a stock position in Raw or Trades view, **When** the user opens the position menu, **Then** the menu shows `Buy` and `Sell`.
2. **Given** a single-leg option or multi-leg options trade in Raw or Trades view, **When** the user opens the position menu, **Then** the menu shows `Buy`, `Sell`, and `Roll`.
3. **Given** the user chooses an action from the menu, **When** the action is triggered, **Then** the Order Entry panel is staged with the correct symbol, side, quantity, and legs.

---

### User Story 2 - Favorites With Rapid Market Refresh (Priority: P1)

Traders need a persistent favorites list in Market Data so they can monitor key tickers with one-second quote refreshes.

**Why this priority**: Fast quote awareness directly affects execution quality and trade timing.

**Independent Test**: Add a symbol to favorites, restart the desktop client, and verify the favorite persists and refreshes every second while connected.

**Acceptance Scenarios**:

1. **Given** a quoted symbol in Market Data, **When** the user adds it to favorites, **Then** it appears in a persistent favorites list.
2. **Given** there are favorites and the engine is connected, **When** one second passes, **Then** the tab refreshes favorite quotes without user interaction.

---

### User Story 3 - Clean Options Chain Expiry Changes (Priority: P2)

Traders need option-chain data to clear immediately when they change expiration so stale rows do not remain visible while new data loads.

**Why this priority**: Stale chain rows can lead to bad orders and poor trust in the UI.

**Independent Test**: Load a chain, change expiry, verify the table clears immediately, then confirm the new expiry data repopulates and resumes live updates.

**Acceptance Scenarios**:

1. **Given** an existing options chain is visible, **When** the user changes the selected expiry, **Then** the chain table clears before any new rows appear.
2. **Given** the new expiry is loaded, **When** live market data arrives, **Then** the displayed bid/ask values update for that expiry only.

---

### User Story 4 - Personal/Work Copilot Profile Picker (Priority: P2)

Users need to choose whether the desktop client uses their Personal or Work Copilot token so all LLM-powered features use the intended account.

**Why this priority**: This is required for account separation, billing control, and predictable behavior.

**Independent Test**: Switch from Personal to Work in the picker and confirm subsequent LLM calls use the Work token without restarting the app.

**Acceptance Scenarios**:

1. **Given** the desktop app is open, **When** the user changes the Copilot profile picker, **Then** the selected profile is saved and immediately becomes the active LLM profile.
2. **Given** a selected profile has an associated token in `.env`, **When** any LLM-assisted feature runs, **Then** it uses that profile's token.

---

### User Story 5 - Better Trades View With GPT-5 mini (Priority: P3)

Users need the Trades view to produce a clearer, more contextual explanation of current trades using GPT-5 mini and the available trading tools.

**Why this priority**: The existing view is hard to interpret and does not convert portfolio state into actionable insight.

**Independent Test**: Open the Trades view and verify the generated summary uses GPT-5 mini and includes information sourced from trading tools and portfolio context.

**Acceptance Scenarios**:

1. **Given** the Trades view is active, **When** the LLM summary is requested, **Then** GPT-5 mini is used.
2. **Given** the summary needs supporting context, **When** the LLM runs, **Then** portfolio and market-data tool outputs are available to the prompt/session.

---

### User Story 6 - Agent-Managed Strategies Tab (Priority: P3)

Users need a dedicated Strategies tab for running automated strategies such as 0DTE setups with stop-loss, profit-taking, and risk warnings.

**Why this priority**: It creates a safe and structured place to operate agent-assisted strategies directly in the desktop client.

**Independent Test**: Start a 0DTE strategy with defined stop-loss and take-profit settings and confirm the strategy state, alerts, and recommendation output update in the tab.

**Acceptance Scenarios**:

1. **Given** a 0DTE strategy is configured, **When** it is started, **Then** the tab shows that it is running and monitoring the trade.
2. **Given** strategy risk becomes elevated near expiration, **When** gamma exposure exceeds the configured threshold, **Then** the tab shows a Taleb-style gamma warning.
3. **Given** the strategy has theta and vega exposure, **When** the strategy summary updates, **Then** it shows the theta/vega ratio and whether it is inside or outside the target band.

### Edge Cases

- What happens when the selected Copilot profile has no token in `.env`?
- What happens when a user opens the context menu on a synthetic/group header row instead of a tradable row?
- What happens when an expiry is changed while previous chain streaming updates are still arriving?
- What happens when a complex options trade is partially closed and the user requests `Roll`?

## Requirements _(mandatory)_

### Functional Requirements

- **FR-001**: The system MUST provide a position action menu in both Raw and Trades portfolio views.
- **FR-002**: The position action menu MUST show `Buy` and `Sell` for stocks and futures.
- **FR-003**: The position action menu MUST show `Buy`, `Sell`, and `Roll` for single-leg and multi-leg option trades.
- **FR-004**: Selecting a position action MUST prefill the Order Entry panel with the correct contract or legs.
- **FR-005**: The Market Data tab MUST allow symbols to be added to and removed from a persistent favorites list.
- **FR-006**: Favorite symbols MUST refresh at a one-second cadence while the client is connected.
- **FR-007**: Changing the options-chain expiry MUST immediately clear the visible chain rows before loading replacement data.
- **FR-008**: Options-chain live updates MUST be tied only to the currently selected expiry and current chain subscription.
- **FR-009**: The desktop client MUST provide a Personal/Work Copilot profile picker.
- **FR-010**: The active profile MUST determine which token from `.env` is used for all LLM calls.
- **FR-011**: The Trades view MUST use GPT-5 mini for its LLM-generated summary.
- **FR-012**: The Trades view prompt/session MUST include richer portfolio context and access to trading tools.
- **FR-013**: The desktop client MUST provide a Strategies tab for agent-managed strategies.
- **FR-014**: Strategies MUST support stop-loss and profit-taking configuration.
- **FR-015**: Strategies MUST show Taleb-style high-gamma warnings for 0-7 DTE exposure when absolute gamma exceeds the threshold.
- **FR-016**: Strategies MUST show the Sebastian theta/vega ratio and whether the ratio is inside the target band.

### Key Entities _(include if feature involves data)_

- **Copilot Profile**: The selected LLM account context (`personal` or `work`) plus the token environment variable that should be used.
- **Favorite Symbol**: A persisted watched symbol entry that includes symbol, security type, exchange, and last refresh timestamp.
- **Position Action**: A user-triggered trading action (`Buy`, `Sell`, `Roll`) derived from a raw position or grouped trade.
- **Strategy Session**: The active configuration and live state for an automated strategy, including stop-loss, take-profit, gamma state, and theta/vega ratio.

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: Users can trigger `Buy`, `Sell`, or `Roll` from the portfolio tables and see the Order Entry panel staged in under 2 seconds.
- **SC-002**: Favorite symbols refresh every 1 second ± 250ms while the desktop client is connected.
- **SC-003**: Options-chain rows clear within 100ms of an expiry change and do not show stale data from the previously selected expiry.
- **SC-004**: Switching the Copilot profile changes the active LLM token without restarting the application.
- **SC-005**: Strategy monitoring shows gamma warnings and theta/vega ratio state for configured 0DTE strategies.

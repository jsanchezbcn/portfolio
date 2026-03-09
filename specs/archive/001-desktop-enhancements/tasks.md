# Implementation Tasks: Desktop Client Enhancements

**Branch**: `001-desktop-enhancements` | **Date**: March 5, 2026 | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Phase 1: Setup & Foundations

- [x] T001 Create `desktop/config/prefs.json` with default `personal` Copilot profile
- [x] T002 Create `desktop/config/preferences.py` for loading and saving desktop preferences
- [x] T003 Create `desktop/engine/token_manager.py` to resolve and switch the active Personal/Work token
- [x] T004 Update `agents/llm_client.py` so every LLM call uses the active profile token when present
- [x] T005 Update `desktop/main.py` to initialize the active Copilot profile and token at startup
- [x] T006 Add unit tests for preferences, token management, and active-token LLM routing

## Phase 2: User Story 4 — Personal/Work Copilot Profile Picker

- [x] T007 Create `desktop/ui/widgets/account_picker.py`
- [x] T008 Add the Copilot profile picker to the main window toolbar
- [x] T009 Connect picker changes to `TokenManager` and persist the selected profile
- [x] T010 Add desktop UI tests for the account picker and toolbar integration

## Phase 3: User Story 1 — Actionable Position Menus

- [x] T011 Create `desktop/ui/widgets/position_menu.py` with `Buy`, `Sell`, and `Roll` actions
- [x] T012 Add row lookup helpers to portfolio table models for raw and trades views
- [x] T013 Emit position action requests from `desktop/ui/portfolio_tab.py`
- [x] T014 Handle position action requests in `desktop/ui/main_window.py` and prefill the Order Entry dock
- [x] T015 Add desktop UI tests for stock and options context menu behavior

## Phase 4: User Story 2 — Favorites With 1-Second Refresh

- [x] T016 Create `desktop/models/favorites.py` for persistent favorite symbol records
- [x] T017 Update `desktop/ui/market_tab.py` to load, save, and render persistent favorites
- [x] T018 Change favorite refresh cadence from 60 seconds to 1 second while connected
- [x] T019 Add tests for favorites persistence and one-second refresh timer behavior

## Phase 5: User Story 3 — Clean Options Chain Expiry Changes

- [x] T020 Clear the chain model immediately when expiry changes in `desktop/ui/chain_tab.py`
- [x] T021 Cancel or reset previous chain streaming state when expiry changes
- [x] T022 Keep chain streaming tied to the currently selected expiry using the existing IBEngine live-ticker path
- [x] T023 Add tests for immediate chain clearing and expiry-specific refresh behavior

## Phase 6: User Story 5 — Better Trades View With GPT-5 mini

- [x] T024 Update the trades-view LLM path to use GPT-5 mini by default
- [x] T025 Expand the trades-view prompt context with portfolio state and tool outputs
- [x] T026 Add tests for the new LLM model selection and prompt context builder

## Phase 7: User Story 6 — Agent-Managed Strategies Tab

- [x] T027 Create the initial Strategies tab shell and registration in the desktop window
- [x] T028 Add strategy configuration controls for stop-loss and take-profit
- [x] T029 Add Taleb gamma warning state for 0-7 DTE strategies
- [x] T030 Add Sebastian theta/vega ratio state for strategy summaries
- [x] T031 Add tests for strategy risk-state calculations and presentation helpers

## Final Phase

- [x] T032 Run targeted desktop and unit tests for all completed phases
- [x] T033 Update feature docs and mark completed tasks in this file
## Post-Spec Follow-Ups

- [x] T034 Update WhatIf tool schema docs so the LLM reliably calls `whatif_order`
- [x] T035 Expand the AI model picker to show the full fallback/live model list with cost multipliers
- [x] T036 Query FOP expiries and chain definitions across multiple active futures months

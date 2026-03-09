# Implementation Plan: Desktop Client Enhancements

**Branch**: `001-desktop-enhancements` | **Date**: March 5, 2026 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-desktop-enhancements/spec.md`

## Summary

Improve the PySide6 desktop client to better mirror IBKR workflows by adding actionable position menus, persistent favorite symbols with one-second quote refreshes, correct expiry-reset behavior in the options chain, Personal/Work Copilot profile switching, improved GPT-5 mini trade summaries, and the first version of a Strategies tab for agent-managed workflows.

## Technical Context

**Language/Version**: Python 3.12  
**Primary Dependencies**: PySide6, qasync, python-dotenv, ib_async, Copilot SDK / `copilot` CLI, pytest, pytest-qt  
**Storage**: Local JSON preferences plus `.env` for Personal/Work tokens  
**Testing**: pytest, pytest-qt, existing desktop test suite  
**Target Platform**: macOS desktop (PySide6 application)  
**Project Type**: single desktop application  
**Performance Goals**: one-second favorite quote refresh; sub-100ms chain reset; no UI freezes during token switching or chain refreshes  
**Constraints**: preserve current IBEngine async patterns; never commit secrets; add tests alongside business logic; keep all UI work inside existing `desktop/` structure  
**Scale/Scope**: one desktop application with focused updates to tabs, models, and LLM integration

## Constitution Check

_GATE: Must pass before Phase 0 research. Re-check after Phase 1 design._

- **Test-First Development**: All new business logic must land with tests in `desktop/tests/` or `tests/`.
- **Adapter Pattern**: Quote streaming must reuse existing engine/adapter paths rather than bypass them with ad-hoc broker logic.
- **Trading Literature Principles**: Strategy work must explicitly surface Taleb gamma warnings and Sebastian theta/vega ratio states.
- **Security Requirements**: Tokens live in `.env`; no token values in committed files; runtime switching should only use environment state and local preferences.

## Project Structure

### Documentation (this feature)

```text
specs/001-desktop-enhancements/
├── plan.md
├── research.md
├── tasks.md
└── checklists/
```

### Source Code (repository root)

```text
desktop/
├── config/
│   ├── prefs.json
│   └── preferences.py
├── engine/
│   ├── ib_engine.py
│   └── token_manager.py
├── models/
│   ├── favorites.py
│   ├── table_models.py
│   └── trade_groups.py
├── ui/
│   ├── main_window.py
│   ├── portfolio_tab.py
│   ├── market_tab.py
│   ├── chain_tab.py
│   └── widgets/
│       ├── account_picker.py
│       └── position_menu.py
└── tests/
    ├── test_main_window.py
    ├── test_market_tab.py
    ├── test_chain_tab.py
    ├── test_account_picker.py
    └── test_token_manager.py

agents/
└── llm_client.py

tests/
└── test_llm_client.py
```

**Structure Decision**: Keep the feature inside the current desktop app architecture. UI widgets stay under `desktop/ui`, non-UI preference/token logic lives in `desktop/config` and `desktop/engine`, and shared LLM token routing remains centralized in `agents/llm_client.py`.

## Complexity Tracking

| Violation                 | Why Needed                                                   | Simpler Alternative Rejected Because                                                        |
| ------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| Runtime profile switching | Required to separate Personal and Work usage without restart | Editing `.env` manually is error-prone and interrupts workflows                             |
| Strategy risk overlays    | Required by constitution for 0DTE strategy work              | A minimal strategy tab without gamma/theta-vega risk signals would violate the constitution |

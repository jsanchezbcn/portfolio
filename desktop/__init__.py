"""desktop — PySide6 desktop trading application.

Architecture inspired by vnpy: a central Engine connects to IBKR via ib_async,
persists data to PostgreSQL (asyncpg), and exposes signals/slots to a PySide6 UI.
"""

-- ────────────────────────────────────────────────────────────────────────────
-- PostgreSQL schema for the PySide6 desktop trading application
-- Run:  psql -U portfoliouser -d portfoliodb -f desktop/db/schema.sql
-- ────────────────────────────────────────────────────────────────────────────

-- 0. Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ────────────────────────────────────────────────────────────────────────────
-- 1. Positions — live state synced from IBKR
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id              SERIAL PRIMARY KEY,
    account_id      TEXT        NOT NULL,
    conid           BIGINT      NOT NULL,              -- IBKR contract id
    symbol          TEXT        NOT NULL,
    sec_type        TEXT        NOT NULL DEFAULT 'STK', -- STK, OPT, FUT, FOP
    exchange        TEXT,
    currency        TEXT        NOT NULL DEFAULT 'USD',
    underlying      TEXT,                               -- for options / FOP
    strike          DOUBLE PRECISION,
    option_right    CHAR(1),                            -- 'C' or 'P'
    expiry          DATE,
    multiplier      DOUBLE PRECISION DEFAULT 1.0,

-- Position data
quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
avg_cost DOUBLE PRECISION,
market_price DOUBLE PRECISION,
market_value DOUBLE PRECISION,
unrealized_pnl DOUBLE PRECISION,
realized_pnl DOUBLE PRECISION,

-- Greeks (optional, populated by greeks worker)
delta DOUBLE PRECISION,
gamma DOUBLE PRECISION,
theta DOUBLE PRECISION,
vega DOUBLE PRECISION,
iv DOUBLE PRECISION,

-- Beta-weighted Greeks


spx_delta       DOUBLE PRECISION,
    beta            DOUBLE PRECISION,

    synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (account_id, conid)
);

CREATE INDEX IF NOT EXISTS idx_positions_account ON positions (account_id);

CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions (symbol);

-- ────────────────────────────────────────────────────────────────────────────
-- 2. Orders — full lifecycle from DRAFT → FILLED / REJECTED
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id      TEXT        NOT NULL,
    broker_order_id TEXT,                               -- IBKR order ID
    status          TEXT        NOT NULL DEFAULT 'DRAFT',
    order_type      TEXT        NOT NULL DEFAULT 'LIMIT',-- LIMIT, MARKET, MOC
    side            TEXT,                               -- composite: BUY / SELL / COMBO
    limit_price     DOUBLE PRECISION,
    filled_price    DOUBLE PRECISION,

-- Legs (JSONB array of { symbol, action, qty, conid, strike, right, expiry })
legs_json JSONB NOT NULL DEFAULT '[]'::jsonb,

-- Origin
source TEXT, -- e.g. "proposer", "manual", "arb_signal"
rationale TEXT,

-- Risk snapshot at submission time
pre_spx_delta DOUBLE PRECISION,
pre_vega DOUBLE PRECISION,
post_spx_delta DOUBLE PRECISION,
post_vega DOUBLE PRECISION,
margin_impact DOUBLE PRECISION,

-- Timestamps
created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at    TIMESTAMPTZ,
    filled_at       TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_account ON orders (account_id);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (created_at DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 3. Fills — individual fill events (one order can have multiple partial fills)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fills (
    id SERIAL PRIMARY KEY,
    order_id UUID REFERENCES orders (id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    conid BIGINT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL, -- BUY / SELL
    quantity DOUBLE PRECISION NOT NULL,
    fill_price DOUBLE PRECISION NOT NULL,
    commission DOUBLE PRECISION DEFAULT 0.0,
    realized_pnl DOUBLE PRECISION,
    execution_id TEXT, -- IBKR execution ID
    filled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills (order_id);

CREATE INDEX IF NOT EXISTS idx_fills_account ON fills (account_id);

CREATE INDEX IF NOT EXISTS idx_fills_filled ON fills (filled_at DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 4. Account summary — periodic NLV / margin snapshots
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS account_snapshots (
    id SERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    net_liquidation DOUBLE PRECISION,
    total_cash DOUBLE PRECISION,
    buying_power DOUBLE PRECISION,
    init_margin DOUBLE PRECISION,
    maint_margin DOUBLE PRECISION,
    unrealized_pnl DOUBLE PRECISION,
    realized_pnl DOUBLE PRECISION,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_acct_snap_acct ON account_snapshots (account_id);

CREATE INDEX IF NOT EXISTS idx_acct_snap_ts ON account_snapshots (timestamp DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 5. Option chains cache (optional, for offline analysis)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS option_chain_cache (
    id SERIAL PRIMARY KEY,
    underlying TEXT NOT NULL,
    expiry DATE NOT NULL,
    strike DOUBLE PRECISION NOT NULL,
    option_right CHAR(1) NOT NULL, -- 'C' or 'P'
    conid BIGINT,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    last DOUBLE PRECISION,
    volume INTEGER,
    open_interest INTEGER,
    iv DOUBLE PRECISION,
    delta DOUBLE PRECISION,
    gamma DOUBLE PRECISION,
    theta DOUBLE PRECISION,
    vega DOUBLE PRECISION,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        underlying,
        expiry,
        strike,
        option_right
    )
);

CREATE INDEX IF NOT EXISTS idx_chain_underlying ON option_chain_cache (underlying, expiry);

-- ────────────────────────────────────────────────────────────────────────────
-- 6. Available expirations cache (for offline fallback when market is closed)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS available_expirations (
    id SERIAL PRIMARY KEY,
    underlying TEXT NOT NULL,
    sec_type TEXT NOT NULL DEFAULT 'FOP', -- FOP or OPT
    exchange TEXT NOT NULL DEFAULT 'CME',
    expirations TEXT [] NOT NULL, -- Array of YYYYMMDD strings
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        underlying,
        sec_type,
        exchange
    )
);

CREATE INDEX IF NOT EXISTS idx_avail_expir_underlying ON available_expirations (underlying, sec_type);

-- ────────────────────────────────────────────────────────────────────────────
-- 7. Trade journal — human notes on trade logic / post-mortem
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_journal (
    id SERIAL PRIMARY KEY,
    order_id UUID REFERENCES orders (id) ON DELETE SET NULL,
    account_id TEXT,
    title TEXT,
    body TEXT,
    tags TEXT [],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_created ON trade_journal (created_at DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 7. Journal notes — desktop-first discretionary notes / thesis history
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS journal_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    account_id TEXT,
    order_id UUID REFERENCES orders (id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    tags TEXT [] NOT NULL DEFAULT ARRAY[]::TEXT [],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_notes_account_created ON journal_notes (account_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_notes_created ON journal_notes (created_at DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 8. Market intelligence — persisted AI/risk/news summaries
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_intel (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    trade_id UUID,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    sentiment_score DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_intel_symbol_source ON market_intel (
    symbol,
    source,
    created_at DESC
);

-- ────────────────────────────────────────────────────────────────────────────
-- 9. Risk snapshots — periodic portfolio-level risk metrics
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS risk_snapshots (
    id SERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    spx_delta DOUBLE PRECISION,
    gamma DOUBLE PRECISION,
    theta DOUBLE PRECISION,
    vega DOUBLE PRECISION,
    vix DOUBLE PRECISION,
    regime TEXT,
    nlv DOUBLE PRECISION,
    margin_used_pct DOUBLE PRECISION,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_risk_snap_ts ON risk_snapshots (timestamp DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 10. Strategy associations — reconstructed multi-leg strategies
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS strategy_groups (
    association_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_family TEXT,
    underlying TEXT NOT NULL,
    expiry_label TEXT,
    matched_by TEXT,
    leg_count INTEGER NOT NULL DEFAULT 0,
    net_delta DOUBLE PRECISION,
    net_gamma DOUBLE PRECISION,
    net_theta DOUBLE PRECISION,
    net_vega DOUBLE PRECISION,
    net_spx_delta DOUBLE PRECISION,
    market_value DOUBLE PRECISION,
    unrealized_pnl DOUBLE PRECISION,
    realized_pnl DOUBLE PRECISION,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strategy_groups_account ON strategy_groups (
    account_id,
    underlying,
    synced_at DESC
);

CREATE TABLE IF NOT EXISTS strategy_group_legs (
    association_id TEXT NOT NULL REFERENCES strategy_groups (association_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    leg_index INTEGER NOT NULL,
    conid BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    sec_type TEXT NOT NULL,
    underlying TEXT,
    expiry DATE,
    strike DOUBLE PRECISION,
    option_right CHAR(1),
    quantity DOUBLE PRECISION NOT NULL,
    avg_cost DOUBLE PRECISION,
    market_price DOUBLE PRECISION,
    market_value DOUBLE PRECISION,
    unrealized_pnl DOUBLE PRECISION,
    realized_pnl DOUBLE PRECISION,
    delta DOUBLE PRECISION,
    gamma DOUBLE PRECISION,
    theta DOUBLE PRECISION,
    vega DOUBLE PRECISION,
    iv DOUBLE PRECISION,
    spx_delta DOUBLE PRECISION,
    leg_role TEXT,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (association_id, leg_index)
);

CREATE INDEX IF NOT EXISTS idx_strategy_group_legs_account ON strategy_group_legs (
    account_id,
    underlying,
    expiry
);

-- ────────────────────────────────────────────────────────────────────────────
-- 11. Positions cache — minute-by-minute snapshots for LLM tools
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions_cache (
    id SERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL, -- UUID per refresh cycle
    conid BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    sec_type TEXT NOT NULL,
    underlying TEXT,
    expiry DATE,
    strike DOUBLE PRECISION,
    option_right CHAR(1),
    quantity DOUBLE PRECISION NOT NULL,
    avg_cost DOUBLE PRECISION,
    market_price DOUBLE PRECISION,
    market_value DOUBLE PRECISION,
    unrealized_pnl DOUBLE PRECISION,
    realized_pnl DOUBLE PRECISION,
    underlying_price DOUBLE PRECISION,
    delta DOUBLE PRECISION,
    gamma DOUBLE PRECISION,
    theta DOUBLE PRECISION,
    vega DOUBLE PRECISION,
    iv DOUBLE PRECISION,
    spx_delta DOUBLE PRECISION,
    cached_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_positions_cache_account_cached ON positions_cache (account_id, cached_at DESC);

CREATE INDEX IF NOT EXISTS idx_positions_cache_snapshot ON positions_cache (snapshot_id);

CREATE INDEX IF NOT EXISTS idx_positions_cache_expiry ON positions_cache (
    account_id,
    expiry,
    cached_at DESC
);

-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS portfolio_greeks_cache (
    id SERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    total_delta DOUBLE PRECISION,
    total_gamma DOUBLE PRECISION,
    total_theta DOUBLE PRECISION,
    total_vega DOUBLE PRECISION,
    total_spx_delta DOUBLE PRECISION,
    underlying_price DOUBLE PRECISION,
    cached_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_portfolio_greeks_cache_account_cached ON portfolio_greeks_cache (account_id, cached_at DESC);

CREATE TABLE IF NOT EXISTS portfolio_metrics_cache (
    id SERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    total_positions INTEGER,
    total_value DOUBLE PRECISION,
    total_spx_delta DOUBLE PRECISION,
    total_delta DOUBLE PRECISION,
    total_gamma DOUBLE PRECISION,
    total_theta DOUBLE PRECISION,
    total_vega DOUBLE PRECISION,
    theta_vega_ratio DOUBLE PRECISION,
    gross_exposure DOUBLE PRECISION,
    net_exposure DOUBLE PRECISION,
    options_count INTEGER,
    stocks_count INTEGER,
    nlv DOUBLE PRECISION,
    buying_power DOUBLE PRECISION,
    init_margin DOUBLE PRECISION,
    maint_margin DOUBLE PRECISION,
    cached_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_portfolio_metrics_cache_account_cached ON portfolio_metrics_cache (account_id, cached_at DESC);
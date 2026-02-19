# 000: System Manifest & Database Schema

## 1. System Vision
A multi-agent trading environment that tracks "Thesis-to-P&L" performance. 
- **DB Access:** `127.0.0.1:5432` (or your specific IP)
- **Primary Stack:** Python 3.12+, Asyncpg, GitHub Copilot SDK, IBKR TWS API.

## 2. Core DB Schema (PostgreSQL)
### Table: `trade_journal`
| Column | Type | Description |
| :--- | :--- | :--- |
| `trade_id` | UUID (PK) | Unique identifier for the trade group. |
| `status` | Enum | OPEN, CLOSED, CANCELLED. |
| `strategy` | Text | e.g., '1-1-2', 'BOX_ARB', 'FUT_SPEC'. |
| `thesis` | Text | The "Why" behind the trade. |
| `sentiment_at_entry` | Float | AI-generated score (-1 to 1). |
| `entry_greeks` | JSONB | Delta, Gamma, Theta, Vega at time of execution. |

### Table: `market_intel`
- `timestamp`: TIMESTAMPTZ
- `news_summary`: Text (Last 15-30 min summary)
- `market_sentiment`: Float
- `vix_regime`: Enum (LOW, MED, HIGH)

## 3. Global Configuration
- **IP Config:** All services point to `DB_HOST=192.168.1.XX` (as per user preference).
- **Unit Testing:** 100% coverage required for `core/` and `agents/` modules.


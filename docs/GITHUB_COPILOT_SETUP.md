# GitHub Copilot SDK Authentication

## ⚠️ Critical: github.com vs GitHub Enterprise

**GitHub Copilot SDK ONLY works with github.com tokens, NOT GitHub Enterprise (GHE) tokens.**

If you're seeing authentication errors like:

```
Authorization error, you may need to run /login
Copilot SDK (gh auth) timed out after 65s
```

This is because your `gh` CLI or .env tokens are from a GitHub Enterprise instance (e.g., `github.azc.ext.hp.com`) instead of `github.com`.

## Verification

Check which GitHub instances you're logged into:

```bash
gh auth status
```

You should see:

```
github.com
  ✓ Logged in to github.com account YOUR_USERNAME (keyring)
  - Active account: true
```

**NOT**:

```
github.azc.ext.hp.com
  ✓ Logged in to github.azc.ext.hp.com account YOUR_USERNAME (keyring)
  - Active account: true
```

## Setup Steps

### Option 1: Using .env Profile Account Mapping (Recommended)

1. Choose github.com usernames for each profile.

2. Add to `.env`:

   ```bash
   GITHUB_COPILOT_PERSONAL=your_personal_github_username
   GITHUB_COPILOT_WORK=your_work_github_username
   GITHUB_COPILOT_ACTIVE_PROFILE=personal
   ```

3. Ensure both accounts are logged in via gh on github.com:

   ```bash
   gh auth login --hostname github.com
   gh auth status
   ```

4. Restart the desktop app or dashboard.

### Option 2: Using gh CLI (Fallback)

If no profile tokens are configured, the SDK falls back to `gh auth login` keyring:

1. Login to github.com (NOT your company GHE):

   ```bash
   gh auth login
   ```

2. Select:
   - **GitHub.com** ← IMPORTANT: Not your company instance
   - **HTTPS** protocol
   - **Login with a web browser** or **Paste an authentication token**

3. Verify:

   ```bash
   gh auth status
   ```

   You should see `github.com` as an active account.

4. If you have multiple accounts (github.com + GHE), set github.com as default:
   ```bash
   gh auth switch --user YOUR_GITHUB_COM_USERNAME
   ```

## Profile Validation

The LLM client now validates/switches gh account at runtime:

```python
# agents/llm_client.py
def _ordered_profile_accounts() -> list[tuple[str, str]]:
   # Resolves configured github.com username by profile
   # active profile first

def _switch_gh_account(username: str) -> None:
   # gh auth switch --hostname github.com --user <username>
```

## LLM Market Data Context

When generating AI trade suggestions, the LLM now has access to:

- **Live bid/ask spreads** from IB Gateway (±3 strikes around ATM)
- **Underlying price** for accurate strike selection
- **ATM strike region** recommendation

This enables the LLM to:

1. Suggest strikes with tight bid/ask spreads (better execution)
2. Avoid illiquid strikes with wide spreads
3. Size trades appropriately based on real market conditions

Example prompt enhancement:

```
## Live Market Data (Expiry 20260430)
Underlying MES price: $5850.75
ATM strike: 5850

Options Chain (Bid/Ask/Mid):
Strike | Call Bid | Call Ask | Call Mid | Put Bid | Put Ask | Put Mid
-------|----------|----------|----------|---------|---------|--------
  5800 |    65.50 |    67.00 |    66.25 |   12.00 |   13.50 |   12.75
  5825 |    48.25 |    49.75 |    49.00 |   18.75 |   20.25 |   19.50
  5850 |    34.00 |    35.50 |    34.75 |   28.50 |   30.00 |   29.25
  5875 |    22.50 |    24.00 |    23.25 |   41.25 |   42.75 |   42.00
  5900 |    14.00 |    15.50 |    14.75 |   57.50 |   59.00 |   58.25
```

## Troubleshooting

### "Copilot SDK (gh auth) timed out"

- No profile account mapping in .env → falling back to current gh CLI account
- gh CLI is authenticated to GHE instead of github.com
- Run `gh auth login` and select github.com

### "Authorization error"

- gh active account is wrong or not licensed for Copilot
- Check `gh auth status` and ensure selected github.com user has Copilot access
- Run `gh auth switch --hostname github.com --user <username>`

### "No LLM response available"

- Profile mapping missing or gh auth invalid
- Check .env has `GITHUB_COPILOT_PERSONAL`/`GITHUB_COPILOT_WORK`
- Ensure `gh auth status` shows logged-in github.com account(s)

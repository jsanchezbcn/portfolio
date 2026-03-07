# LLM Architecture & Authentication

## 🚨 CRITICAL: Copilot SDK Only

**This project uses GitHub Copilot SDK exclusively for LLM operations.**

We **do NOT** use OpenAI API directly. All LLM calls go through the GitHub Copilot SDK, which:

- Authenticates via `gh auth login` (stored in system keyring)
- No OpenAI API key required in `.env`
- Leverages GitHub Copilot subscription for quota
- Routes requests securely through GitHub infrastructure

---

## Authentication Flow

### Primary: GitHub Copilot CLI (Required)

The Copilot SDK authenticates via the `copilot` CLI, which reads credentials from your system keyring after `gh auth login`.

**Setup:**

```bash
# 1. Install GitHub Copilot CLI
brew install gh-copilot  # or visit https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli

# 2. Authenticate (one-time)
gh auth login

# 3. Verify installation
copilot --version
which copilot
```

**No `.env` configuration needed** for the primary flow.

---

### Secondary: Bring-Your-Own-Key (BYOK) with OPENAI_API_KEY

If `OPENAI_API_KEY` is set in `.env`, the Copilot SDK will use it to:

- Bypass GitHub subscription quota limits
- Route requests through your own OpenAI account

**How it works:**

```python
# If OPENAI_API_KEY is available, Copilot SDK uses it internally
# You don't call OpenAI directly—the SDK handles routing
```

**Note:** GitHub Copilot tokens (`gho_*`) are NEVER used with OpenAI API directly. They are only for Copilot SDK authentication via the `copilot` CLI.

---

### Fallback: Direct OpenAI (Last Resort Only)

If Copilot SDK exhausts all retry attempts AND `OPENAI_API_KEY` is configured, a direct OpenAI API call is made as a final fallback.

**Expected behavior:**

- Should rarely occur in normal operation
- Typically indicates an issue with Copilot SDK setup
- Check logs for "No LLM response available" error

---

## Code Implementation

**File:** `agents/llm_client.py`

```python
async def async_llm_chat(
    prompt: str,
    model: str = "gpt-5-mini",
    system: str = "",
    timeout: float = 45.0,
) -> str:
    """
    Failover chain:
    1. Copilot SDK (no API key needed)
    2. Copilot SDK + BYOK (if OPENAI_API_KEY set)
    3. Direct OpenAI (last resort, if OPENAI_API_KEY set)
    4. Final Copilot retry with fast model
    """
```

**Authentication priority:**

1. ✅ `copilot` CLI (system keyring from `gh auth login`)
2. ✅ `OPENAI_API_KEY` environment variable (optional, for quota control)
3. ❌ `GITHUB_COPILOT_TOKEN_*` environment variables (NOT used for LLM calls; only for token picker)

---

## Token Environment Variables

| Variable                        | Purpose                    | Example              |
| ------------------------------- | -------------------------- | -------------------- |
| `GITHUB_COPILOT_TOKEN_PERSONAL` | Token picker UI only       | `gho_xxx...`         |
| `GITHUB_COPILOT_TOKEN_WORK`     | Token picker UI only       | `gho_yyy...`         |
| `OPENAI_API_KEY`                | BYOK + fallback (optional) | `sk_xxx...`          |
| `GITHUB_COPILOT_ACTIVE_PROFILE` | Active profile name        | `personal` or `work` |
| `GITHUB_COPILOT_ACTIVE_TOKEN`   | Currently selected token   | (set by app)         |

**Important:** GitHub Copilot tokens (`gho_*`) are **NEVER** passed to OpenAI API endpoints. They are only used by the Copilot SDK via the `copilot` CLI.

---

## Troubleshooting

### Issue: "Session error: Authorization error, you may need to run /login"

**Solution:** Re-authenticate with GitHub:

```bash
gh auth login
copilot --version  # verify it's installed
```

### Issue: "No LLM response available"

**Cause:** Copilot SDK not responding and no OPENAI_API_KEY fallback.

**Solution:**

1. Verify `copilot` CLI is installed: `which copilot`
2. Verify GitHub auth: `gh auth login`
3. Verify `copilot --version` works
4. Check logs for specific errors
5. (Optional) Set `OPENAI_API_KEY` as backup

### Issue: "Incorrect API key provided: gho_xxx"

**Never do this.** GitHub tokens should NEVER be passed to OpenAI API. If you see this error, the code has a bug—report it immediately.

---

## Migration Notes

- ✅ **Never add direct OpenAI API usage** for regular LLM calls
- ✅ **Always use Copilot SDK** as primary backend
- ✅ **Keep OpenAI fallback minimal** (last resort only)
- ✅ **Document any changes** to LLM routing logic
- ✅ **Use `copilot` CLI for all production setups**

---

## References

- [GitHub Copilot CLI Setup](https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli)
- [GitHub CLI (gh) Authentication](https://cli.github.com/manual/gh_auth_login)
- [Copilot SDK Documentation](https://github.com/copilot-sdk/copilot-sdk-py)

"""

# Copilot Token Picker UI Enhancement

## Overview

Added a comprehensive UI component for selecting between GitHub Copilot personal and work tokens,
with visual status indicators and setup instructions.

## Features Implemented

### 1. Enhanced Account Picker Widget (`desktop/ui/widgets/account_picker.py`)

**New Visual Elements:**

- **Status Label**: Shows ✅ or ❌ emoji indicating if a token is configured for the active profile
- **Info Button**: "ℹ" button to open token configuration dialog
- **Clickable Status**: Click the status label to open the info dialog

**New Capabilities:**

- `token_available(profile)` - Check if a token is configured for a profile
- `refresh_token_status()` - Manually refresh token status indicators
- `token_status_changed` signal - Emitted when token status changes
- Custom token checker callback - Pass your own token validation logic

**Token Checker Integration:**

```python
# Use with custom checker
def my_checker(profile: str) -> bool:
    # Return True if token is available for the profile
    return has_token(profile)

widget = AccountPicker(token_checker=my_checker)
```

**Default Behavior:**

- Reads from environment variables (`GITHUB_COPILOT_TOKEN_PERSONAL`, `GITHUB_COPILOT_TOKEN_WORK`)
- Shows visual status for both profiles
- Gracefully handles missing or misconfigured tokens

### 2. Token Info Dialog (`desktop/ui/widgets/account_picker.py::TokenInfoDialog`)

A helpful dialog that displays:

- Token configuration status for all profiles (✅ CONFIGURED / ❌ NOT CONFIGURED)
- Environment variable names needed for each profile
- Step-by-step setup instructions
- Command to get GitHub Copilot tokens: `gh auth token`

Users can access this by:

- Clicking the status label (✅/❌)
- Clicking the ℹ info button

### 3. Token Manager (`desktop/engine/token_manager.py`)

Core manager class that handles:

- Profile persistence in preferences
- Token routing via environment variables
- State management for active profiles
- Profile normalization and validation

**Usage with Account Picker:**

```python
from desktop.engine.token_manager import TokenManager
from desktop.ui.widgets.account_picker import AccountPicker

manager = TokenManager()

def checker(profile):
    return manager.has_configured_token(profile)

picker = AccountPicker(token_checker=checker)
picker.profile_changed.connect(manager.set_active_profile)
```

## Test Coverage

### Unit Tests (`desktop/tests/test_account_picker.py`) - 21 tests

- Profile switching and normalization
- Signal emission behavior
- Token status checking
- Dialog creation and content
- Integration with TokenManager

### UI Tests (`desktop/tests/test_account_picker_ui.py`) - 23 tests

- Visual element visibility
- User interactions (clicking, selection)
- Dialog behavior
- Visual feedback (icons, tooltips, cursors)
- Edge cases and error handling

### Token Manager Tests (`tests/test_token_manager.py`) - 24 tests

- Initialization and preferences loading
- Profile management
- Token routing
- State persistence
- Error handling
- Integration with AccountPicker

**Total: 68 tests, 100% passing**

## Integration with MainWindow

The AccountPicker is already integrated in the toolbar:

```python
from desktop.ui.widgets.account_picker import AccountPicker
from desktop.engine.token_manager import TokenManager

class MainWindow(QMainWindow):
    def __init__(self, engine, token_manager=None):
        self._token_manager = token_manager or TokenManager()

        # In _setup_toolbar():
        self._account_picker = AccountPicker(
            self._token_manager.active_profile,
            token_checker=lambda p: self._token_manager.has_configured_token(p)
        )
        toolbar.addWidget(self._account_picker)

        # Connect profile change to update token
        self._account_picker.profile_changed.connect(
            self._on_copilot_profile_changed
        )
```

## Files Modified

1. **`desktop/ui/widgets/account_picker.py`** - Enhanced with status display and info dialog
2. **`desktop/tests/test_account_picker.py`** - Expanded test suite (21 tests)
3. **`desktop/tests/test_account_picker_ui.py`** - New UI tests (23 tests)
4. **`tests/test_token_manager.py`** - New TokenManager tests (24 tests)

## Environment Variables

The following environment variables are used:

```bash
# Token storage
GITHUB_COPILOT_TOKEN_PERSONAL=your_personal_token
GITHUB_COPILOT_TOKEN_WORK=your_work_token

# Internal tracking (set by TokenManager)
GITHUB_COPILOT_ACTIVE_PROFILE=personal|work
GITHUB_COPILOT_ACTIVE_TOKEN=the_active_token
```

## Usage Example

```python
from desktop.ui.widgets.account_picker import AccountPicker
from desktop.engine.token_manager import TokenManager
from desktop.ui.main_window import MainWindow

# Setup
token_manager = TokenManager()
main_window = MainWindow(engine, token_manager)

# User can now:
# 1. Click on the Copilot dropdown to switch between Personal/Work
# 2. See ✅ or ❌ status indicator
# 3. Click the ℹ button or status label to see token configuration
# 4. Follow setup instructions to add tokens
# 5. Tokens are automatically persisted in preferences.json
```

## Testing

Run all new tests:

```bash
QT_QPA_PLATFORM=offscreen pytest \
  desktop/tests/test_account_picker.py \
  desktop/tests/test_account_picker_ui.py \
  tests/test_token_manager.py -v
```

Run specific test class:

```bash
QT_QPA_PLATFORM=offscreen pytest \
  desktop/tests/test_account_picker.py::TestTokenStatusDisplay -v
```

## Error Handling

The widget gracefully handles:

- Missing environment variables → Shows ❌ status
- Empty token strings → Shows ❌ status
- Token checker exceptions → Logs warning, defaults to unavailable
- Invalid profile names → Normalizes to "personal"
- Whitespace in profile names → Strips and normalizes

## Future Enhancements

Potential additions:

- Token validation/testing (verify token is valid)
- Automatic token refresh
- Token expiration warnings
- Per-profile usage statistics
- Token rotation helpers
  """

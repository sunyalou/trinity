# Feature: Agent Quotas (QUOTA-001)

## Overview
Per-role agent creation limits enforced on both `POST /api/agents` and `POST /api/agents/deploy-local`. Admin users are always exempt (unlimited).

## User Story
As a platform admin, I want to set per-role limits on how many agents each user can create so that resource usage stays controlled across different user tiers.

## Entry Points
- **UI (admin config)**: `src/frontend/src/views/Settings.vue:1179` - Agent Quotas section
- **UI (user feedback)**: `src/frontend/src/components/CreateAgentModal.vue:354` - QUOTA_EXCEEDED error display
- **API (config)**: `GET /api/settings/agent-quotas`, `PUT /api/settings/agent-quotas`
- **API (enforcement)**: `POST /api/agents`, `POST /api/agents/deploy-local`

## Frontend Layer

### Settings UI (`Settings.vue:1179-1260`)
- Three numeric inputs for creator/operator/user role quotas
- Admin row shown as "Unlimited" (not editable)
- Legacy `max_agents_per_user` warning banner if that setting exists
- Calls `loadAgentQuotas()` on mount, `saveAgentQuotas()` on save

### API Calls
```javascript
// Load
const response = await axios.get('/api/settings/agent-quotas', { headers: authStore.authHeader })
// Save
await axios.put('/api/settings/agent-quotas', {
  max_agents_creator: String(value),
  max_agents_operator: String(value),
  max_agents_user: String(value)
})
```

### CreateAgentModal Error Handling (`CreateAgentModal.vue:354`)
```javascript
if (detail && typeof detail === 'object' && detail.code === 'QUOTA_EXCEEDED') {
  error.value = `${detail.error}`
}
```

## Backend Layer

### Settings Endpoints (`src/backend/routers/settings.py:960-1035`)

**`GET /api/settings/agent-quotas`** (admin-only)
- Returns per-role quota config with current values, defaults, descriptions
- Includes `legacy_setting` field if `max_agents_per_user` exists

**`PUT /api/settings/agent-quotas`** (admin-only)
- Request body: `AgentQuotaUpdate` with optional `max_agents_creator`, `max_agents_operator`, `max_agents_user`
- Validates non-negative integers
- Stores each as individual key in `system_settings` table via `db.set_setting()`

### Quota Resolution (`src/backend/services/settings_service.py:255-282`)

`get_agent_quota_for_role(role)` lookup order:
1. If `role == "admin"` -> return 0 (unlimited, always)
2. Check `max_agents_{role}` in `system_settings`
3. Fall back to legacy `max_agents_per_user` setting
4. Fall back to hardcoded defaults: creator=10, operator=3, user=1

Returns `int` where 0 = unlimited.

### Enforcement: Agent Create (`src/backend/services/agent_service/crud.py:92-108`)
```python
max_agents = get_agent_quota_for_role(current_user.role)
if max_agents > 0:
    owned = db.get_agents_by_owner(current_user.username)
    non_system = [a for a in owned if not (db.get_agent_owner(a) or {}).get("is_system")]
    if len(non_system) >= max_agents:
        raise HTTPException(status_code=429, detail={...})
```

### Enforcement: Deploy Local (`src/backend/services/agent_service/deploy.py:281-300`)
- Same logic as create, but with **redeploy bypass**: if an existing agent with the same name prefix is already owned by this user, quota is not enforced (it's a version update, not a new agent).

## Data Layer

### Database
- **Table**: `system_settings` (key-value store)
- **Keys**: `max_agents_creator`, `max_agents_operator`, `max_agents_user`, `max_agents_per_user` (legacy)
- **Reads**: `db.get_setting_value(key)` for quota lookup
- **Writes**: `db.set_setting(key, value)` for admin config
- **Count query**: `db.get_agents_by_owner(username)` returns list of owned agent names

### System Agent Exclusion
Agents where `agent_ownership.is_system = true` are excluded from the count. Only user-created agents count toward quota.

## Error Handling

| Error Case | HTTP Status | Response Detail |
|------------|-------------|-----------------|
| Quota exceeded | 429 | `{"error": "Agent quota exceeded...", "code": "QUOTA_EXCEEDED", "current": N, "limit": M}` |
| Negative quota value | 400 | `"Quota value for {key} must be non-negative"` |
| Non-integer quota value | 400 | `"Quota value for {key} must be an integer"` |
| Non-admin access to settings | 403 | Standard auth error |

## Side Effects
- None. No WebSocket broadcasts or activity tracking for quota changes.

## Testing

Test file: `tests/test_agent_quota.py`

### Test Coverage
1. Default quota values (creator=10, operator=3, user=1)
2. Admin can update quota settings
3. Agent creation blocked at quota limit (HTTP 429)
4. System agents excluded from count
5. Redeploy of existing agent bypasses quota
6. Admin users always exempt

### Manual Test Steps
1. **Action**: Set quota to 1 via `PUT /api/settings/agent-quotas`
   **Expected**: Setting saved
2. **Action**: Create one agent as creator user
   **Expected**: 200 success
3. **Action**: Create second agent as same user
   **Expected**: 429 with QUOTA_EXCEEDED code
4. **Action**: Create agent as admin
   **Expected**: 200 success (admin exempt)

## Related Flows
- [role-model.md](role-model.md) - Role hierarchy that quotas are based on
- [platform-settings.md](platform-settings.md) - Settings UI where quotas are configured
- [agent-lifecycle.md](agent-lifecycle.md) - Agent creation flow where quotas are enforced

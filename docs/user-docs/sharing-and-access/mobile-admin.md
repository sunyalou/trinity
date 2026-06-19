# Mobile Admin

Standalone mobile-optimized PWA at `/m` for managing agents on the go.

## How It Works

1. Navigate to `http://localhost/m` on a mobile device (or any browser).
2. Install as a PWA via **Add to Home Screen** for a native app experience.
3. The interface has three tabs:
   - **Agents** -- List agents, tap to chat, toggle autonomy, send tasks.
   - **Ops** -- View and act on operator queue items and notifications (the same items as the desktop [Operations page](../operations/operating-room.md)).
   - **System** -- System-level controls and status.
4. Designed for quick interactions: check agent status, respond to agent questions, toggle autonomy on or off.

There is no desktop equivalent -- this is a dedicated mobile interface.

## For Agents

Mobile Admin uses the same backend API as the desktop UI. No additional endpoints are required. All authenticated API calls work identically from the mobile interface.

## See Also

- [Dashboard](../operations/dashboard.md)
- [Operations Page](../operations/operating-room.md)
- [Managing Agents](../agents/managing-agents.md)

# DoraBox browser backend

This fork can drive Chrome through the existing DoraBox Browser Bridge extension
instead of asking the user to enable Chrome remote debugging.

```text
Browser Harness helpers
        |
        | native CDP method + params
        v
DoraBoxCDPClient
        |
        | token-protected localhost /command
        v
DoraBox daemon
        |
        | existing extension WebSocket
        v
Chrome extension / chrome.debugger
        |
        v
the user's current Chrome profile
```

## Selection

`BH_BROWSER_BACKEND` accepts:

- `auto` (default): use DoraBox when it is installed, otherwise retain the
  official local Browser Harness path.
- `dorabox`: require DoraBox and fail with a clear setup error when unavailable.
- `local`: force the official local CDP path.

`BU_BROWSER_ID`, `BU_CDP_WS`, and `BU_CDP_URL` remain explicit overrides and
take precedence.

Optional variables:

- `DORABOX_BIN`: path/name of the DoraBox executable.
- `DORABOX_PROFILE`: exact DoraBox browser profile/context.
- `DORABOX_DAEMON_PORT`: non-default development daemon port.
- `BH_DORABOX_HANDOFF`: exact failed-adapter handoff id to claim at startup.

## Same-tab recovery

DoraBox adapter errors may contain a handoff receipt. Adopt it from a running
Harness daemon:

```python
from browser_harness import *
adopt_dorabox_handoff("handoff_...")
print(page_info())
```

The returned object contains `safety`:

- `continue`: continue from the current page.
- `blocked`: preserve the page and ask the user to resolve login/CAPTCHA/rate
  limiting. Do not bypass it.
- `unknown_effect`: inspect whether the prior write already succeeded before
  performing another mutation.

The transport changes routing only. It does not open a replacement tab or
navigate away, so form values, scroll position and authenticated state remain.

## Security

Page CDP calls stay native, but every request is scoped to a DoraBox session
lease and requires the local daemon capability token. Top-level target
create/select/close operations use DoraBox's scoped tab API; browser-wide
destructive operations are never exposed through the raw channel.

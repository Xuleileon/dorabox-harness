"""DoraBox-backed CDP transport.

The official Browser Harness talks directly to a browser-level CDP WebSocket.
This backend preserves the exact CDP method/params API while replacing only the
transport: commands are sent to DoraBox's token-protected local daemon, which
forwards them to the already-authorized Chrome extension and chrome.debugger.

Top-level Target lifecycle is adapted to DoraBox's scoped tab primitives so one
Harness client cannot escape its assigned browser session. Page/runtime/input
commands pass through unchanged. Same-tab handoff records switch the transport
onto the exact adapter lease that failed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_PORTS = (9925, 19925)
DEFAULT_HTTP_TIMEOUT = 135.0
DEFAULT_HANDOFF_TTL_SECONDS = 10 * 60
VALID_BACKEND_PREFERENCES = {"auto", "dorabox", "local"}


class DoraBoxTransportError(RuntimeError):
    """A structured DoraBox bridge failure."""


class _EventRegistry:
    """Minimal cdp-use-compatible event registry.

    Daemon.start wraps ``handle_event`` to populate its own event queue and
    dialog state, exactly as it does for cdp-use's registry.
    """

    async def handle_event(self, method: str, params: dict[str, Any], session_id: str | None = None):
        return None


def _token_path() -> Path:
    configured = os.environ.get("DORABOX_DAEMON_TOKEN_FILE")
    return Path(configured).expanduser() if configured else Path.home() / ".dorabox" / "daemon-token"


def _read_token() -> str:
    try:
        token = _token_path().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DoraBoxTransportError(
            "DoraBox daemon token is unavailable. Install/start DoraBox before using "
            "the DoraBox Browser Harness backend."
        ) from exc
    if not token:
        raise DoraBoxTransportError("DoraBox daemon token file is empty")
    return token


def _port_candidates() -> list[int]:
    configured = os.environ.get("DORABOX_DAEMON_PORT")
    ports: list[int] = []
    if configured:
        try:
            ports.append(int(configured))
        except ValueError:
            pass
    for port in DEFAULT_PORTS:
        if port not in ports:
            ports.append(port)
    return ports


def _request_json_sync(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
    require_token: bool = False,
) -> dict[str, Any]:
    headers = {"X-Dorabox": "1", "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if require_token:
        headers["X-Dorabox-Token"] = _read_token()

    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except ValueError:
            detail = {"error": raw or str(exc)}
        message = detail.get("error") or detail.get("message") or str(exc)
        code = detail.get("errorCode")
        hint = detail.get("errorHint")
        pieces = [str(message)]
        if code:
            pieces.append(f"[{code}]")
        if hint:
            pieces.append(str(hint))
        raise DoraBoxTransportError(" ".join(pieces)) from exc
    except OSError as exc:
        raise DoraBoxTransportError(f"Cannot reach DoraBox daemon at {base_url}: {exc}") from exc

    try:
        value = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise DoraBoxTransportError("DoraBox daemon returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DoraBoxTransportError("DoraBox daemon returned a non-object response")
    return value


def _probe_status(base_url: str, timeout: float = 0.5) -> dict[str, Any] | None:
    try:
        return _request_json_sync(base_url, "/status", timeout=timeout)
    except Exception:
        return None


def _discover_base_url(timeout: float = 0.5) -> tuple[str, dict[str, Any]] | None:
    context_id = os.environ.get("DORABOX_PROFILE")
    suffix = f"?contextId={urllib.parse.quote(context_id)}" if context_id else ""
    for port in _port_candidates():
        base_url = f"http://127.0.0.1:{port}"
        try:
            status = _request_json_sync(base_url, f"/status{suffix}", timeout=timeout)
        except Exception:
            continue
        if status.get("ok"):
            return base_url, status
    return None


def _dorabox_binary() -> str:
    return os.environ.get("DORABOX_BIN") or shutil.which("dorabox") or "dorabox"


def _start_dorabox_daemon() -> None:
    try:
        subprocess.run(
            [_dorabox_binary(), "daemon", "start"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return


def backend_preference() -> str:
    value = os.environ.get("BH_BROWSER_BACKEND", "auto").strip().lower()
    if value == "native":
        value = "local"
    if value not in VALID_BACKEND_PREFERENCES:
        raise DoraBoxTransportError(
            f"BH_BROWSER_BACKEND must be one of {sorted(VALID_BACKEND_PREFERENCES)}, got {value!r}"
        )
    return value


def should_use_dorabox() -> bool:
    """Whether daemon.py should instantiate the DoraBox backend.

    Explicit Browser Use cloud / CDP endpoints always win. In auto mode, the
    presence of a DoraBox installation/token is enough: start() can launch the
    daemon and wait for the extension.
    """

    if os.environ.get("BU_BROWSER_ID") or os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL"):
        return False
    preference = backend_preference()
    if preference == "dorabox":
        return True
    if preference == "local":
        return False
    return _token_path().exists() or shutil.which(os.environ.get("DORABOX_BIN") or "dorabox") is not None


def browser_kind() -> str:
    if os.environ.get("BU_BROWSER_ID"):
        return "cloud"
    if os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL"):
        return "cdp"
    return "dorabox" if should_use_dorabox() else "local"


class DoraBoxCDPClient:
    """Subset-compatible replacement for :class:`cdp_use.client.CDPClient`."""

    def __init__(self, name: str):
        self.name = name
        self.client_id = f"browser-harness:{name}"
        self.base_url: str | None = None
        self.context_id = os.environ.get("DORABOX_PROFILE") or None
        self.session_name = f"harness:{name}"
        self.surface: str = "browser"
        self.site_session: str | None = None
        self.handoff: dict[str, Any] | None = None
        self.claimed_target_id: str | None = None
        self._root_sessions: dict[str, str] = {}
        self._current_target_id: str | None = None
        self._event_registry = _EventRegistry()
        self._started = False
        self._lock = asyncio.Lock()

    async def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        require_token: bool = False,
    ) -> dict[str, Any]:
        if self.base_url is None:
            raise DoraBoxTransportError("DoraBox transport has not started")
        return await asyncio.to_thread(
            _request_json_sync,
            self.base_url,
            path,
            method=method,
            payload=payload,
            timeout=timeout,
            require_token=require_token,
        )

    def _routing(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "session": self.session_name,
            "surface": self.surface,
        }
        if self.site_session:
            result["siteSession"] = self.site_session
        if self.context_id:
            result["contextId"] = self.context_id
        return result

    async def _command(
        self,
        action: str,
        *,
        page: str | None = None,
        routing: dict[str, Any] | None = None,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        **params: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": f"bh_{uuid.uuid4().hex}",
            "action": action,
            **(routing or self._routing()),
            **params,
        }
        if page:
            body["page"] = page
        response = await self._request(
            "/command",
            method="POST",
            payload=body,
            timeout=timeout,
            require_token=True,
        )
        if not response.get("ok"):
            message = response.get("error") or f"DoraBox action {action} failed"
            code = response.get("errorCode")
            hint = response.get("errorHint")
            suffix = " ".join(str(v) for v in (f"[{code}]" if code else None, hint) if v)
            raise DoraBoxTransportError(f"{message}{' ' + suffix if suffix else ''}")
        if response.get("page"):
            self._current_target_id = str(response["page"])
        return response

    async def start(self):
        if self._started:
            raise RuntimeError("Client is already started")

        discovered = await asyncio.to_thread(_discover_base_url, 0.5)
        if discovered is None:
            await asyncio.to_thread(_start_dorabox_daemon)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and discovered is None:
                await asyncio.sleep(0.25)
                discovered = await asyncio.to_thread(_discover_base_url, 0.75)

        if discovered is None:
            raise DoraBoxTransportError(
                "DoraBox daemon is not reachable. Start DoraBox and make sure the "
                "Browser Bridge extension is installed."
            )

        self.base_url, status = discovered
        if not status.get("extensionConnected"):
            raise DoraBoxTransportError(
                "DoraBox daemon is running but the Browser Bridge extension is not connected. "
                "Open Chrome and enable the DoraBox extension."
            )

        self._started = True
        requested = os.environ.get("BH_DORABOX_HANDOFF")
        if requested:
            await self.claim_handoff(requested)
        else:
            # Auto-claim only when DoraBox reports one unambiguous pending record.
            try:
                listing = await self._handoff_command("list")
                pending = [
                    item for item in (listing.get("data") or [])
                    if isinstance(item, dict) and item.get("status") == "pending"
                ]
                if len(pending) == 1:
                    await self.claim_handoff(str(pending[0]["id"]))
            except DoraBoxTransportError:
                # Ordinary Browser Harness use must remain available when there
                # is no handoff or an old DoraBox version lacks this action.
                pass

    async def stop(self):
        if not self._started:
            return
        if self.handoff:
            try:
                await self.release_handoff()
            except Exception:
                pass
        elif self._current_target_id:
            try:
                await self._command(
                    "harness-events",
                    page=self._current_target_id,
                    harnessClientId=self.client_id,
                    harnessEventOp="release",
                    timeout=15,
                )
            except Exception:
                pass
        self._started = False

    async def _handoff_command(
        self,
        op: str,
        handoff_id: str | None = None,
        *,
        ttl_seconds: int = DEFAULT_HANDOFF_TTL_SECONDS,
    ) -> dict[str, Any]:
        routing = {
            "session": f"harness:{self.name}",
            "surface": "browser",
            **({"contextId": self.context_id} if self.context_id else {}),
        }
        return await self._command(
            "harness-handoff",
            routing=routing,
            handoffOp=op,
            harnessClientId=self.client_id,
            **({"handoffId": handoff_id} if handoff_id else {}),
            handoffTtlSeconds=ttl_seconds,
        )

    async def claim_handoff(self, handoff_id: str | None = None) -> dict[str, Any]:
        async with self._lock:
            if self.handoff and (handoff_id is None or self.handoff.get("id") == handoff_id):
                # Idempotently renew the claim and its lease.
                response = await self._handoff_command(
                    "claim",
                    str(self.handoff["id"]),
                )
            else:
                if self.handoff:
                    try:
                        await self.release_handoff()
                    except Exception:
                        pass
                response = await self._handoff_command("claim", handoff_id)

            record = response.get("data")
            if not isinstance(record, dict) or not record.get("page"):
                raise DoraBoxTransportError("DoraBox returned an invalid handoff record")

            self.handoff = record
            self.context_id = str(record.get("contextId") or self.context_id or "") or None
            self.session_name = str(record.get("session") or self.session_name)
            self.surface = str(record.get("surface") or "adapter")
            self.site_session = record.get("siteSession")
            self.claimed_target_id = str(record["page"])
            self._current_target_id = self.claimed_target_id
            self._root_sessions.clear()
            return record

    async def release_handoff(self) -> None:
        record = self.handoff
        if not record:
            return
        handoff_id = str(record["id"])
        await self._command(
            "harness-handoff",
            handoffOp="release",
            handoffId=handoff_id,
            harnessClientId=self.client_id,
        )
        self.handoff = None

    async def poll_events(self) -> list[dict[str, Any]]:
        target_id = self._current_target_id
        if not target_id:
            return []
        try:
            response = await self._command(
                "harness-events",
                page=target_id,
                harnessClientId=self.client_id,
                harnessEventOp="drain",
                timeout=15,
            )
        except DoraBoxTransportError:
            return []

        events = response.get("data") or []
        normalized: list[dict[str, Any]] = []
        if not isinstance(events, list):
            return normalized
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("method"), str):
                continue
            real_session_id = event.get("sessionId")
            session_id = real_session_id
            if not session_id:
                # Root-tab events use whichever synthetic session currently maps
                # to the event's leased target.
                session_id = next(
                    (sid for sid, target in self._root_sessions.items() if target == target_id),
                    None,
                )
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            await self._event_registry.handle_event(event["method"], params, session_id)
            normalized.append({
                "method": event["method"],
                "params": params,
                "session_id": session_id,
            })
        return normalized

    async def _tabs(self) -> list[dict[str, Any]]:
        response = await self._command("tabs", op="list")
        data = response.get("data") or []
        tabs = [item for item in data if isinstance(item, dict) and item.get("page")]
        if self.claimed_target_id:
            tabs.sort(key=lambda item: 0 if item.get("page") == self.claimed_target_id else 1)
        return tabs

    async def _target_info(self, target_id: str) -> dict[str, Any]:
        for tab in await self._tabs():
            if tab.get("page") == target_id:
                return {
                    "targetId": target_id,
                    "type": "page",
                    "title": tab.get("title") or "",
                    "url": tab.get("url") or "",
                    "attached": target_id in self._root_sessions.values(),
                    "browserContextId": f"dorabox:{self.context_id or 'default'}",
                }
        raise DoraBoxTransportError(f"Target not found in DoraBox session: {target_id}")

    def _synthetic_session(self, target_id: str) -> str:
        for session_id, mapped_target in self._root_sessions.items():
            if mapped_target == target_id:
                return session_id
        session_id = f"dorabox:{target_id}"
        self._root_sessions[session_id] = target_id
        return session_id

    async def _send_page_command(
        self,
        method: str,
        params: dict[str, Any],
        session_id: str | None,
    ) -> dict[str, Any]:
        target_id: str | None
        child_session_id: str | None = None
        if session_id in self._root_sessions:
            target_id = self._root_sessions[session_id]
        else:
            target_id = self._current_target_id
            child_session_id = session_id

        if not target_id:
            tabs = await self._tabs()
            if not tabs:
                raise DoraBoxTransportError("No DoraBox tab is available for CDP")
            target_id = str(tabs[0]["page"])
            self._current_target_id = target_id

        response = await self._command(
            "harness-cdp",
            page=target_id,
            harnessClientId=self.client_id,
            cdpMethod=method,
            cdpParams=params,
            **({"cdpSessionId": child_session_id} if child_session_id else {}),
        )
        result = response.get("data")
        return result if isinstance(result, dict) else ({} if result is None else {"value": result})

    async def send_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not self._started:
            raise DoraBoxTransportError("DoraBox transport is not started")
        params = params or {}

        if method == "Target.getTargets":
            tabs = await self._tabs()
            return {
                "targetInfos": [
                    {
                        "targetId": str(tab["page"]),
                        "type": "page",
                        "title": tab.get("title") or "",
                        "url": tab.get("url") or "",
                        "attached": str(tab["page"]) in self._root_sessions.values(),
                        "browserContextId": f"dorabox:{self.context_id or 'default'}",
                    }
                    for tab in tabs
                ]
            }

        if method == "Target.getTargetInfo":
            target_id = str(params.get("targetId") or self._current_target_id or "")
            if not target_id:
                raise DoraBoxTransportError("Target.getTargetInfo requires a targetId")
            return {"targetInfo": await self._target_info(target_id)}

        if method == "Target.attachToTarget":
            target_id = str(params.get("targetId") or "")
            if not target_id:
                raise DoraBoxTransportError("Target.attachToTarget requires targetId")
            await self._target_info(target_id)
            self._current_target_id = target_id
            return {"sessionId": self._synthetic_session(target_id)}

        if method == "Target.detachFromTarget":
            detached = str(params.get("sessionId") or session_id or "")
            self._root_sessions.pop(detached, None)
            return {}

        if method == "Target.createTarget":
            url = str(params.get("url") or "about:blank")
            command_params: dict[str, Any] = {"op": "new"}
            if url.startswith(("http://", "https://")):
                command_params["url"] = url
            response = await self._command("tabs", **command_params)
            target_id = response.get("page")
            if not target_id:
                raise DoraBoxTransportError("DoraBox did not return a target for the new tab")
            self._current_target_id = str(target_id)
            return {"targetId": str(target_id)}

        if method == "Target.closeTarget":
            target_id = str(params.get("targetId") or "")
            if not target_id:
                raise DoraBoxTransportError("Target.closeTarget requires targetId")
            await self._command("tabs", op="close", page=target_id)
            for synthetic, mapped in list(self._root_sessions.items()):
                if mapped == target_id:
                    self._root_sessions.pop(synthetic, None)
            if self._current_target_id == target_id:
                self._current_target_id = None
            return {"success": True}

        if method == "Target.activateTarget":
            target_id = str(params.get("targetId") or "")
            if not target_id:
                raise DoraBoxTransportError("Target.activateTarget requires targetId")
            await self._command("tabs", op="select", page=target_id)
            self._current_target_id = target_id
            return {}

        if method == "Target.getBrowserContexts":
            return {"browserContextIds": [f"dorabox:{self.context_id or 'default'}"]}

        if method == "Target.createBrowserContext":
            raise DoraBoxTransportError("DoraBox scopes Browser Harness to an existing Chrome profile")

        if method == "Target.disposeBrowserContext":
            raise DoraBoxTransportError("DoraBox Browser Harness cannot dispose the user's Chrome profile")

        return await self._send_page_command(method, params, session_id)

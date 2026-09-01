import asyncio

from browser_harness.dorabox_backend import DoraBoxCDPClient, browser_kind


class FakeDoraBoxClient(DoraBoxCDPClient):
    def __init__(self):
        super().__init__("test")
        self._started = True
        self.commands = []
        self.tabs = [
            {
                "index": 0,
                "page": "target-1",
                "url": "https://example.com/step-2",
                "title": "Step 2",
                "active": True,
            }
        ]

    async def _command(self, action, *, page=None, routing=None, timeout=135.0, **params):
        self.commands.append({
            "action": action,
            "page": page,
            "routing": routing,
            "params": params,
        })
        if action == "tabs" and params.get("op") == "list":
            return {"ok": True, "data": list(self.tabs)}
        if action == "tabs" and params.get("op") == "new":
            self.tabs.append({
                "index": len(self.tabs),
                "page": "target-2",
                "url": params.get("url") or "about:blank",
                "title": "",
                "active": False,
            })
            return {"ok": True, "data": {}, "page": "target-2"}
        if action == "tabs" and params.get("op") == "close":
            self.tabs = [tab for tab in self.tabs if tab["page"] != page]
            return {"ok": True, "data": {"closed": page}}
        if action == "harness-cdp":
            return {"ok": True, "data": {"echo": params}, "page": page}
        return {"ok": True, "data": {}}


def run(coro):
    return asyncio.run(coro)


def test_browser_kind_explicit_cdp_wins(monkeypatch):
    monkeypatch.setenv("BH_BROWSER_BACKEND", "dorabox")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")
    assert browser_kind() == "cdp"


def test_top_level_target_lifecycle_is_scoped_to_dorabox_tabs():
    client = FakeDoraBoxClient()

    targets = run(client.send_raw("Target.getTargets"))
    assert [item["targetId"] for item in targets["targetInfos"]] == ["target-1"]

    attached = run(client.send_raw(
        "Target.attachToTarget",
        {"targetId": "target-1", "flatten": True},
    ))
    assert attached["sessionId"] == "dorabox:target-1"

    created = run(client.send_raw("Target.createTarget", {"url": "about:blank"}))
    assert created == {"targetId": "target-2"}

    closed = run(client.send_raw("Target.closeTarget", {"targetId": "target-2"}))
    assert closed == {"success": True}


def test_page_cdp_method_and_params_are_not_translated():
    client = FakeDoraBoxClient()
    session_id = run(client.send_raw(
        "Target.attachToTarget",
        {"targetId": "target-1", "flatten": True},
    ))["sessionId"]

    result = run(client.send_raw(
        "Runtime.evaluate",
        {"expression": "document.title", "returnByValue": True},
        session_id=session_id,
    ))

    assert result["echo"]["cdpMethod"] == "Runtime.evaluate"
    assert result["echo"]["cdpParams"] == {
        "expression": "document.title",
        "returnByValue": True,
    }
    command = client.commands[-1]
    assert command["action"] == "harness-cdp"
    assert command["page"] == "target-1"
    assert "cdpSessionId" not in command["params"]


def test_real_child_session_is_forwarded_unchanged():
    client = FakeDoraBoxClient()
    client._current_target_id = "target-1"

    run(client.send_raw(
        "Runtime.evaluate",
        {"expression": "location.href"},
        session_id="real-child-session",
    ))

    assert client.commands[-1]["params"]["cdpSessionId"] == "real-child-session"


def test_handoff_switches_routing_without_opening_or_navigating():
    client = FakeDoraBoxClient()

    async def fake_handoff_command(op, handoff_id=None, ttl_seconds=600):
        assert op == "claim"
        assert handoff_id == "handoff-1"
        return {
            "ok": True,
            "page": "target-1",
            "data": {
                "id": "handoff-1",
                "contextId": "profile-a",
                "session": "site:xhs",
                "surface": "adapter",
                "siteSession": "ephemeral",
                "page": "target-1",
                "url": "https://example.com/step-2",
                "site": "xhs",
                "command": "xhs/search",
                "safety": "continue",
                "status": "claimed",
                "createdAt": 1,
                "expiresAt": 600_001,
            },
        }

    client._handoff_command = fake_handoff_command
    record = run(client.claim_handoff("handoff-1"))

    assert record["page"] == "target-1"
    assert client.claimed_target_id == "target-1"
    assert client.session_name == "site:xhs"
    assert client.surface == "adapter"
    assert client.site_session == "ephemeral"
    assert not any(
        item["action"] == "tabs" and item["params"].get("op") == "new"
        for item in client.commands
    )

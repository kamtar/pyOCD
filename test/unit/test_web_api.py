import asyncio

from aiohttp.test_utils import TestClient, TestServer

from pyocd.web.application import (
    DEFAULT_AUTH_TOKEN,
    _WebClients,
    _effective_auth_token,
    create_application,
)
from pyocd.web.controller import WebController

CSRF = {"X-pyOCD-CSRF": "1"}


def run(coro):
    return asyncio.run(coro)


def test_web_clients_tracks_tabs_and_expires_stale_heartbeats():
    clients = _WebClients(timeout=3.0)

    assert clients.touch("tab-a", now=1.0) == {"connected": 1, "others": 0}
    assert clients.touch("tab-b", now=2.0) == {"connected": 2, "others": 1}
    assert clients.touch("tab-a", now=2.5) == {"connected": 2, "others": 1}
    assert clients.touch("tab-a", now=5.1) == {"connected": 1, "others": 0}


def test_health_and_frontend(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            health = await client.get("/api/v1/health")
            assert health.status == 200
            assert await health.json() == {"status": "ok"}
            index = await client.get("/")
            assert index.status == 200
            assert "pyOCD Control Center" in await index.text()
        finally:
            await client.close()
    run(scenario())


def test_system_information_route(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        controller.system_info = lambda: {
            "pyocd_version": "1.0", "hostname": "debug-host", "addresses": ["192.0.2.1"]}
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            response = await client.get("/api/v1/system")
            assert response.status == 200
            assert (await response.json())["hostname"] == "debug-host"
        finally:
            await client.close()
    run(scenario())


def test_probe_reset_route(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        controller.pulse_probe_reset = lambda profile: {
            "reset": True, "probe": profile["probe"]}
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            response = await client.post(
                "/api/v1/probe/reset", json={"probe": "adapter"}, headers=CSRF)
            assert response.status == 200
            assert await response.json() == {"reset": True, "probe": "adapter"}
        finally:
            await client.close()
    run(scenario())


def test_system_power_route(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        controller.system_power = lambda action: {"accepted": True, "action": action}
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            response = await client.post(
                "/api/v1/system/power/reboot", json={}, headers=CSRF)
            assert response.status == 202
            assert await response.json() == {"accepted": True, "action": "reboot"}
        finally:
            await client.close()
    run(scenario())


def test_runtime_restart_route_schedules_process_replacement(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        restarted = asyncio.Event()
        client = TestClient(TestServer(create_application(
            controller, restart_process=restarted.set)))
        await client.start_server()
        try:
            response = await client.post(
                "/api/v1/runtime/restart", json={}, headers=CSRF)
            assert response.status == 202
            assert await response.json() == {"accepted": True, "action": "restart"}
            await asyncio.wait_for(restarted.wait(), timeout=1.0)
        finally:
            await client.close()
    run(scenario())


def test_update_check_route(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        controller.check_for_update = lambda: {
            "current": "1.0", "latest": "1.1", "update_available": True}
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            response = await client.get("/api/v1/update/check")
            assert response.status == 200
            assert (await response.json())["update_available"] is True
        finally:
            await client.close()
    run(scenario())


def test_unknown_route_remains_not_found(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            response = await client.get("/favicon.ico")
            assert response.status == 404
        finally:
            await client.close()
    run(scenario())


def test_api_authentication(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        client = TestClient(TestServer(create_application(controller, token="secret")))
        await client.start_server()
        try:
            denied = await client.get("/api/v1/state")
            assert denied.status == 401
            allowed = await client.get("/api/v1/state", headers={"Authorization": "Bearer secret"})
            assert allowed.status == 200
        finally:
            await client.close()
    run(scenario())


def test_default_authentication_policy():
    assert DEFAULT_AUTH_TOKEN == "cutter"
    assert _effective_auth_token("127.0.0.1", None, False) is None
    assert _effective_auth_token("localhost", None, False) is None
    assert _effective_auth_token("0.0.0.0", None, False) == "cutter"
    assert _effective_auth_token("0.0.0.0", None, True) is None
    assert _effective_auth_token("127.0.0.1", "secret", False) == "secret"


def test_configuration_round_trip(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path), config_path=str(tmp_path / "web.json"))
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            profile = {"target_override": "stm32f103rc", "gpio": {"swclk": 11, "swdio": 8}}
            saved = await client.put("/api/v1/config", json=profile, headers=CSRF)
            assert saved.status == 200
            loaded = await client.get("/api/v1/config")
            assert await loaded.json() == profile
        finally:
            await client.close()
    run(scenario())


def test_browser_debugger_routes(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        controller.debug_start = lambda core, port: {
            "debugger": {"active": True, "core": core}, "port": port}
        controller.debug_frames = lambda: {
            "frames": [{"level": "0", "func": "main"}]}
        controller.debug_select_frame = lambda level: {"selected": level}
        controller.debug_locals = lambda: {
            "variables": [{"name": "counter", "value": "1"}]}
        controller.debug_globals = lambda query, limit: {
            "query": query, "limit": limit, "variables": []}
        controller.debug_variable_children = lambda handle: {
            "handle": handle, "variables": []}
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            started = await client.post(
                "/api/v1/debug/start", json={"core": 1, "port": 4444}, headers=CSRF)
            assert (await started.json())["debugger"]["core"] == 1
            frames = await client.get("/api/v1/debug/frames")
            assert (await frames.json())["frames"][0]["func"] == "main"
            selected = await client.put(
                "/api/v1/debug/frame/2", json={}, headers=CSRF)
            assert (await selected.json())["selected"] == 2
            locals_response = await client.get("/api/v1/debug/variables/locals")
            assert (await locals_response.json())["variables"][0]["name"] == "counter"
            globals_response = await client.get(
                "/api/v1/debug/variables/globals?q=state&limit=12")
            assert (await globals_response.json())["limit"] == 12
            children = await client.get("/api/v1/debug/variables/webvar1/children")
            assert (await children.json())["handle"] == "webvar1"
        finally:
            await client.close()
    run(scenario())


def test_simple_target_control_routes(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        calls = []
        controller.probes = lambda: {
            "boards": [{"unique_id": "probe-1", "info": "CMSIS-DAP"}]}
        controller.targets = lambda query=None: {
            "targets": [{"name": query or "stm32f103rc"}]}
        controller.connect = lambda profile: calls.append(("connect", profile)) or {
            "connected": True, "target": {"name": profile["target_override"]}}
        controller.target_action = lambda action: calls.append(("target", action)) or {
            "target": {"state": action}}
        controller.gdb_start = lambda port, cores: calls.append(("gdb-start", port, cores)) or {
            "gdb": [{"core": 0, "port": port, "running": True}]}
        controller.gdb_stop = lambda: calls.append(("gdb-stop",)) or {"gdb": []}
        controller.disconnect = lambda: calls.append(("disconnect",)) or {
            "connected": False}
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            probes = await client.get("/api/v1/probes")
            assert (await probes.json())["boards"][0]["unique_id"] == "probe-1"
            targets = await client.get("/api/v1/targets?q=stm32")
            assert (await targets.json())["targets"][0]["name"] == "stm32"
            connected = await client.post(
                "/api/v1/session/connect",
                json={"probe": "probe-1", "target_override": "stm32f103rc"},
                headers=CSRF)
            assert (await connected.json())["connected"] is True
            started = await client.post(
                "/api/v1/gdb/start", json={"port": 3333, "cores": [0]}, headers=CSRF)
            assert (await started.json())["gdb"][0]["port"] == 3333
            action = await client.post("/api/v1/target/halt", json={}, headers=CSRF)
            assert (await action.json())["target"]["state"] == "halt"
            stopped = await client.post("/api/v1/gdb/stop", json={}, headers=CSRF)
            assert (await stopped.json())["gdb"] == []
            disconnected = await client.post(
                "/api/v1/session/disconnect", json={}, headers=CSRF)
            assert (await disconnected.json())["connected"] is False
            assert calls == [
                ("connect", {"probe": "probe-1", "target_override": "stm32f103rc"}),
                ("gdb-start", 3333, [0]),
                ("target", "halt"),
                ("gdb-stop",),
                ("disconnect",),
            ]
        finally:
            await client.close()
    run(scenario())


def test_tokenless_mutations_require_csrf_header(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        controller.erase = lambda mode, addresses: {"mode": mode}
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            denied = await client.post("/api/v1/jobs/erase")
            assert denied.status == 403
            assert (await denied.json())["error"]["code"] == "csrf_required"

            missing_mode = await client.post(
                "/api/v1/jobs/erase", json={}, headers=CSRF)
            assert missing_mode.status == 400
            assert (await missing_mode.json())["error"]["code"] == "erase_mode_required"

            allowed = await client.post(
                "/api/v1/jobs/erase", json={"mode": "chip"}, headers=CSRF)
            assert allowed.status == 202
        finally:
            await client.close()
    run(scenario())


def test_verify_job_route_accepts_image_plan(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        controller.verify_images = lambda images, options: {
            "id": "verify-job", "images": images, "options": options}
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            response = await client.post(
                "/api/v1/jobs/verify",
                json={"images": [{"artifact_id": "firmware"}],
                      "options": {"reset_method": "hardware"}},
                headers=CSRF)
            assert response.status == 202
            assert (await response.json())["id"] == "verify-job"
        finally:
            await client.close()
    run(scenario())


def test_cross_origin_requests_are_rejected(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            response = await client.get(
                "/api/v1/state", headers={"Origin": "https://attacker.example"})
            assert response.status == 403
            assert (await response.json())["error"]["code"] == "forbidden_origin"
        finally:
            await client.close()
    run(scenario())


def test_loopback_server_rejects_dns_rebinding_host(tmp_path):
    async def scenario():
        controller = WebController(str(tmp_path))
        client = TestClient(TestServer(create_application(controller)))
        await client.start_server()
        try:
            response = await client.get(
                "/api/v1/state",
                headers={"Host": "attacker.example", "Origin": "http://attacker.example"})
            assert response.status == 403
            assert (await response.json())["error"]["code"] == "forbidden_host"
        finally:
            await client.close()
    run(scenario())

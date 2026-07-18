import asyncio

from aiohttp.test_utils import TestClient, TestServer

from pyocd.web.application import create_application
from pyocd.web.controller import WebController


def run(coro):
    return asyncio.run(coro)


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
                "/api/v1/debug/start", json={"core": 1, "port": 4444})
            assert (await started.json())["debugger"]["core"] == 1
            frames = await client.get("/api/v1/debug/frames")
            assert (await frames.json())["frames"][0]["func"] == "main"
            selected = await client.put("/api/v1/debug/frame/2", json={})
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

"""aiohttp application and versioned API for the pyOCD control centre."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from .controller import WebController, WebError

LOG = logging.getLogger(__name__)
MAX_UPLOAD = 128 * 1024 * 1024
MAX_DUMP = 512 * 1024 * 1024
DEFAULT_AUTH_TOKEN = "cutter"
DEFAULT_GDB_PORT = 3030
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
WEB_CLIENT_TIMEOUT = 3.0


class _WebClients:
    """Track recently active browser tabs from their state-poll heartbeats."""

    def __init__(self, timeout: float = WEB_CLIENT_TIMEOUT):
        self._timeout = timeout
        self._seen: Dict[str, float] = {}

    def touch(self, client_id: Optional[str], now: Optional[float] = None) -> Dict[str, int]:
        current = time.monotonic() if now is None else now
        self._seen = {
            key: seen for key, seen in self._seen.items()
            if current - seen <= self._timeout
        }
        if client_id:
            self._seen[client_id] = current
        total = len(self._seen)
        return {
            "connected": total,
            "others": max(0, total - (1 if client_id in self._seen else 0)),
        }


def _is_loopback_host(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def _effective_auth_token(host: str, token: Optional[str], insecure: bool) -> Optional[str]:
    if token:
        return token
    if insecure or _is_loopback_host(host):
        return None
    return DEFAULT_AUTH_TOKEN


def create_application(
        controller: Optional[WebController] = None, token: Optional[str] = None,
        local_only: bool = True,
        restart_process: Optional[Callable[[], None]] = None,
        ssdp=None):
    try:
        from aiohttp import web
    except ImportError as exc:
        raise RuntimeError(
            "The web interface requires aiohttp; reinstall pyOCD to restore its dependencies") from exc

    ctrl = controller or WebController()
    web_clients = _WebClients()

    def default_restart_process() -> None:
        # Replace the process instead of attempting to unwind possibly wedged
        # probe/GDB threads. This also preserves all original command options.
        argv = [sys.executable, "-m", "pyocd", *sys.argv[1:]]
        if getattr(sys, "frozen", False):
            argv = [sys.executable, *sys.argv[1:]]
        os.execv(sys.executable, argv)

    restart = restart_process or default_restart_process

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except WebError as exc:
            return web.json_response(
                {"error": {"code": exc.code, "message": str(exc)}}, status=exc.status)
        except json.JSONDecodeError:
            return web.json_response(
                {"error": {"code": "invalid_json", "message": "Invalid JSON body"}}, status=400)
        except web.HTTPException:
            # Preserve expected HTTP responses such as a missing favicon.
            raise
        except Exception as exc:
            LOG.exception("web request failed")
            return web.json_response(
                {"error": {"code": "internal_error", "message": str(exc)}}, status=500)

    @web.middleware
    async def authenticate(request, handler):
        if token and request.path.startswith("/api/"):
            supplied = request.headers.get(
                "Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(supplied, token):
                return web.json_response(
                    {"error": {"code": "unauthorized", "message": "Authentication required"}}, status=401)
        return await handler(request)

    @web.middleware
    async def browser_security(request, handler):
        """Reject cross-origin browser access and simple cross-site mutations."""
        request_hostname = urlsplit("//" + request.host).hostname
        if local_only and request_hostname not in {"127.0.0.1", "localhost", "::1"}:
            return web.json_response(
                {"error": {"code": "forbidden_host", "message": "Non-loopback Host is not allowed"}},
                status=403)
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != request.host.lower():
            return web.json_response(
                {"error": {"code": "forbidden_origin", "message": "Cross-origin access is not allowed"}},
                status=403)
        if (not token
                and request.path.startswith("/api/")
                and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and request.headers.get("X-pyOCD-CSRF") != "1"):
            return web.json_response(
                {"error": {"code": "csrf_required", "message": "Missing pyOCD request header"}},
                status=403)
        return await handler(request)

    app = web.Application(
        middlewares=[
            errors,
            browser_security,
            authenticate],
        client_max_size=MAX_UPLOAD)

    async def body(request) -> Dict[str, Any]:
        return await request.json() if request.can_read_body else {}

    async def state(request):
        snapshot = ctrl.snapshot()
        snapshot["web_clients"] = web_clients.touch(
            request.headers.get("X-pyOCD-Client-ID"))
        return web.json_response(snapshot)

    async def configuration(request):
        if request.method == "GET":
            return web.json_response(ctrl.snapshot()["profile"])
        return web.json_response(ctrl.save_profile(await body(request)))

    async def health(request):
        return web.json_response({"status": "ok"})

    async def runtime_restart(request):
        # Deliberately does not touch the controller or its lock. This endpoint
        # must remain usable when a probe operation has wedged a worker thread.
        asyncio.get_running_loop().call_later(0.25, restart)
        return web.json_response({"accepted": True, "action": "restart"}, status=202)

    async def system_info(request):
        return web.json_response(await asyncio.to_thread(ctrl.system_info))

    async def system_power(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.system_power, request.match_info["action"]), status=202)

    async def update_check(request):
        return web.json_response(await asyncio.to_thread(ctrl.check_for_update))

    async def probes(request):
        return web.json_response(await asyncio.to_thread(ctrl.probes))

    async def targets(request):
        return web.json_response(await asyncio.to_thread(ctrl.targets, request.query.get("q")))

    async def target_pack_info(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.target_pack_info, request.match_info["target"]))

    async def run_pack_sequence(request):
        data = await body(request)
        return web.json_response(await asyncio.to_thread(
            ctrl.run_pack_sequence, str(data.get("name", "")), data.get("pname")))

    async def unlock_target(request):
        return web.json_response(await asyncio.to_thread(ctrl.unlock_target))

    async def pack_search(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.pack_search, request.query.get("q", ""), int(request.query.get("limit", 100))))

    async def pack_update(request):
        return web.json_response(await asyncio.to_thread(ctrl.pack_update))

    async def pack_install(request):
        data = await body(request)
        return web.json_response(await asyncio.to_thread(ctrl.pack_install, str(data.get("device", ""))))

    async def connect(request):
        return web.json_response(await asyncio.to_thread(ctrl.connect, await body(request)))

    async def disconnect(request):
        return web.json_response(await asyncio.to_thread(ctrl.disconnect))

    async def probe_reset(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.pulse_probe_reset, await body(request)))

    async def target_action(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.target_action, request.match_info["action"]))

    async def registers(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.registers, int(request.match_info["core"])))

    async def memory(request):
        data = await body(request)
        address = int(str(data["address"]), 0)
        length = int(data["length"])
        result = await asyncio.to_thread(ctrl.read_memory, address, length)
        return web.json_response(
            {"address": address, "length": length, "data": result.hex()})

    async def dump(request):
        data = await body(request)
        address = int(str(data["address"]), 0)
        length = int(data["length"])
        if length < 1 or length > MAX_DUMP:
            raise WebError("invalid_dump_length", "Dump length must be between 1 byte and 512 MiB")
        chunk_size = min(ctrl.MAX_MEMORY_READ, length)
        first = await asyncio.to_thread(ctrl.read_memory, address, chunk_size)
        response = web.StreamResponse(headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="memory-{address:08x}-{length}.bin"',
            "Content-Length": str(length),
        })
        await response.prepare(request)
        await response.write(first)
        offset = len(first)
        while offset < length:
            count = min(ctrl.MAX_MEMORY_READ, length - offset)
            chunk = await asyncio.to_thread(ctrl.read_memory, address + offset, count)
            await response.write(chunk)
            offset += len(chunk)
        await response.write_eof()
        return response

    async def upload(request):
        reader = await request.multipart()
        part = await reader.next()
        if part is None or not part.filename:
            raise WebError("missing_file", "Select a file to upload")
        chunks, total = [], 0
        while True:
            chunk = await part.read_chunk()
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD:
                raise WebError(
                    "upload_too_large",
                    "Upload exceeds 128 MiB",
                    413)
            chunks.append(chunk)
        return web.json_response(await asyncio.to_thread(ctrl.upload, part.filename, b"".join(chunks)), status=201)

    async def program(request):
        data = await body(request)
        if data.get("images") is not None:
            job = ctrl.program_images(data["images"], data.get("options") or {})
        else:
            job = ctrl.program(data["artifact_id"], data.get("options") or {})
        return web.json_response(job, status=202)

    async def erase(request):
        data = await body(request)
        if "mode" not in data:
            raise WebError("erase_mode_required", "Select an explicit erase mode")
        return web.json_response(ctrl.erase(
            data["mode"], data.get("addresses")), status=202)

    async def attach_elf(request):
        data = await body(request)
        return web.json_response(await asyncio.to_thread(ctrl.attach_elf, data["artifact_id"]))

    async def stack(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.stack, int(request.query.get("core", 0))))

    async def gdb_start(request):
        data = await body(request)
        return web.json_response(await asyncio.to_thread(ctrl.gdb_start, int(data.get("port", DEFAULT_GDB_PORT)), data.get("cores")))

    async def gdb_stop(request):
        return web.json_response(await asyncio.to_thread(ctrl.gdb_stop))

    async def debug_start(request):
        data = await body(request)
        return web.json_response(await asyncio.to_thread(
            ctrl.debug_start, int(data.get("core", 0)), int(data.get("port", DEFAULT_GDB_PORT))))

    async def debug_stop(request):
        return web.json_response(await asyncio.to_thread(ctrl.debug_stop))

    async def debug_frames(request):
        return web.json_response(await asyncio.to_thread(ctrl.debug_frames))

    async def debug_select_frame(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.debug_select_frame, int(request.match_info["level"])))

    async def debug_locals(request):
        return web.json_response(await asyncio.to_thread(ctrl.debug_locals))

    async def debug_globals(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.debug_globals, request.query.get("q", ""),
            int(request.query.get("limit", 50))))

    async def debug_children(request):
        return web.json_response(await asyncio.to_thread(
            ctrl.debug_variable_children, request.match_info["handle"]))

    async def console(request):
        data = await body(request)
        return web.json_response({"output": await asyncio.to_thread(ctrl.console, data.get("command", ""))})

    async def logs(request):
        return web.json_response(ctrl.logs(float(request.query.get("after", 0))))

    async def clear_logs(request):
        ctrl.clear_logs()
        return web.Response(status=204)

    async def events(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        previous = None
        try:
            while not ws.closed:
                current = json.dumps(
                    ctrl.snapshot(), sort_keys=True, default=str)
                if current != previous:
                    await ws.send_str(current)
                    previous = current
                await asyncio.sleep(.5)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        return ws

    routes = [
        web.get("/api/v1/state", state), web.get("/api/v1/health", health),
        web.post("/api/v1/runtime/restart", runtime_restart),
        web.get("/api/v1/system", system_info),
        web.post("/api/v1/system/power/{action}", system_power),
        web.get("/api/v1/update/check", update_check),
        web.get("/api/v1/config", configuration), web.put("/api/v1/config", configuration),
        web.get("/api/v1/probes", probes), web.get("/api/v1/targets", targets),
        web.get("/api/v1/targets/{target}/pack", target_pack_info),
        web.post("/api/v1/pack/sequences/run", run_pack_sequence),
        web.get("/api/v1/packs/devices", pack_search),
        web.post("/api/v1/packs/index", pack_update),
        web.post("/api/v1/packs/install", pack_install),
        web.post("/api/v1/session/connect",
                 connect), web.post("/api/v1/session/disconnect", disconnect),
        web.post("/api/v1/probe/reset", probe_reset),
        web.post("/api/v1/target/unlock", unlock_target),
        web.post("/api/v1/target/{action}", target_action), web.get(
            "/api/v1/cores/{core}/registers", registers),
        web.post("/api/v1/memory/read",
                 memory), web.post("/api/v1/memory/dump", dump),
        web.post("/api/v1/artifacts",
                 upload), web.post("/api/v1/elf/attach", attach_elf),
        web.post("/api/v1/jobs/program",
                 program), web.post("/api/v1/jobs/erase", erase),
        web.get("/api/v1/stack", stack), web.post("/api/v1/gdb/start",
                                                  gdb_start), web.post("/api/v1/gdb/stop", gdb_stop),
        web.post("/api/v1/debug/start", debug_start),
        web.post("/api/v1/debug/stop", debug_stop),
        web.get("/api/v1/debug/frames", debug_frames),
        web.put("/api/v1/debug/frame/{level}", debug_select_frame),
        web.get("/api/v1/debug/variables/locals", debug_locals),
        web.get("/api/v1/debug/variables/globals", debug_globals),
        web.get("/api/v1/debug/variables/{handle}/children", debug_children),
        web.post("/api/v1/console",
                 console), web.get("/api/v1/logs", logs),
        web.delete("/api/v1/logs", clear_logs),
        web.get("/api/v1/ws/events", events),
    ]
    app.add_routes(routes)
    static = Path(__file__).with_name("static")

    async def index(request):
        return web.FileResponse(static / "index.html")

    async def cleanup(_app):
        await asyncio.to_thread(ctrl.close)

    app.router.add_get("/", index)
    app.router.add_static("/assets", static / "assets", show_index=False)
    if ssdp is not None:
        async def ssdp_description(request):
            return web.Response(
                body=ssdp.device_description(),
                content_type="text/xml",
                charset="utf-8")

        app.router.add_get(ssdp.description_path, ssdp_description)
    app.on_cleanup.append(cleanup)
    return app


def run_webserver(host: str = "127.0.0.1", port: int = 8080, token: Optional[str] = None,
                  artifact_dir: Optional[str] = None, unsafe_console: bool = False,
                  insecure: bool = False, gdb_executable: Optional[str] = None,
                  force_rpi: bool = False, mdns: bool = False, ssdp: bool = False) -> None:
    from aiohttp import web
    effective_token = _effective_auth_token(host, token, insecure)
    controller = WebController(
        artifact_dir=artifact_dir,
        unsafe_console=unsafe_console,
        gdb_executable=gdb_executable,
        force_rpi=force_rpi,
        serve_local_only=_is_loopback_host(host))
    advertiser = None
    ssdp_advertiser = None
    try:
        if mdns or ssdp:
            from .mdns import MdnsAdvertiser
            from .ssdp import SsdpAdvertiser
            if mdns:
                advertiser = MdnsAdvertiser(controller.interface_name, host, port)
            if ssdp:
                try:
                    ssdp_advertiser = SsdpAdvertiser(controller.interface_name, host, port)
                except (OSError, RuntimeError, ValueError) as exc:
                    LOG.warning("SSDP advertisement unavailable: %s", exc)
    except Exception:
        if advertiser is not None:
            advertiser.close()
        controller.close()
        raise
    LOG.info("pyOCD web interface listening on http://%s:%d", host, port)
    try:
        web.run_app(
            create_application(
                controller,
                effective_token,
                local_only=_is_loopback_host(host),
                ssdp=ssdp_advertiser),
            host=host,
            port=port,
            print=None,
            access_log=None)
    finally:
        if ssdp_advertiser is not None:
            ssdp_advertiser.close()
        if advertiser is not None:
            advertiser.close()

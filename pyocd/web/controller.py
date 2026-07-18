"""Stateful, thread-safe orchestration for the pyOCD web interface."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from enum import Enum
from io import StringIO
import logging
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from ..commands.execution_context import CommandExecutionContext
from ..core.helpers import ConnectHelper
from ..core.session import Session
from ..flash.eraser import FlashEraser
from ..flash.file_programmer import FileProgrammer
from ..gdbserver import GDBServer
from ..tools.lists import ListGenerator
from .gdb_mi import GdbMiClient, GdbMiError, MiRecord, quote_mi


class WebError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BUSY = "busy"
    ERROR = "error"


@dataclass
class Job:
    id: str
    kind: str
    state: str = "queued"
    progress: float = 0.0
    message: str = "Waiting"
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    error: Optional[str] = None


class WebLogHandler(logging.Handler):
    """Small in-memory log sink for the browser log viewer."""

    def __init__(self, limit: int = 2000):
        super().__init__()
        self.limit = limit
        self.records: list[Dict[str, Any]] = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        item = {"time": record.created, "level": record.levelname,
                "logger": record.name, "message": self.format(record)}
        self.records.append(item)
        if len(self.records) > self.limit:
            del self.records[:len(self.records) - self.limit]


class WebController:
    """Owns the sole active Session and serialises access to the probe."""

    MAX_MEMORY_READ = 1024 * 1024

    def __init__(
            self, artifact_dir: Optional[str] = None, unsafe_console: bool = False,
            gdb_executable: Optional[str] = None):
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pyocd-web")
        self._session: Optional[Session] = None
        self._console: Optional[CommandExecutionContext] = None
        self._state = ConnectionState.DISCONNECTED
        self._error: Optional[str] = None
        self._profile: Dict[str, Any] = {}
        self._gdb: Dict[int, GDBServer] = {}
        self._debugger: Optional[GdbMiClient] = None
        self._debugger_state = "inactive"
        self._debugger_stopped = threading.Event()
        self._debugger_core = 0
        self._gdb_executable = gdb_executable
        self._target_locked: Optional[bool] = None
        self._var_objects: set[str] = set()
        self._next_var_object = 0
        self._jobs: Dict[str, Job] = {}
        self._artifacts: Dict[str, Dict[str, Any]] = {}
        self._attached_elf_artifact: Optional[str] = None
        self._log_handler = WebLogHandler()
        logging.getLogger("pyocd").addHandler(self._log_handler)
        self._artifact_dir = Path(
            artifact_dir or tempfile.mkdtemp(
                prefix="pyocd-web-"))
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self.unsafe_console = unsafe_console

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._disconnect_locked()
            self._cleanup_attached_elf_locked()
            self._cleanup_all_elf_artifacts_locked()
        logging.getLogger("pyocd").removeHandler(self._log_handler)

    def logs(self, after: float = 0.0) -> Dict[str, Any]:
        with self._lock:
            records = [item for item in self._log_handler.records if item["time"] > after]
            return {"records": records, "count": len(self._log_handler.records)}

    def clear_logs(self) -> None:
        with self._lock:
            self._log_handler.records.clear()

    def _record_activity_locked(self, kind: str, message: str) -> None:
        now = time.time()
        job = Job(uuid.uuid4().hex, kind, state="completed", progress=1.0,
                  message=message, created_at=now, finished_at=now)
        self._jobs[job.id] = job

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            result: Dict[str, Any] = {
                "state": self._state.value,
                "error": self._error,
                "profile": self._profile,
                "connected": self._session is not None and self._session.is_open,
                "gdb": [{"core": c, "port": s.port, "running": s.is_alive(),
                         "clients": len(s.client_sessions)} for c, s in self._gdb.items()],
                "debugger": {
                    "active": self._debugger is not None and self._debugger.is_alive,
                    "state": self._debugger_state,
                    "core": self._debugger_core,
                    "owner": "browser" if self._debugger else (
                        "external" if self._gdb else None),
                    "executable": self._debugger.executable if self._debugger else None,
                },
                "jobs": [asdict(j) for j in sorted(self._jobs.values(), key=lambda x: x.created_at, reverse=True)[:20]],
                "artifacts": [{k: v for k, v in item.items() if k != "path"}
                              for item in self._artifacts.values()],
            }
            if self._session and self._session.is_open:
                target, probe = self._session.target, self._session.probe
                # A GDB server accesses the probe from its own thread. Never
                # perform competing hardware reads from the state poller.
                if self._debugger:
                    target_state = self._debugger_state
                elif self._gdb:
                    server = next(iter(self._gdb.values()))
                    target_state = "running" if server.is_target_running else "halted"
                else:
                    try:
                        target_state = target.get_state().name.lower()
                    except Exception:
                        target_state = "unknown"
                result["target"] = {
                    "name": self._session.board.target_type,
                    "vendor": getattr(target, "vendor", None),
                    "part_number": getattr(target, "part_number", None),
                    "state": target_state,
                    "locked": self._target_locked if self._gdb else target.is_locked(),
                    "cores": sorted(target.cores.keys()),
                    "selected_core": getattr(target.selected_core, "core_number", 0),
                    "memory_map": [self._region_dict(r) for r in target.memory_map],
                }
                result["probe"] = {
                    "unique_id": probe.unique_id, "vendor": probe.vendor_name,
                    "product": probe.product_name,
                    "protocol": probe.wire_protocol.name.lower() if probe.wire_protocol else None,
                    "frequency": self._session.options.get("frequency"),
                }
            return result

    @staticmethod
    def _region_dict(region: Any) -> Dict[str, Any]:
        return {"name": region.name, "type": region.type.name.lower(), "start": region.start,
                "end": region.end, "length": region.length, "access": region.access,
                "is_boot": region.is_boot_memory}

    def probes(self) -> Dict[str, Any]:
        obj = ListGenerator.list_probes()
        if not sys.platform.startswith("linux"):
            obj["boards"] = [probe for probe in obj["boards"]
                             if probe.get("vendor_name") != "Raspberry Pi"
                             or probe.get("product_name") != "GPIO SWD"]
        # GPIO is intentionally not auto-discovered, so explicitly offer it
        # when present.
        if sys.platform.startswith("linux") and Path("/dev/gpiomem").exists():
            try:
                gpio = ConnectHelper.get_all_connected_probes(
                    blocking=False, unique_id="rpi-gpio:")
                for probe in gpio:
                    obj["boards"].append({"unique_id": "rpi-gpio:", "info": "Raspberry Pi GPIO SWD",
                                          "board_vendor": "Raspberry Pi", "board_name": "GPIO header", "target": "cortex_m",
                                          "vendor_name": probe.vendor_name, "product_name": probe.product_name})
            except Exception:
                pass
        return obj

    def targets(self, query: Optional[str] = None) -> Dict[str, Any]:
        return ListGenerator.list_targets(name_filter=query)

    def connect(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._state == ConnectionState.BUSY:
                raise WebError("operation_busy", "Wait for the active operation to finish", 409)
            self._disconnect_locked()
            self._state, self._error = ConnectionState.CONNECTING, None
            try:
                selector = profile.get("probe") or None
                if selector is None:
                    probes = ConnectHelper.get_all_connected_probes(blocking=False)
                    probe = probes[0] if probes else None
                else:
                    probe = ConnectHelper.choose_probe(
                        blocking=False, return_first=True, unique_id=selector)
                if probe is None:
                    raise WebError(
                        "probe_not_found",
                        "The selected debug probe is not available",
                        404)
                options = dict(profile.get("options") or {})
                for key in ("target_override", "frequency",
                            "connect_mode", "project_dir", "pack"):
                    if profile.get(key) is not None:
                        options[key] = profile[key]
                gpio = profile.get("gpio") or {}
                for key in ("swclk", "swdio", "nreset", "swdio_dir",
                            "restore_pins", "wait_retries"):
                    if key in gpio:
                        options["rpi_gpio." + key] = gpio[key]
                session = Session(probe, options=options)
                session.open()
                try:
                    self._target_locked = session.target.is_locked()
                except Exception:
                    self._target_locked = None
                output = StringIO()
                console = CommandExecutionContext(output_stream=output)
                console.attach_session(session)
                self._session, self._console = session, console
                self._profile = profile
                self._state = ConnectionState.CONNECTED
                self._record_activity_locked("connect", "Target connected")
                return self.snapshot()
            except Exception as exc:
                self._state, self._error = ConnectionState.ERROR, str(exc)
                if self._session:
                    self._session.close()
                self._session, self._console = None, None
                raise

    def disconnect(self) -> Dict[str, Any]:
        with self._lock:
            if self._state == ConnectionState.BUSY:
                raise WebError("operation_busy", "Wait for the active operation to finish", 409)
            was_connected = self._session is not None
            self._disconnect_locked()
            if was_connected:
                self._record_activity_locked("disconnect", "Target disconnected")
            return self.snapshot()

    def _disconnect_locked(self) -> None:
        self._stop_gdb_locked()
        if self._session:
            self._session.close()
        self._session, self._console = None, None
        self._target_locked = None
        self._cleanup_attached_elf_locked()
        self._state, self._error = ConnectionState.DISCONNECTED, None

    def _require_session(self) -> Session:
        if not self._session or not self._session.is_open:
            raise WebError("not_connected", "Connect to a target first", 409)
        return self._session

    def _require_exclusive(self) -> Session:
        if self._state == ConnectionState.BUSY:
            raise WebError("operation_busy", "Another target operation is running", 409)
        if any(s.is_alive() for s in self._gdb.values()):
            raise WebError(
                "gdb_active",
                "Stop the GDB server before using direct target controls",
                409)
        return self._require_session()

    def target_action(self, action: str) -> Dict[str, Any]:
        with self._lock:
            if self._debugger:
                commands = {
                    "halt": "-exec-interrupt",
                    "resume": "-exec-continue",
                    "step": "-exec-next",
                    "reset": f"-interpreter-exec console {quote_mi('monitor reset')}",
                    "reset-halt": f"-interpreter-exec console {quote_mi('monitor reset halt')}",
                }
                if action not in commands:
                    raise WebError("bad_action", "Unknown target action")
                try:
                    if action == "halt":
                        self._debugger_stopped.clear()
                    elif action in ("resume", "step"):
                        self._debugger_state = "running"
                        self._debugger_stopped.clear()
                    self._debugger.command(commands[action])
                    if action == "halt":
                        # ^done acknowledges the interrupt request; *stopped is
                        # asynchronous and can arrive just afterwards. Wait for
                        # it so the response and UI state agree with the core.
                        if not self._debugger_stopped.wait(1.0):
                            # Some older GDB/pyOCD combinations omit the async
                            # notification even though inspection is available.
                            # A successful frame query is an authoritative GDB-
                            # side confirmation that the inferior has stopped.
                            self._debugger.command("-stack-info-frame", timeout=3.0)
                            self._debugger_state = "stopped"
                            self._debugger_stopped.set()
                    elif action == "reset-halt":
                        self._debugger_state = "stopped"
                        self._debugger_stopped.set()
                except GdbMiError as exc:
                    raise WebError("gdb_command_failed", str(exc), 409) from exc
                self._record_activity_locked(action, f"Target {action} requested through GDB")
                return self.snapshot()
            target = self._require_exclusive().target
            actions: Dict[str, Callable[[], None]] = {"halt": target.halt, "resume": target.resume,
                                                      "step": target.step, "reset": target.reset, "reset-halt": target.reset_and_halt}
            if action not in actions:
                raise WebError("bad_action", "Unknown target action")
            actions[action]()
            self._record_activity_locked(action, f"Target {action} completed")
            return self.snapshot()

    def registers(self, core: int = 0) -> Dict[str, Any]:
        with self._lock:
            if self._debugger:
                try:
                    names = self._debugger.command("-data-list-register-names").get(
                        "register-names", [])
                    raw_values = self._debugger.command(
                        "-data-list-register-values x").get("register-values", [])
                except GdbMiError as exc:
                    raise WebError("gdb_command_failed", str(exc), 409) from exc
                values: Dict[str, Any] = {}
                for wrapped in raw_values:
                    item = wrapped.get("register-value", wrapped) if isinstance(wrapped, dict) else {}
                    try:
                        number = int(item.get("number", -1))
                        name = names[number] if number < len(names) else str(number)
                        value = item.get("value", "")
                        values[name] = int(value, 0) if str(value).startswith("0x") else value
                    except (TypeError, ValueError):
                        continue
                return {"core": self._debugger_core, "registers": values}
            target = self._require_exclusive().target.cores[core]
            names = ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11",
                     "r12", "sp", "lr", "pc", "xpsr", "msp", "psp", "control", "primask", "basepri", "faultmask"]
            values = target.read_core_registers_raw(names)
            return {"core": core, "registers": dict(zip(names, values))}

    def read_memory(self, address: int, length: int) -> bytes:
        if length < 1 or length > self.MAX_MEMORY_READ:
            raise WebError("invalid_length",
                           f"Length must be between 1 and {self.MAX_MEMORY_READ} bytes")
        with self._lock:
            if self._debugger:
                try:
                    result = self._debugger.command(
                        f"-data-read-memory-bytes 0x{address:x} {length}")
                    chunks = result.get("memory", [])
                    contents = "".join(
                        str((item.get("memory", item) if isinstance(item, dict) else {}).get(
                            "contents", "")) for item in chunks)
                    data = bytes.fromhex(contents)
                    if len(data) != length:
                        raise GdbMiError(
                            f"GDB returned {len(data)} of {length} requested bytes")
                    return data
                except (GdbMiError, ValueError) as exc:
                    raise WebError("gdb_memory_failed", str(exc), 409) from exc
            data = self._require_exclusive().target.read_memory_block8(address, length)
            return bytes(data)

    def upload(self, name: str, content: bytes) -> Dict[str, Any]:
        artifact_id = uuid.uuid4().hex
        safe_name = Path(name).name or "firmware.bin"
        path = self._artifact_dir / f"{artifact_id}-{safe_name}"
        path.write_bytes(content)
        item = {
            "id": artifact_id,
            "name": safe_name,
            "size": len(content),
            "uploaded_at": time.time()}
        self._artifacts[artifact_id] = {**item, "path": str(path)}
        return item

    def attach_elf(self, artifact_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._require_exclusive()
            artifact = self._artifacts.get(artifact_id)
            if not artifact:
                raise WebError(
                    "artifact_not_found",
                    "Uploaded file not found",
                    404)
            if Path(artifact["name"]).suffix.lower() not in (".elf", ".axf"):
                raise WebError("invalid_elf", "Only ELF or AXF files can be attached")
            if self._attached_elf_artifact != artifact_id:
                self._cleanup_attached_elf_locked()
            session.target.elf = artifact["path"]
            self._attached_elf_artifact = artifact_id
            self._record_activity_locked("elf", f"Attached {artifact['name']}")
            return {"attached": True, "artifact": artifact_id,
                    "name": artifact["name"]}

    def _cleanup_attached_elf_locked(self) -> None:
        artifact_id = self._attached_elf_artifact
        self._attached_elf_artifact = None
        if artifact_id is None:
            return
        artifact = self._artifacts.pop(artifact_id, None)
        if artifact:
            try:
                Path(artifact["path"]).unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning(
                    "Unable to remove temporary ELF file %s", artifact["path"])

    def _cleanup_all_elf_artifacts_locked(self) -> None:
        for artifact_id, artifact in list(self._artifacts.items()):
            if Path(artifact["name"]).suffix.lower() not in (".elf", ".axf"):
                continue
            try:
                Path(artifact["path"]).unlink(missing_ok=True)
                self._artifacts.pop(artifact_id, None)
            except OSError:
                logging.getLogger(__name__).warning(
                    "Unable to remove temporary ELF file %s", artifact["path"])

    def start_job(self, kind: str, fn: Callable[[
                  Job], None]) -> Dict[str, Any]:
        job = Job(uuid.uuid4().hex, kind)
        self._jobs[job.id] = job

        def runner() -> None:
            with self._lock:
                try:
                    self._require_exclusive()
                    self._state, job.state, job.message = ConnectionState.BUSY, "running", "Starting"
                except Exception as exc:
                    job.state, job.error, job.message = "failed", str(exc), "Failed"
                    job.finished_at = time.time()
                    return
            try:
                fn(job)
                with self._lock:
                    job.progress, job.state, job.message = 1.0, "completed", "Completed"
            except Exception as exc:
                with self._lock:
                    job.state, job.error, job.message = "failed", str(exc), "Failed"
            finally:
                with self._lock:
                    job.finished_at = time.time()
                    self._state = ConnectionState.CONNECTED if self._session else ConnectionState.DISCONNECTED

        self._executor.submit(runner)
        return asdict(job)

    def program(self, artifact_id: str,
                options: Dict[str, Any]) -> Dict[str, Any]:
        return self.program_images([{"artifact_id": artifact_id,
                                     "base_address": options.get("base_address")}], options)

    def program_images(self, images: list[Dict[str, Any]],
                       options: Dict[str, Any]) -> Dict[str, Any]:
        if not images or len(images) > 2:
            raise WebError("invalid_images", "Select one or two firmware images")
        artifacts = []
        for image in images:
            artifact = self._artifacts.get(image.get("artifact_id"))
            if not artifact:
                raise WebError("artifact_not_found", "Uploaded file not found", 404)
            artifacts.append((artifact, image))

        def work(job: Job) -> None:
            session = self._require_session()
            count = len(artifacts)
            for index, (artifact, image) in enumerate(artifacts):
                def progress(value: float, image_index: int = index) -> None:
                    total = (image_index + float(value)) / count
                    job.progress = total
                    job.message = f"Programming {artifact['name']} · {total * 100:.0f}%"
                # Only the first image may use chip erase. Following images use sector erase
                # so a bootloader programmed first cannot be erased by the application image.
                erase_mode = options.get("erase", "auto") if index == 0 else "sector"
                programmer = FileProgrammer(
                    session, progress=progress, chip_erase=erase_mode,
                    trust_crc=options.get("trust_crc", False),
                    keep_unwritten=options.get("keep_unwritten", False))
                kwargs = {}
                base_address = image.get("base_address")
                if base_address not in (None, ""):
                    kwargs["base_address"] = int(str(base_address), 0)
                programmer.program(artifact["path"], **kwargs)
            post = options.get("post_action", "reset")
            if post == "reset":
                session.target.reset()
            elif post == "halt":
                session.target.reset_and_halt()
            elif post == "run":
                session.target.reset()
                session.target.resume()
        return self.start_job("program", work)

    def erase(self, mode: str, addresses: Any = None) -> Dict[str, Any]:
        modes = {
            "chip": FlashEraser.Mode.CHIP,
            "mass": FlashEraser.Mode.MASS,
            "sector": FlashEraser.Mode.SECTOR}
        if mode not in modes:
            raise WebError("invalid_erase_mode",
                           "Erase mode must be chip, mass, or sector")
        return self.start_job("erase", lambda job: FlashEraser(
            self._require_session(), modes[mode]).erase(addresses))

    def gdb_start(self, port: int = 3333,
                  cores: Optional[list[int]] = None) -> Dict[str, Any]:
        with self._lock:
            if self._debugger:
                raise WebError(
                    "browser_debugger_active",
                    "Stop the browser debugger before enabling an external GDB server",
                    409)
            session = self._require_session()
            if self._gdb:
                return self.snapshot()
            session.options.set("gdbserver_port", port)
            for core in cores or sorted(session.target.cores.keys()):
                server = GDBServer(session, core=core)
                session.gdbservers[core] = server
                self._gdb[core] = server
                server.start()
            self._record_activity_locked("gdb", "GDB server started")
            return self.snapshot()

    def gdb_stop(self) -> Dict[str, Any]:
        with self._lock:
            was_running = bool(self._gdb)
            self._stop_gdb_locked()
            if was_running:
                self._record_activity_locked("gdb", "GDB server stopped")
            return self.snapshot()

    def _stop_gdb_locked(self) -> None:
        self._stop_debugger_locked()
        for server in list(self._gdb.values()):
            server.stop()
        if self._session:
            self._session.gdbservers.clear()
        self._gdb.clear()

    def _debugger_event(self, record: MiRecord) -> None:
        if record.prefix != "*":
            return
        # A single string assignment is atomic in CPython. Do not acquire the
        # controller lock here: GDB can emit an async stop record immediately
        # before a command result while the command caller owns that lock.
        if record.cls == "stopped":
            self._debugger_state = "stopped"
            self._debugger_stopped.set()
        elif record.cls == "running":
            self._debugger_state = "running"
            self._debugger_stopped.clear()

    def debug_start(self, core: int = 0, port: int = 3333) -> Dict[str, Any]:
        """Start a pyOCD GDB server and the browser-owned GDB/MI client."""
        with self._lock:
            session = self._require_session()
            if self._debugger and self._debugger.is_alive:
                return self.snapshot()
            if self._gdb:
                raise WebError(
                    "external_gdb_active",
                    "Stop the external GDB server before starting the browser debugger",
                    409)
            if core not in session.target.cores:
                raise WebError("invalid_core", f"Core {core} is not available")
            artifact = self._artifacts.get(self._attached_elf_artifact or "")
            if not artifact:
                raise WebError(
                    "elf_required",
                    "Attach an ELF or AXF file with debug symbols before starting the browser debugger",
                    409)
            client: Optional[GdbMiClient] = None
            try:
                # Enter a known state before the RSP client connects. Although
                # GDBServer also requests a halt when accepting a client, doing
                # it here avoids an attach-time interrupt race in all-stop mode
                # and lets us report probe failures before GDB is involved.
                target_core = session.target.cores[core]
                target_core.halt()
                if target_core.get_state() != target_core.State.HALTED:
                    raise WebError(
                        "target_did_not_halt",
                        "The target did not enter the halted state before GDB attach",
                        409)
                self._debugger_state = "stopped"
                self._debugger_stopped.set()
                session.options.set("gdbserver_port", port)
                server = GDBServer(session, core=core)
                session.gdbservers[core] = server
                self._gdb[core] = server
                server.start()
                client = GdbMiClient(self._gdb_executable, self._debugger_event)
                client.start()
                client.command(f"-file-exec-and-symbols {quote_mi(artifact['path'])}")
                client.command(
                    f"-target-select remote 127.0.0.1:{server.port}", timeout=60.0)
                self._debugger = client
                self._debugger_core = core
                self._debugger_state = "stopped"
                self._record_activity_locked("debugger", "Browser GDB debugger started")
                return self.snapshot()
            except Exception as exc:
                if client:
                    diagnostics = (client.stderr[-3:] + client.stream[-5:])
                    client.close(force=True)
                else:
                    diagnostics = []
                for server in list(self._gdb.values()):
                    server.stop()
                session.gdbservers.clear()
                self._gdb.clear()
                if isinstance(exc, WebError):
                    raise
                detail = str(exc)
                if diagnostics:
                    detail += " · GDB: " + " | ".join(diagnostics)
                raise WebError("gdb_start_failed", detail, 409) from exc

    def debug_stop(self) -> Dict[str, Any]:
        with self._lock:
            was_running = self._debugger is not None
            self._stop_debugger_locked()
            for server in list(self._gdb.values()):
                server.stop()
            if self._session:
                self._session.gdbservers.clear()
            self._gdb.clear()
            if was_running:
                self._record_activity_locked("debugger", "Browser GDB debugger stopped")
            return self.snapshot()

    def _stop_debugger_locked(self) -> None:
        self._clear_var_objects_locked()
        if self._debugger:
            self._debugger.close()
        self._debugger = None
        self._debugger_state = "inactive"
        self._debugger_stopped.clear()

    def _require_debugger(self) -> GdbMiClient:
        if not self._debugger or not self._debugger.is_alive:
            raise WebError("debugger_inactive", "Start the browser debugger first", 409)
        if self._debugger_state == "running":
            raise WebError("target_running", "Halt the target to inspect debug state", 409)
        return self._debugger

    @staticmethod
    def _unwrap_list(items: Any, key: str) -> list[Dict[str, Any]]:
        result = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                value = item.get(key, item)
                if isinstance(value, dict):
                    result.append(value)
        return result

    def debug_frames(self) -> Dict[str, Any]:
        with self._lock:
            debugger = self._require_debugger()
            try:
                frames = self._unwrap_list(
                    debugger.command("-stack-list-frames").get("stack", []), "frame")
                return {"core": self._debugger_core, "frames": frames}
            except GdbMiError as exc:
                raise WebError("gdb_frames_failed", str(exc), 409) from exc

    def debug_select_frame(self, level: int) -> Dict[str, Any]:
        with self._lock:
            debugger = self._require_debugger()
            if level < 0:
                raise WebError("invalid_frame", "Frame level cannot be negative")
            try:
                debugger.command(f"-stack-select-frame {level}")
                self._clear_var_objects_locked()
                return {"selected": level}
            except GdbMiError as exc:
                raise WebError("gdb_frame_failed", str(exc), 409) from exc

    def _new_var_object_locked(self, expression: str) -> Dict[str, Any]:
        debugger = self._require_debugger()
        self._next_var_object += 1
        handle = f"webvar{self._next_var_object}"
        result = debugger.command(
            f"-var-create {handle} @ {quote_mi(expression)}")
        self._var_objects.add(handle)
        return {
            "handle": handle,
            "name": expression,
            "value": result.get("value", ""),
            "type": result.get("type", ""),
            "numchild": int(result.get("numchild", 0)),
        }

    def _clear_var_objects_locked(self) -> None:
        debugger = self._debugger
        if debugger and debugger.is_alive:
            for handle in self._var_objects:
                try:
                    debugger.command(f"-var-delete {handle}", timeout=1.0)
                except GdbMiError:
                    pass
        self._var_objects.clear()

    def debug_locals(self) -> Dict[str, Any]:
        with self._lock:
            debugger = self._require_debugger()
            self._clear_var_objects_locked()
            try:
                variables = debugger.command(
                    "-stack-list-variables --all-values").get("variables", [])
                result = []
                for item in variables if isinstance(variables, list) else []:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    try:
                        variable = self._new_var_object_locked(str(item["name"]))
                        variable["arg"] = item.get("arg") == "1"
                    except GdbMiError:
                        variable = {
                            "name": item["name"], "value": item.get("value", "unavailable"),
                            "type": item.get("type", ""), "numchild": 0,
                        }
                    result.append(variable)
                return {"variables": result}
            except GdbMiError as exc:
                raise WebError("gdb_variables_failed", str(exc), 409) from exc

    @staticmethod
    def _find_symbols(value: Any) -> list[Dict[str, Any]]:
        found: list[Dict[str, Any]] = []
        if isinstance(value, dict):
            if "name" in value and any(k in value for k in ("type", "description", "line")):
                found.append(value)
            for child in value.values():
                found.extend(WebController._find_symbols(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(WebController._find_symbols(child))
        return found

    def debug_globals(self, query: str = "", limit: int = 50) -> Dict[str, Any]:
        with self._lock:
            debugger = self._require_debugger()
            self._clear_var_objects_locked()
            limit = max(1, min(int(limit), 100))
            command = f"-symbol-info-variables --max-results {limit}"
            if query:
                command += f" --name {quote_mi(re.escape(query))}"
            try:
                symbols = self._find_symbols(debugger.command(command).get("symbols", {}))[:limit]
                variables = []
                for symbol in symbols:
                    name = str(symbol.get("name", ""))
                    if not name:
                        continue
                    try:
                        variable = self._new_var_object_locked(name)
                    except GdbMiError:
                        variable = {"name": name, "value": "unavailable", "numchild": 0}
                    variable["type"] = variable.get("type") or symbol.get("type", "")
                    variable["file"] = symbol.get("fullname") or symbol.get("filename")
                    variables.append(variable)
                return {"query": query, "variables": variables, "limit": limit}
            except GdbMiError as exc:
                raise WebError("gdb_globals_failed", str(exc), 409) from exc

    def debug_variable_children(self, handle: str) -> Dict[str, Any]:
        with self._lock:
            debugger = self._require_debugger()
            if handle not in self._var_objects:
                raise WebError("unknown_variable", "Variable handle is no longer valid", 404)
            try:
                raw = debugger.command(
                    f"-var-list-children --all-values {handle}").get("children", [])
                children = self._unwrap_list(raw, "child")
                result = []
                for child in children:
                    child_handle = str(child.get("name", ""))
                    if child_handle:
                        self._var_objects.add(child_handle)
                    result.append({
                        "handle": child_handle,
                        "name": child.get("exp", child_handle),
                        "value": child.get("value", ""),
                        "type": child.get("type", ""),
                        "numchild": int(child.get("numchild", 0)),
                    })
                return {"handle": handle, "variables": result}
            except GdbMiError as exc:
                raise WebError("gdb_children_failed", str(exc), 409) from exc

    def console(self, command: str) -> str:
        if not command.strip():
            return ""
        if command.lstrip().startswith(("!", "$")) and not self.unsafe_console:
            raise WebError(
                "unsafe_console_disabled",
                "Host shell and Python expressions are disabled",
                403)
        with self._lock:
            self._require_exclusive()
            assert self._console
            output = StringIO()
            self._console.output_stream = output
            self._console.process_command_line(command)
            return output.getvalue()

    def stack(self, core: int = 0, words: int = 64) -> Dict[str, Any]:
        with self._lock:
            session = self._require_exclusive()
            target = session.target.cores[core]
            if target.get_state() != target.State.HALTED:
                raise WebError("target_running", "Halt the target before analyzing its stack", 409)
            names = ["sp", "msp", "psp", "lr", "pc", "xpsr", "control"]
            vals = dict(zip(names, target.read_core_registers_raw(names)))
            # The architectural SP register is already resolved to the active MSP or PSP.
            # CONTROL.SPSEL alone is not sufficient while handling an exception and caused
            # the old implementation to select inactive or invalid stack memory.
            sp = vals["sp"]
            warnings = []
            raw = []
            stack_region = session.target.memory_map.get_region_for_address(sp)
            if stack_region is None or not stack_region.is_readable:
                warnings.append(f"Active SP 0x{sp:08x} is outside readable target memory")
            else:
                available_words = max(0, min(words, (stack_region.end - sp + 1) // 4))
                for offset in range(0, available_words, 8):
                    count = min(8, available_words - offset)
                    try:
                        raw.extend(target.read_memory_block32(sp + offset * 4, count))
                    except Exception as exc:
                        warnings.append(
                            f"Stack memory became unreadable at 0x{sp + offset * 4:08x}: {exc}")
                        break
            candidates = []
            elf = session.target.elf
            for i, value in enumerate(raw):
                region = session.target.memory_map.get_region_for_address(
                    value & ~1)
                if region and region.is_executable:
                    entry = {"stack_address": sp + i * 4, "address": value}
                    if elf:
                        try:
                            sym = elf.symbol_decoder.get_symbol_for_address(value & ~1)
                            if sym:
                                entry["symbol"] = sym.name
                        except Exception as exc:
                            if not warnings:
                                warnings.append(f"ELF symbols could not be resolved: {exc}")
                    candidates.append(entry)
            return {"core": core, "registers": vals, "stack_pointer": sp, "words": raw,
                    "return_address_candidates": candidates,
                    "warnings": warnings,
                    "note": "Candidates are not confirmed frames unless unwind metadata is available."}

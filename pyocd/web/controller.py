"""Stateful, thread-safe orchestration for the pyOCD web interface."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from enum import Enum
from io import StringIO
from importlib import metadata
import json
import logging
import os
import platform
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib import error as urlerror
from urllib import request as urlrequest
from typing import Any, Callable, Dict, Optional

from .. import __version__
from ..commands.execution_context import CommandExecutionContext
from ..core.exceptions import TransferError
from ..core.helpers import ConnectHelper
from ..core.options import OPTIONS_INFO
from ..core.session import Session
from ..core.target import Target
from ..flash.eraser import FlashEraser
from ..flash.file_programmer import FileProgrammer
from ..gdbserver import GDBServer
from ..tools.lists import ListGenerator
from ..target.pack import pack_target
from ..target import TARGET
from .gdb_mi import GdbMiClient, GdbMiError, MiRecord, quote_mi

try:
    import cmsis_pack_manager
except ImportError:
    cmsis_pack_manager = None


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
    events: list[Dict[str, Any]] = field(default_factory=list)
    source: str = "web"
    bytes_total: Optional[int] = None
    bytes_completed: Optional[int] = None
    speed_bps: Optional[float] = None
    phase: Optional[str] = None


class JobLogHandler(logging.Handler):
    """Capture pyOCD records emitted by the thread running one web job."""

    def __init__(self, job: Job, lock: threading.RLock, limit: int = 200):
        super().__init__()
        self._job = job
        self._lock = lock
        self._thread_id = threading.get_ident()
        self._limit = limit
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._thread_id:
            return
        with self._lock:
            self._job.events.append({"time": record.created, "level": record.levelname,
                                     "logger": record.name, "message": self.format(record)})
            if len(self._job.events) > self._limit:
                del self._job.events[:-self._limit]


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
    # Publish the actual Session defaults so the browser does not maintain a
    # second, potentially divergent set of connection defaults.
    CONNECTION_DEFAULTS = {
        name: OPTIONS_INFO[name].default
        for name in ("frequency", "connect_mode", "dap_protocol")
    }
    SAFE_SESSION_OPTIONS = {
        "auto_unlock", "cmsis_dap.deferred_transfers", "cmsis_dap.limit_packets",
        "cmsis_dap.prefer_v1", "connect_mode", "dap_protocol", "dap_swj_enable",
        "dap_swj_use_dormant", "frequency", "jlink.power", "reset.hold_time",
        "reset.post_delay", "reset_type", "resume_on_disconnect",
        "stlink.v3_prescaler", "pack.debug_sequences.enable",
        "pack.debug_sequences.disabled_sequences",
        "chip_erase", "smart_flash", "fast_program", "keep_unwritten",
    }

    def __init__(
            self, artifact_dir: Optional[str] = None, unsafe_console: bool = False,
            gdb_executable: Optional[str] = None, config_path: Optional[str] = None,
            force_rpi: bool = False, serve_local_only: bool = True):
        self._lock = threading.RLock()
        self._pack_lock = threading.Lock()
        self._target_catalog_lock = threading.RLock()
        self._pack_cache_instance = None
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pyocd-web")
        self._session: Optional[Session] = None
        self._console: Optional[CommandExecutionContext] = None
        self._state = ConnectionState.DISCONNECTED
        self._error: Optional[str] = None
        default_config = (Path(artifact_dir) / ".pyocd-web.json"
                          if artifact_dir else Path(".pyocd-web.json"))
        self._config_path = Path(config_path) if config_path else default_config.resolve()
        self._profile: Dict[str, Any] = self._load_profile()
        self._gdb: Dict[int, GDBServer] = {}
        self._debugger: Optional[GdbMiClient] = None
        self._debugger_state = "inactive"
        self._debugger_stopped = threading.Event()
        self._debugger_core = 0
        self._gdb_executable = gdb_executable
        self._serve_local_only = serve_local_only
        self._force_rpi = force_rpi
        self._target_locked: Optional[bool] = None
        # Capture static connection details once. Some remote probes implement property
        # reads as transport requests, so flash jobs must serve polling from this cache.
        self._session_metadata: Optional[Dict[str, Any]] = None
        self._var_objects: set[str] = set()
        self._next_var_object = 0
        self._jobs: Dict[str, Job] = {}
        self._gdb_flash_jobs: Dict[int, Job] = {}
        self._artifacts: Dict[str, Dict[str, Any]] = {}
        self._attached_elf_artifact: Optional[str] = None
        self._log_handler = WebLogHandler()
        logging.getLogger("pyocd").addHandler(self._log_handler)
        self._artifact_dir = Path(
            artifact_dir or tempfile.mkdtemp(
                prefix="pyocd-web-"))
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        # Catalog generation can parse every installed CMSIS-Pack. Cache its
        # JSON representation on disk so a Pi does not retain the complete
        # Python object graph between browser requests.
        self._target_catalog_path = self._artifact_dir / ".target-catalog.json"
        self._target_catalog_invalidated = False
        try:
            self._target_catalog_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._target_catalog_invalidated = True
            LOG.warning("unable to clear stale target catalog cache: %s", exc)
        self.unsafe_console = unsafe_console
        self._started_at = time.time()

    def _load_profile(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_profile(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Persist connection settings independently of a probe connection."""
        if not isinstance(profile, dict):
            raise WebError("invalid_config", "Configuration must be a JSON object")
        interface_name = profile.get("interface_name")
        if interface_name is not None and (not isinstance(interface_name, str)
                                           or len(interface_name.strip()) > 48):
            raise WebError("invalid_interface_name", "Interface name must be at most 48 characters")
        with self._lock:
            self._profile = profile
            try:
                self._config_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._config_path.with_suffix(self._config_path.suffix + ".tmp")
                temporary.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
                temporary.replace(self._config_path)
            except OSError as exc:
                raise WebError("config_write_failed", str(exc), 500) from exc
            return self._profile

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

    @staticmethod
    def _memory_info() -> Dict[str, Optional[int]]:
        """Return system and process memory figures without requiring psutil."""
        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                class MemoryStatus(ctypes.Structure):
                    _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                                ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                                ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                                ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                                ("available_extended", ctypes.c_ulonglong)]

                class ProcessMemory(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD),
                                ("peak_working_set", ctypes.c_size_t), ("working_set", ctypes.c_size_t),
                                ("peak_paged_pool", ctypes.c_size_t), ("paged_pool", ctypes.c_size_t),
                                ("peak_nonpaged_pool", ctypes.c_size_t), ("nonpaged_pool", ctypes.c_size_t),
                                ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]

                memory = MemoryStatus()
                memory.length = ctypes.sizeof(memory)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
                process = ProcessMemory()
                process.cb = ctypes.sizeof(process)
                ctypes.windll.psapi.GetProcessMemoryInfo(
                    ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(process), process.cb)
                return {"system_total": memory.total,
                        "system_used": memory.total - memory.available,
                        "process_used": process.working_set}
            page_size = os.sysconf("SC_PAGE_SIZE")
            pages = os.sysconf("SC_PHYS_PAGES")
            available = os.sysconf("SC_AVPHYS_PAGES")
            import resource
            process = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform != "darwin":
                process *= 1024
            return {"system_total": page_size * pages,
                    "system_used": page_size * (pages - available), "process_used": process}
        except (AttributeError, ImportError, OSError):
            return {"system_total": None, "system_used": None, "process_used": None}

    def system_info(self) -> Dict[str, Any]:
        hostname = socket.gethostname()
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(hostname, None)
                                if item[4][0] not in {"127.0.0.1", "::1"}})
        except socket.gaierror:
            addresses = []
        return {"pyocd_version": __version__, "python_version": platform.python_version(),
                "platform": platform.platform(), "hostname": hostname, "addresses": addresses,
                "pid": os.getpid(), "uptime": max(0, time.time() - self._started_at),
                "memory": self._memory_info(),
                "system_power_supported": sys.platform.startswith("linux")}

    def system_power(self, action: str) -> Dict[str, Any]:
        """Request a Linux host reboot or poweroff."""
        if not sys.platform.startswith("linux"):
            raise WebError("system_power_unsupported",
                           "System reboot and shutdown are available only on Linux", 403)
        commands = {"reboot": "reboot", "shutdown": "poweroff"}
        command = commands.get(action)
        if command is None:
            raise WebError("invalid_power_action", "Action must be reboot or shutdown")
        try:
            subprocess.run(
                ["systemctl", command], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                check=True, timeout=15, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise WebError("system_power_failed",
                           f"Unable to request system {action}: {detail.strip()}", 500) from exc
        return {"accepted": True, "action": action}

    def check_for_update(self) -> Dict[str, Any]:
        """Check this fork's main branch without modifying the installation."""
        request = urlrequest.Request(
            "https://api.github.com/repos/kamtar/pyOCD/commits/main",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "pyOCD-web"})
        try:
            with urlrequest.urlopen(request, timeout=8) as response:
                head = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urlerror.URLError) as exc:
            raise WebError("update_check_failed", f"Unable to check for updates: {exc}", 502) from exc
        latest_revision = str(head.get("sha", ""))
        if not latest_revision:
            raise WebError("update_check_failed", "GitHub did not return the main revision", 502)
        source, current_revision = "Unknown installation source", None
        command = ("python -m pip install --upgrade --force-reinstall "
                   "\"git+https://github.com/kamtar/pyOCD.git@main\"")
        try:
            direct = json.loads(metadata.distribution("pyocd").read_text("direct_url.json") or "{}")
            url = str(direct.get("url", ""))
            if "github.com/kamtar/pyocd" in url.lower():
                current_revision = direct.get("vcs_info", {}).get("commit_id")
                source = "GitHub fork (kamtar/pyOCD)"
            elif direct.get("dir_info", {}).get("editable"):
                source = "Editable local checkout"
            elif url:
                source = url
        except (metadata.PackageNotFoundError, ValueError):
            pass
        return {"current": __version__, "current_revision": current_revision,
                "latest_revision": latest_revision,
                "update_available": (current_revision.lower() != latest_revision.lower()
                                     if current_revision else None),
                "release_url": str(head.get("html_url", "https://github.com/kamtar/pyOCD/commits/main")),
                "install_source": source, "command": command}

    def _record_activity_locked(self, kind: str, message: str) -> None:
        now = time.time()
        job = Job(uuid.uuid4().hex, kind, state="completed", progress=1.0,
                  message=message, created_at=now, finished_at=now)
        self._jobs[job.id] = job

    def _gdb_flash_activity(self, core: int, event: str, **details: Any) -> None:
        """Translate GDB remote-flash activity into dashboard job telemetry."""
        with self._lock:
            now = time.time()
            job = self._gdb_flash_jobs.get(core)
            if event == "erase" or job is None:
                job = Job(uuid.uuid4().hex, "erase", state="running",
                          message=f"Preparing erase · core {core}", source="gdb",
                          bytes_total=0, bytes_completed=0, phase="erase")
                job.events.append({"time": now, "level": "INFO", "logger": "pyocd.gdbserver",
                                   "message": f"GDB erase requested on core {core}"})
                self._gdb_flash_jobs[core] = job
                self._jobs[job.id] = job
            if event == "erase":
                length = details.get("length")
                job.message = (f"Preparing erase · {int(length) / 1024:.1f} KiB"
                               if length else "Preparing target erase")
            elif event == "buffered":
                job.kind = "program"
                job.bytes_total = int(details.get("bytes_total", 0))
                job.message = f"Receiving program data · {job.bytes_total / 1024:.1f} KiB"
            elif event == "phase":
                job.phase = str(details.get("phase", "program"))
                job.message = "Erasing target" if job.phase == "erase" else "Programming target"
            elif event == "progress":
                job.progress = max(0.0, min(1.0, float(details.get("progress", 0.0))))
                job.bytes_total = int(details.get("bytes_total", job.bytes_total or 0))
                job.bytes_completed = round((job.bytes_total or 0) * job.progress)
                job.speed_bps = job.bytes_completed / max(now - job.created_at, 0.001)
                action = "Erasing" if job.phase == "erase" else "Programming"
                job.message = f"{action} target · {job.progress * 100:.0f}%"
            elif event in ("complete", "failed"):
                job.finished_at = now
                job.state = "completed" if event == "complete" else "failed"
                job.error = details.get("error")
                if event == "complete":
                    job.progress = 1.0
                    job.bytes_completed = job.bytes_total
                    job.speed_bps = (job.bytes_total or 0) / max(now - job.created_at, 0.001)
                    job.message = ("GDB program completed" if job.bytes_total
                                   else "GDB erase completed")
                else:
                    job.message = "GDB flash failed"
                job.events.append({"time": now, "level": "INFO" if event == "complete" else "ERROR",
                                   "logger": "pyocd.gdbserver",
                                   "message": job.message if not job.error else str(job.error)})
                self._gdb_flash_jobs.pop(core, None)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            result: Dict[str, Any] = {
                "state": self._state.value,
                "error": self._error,
                "profile": self._profile,
                "connection_defaults": self.CONNECTION_DEFAULTS,
                "connected": self._session is not None and self._session.is_open,
                "gdb": [{"core": c, "port": s.port, "running": s.is_alive(),
                         "clients": len(s.client_sessions),
                         "client_addresses": [str(client.remote_address)
                                              for client in s.client_sessions
                                              if client.is_socket_connected]}
                        for c, s in self._gdb.items()],
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
                if (self._state == ConnectionState.BUSY
                        and self._session_metadata is not None):
                    result.update(copy.deepcopy(self._session_metadata))
                    result["target"]["state"] = "busy"
                    result["target"]["locked"] = self._target_locked
                    return result
                target, probe = self._session.target, self._session.probe
                # A GDB server accesses the probe from its own thread. Never
                # perform competing hardware reads from the state poller.
                if self._debugger:
                    target_state = self._debugger_state
                elif self._gdb:
                    server = next(iter(self._gdb.values()))
                    target_state = "running" if server.is_target_running else "halted"
                elif self._state == ConnectionState.BUSY:
                    # Flash jobs own the probe. Polling target state concurrently can corrupt
                    # the debug transport or make an erase/program operation appear to hang.
                    target_state = "busy"
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
                    "locked": (self._target_locked
                               if self._gdb or self._state == ConnectionState.BUSY
                               else target.is_locked()),
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
                self._session_metadata = {
                    "target": copy.deepcopy(result["target"]),
                    "probe": copy.deepcopy(result["probe"]),
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
        if self._force_rpi and not any(
                probe.get("unique_id") == "rpi-gpio:" for probe in obj["boards"]):
            obj["boards"].append({
                "unique_id": "rpi-gpio:",
                "info": "Raspberry Pi GPIO SWD (preview only)",
                "board_vendor": "Raspberry Pi",
                "board_name": "GPIO header",
                "target": "cortex_m",
                "vendor_name": "Raspberry Pi",
                "product_name": "GPIO SWD",
                "preview_only": True,
            })
        return obj

    def targets(self, query: Optional[str] = None) -> Dict[str, Any]:
        # The web client requests the complete list. Cache only that form so a
        # caller that asks for a filtered list retains ListGenerator's existing
        # filtering semantics.
        with self._target_catalog_lock:
            if query is None and not self._target_catalog_invalidated:
                try:
                    cached = json.loads(self._target_catalog_path.read_text(encoding="utf-8"))
                    if isinstance(cached, dict) and isinstance(cached.get("targets"), list):
                        return cached
                except (OSError, ValueError):
                    pass

            result = ListGenerator.list_targets(name_filter=query)
            unique_targets: Dict[str, Dict[str, Any]] = {}
            for target in result.get("targets", []):
                name = target.get("name")
                if name and name != "cortex_m":
                    # Managed Pack targets can appear once from TARGET and once from
                    # the Pack cache after metadata inspection has populated them.
                    unique_targets.setdefault(name, target)
            result["targets"] = list(unique_targets.values())
            if query is None:
                temporary = self._target_catalog_path.with_suffix(".tmp")
                try:
                    temporary.write_text(json.dumps(result), encoding="utf-8")
                    temporary.replace(self._target_catalog_path)
                    self._target_catalog_invalidated = False
                except OSError:
                    # The catalog is an optimisation; serving it must not depend
                    # on the artifact directory remaining writable.
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
            return result

    def _invalidate_target_catalog(self) -> None:
        """Discard target metadata after installed Pack contents change."""
        with self._target_catalog_lock:
            self._target_catalog_invalidated = True
            try:
                self._target_catalog_path.unlink()
            except OSError:
                pass

    def target_pack_info(self, target_name: str) -> Dict[str, Any]:
        """Return CMSIS-Pack debug-sequence metadata without opening a probe."""
        # Managed Pack devices are included in ListGenerator.list_targets() without
        # being inserted into TARGET. Populate only the selected device so metadata
        # inspection never depends on a prior (possibly failed) connection attempt.
        target_class = TARGET.get(target_name.lower())
        if target_class is None:
            pack_target.ManagedPacks.populate_target(target_name)
            target_class = TARGET.get(target_name.lower())
        recovery = self._target_recovery_info(target_class)
        pack_device = getattr(target_class, "_pack_device", None) if target_class else None
        if pack_device is None:
            return {"target": target_name, "source": "builtin", "sequences": [],
                    "processors": [], "pack": None, "recovery": recovery}

        sequences = sorted(({
            "name": sequence.name,
            "pname": sequence.pname,
            "info": sequence.info,
            "enabled": sequence.is_enabled,
        } for sequence in pack_device.sequences),
            key=lambda item: (item["name"].lower(), item["pname"] or ""))
        pdsc = pack_device.pack_description
        return {
            "target": target_name,
            "source": "pack",
            "pack": {"name": pdsc.pack_name, "path": pack_device.pack_path},
            "processors": sorted(pack_device.processors_map),
            "sequences": sequences,
            "recovery": recovery,
        }

    @staticmethod
    def _target_recovery_info(target_class: Optional[type]) -> Dict[str, Any]:
        if target_class is None:
            return {"available": False, "implementation": "Unavailable", "automatic": False}
        handler = next((base for base in target_class.__mro__ if "mass_erase" in base.__dict__), None)
        automatic_handler = next((base for base in target_class.__mro__
                                  if "check_flash_security" in base.__dict__), None)
        if handler is None:
            return {"available": False, "implementation": "Unavailable", "automatic": False}
        labels = {
            "Kinetis": "Kinetis MDM-AP mass erase",
            "NRF52": "Nordic CTRL-AP mass erase",
            "NRF91": "Nordic CTRL-AP mass erase",
            "NRF54L": "Nordic CTRL-AP mass erase",
            "LPC5500": "LPC debugger-mailbox recovery",
            "SoCTarget": "Generic chip erase",
        }
        return {
            "available": True,
            "implementation": labels.get(handler.__name__, f"{handler.__name__} mass erase"),
            "handler": f"{handler.__module__}.{handler.__name__}",
            "automatic": automatic_handler is not None,
        }

    def unlock_target(self) -> Dict[str, Any]:
        """Mass erase the connected target using its recovery implementation."""
        with self._lock:
            session = self._require_exclusive()
            result = session.target.mass_erase()
            if result is False:
                raise WebError("unlock_failed", "Target mass erase and unlock failed", 409)
            try:
                self._target_locked = session.target.is_locked()
            except Exception:
                self._target_locked = None
            self._record_activity_locked("unlock", "Target mass erased and unlock requested")
            return self.snapshot()

    def run_pack_sequence(self, name: str, pname: Optional[str] = None) -> Dict[str, Any]:
        """Manually execute a top-level sequence declared by the active target's Pack."""
        if not name:
            raise WebError("sequence_required", "Select a debug sequence")
        with self._lock:
            session = self._require_exclusive()
            delegate = getattr(session.target, "debug_sequence_delegate", None)
            pack_device = getattr(session.target, "_pack_device", None)
            if delegate is None or pack_device is None:
                raise WebError("pack_sequence_unavailable",
                               "The connected target is not provided by a CMSIS-Pack", 409)
            declared = any(seq.name == name and seq.pname == pname for seq in pack_device.sequences)
            if not declared:
                raise WebError("pack_sequence_not_found",
                               "That sequence is not declared by the connected target's Pack", 404)
            result = delegate.run_sequence(name, pname)
            if result is None:
                raise WebError("pack_sequence_disabled",
                               "The selected sequence is disabled by the Pack or connection settings", 409)
            self._record_activity_locked("pack-sequence", f"Ran CMSIS-Pack sequence {name}")
            return {"ran": True, "name": name, "pname": pname}

    def _pack_cache(self):
        if cmsis_pack_manager is None:
            raise WebError("pack_manager_unavailable",
                           "Open-CMSIS-Pack support requires cmsis-pack-manager", 501)
        if self._pack_cache_instance is None:
            self._pack_cache_instance = cmsis_pack_manager.Cache(True, False)
        return self._pack_cache_instance

    def pack_search(self, query: str, limit: int = 100) -> Dict[str, Any]:
        query = query.strip().lower()
        if len(query) < 2:
            raise WebError("pack_query_too_short", "Enter at least two characters")
        with self._pack_lock:
            cache = self._pack_cache()
            if not cache.index:
                return {"devices": [], "index_available": False}
            devices = []
            for key, info in cache.index.items():
                name = str(info.get("name", key))
                vendor = str(info.get("vendor", "")).split(":", 1)[0]
                if query not in name.lower() and query not in vendor.lower():
                    continue
                refs = list(cache.packs_for_devices([info]))
                if not refs:
                    continue
                ref = refs[0]
                installed = os.path.isfile(os.path.join(cache.data_path, ref.get_pack_name()))
                devices.append({"name": name, "vendor": vendor,
                                "pack": f"{ref.vendor}.{ref.pack}", "version": str(ref.version),
                                "installed": installed})
                if len(devices) >= min(max(limit, 1), 500):
                    break
        devices.sort(key=lambda item: (item["name"].lower(), item["pack"].lower()))
        return {"devices": devices, "index_available": True}

    def pack_update(self) -> Dict[str, Any]:
        with self._pack_lock:
            cache = self._pack_cache()
            cache.cache_descriptors()
            return {"updated": True, "device_count": len(cache.index)}

    def pack_install(self, device_name: str) -> Dict[str, Any]:
        with self._pack_lock:
            cache = self._pack_cache()
            if not cache.index:
                raise WebError("pack_index_missing", "Refresh the pack index before installing", 409)
            match = next((info for key, info in cache.index.items()
                          if key.lower() == device_name.lower()
                          or str(info.get("name", "")).lower() == device_name.lower()), None)
            if match is None:
                raise WebError("pack_device_not_found", "Device is not present in the pack index", 404)
            refs = list(cache.packs_for_devices([match]))
            if not refs:
                raise WebError("pack_not_found", "No pack provides this device", 404)
            cache.download_pack_list(refs)
        name = str(match.get("name", device_name))
        pack_target.ManagedPacks.populate_target(name)
        self._invalidate_target_catalog()
        return {"installed": True, "device": name, "packs": [str(ref) for ref in refs]}

    def connect(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(profile, dict):
            raise WebError("invalid_profile", "Connection profile must be a JSON object")
        with self._lock:
            if self._state == ConnectionState.BUSY:
                raise WebError("operation_busy", "Wait for the active operation to finish", 409)
            target_override = profile.get("target_override")
            if not target_override or target_override == "cortex_m":
                raise WebError(
                    "target_required",
                    "Select a specific target MCU before connecting",
                    400)
            self._disconnect_locked()
            self._state, self._error = ConnectionState.CONNECTING, None
            session: Optional[Session] = None
            try:
                selector = profile.get("probe") or None
                probe = ConnectHelper.choose_probe(
                    blocking=False, return_first=True, unique_id=selector)
                if probe is None:
                    raise WebError(
                        "probe_not_found",
                        "The selected debug probe is not available",
                        404)
                supplied_options = profile.get("options") or {}
                if not isinstance(supplied_options, dict):
                    raise WebError("invalid_options", "Connection options must be a JSON object")
                rejected = set(supplied_options) - self.SAFE_SESSION_OPTIONS
                if rejected:
                    raise WebError(
                        "unsupported_options",
                        "Unsupported connection option(s): " + ", ".join(sorted(rejected)))
                options = dict(supplied_options)
                for key in ("target_override", "frequency", "connect_mode", "dap_protocol"):
                    if profile.get(key) is not None:
                        options[key] = profile[key]
                reset_method = profile.get("reset_method", "hardware")
                if reset_method not in ("hardware", "core"):
                    raise WebError("invalid_reset_method", "Reset method must be hardware or core")
                options["reset_type"] = reset_method
                gpio = profile.get("gpio") or {}
                if not isinstance(gpio, dict):
                    raise WebError("invalid_gpio", "GPIO settings must be a JSON object")
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
                self.save_profile(profile)
                self._state = ConnectionState.CONNECTED
                self._record_activity_locked("connect", "Target connected")
                return self.snapshot()
            except Exception as exc:
                self._state, self._error = ConnectionState.ERROR, str(exc)
                if self._session:
                    self._session.close()
                elif session:
                    session.close()
                self._session, self._console = None, None
                self._session_metadata = None
                raise

    def pulse_probe_reset(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Pulse the selected probe's nRESET pin without connecting to the target."""
        if not isinstance(profile, dict):
            raise WebError("invalid_profile", "Connection profile must be a JSON object")
        with self._lock:
            if self._state == ConnectionState.BUSY:
                raise WebError("operation_busy", "Wait for the active operation to finish", 409)
            if self._session and self._session.is_open:
                raise WebError("already_connected", "Disconnect before pulsing the probe reset pin", 409)
            selector = profile.get("probe") or None
            probe = ConnectHelper.choose_probe(
                blocking=False, return_first=True, unique_id=selector)
            if probe is None:
                raise WebError("probe_not_found", "No debug probe is available", 404)

            options: Dict[str, Any] = {}
            if profile.get("frequency") is not None:
                options["frequency"] = profile["frequency"]
            gpio = profile.get("gpio") or {}
            if not isinstance(gpio, dict):
                raise WebError("invalid_gpio", "GPIO settings must be a JSON object")
            for key in ("swclk", "swdio", "nreset", "swdio_dir",
                        "restore_pins", "wait_retries"):
                if key in gpio:
                    options["rpi_gpio." + key] = gpio[key]

            session = Session(probe, auto_open=False, options=options)
            try:
                # This opens only the adapter. init_board=False deliberately avoids
                # target discovery and any SWD/JTAG connection sequence.
                session.open(init_board=False)
                probe.reset()
            except NotImplementedError as exc:
                raise WebError(
                    "reset_pin_unsupported",
                    "The selected debug probe does not support direct reset-pin control",
                    409) from exc
            finally:
                session.close()
            self._record_activity_locked("probe-reset", "Probe nRESET pin pulsed")
            return {"reset": True, "probe": probe.unique_id}

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
        self._session_metadata = None
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
                    "reset": f"-interpreter-exec console {quote_mi('monitor reset core')}",
                    "reset-hardware": f"-interpreter-exec console {quote_mi('monitor reset hardware')}",
                    "reset-halt": f"-interpreter-exec console {quote_mi('monitor reset halt')}",
                    "reset-halt-core": f"-interpreter-exec console {quote_mi('monitor reset halt core')}",
                    "reset-halt-hardware": f"-interpreter-exec console {quote_mi('monitor reset halt hardware')}",
                }
                if action not in commands:
                    raise WebError("bad_action", "Unknown target action")
                try:
                    if action == "halt":
                        self._debugger_stopped.clear()
                    elif action in ("resume", "step", "reset", "reset-hardware"):
                        self._debugger_state = "running"
                        self._debugger_stopped.clear()
                    self._debugger.command(commands[action])
                    if action in ("reset", "reset-hardware"):
                        self._debugger.command("-exec-continue")
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
                    elif action in ("reset-halt", "reset-halt-core", "reset-halt-hardware"):
                        self._debugger_state = "stopped"
                        self._debugger_stopped.set()
                except GdbMiError as exc:
                    raise WebError("gdb_command_failed", str(exc), 409) from exc
                self._record_activity_locked(action, f"Target {action} requested through GDB")
                return self.snapshot()
            target = self._require_exclusive().target

            def reset_hardware() -> None:
                try:
                    target.reset(Target.ResetType.HARDWARE)
                except TransferError as exc:
                    # The reset itself can succeed while the first post-reset SWD
                    # access receives no ACK. Recover the existing DP in place;
                    # never close or discard the web session because of a reset.
                    logging.getLogger(__name__).warning(
                        "Recovering debug link after hardware reset: %s", exc)
                    dp = getattr(target, "dp", None)
                    if dp is None:
                        raise
                    last_error: Optional[TransferError] = exc
                    for _ in range(3):
                        try:
                            dp.post_reset_recovery()
                            return
                        except TransferError as recovery_error:
                            last_error = recovery_error
                            time.sleep(0.1)
                    raise last_error

            def reset_and_halt(reset_type: Target.ResetType) -> None:
                try:
                    target.reset_and_halt(reset_type)
                except TransferError:
                    if reset_type is not Target.ResetType.HARDWARE:
                        raise
                    dp = getattr(target, "dp", None)
                    if dp is None:
                        raise
                    dp.post_reset_recovery()
                    target.halt()

            actions: Dict[str, Callable[[], None]] = {
                "halt": target.halt,
                "resume": target.resume,
                "step": target.step,
                # A connected reset must preserve the live debug link. A core-only
                # reset avoids the ResetSystem sequence, which can momentarily remove
                # the DAP and leave this long-lived web session without acknowledgements.
                "reset": lambda: target.reset(Target.ResetType.CORE),
                "reset-hardware": reset_hardware,
                "reset-halt": target.reset_and_halt,
                "reset-halt-core": lambda: reset_and_halt(Target.ResetType.CORE),
                "reset-halt-hardware": lambda: reset_and_halt(Target.ResetType.HARDWARE),
            }
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
        safe_name = Path(name).name or "firmware.bin"
        with self._lock:
            existing = next((artifact for artifact in self._artifacts.values()
                             if artifact["name"] == safe_name), None)
            artifact_id = existing["id"] if existing else uuid.uuid4().hex
            path = (Path(existing["path"]) if existing else
                    self._artifact_dir / f"{artifact_id}-{safe_name}")
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
        job = Job(uuid.uuid4().hex, kind, phase=kind if kind in ("erase", "program") else None)
        job.events.append({"time": job.created_at, "level": "INFO",
                           "logger": "pyocd.web", "message": f"{kind.title()} queued"})
        # Reserve exclusive target ownership before returning the accepted job to
        # the browser. Otherwise direct controls can slip in before the worker gets
        # scheduled and marks the controller busy.
        with self._lock:
            self._require_exclusive()
            initial_message = "Erasing target" if kind == "erase" else "Preparing programming" \
                if kind == "program" else "Starting"
            self._state, job.state, job.message = ConnectionState.BUSY, "running", initial_message
            self._jobs[job.id] = job

        def runner() -> None:
            handler = JobLogHandler(job, self._lock)
            logging.getLogger("pyocd").addHandler(handler)
            try:
                fn(job)
                with self._lock:
                    job.progress, job.state, job.message = 1.0, "completed", "Completed"
                    job.events.append({"time": time.time(), "level": "INFO",
                                       "logger": "pyocd.web", "message": f"{kind.title()} completed"})
            except Exception as exc:
                with self._lock:
                    job.state, job.error, job.message = "failed", str(exc), "Failed"
                    job.events.append({"time": time.time(), "level": "ERROR",
                                       "logger": "pyocd.web", "message": str(exc)})
                    logging.getLogger(__name__).exception(
                        "%s job failed: %s", kind.title(), exc)
            finally:
                logging.getLogger("pyocd").removeHandler(handler)
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
            reset_type = (Target.ResetType.HARDWARE
                          if options.get("reset_method", "hardware") == "hardware"
                          else Target.ResetType.CORE)
            # Match the load command's default pre-program reset. A long-lived web
            # session may have left the core running or peripherals in a state that
            # prevents the RAM flash algorithm from starting reliably.
            session.target.reset_and_halt(reset_type)
            count = len(artifacts)
            for index, (artifact, image) in enumerate(artifacts):
                with self._lock:
                    job.events.append({"time": time.time(), "level": "INFO",
                                       "logger": "pyocd.web",
                                       "message": f"Preparing {artifact['name']} ({index + 1}/{count})"})
                def flash_phase(notification: Any) -> None:
                    if notification.event == Target.Event.PRE_FLASH_ERASE:
                        phase, action = "erase", "Erasing"
                    elif notification.event == Target.Event.PRE_FLASH_PROGRAM:
                        phase, action = "program", "Programming"
                    else:
                        return
                    with self._lock:
                        job.phase = phase
                        job.message = f"{action} {artifact['name']}"

                def progress(value: float, image_index: int = index) -> None:
                    with self._lock:
                        job.progress = float(value)
                        action = "Erasing" if job.phase == "erase" else "Programming"
                        image_position = f" ({image_index + 1}/{count})" if count > 1 else ""
                        job.message = (f"{action} {artifact['name']}{image_position} · "
                                       f"{job.progress * 100:.0f}%")
                # The first image uses the session's global flashing policy. Following
                # images must use sector erase so they cannot erase an earlier image.
                programmer = FileProgrammer(
                    session, progress=progress,
                    chip_erase=None if index == 0 else "sector")
                kwargs = {}
                base_address = image.get("base_address")
                if base_address not in (None, ""):
                    kwargs["base_address"] = int(str(base_address), 0)
                flash_events = (Target.Event.PRE_FLASH_ERASE, Target.Event.PRE_FLASH_PROGRAM)
                notifications_supported = hasattr(session, "subscribe") and hasattr(session, "unsubscribe")
                if notifications_supported:
                    session.subscribe(flash_phase, flash_events)
                try:
                    programmer.program(artifact["path"], **kwargs)
                finally:
                    if notifications_supported:
                        session.unsubscribe(flash_phase, flash_events)
            # Flash contents are committed at this point. Keep post-action failures
            # from making a successful program operation look partially complete.
            with self._lock:
                job.progress = 1.0
                job.message = "Programming complete"
            post = options.get("post_action", "reset")
            try:
                if post == "reset":
                    session.target.reset(reset_type)
                elif post == "halt":
                    session.target.reset_and_halt(reset_type)
                elif post == "run":
                    session.target.reset(reset_type)
                    session.target.resume()
            except TransferError as exc:
                # Match the load command: reset can momentarily remove the DAP even
                # though all flash writes completed successfully. The long-lived web
                # session cannot safely remain connected after failed DAP recovery,
                # so close it and report a completed job with a warning.
                warning = f"Programming succeeded; post-program {post} lost debug access: {exc}"
                logging.getLogger(__name__).warning(warning)
                try:
                    if not session.options.is_set("resume_on_disconnect"):
                        session.options.set("resume_on_disconnect", False)
                    session.context_state.suppress_disconnect_error = True
                except AttributeError:
                    pass
                try:
                    session.close()
                except Exception as close_exc:
                    logging.getLogger(__name__).debug(
                        "Ignoring session close error after lost post-reset DAP: %s",
                        close_exc)
                finally:
                    with self._lock:
                        if self._session is session:
                            self._session, self._console = None, None
                            self._session_metadata = None
                            self._target_locked = None
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

    def gdb_start(self, port: int = 3030,
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
            if cores is not None and not isinstance(cores, list):
                raise WebError("invalid_cores", "Cores must be a JSON array")
            selected_cores = cores or sorted(session.target.cores.keys())
            if any(not isinstance(core, int) for core in selected_cores):
                raise WebError("invalid_core", "Core numbers must be integers")
            if len(set(selected_cores)) != len(selected_cores):
                raise WebError("duplicate_core", "Each core may only be selected once")
            invalid_cores = [core for core in selected_cores if core not in session.target.cores]
            if invalid_cores:
                raise WebError(
                    "invalid_core",
                    "Core(s) are not available: " + ", ".join(map(str, invalid_cores)))
            session.options.set("gdbserver_port", port)
            session.options.set("serve_local_only", self._serve_local_only)
            # The web UI owns this server's lifetime. A GDB detach must only
            # end that client session, leaving the listener ready for another
            # connection until the user turns the service off.
            session.options.set("persist", True)
            try:
                for core in selected_cores:
                    server = GDBServer(session, core=core, activity_callback=self._gdb_flash_activity)
                    session.gdbservers[core] = server
                    self._gdb[core] = server
                    server.start()
            except Exception:
                for server in list(self._gdb.values()):
                    try:
                        server.stop()
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "Unable to stop GDB server after startup failure")
                session.gdbservers.clear()
                self._gdb.clear()
                raise
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

    def debug_start(self, core: int = 0, port: int = 3030) -> Dict[str, Any]:
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
                session.options.set("serve_local_only", self._serve_local_only)
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

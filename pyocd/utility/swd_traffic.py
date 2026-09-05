"""Low-overhead, in-memory capture of SWD traffic.

The recorder deliberately lives below the web layer.  A session can attach one
to a probe at runtime and the probe methods are wrapped only for that session.
When capture is disabled the wrapper immediately calls the original method, so
normal pyOCD sessions do not pay the cost of formatting or retaining traffic.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from functools import wraps
import threading
import time
from typing import Any, Dict, Iterator, Optional, Sequence


class SwdTrafficRecorder:
    """Thread-safe bounded recorder for structured SWD/DAP transactions."""

    _PROBE_METHODS = (
        "read_dp", "write_dp", "read_ap", "write_ap",
        "read_ap_multiple", "write_ap_multiple", "swd_sequence", "swj_sequence",
    )
    _MAX_DATA_BYTES = 64
    _MAX_RECORDS = 50_000
    _MAX_GROUPS = 4_000

    def __init__(self, max_records: int = _MAX_RECORDS) -> None:
        self._lock = threading.RLock()
        self._enabled = False
        self._records: deque[Dict[str, Any]] = deque(maxlen=max(1, max_records))
        self._groups: deque[Dict[str, Any]] = deque(maxlen=self._MAX_GROUPS)
        self._groups_by_id: Dict[int, Dict[str, Any]] = {}
        self._next_record_id = 0
        self._next_group_id = 0
        self._dropped = 0
        self._thread_state = threading.local()
        self._probes: Dict[int, Any] = {}
        self._bindings: Dict[int, tuple[Any, Dict[str, Any]]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable capture without clearing already captured data."""
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            for probe in list(self._probes.values()):
                self._bind_probe(probe)
        else:
            for probe_id in list(self._bindings):
                self._unbind_probe(probe_id)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._groups.clear()
            self._groups_by_id.clear()
            self._dropped = 0

    def finish_thread_groups(self, status: str = "completed") -> None:
        """Close groups owned by the calling thread (used during disconnect)."""
        state = self._state()
        for group_id in list(reversed(state.groups)):
            self._finish_group(group_id, status)
        state.event_groups.clear()

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "records": len(self._records),
                "groups": len(self._groups),
                "latest_id": self._next_record_id,
                "dropped": self._dropped,
                "capacity": self._records.maxlen,
            }

    def snapshot(self, after: int = 0, limit: int = 500) -> Dict[str, Any]:
        """Return one incremental batch and the groups referenced by it."""
        after = max(0, int(after))
        limit = min(max(1, int(limit)), 2_000)
        with self._lock:
            records = [item for item in self._records if item["id"] > after][:limit]
            group_ids = {item.get("group_id") for item in records if item.get("group_id") is not None}
            groups = [dict(group) for group in self._groups if group["id"] in group_ids]
            oldest_id = self._records[0]["id"] if self._records else self._next_record_id + 1
            return {
                "enabled": self._enabled,
                "transactions": records,
                "groups": groups,
                "next_cursor": records[-1]["id"] if records else after,
                "latest_id": self._next_record_id,
                "oldest_id": oldest_id,
                "dropped": self._dropped,
                "capacity": self._records.maxlen,
            }

    def _state(self) -> Any:
        state = self._thread_state
        if not hasattr(state, "groups"):
            state.groups = []
        if not hasattr(state, "transfer_depth"):
            state.transfer_depth = 0
        if not hasattr(state, "event_groups"):
            state.event_groups = []
        return state

    def current_group_id(self) -> Optional[int]:
        groups = self._state().groups
        return groups[-1] if groups else None

    def annotate_current_group(self, name: str, category: Optional[str] = None,
                               details: Optional[Dict[str, Any]] = None) -> None:
        """Refine a generic group once a halt reason becomes observable."""
        group_id = self.current_group_id()
        if group_id is None:
            return
        with self._lock:
            group = self._groups_by_id.get(group_id)
            if group is not None:
                group["name"] = str(name)
                if category is not None:
                    group["category"] = str(category)
                if details:
                    group["details"].update(self._safe_details(details))

    def _new_group(self, name: str, category: str, details: Optional[Dict[str, Any]]) -> Optional[int]:
        if not self._enabled:
            return None
        now = time.time()
        with self._lock:
            self._next_group_id += 1
            group_id = self._next_group_id
            parent_id = self.current_group_id()
            group = {
                "id": group_id,
                "name": str(name),
                "category": str(category),
                "parent_id": parent_id,
                "started_at": now,
                "finished_at": None,
                "status": "running",
                "transaction_count": 0,
                "first_id": None,
                "last_id": None,
                "details": self._safe_details(details or {}),
            }
            if len(self._groups) == self._groups.maxlen:
                removed = self._groups.popleft()
                self._groups_by_id.pop(removed["id"], None)
            self._groups.append(group)
            self._groups_by_id[group_id] = group
        self._state().groups.append(group_id)
        return group_id

    def _finish_group(self, group_id: Optional[int], status: str = "completed",
                      error: Optional[str] = None) -> None:
        if group_id is None:
            return
        state = self._state()
        if state.groups and state.groups[-1] == group_id:
            state.groups.pop()
        elif group_id in state.groups:
            state.groups.remove(group_id)
        with self._lock:
            group = self._groups_by_id.get(group_id)
            if group is None:
                return
            group["finished_at"] = time.time()
            group["status"] = status
            if error:
                group["error"] = str(error)

    @contextmanager
    def operation(self, name: str, category: str = "operation",
                  details: Optional[Dict[str, Any]] = None) -> Iterator[None]:
        """Group all transfers made by the current thread until the block ends."""
        group_id = self._new_group(name, category, details)
        try:
            yield
        except BaseException as exc:
            self._finish_group(group_id, "failed", str(exc))
            raise
        else:
            self._finish_group(group_id)

    def handle_notification(self, notification: Any) -> None:
        """Turn standard Target events into logical groups.

        Event names are used instead of importing Target so this small utility
        remains independent of the target implementation and import graph.
        """
        if not self._enabled:
            return
        event = getattr(getattr(notification, "event", None), "name", "")
        pre_groups = {
            "PRE_DISCONNECT": ("Detach / disconnect", "detach"),
            "PRE_RUN": (self._run_name(notification), "run"),
            "PRE_HALT": (self._halt_name(notification), "halt"),
            "PRE_RESET": ("Reset", "reset"),
            "PRE_FLASH_ERASE": ("Flash erase", "flash_erase"),
            "PRE_FLASH_PROGRAM": ("Flash program", "flash_program"),
        }
        post_events = {
            "POST_RUN", "POST_HALT", "POST_RESET", "POST_FLASH_ERASE", "POST_FLASH_PROGRAM",
        }
        state = self._state()
        if event in pre_groups:
            group_id = None
            if not state.groups:
                name, category = pre_groups[event]
                group_id = self._new_group(name, category, self._notification_details(notification))
            state.event_groups.append(group_id)
        elif event in post_events and state.event_groups:
            self._finish_group(state.event_groups.pop())

    @staticmethod
    def _notification_details(notification: Any) -> Dict[str, Any]:
        data = getattr(notification, "data", None)
        details: Dict[str, Any] = {}
        if data is not None:
            details["event_data"] = getattr(data, "name", str(data))
        source = getattr(notification, "source", None)
        if source is not None:
            details["source"] = type(source).__name__
        return details

    @classmethod
    def _run_name(cls, notification: Any) -> str:
        data = getattr(notification, "data", None)
        return "Step" if getattr(data, "name", "") == "STEP" else "Resume"

    @classmethod
    def _halt_name(cls, notification: Any) -> str:
        data = getattr(notification, "data", None)
        reason = getattr(data, "name", "")
        labels = {
            "BREAKPOINT": "Breakpoint hit",
            "WATCHPOINT": "Watchpoint hit",
            "VECTOR_CATCH": "Vector catch",
        }
        return labels.get(reason, "Stop / halt")

    @staticmethod
    def _safe_details(details: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in details.items():
            if value is None or isinstance(value, (bool, int, float, str)):
                result[str(key)] = value
            else:
                result[str(key)] = getattr(value, "name", str(value))
        return result

    def begin_transfer(self, method: str, args: Sequence[Any], kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Create an internal transfer token for a wrapped probe method."""
        if not self._enabled:
            return None
        state = self._state()
        if method in ("swd_sequence", "swj_sequence") and state.transfer_depth:
            return None
        state.transfer_depth += 1
        token: Dict[str, Any] = {
            "method": method,
            "started_at": time.time(),
            "group_id": self.current_group_id(),
            "depth": state.transfer_depth,
            "depth_active": True,
            "completed": False,
        }
        token.update(self._describe_transfer(method, args, kwargs))
        return token

    def _leave_transfer(self, token: Dict[str, Any]) -> None:
        if not token.pop("depth_active", False):
            return
        state = self._state()
        if state.transfer_depth >= token.get("depth", 1):
            state.transfer_depth -= 1

    def complete_transfer(self, token: Optional[Dict[str, Any]], result: Any = None,
                          error: Optional[BaseException] = None) -> None:
        if token is None or token.get("completed"):
            return
        token["completed"] = True
        self._leave_transfer(token)
        if not self._enabled:
            return
        now = time.time()
        item = {
            "id": 0,
            "time": token["started_at"],
            "duration_ms": round((now - token["started_at"]) * 1000.0, 3),
            "operation": token.get("operation", token["method"]),
            "kind": token["method"],
            "port": token.get("port"),
            "direction": token.get("direction"),
            "address": token.get("address"),
            "address_hex": (f"0x{token['address']:08x}" if isinstance(token.get("address"), int) else None),
            "count": token.get("count", 1),
            "group_id": token.get("group_id"),
            "status": "error" if error is not None else "ok",
        }
        if error is not None:
            item["error"] = f"{type(error).__name__}: {error}"
        else:
            item.update(self._result_details(token, result))
        with self._lock:
            self._next_record_id += 1
            item["id"] = self._next_record_id
            if len(self._records) == self._records.maxlen:
                self._dropped += 1
            self._records.append(item)
            group = self._groups_by_id.get(item["group_id"])
            if group is not None:
                group["transaction_count"] += 1
                if group["first_id"] is None:
                    group["first_id"] = item["id"]
                group["last_id"] = item["id"]

    def _describe_transfer(self, method: str, args: Sequence[Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
        def value(index: int, name: str, default: Any = None) -> Any:
            return args[index] if len(args) > index else kwargs.get(name, default)

        if method in ("read_dp", "write_dp"):
            return {"operation": "DP read" if method == "read_dp" else "DP write",
                    "port": "DP", "direction": "read" if method == "read_dp" else "write",
                    "address": value(0, "addr"), "write_data": value(1, "data") if method == "write_dp" else None,
                    "async": method == "read_dp" and not bool(value(1, "now", True))}
        if method in ("read_ap", "write_ap", "read_ap_multiple", "write_ap_multiple"):
            read = method.startswith("read")
            multiple = method.endswith("multiple")
            return {"operation": f"AP {'read' if read else 'write'}" + (" ×N" if multiple else ""),
                    "port": "AP", "direction": "read" if read else "write",
                    "address": value(0, "addr"),
                    "count": value(1, "count", 1) if multiple and read else (len(value(1, "values", ())) if multiple else 1),
                    "write_data": value(1, "values") if method == "write_ap_multiple" else (
                        value(1, "data") if method == "write_ap" else None),
                    "async": read and not bool(value(2 if multiple else 1, "now", True))}
        if method == "swj_sequence":
            return {"operation": "SWJ sequence", "port": "SWJ", "direction": "write",
                    "count": value(0, "length", 0), "write_data": value(1, "bits")}
        if method == "swd_sequence":
            return {"operation": "SWD bit sequence", "port": "SWD", "direction": "mixed",
                    "count": len(value(0, "sequences", ())), "request": self._sequence_request(value(0, "sequences", ())) }
        return {"operation": method}

    def _result_details(self, token: Dict[str, Any], result: Any) -> Dict[str, Any]:
        method = token["method"]
        if method == "swd_sequence":
            try:
                status, response = result
                return {"status_code": status, "response": [bytes(item).hex() for item in response]}
            except (TypeError, ValueError):
                return {"data": self._format_data(result)}
        if method == "swj_sequence":
            return {"data": self._format_data(token.get("write_data"))}
        data = result if token.get("direction") == "read" else token.get("write_data")
        formatted, length, truncated = self._format_data(data), self._data_length(data), self._is_truncated(data)
        return {"data": formatted, "data_length": length, "data_truncated": truncated}

    @classmethod
    def _format_data(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, int):
            return f"0x{value & 0xffffffff:08x}"
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            return raw[:cls._MAX_DATA_BYTES].hex(" ")
        try:
            values = list(value)
        except TypeError:
            return str(value)
        if values and all(isinstance(item, int) for item in values):
            max_words = cls._MAX_DATA_BYTES // 4
            return " ".join(f"0x{item & 0xffffffff:08x}" for item in values[:max_words])
        return str(values[:cls._MAX_DATA_BYTES])

    @staticmethod
    def _data_length(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, int):
            return 4
        if isinstance(value, (bytes, bytearray, memoryview)):
            return len(value)
        try:
            return len(value) * 4
        except TypeError:
            return 0

    @classmethod
    def _is_truncated(cls, value: Any) -> bool:
        return cls._data_length(value) > cls._MAX_DATA_BYTES

    @staticmethod
    def _sequence_request(sequences: Sequence[Any]) -> list[Dict[str, Any]]:
        result = []
        for sequence in sequences:
            item = {"cycles": int(sequence[0]), "direction": "write" if len(sequence) > 1 else "read"}
            if len(sequence) > 1:
                item["bits"] = f"0x{int(sequence[1]):x}"
            result.append(item)
        return result

    def _bind_probe(self, probe: Any) -> None:
        if probe is None or id(probe) in self._bindings:
            return
        originals: Dict[str, Any] = {}
        for name in self._PROBE_METHODS:
            original = getattr(probe, name, None)
            if not callable(original):
                continue

            @wraps(original)
            def wrapped(*args: Any, __name: str = name, __original: Any = original, **kwargs: Any) -> Any:
                token = self.begin_transfer(__name, args, kwargs)
                if token is None:
                    return __original(*args, **kwargs)
                try:
                    result = __original(*args, **kwargs)
                except BaseException as exc:
                    self.complete_transfer(token, error=exc)
                    raise
                async_result = token.get("async") and callable(result)
                if async_result:
                    # The callback may be completed on a different thread. The
                    # call itself is finished, so do not leave thread-local
                    # nesting state pinned until that callback arrives.
                    self._leave_transfer(token)
                    @wraps(result)
                    def callback(*callback_args: Any, **callback_kwargs: Any) -> Any:
                        try:
                            callback_result = result(*callback_args, **callback_kwargs)
                        except BaseException as exc:
                            self.complete_transfer(token, error=exc)
                            raise
                        self.complete_transfer(token, result=callback_result)
                        return callback_result
                    return callback
                self.complete_transfer(token, result=result)
                return result

            try:
                setattr(probe, name, wrapped)
            except (AttributeError, TypeError):
                continue
            originals[name] = original
        self._bindings[id(probe)] = (probe, originals)

    def bind_probe(self, probe: Any) -> None:
        """Register a probe and wrap it only while capture is enabled."""
        if probe is None:
            return
        self._probes[id(probe)] = probe
        if self._enabled:
            self._bind_probe(probe)

    def _unbind_probe(self, probe_id: int) -> None:
        binding = self._bindings.pop(probe_id, None)
        if binding is None:
            return
        bound_probe, originals = binding
        for name, original in originals.items():
            try:
                setattr(bound_probe, name, original)
            except (AttributeError, TypeError):
                pass

    def unbind_probe(self, probe: Any) -> None:
        if probe is None:
            return
        self._probes.pop(id(probe), None)
        self._unbind_probe(id(probe))

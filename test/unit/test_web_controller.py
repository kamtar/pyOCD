from pathlib import Path
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyocd.web.controller import WebController, WebError
from pyocd.web import controller as web_controller
from pyocd.core.exceptions import TransferError
from pyocd.core.target import Target
from pyocd.tools import lists


def test_upload_hides_server_path(tmp_path):
    controller = WebController(str(tmp_path))
    uploaded = controller.upload("../firmware.bin", b"abc")
    assert uploaded["name"] == "firmware.bin"
    assert "path" not in controller.snapshot()["artifacts"][0]
    assert len(list(Path(tmp_path).iterdir())) == 1
    controller.close()


def test_upload_with_same_name_gets_windows_style_suffix(tmp_path):
    controller = WebController(str(tmp_path))
    first = controller.upload("firmware.bin", b"old")
    second = controller.upload("firmware.bin", b"new content")
    third = controller.upload("firmware.bin", b"newest content")

    artifacts = controller.snapshot()["artifacts"]
    assert first["name"] == "firmware.bin"
    assert second["name"] == "firmware_1.bin"
    assert third["name"] == "firmware_2.bin"
    assert len(artifacts) == 3
    assert (Path(tmp_path) / f"{first['id']}-firmware.bin").read_bytes() == b"old"
    assert (Path(tmp_path) / f"{second['id']}-firmware_1.bin").read_bytes() == b"new content"
    assert (Path(tmp_path) / f"{third['id']}-firmware_2.bin").read_bytes() == b"newest content"
    controller.close()


def test_delete_artifact_removes_uploaded_file(tmp_path):
    controller = WebController(str(tmp_path))
    artifact = controller.upload("firmware.bin", b"content")
    path = Path(tmp_path) / f"{artifact['id']}-firmware.bin"

    assert controller.delete_artifact(artifact["id"]) == {
        "deleted": True, "artifact": artifact["id"], "name": "firmware.bin"}
    assert not path.exists()
    assert controller.snapshot()["artifacts"] == []
    with pytest.raises(WebError, match="Uploaded file not found") as error:
        controller.delete_artifact(artifact["id"])
    assert error.value.code == "artifact_not_found"
    controller.close()


def test_memory_read_limit_is_checked_before_session(tmp_path):
    controller = WebController(str(tmp_path))
    with pytest.raises(WebError, match="Length must be") as error:
        controller.read_memory(0, controller.MAX_MEMORY_READ + 1)
    assert error.value.code == "invalid_length"
    controller.close()


def test_unsafe_console_rejected_before_connection(tmp_path):
    controller = WebController(str(tmp_path), unsafe_console=False)
    with pytest.raises(WebError) as error:
        controller.console("! whoami")
    assert error.value.code == "unsafe_console_disabled"
    controller.close()


def test_system_power_is_disabled_outside_linux(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    monkeypatch.setattr(web_controller.sys, "platform", "win32")
    monkeypatch.setattr(web_controller.subprocess, "run",
                        lambda *args, **kwargs: pytest.fail("power command was executed"))
    assert controller.system_info()["system_power_supported"] is False
    with pytest.raises(WebError) as error:
        controller.system_power("reboot")
    assert error.value.code == "system_power_unsupported"
    controller.close()


def test_system_power_uses_fixed_linux_commands(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    calls = []
    monkeypatch.setattr(web_controller.sys, "platform", "linux")
    monkeypatch.setattr(web_controller.subprocess, "run",
                        lambda command, **kwargs: calls.append(command))
    assert controller.system_power("reboot") == {"accepted": True, "action": "reboot"}
    assert controller.system_power("shutdown") == {"accepted": True, "action": "shutdown"}
    assert calls == [["systemctl", "reboot"], ["systemctl", "poweroff"]]
    with pytest.raises(WebError) as error:
        controller.system_power("invalid")
    assert error.value.code == "invalid_power_action"
    controller.close()


def test_job_progress_remains_observable(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(controller, "_require_exclusive", lambda: object())

    def work(job):
        job.progress = 0.5
        web_controller.logging.getLogger("pyocd.flash.test").warning("Erasing test flash")
        started.set()
        assert release.wait(2)

    job = controller.start_job("test", work)
    assert started.wait(2)
    snapshot = controller.snapshot()
    assert snapshot["state"] == "busy"
    assert next(item for item in snapshot["jobs"] if item["id"] == job["id"])["progress"] == 0.5
    events = next(item for item in snapshot["jobs"] if item["id"] == job["id"])["events"]
    assert any(item["message"] == "Erasing test flash" for item in events)
    release.set()
    controller.close()


def test_job_reserves_target_before_worker_is_scheduled(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    target = SimpleNamespace(resume=lambda: pytest.fail("target action ran during job"))
    controller._session = SimpleNamespace(
        is_open=True, target=target, gdbservers={}, close=lambda: None)
    controller._session_metadata = {
        "target": {"state": "halted", "locked": False},
        "probe": {"unique_id": "probe"},
    }
    monkeypatch.setattr(controller._executor, "submit", lambda fn: None)

    job = controller.start_job("program", lambda item: None)

    assert job["state"] == "running"
    assert controller.snapshot()["state"] == "busy"
    with pytest.raises(WebError) as error:
        controller.target_action("resume")
    assert error.value.code == "operation_busy"
    controller._session = None
    controller.close()


def test_busy_snapshot_does_not_access_target_hardware(tmp_path):
    controller = WebController(str(tmp_path))

    def unexpected_hardware_access():
        pytest.fail("snapshot accessed target hardware during a job")

    core = SimpleNamespace(core_number=0)
    target = SimpleNamespace(
        get_state=unexpected_hardware_access, is_locked=unexpected_hardware_access,
        vendor="Vendor", part_number="Part", cores={0: core}, selected_core=core,
        memory_map=[])
    probe = SimpleNamespace(
        unique_id="probe", vendor_name="Vendor", product_name="Probe",
        wire_protocol=None)
    controller._session = SimpleNamespace(
        is_open=True, target=target, probe=probe,
        board=SimpleNamespace(target_type="part"),
        options=SimpleNamespace(get=lambda key: 1000000))
    controller._state = web_controller.ConnectionState.BUSY

    snapshot = controller.snapshot()

    assert snapshot["target"]["state"] == "busy"
    assert snapshot["target"]["locked"] is None
    controller._session = None
    controller.close()


def test_busy_snapshot_uses_cached_metadata_without_probe_properties(tmp_path):
    controller = WebController(str(tmp_path))

    class Session:
        is_open = True

        @property
        def target(self):
            pytest.fail("busy snapshot accessed the target")

        @property
        def probe(self):
            pytest.fail("busy snapshot accessed the probe")

    controller._session = Session()
    controller._session_metadata = {
        "target": {
            "name": "part", "vendor": "Vendor", "part_number": "Part",
            "state": "halted", "locked": False, "cores": [0],
            "selected_core": 0, "memory_map": [],
        },
        "probe": {
            "unique_id": "probe", "vendor": "Vendor", "product": "Probe",
            "protocol": "swd", "frequency": 1_000_000,
        },
    }
    controller._target_locked = False
    controller._state = web_controller.ConnectionState.BUSY

    snapshot = controller.snapshot()

    assert snapshot["target"]["state"] == "busy"
    assert snapshot["probe"]["unique_id"] == "probe"
    controller._session = None
    controller.close()


def test_stack_transfer_fault_returns_partial_diagnostic(tmp_path):
    controller = WebController(str(tmp_path))
    core = SimpleNamespace(
        State=Target.State,
        get_state=lambda: Target.State.HALTED,
        read_core_registers_raw=lambda names: [0x10010000, 0x20000000, 0x10010000,
                                               0x123, 0x456, 0x1000000, 2],
        read_memory_block32=lambda address, count: (_ for _ in ()).throw(
            RuntimeError("FAULT ACK")),
    )
    readable = SimpleNamespace(is_readable=True, end=0x10010100)
    memory_map = SimpleNamespace(get_region_for_address=lambda address: readable)
    target = SimpleNamespace(cores={0: core}, memory_map=memory_map, elf=None)
    controller._session = SimpleNamespace(is_open=True, target=target)

    result = controller.stack()

    assert result["stack_pointer"] == 0x10010000
    assert result["words"] == []
    assert "FAULT ACK" in result["warnings"][0]
    controller._session = None
    controller.close()


def test_shutdown_removes_elf_but_keeps_firmware_bin(tmp_path):
    controller = WebController(str(tmp_path))
    controller.upload("symbols.elf", b"elf")
    controller.upload("firmware.bin", b"bin")

    controller.close()

    assert not list(tmp_path.glob("*.elf"))
    assert len(list(tmp_path.glob("*.bin"))) == 1


def test_logs_can_be_cleared(tmp_path):
    controller = WebController(str(tmp_path))
    controller._log_handler.records.append(
        {"time": 1.0, "level": "INFO", "logger": "pyocd.test", "message": "hello"})
    assert controller.logs()["count"] == 1
    controller.clear_logs()
    assert controller.logs()["count"] == 0
    controller.close()


def test_pack_catalog_search_and_install(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    ref = SimpleNamespace(vendor="Keil", pack="STM32F4xx_DFP", version="2.17.1",
                          get_pack_name=lambda: "Keil.STM32F4xx_DFP.pack")

    class Cache:
        data_path = str(tmp_path)
        index = {"STM32F401RE": {
            "name": "STM32F401RE", "vendor": "STMicroelectronics:13"}}

        def packs_for_devices(self, devices):
            return [ref]

        def download_pack_list(self, refs):
            self.downloaded = refs

    cache = Cache()
    populated = []
    controller._target_catalog_path.write_text('{"targets": []}', encoding="utf-8")
    monkeypatch.setattr(controller, "_pack_cache", lambda: cache)
    monkeypatch.setattr(
        "pyocd.web.controller.pack_target.ManagedPacks.populate_target", populated.append)

    result = controller.pack_search("f401")
    installed = controller.pack_install("stm32f401re")

    assert result["devices"] == [{
        "name": "STM32F401RE", "vendor": "STMicroelectronics",
        "pack": "Keil.STM32F4xx_DFP", "version": "2.17.1", "installed": False}]
    assert installed["device"] == "STM32F401RE"
    assert cache.downloaded == [ref]
    assert populated == ["STM32F401RE"]
    assert not controller._target_catalog_path.exists()
    controller.close()


def test_connection_profile_is_persisted_and_reloaded(tmp_path):
    config = tmp_path / "web.json"
    profile = {"interface_name": "Lab bench", "target_override": "stm32f103rc", "gpio": {
        "swclk": 11, "swdio": 8, "nreset": 25}}
    controller = WebController(str(tmp_path / "artifacts"), config_path=str(config))
    controller.save_profile(profile)
    controller.close()

    restored = WebController(str(tmp_path / "artifacts2"), config_path=str(config))
    assert restored.snapshot()["profile"] == profile
    restored.close()


def test_web_connection_defaults_use_watchdog_safe_connect_mode(tmp_path):
    controller = WebController(str(tmp_path), config_path=str(tmp_path / "web.json"))

    assert controller.snapshot()["connection_defaults"] == {
        "frequency": 1_000_000,
        "connect_mode": "under-reset",
        "dap_protocol": "default",
    }
    controller.close()


def test_interface_name_length_is_limited(tmp_path):
    controller = WebController(str(tmp_path), config_path=str(tmp_path / "web.json"))
    with pytest.raises(WebError) as error:
        controller.save_profile({"interface_name": "x" * 49})
    assert error.value.code == "invalid_interface_name"
    controller.close()


def test_profile_can_be_saved_before_specific_mcu_is_selected(tmp_path):
    controller = WebController(str(tmp_path), config_path=str(tmp_path / "web.json"))
    saved = controller.save_profile({"interface_name": "Bench", "target_override": "cortex_m"})
    assert saved["interface_name"] == "Bench"
    controller.close()


def test_target_metadata_session_has_probe_without_creating_board(monkeypatch):
    seen = []

    class ProbeRequiredTarget:
        vendor = "Vendor"
        part_families = []
        part_number = "ProbeRequired"
        _svd_location = None

        def __init__(self, session):
            assert session.probe is not None
            assert session.board is None
            seen.append(session.probe.unique_id)

    monkeypatch.setattr(lists, "TARGET", {"probe_required": ProbeRequiredTarget})
    monkeypatch.setattr(lists.pack_target.ManagedPacks, "get_installed_targets", lambda: [])

    result = lists.ListGenerator.list_targets()

    assert seen == ["0"]
    assert result["targets"][0]["name"] == "probe_required"


def test_target_actions_work_without_browser_debugger(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path), config_path=str(tmp_path / "web.json"))
    calls = []
    target = SimpleNamespace(
        halt=lambda: calls.append("halt"), resume=lambda: calls.append("resume"),
        reset=lambda reset_type=None: calls.append(("reset", reset_type)),
        reset_and_halt=lambda reset_type=None: calls.append("reset-halt"),
        step=lambda: calls.append("step"))
    monkeypatch.setattr(controller, "_require_exclusive", lambda: SimpleNamespace(target=target))
    monkeypatch.setattr(controller, "snapshot", lambda: {"connected": True})

    for action in ("halt", "reset-halt", "resume", "reset", "reset-hardware"):
        controller.target_action(action)

    assert calls == [
        "halt", "reset-halt", "resume",
        ("reset", Target.ResetType.CORE),
        ("reset", Target.ResetType.HARDWARE),
    ]
    controller.close()


def test_two_image_plan_uses_offsets_and_preserves_first_image(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    first = controller.upload("application.bin", b"app")
    second = controller.upload("bootloader.bin", b"boot")
    calls = []

    class Programmer:
        def __init__(self, session, progress, chip_erase, **kwargs):
            self.progress = progress
            self.erase = chip_erase

        def program(self, path, **kwargs):
            calls.append((Path(path).name, self.erase, kwargs.get("base_address")))
            self.progress(1.0)

    monkeypatch.setattr("pyocd.web.controller.FileProgrammer", Programmer)
    target_calls = []
    target = SimpleNamespace(
        reset=lambda reset_type=None: target_calls.append(("reset", reset_type)),
        reset_and_halt=lambda reset_type=None: target_calls.append(("reset-halt", reset_type)))
    session = SimpleNamespace(is_open=True, target=target, gdbservers={}, close=lambda: None)
    controller._session = session
    controller.program_images([
        {"artifact_id": first["id"], "base_address": "0x08008000"},
        {"artifact_id": second["id"], "base_address": "0x08000000"},
    ], {"erase": "chip", "post_action": "reset"})

    controller.close()

    assert calls == [
        (next(tmp_path.glob("*-application.bin")).name, None, 0x08008000),
        (next(tmp_path.glob("*-bootloader.bin")).name, "sector", 0x08000000),
    ]
    assert target_calls == [
        ("reset-halt", Target.ResetType.HARDWARE),
        ("reset", Target.ResetType.HARDWARE),
    ]


@pytest.mark.parametrize("target_data, expected_state", [(b"app", "completed"), (b"bad", "failed")])
def test_verify_image_reads_flash_and_detects_mismatch(tmp_path, target_data, expected_state):
    controller = WebController(str(tmp_path))
    artifact = controller.upload("application.bin", b"app")
    start = 0x08000000
    region = SimpleNamespace(
        start=start, end=start + len(target_data) - 1,
        is_flash=True, is_powered_on_boot=True, flash=None)
    memory_map = SimpleNamespace(
        get_boot_memory=lambda: region,
        get_region_for_address=lambda address: region)
    target = SimpleNamespace(
        memory_map=memory_map,
        read_memory_block8=lambda address, length: list(
            target_data[address - start:address - start + length]))
    controller._session = SimpleNamespace(
        is_open=True, target=target, gdbservers={}, close=lambda: None)

    accepted = controller.verify(artifact["id"], {"base_address": hex(start)})
    controller.close()
    job = next(item for item in controller.snapshot()["jobs"]
               if item["id"] == accepted["id"])

    assert job["state"] == expected_state
    if expected_state == "failed":
        assert "Flash verification failed" in job["error"]


def test_post_reset_transfer_error_does_not_fail_completed_program(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    artifact = controller.upload("application.elf", b"elf")
    closed = []

    class Programmer:
        def __init__(self, session, progress, **kwargs):
            self.progress = progress

        def program(self, path, **kwargs):
            self.progress(0.68)

    class Options:
        def is_set(self, name):
            return False

        def set(self, name, value):
            assert (name, value) == ("resume_on_disconnect", False)

    target = SimpleNamespace(
        reset_and_halt=lambda reset_type=None: None,
        reset=lambda reset_type=None: (_ for _ in ()).throw(TransferError("No ACK")))
    session = SimpleNamespace(
        is_open=True, target=target, options=Options(),
        context_state=SimpleNamespace(suppress_disconnect_error=False),
        gdbservers={}, close=lambda: closed.append(True))
    controller._session = session
    monkeypatch.setattr("pyocd.web.controller.FileProgrammer", Programmer)

    accepted = controller.program(artifact["id"], {"post_action": "reset"})
    controller.close()
    job = next(item for item in controller.snapshot()["jobs"]
               if item["id"] == accepted["id"])

    assert job["state"] == "completed"
    assert job["progress"] == 1.0
    assert job["error"] is None
    assert any("Programming succeeded" in event["message"] for event in job["events"])
    assert closed == [True]
    assert session.context_state.suppress_disconnect_error is True


def test_program_progress_reports_erase_and_program_phases(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    artifact = controller.upload("application.elf", b"elf")
    phase_messages = []

    class Session:
        is_open = True
        gdbservers = {}
        target = SimpleNamespace(reset_and_halt=lambda reset_type=None: None)
        close = lambda self: None

        def subscribe(self, callback, events):
            self.callback = callback

        def unsubscribe(self, callback, events):
            assert callback == self.callback

    session = Session()
    controller._session = session

    class Programmer:
        def __init__(self, session, progress, **kwargs):
            self.progress = progress

        def program(self, path, **kwargs):
            for event, value in ((Target.Event.PRE_FLASH_ERASE, 0.4),
                                 (Target.Event.PRE_FLASH_PROGRAM, 0.6)):
                session.callback(SimpleNamespace(event=event))
                self.progress(value)
                with controller._lock:
                    phase_messages.append(next(iter(controller._jobs.values())).message)

    monkeypatch.setattr("pyocd.web.controller.FileProgrammer", Programmer)
    controller.program(artifact["id"], {"post_action": "none"})
    controller.close()

    assert phase_messages == [
        "Erasing application.elf · 40%",
        "Programming application.elf · 60%",
    ]


def test_gdb_progress_reports_current_flash_phase(tmp_path):
    controller = WebController(str(tmp_path))

    controller._gdb_flash_activity(0, "erase", length=4096)
    controller._gdb_flash_activity(0, "buffered", bytes_total=4096)
    controller._gdb_flash_activity(0, "phase", phase="erase")
    controller._gdb_flash_activity(0, "progress", progress=0.25, bytes_total=4096)
    erase_job = controller.snapshot()["jobs"][0]
    controller._gdb_flash_activity(0, "phase", phase="program")
    controller._gdb_flash_activity(0, "progress", progress=0.5, bytes_total=4096)
    program_job = controller.snapshot()["jobs"][0]
    controller.close()

    assert erase_job["phase"] == "erase"
    assert erase_job["message"] == "Erasing target · 25%"
    assert program_job["phase"] == "program"
    assert program_job["message"] == "Programming target · 50%"


def test_raspberry_pi_gpio_is_hidden_on_windows(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    monkeypatch.setattr("pyocd.web.controller.sys.platform", "win32")
    monkeypatch.setattr("pyocd.web.controller.ListGenerator.list_probes", lambda: {
        "boards": [
            {"unique_id": "0", "vendor_name": "Raspberry Pi", "product_name": "GPIO SWD"},
            {"unique_id": "cmsis", "vendor_name": "Arm", "product_name": "CMSIS-DAP"},
        ]})

    result = controller.probes()

    assert [probe["unique_id"] for probe in result["boards"]] == ["cmsis"]
    controller.close()


def test_target_pack_info_lists_vendor_sequences(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    device = SimpleNamespace(
        sequences=[SimpleNamespace(name="ResetSystem", pname="cm4", info="Vendor reset",
                                   is_enabled=True)],
        pack_description=SimpleNamespace(pack_name="Vendor.Device_DFP"),
        pack_path="Vendor.Device_DFP.pack",
        processors_map={"cm4": object()},
    )
    monkeypatch.setitem(web_controller.TARGET, "web_pack_test", type(
        "WebPackTarget", (), {"_pack_device": device}))

    result = controller.target_pack_info("web_pack_test")

    assert result["source"] == "pack"
    assert result["pack"]["name"] == "Vendor.Device_DFP"
    assert result["sequences"] == [{
        "name": "ResetSystem", "pname": "cm4", "info": "Vendor reset", "enabled": True}]
    controller.close()


def test_targets_deduplicates_populated_managed_pack_devices(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    monkeypatch.setattr(web_controller.ListGenerator, "list_targets", lambda name_filter=None: {
        "targets": [
            {"name": "mimxrt1011xxxxx", "source": "pack", "vendor": "NXP"},
            {"name": "mimxrt1011xxxxx", "source": "pack", "vendor": "NXP"},
        ]})

    assert controller.targets()["targets"] == [
        {"name": "mimxrt1011xxxxx", "source": "pack", "vendor": "NXP"}]
    controller.close()


def test_targets_catalog_is_disk_cached_and_can_be_invalidated(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    calls = []

    def list_targets(name_filter=None):
        calls.append(name_filter)
        return {"targets": [{"name": "cached-target", "source": "builtin"}]}

    monkeypatch.setattr(web_controller.ListGenerator, "list_targets", list_targets)

    first = controller.targets()
    assert controller._target_catalog_path.exists()
    first["targets"].clear()
    assert controller.targets()["targets"] == [{"name": "cached-target", "source": "builtin"}]
    assert calls == [None]

    controller._invalidate_target_catalog()
    assert not controller._target_catalog_path.exists()
    assert controller.targets()["targets"] == [{"name": "cached-target", "source": "builtin"}]
    assert calls == [None, None]
    controller.close()


def test_run_pack_sequence_validates_and_executes_declared_sequence(tmp_path):
    controller = WebController(str(tmp_path))
    sequence = SimpleNamespace(name="ResetSystem", pname=None)
    calls = []
    delegate = SimpleNamespace(run_sequence=lambda name, pname: calls.append((name, pname)) or object())
    target = SimpleNamespace(
        _pack_device=SimpleNamespace(sequences=[sequence]), debug_sequence_delegate=delegate)
    controller._session = SimpleNamespace(is_open=True, target=target)

    assert controller.run_pack_sequence("ResetSystem") == {
        "ran": True, "name": "ResetSystem", "pname": None}
    assert calls == [("ResetSystem", None)]
    with pytest.raises(WebError) as error:
        controller.run_pack_sequence("DebugPortStop")
    assert error.value.code == "pack_sequence_not_found"
    controller._session = None
    controller.close()


def test_target_recovery_reports_family_implementation(tmp_path):
    controller = WebController(str(tmp_path))

    class Kinetis:
        def check_flash_security(self):
            pass

        def mass_erase(self):
            pass

    class Device(Kinetis):
        pass

    assert controller._target_recovery_info(Device) == {
        "available": True,
        "implementation": "Kinetis MDM-AP mass erase",
        "handler": f"{Kinetis.__module__}.Kinetis",
        "automatic": True,
    }
    controller.close()


def test_unlock_target_uses_target_mass_erase(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    calls = []
    target = SimpleNamespace(
        mass_erase=lambda: calls.append("erase") or True,
        is_locked=lambda: False,
    )
    controller._session = SimpleNamespace(is_open=True, target=target)
    monkeypatch.setattr(controller, "snapshot", lambda: {"connected": True})

    assert controller.unlock_target() == {"connected": True}
    assert calls == ["erase"]
    assert controller._target_locked is False
    controller._session = None
    controller.close()


def test_force_raspberry_pi_gpio_adds_preview_adapter(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path), force_rpi=True)
    monkeypatch.setattr("pyocd.web.controller.sys.platform", "win32")
    monkeypatch.setattr(
        "pyocd.web.controller.ListGenerator.list_probes",
        lambda: {"boards": []})

    result = controller.probes()

    assert result["boards"] == [{
        "unique_id": "rpi-gpio:",
        "info": "Raspberry Pi GPIO SWD (preview only)",
        "board_vendor": "Raspberry Pi",
        "board_name": "GPIO header",
        "target": "cortex_m",
        "vendor_name": "Raspberry Pi",
        "product_name": "GPIO SWD",
        "preview_only": True,
    }]
    controller.close()


def test_connect_defaults_to_first_available_probe(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    selected = []

    monkeypatch.setattr(
        "pyocd.web.controller.ConnectHelper.choose_probe",
        lambda **kwargs: selected.append(kwargs) or None)
    with pytest.raises(WebError) as error:
        controller.connect({"target_override": "stm32f103rc"})

    assert error.value.code == "probe_not_found"
    assert selected == [{"blocking": False, "return_first": True, "unique_id": None}]
    controller.close()


def test_connect_uses_watchdog_safe_mode_when_omitted(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    selected = []
    options_seen = {}

    monkeypatch.setattr(
        "pyocd.web.controller.ConnectHelper.choose_probe",
        lambda **kwargs: selected.append(kwargs) or object())

    class FakeSession:
        def __init__(self, probe, options):
            options_seen.update(options)

        def open(self):
            raise RuntimeError("stop after inspecting options")

        def close(self):
            pass

    monkeypatch.setattr("pyocd.web.controller.Session", FakeSession)

    with pytest.raises(RuntimeError, match="stop after inspecting options"):
        controller.connect({"target_override": "stm32f103rc"})

    assert options_seen["connect_mode"] == "under-reset"
    assert selected == [{"blocking": False, "return_first": True, "unique_id": None}]
    controller.close()


def test_connect_resets_and_halts_kinetis_under_reset(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    calls = []

    class FakeKinetis:
        pass

    class FakeTarget(FakeKinetis):
        def is_locked(self):
            return False

        def reset_and_halt(self):
            calls.append("reset_and_halt")

    class FakeSession:
        is_open = True

        def __init__(self, probe, options):
            self.target = FakeTarget()
            self.options = SimpleNamespace(get=lambda key: options.get(key))

        def open(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(web_controller, "Kinetis", FakeKinetis)
    monkeypatch.setattr(
        "pyocd.web.controller.ConnectHelper.choose_probe", lambda **kwargs: object())
    monkeypatch.setattr("pyocd.web.controller.Session", FakeSession)
    monkeypatch.setattr(
        "pyocd.web.controller.CommandExecutionContext",
        lambda **kwargs: SimpleNamespace(attach_session=lambda session: None))
    monkeypatch.setattr(controller, "snapshot", lambda: {"connected": True})

    controller.connect({"probe": "probe", "target_override": "mkl17z256vft4"})

    assert calls == ["reset_and_halt"]
    controller.close()


def test_probe_reset_opens_adapter_without_initialising_target(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path), config_path=str(tmp_path / "web.json"))
    calls = []

    class Probe:
        unique_id = "probe"

        def reset(self):
            calls.append("reset")

    class ProbeSession:
        def __init__(self, probe, auto_open, options):
            calls.append(("session", auto_open, options))

        def open(self, init_board=True):
            calls.append(("open", init_board))

        def close(self):
            calls.append("close")

    monkeypatch.setattr(
        "pyocd.web.controller.ConnectHelper.choose_probe", lambda **kwargs: Probe())
    monkeypatch.setattr("pyocd.web.controller.Session", ProbeSession)

    result = controller.pulse_probe_reset({
        "probe": "probe", "frequency": 1_000_000,
        "gpio": {"nreset": 16, "swclk": 20, "swdio": 21},
    })

    assert result == {"reset": True, "probe": "probe"}
    assert ("open", False) in calls
    assert "reset" in calls
    assert calls[-1] == "close"
    controller.close()


def test_probe_reset_defaults_to_first_available_probe(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path), config_path=str(tmp_path / "web.json"))
    selected = []
    monkeypatch.setattr(
        "pyocd.web.controller.ConnectHelper.choose_probe",
        lambda **kwargs: selected.append(kwargs) or None)

    with pytest.raises(WebError) as error:
        controller.pulse_probe_reset({})

    assert error.value.code == "probe_not_found"
    assert selected == [{"blocking": False, "return_first": True, "unique_id": None}]
    controller.close()


def test_connect_rejects_host_execution_options(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    monkeypatch.setattr(
        "pyocd.web.controller.ConnectHelper.choose_probe", lambda **kwargs: object())

    with pytest.raises(WebError) as error:
        controller.connect({
            "probe": "probe", "target_override": "stm32f103rc",
            "options": {"user_script": "uploaded.py"},
        })

    assert error.value.code == "unsupported_options"
    controller.close()


def test_connect_closes_local_session_when_console_setup_fails(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    closed = []

    class FakeSession:
        def __init__(self, probe, options):
            self.target = SimpleNamespace(is_locked=lambda: False)

        def open(self):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "pyocd.web.controller.ConnectHelper.choose_probe", lambda **kwargs: object())
    monkeypatch.setattr("pyocd.web.controller.Session", FakeSession)
    monkeypatch.setattr(
        "pyocd.web.controller.CommandExecutionContext",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("console failed")))

    with pytest.raises(RuntimeError, match="console failed"):
        controller.connect({"probe": "probe", "target_override": "stm32f103rc"})

    assert closed == [True]
    controller.close()


def test_gdb_start_rolls_back_partial_startup(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path), serve_local_only=False)
    stopped = []
    option_values = {}

    class FakeServer:
        def __init__(self, session, core):
            self.core = core

        def start(self):
            if self.core == 1:
                raise RuntimeError("bind failed")

        def stop(self):
            stopped.append(self.core)

    options = SimpleNamespace(set=lambda key, value: option_values.__setitem__(key, value))
    session = SimpleNamespace(
        is_open=True, target=SimpleNamespace(cores={0: object(), 1: object()}),
        options=options, gdbservers={})
    controller._session = session
    monkeypatch.setattr("pyocd.web.controller.GDBServer", FakeServer)

    with pytest.raises(RuntimeError, match="bind failed"):
        controller.gdb_start(3333, [0, 1])

    assert stopped == [0, 1]
    assert option_values["serve_local_only"] is False
    assert option_values["persist"] is True
    assert controller._gdb == {}
    assert session.gdbservers == {}
    controller._session = None
    controller.close()


def test_browser_debugger_supplies_frames_locals_registers_and_memory(tmp_path):
    controller = WebController(str(tmp_path))

    class FakeDebugger:
        is_alive = True
        executable = "arm-none-eabi-gdb"

        def command(self, command, timeout=10.0):
            if command == "-stack-list-frames":
                return {"stack": [{"frame": {"level": "0", "func": "main",
                                                "file": "main.c", "line": "7"}}]}
            if command == "-stack-list-variables --all-values":
                return {"variables": [{"name": "counter", "value": "3"}]}
            if command.startswith("-var-create"):
                return {"value": "3", "type": "int", "numchild": "0"}
            if command == "-data-list-register-names":
                return {"register-names": ["r0", "pc"]}
            if command == "-data-list-register-values x":
                return {"register-values": [
                    {"number": "0", "value": "0x00000003"},
                    {"number": "1", "value": "0x08000100"},
                ]}
            if command.startswith("-data-read-memory-bytes"):
                return {"memory": [{"begin": "0x20000000", "contents": "01020304"}]}
            if command.startswith("-var-delete"):
                return {}
            raise AssertionError(command)

        def close(self):
            self.is_alive = False

    controller._debugger = FakeDebugger()
    controller._debugger_state = "stopped"

    assert controller.debug_frames()["frames"][0]["func"] == "main"
    assert controller.debug_locals()["variables"][0] == {
        "handle": "webvar1", "name": "counter", "value": "3",
        "type": "int", "numchild": 0, "arg": False,
    }
    assert controller.registers()["registers"]["pc"] == 0x08000100
    assert controller.read_memory(0x20000000, 4) == b"\x01\x02\x03\x04"
    controller._debugger = None
    controller.close()


def test_browser_debugger_rejects_inspection_while_running(tmp_path):
    controller = WebController(str(tmp_path))
    controller._debugger = SimpleNamespace(is_alive=True)
    controller._debugger_state = "running"

    with pytest.raises(WebError) as error:
        controller.debug_frames()

    assert error.value.code == "target_running"
    controller._debugger = None
    controller.close()


def test_state_poll_does_not_touch_target_hardware_while_gdb_owns_probe(tmp_path):
    controller = WebController(str(tmp_path))
    target = SimpleNamespace(
        vendor="Vendor", part_number="Part", cores={0: object()},
        selected_core=SimpleNamespace(core_number=0), memory_map=[],
        get_state=lambda: (_ for _ in ()).throw(AssertionError("hardware state read")),
        is_locked=lambda: (_ for _ in ()).throw(AssertionError("lock state read")),
    )
    probe = SimpleNamespace(
        unique_id="probe", vendor_name="Vendor", product_name="Probe",
        wire_protocol=None)
    controller._session = SimpleNamespace(
        is_open=True, target=target, probe=probe,
        board=SimpleNamespace(target_type="cortex_m"),
        options=SimpleNamespace(get=lambda key: 1000000),
        gdbservers={}, close=lambda: None)
    controller._gdb[0] = SimpleNamespace(
        port=3333, is_alive=lambda: True, client_sessions=[],
        is_target_running=False, stop=lambda: None)
    controller._target_locked = False

    snapshot = controller.snapshot()

    assert snapshot["target"]["state"] == "halted"
    assert snapshot["target"]["locked"] is False
    controller._gdb.clear()
    controller._session = None
    controller.close()


def test_halt_reconciles_state_when_gdb_omits_async_stop(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    commands = []

    class FakeDebugger:
        is_alive = True
        executable = "gdb"

        def command(self, command, timeout=10.0):
            commands.append(command)
            return {"frame": {"func": "main"}} if command == "-stack-info-frame" else {}

    controller._debugger = FakeDebugger()
    controller._debugger_state = "running"
    monkeypatch.setattr(controller._debugger_stopped, "wait", lambda timeout: False)

    result = controller.target_action("halt")

    assert commands == ["-exec-interrupt", "-stack-info-frame"]
    assert result["debugger"]["state"] == "stopped"
    controller._debugger = None
    controller.close()


def test_gdb_async_stop_updates_browser_state(tmp_path):
    controller = WebController(str(tmp_path))
    controller._debugger_state = "running"

    controller._debugger_event(SimpleNamespace(prefix="*", cls="stopped"))

    assert controller._debugger_state == "stopped"
    assert controller._debugger_stopped.is_set()
    controller.close()


def test_browser_reset_continues_target_after_reset(tmp_path):
    controller = WebController(str(tmp_path))
    commands = []

    class FakeDebugger:
        is_alive = True
        executable = "gdb"

        def command(self, command, timeout=10.0):
            commands.append(command)
            return {}

    controller._debugger = FakeDebugger()
    controller._debugger_state = "stopped"

    controller.target_action("reset")

    assert commands == [
        "-interpreter-exec console \"monitor reset core\"",
        "-exec-continue",
    ]
    assert controller._debugger_state == "running"
    controller._debugger = None
    controller.close()


def test_hardware_reset_recovers_link_without_disconnecting(tmp_path, monkeypatch):
    controller = WebController(str(tmp_path))
    recoveries = []
    dp = SimpleNamespace(post_reset_recovery=lambda: recoveries.append(True))
    target = SimpleNamespace(
        dp=dp,
        reset=lambda reset_type: (_ for _ in ()).throw(TransferError("No ACK")),
    )
    session = SimpleNamespace(is_open=True, target=target)
    controller._session = session
    monkeypatch.setattr(controller, "snapshot", lambda: {"connected": True})

    result = controller.target_action("reset-hardware")

    assert result == {"connected": True}
    assert recoveries == [True]
    assert controller._session is session
    controller._session = None
    controller.close()

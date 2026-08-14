---
title: Web HTTP API
---

The `pyocd web` command includes a versioned HTTP API for small, programmatic
control tasks. It is the same API used by the browser interface, so a client
can discover probes and targets, connect a session, start a GDB server, issue
basic target actions, and disconnect without using the browser.

Start the service locally:

```console
pyocd web --host 127.0.0.1 --port 8080
```

The base URL is `http://127.0.0.1:8080/api/v1`. The API returns JSON unless
noted otherwise. Mutating requests use `POST`, `PUT`, or `DELETE`.

## Authentication and request headers

Loopback binds are unauthenticated by default. Tokenless mutating requests
must include the following header to protect against cross-site requests:

```http
X-pyOCD-CSRF: 1
```

For a non-loopback bind, pyOCD requires a bearer token. Set one explicitly with
`--auth-token` or `--auth-token-file`; otherwise the default token is `cutter`.
Send it as:

```http
Authorization: Bearer <token>
```

Bearer-authenticated clients do not need the CSRF header. Do not use
`--insecure` outside an isolated development network.

All API errors have this shape:

```json
{
  "error": {
    "code": "not_connected",
    "message": "Connect to a target first"
  }
}
```

## Minimal control flow

The usual sequence is:

1. `GET /probes` and choose a probe `unique_id`.
2. `GET /targets?q=<text>` and choose a target `name`.
3. `POST /session/connect` with the selected probe and target.
4. `POST /gdb/start` to enable an external GDB endpoint.
5. `GET /state` to observe connection and GDB status.
6. `POST /gdb/stop`, then `POST /session/disconnect` when finished.

For loopback, the following shell example connects the first available probe
to an STM32F103 target and starts a GDB server on port 3333:

```console
curl http://127.0.0.1:8080/api/v1/probes
curl "http://127.0.0.1:8080/api/v1/targets?q=stm32f103"

curl -X POST http://127.0.0.1:8080/api/v1/session/connect ^
  -H "Content-Type: application/json" ^
  -H "X-pyOCD-CSRF: 1" ^
  -d "{\"target_override\":\"stm32f103rc\",\"frequency\":1000000,\"connect_mode\":\"under-reset\"}"

curl -X POST http://127.0.0.1:8080/api/v1/gdb/start ^
  -H "Content-Type: application/json" ^
  -H "X-pyOCD-CSRF: 1" ^
  -d "{\"port\":3333,\"cores\":[0]}"

curl http://127.0.0.1:8080/api/v1/state
```

The line-continuation syntax above is for PowerShell and `cmd.exe`. On a
POSIX shell, replace `^` with `\`.

## Connection profile

`POST /session/connect` accepts a JSON object. `target_override` is required
and must be a specific MCU name returned by `/targets`; the generic
`cortex_m` target is rejected. `probe` is optional and selects a probe by
`unique_id`; when omitted, the first available probe is selected.

```json
{
  "probe": "0240000031234",
  "target_override": "stm32f103rc",
  "frequency": 1000000,
  "connect_mode": "under-reset",
  "dap_protocol": "default",
  "reset_method": "hardware",
  "gpio": {
    "swclk": 20,
    "swdio": 21,
    "nreset": 16
  },
  "options": {
    "auto_unlock": true,
    "resume_on_disconnect": true
  }
}
```

`connect_mode` is one of `halt`, `attach`, `under-reset`, or `pre-reset`. The
web UI and web API default to `under-reset`, which keeps a watchdog-controlled
target from resetting before pyOCD gains debug control; choose another mode
explicitly when needed.
`dap_protocol` is one of `default`, `swd`, or `jtag`. `reset_method` is one of
`hardware` or `core`. The `gpio` object is intended for the Raspberry Pi GPIO
adapter. Only safe session options are accepted in `options`; host execution
options and user scripts are deliberately rejected by the web API.

`POST /session/connect` returns the same state document as `/state`. A
successful connection replaces any previous web session. Disconnecting also
stops GDB servers owned by the web process and releases the probe.

## Core control endpoints

| Method | Path | Purpose | Body |
| --- | --- | --- | --- |
| `GET` | `/health` | Liveness check. | — |
| `GET` | `/state` | Current connection, target, GDB, debugger, jobs, and artifacts. | — |
| `GET` | `/probes` | Enumerate available debug probes. | — |
| `GET` | `/targets?q=<text>` | Enumerate target MCU names; `q` is optional. | — |
| `POST` | `/session/connect` | Open a pyOCD session. | Connection profile. |
| `POST` | `/session/disconnect` | Stop owned services and close the session. | `{}` |
| `POST` | `/gdb/start` | Start one GDB server per selected core. | `{"port":3333,"cores":[0]}` |
| `POST` | `/gdb/stop` | Stop all GDB servers owned by the web process. | `{}` |
| `POST` | `/target/{action}` | Control the target. | `{}` |
| `GET` | `/cores/{core}/registers` | Read core registers. | — |
| `POST` | `/memory/read` | Read target memory as a hexadecimal string. | `{"address":"0x20000000","length":64}` |
| `GET` | `/ws/events` | WebSocket stream of changed state snapshots. | — |

Supported target actions are `halt`, `resume`, `step`, `reset`,
`reset-hardware`, `reset-halt`, `reset-halt-core`, and
`reset-halt-hardware`. Direct target actions require an active session and
cannot run while an external GDB server is active.

The GDB status is included in `/state`:

```json
{
  "connected": true,
  "target": {"name": "stm32f103rc", "state": "halted", "cores": [0]},
  "gdb": [
    {"core": 0, "port": 3333, "running": true, "clients": 0,
     "client_addresses": []}
  ]
}
```

When multiple cores are requested, the first server uses `port` and later
cores use subsequent ports unless an ephemeral port (`0`) is requested. Always
use the ports returned by `/state` rather than assuming them.

## Programming, diagnostics, and advanced controls

The same API also exposes the browser's other operations:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/probe/reset` | Pulse the selected probe's nRESET pin without opening a target session. |
| `POST` | `/target/unlock` | Mass-erase and unlock when the target family supports recovery. |
| `POST` | `/artifacts` | Upload an ELF, AXF, HEX, or BIN file as multipart form data. |
| `DELETE` | `/artifacts/{artifact_id}` | Delete an uploaded file. |
| `POST` | `/elf/attach` | Attach an uploaded ELF/AXF for the browser-owned debugger. |
| `POST` | `/jobs/program` | Start a programming job; returns `202` with a job record. |
| `POST` | `/jobs/verify` | Start a flash verification job against the selected uploaded image(s); returns `202` with a job record. |
| `POST` | `/jobs/erase` | Start an explicit `chip`, `mass`, or `sector` erase job. |
| `POST` | `/memory/dump` | Stream a binary memory dump. |
| `GET` | `/stack?core=0` | Read a stack trace from the selected core. |
| `POST` | `/debug/start` | Start the browser-owned GDB/MI debugger; an ELF must be attached. |
| `POST` | `/debug/stop` | Stop the browser-owned debugger. |
| `GET` | `/logs?after=<timestamp>` | Read captured pyOCD log records. |
| `DELETE` | `/logs` | Clear captured web logs. |
| `POST` | `/console` | Run a commander command; Python and host-shell commands require `--unsafe-console`. |

The browser-owned debugger (`/debug/*`) and external GDB server
(`/gdb/*`) are mutually exclusive. Programming, erase, memory, and direct
target controls are serialized by the web controller and return a `409`
error when the operation is not safe to run in the current state.

## Management and CMSIS-Pack endpoints

These routes support the remaining web-interface pages and are useful for
deployment tooling:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` / `PUT` | `/config` | Read or persist the connection profile. |
| `GET` | `/system` | Read pyOCD version, host, platform, and network information. |
| `POST` | `/system/power/{action}` | Request host `reboot` or `shutdown`; returns `202`. |
| `GET` | `/update/check` | Check the configured pyOCD release source for an update. |
| `POST` | `/runtime/restart` | Request a pyOCD process restart; returns `202`. |
| `GET` | `/targets/{target}/pack` | Read CMSIS-Pack metadata and debug sequences for a target. |
| `GET` | `/packs/devices?q=<text>&limit=100` | Search the CMSIS-Pack device index. |
| `POST` | `/packs/index` | Refresh the CMSIS-Pack index. |
| `POST` | `/packs/install` | Install the pack providing a named device. |
| `POST` | `/pack/sequences/run` | Run a declared CMSIS-Pack debug sequence. |

Host power and process-restart routes affect the machine or the pyOCD
service. Keep them behind authentication and expose them only to trusted
clients.

The browser-debugger-specific routes are:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/debug/frames` | Read GDB call frames. |
| `PUT` | `/debug/frame/{level}` | Select a call frame. |
| `GET` | `/debug/variables/locals` | Read locals for the selected frame. |
| `GET` | `/debug/variables/globals?q=<text>&limit=50` | Search global variables. |
| `GET` | `/debug/variables/{handle}/children` | Expand a variable object. |

## Compatibility notes

Use the `/api/v1` prefix in clients. The API is versioned independently of the
browser assets; clients should ignore response fields they do not need and
use the `code` field in errors for recovery decisions. The web controller owns
the probe and GDB lifetimes, so another pyOCD process must not use the same
probe concurrently.

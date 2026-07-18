# Web control center

The pyOCD web control center is a browser interface for probe selection, target connection,
GDB server control, programming, memory inspection, basic fault analysis, and commander commands.
It is designed for a long-running pyOCD installation such as a Raspberry Pi programmer.

## Installation and startup

Install the optional web dependencies, then start the service:

```console
pip install "pyocd[web]"
pyocd web
```

Open `http://127.0.0.1:8080`. The web process owns the selected probe and any GDB servers it
starts. Do not run a separate pyOCD process against the same probe.

To expose the service on a trusted LAN, authentication is required by default:

```console
pyocd web --host 0.0.0.0 --auth-token-file /etc/pyocd/web-token
```

Use HTTPS at a reverse proxy when traffic leaves the device. `--insecure` permits an unauthenticated
remote bind and is intended only for isolated development networks.

## Raspberry Pi GPIO

Select **Raspberry Pi GPIO SWD** on the Connection page. The GPIO fields use BCM numbering.
The interface exposes SWCLK, SWDIO, optional nRESET, and frequency. Raspberry Pi Zero through Pi 4
are supported; Pi 5 is not supported by the current backend. Use 3.3 V logic and a shared ground.

## Target ownership

Only one target operation runs at a time. Direct browser debug, flash, erase, memory, and console
operations are disabled while GDB is active. Disconnect closes the commander, stops GDB servers,
and releases the probe.

## Console security

Standard commander commands are available. Python (`$`) and host-shell (`!`) commands are disabled
unless started with `--unsafe-console`. Do not combine unsafe mode with an unauthenticated listener.

## Uploaded files

ELF, AXF, Intel HEX, and binary images can be uploaded and programmed. The upload limit is 128 MiB.
Browser filenames are never treated as server paths. Use `--artifact-dir` to select storage.

One or two images can be programmed as a plan, for example a bootloader followed by the main
application. Each BIN image accepts its own numeric base address. Only the first image may use a
chip erase; subsequent images use sector erase to preserve the first image.

An attached ELF is temporary. Replacing it deletes the previously attached file, disconnecting
deletes the active ELF, and server shutdown removes all uploaded ELF/AXF artifacts.

## Dumps and logs

The Program & dump page can download a custom memory range or all readable flash/RAM regions as
binary files. Register snapshots are downloaded as JSON with target and probe metadata. Custom
memory dumps are streamed in chunks and are limited to 512 MiB per file.

The pyOCD log page captures recent messages from the pyOCD logger and supports live viewing,
clearing, and download. The top-bar GDB state is clickable and starts or stops the server.

## Browser debugger

The Debug page can run a GDB client inside the web process. Attach an ELF or AXF file that
contains DWARF debug information, halt the target, and select **Start debugger**. The browser
then shows GDB-unwound call frames, frame-scoped arguments and local variables, searchable
global variables, expandable arrays/structures, registers, and memory. Selecting a call frame
updates the Locals view to that frame.

The web process searches for `arm-none-eabi-gdb`, `gdb-multiarch`, and `gdb`, in that order.
Use an explicit executable when auto-detection is not appropriate:

```console
pyocd web --gdb-executable /opt/gcc-arm-none-eabi/bin/arm-none-eabi-gdb
```

Only one debugger may own a core. The browser-managed debugger and the external GDB-server
mode are therefore mutually exclusive. Programming, erase, ELF replacement, and commander
operations require the browser debugger to be stopped first.

Variable availability depends on the ELF's debug information and compiler optimisation.
Optimised-out variables are reported as unavailable by GDB, and firmware without DWARF data
cannot provide source-level locals even when function symbols are present.

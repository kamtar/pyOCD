"""Implementation of the ``pyocd web`` subcommand."""

import argparse
from pathlib import Path
from typing import List

from .base import SubcommandBase


class WebSubcommand(SubcommandBase):
    NAMES = ["web"]
    HELP = "Run the browser-based pyOCD control centre."

    @classmethod
    def get_args(cls) -> List[argparse.ArgumentParser]:
        parser = argparse.ArgumentParser(description=cls.HELP, add_help=False)
        parser.add_argument(
            "--host",
            default="127.0.0.1",
            help="Address to bind (default: 127.0.0.1).")
        parser.add_argument(
            "--port",
            type=int,
            default=8080,
            help="HTTP port (default: 8080).")
        parser.add_argument(
            "--mdns",
            action="store_true",
            help="Advertise the web interface over mDNS.")
        parser.add_argument(
            "--ssdp",
            action="store_true",
            help="Advertise the web interface using SSDP/UPnP.")
        parser.add_argument("--auth-token",
                            help="Bearer token for API access (default for non-loopback binds: cutter; loopback is unsecured).")
        parser.add_argument("--auth-token-file",
                            help="Read the bearer token from this file.")
        parser.add_argument(
            "--insecure",
            action="store_true",
            help="Allow remote binding without authentication.")
        parser.add_argument(
            "--artifact-dir",
            help="Directory for uploaded firmware and ELF files.")
        parser.add_argument(
            "--unsafe-console",
            action="store_true",
            help="Enable Python and host-shell console commands.")
        parser.add_argument(
            "--gdb-executable",
            help="GDB executable used by the browser debugger (auto-detected by default).")
        parser.add_argument(
            "--force-rpi",
            action="store_true",
            help="Show the Raspberry Pi GPIO adapter even when it is unavailable (UI preview only).")
        return [cls.CommonOptions.LOGGING, parser]

    def invoke(self) -> int:
        from ..web import run_webserver
        token = self._args.auth_token
        if self._args.auth_token_file:
            token = Path(
                self._args.auth_token_file).read_text(
                encoding="utf-8").strip()
        run_webserver(self._args.host, self._args.port, token,
                      self._args.artifact_dir, self._args.unsafe_console,
                      self._args.insecure, self._args.gdb_executable,
                      self._args.force_rpi, self._args.mdns, self._args.ssdp)
        return 0

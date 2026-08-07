"""mDNS advertisement for the pyOCD web interface."""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import List

LOG = logging.getLogger(__name__)
HTTP_SERVICE_TYPE = "_http._tcp.local."


def _local_addresses(family: int) -> List[bytes]:
    """Return usable addresses for a wildcard web-server bind."""
    addresses = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), 0, family=family,
                                   type=socket.SOCK_STREAM)
    except socket.gaierror:
        infos = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_unspecified or address.is_loopback:
            continue
        if address.packed not in addresses:
            addresses.append(address.packed)
    return addresses


def resolve_addresses(host: str) -> List[bytes]:
    """Resolve the web-server bind address into mDNS address records."""
    host = host.strip()
    if host == "localhost":
        host = "127.0.0.1"

    try:
        address = ipaddress.ip_address(host)
        if address.is_unspecified:
            addresses = _local_addresses(socket.AF_INET6 if address.version == 6 else socket.AF_INET)
            fallback = "::1" if address.version == 6 else "127.0.0.1"
            return addresses or [ipaddress.ip_address(fallback).packed]
        return [address.packed]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"unable to resolve web host '{host}'") from exc

    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.packed not in addresses:
            addresses.append(address.packed)
    if not addresses:
        raise ValueError(f"unable to resolve web host '{host}'")
    return addresses


class MdnsAdvertiser:
    """Register and unregister one HTTP service using mDNS."""

    def __init__(self, name: str, host: str, port: int):
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError as exc:
            raise RuntimeError(
                "mDNS support requires the zeroconf package; reinstall pyOCD to restore its dependencies"
            ) from exc

        display_name = name.strip() or "pyOCD"
        server_name = socket.gethostname().rstrip(".") or "pyocd"
        addresses = resolve_addresses(host)
        self._service_info = ServiceInfo(
            HTTP_SERVICE_TYPE,
            f"{display_name}.{HTTP_SERVICE_TYPE}",
            addresses=addresses,
            port=port,
            properties={"path": "/"},
            server=f"{server_name}.local.",
        )
        self._zeroconf = Zeroconf()
        try:
            self._zeroconf.register_service(self._service_info)
        except Exception:
            self._zeroconf.close()
            raise
        LOG.info("mDNS service advertised as %s on http://%s:%d", display_name, host, port)

    def close(self) -> None:
        """Stop advertising the service."""
        try:
            self._zeroconf.unregister_service(self._service_info)
        except Exception:
            LOG.debug("unable to unregister mDNS service", exc_info=True)
        finally:
            self._zeroconf.close()

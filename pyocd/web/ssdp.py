"""SSDP/UPnP advertisement for the pyOCD web interface."""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Optional

from .mdns import resolve_addresses

LOG = logging.getLogger(__name__)

SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
DEVICE_TYPE = "urn:schemas-upnp-org:device:Basic:1"
ROOT_DEVICE = "upnp:rootdevice"
CACHE_MAX_AGE = 1800
ANNOUNCE_INTERVAL = CACHE_MAX_AGE / 2
DEVICE_NS = "urn:schemas-upnp-org:device-1-0"


def _http_address(host: str, address: bytes) -> str:
    """Return the address that should be used in an HTTP URL."""
    if host == "localhost":
        return "127.0.0.1"
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return str(ipaddress.ip_address(address))
    if parsed.version == 6:
        return f"[{parsed}]"
    if not parsed.is_unspecified:
        return str(parsed)
    return str(ipaddress.ip_address(address))


class SsdpAdvertiser:
    """Advertise the web endpoint as a small UPnP device."""

    description_path = "/device.xml"

    def __init__(self, name: str, host: str, port: int):
        self._name = name.strip() or "pyOCD"
        addresses = [
            address for address in resolve_addresses(host)
            if ipaddress.ip_address(address).version == 4
        ]
        if not addresses:
            raise RuntimeError("SSDP requires an IPv4 web-server address")

        self._address = _http_address(host, addresses[0])
        if self._address.startswith("["):
            raise RuntimeError("SSDP requires an IPv4 web-server address")
        self._base_url = f"http://{self._address}:{port}"
        self._location = f"{self._base_url}{self.description_path}"
        self._device_uuid = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"pyocd-web:{socket.gethostname()}:{self._address}:{port}:{self._name}"))
        self._targets = (ROOT_DEVICE, f"uuid:{self._device_uuid}", DEVICE_TYPE)
        self._stop = threading.Event()
        self._closed = False
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind(("0.0.0.0", SSDP_PORT))
            interface = socket.inet_aton(self._address)
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, interface)
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            self._socket.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(SSDP_ADDRESS) + interface)
            self._socket.settimeout(0.5)
            self._thread = threading.Thread(
                target=self._run, name="pyocd-web-ssdp", daemon=True)
            self._thread.start()
            LOG.info("SSDP advertising %s at %s", self._name, self._base_url)
        except Exception:
            self._socket.close()
            raise

    @property
    def location(self) -> str:
        """Return the URL of the UPnP device description."""
        return self._location

    def _usn(self, target: str) -> str:
        if target == ROOT_DEVICE:
            return f"uuid:{self._device_uuid}::{ROOT_DEVICE}"
        if target == f"uuid:{self._device_uuid}":
            return target
        return f"uuid:{self._device_uuid}::{target}"

    def _send(self, payload: bytes, destination) -> None:
        try:
            self._socket.sendto(payload, destination)
        except OSError:
            if not self._stop.is_set():
                LOG.debug("unable to send SSDP message", exc_info=True)

    def _notification(self, target: str, state: str) -> bytes:
        return (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDRESS}:{SSDP_PORT}\r\n"
            f"CACHE-CONTROL: max-age={CACHE_MAX_AGE}\r\n"
            f"LOCATION: {self._location}\r\n"
            f"NT: {target}\r\n"
            f"NTS: {state}\r\n"
            "SERVER: pyOCD/1.0 UPnP/1.0\r\n"
            f"USN: {self._usn(target)}\r\n"
            "\r\n"
        ).encode("ascii")

    def _send_notifications(self, state: str) -> None:
        for target in self._targets:
            self._send(
                self._notification(target, state),
                (SSDP_ADDRESS, SSDP_PORT))

    def _response(self, target: str) -> bytes:
        return (
            "HTTP/1.1 200 OK\r\n"
            f"CACHE-CONTROL: max-age={CACHE_MAX_AGE}\r\n"
            "EXT:\r\n"
            f"LOCATION: {self._location}\r\n"
            "SERVER: pyOCD/1.0 UPnP/1.0\r\n"
            f"ST: {target}\r\n"
            f"USN: {self._usn(target)}\r\n"
            "\r\n"
        ).encode("ascii")

    @staticmethod
    def _search_target(payload: bytes) -> Optional[str]:
        try:
            lines = payload.decode("ascii", errors="ignore").splitlines()
        except UnicodeDecodeError:
            return None
        if not lines or not lines[0].upper().startswith("M-SEARCH "):
            return None
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        return headers.get("st")

    def _handle_search(self, payload: bytes, sender) -> None:
        target = self._search_target(payload)
        if target is None:
            return
        if target.lower() == "ssdp:all":
            targets = self._targets
        else:
            targets = tuple(
                supported for supported in self._targets
                if supported.lower() == target.lower())
        for supported in targets:
            self._send(self._response(supported), sender)

    def _run(self) -> None:
        next_announcement = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_announcement:
                self._send_notifications("ssdp:alive")
                next_announcement = now + ANNOUNCE_INTERVAL
            try:
                payload, sender = self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_search(payload, sender)

    def device_description(self) -> bytes:
        """Build the UPnP device description served by the web application."""
        ET.register_namespace("", DEVICE_NS)
        root = ET.Element(f"{{{DEVICE_NS}}}root")
        spec_version = ET.SubElement(root, f"{{{DEVICE_NS}}}specVersion")
        ET.SubElement(spec_version, f"{{{DEVICE_NS}}}major").text = "1"
        ET.SubElement(spec_version, f"{{{DEVICE_NS}}}minor").text = "0"
        device = ET.SubElement(root, f"{{{DEVICE_NS}}}device")
        fields = {
            "deviceType": DEVICE_TYPE,
            "friendlyName": self._name,
            "manufacturer": "pyOCD",
            "modelDescription": "pyOCD web interface",
            "modelName": "pyOCD",
            "modelNumber": "1",
            "serialNumber": self._device_uuid,
            "UDN": f"uuid:{self._device_uuid}",
            "presentationURL": f"{self._base_url}/",
        }
        for name, value in fields.items():
            ET.SubElement(device, f"{{{DEVICE_NS}}}{name}").text = value
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def close(self) -> None:
        """Stop responding to SSDP and announce that the device is gone."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._send_notifications("ssdp:byebye")
        self._socket.close()
        self._thread.join(timeout=1.0)

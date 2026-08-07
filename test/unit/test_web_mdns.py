import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from pyocd.__main__ import PyOCDTool
from pyocd.web import mdns
from pyocd.web import ssdp


def test_resolve_addresses_preserves_loopback_bind():
    assert mdns.resolve_addresses("127.0.0.1") == [b"\x7f\x00\x00\x01"]
    assert mdns.resolve_addresses("localhost") == [b"\x7f\x00\x00\x01"]


def test_wildcard_bind_adds_route_selected_interface(monkeypatch):
    monkeypatch.setattr(
        mdns.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (mdns.socket.AF_INET, mdns.socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))])
    monkeypatch.setattr(mdns, "_route_address", lambda family: b"\xc0\xa8\x02\x75")

    assert mdns.resolve_addresses("0.0.0.0") == [b"\xc0\xa8\x02\x75"]


def test_advertiser_registers_http_service_and_unregisters_on_close(monkeypatch):
    calls = []

    class FakeServiceInfo:
        def __init__(self, service_type, name, **kwargs):
            self.service_type = service_type
            self.name = name
            self.kwargs = kwargs

    class FakeZeroconf:
        def register_service(self, info):
            calls.append(("register", info))

        def unregister_service(self, info):
            calls.append(("unregister", info))

        def close(self):
            calls.append(("close",))

    monkeypatch.setitem(
        sys.modules, "zeroconf", SimpleNamespace(ServiceInfo=FakeServiceInfo, Zeroconf=FakeZeroconf))
    monkeypatch.setattr(mdns, "resolve_addresses", lambda host: [b"\xc0\x00\x02\x0a"])

    advertiser = mdns.MdnsAdvertiser("Lab bench", "192.0.2.10", 8080)
    advertiser.close()

    assert calls[0][0] == "register"
    info = calls[0][1]
    assert info.service_type == "_http._tcp.local."
    assert info.name == "Lab bench._http._tcp.local."
    assert info.kwargs["addresses"] == [b"\xc0\x00\x02\x0a"]
    assert info.kwargs["port"] == 8080
    assert info.kwargs["properties"] == {"path": "/"}
    assert info.kwargs["server"].endswith(".local.")
    assert [call[0] for call in calls] == ["register", "unregister", "close"]


def test_web_cli_mdns_is_opt_in():
    parser = PyOCDTool()._parser
    assert parser.parse_args(["web"]).mdns is False
    assert parser.parse_args(["web", "--mdns"]).mdns is True
    assert parser.parse_args(["web"]).ssdp is False
    assert parser.parse_args(["web", "--ssdp"]).ssdp is True


def test_ssdp_device_description_uses_interface_name_and_presentation_url():
    advertiser = object.__new__(ssdp.SsdpAdvertiser)
    advertiser._name = "Lab bench"
    advertiser._device_uuid = "12345678-1234-5678-1234-567812345678"
    advertiser._base_url = "http://192.0.2.10:8080"

    document = ET.fromstring(advertiser.device_description())
    friendly_name = document.find(f".//{{{ssdp.DEVICE_NS}}}friendlyName")
    presentation_url = document.find(f".//{{{ssdp.DEVICE_NS}}}presentationURL")
    udn = document.find(f".//{{{ssdp.DEVICE_NS}}}UDN")
    assert friendly_name.text == "Lab bench"
    assert presentation_url.text == "http://192.0.2.10:8080/"
    assert udn.text == "uuid:12345678-1234-5678-1234-567812345678"


def test_ssdp_search_response_uses_web_location():
    advertiser = object.__new__(ssdp.SsdpAdvertiser)
    advertiser._device_uuid = "12345678-1234-5678-1234-567812345678"
    advertiser._location = "http://192.0.2.10:8080/device.xml"

    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        "MAN: \"ssdp:discover\"\r\n"
        "ST: upnp:rootdevice\r\n"
        "\r\n").encode("ascii")
    assert ssdp.SsdpAdvertiser._search_target(request) == "upnp:rootdevice"
    response = advertiser._response(ssdp.ROOT_DEVICE).decode("ascii")
    assert "LOCATION: http://192.0.2.10:8080/device.xml\r\n" in response
    assert "USN: uuid:12345678-1234-5678-1234-567812345678::upnp:rootdevice\r\n" in response

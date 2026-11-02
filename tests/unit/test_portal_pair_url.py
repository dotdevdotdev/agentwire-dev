"""`agentwire portal pair` must print a URL the pairing device can reach."""

from agentwire.portal_cli import derive_pair_url

LAN = ["192.168.1.20", "10.0.0.5"]


def _cfg(tmp_path, host="127.0.0.1", port=8765, ssl=False, portal_url=None):
    server = {"host": host, "port": port}
    if ssl:
        cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
        cert.write_text("x")
        key.write_text("x")
        server["ssl"] = {"cert": str(cert), "key": str(key)}
    cfg = {"server": server}
    if portal_url:
        cfg["portal"] = {"url": portal_url}
    return cfg


def test_pair_url_derivation(tmp_path):
    cases = {
        "loopback bind keeps localhost": (
            dict(host="127.0.0.1"), {}, "http://localhost:8765"),
        "wildcard bind uses primary LAN ip": (
            dict(host="0.0.0.0"), {}, "http://192.168.1.20:8765"),
        "wildcard bind prefers mDNS name": (
            dict(host="0.0.0.0"), dict(mdns_host="box.local"), "http://box.local:8765"),
        "specific bind address is used": (
            dict(host="10.0.0.5"), {}, "http://10.0.0.5:8765"),
        "explicit portal.url wins over LAN": (
            dict(host="0.0.0.0", portal_url="https://portal.example"), {},
            "https://portal.example"),
        "https follows ssl state": (
            dict(host="0.0.0.0", ssl=True), {}, "https://192.168.1.20:8765"),
        "port follows server.port": (
            dict(host="0.0.0.0", port=9000), {}, "http://192.168.1.20:9000"),
        "--url override wins over everything": (
            dict(host="0.0.0.0", portal_url="https://portal.example"),
            dict(override="http://x:1/"), "http://x:1"),
    }
    bad = {}
    for name, (cfg_kw, call_kw, want) in cases.items():
        kw = dict(lan_addrs=LAN, mdns_host=None) | call_kw
        got = derive_pair_url(_cfg(tmp_path, **cfg_kw), **kw)[0]
        if got != want:
            bad[name] = (got, want)
    assert not bad, bad


def test_alternatives_listed_for_multiple_addresses(tmp_path):
    _, source, alts = derive_pair_url(
        _cfg(tmp_path, host="0.0.0.0"), lan_addrs=LAN, mdns_host=None
    )
    assert source == "primary LAN address"
    assert alts == ["http://10.0.0.5:8765"]


def test_wildcard_bind_without_any_address_falls_back_to_localhost(tmp_path):
    url, source, _ = derive_pair_url(
        _cfg(tmp_path, host="0.0.0.0"), lan_addrs=[], mdns_host=None
    )
    assert url == "http://localhost:8765"
    assert "no LAN address" in source

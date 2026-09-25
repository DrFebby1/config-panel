"""Turn a server template plus a user into real client share links.

A "server" row is an inbound definition: protocol, address, port and a JSON bag
of transport/security parameters. Combined with a user's UUID/password it
renders the exact URI formats that v2rayN, v2rayNG, Nekobox, Streisand, Clash
and Hiddify understand.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, urlencode

PROTOCOLS = ("vless", "vmess", "trojan", "shadowsocks")

NETWORKS = ("tcp", "ws", "grpc", "http")
SECURITIES = ("none", "tls", "reality")

SS_METHODS = (
    "aes-256-gcm",
    "aes-128-gcm",
    "chacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
)


def server_params(server: dict[str, Any]) -> dict[str, Any]:
    raw = server.get("params") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean(mapping: dict[str, Any]) -> dict[str, str]:
    """Drop empty values and stringify the rest for use in a query string."""
    out: dict[str, str] = {}
    for key, value in mapping.items():
        if value is None or value == "" or value is False:
            continue
        out[key] = "1" if value is True else str(value)
    return out


def _query(params: dict[str, Any]) -> str:
    return urlencode(_clean(params))


def _b64(raw: str) -> str:
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _remark(user: dict[str, Any], server: dict[str, Any]) -> str:
    name = str(user.get("name") or "user").strip()
    server_name = str(server.get("name") or "").strip()
    return f"{name} | {server_name}" if server_name else name


def _security_params(params: dict[str, Any], server: dict[str, Any]) -> dict[str, Any]:
    security = (params.get("security") or "none").lower()
    out: dict[str, Any] = {}
    if security in ("tls", "reality"):
        out["security"] = security
        out["sni"] = params.get("sni") or params.get("host") or server.get("address")
        if params.get("fingerprint"):
            out["fp"] = params["fingerprint"]
        if params.get("alpn"):
            out["alpn"] = params["alpn"]
        if params.get("allow_insecure"):
            out["allowInsecure"] = "1"
    if security == "reality":
        if params.get("public_key"):
            out["pbk"] = params["public_key"]
        if params.get("short_id"):
            out["sid"] = params["short_id"]
        if params.get("spider_x"):
            out["spx"] = params["spider_x"]
    return out


def _transport_params(params: dict[str, Any]) -> dict[str, Any]:
    network = (params.get("network") or "tcp").lower()
    out: dict[str, Any] = {"type": network}
    if network == "ws":
        out["path"] = params.get("path") or "/"
        if params.get("host"):
            out["host"] = params["host"]
    elif network == "grpc":
        out["serviceName"] = params.get("service_name") or ""
        if params.get("grpc_mode"):
            out["mode"] = params["grpc_mode"]
    elif network == "http":
        out["path"] = params.get("path") or "/"
        if params.get("host"):
            out["host"] = params["host"]
        if params.get("http_method"):
            out["method"] = params["http_method"]
    elif network == "tcp" and params.get("http_header"):
        out["headerType"] = "http"
        out["host"] = params.get("host") or ""
        out["path"] = params.get("path") or "/"
    return out


def _vless(user: dict[str, Any], server: dict[str, Any]) -> str:
    params = server_params(server)
    query = {"encryption": "none"}
    query.update(_transport_params(params))
    query.update(_security_params(params, server))
    if params.get("flow"):
        query["flow"] = params["flow"]
    return (
        f"vless://{user['uuid']}@{server['address']}:{server['port']}"
        f"?{_query(query)}#{quote(_remark(user, server), safe='')}"
    )


def _vmess(user: dict[str, Any], server: dict[str, Any]) -> str:
    params = server_params(server)
    network = (params.get("network") or "tcp").lower()
    security = (params.get("security") or "none").lower()
    payload = {
        "v": "2",
        "ps": _remark(user, server),
        "add": server["address"],
        "port": str(server["port"]),
        "id": user["uuid"],
        "aid": str(params.get("alter_id") or 0),
        "scy": params.get("cipher") or "auto",
        "net": network,
        "type": "http" if (network == "tcp" and params.get("http_header")) else "none",
        "host": params.get("host") or server["address"],
        "path": params.get("path") or "/",
        "tls": "tls" if security in ("tls", "reality") else "",
        "sni": params.get("sni") or server["address"] if security in ("tls", "reality") else "",
        "alpn": params.get("alpn") or "",
        "fp": params.get("fingerprint") or "",
    }
    return "vmess://" + _b64(json.dumps(payload, ensure_ascii=False))


def _trojan(user: dict[str, Any], server: dict[str, Any]) -> str:
    params = server_params(server)
    query = _transport_params(params)
    # Trojan is TLS by definition; expose it explicitly for clarity.
    security = (params.get("security") or "tls").lower()
    query["security"] = "none" if security == "none" else security
    if security != "none":
        query["sni"] = params.get("sni") or params.get("host") or server["address"]
    if params.get("fingerprint"):
        query["fp"] = params["fingerprint"]
    if params.get("alpn"):
        query["alpn"] = params["alpn"]
    if params.get("allow_insecure"):
        query["allowInsecure"] = "1"
    if params.get("network") in (None, "", "tcp") and params.get("flow"):
        query["flow"] = params["flow"]
    password = user.get("password") or user["uuid"]
    return (
        f"trojan://{quote(str(password), safe='')}@{server['address']}:{server['port']}"
        f"?{_query(query)}#{quote(_remark(user, server), safe='')}"
    )


def _shadowsocks(user: dict[str, Any], server: dict[str, Any]) -> str:
    params = server_params(server)
    method = params.get("method") or "chacha20-ietf-poly1305"
    password = user.get("password") or user["uuid"]
    userinfo = _b64(f"{method}:{password}")
    link = f"ss://{userinfo}@{server['address']}:{server['port']}"
    if params.get("plugin"):
        plugin = params["plugin"]
        opts = params.get("plugin_opts") or ""
        link += "?" + urlencode({"plugin": f"{plugin};{opts}" if opts else plugin})
    return link + "#" + quote(_remark(user, server), safe="")


_BUILDERS = {
    "vless": _vless,
    "vmess": _vmess,
    "trojan": _trojan,
    "shadowsocks": _shadowsocks,
}


def build_link(user: dict[str, Any], server: dict[str, Any]) -> str:
    """Render one share link. Unknown protocols raise ValueError."""
    protocol = str(server.get("protocol") or "").lower()
    builder = _BUILDERS.get(protocol)
    if builder is None:
        raise ValueError(f"unknown protocol: {protocol!r}")
    return builder(user, server)


def build_links(
    user: dict[str, Any], servers: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """All links for a user, including any hand-written extra configs."""
    out: list[dict[str, str]] = []
    for server in servers:
        if not server.get("enabled", 1):
            continue
        try:
            link = build_link(user, server)
        except (ValueError, KeyError):
            continue
        out.append(
            {
                "link": link,
                "protocol": str(server.get("protocol") or ""),
                "server": str(server.get("name") or ""),
                "remark": _remark(user, server),
            }
        )
    for line in str(user.get("extra_configs") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        scheme = line.split("://", 1)[0].lower() if "://" in line else "custom"
        out.append({"link": line, "protocol": scheme, "server": "دستی", "remark": line[:40]})
    return out


def subscription_text(links: list[dict[str, str]]) -> str:
    """Base64 body that subscription clients expect."""
    body = "\n".join(item["link"] for item in links)
    return _b64(body) if body else ""

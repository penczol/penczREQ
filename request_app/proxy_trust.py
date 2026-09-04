from __future__ import annotations

import ipaddress
import os
import sqlite3
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path


AUTO_TRUST_RUNTIME_GATEWAY_ENV = "AUTO_TRUST_RUNTIME_GATEWAY"
EFFECTIVE_TRUSTED_PROXIES_ENV = "PENCZREQ_EFFECTIVE_TRUSTED_PROXIES"
MANUAL_PROXY_LIMIT = 32
EFFECTIVE_PROXY_LIMIT = MANUAL_PROXY_LIMIT + 1
_MAX_ROUTE_BYTES = 64 * 1024
_ROUTE_UP = 0x0001
_ROUTE_GATEWAY = 0x0002
_ROUTE_REJECT = 0x0200
_RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class ProxyTrustError(ValueError):
    pass


class RuntimeGatewayError(ProxyTrustError):
    pass


ProxyNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class RuntimeProxyResolution:
    component: str
    access_mode: str
    auto_trust_enabled: bool
    manual_trusted_proxies: str
    runtime_gateway: str
    effective_trusted_proxies: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _RouteEntry:
    interface: str
    destination: ipaddress.IPv4Address
    gateway: ipaddress.IPv4Address
    flags: int
    metric: int
    mask: ipaddress.IPv4Address


def parse_proxy_networks(
    value: str, *, max_networks: int = MANUAL_PROXY_LIMIT
) -> tuple[ProxyNetwork, ...]:
    result: list[ProxyNetwork] = []
    for raw in value.split(","):
        candidate = raw.strip()
        if not candidate:
            continue
        if candidate == "*":
            raise ProxyTrustError("Zaufane proxy nie może używać wildcard '*'.")
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError as exc:
            raise ProxyTrustError(
                f"Nieprawidłowy adres lub podsieć proxy: {candidate}"
            ) from exc
        if network.prefixlen == 0:
            raise ProxyTrustError(
                "Zaufane proxy nie może obejmować całego Internetu (/0)."
            )
        if network not in result:
            result.append(network)
    if len(result) > max_networks:
        raise ProxyTrustError(
            f"Można skonfigurować maksymalnie {max_networks} sieci zaufanego proxy."
        )
    return tuple(result)


def render_proxy_networks(networks: tuple[ProxyNetwork, ...], *, separator: str = ",") -> str:
    return separator.join(str(network) for network in networks)


def normalize_proxy_networks(
    value: str, *, max_networks: int = MANUAL_PROXY_LIMIT, separator: str = ","
) -> str:
    return render_proxy_networks(
        parse_proxy_networks(value, max_networks=max_networks), separator=separator
    )


def combine_proxy_networks(
    *values: str, max_networks: int = MANUAL_PROXY_LIMIT
) -> str:
    return normalize_proxy_networks(
        ",".join(value for value in values if value), max_networks=max_networks
    )


def _boolean(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ProxyTrustError(f"{name} musi być wartością logiczną true/false.")


def _decode_route_ipv4(value: str) -> ipaddress.IPv4Address:
    if len(value) != 8:
        raise ValueError("route IPv4 value must contain eight hexadecimal digits")
    raw = bytes.fromhex(value)
    return ipaddress.IPv4Address(raw[::-1])


def _parse_route_table(content: str) -> tuple[_RouteEntry, ...]:
    lines = content.splitlines()
    if not lines:
        raise RuntimeGatewayError("Brak tabeli routingu kontenera.")
    header = lines[0].split()
    if header[:4] != ["Iface", "Destination", "Gateway", "Flags"]:
        raise RuntimeGatewayError("Tabela routingu kontenera ma nieprawidłowy nagłówek.")
    entries: list[_RouteEntry] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 8:
            raise RuntimeGatewayError("Tabela routingu kontenera zawiera niepełny wpis.")
        try:
            entries.append(
                _RouteEntry(
                    interface=fields[0],
                    destination=_decode_route_ipv4(fields[1]),
                    gateway=_decode_route_ipv4(fields[2]),
                    flags=int(fields[3], 16),
                    metric=int(fields[6], 10),
                    mask=_decode_route_ipv4(fields[7]),
                )
            )
        except (ValueError, ipaddress.AddressValueError) as exc:
            raise RuntimeGatewayError(
                "Tabela routingu kontenera zawiera nieprawidłowy wpis."
            ) from exc
    return tuple(entries)


def _is_rfc1918(address: ipaddress.IPv4Address) -> bool:
    return any(address in network for network in _RFC1918_NETWORKS)


def _connected_network_for_gateway(
    gateway: ipaddress.IPv4Address,
    interface: str,
    entries: tuple[_RouteEntry, ...],
) -> ipaddress.IPv4Network | None:
    matches: list[ipaddress.IPv4Network] = []
    for entry in entries:
        if (
            entry.interface != interface
            or entry.gateway != ipaddress.IPv4Address("0.0.0.0")
            or not entry.flags & _ROUTE_UP
            or entry.flags & _ROUTE_REJECT
            or entry.mask == ipaddress.IPv4Address("0.0.0.0")
        ):
            continue
        try:
            network = ipaddress.IPv4Network(
                f"{entry.destination}/{entry.mask}", strict=True
            )
        except (ValueError, ipaddress.NetmaskValueError):
            continue
        if not 8 <= network.prefixlen <= 30:
            continue
        if gateway in network and gateway not in {
            network.network_address,
            network.broadcast_address,
        }:
            matches.append(network)
    unique = tuple(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def detect_runtime_gateway(route_path: Path = Path("/proc/net/route")) -> str:
    try:
        with route_path.open("r", encoding="ascii", errors="strict") as stream:
            content = stream.read(_MAX_ROUTE_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise RuntimeGatewayError("Nie można odczytać tabeli routingu kontenera.") from exc
    if len(content) > _MAX_ROUTE_BYTES:
        raise RuntimeGatewayError("Tabela routingu kontenera jest zbyt duża.")

    entries = _parse_route_table(content)
    default_routes = [
        entry
        for entry in entries
        if entry.destination == ipaddress.IPv4Address("0.0.0.0")
        and entry.mask == ipaddress.IPv4Address("0.0.0.0")
        and entry.interface != "lo"
        and entry.flags & (_ROUTE_UP | _ROUTE_GATEWAY) == (_ROUTE_UP | _ROUTE_GATEWAY)
        and not entry.flags & _ROUTE_REJECT
        and entry.gateway != ipaddress.IPv4Address("0.0.0.0")
    ]
    if len(default_routes) != 1:
        raise RuntimeGatewayError(
            "Nie znaleziono jednej jednoznacznej bramy domyślnej kontenera."
        )

    route = default_routes[0]
    gateway = route.gateway
    if (
        not _is_rfc1918(gateway)
        or gateway.is_unspecified
        or gateway.is_loopback
        or gateway.is_link_local
        or gateway.is_multicast
        or gateway.is_reserved
    ):
        raise RuntimeGatewayError(
            "Brama domyślna kontenera nie spełnia prywatnego kontraktu IPv4."
        )
    if _connected_network_for_gateway(gateway, route.interface, entries) is None:
        raise RuntimeGatewayError(
            "Brama domyślna kontenera nie jest jednoznacznym hostem on-link."
        )
    return f"{gateway}/32"


def _read_public_manual_proxies(database_path: Path) -> tuple[str, str | None]:
    if not database_path.is_file():
        return "", None
    try:
        uri = f"{database_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'known_proxies'"
            ).fetchone()
    except sqlite3.Error:
        return "", "Nie można odczytać ręcznych proxy z bazy; użyto wyłącznie env."
    return (str(row[0]) if row else ""), None


def resolve_runtime_proxy(
    component: str,
    *,
    environ: Mapping[str, str] | None = None,
    route_path: Path = Path("/proc/net/route"),
    database_path: Path | None = None,
) -> RuntimeProxyResolution:
    if component not in {"public", "control"}:
        raise ProxyTrustError("Komponent runtime musi mieć wartość public albo control.")
    source = os.environ if environ is None else environ
    access_name = "PUBLIC_ACCESS_MODE" if component == "public" else "CONTROL_ACCESS_MODE"
    trusted_name = (
        "PUBLIC_TRUSTED_PROXIES" if component == "public" else "CONTROL_TRUSTED_PROXIES"
    )
    access_mode = source.get(access_name, "lan").strip().lower().replace("_", "-")
    if access_mode not in {"lan", "reverse-proxy"}:
        raise ProxyTrustError(f"{access_name} musi mieć wartość lan albo reverse-proxy.")
    auto_trust_enabled = _boolean(
        AUTO_TRUST_RUNTIME_GATEWAY_ENV,
        source.get(AUTO_TRUST_RUNTIME_GATEWAY_ENV, "false"),
    )

    warnings: list[str] = []
    manual_values = [source.get(trusted_name, ""), source.get("FORWARDED_ALLOW_IPS", "")]
    if component == "public":
        if database_path is None:
            database_path = Path(source.get("DATA_DIR", "/data")) / "app.db"
        database_value, database_warning = _read_public_manual_proxies(database_path)
        manual_values.append(database_value)
        if database_warning:
            warnings.append(database_warning)
    manual = combine_proxy_networks(*manual_values, max_networks=MANUAL_PROXY_LIMIT)

    runtime_gateway = ""
    if auto_trust_enabled and access_mode == "reverse-proxy":
        try:
            runtime_gateway = detect_runtime_gateway(route_path)
        except RuntimeGatewayError as exc:
            warnings.append(str(exc))
    effective = combine_proxy_networks(
        manual, runtime_gateway, max_networks=EFFECTIVE_PROXY_LIMIT
    )
    return RuntimeProxyResolution(
        component=component,
        access_mode=access_mode,
        auto_trust_enabled=auto_trust_enabled,
        manual_trusted_proxies=manual,
        runtime_gateway=runtime_gateway,
        effective_trusted_proxies=effective,
        warnings=tuple(warnings),
    )


def prepare_runtime_proxy_environment(
    component: str,
    *,
    environ: MutableMapping[str, str] | None = None,
    route_path: Path = Path("/proc/net/route"),
    database_path: Path | None = None,
) -> RuntimeProxyResolution:
    target = os.environ if environ is None else environ
    resolution = resolve_runtime_proxy(
        component,
        environ=target,
        route_path=route_path,
        database_path=database_path,
    )
    target[EFFECTIVE_TRUSTED_PROXIES_ENV] = resolution.effective_trusted_proxies
    target["FORWARDED_ALLOW_IPS"] = resolution.effective_trusted_proxies
    return resolution

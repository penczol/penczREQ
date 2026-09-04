from __future__ import annotations

import hmac
import ipaddress
import secrets
from collections.abc import Callable
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .i18n import normalize_language, translate
from .proxy_trust import EFFECTIVE_PROXY_LIMIT, ProxyTrustError, parse_proxy_networks


PUBLIC_CSP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
    "form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://image.tmdb.org; "
    "connect-src 'self' https://api.themoviedb.org https://fcm.googleapis.com "
    "https://updates.push.services.mozilla.com https://*.push.apple.com https://*.notify.windows.com; "
    "manifest-src 'self'; worker-src 'self'; upgrade-insecure-requests"
)
CONTROL_CSP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
    "form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'"
)

LOGIN_CSRF_MAX_AGE_SECONDS = 600


def new_login_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def login_csrf_matches(cookie_token: str | None, form_token: str | None) -> bool:
    if not cookie_token or not form_token:
        return False
    if len(cookie_token) > 128 or len(form_token) > 128:
        return False
    return hmac.compare_digest(cookie_token, form_token)


def set_login_csrf_cookie(
    response: Response,
    cookie_name: str,
    token: str,
    *,
    secure: bool,
) -> None:
    response.set_cookie(
        cookie_name,
        token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=LOGIN_CSRF_MAX_AGE_SECONDS,
        path="/login",
    )


def clear_login_csrf_cookie(
    response: Response,
    cookie_name: str,
    *,
    secure: bool,
) -> None:
    response.delete_cookie(
        cookie_name,
        path="/login",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def _scope_language(scope: Scope, cookie_name: str) -> str:
    raw_cookie = Headers(scope=scope).get("cookie", "")
    cookies = SimpleCookie()
    try:
        cookies.load(raw_cookie)
    except Exception:
        return "en"
    value = cookies.get(cookie_name)
    return normalize_language(value.value if value else None)


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        language_cookie_name: str = "penczreq_language",
    ):
        self.app = app
        self.max_bytes = max_bytes
        self.language_cookie_name = language_cookie_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
            detail = translate(
                "Żądanie jest zbyt duże.",
                _scope_language(scope, self.language_cookie_name),
            )
            await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
            return
        has_body = (
            scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}
            or bool(headers.get("transfer-encoding"))
            or bool(content_length and content_length != "0")
        )
        if not has_body:
            await self.app(scope, receive, send)
            return

        buffered: list[Message] = []
        consumed = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                await self.app(scope, _replay(buffered), send)
                return
            if message["type"] != "http.request":
                continue
            consumed += len(message.get("body", b""))
            if consumed > self.max_bytes:
                detail = translate(
                    "Żądanie jest zbyt duże.",
                    _scope_language(scope, self.language_cookie_name),
                )
                await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        await self.app(scope, _replay(buffered), send)


def _replay(messages: list[Message]) -> Receive:
    queue = list(messages)

    async def receive() -> Message:
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    return receive


class AllowedHostsMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: tuple[str, ...],
        dynamic_base_url: Callable[[], str] | None = None,
        language_cookie_name: str = "penczreq_language",
    ):
        self.app = app
        self.allowed_hosts = {host.casefold() for host in allowed_hosts}
        self.dynamic_base_url = dynamic_base_url
        self.language_cookie_name = language_cookie_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host_value = Headers(scope=scope).get("host", "")
        try:
            hostname = urlsplit(f"//{host_value}").hostname or ""
        except ValueError:
            hostname = ""
        allowed = set(self.allowed_hosts)
        if self.dynamic_base_url:
            try:
                dynamic_host = urlsplit(self.dynamic_base_url()).hostname
                if dynamic_host:
                    allowed.add(dynamic_host.casefold())
            except ValueError:
                pass
        if "*" not in allowed and hostname.casefold() not in allowed:
            detail = translate(
                "Nieprawidłowy host.",
                _scope_language(scope, self.language_cookie_name),
            )
            await PlainTextResponse(detail, status_code=400)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class ControlNetworkMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        networks: str,
        *,
        trusted_proxies: str = "",
        allow_testclient: bool = False,
        language_cookie_name: str = "penczreq_control_language",
    ):
        self.app = app
        self.allow_testclient = allow_testclient
        self.networks = self._parse_allowed_networks(networks)
        self.trusted_proxies = self._parse_trusted_proxies(trusted_proxies)
        self.language_cookie_name = language_cookie_name

    @staticmethod
    def _parse_allowed_networks(value: str) -> tuple:
        parsed = []
        for raw in value.split(","):
            candidate = raw.strip()
            if not candidate:
                continue
            try:
                network = ipaddress.ip_network(candidate, strict=False)
            except ValueError as exc:
                raise RuntimeError(
                    f"Nieprawidłowa sieć CONTROL_ALLOWED_NETWORKS: {candidate}"
                ) from exc
            if network.prefixlen == 0:
                raise RuntimeError("Sieci Control nie mogą zawierać /0.")
            parsed.append(network)
        if not parsed:
            raise RuntimeError("CONTROL_ALLOWED_NETWORKS nie może być puste.")
        return tuple(parsed)

    @staticmethod
    def _parse_trusted_proxies(value: str) -> tuple:
        try:
            parsed = parse_proxy_networks(value, max_networks=EFFECTIVE_PROXY_LIMIT)
        except ProxyTrustError as exc:
            raise RuntimeError(str(exc)) from exc
        return parsed

    def _client_address(self, scope: Scope):
        peer = scope.get("client", ("", 0))[0]
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return None
        if not any(peer_address in network for network in self.trusted_proxies):
            return peer_address

        chain = []
        for raw in Headers(scope=scope).get("x-forwarded-for", "").split(","):
            candidate = raw.strip()
            try:
                chain.append(ipaddress.ip_address(candidate))
            except ValueError:
                continue
        chain.append(peer_address)
        while len(chain) > 1 and any(
            chain[-1] in network for network in self.trusted_proxies
        ):
            chain.pop()
        return chain[-1]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        peer = scope.get("client", ("", 0))[0]
        if self.allow_testclient and peer == "testclient":
            await self.app(scope, receive, send)
            return
        address = self._client_address(scope)
        if address is None:
            detail = translate(
                "Panel Control jest dostępny wyłącznie lokalnie.",
                _scope_language(scope, self.language_cookie_name),
            )
            await PlainTextResponse(detail, status_code=403)(scope, receive, send)
            return
        if not any(address in network for network in self.networks):
            detail = translate(
                "Panel Control jest dostępny wyłącznie lokalnie.",
                _scope_language(scope, self.language_cookie_name),
            )
            await PlainTextResponse(detail, status_code=403)(scope, receive, send)
            return
        effective_scope = dict(scope)
        effective_scope["client"] = (
            str(address),
            scope.get("client", ("", 0))[1],
        )
        await self.app(effective_scope, receive, send)


def apply_security_headers(response, *, csp: str, secure: bool, no_store: bool = False) -> None:
    response.headers["Content-Security-Policy"] = csp
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if no_store:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    if secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

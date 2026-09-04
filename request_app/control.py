from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .audit import SecurityAudit
from .auth_protection import LoginProtection, client_scope
from .config import Settings, load_settings
from .control_store import ControlError, ControlStore
from .database import Database
from .http_security import (
    AllowedHostsMiddleware,
    CONTROL_CSP,
    ControlNetworkMiddleware,
    RequestBodyLimitMiddleware,
    apply_security_headers,
    clear_login_csrf_cookie,
    login_csrf_matches,
    new_login_csrf_token,
    set_login_csrf_cookie,
)
from .i18n import client_translations, localize_message, normalize_language, translator
from .maintenance import backup_due, create_backup, integrity_report, list_backups
from .proxy_trust import ProxyTrustError, normalize_proxy_networks
from .repository import Repository, RepositoryError
from .secure_config import SecureConfigError, SecureConfigStore, localize_settings_history
from .tmdb import TMDBClient, TMDBError


APP_VERSION = __version__
LANGUAGE_COOKIE = "penczreq_control_language"
LOGIN_CSRF_COOKIE = "penczreq_control_login_csrf"
logger = logging.getLogger(__name__)
settings: Settings = load_settings()
db = Database(settings.database_path)
repo = Repository(db)
secure_config = SecureConfigStore(db, settings.config_encryption_key)
audit = SecurityAudit(db, settings.logs_dir)
login_protection = LoginProtection(db, audit)
control_store = ControlStore(settings.control_database_path, settings.control_session_secret)
tmdb = TMDBClient(secure_config.tmdb_token, settings.posters_dir, settings.poster_max_bytes)
auth_semaphore = asyncio.Semaphore(1)
static_dir = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def backup_maintenance_loop() -> None:
    while True:
        try:
            control_store.prune()
            if backup_due(db):
                retention = int(secure_config.get_setting(
                    "backup_retention_days", str(settings.backup_retention_days)
                ))
                await asyncio.to_thread(
                    create_backup,
                    db,
                    settings.control_database_path,
                    settings.backups_dir,
                    retention_days=retention,
                )
                audit.emit("automatic_backup_created", actor_type="control")
        except Exception:
            logger.exception("Nie udało się wykonać automatycznej kopii baz.")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    if db.quick_check() != "ok":
        raise RuntimeError("Baza aplikacji nie przeszła kontroli integralności.")
    secure_config.initialize(
        tmdb_token=settings.tmdb_token,
        public_base_url=settings.app_base_url,
        known_proxies=settings.public_trusted_proxies,
    )
    repo.enforce_roles(settings.public_admin_username, settings.public_admin_bootstrap_password)
    control_store.initialize()
    created = control_store.bootstrap(
        settings.control_admin_username,
        settings.control_bootstrap_password,
        development=settings.app_env == "development",
    )
    if settings.control_recovery_nonce:
        if not settings.control_recovery_password:
            raise RuntimeError("CONTROL_RECOVERY_NONCE wymaga CONTROL_RECOVERY_PASSWORD.")
        if control_store.apply_recovery(settings.control_recovery_nonce, settings.control_recovery_password):
            audit.emit("control_recovery_applied", severity="critical", actor_type="truenas-recovery")
    control_store.prune()
    audit.emit(
        "control_started", details={"version": APP_VERSION, "bootstrap_created": created}
    )
    task = asyncio.create_task(backup_maintenance_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="penczREQ Control", version=APP_VERSION, lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def raw_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def control_security_context(request: Request, call_next):
    ip = raw_client_ip(request)
    request.state.control_user = control_store.session_user(
        request.cookies.get(settings.control_cookie_name), ip, settings.control_idle_minutes
    )
    request.state.language = normalize_language(
        request.state.control_user["language"]
        if request.state.control_user
        else request.cookies.get(LANGUAGE_COOKIE)
    )
    response = await call_next(request)
    apply_security_headers(
        response, csp=CONTROL_CSP, secure=settings.cookie_secure, no_store=True
    )
    return response


def page_context(request: Request, **extra: Any) -> dict[str, Any]:
    user = request.state.control_user
    context = {
        "request": request,
        "control_user": {"username": user["username"]} if user else None,
        "csrf_token": user["csrf_token"] if user else "",
        "app_version": APP_VERSION,
        "app_env": settings.app_env,
        "language": request.state.language,
        "t": translator(request.state.language),
        "client_translations": client_translations(request.state.language),
    }
    context.update(extra)
    return context


def render_page(request: Request, name: str, *, status_code: int = 200, **extra: Any):
    return templates.TemplateResponse(
        request=request, name=name, context=page_context(request, **extra), status_code=status_code
    )


def render_login_page(request: Request, *, status_code: int = 200, error: str | None = None):
    token = new_login_csrf_token()
    response = render_page(
        request,
        "control_login.html",
        status_code=status_code,
        error=error,
        login_csrf_token=token,
    )
    set_login_csrf_cookie(
        response,
        LOGIN_CSRF_COOKIE,
        token,
        secure=settings.cookie_secure,
    )
    return response


def require_control_user(request: Request):
    user = request.state.control_user
    if not user:
        raise HTTPException(status_code=401, detail="Sesja panelu wygasła.")
    if user["must_change_password"] and request.url.path not in {
        "/force-password", "/logout", "/api/control/password",
    }:
        raise HTTPException(status_code=428, detail="Najpierw zmień hasło startowe panelu.")
    return user


def verify_csrf(request: Request, user: Any, form_token: str | None = None) -> None:
    received = request.headers.get("X-CSRF-Token") or form_token or ""
    if not hmac.compare_digest(str(received), str(user["csrf_token"])):
        raise HTTPException(status_code=403, detail="Nieprawidłowy token formularza.")


async def require_reauthentication(user: Any, password: str) -> None:
    valid = await asyncio.to_thread(control_store.verify_current_password, user["id"], password)
    if not valid:
        raise ControlError("Hasło panelu Control jest nieprawidłowe.")


def normalized_proxy_networks(value: str) -> str:
    try:
        return normalize_proxy_networks(value, separator=", ")
    except ProxyTrustError as exc:
        raise ControlError(str(exc)) from exc


def normalized_public_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ControlError("Adres publiczny musi być pełnym adresem HTTP lub HTTPS.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ControlError("Adres publiczny nie może zawierać danych logowania, zapytania ani fragmentu.")
    if settings.app_env == "production":
        expected_scheme = (
            "http" if settings.public_access_mode == "lan" else "https"
        )
        if parsed.scheme != expected_scheme:
            raise ControlError(
                "Publiczny adres nie odpowiada skonfigurowanemu trybowi dostępu."
            )
    return candidate


@app.exception_handler(ControlError)
@app.exception_handler(RepositoryError)
async def control_error(request: Request, exc: RuntimeError):
    return JSONResponse(
        {"detail": localize_message(str(exc), request.state.language)}, status_code=400
    )


@app.exception_handler(SecureConfigError)
@app.exception_handler(TMDBError)
async def integration_error(request: Request, exc: RuntimeError):
    return JSONResponse(
        {"detail": localize_message(str(exc), request.state.language)}, status_code=502
    )


@app.exception_handler(StarletteHTTPException)
async def localized_http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if exc.status_code == 404 and detail == "Not Found":
        detail = "Nie znaleziono strony."
    elif exc.status_code == 405 and detail == "Method Not Allowed":
        detail = "Metoda nie jest dozwolona."
    if isinstance(detail, str):
        detail = localize_message(detail, request.state.language)
    return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def localized_validation_error(request: Request, _: RequestValidationError):
    message = localize_message("Nieprawidłowe dane żądania.", request.state.language)
    return JSONResponse({"detail": message}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    logger.error(
        "Unhandled Control request error.",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    detail = localize_message("Wewnętrzny błąd serwera.", request.state.language)
    return JSONResponse({"detail": detail}, status_code=500)


@app.get("/internal/health", include_in_schema=False)
async def health(request: Request):
    try:
        if not ipaddress.ip_address(raw_client_ip(request)).is_loopback:
            raise HTTPException(status_code=404)
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.state.control_user:
        return RedirectResponse("/", status_code=303)
    return render_login_page(request)


def set_language_cookie(response, language: str) -> None:
    response.set_cookie(
        LANGUAGE_COOKIE,
        normalize_language(language),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=365 * 86400,
        path="/",
    )


@app.post("/language")
async def set_language(
    request: Request,
    language: Annotated[Literal["en", "pl"], Form()],
    next_path: Annotated[str, Form(alias="next")] = "/",
    csrf_token: Annotated[str, Form()] = "",
):
    user = request.state.control_user
    if user:
        verify_csrf(request, user, csrf_token)
        await asyncio.to_thread(control_store.set_language, user["id"], language)
    destination = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    response = RedirectResponse(destination, status_code=303)
    set_language_cookie(response, language)
    return response


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    login_csrf_token: Annotated[str, Form()] = "",
):
    if not login_csrf_matches(
        request.cookies.get(LOGIN_CSRF_COOKIE),
        login_csrf_token,
    ):
        raise HTTPException(status_code=403, detail="Nieprawidłowy token formularza.")
    ip = raw_client_ip(request)
    ip_key = client_scope(ip)
    generic = localize_message(
        "Nie udało się zalogować. Sprawdź dane lub spróbuj ponownie później.",
        request.state.language,
    )
    retry = control_store.login_gate(ip_key)
    if retry:
        response = render_login_page(request, status_code=429, error=generic)
        response.headers["Retry-After"] = str(retry)
        return response
    if len(username) > 64 or len(password) > 128:
        control_store.record_failure(ip_key)
        return render_login_page(request, status_code=401, error=generic)
    async with auth_semaphore:
        user = await asyncio.to_thread(control_store.authenticate, username, password, ip)
    if not user:
        retry = await asyncio.to_thread(control_store.record_failure, ip_key)
        audit.emit(
            "control_login_failure", severity="critical", username=username.strip().lower()[:64],
            ip_address=ip, details={"blocked": bool(retry), "retry_after": retry},
        )
        response = render_login_page(
            request, status_code=429 if retry else 401, error=generic
        )
        if retry:
            response.headers["Retry-After"] = str(retry)
        return response
    control_store.clear_failures(ip_key)
    raw_token, _, _ = control_store.create_session(user["id"], ip, settings.control_session_hours)
    audit.emit(
        "control_login_success", actor_type="control_admin", actor_id=user["id"],
        username=user["username"], ip_address=ip,
    )
    destination = "/force-password" if user["must_change_password"] else "/"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        settings.control_cookie_name, raw_token, httponly=True, secure=settings.cookie_secure,
        samesite="strict", max_age=settings.control_session_hours * 3600, path="/",
    )
    clear_login_csrf_cookie(
        response,
        LOGIN_CSRF_COOKIE,
        secure=settings.cookie_secure,
    )
    set_language_cookie(response, user["language"])
    return response


@app.post("/logout")
async def logout(request: Request, csrf_token: Annotated[str, Form()] = ""):
    user = request.state.control_user
    if user:
        verify_csrf(request, user, csrf_token)
        audit.emit(
            "control_logout", actor_type="control_admin", actor_id=user["id"],
            username=user["username"], ip_address=raw_client_ip(request),
        )
    control_store.delete_session(request.cookies.get(settings.control_cookie_name))
    response = RedirectResponse("/login", status_code=303)
    if user:
        set_language_cookie(response, user["language"])
    response.delete_cookie(settings.control_cookie_name, path="/")
    return response


@app.get("/force-password", response_class=HTMLResponse)
async def force_password_page(request: Request):
    user = request.state.control_user
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user["must_change_password"]:
        return RedirectResponse("/", status_code=303)
    return render_page(request, "control_force_password.html", error=None)


@app.post("/force-password", response_class=HTMLResponse)
async def force_password(
    request: Request,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
):
    user = request.state.control_user
    if not user:
        return RedirectResponse("/login", status_code=303)
    verify_csrf(request, user, csrf_token)
    if new_password != confirm_password:
        return render_page(
            request, "control_force_password.html", status_code=400,
            error=localize_message("Wprowadzone hasła nie są identyczne.", request.state.language),
        )
    try:
        await asyncio.to_thread(
            control_store.change_password, user["id"], current_password, new_password
        )
    except ControlError as exc:
        return render_page(
            request,
            "control_force_password.html",
            status_code=400,
            error=localize_message(str(exc), request.state.language),
        )
    audit.emit(
        "control_password_changed", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
    )
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.control_cookie_name, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = request.state.control_user
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["must_change_password"]:
        return RedirectResponse("/force-password", status_code=303)
    return render_page(request, "control_index.html")


class ReauthBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)


class SettingsBody(ReauthBody):
    tmdb_token: str = Field(default="", max_length=4096)
    public_base_url: str = Field(min_length=8, max_length=500)
    known_proxies: str = Field(default="", max_length=1000)
    security_log_retention_days: int = Field(ge=7, le=365)
    backup_retention_days: int = Field(ge=3, le=365)


class PublicUserCreateBody(ReauthBody):
    username: str = Field(min_length=3, max_length=32)
    temporary_password: str = Field(min_length=15, max_length=128)


class PublicUserActiveBody(ReauthBody):
    active: bool


class PublicUserPasswordBody(ReauthBody):
    temporary_password: str = Field(min_length=15, max_length=128)


class PublicUserRenameBody(ReauthBody):
    username: str = Field(min_length=3, max_length=32)


class ThrottleResetBody(ReauthBody):
    source: str = Field(pattern="^(public|control)$")
    scope: str = Field(pattern="^(ip|account)$")
    key: str = Field(min_length=1, max_length=128)


class ControlPasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=15, max_length=128)
    confirm_password: str = Field(min_length=15, max_length=128)


class ControlUsernameBody(ReauthBody):
    username: str = Field(min_length=3, max_length=32)


class BroadcastBody(ReauthBody):
    title_en: str = Field(min_length=1, max_length=100)
    body_en: str = Field(min_length=1, max_length=1000)
    title_pl: str = Field(min_length=1, max_length=100)
    body_pl: str = Field(min_length=1, max_length=1000)


class TmdbTestBody(ReauthBody):
    tmdb_token: str = Field(default="", max_length=4096)


@app.get("/api/control/overview")
async def api_overview(user=Depends(require_control_user)):
    users = [dict(item) for item in repo.list_users()]
    public_admin = next((item for item in users if item["role"] == "admin"), None)
    return {
        "version": APP_VERSION,
        "environment": settings.app_env,
        "public_admin": public_admin,
        "tmdb_configured": secure_config.has_secret("tmdb_token"),
        "public_base_url": secure_config.get_setting("public_base_url", settings.app_base_url),
        "integrity": await asyncio.to_thread(integrity_report, db, settings.control_database_path),
        "backups": await asyncio.to_thread(list_backups, settings.backups_dir),
        "bootstrap_file_exists": control_store.bootstrap_file.exists(),
    }


@app.get("/api/control/users")
async def api_users(user=Depends(require_control_user)):
    return {"items": [dict(item) for item in repo.list_users()]}


@app.post("/api/control/users")
async def api_create_user(body: PublicUserCreateBody, request: Request, user=Depends(require_control_user)):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    created = await asyncio.to_thread(repo.create_user, body.username, body.temporary_password)
    audit.emit(
        "public_user_created", severity="warning", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={"created_user_id": created["id"], "created_username": created["username"]},
    )
    return {
        "item": {
            "id": created["id"], "username": created["username"], "role": created["role"],
            "is_active": created["is_active"], "must_change_password": created["must_change_password"],
            "created_at": created["created_at"], "last_login_at": created["last_login_at"],
            "last_login_ip": created["last_login_ip"],
        }
    }


@app.put("/api/control/users/{user_id}/active")
async def api_user_active(
    user_id: int, body: PublicUserActiveBody, request: Request, user=Depends(require_control_user)
):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    await asyncio.to_thread(repo.set_user_active, user_id, body.active)
    audit.emit(
        "public_user_active_changed", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={"target_user_id": user_id, "active": body.active},
    )
    return {"ok": True}


@app.put("/api/control/users/{user_id}/password")
async def api_user_password(
    user_id: int, body: PublicUserPasswordBody, request: Request, user=Depends(require_control_user)
):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    await asyncio.to_thread(repo.set_password, user_id, body.temporary_password, force_change=True)
    revoked = await asyncio.to_thread(repo.revoke_user_sessions, user_id)
    audit.emit(
        "public_user_password_reset", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={"target_user_id": user_id, "sessions_revoked": revoked},
    )
    return {"ok": True}


@app.put("/api/control/users/{user_id}/username")
async def api_user_username(
    user_id: int, body: PublicUserRenameBody, request: Request, user=Depends(require_control_user)
):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    await asyncio.to_thread(repo.rename_user, user_id, body.username)
    audit.emit(
        "public_user_renamed", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={"target_user_id": user_id, "new_username": body.username.strip().lower()},
    )
    return {"ok": True}


@app.put("/api/control/users/{user_id}/admin")
async def api_transfer_admin(
    user_id: int, body: ReauthBody, request: Request, user=Depends(require_control_user)
):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    await asyncio.to_thread(repo.transfer_admin_role, user_id)
    audit.emit(
        "public_admin_transferred", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={"new_admin_user_id": user_id, "all_public_sessions_revoked": True},
    )
    return {"ok": True}


@app.post("/api/control/users/{user_id}/revoke-sessions")
async def api_revoke_sessions(
    user_id: int, body: ReauthBody, request: Request, user=Depends(require_control_user)
):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    count = await asyncio.to_thread(repo.revoke_user_sessions, user_id)
    audit.emit(
        "public_sessions_revoked", severity="warning", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={"target_user_id": user_id, "sessions": count},
    )
    return {"ok": True, "revoked": count}


@app.delete("/api/control/users/{user_id}")
async def api_delete_user(
    user_id: int, body: ReauthBody, request: Request, user=Depends(require_control_user)
):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    result = await asyncio.to_thread(repo.delete_user, user_id)
    audit.emit(
        "public_user_deleted", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details=result,
    )
    return {"ok": True, "result": result}


@app.get("/api/control/throttles")
async def api_throttles(user=Depends(require_control_user)):
    public_items = await asyncio.to_thread(login_protection.list_states)
    for item in public_items:
        item["source"] = "public"
    control_items = await asyncio.to_thread(control_store.list_throttles)
    items = public_items + control_items
    items.sort(key=lambda item: (bool(item.get("blocked_until")), item.get("updated_at") or ""), reverse=True)
    return {"items": items}


@app.post("/api/control/throttles/reset")
async def api_reset_throttle(
    body: ThrottleResetBody, request: Request, user=Depends(require_control_user)
):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    if body.source == "control":
        if body.scope != "ip":
            raise ControlError("Limiter Control obsługuje wyłącznie adresy IP.")
        removed = await asyncio.to_thread(control_store.reset_throttle, body.key)
    else:
        removed = await asyncio.to_thread(login_protection.reset, body.scope, body.key)
    if not removed:
        raise ControlError("Wpis blokady już nie istnieje.")
    audit.emit(
        "login_throttle_reset", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={"source": body.source, "scope": body.scope, "key": body.key},
    )
    return {"ok": True}


@app.get("/api/control/settings")
async def api_settings(user=Depends(require_control_user)):
    return {
        "tmdb_configured": secure_config.has_secret("tmdb_token"),
        "public_base_url": secure_config.get_setting("public_base_url", settings.app_base_url),
        "known_proxies": secure_config.get_setting("known_proxies", ""),
        "security_log_retention_days": int(secure_config.get_setting(
            "security_log_retention_days", str(settings.security_log_retention_days)
        )),
        "backup_retention_days": int(secure_config.get_setting(
            "backup_retention_days", str(settings.backup_retention_days)
        )),
    }


@app.put("/api/control/settings")
async def api_settings_update(body: SettingsBody, request: Request, user=Depends(require_control_user)):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    actor = f"control:{user['username']}"
    public_url = normalized_public_url(body.public_base_url)
    proxies = normalized_proxy_networks(body.known_proxies)
    if body.tmdb_token.strip():
        secure_config.set_secret("tmdb_token", body.tmdb_token, changed_by=actor)
    secure_config.set_setting("public_base_url", public_url, changed_by=actor)
    secure_config.set_setting("known_proxies", proxies, changed_by=actor)
    secure_config.set_setting(
        "security_log_retention_days", str(body.security_log_retention_days), changed_by=actor
    )
    secure_config.set_setting(
        "backup_retention_days", str(body.backup_retention_days), changed_by=actor
    )
    audit.emit(
        "security_settings_changed", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={
            "public_base_url": public_url, "known_proxies": proxies,
            "tmdb_token_changed": bool(body.tmdb_token.strip()),
            "security_log_retention_days": body.security_log_retention_days,
            "backup_retention_days": body.backup_retention_days,
        },
    )
    return {"ok": True}


@app.post("/api/control/test-tmdb")
async def api_test_tmdb(body: TmdbTestBody, request: Request, user=Depends(require_control_user)):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    token = body.tmdb_token.strip() or secure_config.tmdb_token()
    test_client = TMDBClient(token, settings.posters_dir, settings.poster_max_bytes)
    result = await test_client.search("Fight Club")
    audit.emit(
        "tmdb_connection_tested", actor_type="control_admin", actor_id=user["id"],
        username=user["username"], ip_address=raw_client_ip(request),
        details={"success": True, "results": len(result)},
    )
    return {"ok": True, "results": len(result)}


@app.post("/api/control/backup")
async def api_backup(body: ReauthBody, request: Request, user=Depends(require_control_user)):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    retention = int(secure_config.get_setting("backup_retention_days", str(settings.backup_retention_days)))
    created = await asyncio.to_thread(
        create_backup, db, settings.control_database_path, settings.backups_dir,
        retention_days=retention,
    )
    audit.emit(
        "manual_backup_created", severity="warning", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={"files": created},
    )
    return {"ok": True, "files": created}


@app.get("/api/control/backups")
async def api_backups(user=Depends(require_control_user)):
    return {"items": await asyncio.to_thread(list_backups, settings.backups_dir)}


@app.get("/api/control/events")
async def api_events(user=Depends(require_control_user)):
    return {"items": await asyncio.to_thread(audit.recent, 300)}


@app.get("/api/control/settings-history")
async def api_settings_history(user=Depends(require_control_user)):
    items = await asyncio.to_thread(secure_config.settings_history, 200)
    return {"items": localize_settings_history(items, user["language"])}


@app.post("/api/control/broadcast")
async def api_broadcast(body: BroadcastBody, request: Request, user=Depends(require_control_user)):
    verify_csrf(request, user)
    await require_reauthentication(user, body.current_password)
    count = await asyncio.to_thread(
        repo.broadcast_localized,
        0,
        title_en=body.title_en,
        body_en=body.body_en,
        title_pl=body.title_pl,
        body_pl=body.body_pl,
    )
    audit.emit(
        "notification_broadcast", severity="warning", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
        details={"recipients": count, "title_en": body.title_en, "title_pl": body.title_pl},
    )
    return {"ok": True, "recipients": count}


@app.put("/api/control/account/password")
async def api_control_password(
    body: ControlPasswordBody, request: Request, user=Depends(require_control_user)
):
    verify_csrf(request, user)
    if body.new_password != body.confirm_password:
        raise ControlError("Wprowadzone hasła nie są identyczne.")
    await asyncio.to_thread(
        control_store.change_password, user["id"], body.current_password, body.new_password
    )
    audit.emit(
        "control_password_changed", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=user["username"], ip_address=raw_client_ip(request),
    )
    response = JSONResponse({"ok": True, "reauthenticate": True})
    response.delete_cookie(settings.control_cookie_name, path="/")
    return response


@app.put("/api/control/account/username")
async def api_control_username(
    body: ControlUsernameBody, request: Request, user=Depends(require_control_user)
):
    verify_csrf(request, user)
    old_username = user["username"]
    await asyncio.to_thread(
        control_store.rename_user, user["id"], body.current_password, body.username
    )
    audit.emit(
        "control_username_changed", severity="critical", actor_type="control_admin",
        actor_id=user["id"], username=old_username, ip_address=raw_client_ip(request),
        details={"new_username": body.username.strip().lower()},
    )
    return {"ok": True}


# Added last so the LAN boundary rejects traffic before session or database access.
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_bytes=settings.max_request_body_bytes,
    language_cookie_name=LANGUAGE_COOKIE,
)
app.add_middleware(
    AllowedHostsMiddleware,
    allowed_hosts=settings.control_allowed_hosts,
    language_cookie_name=LANGUAGE_COOKIE,
)
app.add_middleware(
    ControlNetworkMiddleware,
    networks=settings.control_allowed_networks,
    trusted_proxies=settings.control_effective_trusted_proxies,
    allow_testclient=settings.app_env != "production",
    language_cookie_name=LANGUAGE_COOKIE,
)

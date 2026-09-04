from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings, load_settings
from .audit import SecurityAudit
from .auth_protection import LoginProtection
from .changelog import changelog_for, update_notification_bodies
from .database import Database
from .http_security import (
    AllowedHostsMiddleware,
    PUBLIC_CSP,
    RequestBodyLimitMiddleware,
    apply_security_headers,
    clear_login_csrf_cookie,
    login_csrf_matches,
    new_login_csrf_token,
    set_login_csrf_cookie,
)
from .i18n import client_translations, localize_message, normalize_language, translator
from .push import PushService
from .pwa import manifest_document
from .proxy_trust import (
    EFFECTIVE_PROXY_LIMIT,
    ProxyTrustError,
    parse_proxy_networks as parse_trusted_proxy_networks,
)
from .rate_limit import SlidingWindowLimiter
from .repository import Repository, RepositoryError
from .secure_config import SecureConfigError, SecureConfigStore
from .security import create_session, delete_other_sessions, delete_session, get_session_user, prune_expired_sessions
from .tmdb import TMDBClient, TMDBError
from .updates import notify_upcoming_request, notify_users_about_update


APP_VERSION = __version__
ADMIN_VIEW_COOKIE = "request_admin_view"
LANGUAGE_COOKIE = "penczreq_language"
LOGIN_CSRF_COOKIE = "penczreq_login_csrf"
logger = logging.getLogger(__name__)
settings: Settings = load_settings()
COOKIE_NAME = settings.public_cookie_name
db = Database(settings.database_path)
repo = Repository(db)
secure_config = SecureConfigStore(db, settings.config_encryption_key)
audit = SecurityAudit(db, settings.logs_dir)
login_protection = LoginProtection(db, audit)
rate_limiter = SlidingWindowLimiter()
tmdb = TMDBClient(secure_config.tmdb_token, settings.posters_dir, settings.poster_max_bytes)
push_service = PushService(db, settings.vapid_private_key_path, settings.vapid_subject)
auth_semaphore = asyncio.Semaphore(2)
static_dir = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["changelog_for"] = changelog_for


async def refresh_release_metadata() -> None:
    if not secure_config.has_secret("tmdb_token"):
        return
    for target in repo.release_refresh_targets():
        try:
            metadata = await tmdb.release_metadata(
                target["media_type"], target["tmdb_id"], target["season_number"]
            )
            repo.update_release_metadata(target["id"], metadata)
        except TMDBError:
            continue
        await asyncio.sleep(0.15)
    repo.promote_due_requests()


async def release_loop() -> None:
    while True:
        await refresh_release_metadata()
        repo.promote_due_requests()
        prune_expired_sessions(db, settings.session_idle_minutes)
        await asyncio.sleep(settings.tmdb_refresh_hours * 3600)


async def maintenance_loop() -> None:
    while True:
        try:
            login_protection.prune()
            retention = int(secure_config.get_setting(
                "security_log_retention_days", str(settings.security_log_retention_days)
            ))
            audit.prune(retention)
        except Exception:
            logger.exception("Nie udało się wykonać konserwacji bezpieczeństwa.")
        await asyncio.sleep(6 * 3600)


async def push_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(push_service.deliver_pending)
        except Exception:
            logger.exception("Nie udało się przetworzyć kolejki Web Push.")
        await asyncio.sleep(3)


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
    if settings.app_component == "public":
        repo.require_admin_exists()
    else:
        repo.enforce_roles(settings.public_admin_username, settings.public_admin_bootstrap_password)
    login_protection.migrate_legacy_lockouts()
    upgraded_accounts = repo.require_password_policy_upgrade()
    if upgraded_accounts:
        audit.emit(
            "password_policy_upgrade_required", severity="warning",
            details={"accounts": upgraded_accounts, "minimum_length": 15},
        )
    push_service.initialize()
    public_update, admin_update = update_notification_bodies(APP_VERSION, "pl")
    public_update_en, admin_update_en = update_notification_bodies(APP_VERSION, "en")
    notify_users_about_update(
        db,
        APP_VERSION,
        public_update,
        admin_update,
        public_update_en,
        admin_update_en,
    )
    repo.promote_due_requests()
    tasks = [
        asyncio.create_task(release_loop()),
        asyncio.create_task(push_loop()),
        asyncio.create_task(maintenance_loop()),
    ]
    audit.emit("application_started", details={"version": APP_VERSION, "environment": settings.app_env})
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="Requesty", version=APP_VERSION, lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.middleware("http")
async def add_user_to_request(request: Request, call_next):
    raw_token = request.cookies.get(COOKIE_NAME)
    request.state.user = get_session_user(
        db, raw_token, secret=settings.session_secret, idle_minutes=settings.session_idle_minutes
    )
    request.state.language = normalize_language(
        request.state.user["language"] if request.state.user else request.cookies.get(LANGUAGE_COOKIE)
    )
    response = await call_next(request)
    csp = PUBLIC_CSP if settings.cookie_secure else PUBLIC_CSP.replace("; upgrade-insecure-requests", "")
    apply_security_headers(
        response,
        csp=csp,
        secure=settings.cookie_secure,
        no_store=request.url.path in {"/login", "/force-password"},
    )
    if request.url.path == "/service-worker.js":
        response.headers["Cache-Control"] = "no-cache"
    elif request.url.path.startswith("/static/icons/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif request.url.path not in {"/login", "/force-password"}:
        response.headers["Cache-Control"] = "private, no-cache"
    return response


def is_real_admin(user: Any) -> bool:
    return bool(user and user["role"] == "admin")


def admin_view_enabled(request: Request, user: Any) -> bool:
    return is_real_admin(user) and request.cookies.get(ADMIN_VIEW_COOKIE) != "user"


def page_context(request: Request, **extra: Any) -> dict[str, Any]:
    user = request.state.user
    real_admin = is_real_admin(user)
    context = {
        "request": request,
        "user": {"username": user["username"]} if user else None,
        "is_admin": admin_view_enabled(request, user),
        "is_real_admin": real_admin,
        "csrf_token": user["csrf_token"] if user else "",
        "app_env": settings.app_env,
        "app_version": APP_VERSION,
        "language": request.state.language,
        "t": translator(request.state.language),
        "client_translations": client_translations(request.state.language),
    }
    context.update(extra)
    return context


def render_page(request: Request, name: str, *, status_code: int = 200, **extra: Any):
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=page_context(request, **extra),
        status_code=status_code,
    )


def render_login_page(request: Request, *, status_code: int = 200, error: str | None = None):
    token = new_login_csrf_token()
    response = render_page(
        request,
        "login.html",
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


def set_public_session_cookie(response, raw_token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        raw_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_days * 86400,
        path="/",
    )


def set_language_cookie(response, language: str) -> None:
    response.set_cookie(
        LANGUAGE_COOKIE,
        normalize_language(language),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=365 * 86400,
        path="/",
    )


def require_user(request: Request):
    user = request.state.user
    if not user:
        raise HTTPException(status_code=401, detail="Zaloguj się ponownie.")
    if user["must_change_password"] and request.url.path not in {"/force-password", "/logout"}:
        raise HTTPException(status_code=428, detail="Najpierw zmień hasło tymczasowe.")
    return user


def require_admin(user=Depends(require_user)):
    if not is_real_admin(user):
        raise HTTPException(status_code=403, detail="Brak uprawnień administratora.")
    return user


def verify_csrf(request: Request, user: Any, form_token: str | None = None) -> None:
    received = request.headers.get("X-CSRF-Token") or form_token or ""
    if not hmac.compare_digest(str(received), str(user["csrf_token"])):
        raise HTTPException(status_code=403, detail="Nieprawidłowy token formularza. Odśwież stronę.")


def enforce_rate_limit(bucket: str, key: str, *, limit: int, window_seconds: int) -> None:
    retry_after = rate_limiter.check(bucket, key, limit=limit, window_seconds=window_seconds)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="Zbyt wiele operacji. Spróbuj ponownie później.",
            headers={"Retry-After": str(retry_after)},
        )


def parse_proxy_networks(value: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    try:
        return list(
            parse_trusted_proxy_networks(value, max_networks=EFFECTIVE_PROXY_LIMIT)
        )
    except ProxyTrustError as exc:
        raise RepositoryError(str(exc)) from exc


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    configured_values = [settings.public_effective_trusted_proxies]
    if not settings.runtime_proxy_resolved:
        configured_values.append(secure_config.get_setting("known_proxies", ""))
    configured_proxies = ",".join(value for value in configured_values if value)
    networks = parse_proxy_networks(configured_proxies)
    if not any(peer_ip in network for network in networks):
        return peer
    chain: list[str] = []
    for raw in request.headers.get("x-forwarded-for", "").split(","):
        candidate = raw.strip()
        try:
            ipaddress.ip_address(candidate)
            chain.append(candidate)
        except ValueError:
            continue
    chain.append(peer)
    while len(chain) > 1:
        try:
            current = ipaddress.ip_address(chain[-1])
        except ValueError:
            break
        if not any(current in network for network in networks):
            break
        chain.pop()
    return chain[-1]


@app.exception_handler(RepositoryError)
async def repository_error(request: Request, exc: RepositoryError):
    return JSONResponse(
        {"detail": localize_message(str(exc), request.state.language)}, status_code=400
    )


@app.exception_handler(TMDBError)
async def tmdb_error(request: Request, exc: TMDBError):
    return JSONResponse(
        {"detail": localize_message(str(exc), request.state.language)}, status_code=502
    )


@app.exception_handler(SecureConfigError)
async def secure_config_error(request: Request, exc: SecureConfigError):
    return JSONResponse(
        {"detail": localize_message(str(exc), request.state.language)}, status_code=500
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
        "Unhandled Public request error.",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    detail = localize_message("Wewnętrzny błąd serwera.", request.state.language)
    return JSONResponse({"detail": detail}, status_code=500)


@app.get("/internal/health", include_in_schema=False)
async def health(request: Request):
    peer = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            raise HTTPException(status_code=404)
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    return {"status": "ok"}


@app.get("/posters/{filename}", include_in_schema=False)
async def poster(filename: str, user=Depends(require_user)):
    if filename != Path(filename).name or filename.startswith("."):
        raise HTTPException(status_code=404)
    target = (settings.posters_dir / filename).resolve()
    try:
        target.relative_to(settings.posters_dir.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    if not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(target, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/manifest.webmanifest", include_in_schema=False)
async def web_manifest(request: Request):
    return JSONResponse(
        manifest_document(request.state.language),
        media_type="application/manifest+json",
    )


@app.get("/service-worker.js", include_in_schema=False)
async def service_worker():
    return FileResponse(
        static_dir / "service-worker.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.state.user:
        return RedirectResponse("/", status_code=303)
    return render_login_page(request)


@app.post("/language")
async def set_language(
    request: Request,
    language: Annotated[Literal["en", "pl"], Form()],
    next_path: Annotated[str, Form(alias="next")] = "/",
    csrf_token: Annotated[str, Form()] = "",
):
    user = request.state.user
    if user:
        verify_csrf(request, user, csrf_token)
        await asyncio.to_thread(repo.set_user_language, user["id"], language)
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
        raise HTTPException(status_code=403, detail="Nieprawidłowy token formularza. Odśwież stronę.")
    ip = client_ip(request)
    normalized = username.strip().lower()[:64]
    generic_error = localize_message(
        "Nie udało się zalogować. Sprawdź dane lub spróbuj ponownie później.",
        request.state.language,
    )
    if len(username) > 64 or len(password) > 128:
        await asyncio.to_thread(login_protection.record_failure, ip, normalized)
        return render_login_page(request, status_code=401, error=generic_error)
    gate = await asyncio.to_thread(login_protection.check, ip, normalized)
    if gate.blocked:
        response = render_login_page(request, status_code=429, error=generic_error)
        response.headers["Retry-After"] = str(gate.retry_after)
        return response
    if gate.delay_seconds:
        await asyncio.sleep(gate.delay_seconds)
    async with auth_semaphore:
        user = await asyncio.to_thread(repo.authenticate, normalized, password, ip)
    if not user:
        failure_gate = await asyncio.to_thread(login_protection.record_failure, ip, normalized)
        status_code = 429 if failure_gate.blocked else 401
        response = render_login_page(request, status_code=status_code, error=generic_error)
        if failure_gate.blocked:
            response.headers["Retry-After"] = str(failure_gate.retry_after)
        return response
    await asyncio.to_thread(login_protection.record_success, ip, normalized)
    audit.emit(
        "login_success", actor_type="public_user", actor_id=user["id"],
        username=user["username"], ip_address=ip,
    )
    session = create_session(db, user["id"], secret=settings.session_secret, days=settings.session_days)
    destination = "/force-password" if user["must_change_password"] else "/"
    response = RedirectResponse(destination, status_code=303)
    set_public_session_cookie(response, session.raw_token)
    clear_login_csrf_cookie(
        response,
        LOGIN_CSRF_COOKIE,
        secure=settings.cookie_secure,
    )
    set_language_cookie(response, user["language"])
    response.delete_cookie(ADMIN_VIEW_COOKIE, path="/")
    response.delete_cookie("request_session", path="/")
    return response


@app.post("/logout")
async def logout(request: Request, csrf_token: Annotated[str, Form()] = ""):
    user = request.state.user
    if user:
        verify_csrf(request, user, csrf_token)
    delete_session(db, request.cookies.get(COOKIE_NAME), secret=settings.session_secret)
    if user:
        audit.emit(
            "logout", actor_type="public_user", actor_id=user["id"],
            username=user["username"], ip_address=client_ip(request),
        )
    response = RedirectResponse("/login", status_code=303)
    if user:
        set_language_cookie(response, user["language"])
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie("request_session", path="/")
    response.delete_cookie(ADMIN_VIEW_COOKIE, path="/")
    return response


@app.get("/force-password", response_class=HTMLResponse)
async def force_password_page(request: Request):
    user = request.state.user
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user["must_change_password"]:
        return RedirectResponse("/", status_code=303)
    return render_page(request, "force_password.html", error=None)


@app.post("/force-password", response_class=HTMLResponse)
async def force_password(
    request: Request,
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
):
    user = request.state.user
    if not user:
        return RedirectResponse("/login", status_code=303)
    verify_csrf(request, user, csrf_token)
    error = None
    if new_password != confirm_password:
        error = localize_message("Wprowadzone hasła nie są identyczne.", request.state.language)
    else:
        try:
            await asyncio.to_thread(repo.set_password, user["id"], new_password, force_change=False)
        except RepositoryError as exc:
            error = localize_message(str(exc), request.state.language)
    if error:
        return render_page(request, "force_password.html", status_code=400, error=error)
    await asyncio.to_thread(delete_other_sessions, db, user["id"])
    session = await asyncio.to_thread(
        create_session, db, user["id"], secret=settings.session_secret, days=settings.session_days
    )
    audit.emit(
        "password_changed", actor_type="public_user", actor_id=user["id"],
        username=user["username"], ip_address=client_ip(request), details={"forced": True},
    )
    response = RedirectResponse("/", status_code=303)
    set_public_session_cookie(response, session.raw_token)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = request.state.user
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["must_change_password"]:
        return RedirectResponse("/force-password", status_code=303)
    return render_page(request, "index.html")


class AddRequestBody(BaseModel):
    media_type: Literal["movie", "tv"]
    tmdb_id: int = Field(gt=0, le=2_147_483_647)
    season_number: int | None = Field(default=None, ge=0, le=1000)


class StatusBody(BaseModel):
    status: Literal["pending", "translation", "in_progress", "missing"]


class DeleteBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class PreferencesBody(BaseModel):
    own_request_liked: bool = True
    request_changes: bool = True
    admin_new_request: bool = True
    admin_messages: bool = True


class AdminViewBody(BaseModel):
    mode: Literal["admin", "user"]


class PushKeysBody(BaseModel):
    p256dh: str = Field(min_length=1, max_length=512)
    auth: str = Field(min_length=1, max_length=512)


class PushSubscriptionBody(BaseModel):
    endpoint: str = Field(min_length=1, max_length=4096)
    keys: PushKeysBody


class PushEndpointBody(BaseModel):
    endpoint: str = Field(min_length=1, max_length=4096)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=15, max_length=128)
    confirm_password: str = Field(min_length=15, max_length=128)


class UserCreateBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    temporary_password: str = Field(min_length=15, max_length=128)


class UserPasswordBody(BaseModel):
    temporary_password: str = Field(min_length=15, max_length=128)


class UserActiveBody(BaseModel):
    active: bool


class BroadcastBody(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=1000)


class ProxySettingsBody(BaseModel):
    known_proxies: str = Field(max_length=1000)


@app.get("/api/tmdb/search")
async def api_tmdb_search(q: str, request: Request, user=Depends(require_user)):
    query = q.strip()
    if len(query) < 2 or len(query) > 100:
        raise RepositoryError("Wyszukiwana fraza musi mieć 2–100 znaków.")
    enforce_rate_limit("tmdb-search", f"{user['id']}:{client_ip(request)}", limit=30, window_seconds=60)
    return {"items": await tmdb.search(query, request.state.language)}


@app.get("/api/tmdb/{media_type}/{tmdb_id}")
async def api_tmdb_details(media_type: str, tmdb_id: int, request: Request, user=Depends(require_user)):
    enforce_rate_limit("tmdb-details", f"{user['id']}:{client_ip(request)}", limit=60, window_seconds=300)
    details = await tmdb.title_details(media_type, tmdb_id, request.state.language)
    localized_key = "title" if media_type == "movie" else "name"
    localized = details.get(localized_key)
    if media_type == "movie":
        return {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title_pl": localized if request.state.language == "pl" else None,
            "title_en": localized if request.state.language == "en" else None,
            "title_original": details.get("original_title") or "",
            "release_date": details.get("release_date"),
            "seasons": [],
        }
    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title_pl": localized if request.state.language == "pl" else None,
        "title_en": localized if request.state.language == "en" else None,
        "title_original": details.get("original_name") or "",
        "release_date": details.get("first_air_date"),
        "seasons": details.get("seasons", []),
    }


@app.get("/api/requests")
async def api_requests(
    request: Request,
    state: str = "active",
    page: int = 1,
    page_size: int = 25,
    sort: str = "newest",
    status_filter: str = "all",
    user=Depends(require_user),
):
    is_admin = admin_view_enabled(request, user)
    return repo.paginated_requests(
        state,
        user["id"],
        is_admin,
        page=page,
        page_size=page_size,
        sort=sort,
        status_filter=status_filter,
    )


@app.post("/api/requests")
async def api_add_request(body: AddRequestBody, request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    enforce_rate_limit("add-request", str(user["id"]), limit=30, window_seconds=3600)
    media = await tmdb.media_for_request(body.media_type, body.tmdb_id, body.season_number)
    warning = None
    try:
        poster = await tmdb.cache_poster(media)
    except TMDBError as exc:
        poster = None
        warning = localize_message(str(exc), request.state.language)
    request_id, duplicate, state = repo.create_request(media, poster, user["id"])
    if state == "upcoming" and not duplicate:
        notify_upcoming_request(
            db,
            user["id"],
            media.title_pl,
            media.season_number,
            media.title_original,
            media.title_en,
        )
    if duplicate:
        message = "Ta pozycja już istniała — dodano Twój like."
    elif state == "upcoming":
        message = "Request został dodany i przeniesiony do działu „Przed premierą”."
    else:
        message = "Request został dodany."
    return {
        "id": request_id,
        "duplicate": duplicate,
        "state": state,
        "warning": warning,
        "message": localize_message(message, request.state.language),
    }


@app.post("/api/requests/{request_id}/like")
async def api_like(request_id: int, request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    return repo.toggle_like(request_id, user["id"])


@app.post("/api/requests/{request_id}/withdraw")
async def api_withdraw(request_id: int, request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    result = repo.withdraw_request(request_id, user["id"])
    audit.emit(
        "request_withdrawn",
        actor_type="public_user",
        actor_id=user["id"],
        details={"request_id": request_id, "result": result},
    )
    return {"result": result}


@app.patch("/api/requests/{request_id}/status")
async def api_status(request_id: int, body: StatusBody, request: Request, admin=Depends(require_admin)):
    verify_csrf(request, admin)
    repo.set_status(request_id, body.status)
    return {"ok": True}


@app.post("/api/requests/{request_id}/complete")
async def api_complete(request_id: int, request: Request, admin=Depends(require_admin)):
    verify_csrf(request, admin)
    repo.complete_request(request_id, admin["id"])
    return {"ok": True}


@app.post("/api/requests/{request_id}/restore")
async def api_restore(request_id: int, request: Request, admin=Depends(require_admin)):
    verify_csrf(request, admin)
    repo.restore_request(request_id)
    return {"ok": True}


@app.delete("/api/requests/{request_id}")
async def api_delete(request_id: int, body: DeleteBody, request: Request, admin=Depends(require_admin)):
    verify_csrf(request, admin)
    repo.delete_request(request_id, body.reason)
    return {"ok": True}


@app.get("/api/notifications/counts")
async def api_notification_counts(user=Depends(require_user)):
    return repo.notification_counts(user["id"])


@app.get("/api/notifications")
async def api_notifications(bucket: str = "all", user=Depends(require_user)):
    return repo.notifications(user["id"], bucket=bucket)


@app.post("/api/notifications/read")
async def api_notifications_read(request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    repo.mark_notifications_read(user["id"])
    return {"ok": True}


@app.post("/api/notifications/{notification_id}/read")
async def api_notification_read(notification_id: int, request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    repo.mark_notification_read(user["id"], notification_id)
    return {"ok": True}


@app.delete("/api/notifications/{notification_id}")
async def api_notification_delete(notification_id: int, request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    repo.delete_notification(user["id"], notification_id)
    return {"ok": True}


@app.delete("/api/notifications/read/all")
async def api_notifications_delete_read(request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    return {"deleted": repo.delete_read_notifications(user["id"])}


@app.get("/api/push/config")
async def api_push_config(user=Depends(require_user)):
    return {
        "public_key": push_service.public_key,
        "subscription_count": push_service.subscription_count(user["id"]),
    }


@app.put("/api/push/subscription")
async def api_push_subscribe(body: PushSubscriptionBody, request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    subscription_id = push_service.save_subscription(
        user["id"],
        body.endpoint,
        body.keys.p256dh,
        body.keys.auth,
    )
    return {"ok": True, "subscription_id": subscription_id}


@app.delete("/api/push/subscription")
async def api_push_unsubscribe(body: PushEndpointBody, request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    return {"removed": push_service.remove_subscription(user["id"], body.endpoint)}


@app.post("/api/push/test")
async def api_push_test(request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    enforce_rate_limit("push-test", str(user["id"]), limit=3, window_seconds=600)
    if push_service.subscription_count(user["id"]) == 0:
        raise RepositoryError("Najpierw włącz powiadomienia na tym urządzeniu.")
    notification_id = push_service.create_test_notification(user["id"])
    return {"ok": True, "notification_id": notification_id}


@app.get("/api/preferences")
async def api_preferences(user=Depends(require_user)):
    return repo.preferences(user["id"])


@app.put("/api/preferences")
async def api_preferences_update(body: PreferencesBody, request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    values = body.model_dump()
    if user["role"] != "admin":
        values.pop("admin_new_request", None)
    repo.update_preferences(user["id"], values)
    return {"ok": True}


@app.post("/api/account/admin-view")
async def api_admin_view(body: AdminViewBody, request: Request, admin=Depends(require_admin)):
    verify_csrf(request, admin)
    response = JSONResponse({"ok": True, "mode": body.mode})
    if body.mode == "user":
        response.set_cookie(
            ADMIN_VIEW_COOKIE,
            "user",
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
    else:
        response.delete_cookie(ADMIN_VIEW_COOKIE, path="/")
    return response


@app.post("/api/account/password")
async def api_change_password(body: PasswordBody, request: Request, user=Depends(require_user)):
    verify_csrf(request, user)
    if body.new_password != body.confirm_password:
        raise RepositoryError("Wprowadzone hasła nie są identyczne.")
    await asyncio.to_thread(
        repo.set_password, user["id"], body.new_password,
        current_password=body.current_password, force_change=False,
    )
    await asyncio.to_thread(delete_other_sessions, db, user["id"])
    session = await asyncio.to_thread(
        create_session, db, user["id"], secret=settings.session_secret, days=settings.session_days
    )
    audit.emit(
        "password_changed", actor_type="public_user", actor_id=user["id"],
        username=user["username"], ip_address=client_ip(request), details={"forced": False},
    )
    response = JSONResponse({
        "ok": True,
        "message": localize_message(
            "Hasło zostało zmienione. Pozostałe sesje zakończono.", request.state.language
        ),
    })
    set_public_session_cookie(response, session.raw_token)
    return response


# Added last so these pure ASGI guards wrap session loading and all routes.
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_bytes=settings.max_request_body_bytes,
    language_cookie_name=LANGUAGE_COOKIE,
)
app.add_middleware(
    AllowedHostsMiddleware,
    allowed_hosts=settings.allowed_hosts,
    dynamic_base_url=lambda: secure_config.get_setting("public_base_url", settings.app_base_url),
    language_cookie_name=LANGUAGE_COOKIE,
)

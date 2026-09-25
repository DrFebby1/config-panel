"""Application wiring: middleware, page routes and router registration."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import api_admin, api_public, config, db, service

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Endpoints that legitimately accept cross-origin or key-authenticated calls.
CSRF_EXEMPT = {"/api/auth/login", "/api/auth/setup", "/api/report"}

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#111827"/>'
    '<path d="M18 34l9 9 19-22" fill="none" stroke="#38bdf8" stroke-width="7" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Config Panel",
    version="1.0.0",
    docs_url="/docs" if config.ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if config.ENABLE_DOCS else None,
    lifespan=lifespan,
)


@app.exception_handler(service.ValidationError)
async def validation_error_handler(request, exc: service.ValidationError):
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


@app.middleware("http")
async def security_middleware(request, call_next):
    # Defence in depth against cross-site writes: the session cookie is already
    # SameSite=Lax, and any browser-supplied Origin must match our own host.
    if (
        request.method in UNSAFE_METHODS
        and request.url.path.startswith("/api/")
        and request.url.path not in CSRF_EXEMPT
    ):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            origin_host = origin.split("://", 1)[-1].split("/")[0]
            host = request.headers.get("host", "")
            if origin_host and host and origin_host != host:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "درخواست از منبع ناشناس رد شد"},
                )

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'",
    )
    return response


app.include_router(api_admin.router)
app.include_router(api_public.router)

if config.STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    return Response(content=FAVICON, media_type="image/svg+xml")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url=config.ADMIN_PATH + "/", status_code=307)


@app.get(config.ADMIN_PATH, include_in_schema=False)
def admin_root():
    return RedirectResponse(url=config.ADMIN_PATH + "/", status_code=307)


@app.get(config.ADMIN_PATH + "/", include_in_schema=False)
def admin_page():
    page = config.STATIC_DIR / "index.html"
    if not page.is_file():
        return JSONResponse(
            status_code=500, content={"detail": "static/index.html missing"}
        )
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/u/{token}", include_in_schema=False)
def user_page(token: str):
    """The dedicated link each user gets to inspect their own config."""
    page = config.STATIC_DIR / "user.html"
    if not page.is_file():
        return JSONResponse(status_code=500, content={"detail": "static/user.html missing"})
    return FileResponse(page, media_type="text/html; charset=utf-8")

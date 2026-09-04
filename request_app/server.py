from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

import uvicorn

from .proxy_trust import prepare_runtime_proxy_environment


LOGGER = logging.getLogger("penczreq.runtime_proxy")
APPLICATIONS = {
    "public": "request_app.main:app",
    "control": "request_app.control:app",
}


def run(component: str, *, host: str, port: int, reload: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    resolution = prepare_runtime_proxy_environment(component)
    for warning in resolution.warnings:
        LOGGER.warning("Runtime gateway auto-trust nie rozszerzył zaufania: %s", warning)
    if resolution.runtime_gateway:
        LOGGER.info(
            "Runtime gateway auto-trust dodał %s dla komponentu %s.",
            resolution.runtime_gateway,
            component,
        )
    elif resolution.auto_trust_enabled and resolution.access_mode != "reverse-proxy":
        LOGGER.info(
            "Runtime gateway auto-trust jest nieaktywny dla komponentu %s w trybie %s.",
            component,
            resolution.access_mode,
        )
    uvicorn.run(
        APPLICATIONS[component],
        host=host,
        port=port,
        reload=reload,
        server_header=False,
        proxy_headers=True,
        forwarded_allow_ips=resolution.effective_trusted_proxies,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="penczREQ runtime server")
    parser.add_argument("component", choices=sorted(APPLICATIONS))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--reload", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run(args.component, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

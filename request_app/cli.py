from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from .config import load_settings
from .database import Database, utc_now
from .repository import Repository
from .secure_config import SecureConfigStore
from .security import hash_password, normalize_username, validate_password, validate_username
from .title_backfill import backfill_english_titles
from .tmdb import TMDBClient, TMDBError


def init_admin(username: str) -> int:
    username = normalize_username(username)
    if error := validate_username(username):
        print(error, file=sys.stderr)
        return 2

    first = getpass.getpass("Nowe hasło administratora: ")
    second = getpass.getpass("Powtórz hasło: ")
    if first != second:
        print("Hasła nie są identyczne.", file=sys.stderr)
        return 2
    if error := validate_password(first, username=username):
        print(error, file=sys.stderr)
        return 2

    settings = load_settings()
    db = Database(settings.database_path)
    db.initialize()
    now = utc_now()
    with db.transaction() as conn:
        admin_exists = conn.execute(
            "SELECT username FROM users WHERE role = 'admin' AND is_active = 1 LIMIT 1"
        ).fetchone()
        if admin_exists:
            print(
                "Aktywny administrator publiczny już istnieje. Zmianę konta wykonaj w panelu Control.",
                file=sys.stderr,
            )
            return 1
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            print(f"Konto '{username}' już istnieje.", file=sys.stderr)
            return 1
        conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, created_at, password_changed_at) VALUES (?, ?, 'admin', 1, 0, ?, ?)",
            (username, hash_password(first), now, now),
        )
    print(f"Utworzono administratora '{username}' w {settings.database_path}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m request_app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("init-admin", help="Utwórz pierwszego administratora")
    admin.add_argument("username")
    titles = sub.add_parser(
        "backfill-english-titles",
        help="Jawnie zaplanuj lub zastosuj sieciowy backfill tytułów EN",
    )
    titles.add_argument(
        "--apply",
        action="store_true",
        help="Zapisz pobrane tytuły; bez tej flagi baza pozostaje bez zmian",
    )
    titles.add_argument(
        "--allow-production-network-backfill",
        action="store_true",
        help="Osobna bramka dla jawnie zatwierdzonego środowiska production",
    )
    args = parser.parse_args()
    if args.command == "init-admin":
        return init_admin(args.username)
    if args.command == "backfill-english-titles":
        settings = load_settings()
        if settings.app_env == "production" and not args.allow_production_network_backfill:
            print(
                "Odmowa: sieciowy backfill production wymaga osobnej jawnej bramki.",
                file=sys.stderr,
            )
            return 2
        db = Database(settings.database_path)
        if args.apply:
            db.initialize()
        repository = Repository(db)
        secure_config = SecureConfigStore(db, settings.config_encryption_key)
        client = TMDBClient(
            secure_config.tmdb_token,
            settings.posters_dir,
            settings.poster_max_bytes,
        )
        try:
            report = asyncio.run(
                backfill_english_titles(repository, client, apply=args.apply)
            )
        except TMDBError as exc:
            print(f"Backfill przerwany bez zapisu tytułów: {exc}", file=sys.stderr)
            return 1
        print(f"target_titles: {report.target_titles}")
        print(f"skipped_titles: {report.skipped_titles}")
        print(f"affected_requests: {report.affected_requests}")
        print(
            "mutations_performed: "
            f"{'true' if report.mutations_performed else 'false'}"
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

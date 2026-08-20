from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, question TEXT NOT NULL,
    domain TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT, source_type TEXT NOT NULL,
    publisher TEXT, published_at TEXT, accessed_at TEXT NOT NULL, local_file TEXT,
    sha256 TEXT, status TEXT NOT NULL DEFAULT 'available', notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY, text TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_links (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    claim_id TEXT NOT NULL REFERENCES claims(id), relation TEXT NOT NULL,
    locator TEXT, quote TEXT, limitations TEXT, created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def item_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def workspace(path: str, must_exist: bool = True) -> tuple[Path, Path]:
    root = Path(path).expanduser().resolve()
    database = root / "atlas.sqlite3"
    if must_exist and not database.is_file():
        raise SystemExit(f"Not a Corpus Atlas workspace: {root}")
    return root, database


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def write_snapshot(root: Path, connection: sqlite3.Connection) -> None:
    investigation = connection.execute("SELECT * FROM investigations LIMIT 1").fetchone()
    data = {
        "format": "corpus-atlas/0.1",
        "investigation": dict(investigation) if investigation else None,
        "sources": [dict(row) for row in connection.execute("SELECT * FROM sources ORDER BY created_at, id")],
        "claims": [dict(row) for row in connection.execute("SELECT * FROM claims ORDER BY created_at, id")],
        "evidence_links": [dict(row) for row in connection.execute("SELECT * FROM evidence_links ORDER BY created_at, id")],
    }
    (root / "atlas.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_init(args: argparse.Namespace) -> None:
    root, database = workspace(args.path, must_exist=False)
    root.mkdir(parents=True, exist_ok=True)
    (root / "attachments").mkdir(exist_ok=True)
    if database.exists() and not args.force:
        raise SystemExit(f"Workspace already exists: {root}")
    if database.exists():
        database.unlink()
    with connect(database) as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO investigations VALUES (?, ?, ?, ?, ?)",
            (item_id("inv"), args.title, args.question, args.domain, now()),
        )
        write_snapshot(root, connection)
    print(root)


def command_source_add(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    source_id = item_id("src")
    local_file = None
    digest = None
    if args.file:
        original = Path(args.file).expanduser().resolve()
        if not original.is_file():
            raise SystemExit(f"Attachment not found: {original}")
        target = root / "attachments" / f"{source_id}-{original.name}"
        shutil.copy2(original, target)
        local_file = str(target.relative_to(root))
        digest = sha256(target)
    with connect(database) as connection:
        connection.execute(
            """INSERT INTO sources
               (id,title,url,source_type,publisher,published_at,accessed_at,local_file,sha256,status,notes,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source_id, args.title, args.url, args.source_type, args.publisher,
             args.published_at, args.accessed_at or now(), local_file, digest,
             args.status, args.notes, now()),
        )
        write_snapshot(root, connection)
    print(source_id)


def command_claim_add(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    claim_id = item_id("clm")
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO claims VALUES (?, ?, ?, ?, ?)",
            (claim_id, args.text, args.kind, args.status, now()),
        )
        write_snapshot(root, connection)
    print(claim_id)


def command_link_add(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    link_id = item_id("lnk")
    try:
        with connect(database) as connection:
            connection.execute(
                "INSERT INTO evidence_links VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (link_id, args.source, args.claim, args.relation, args.locator,
                 args.quote, args.limitations, now()),
            )
            write_snapshot(root, connection)
    except sqlite3.IntegrityError as error:
        raise SystemExit(f"Unknown source or claim ID: {error}") from error
    print(link_id)


def command_status(args: argparse.Namespace) -> None:
    _, database = workspace(args.path)
    with connect(database) as connection:
        inv = connection.execute("SELECT title, question, domain FROM investigations LIMIT 1").fetchone()
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "claims", "evidence_links")
        }
        unresolved = connection.execute("SELECT COUNT(*) FROM claims WHERE status = 'unresolved'").fetchone()[0]
        unlinked = connection.execute(
            "SELECT COUNT(*) FROM claims c WHERE NOT EXISTS (SELECT 1 FROM evidence_links e WHERE e.claim_id=c.id)"
        ).fetchone()[0]
    print(json.dumps({"title": inv["title"], "question": inv["question"],
                      "domain": inv["domain"], **counts,
                      "unresolved_claims": unresolved, "unlinked_claims": unlinked},
                     ensure_ascii=False, indent=2))


def command_export(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    with connect(database) as connection:
        write_snapshot(root, connection)
    source = root / "atlas.json"
    if args.output:
        destination = Path(args.output).expanduser().resolve()
        shutil.copy2(source, destination)
        print(destination)
    else:
        print(source.read_text(encoding="utf-8"), end="")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="corpus-atlas", description="Local-first investigation corpus")
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create an investigation workspace")
    init.add_argument("path")
    init.add_argument("--title", required=True)
    init.add_argument("--question", required=True)
    init.add_argument("--domain", choices=("general", "science", "finance", "public-affairs"), default="general")
    init.add_argument("--force", action="store_true")
    init.set_defaults(run=command_init)

    source = commands.add_parser("source", help="manage sources").add_subparsers(dest="source_command", required=True)
    source_add = source.add_parser("add")
    source_add.add_argument("path")
    source_add.add_argument("--title", required=True)
    source_add.add_argument("--url")
    source_add.add_argument("--source-type", required=True)
    source_add.add_argument("--publisher")
    source_add.add_argument("--published-at")
    source_add.add_argument("--accessed-at")
    source_add.add_argument("--file")
    source_add.add_argument("--status", choices=("available", "missing", "inaccessible", "superseded"), default="available")
    source_add.add_argument("--notes")
    source_add.set_defaults(run=command_source_add)

    claim = commands.add_parser("claim", help="manage atomic claims").add_subparsers(dest="claim_command", required=True)
    claim_add = claim.add_parser("add")
    claim_add.add_argument("path")
    claim_add.add_argument("--text", required=True)
    claim_add.add_argument("--kind", choices=("observed", "interpreted", "hypothesis"), default="observed")
    claim_add.add_argument("--status", choices=("unresolved", "supported", "contested", "rejected"), default="unresolved")
    claim_add.set_defaults(run=command_claim_add)

    link = commands.add_parser("link", help="connect evidence to claims").add_subparsers(dest="link_command", required=True)
    link_add = link.add_parser("add")
    link_add.add_argument("path")
    link_add.add_argument("--source", required=True)
    link_add.add_argument("--claim", required=True)
    link_add.add_argument("--relation", choices=("supports", "contradicts", "qualifies", "contextualizes", "mentions"), required=True)
    link_add.add_argument("--locator")
    link_add.add_argument("--quote")
    link_add.add_argument("--limitations")
    link_add.set_defaults(run=command_link_add)

    status = commands.add_parser("status", help="summarize a workspace")
    status.add_argument("path")
    status.set_defaults(run=command_status)

    export = commands.add_parser("export", help="write portable JSON")
    export.add_argument("path")
    export.add_argument("--output")
    export.set_defaults(run=command_export)
    return root


def main() -> None:
    try:
        args = parser().parse_args()
        args.run(args)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()

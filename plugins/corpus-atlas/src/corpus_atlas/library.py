"""Library maintenance: matrix links, publisher supplement rules, browser queue
and import, dedupe/merge, citation-key normalization, and canonical BibTeX.

All network access goes through :func:`corpus_atlas.fulltext.http_get`.
"""
from __future__ import annotations

import csv
import html
import json
import re
import sqlite3
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from . import fulltext as ft
from .scholarly import citation_key, current_time, normalize_doi, normalized_title
from .urls import sanitize_obj, sanitize_url

LINKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_links (
    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
    kind TEXT NOT NULL, url TEXT NOT NULL, source TEXT, license TEXT, filename TEXT,
    UNIQUE(paper_id, kind, url)
);
"""

BLOCKED_SUFFIXES = {".app", ".bat", ".cmd", ".command", ".dmg", ".exe", ".html", ".htm", ".js", ".msi", ".pkg", ".sh"}
TRUSTED_REPOSITORIES = ("arxiv.org", "biorxiv.org", "medrxiv.org", "ncbi.nlm.nih.gov", "europepmc.org",
                        "projecteuclid.org", "plos.org", "openreview.net", "aclanthology.org")


# ---------------------------------------------------------------- matrix links
def supplement_entries(record: dict[str, Any]) -> list[dict[str, str]]:
    """Parse ``supplement_urls`` / ``supplementary_materials`` (JSON list/object or ;-separated)."""
    raw = record.get("supplement_urls") or record.get("supplementary_materials") or ""
    if not raw:
        return []
    if isinstance(raw, (list, dict)):
        parsed: Any = raw
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [value.strip() for value in str(raw).replace("\n", ";").split(";") if value.strip()]
    if not isinstance(parsed, list):
        parsed = [parsed]
    output = []
    for item in parsed:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict) or not item.get("url"):
            continue
        output.append({
            "url": str(item["url"]),
            "source": str(item.get("source") or record.get("supplement_source") or "project matrix"),
            "license": str(item.get("license") or record.get("supplement_license") or record.get("oa_license") or "oa-unspecified"),
            "filename": str(item.get("filename") or ""),
        })
    return output


def store_record_links(connection: sqlite3.Connection, paper_id: str, record: dict[str, Any]) -> int:
    connection.executescript(LINKS_SCHEMA)
    links = []
    pdf_url = str(record.get("open_access_pdf") or record.get("pdf_url") or "")
    if pdf_url:
        host = urllib.parse.urlparse(pdf_url).netloc.lower()
        trusted = any(domain in host for domain in TRUSTED_REPOSITORIES)
        if str(record.get("oa_status") or "").lower() != "closed" or trusted:
            links.append(("pdf", pdf_url, "verified metadata OA location", record.get("oa_license") or "oa-unspecified", ""))
    for entry in supplement_entries(record):
        links.append(("supplement", entry["url"], entry["source"], entry["license"], entry["filename"]))
    for kind, url, source, license_name, filename in links:
        connection.execute("INSERT OR IGNORE INTO paper_links VALUES (?,?,?,?,?,?,?)",
                           (f"lnk_{uuid.uuid4().hex[:12]}", paper_id, kind, sanitize_url(url), source, license_name, filename))
    return len(links)


def record_links(connection: sqlite3.Connection, paper_id: str, kind: str) -> list[dict[str, str]]:
    connection.executescript(LINKS_SCHEMA)
    return [dict(row) for row in connection.execute(
        "SELECT url, source, license, filename FROM paper_links WHERE paper_id=? AND kind=? ORDER BY rowid", (paper_id, kind))]


# ---------------------------------------------------------------- publisher rules
def _page(doi: str, email: str | None) -> tuple[str, str]:
    response = ft.http_get("https://doi.org/" + urllib.parse.quote(doi, safe="/"), "text/html,application/xhtml+xml", email, timeout=30, attempts=1)
    if response.status != 200:
        return response.url, ""
    return response.url, response.body.decode("utf-8", "ignore")


def _hrefs(page: str, base: str) -> list[str]:
    return [urllib.parse.urljoin(base, html.unescape(value)) for value in re.findall(r'href=["\']([^"\']+)["\']', page)]


def springer_nature_links(page: str, base: str) -> list[str]:
    """Springer Nature / BMC: electronic supplementary material lives under media.springernature.com/.../esm/."""
    return sorted({u for u in _hrefs(page, base) if "static-content.springer.com/esm/" in u.lower()
                   or ("media.springernature.com" in u and "/esm/" in u.lower())})


def elsevier_links(page: str, base: str) -> list[str]:
    return sorted({u for u in _hrefs(page, base) if re.search(r"ars\.els-cdn\.com/content/image/.*mmc\d+", u, re.I)})


def generic_links(page: str, base: str) -> list[str]:
    """Wiley / ACS / PLOS / Frontiers / MDPI / RSC supplement endpoints; never figure images."""
    patterns = (r"/action/downloadSupplement\?", r"/doi/suppl/", r"/suppl_file/", r"journals\.plos\.org/.*/article/file\?id=.*\.s\d+",
                r"frontiersin\.org/api/v\d/articles/\d+/file/Data_Sheet", r"mdpi\.com/.*/s\d+$", r"/suppdata/", r"supplementary[-_]?(?:material|file|data|information)",
                r"esm\d*\.(?:pdf|docx?|xlsx?|zip|csv)$")
    output = set()
    for url in _hrefs(page, base):
        low = url.lower()
        if Path(urllib.parse.urlparse(low).path).suffix in ft.IMAGE_SUFFIXES or "/figures/" in low:
            continue
        if any(re.search(pattern, url, re.I) for pattern in patterns):
            output.add(url)
    return sorted(output)


PUBLISHER_RULES: tuple[tuple[tuple[str, ...], Any, str], ...] = (
    (("10.1038/", "10.1186/", "10.1007/", "10.1140/", "10.1057/"), springer_nature_links, "Springer Nature article page"),
    (("10.1016/",), elsevier_links, "Elsevier article page"),
    (("10.",), generic_links, "publisher article page"),
)


def publisher_supplement_links(doi: str, email: str | None) -> tuple[str, list[str], str]:
    page_url, page = _page(doi, email)
    if not page:
        return page_url, [], ""
    for prefixes, rule, label in PUBLISHER_RULES:
        if doi.startswith(prefixes):
            links = rule(page, page_url)
            if links:
                return page_url, links, label
    return page_url, [], ""


def acceptable_supplement(body: bytes, content_type: str, name: str) -> str | None:
    """Return the reason to reject a fetched supplement, or None if it is a real file."""
    if not body:
        return "empty"
    if content_type in {"text/html", "application/xhtml+xml"} or ft.infer_extension(body, content_type, name) == ".html":
        return "HTML page instead of a file"
    if Path(urllib.parse.urlparse(name).path).suffix.lower() in BLOCKED_SUFFIXES:
        return "blocked file type"
    return None


# ---------------------------------------------------------------- browser queue / import
QUEUE_FIELDS = ("citation_key", "doi", "title", "kind", "reason", "landing_url", "file", "url", "source", "license")


def write_browser_queue(root: Path, rows: list[dict[str, str]]) -> Path:
    path = root / "reports" / "browser-queue.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[tuple[str, str], dict[str, Any]] = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                existing[(row.get("citation_key", ""), row.get("kind", ""))] = row
    for row in sanitize_obj(rows):
        existing[(row["citation_key"], row["kind"])] = {**{f: "" for f in QUEUE_FIELDS}, **row}
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=QUEUE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(sanitize_obj(list(existing.values())), key=lambda r: (r["kind"], r["citation_key"])))
    return path


def import_browser_results(connection: sqlite3.Connection, root: Path, csv_path: Path, apply: bool) -> list[dict[str, Any]]:
    """Ingest files fetched manually/in a browser. Rows need citation_key|doi, kind
    (main|supplement), file (path relative to the CSV), url, source, license."""
    report = []
    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        ref = row.get("citation_key") or row.get("ref") or ""
        doi = normalize_doi(row.get("doi"))
        paper = connection.execute("SELECT * FROM papers WHERE citation_key=? OR (doi IS NOT NULL AND doi=?)", (ref, doi)).fetchone()
        file_value = (row.get("file") or "").strip()
        entry: dict[str, Any] = {"citation_key": ref or doi, "file": file_value}
        if not file_value:
            continue  # still queued; nothing fetched yet
        path = Path(file_value).expanduser()
        if not path.is_absolute():
            path = (csv_path.parent / path).resolve()
        if paper is None:
            entry["status"] = "unknown-paper"
        elif not path.is_file():
            entry["status"] = "missing-file"
        elif not (row.get("source") and row.get("license")):
            entry["status"] = "needs-source-and-license"
        else:
            kind = "main" if (row.get("kind") or "main") == "main" else "supplement"
            body = path.read_bytes()
            if kind == "main" and not body.startswith(b"%PDF-"):
                entry["status"] = "not-a-pdf"
            elif kind == "supplement" and (reason := acceptable_supplement(body, "", path.name)):
                entry["status"] = reason
            elif not apply:
                entry["status"] = "ready-preview"
            else:
                stored = ft.store_file(connection, root, paper, kind, body, row.get("url") or path.as_uri(),
                                       row["source"], row["license"], "application/pdf" if kind == "main" else "", path.name)
                entry.update(status="imported", stored=stored["file"])
        report.append(entry)
    if apply:
        connection.commit()
    return report


# ---------------------------------------------------------------- dedupe / normalize
def _identity_groups(connection: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    rows = list(connection.execute("SELECT * FROM papers ORDER BY created_at, citation_key"))
    parent = {row["id"]: row["id"] for row in rows}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen: dict[str, str] = {}
    for row in rows:
        keys = [f"doi:{row['doi']}" if row["doi"] else "", f"arxiv:{row['arxiv']}" if row["arxiv"] else "",
                f"pmid:{row['pmid']}" if row["pmid"] else ""]
        title = normalized_title(row["title"]).replace(" ", "")
        if len(title) >= 20:
            keys.append(f"title:{title}")
        for key in filter(None, keys):
            if key in seen:
                parent[find(row["id"])] = find(seen[key])
            else:
                seen[key] = row["id"]
    # sha256-identical main files also mark duplicates
    for row in connection.execute("SELECT sha256, GROUP_CONCAT(paper_id) ids FROM paper_files WHERE role='main' GROUP BY sha256 HAVING COUNT(DISTINCT paper_id)>1"):
        ids = row["ids"].split(",")
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(find(row["id"]), []).append(row)
    return [group for group in groups.values() if len(group) > 1]


def _score(connection: sqlite3.Connection, row: sqlite3.Row) -> tuple:
    files = connection.execute("SELECT COUNT(*) FROM paper_files WHERE paper_id=? AND role='main'", (row["id"],)).fetchone()[0]
    filled = sum(1 for key in row.keys() if row[key] not in (None, "", "[]"))
    preprint = (row["doi"] or "").startswith(("10.48550/", "10.1101/"))
    return (files > 0, not preprint, filled, -len(row["citation_key"]))


SCORE_RULES = ("has-main-file", "not-preprint", "most-complete-metadata", "shortest-citation-key")
DEFAULT_PREFERENCES = ("config/duplicate-preferences.yaml", "config/duplicate-preferences.yml",
                       "config/duplicate-preferences.json", "config/duplicate-pdf-preferences.yaml")


def load_preferences(path: Path) -> dict[str, str]:
    """paper-lib format: ``duplicates: {<group key>: {preferred: <record>}}`` (YAML or JSON).

    ``<group key>`` is the sha256 of a duplicated main file (paper-lib), or any member's
    citation key / DOI / arXiv ID / paper ID. ``<record>`` names the record to keep by
    citation key, paper ID, DOI, or arXiv ID. A bare ``{<group key>: <record>}`` mapping
    is also accepted."""
    from .yamlio import YAMLError, load_yaml
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text) if path.suffix.lower() == ".json" else load_yaml(text)
    except (OSError, json.JSONDecodeError, YAMLError) as error:
        raise SystemExit(f"Could not read duplicate preferences {path}: {error}") from error
    payload = payload or {}
    if not isinstance(payload, dict):
        raise SystemExit(f"Duplicate preferences must be a mapping: {path}")
    groups = payload.get("duplicates", payload)
    if not isinstance(groups, dict):
        raise SystemExit(f"'duplicates' must be a mapping: {path}")
    output = {}
    for key, item in groups.items():
        preferred = item.get("preferred") if isinstance(item, dict) else item
        if not preferred:
            raise SystemExit(f"Duplicate preference {key!r} has no 'preferred' record")
        output[str(key).strip().lower()] = str(preferred).strip()
    return output


def find_preferences(root: Path, explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"Duplicate preferences file not found: {path}")
        return path
    return next((root / name for name in DEFAULT_PREFERENCES if (root / name).is_file()), None)


def _member_aliases(connection: sqlite3.Connection, row: sqlite3.Row) -> set[str]:
    aliases = {row["id"], row["citation_key"], row["doi"] or "", row["arxiv"] or "", row["pmid"] or ""}
    aliases.update(r["sha256"] for r in connection.execute("SELECT sha256 FROM paper_files WHERE paper_id=? AND role='main'", (row["id"],)))
    return {alias.lower() for alias in aliases if alias}


def _preferred(connection: sqlite3.Connection, group: list[sqlite3.Row], preferences: dict[str, str]) -> tuple[sqlite3.Row | None, str]:
    aliases = {row["id"]: _member_aliases(connection, row) for row in group}
    every = set().union(*aliases.values())
    matched = {key: value for key, value in preferences.items() if key in every}
    if not matched:
        return None, ""
    choices = set()
    for key, value in matched.items():
        winners = [row for row in group if value.lower() in aliases[row["id"]]]
        if not winners:
            raise SystemExit(f"Invalid duplicate preference for {key}: {value} is not a member of "
                             f"{sorted(row['citation_key'] for row in group)}")
        choices.add(winners[0]["id"])
    if len(choices) != 1:
        raise SystemExit(f"Conflicting duplicate preferences for {sorted(row['citation_key'] for row in group)}: {sorted(matched)}")
    keeper = next(row for row in group if row["id"] in choices)
    return keeper, "preference:" + ",".join(sorted(matched))


def _deciding_rule(connection: sqlite3.Connection, keeper: sqlite3.Row, group: list[sqlite3.Row]) -> str:
    best = _score(connection, keeper)
    runner = max((_score(connection, row) for row in group if row["id"] != keeper["id"]), default=best)
    for rule, mine, theirs in zip(SCORE_RULES, best, runner):
        if mine != theirs:
            return f"score:{rule}"
    return "score:first-imported"


def dedupe(connection: sqlite3.Connection, apply: bool, preferences: dict[str, str] | None = None,
           require_preferences: bool = False) -> list[dict[str, Any]]:
    """Group by DOI/arXiv/PMID/normalized title/identical main PDF; keep the record named
    by a manual preference, else the richest record. Each entry reports the deciding rule."""
    connection.executescript(LINKS_SCHEMA)
    preferences = preferences or {}
    report = []
    groups = _identity_groups(connection)
    plans = []
    for group in groups:
        keeper, rule = _preferred(connection, group, preferences)
        if keeper is None:
            if require_preferences:
                raise SystemExit("Duplicate group needs an explicit preference: "
                                 + ", ".join(sorted(row["citation_key"] for row in group)))
            keeper = max(group, key=lambda r: _score(connection, r))
            rule = _deciding_rule(connection, keeper, group)
        plans.append((group, keeper, rule))
    for group, keeper, rule in plans:
        others = [row for row in group if row["id"] != keeper["id"]]
        entry = {"keep": keeper["citation_key"], "merge": [row["citation_key"] for row in others], "rule": rule, "applied": False}
        if apply:
            for row in others:
                updates = {key: row[key] for key in ("doi", "arxiv", "pmid", "semantic_scholar_id", "openalex_id", "venue",
                                                     "publisher", "abstract", "url", "year")
                           if keeper[key] in (None, "") and row[key] not in (None, "")}
                for table in ("paper_relations", "paper_files", "paper_links"):
                    connection.execute(f"UPDATE OR IGNORE {table} SET paper_id=? WHERE paper_id=?", (keeper["id"], row["id"]))
                    connection.execute(f"DELETE FROM {table} WHERE paper_id=?", (row["id"],))
                connection.execute("DELETE FROM papers WHERE id=?", (row["id"],))
                if updates:
                    connection.execute(f"UPDATE papers SET {', '.join(f'{k}=?' for k in updates)}, updated_at=? WHERE id=?",
                                       (*updates.values(), current_time(), keeper["id"]))
            if connection.execute("SELECT 1 FROM paper_files WHERE paper_id=? AND role='main'", (keeper["id"],)).fetchone():
                connection.execute("UPDATE papers SET fulltext_status='local-fulltext' WHERE id=?", (keeper["id"],))
            entry["applied"] = True
        report.append(entry)
    if apply:
        connection.commit()
    return report


def alphabetic_suffix(index: int) -> str:
    output = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        output = chr(ord("a") + remainder) + output
    return output


def normalize_keys(connection: sqlite3.Connection, root: Path, apply: bool) -> list[dict[str, Any]]:
    """Re-derive canonical citation keys (author+year+keyword, a/b/c suffixes) and
    attachment names (<key>.pdf, <key>-2.pdf, <key>-supplement[-n].<ext>)."""
    rows = list(connection.execute("SELECT * FROM papers"))
    used: set[str] = set()
    allocation: dict[str, str] = {}
    for row in sorted(rows, key=lambda r: (citation_key({"title": r["title"], "authors": json.loads(r["authors"] or "[]"), "year": r["year"]}), r["created_at"], r["id"])):
        base = citation_key({"title": row["title"], "authors": json.loads(row["authors"] or "[]"), "year": row["year"]})
        key, counter = base, 0
        while key in used:
            counter += 1
            key = base + alphabetic_suffix(counter)
        used.add(key)
        allocation[row["id"]] = key
    report = []
    renames: list[Any] = []
    for row in rows:
        key = allocation[row["id"]]
        files = list(connection.execute("SELECT * FROM paper_files WHERE paper_id=? ORDER BY role, retrieved_at, id", (row["id"],)))
        numbers: dict[str, int] = {}
        moves = []
        for file_row in files:
            suffix = Path(file_row["local_file"]).suffix.lower() or ".bin"
            label = {"main": "", "preprint": "-preprint"}.get(file_row["role"], "-supplement")
            group = file_row["role"] + (suffix if label else "")
            numbers[group] = numbers.get(group, 0) + 1
            n = numbers[group]
            target = f"attachments/{key}{label}{'' if n == 1 else f'-{n}'}{suffix}"
            if target != file_row["local_file"]:
                moves.append((file_row["id"], file_row["local_file"], target))
        if key != row["citation_key"] or moves:
            report.append({"from": row["citation_key"], "to": key, "files": [{"from": a, "to": b} for _, a, b in moves]})
            renames.append((row["id"], key, moves))
    if apply and renames:
        for paper_id, _key, _moves in renames:  # two-phase to avoid UNIQUE clashes
            connection.execute("UPDATE papers SET citation_key=? WHERE id=?", (f"__tmp_{paper_id}", paper_id))
        staged = []
        for paper_id, key, moves in renames:
            connection.execute("UPDATE papers SET citation_key=?, updated_at=? WHERE id=?", (key, current_time(), paper_id))
            for file_id, old, new in moves:
                temp = root / f"{new}.__moving__{file_id}"
                if (root / old).exists():
                    (root / old).rename(temp)
                staged.append((file_id, temp, root / new, new))
        for file_id, temp, final, new in staged:
            if temp.exists():
                temp.rename(final)
            connection.execute("UPDATE paper_files SET local_file=? WHERE id=?", (new, file_id))
        connection.commit()
    for entry in report:
        entry["applied"] = bool(apply)
    return report


# ---------------------------------------------------------------- BibTeX
LATEX_MAP = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_"}
ARTICLE_HINT = re.compile(r"journal|letters|review|transactions|annals|proceedings of the national", re.I)
CONFERENCE_HINT = re.compile(r"conference|proceedings|symposium|workshop|neurips|icml|iclr|cvpr|acl\b", re.I)


def bib_value(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return "".join(LATEX_MAP.get(ch, ch) for ch in text)


def bib_author(name: str) -> str:
    name = name.strip()
    if "," in name or " " not in name:
        return name
    parts = name.split()
    particles = {"van", "von", "de", "der", "den", "da", "di", "del", "la", "le", "du"}
    index = len(parts) - 1
    while index > 1 and parts[index - 1].lower() in particles:
        index -= 1
    return f"{' '.join(parts[index:])}, {' '.join(parts[:index])}"


def bibtex(connection: sqlite3.Connection, include_files: bool = False) -> str:
    entries = []
    for row in connection.execute("SELECT * FROM papers ORDER BY citation_key"):
        venue = row["venue"] or ""
        arxiv = row["arxiv"] or ((row["doi"] or "").split("arxiv.", 1)[1] if (row["doi"] or "").startswith("10.48550/arxiv.") else "")
        if CONFERENCE_HINT.search(venue) and not ARTICLE_HINT.search(venue):
            kind, venue_field = "inproceedings", "booktitle"
        elif venue or row["doi"]:
            kind, venue_field = "article", "journal"
        else:
            kind, venue_field = "misc", "howpublished"
        authors = json.loads(row["authors"] or "[]")
        fields: list[tuple[str, Any]] = [
            ("title", bib_value(row["title"])), ("author", " and ".join(bib_value(bib_author(a)) for a in authors)),
            ("year", row["year"]), (venue_field, bib_value(venue)), ("publisher", bib_value(row["publisher"])),
            ("doi", row["doi"]), ("url", row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")),
        ]
        if arxiv:
            fields += [("eprint", arxiv), ("archiveprefix", "arXiv")]
        if row["pmid"]:
            fields.append(("pmid", row["pmid"]))
        if row["abstract"]:
            fields.append(("abstract", bib_value(row["abstract"])))
        if include_files:
            files = [r["local_file"] for r in connection.execute("SELECT local_file FROM paper_files WHERE paper_id=? ORDER BY role, local_file", (row["id"],))]
            if files:
                fields.append(("file", ";".join(files)))
        body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value not in (None, ""))
        entries.append(f"@{kind}{{{row['citation_key']},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")

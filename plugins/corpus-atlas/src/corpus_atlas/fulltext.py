"""Legal open-access full-text resolution, batch retrieval, supplements,
preprint publication checks, integrity checks, and citation expansion.

Every network call goes through :func:`http_get` so tests can mock it.
Nothing here bypasses paywalls, logins, or CAPTCHAs: a candidate that does not
return the expected file type directly is recorded as a failure and the next
legal candidate is tried.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import ssl
import tarfile
import threading
import time
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scholarly import current_time, normalize_arxiv, normalize_doi, normalized_title
from .urls import sanitize_obj, sanitize_url

PROJECT_URL = "https://github.com/Li-Hongmin/corpus-atlas"
MAX_BYTES = 200 * 1024 * 1024
PDF_ACCEPT = "application/pdf,*/*;q=0.8"


def user_agent(email: str | None = None, api: bool = True) -> str:
    """API calls identify the project and contact (polite pools). File downloads use a
    plain tool UA: publisher WAFs (e.g. MDPI) 403 UAs carrying URLs/mailto, and HAL
    serves an HTML challenge to browser-like "Mozilla" UAs."""
    if not api:
        return "CorpusAtlas/0.3 (open-access retrieval)"
    contact = f"; mailto:{email}" if email else ""
    return f"CorpusAtlas/0.3 (+{PROJECT_URL}{contact})"


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    url: str
    error: str | None = None


CA_PATHS = ("/etc/ssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem", "/usr/local/etc/openssl@3/cert.pem",
            "/opt/homebrew/etc/ca-certificates/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")
_SSL_CONTEXT: ssl.SSLContext | None = None
_HOST_LOCK = threading.Lock()
_HOST_NEXT: dict[str, float] = {}
HOST_INTERVAL = float(os.environ.get("CORPUS_ATLAS_HOST_INTERVAL", "0.2"))
RETRY_STATUS = {429, 500, 502, 503, 504}


def ssl_context() -> ssl.SSLContext:
    """Default trust store, plus system bundles (fixes CERTIFICATE_VERIFY_FAILED on python.org builds)."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        context = ssl.create_default_context()
        explicit = os.environ.get("SSL_CERT_FILE")
        for path in ([explicit] if explicit else []) + list(CA_PATHS):
            if path and os.path.isfile(path):
                try:
                    context.load_verify_locations(cafile=path)
                except (ssl.SSLError, OSError):
                    continue
        _SSL_CONTEXT = context
    return _SSL_CONTEXT


def throttle(url: str) -> None:
    """Per-host politeness spacing shared across worker threads."""
    host = urllib.parse.urlparse(url).netloc.lower()
    with _HOST_LOCK:
        now = time.monotonic()
        start = max(now, _HOST_NEXT.get(host, 0.0))
        _HOST_NEXT[host] = start + HOST_INTERVAL
    if start > now:
        time.sleep(start - now)


def http_get(url: str, accept: str = "application/json", email: str | None = None, timeout: int = 60,
             attempts: int = 3, max_delay: float = 20.0) -> Response:
    """Fetch a URL with a descriptive UA, retry/backoff on 429/5xx and transient
    network errors, and per-host rate limiting. Never raises for HTTP/network errors."""
    api = accept.split(",")[0].strip() in {"application/json", "application/xml", "application/atom+xml"}
    headers = {"User-Agent": user_agent(email, api), "Accept": accept}
    result = Response(0, "", b"", url, "not attempted")
    for attempt in range(max(1, attempts)):
        throttle(url)
        request = urllib.request.Request(url, headers=headers)
        delay = min(2 ** attempt, max_delay)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    return Response(0, "", b"", url, "response exceeds size limit")
                ctype = response.headers.get_content_type() if response.headers else ""
                return Response(getattr(response, "status", 200) or 200, ctype, body, response.geturl() or url)
        except urllib.error.HTTPError as error:
            result = Response(error.code, "", b"", url, f"HTTP {error.code}")
            if error.code not in RETRY_STATUS:
                return result
            retry_after = error.headers.get("Retry-After") if error.headers else None
            if retry_after and retry_after.strip().isdigit():
                delay = min(float(retry_after), max_delay)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            reason = getattr(error, "reason", error)
            result = Response(0, "", b"", url, str(reason))
            if isinstance(error, ValueError) or isinstance(reason, ssl.SSLError):
                return result
        if attempt < attempts - 1:
            time.sleep(delay)
    return result


def get_json(url: str, email: str | None = None) -> Any:
    response = http_get(url, "application/json", email)
    if response.status != 200:
        return None
    try:
        return json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------- file types
OFFICE = {"word/": ".docx", "xl/": ".xlsx", "ppt/": ".pptx"}


def infer_extension(body: bytes, content_type: str = "", filename: str = "") -> str:
    """Infer a file extension from magic bytes, then content type, then name."""
    head = body[:512]
    if head.startswith(b"%PDF-"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(io.BytesIO(body)).namelist()
            for prefix, suffix in OFFICE.items():
                if any(name.startswith(prefix) for name in names):
                    return suffix
        except zipfile.BadZipFile:
            pass
        named = Path(urllib.parse.urlparse(filename).path).suffix.lower()
        return named if named in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub"} else ".zip"
    if head.startswith(b"\x1f\x8b"):
        return ".gz"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return Path(filename).suffix.lower() if Path(filename).suffix.lower() in {".doc", ".xls", ".ppt"} else ".doc"
    stripped = head.lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html")):
        return ".html"
    if stripped.startswith(b"<?xml"):
        return ".xml"
    guessed = mimetypes.guess_extension(content_type or "") if content_type and content_type != "application/octet-stream" else None
    if guessed:
        return {".htm": ".html", ".jpe": ".jpg"}.get(guessed, guessed)
    suffix = Path(urllib.parse.urlparse(filename).path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix or ""):
        return suffix
    return ".bin"


def with_extension(name: str, extension: str) -> str:
    stem = Path(name).stem if Path(name).suffix.lower() == extension else name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "file"
    return stem + extension


# ---------------------------------------------------------------- candidates
def openalex_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    doi = paper.get("doi")
    work_id = paper.get("openalex_id")
    if doi:
        url = f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi, safe='/')}"
    elif work_id:
        url = f"https://api.openalex.org/works/{str(work_id).rsplit('/', 1)[-1]}"
    else:
        return []
    if email:
        url += f"?mailto={urllib.parse.quote(email)}"
    work = _OPENALEX_CACHE[doi] if doi and doi in _OPENALEX_CACHE else (get_json(url, email) or {})
    locations = [work.get("best_oa_location")] + list(work.get("locations") or [])
    result = []
    for location in locations:
        if not location or not location.get("pdf_url"):
            continue
        if location.get("is_oa") is False:
            continue
        source = (location.get("source") or {}).get("display_name") or "OpenAlex location"
        result.append({"url": location["pdf_url"], "source": f"OpenAlex: {source}",
                       "license": location.get("license") or "open-access (license unspecified)",
                       "version": location.get("version") or ""})
    return result


def unpaywall_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    if not (paper.get("doi") and email):
        return []
    data = get_json(f"https://api.unpaywall.org/v2/{urllib.parse.quote(paper['doi'], safe='/')}?email={urllib.parse.quote(email)}", email) or {}
    locations = [data.get("best_oa_location")] + list(data.get("oa_locations") or [])
    return [{"url": loc["url_for_pdf"], "source": f"Unpaywall: {loc.get('host_type') or 'location'}",
             "license": loc.get("license") or "open-access (license unspecified)", "version": loc.get("version") or ""}
            for loc in locations if loc and loc.get("url_for_pdf")]


_EPMC_CACHE: dict[str, Any] = {}
_OPENALEX_CACHE: dict[str, Any] = {}


def prefetch_metadata(dois: list[str], email: str | None) -> None:
    """Batch OpenAlex (40 DOIs/request) and Europe PMC (50 DOIs/request) lookups so a
    batch run makes O(n/40) metadata requests instead of O(n). Misses fall back to
    per-paper lookups."""
    dois = [d for d in dict.fromkeys(dois) if d]
    for start in range(0, len(dois), 40):
        group = [d for d in dois[start:start + 40] if d not in _OPENALEX_CACHE]
        if not group:
            continue
        params = {"filter": "doi:" + "|".join(group), "per-page": 50,
                  "select": "doi,open_access,best_oa_location,primary_location,locations"}
        if email:
            params["mailto"] = email
        data = get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params), email)
        for work in (data or {}).get("results", []):
            _OPENALEX_CACHE[normalize_doi(work.get("doi")) or ""] = work
        if data is not None:
            for d in group:
                _OPENALEX_CACHE.setdefault(d, {})
    for start in range(0, len(dois), 50):
        group = [d for d in dois[start:start + 50] if d not in _EPMC_CACHE]
        if not group:
            continue
        query = urllib.parse.urlencode({"query": " OR ".join(f'DOI:"{d}"' for d in group), "format": "json",
                                        "resultType": "core", "pageSize": 100})
        data = get_json(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{query}", email)
        if data is None:
            continue
        for result in (data.get("resultList") or {}).get("result", []):
            doi = normalize_doi(result.get("doi"))
            if doi in group and result.get("pmcid") and not _EPMC_CACHE.get(doi):
                _EPMC_CACHE[doi] = result
        for d in group:
            _EPMC_CACHE.setdefault(d, None)


def europe_pmc_lookup(doi: str, email: str | None) -> dict[str, Any] | None:
    if doi in _EPMC_CACHE:
        return _EPMC_CACHE[doi]
    _EPMC_CACHE[doi] = _europe_pmc_lookup(doi, email)
    return _EPMC_CACHE[doi]


def _europe_pmc_lookup(doi: str, email: str | None) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"query": f'DOI:"{doi}"', "format": "json", "resultType": "core", "pageSize": 1})
    data = get_json(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{query}", email) or {}
    for result in (data.get("resultList") or {}).get("result", []):
        if normalize_doi(result.get("doi")) == doi and result.get("pmcid"):
            return result
    return None


def europe_pmc_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    if not paper.get("doi"):
        return []
    result = europe_pmc_lookup(paper["doi"], email)
    if not result or result.get("isOpenAccess") != "Y":
        return []
    pmcid = result["pmcid"]
    license_name = result.get("license") or "PMC open-access subset"
    return [{"url": f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf",
             "source": f"Europe PMC: {pmcid}", "license": license_name},
            {"url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/", "source": f"PMC: {pmcid}", "license": license_name}]


def arxiv_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    identifier = paper.get("arxiv")
    doi = paper.get("doi") or ""
    if not identifier and doi.startswith("10.48550/arxiv."):
        identifier = doi.split("arxiv.", 1)[1]
    if not identifier:
        return []
    return [{"url": f"https://arxiv.org/pdf/{identifier}", "source": f"arXiv: {identifier}",
             "license": "arXiv distribution license (or as stated on the record)"}]


def biorxiv_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    doi = paper.get("doi") or ""
    if not doi.startswith("10.1101/"):
        return []
    for server in ("biorxiv", "medrxiv"):
        data = get_json(f"https://api.biorxiv.org/details/{server}/{doi}", email) or {}
        versions = data.get("collection") or []
        if versions:
            latest = versions[-1]
            return [{"url": f"https://www.{server}.org/content/{doi}v{latest.get('version', 1)}.full.pdf",
                     "source": f"{server}: {doi}", "license": latest.get("license") or f"{server} license"}]
    return []


def record_link_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    """OA PDF URLs carried by the imported record/matrix (open_access_pdf column)."""
    return [{"url": link["url"], "source": link.get("source") or "record OA location",
             "license": link.get("license") or "oa-unspecified"} for link in paper.get("_links") or []]


def springer_oa_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    doi = paper.get("doi") or ""
    if not doi.startswith(("10.1186/", "10.1007/", "10.1038/s41")):
        return []
    return [{"url": f"https://link.springer.com/content/pdf/{doi}.pdf", "source": "BMC/Springer OA",
             "license": "open-access (license as stated by publisher)"}]


def ncbi_package_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    """PMC OA-subset package (.tar.gz) via the NCBI OA web service; the largest PDF is extracted."""
    if not paper.get("doi"):
        return []
    result = europe_pmc_lookup(paper["doi"], email)
    if not result or result.get("isOpenAccess") != "Y":
        return []
    pmcid = result["pmcid"]
    response = http_get(f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}", "application/xml", email)
    if response.status != 200:
        return []
    try:
        record = ET.fromstring(response.body).find(".//record")
    except ET.ParseError:
        return []
    if record is None:
        return []
    output = []
    for link in record.findall("link"):
        href = link.attrib.get("href") or ""
        if link.attrib.get("format") in {"pdf", "tgz"} and href:
            output.append({"url": href.replace("ftp://ftp.ncbi.nlm.nih.gov/", "https://ftp.ncbi.nlm.nih.gov/"),
                           "source": f"NCBI OA package: {pmcid}", "license": record.attrib.get("license") or "PMC open-access subset"})
    return sorted(output, key=lambda c: not c["url"].endswith(".pdf"))


RESOLVERS = (record_link_candidates, openalex_candidates, unpaywall_candidates, europe_pmc_candidates, ncbi_package_candidates,
             arxiv_candidates, biorxiv_candidates, springer_oa_candidates)


def pdf_from_package(body: bytes) -> bytes | None:
    """Extract the largest PDF from a gzip tarball (PMC OA package)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            members = [m for m in archive.getmembers() if m.isfile() and m.name.lower().endswith(".pdf") and m.size <= MAX_BYTES]
            if not members:
                return None
            handle = archive.extractfile(max(members, key=lambda m: m.size))
            data = handle.read() if handle else b""
            return data if data.startswith(b"%PDF-") else None
    except (tarfile.TarError, OSError, EOFError):
        return None


def resolve_candidates(paper: dict[str, Any], email: str | None = None) -> list[dict[str, str]]:
    return list(iter_candidates(paper, email))


def iter_candidates(paper: dict[str, Any], email: str | None = None):
    """Yield deduplicated candidates lazily: later sources are queried only if earlier ones fail."""
    seen: set[str] = set()
    for resolver in RESOLVERS:
        try:
            found = resolver(paper, email)
        except Exception:  # one broken source must not stop the others
            found = []
        for candidate in found:
            if candidate["url"].startswith("http://"):
                candidate["url"] = "https://" + candidate["url"][len("http://"):]
            if candidate["url"] not in seen:
                seen.add(candidate["url"])
                yield candidate


def fetch_fulltext(paper: dict[str, Any], email: str | None = None, accept=None) -> dict[str, Any]:
    """Try each legal candidate in order; return the first real PDF or all failures.
    ``accept(candidate)`` may veto candidates (e.g. preprint servers)."""
    attempts: list[dict[str, str]] = []
    candidates = []
    for candidate in iter_candidates(paper, email):
        if accept is not None and not accept(candidate):
            continue
        candidates.append(candidate)
        url = candidate["url"]
        if url.startswith("http://"):
            url = candidate["url"] = "https://" + url[len("http://"):]
        response = http_get(url, PDF_ACCEPT, email, timeout=90)
        if response.status == 200 and response.body.startswith(b"\x1f\x8b"):
            extracted = pdf_from_package(response.body)
            if extracted:
                return {"status": "downloaded", "candidate": candidate, "body": extracted,
                        "content_type": "application/pdf (from OA package)", "attempts": attempts}
        if response.status == 200 and response.body.startswith(b"%PDF-"):
            return {"status": "downloaded", "candidate": candidate, "body": response.body,
                    "content_type": response.content_type or "application/pdf", "attempts": attempts}
        if response.status == 200:
            reason = f"not a PDF ({response.content_type or infer_extension(response.body)}); likely landing page or access wall"
        else:
            reason = response.error or f"HTTP {response.status}"
        attempts.append({"url": candidate["url"], "source": candidate["source"], "reason": reason})
    status = "no-oa-candidate" if not candidates else "failed"
    return {"status": status, "attempts": attempts}


# ---------------------------------------------------------------- storage
def store_file(connection: sqlite3.Connection, root: Path, paper: sqlite3.Row | dict, role: str, body: bytes,
               url: str, source: str, license_name: str, content_type: str, original: str = "") -> dict[str, Any]:
    extension = infer_extension(body, content_type, original or url)
    url = sanitize_url(url)
    base = original or Path(urllib.parse.urlparse(url).path).name or role
    name = with_extension(f"{paper['citation_key']}-{role}-{Path(base).stem or role}", extension)
    destination = root / "attachments" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    existing = connection.execute("SELECT local_file FROM paper_files WHERE paper_id=? AND role=? AND sha256=?",
                                  (paper["id"], role, digest)).fetchone()
    if existing:
        return {"file": existing["local_file"], "sha256": digest, "bytes": len(body), "duplicate": True}
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        destination = destination.with_name(f"{destination.stem}-{digest[:8]}{extension}")
    destination.write_bytes(body)
    relative = destination.relative_to(root).as_posix()
    connection.execute("INSERT OR IGNORE INTO paper_files VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"fil_{uuid.uuid4().hex[:12]}", paper["id"], role, relative, url, source, license_name,
                        original or Path(urllib.parse.urlparse(url).path).name, content_type, digest, len(body), current_time()))
    if role == "main":
        connection.execute("UPDATE papers SET fulltext_status='local-fulltext',updated_at=? WHERE id=?", (current_time(), paper["id"]))
    return {"file": relative, "sha256": digest, "bytes": len(body)}


def resolve_email(value: str | None) -> str | None:
    return value or os.environ.get("CORPUS_ATLAS_EMAIL") or os.environ.get("UNPAYWALL_EMAIL") or None


def validate_main_payload(body: bytes) -> tuple[bytes | None, str, str]:
    """Same acceptance rule as :func:`fetch_fulltext`: a PDF, or a PDF inside an OA package."""
    if len(body) > MAX_BYTES:
        return None, "", "response exceeds size limit"
    if body.startswith(b"\x1f\x8b"):
        extracted = pdf_from_package(body)
        if extracted:
            return extracted, "application/pdf (from OA package)", ""
    if body.startswith(b"%PDF-"):
        return body, "application/pdf", ""
    return None, "", f"not a PDF ({infer_extension(body)}); likely landing page or access wall"


def _python_outcomes(papers: list[dict[str, Any]], email: str | None, max_workers: int):
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(fetch_fulltext, paper, email): paper for paper in papers}
        for future in as_completed(futures):
            paper = futures[future]
            try:
                outcome = future.result()
            except Exception as error:  # keep the batch alive
                outcome = {"status": "failed", "attempts": [{"url": "", "source": "", "reason": repr(error)}]}
            outcome["transport"] = "python"
            yield paper, outcome


def _aria2_outcomes(papers: list[dict[str, Any]], root: Path, email: str | None, max_workers: int,
                    connections: int) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Resolve candidates, then download candidate pass N for every unresolved paper in one
    aria2c run, validating each payload. Returns (outcomes, papers needing Python fallback)."""
    from . import transport
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        resolved = dict(zip([p["id"] for p in papers], pool.map(lambda p: resolve_candidates(p, email), papers)))
    by_id = {paper["id"]: paper for paper in papers}
    attempts: dict[str, list[dict[str, str]]] = {pid: [] for pid in by_id}
    outcomes: dict[str, dict[str, Any]] = {}
    staging = root / ".staging" / "aria2"
    deferred: set[str] = set()
    for index in range(max((len(c) for c in resolved.values()), default=0)):
        jobs = []
        for pid in by_id:
            if pid in outcomes or pid in deferred or index >= len(resolved[pid]):
                continue
            if transport.python_only(resolved[pid][index]["url"]):
                deferred.add(pid)  # keep candidate order: the Python transport continues from here
                continue
            jobs.append({"id": pid, "url": resolved[pid][index]["url"]})
        if not jobs:
            continue
        results = transport.aria2_download(jobs, staging, user_agent=user_agent(email, api=False), accept=PDF_ACCEPT,
                                           max_concurrent=max(1, max_workers) * 2, connections_per_server=connections,
                                           split=connections)
        for job in jobs:
            pid, candidate, result = job["id"], resolved[job["id"]][index], results[job["id"]]
            if "error" in result:
                attempts[pid].append({"url": candidate["url"], "source": candidate["source"], "reason": "aria2: " + result["error"]})
                if not result["path"].with_name(result["path"].name + ".aria2").exists():
                    transport.cleanup(result)
                continue
            body = result["path"].read_bytes()
            transport.cleanup(result)
            payload, ctype, reason = validate_main_payload(body)
            if payload is None:
                attempts[pid].append({"url": candidate["url"], "source": candidate["source"], "reason": reason})
                continue
            outcomes[pid] = {"status": "downloaded", "candidate": candidate, "body": payload, "content_type": ctype,
                             "attempts": attempts[pid], "transport": "aria2"}
    fallback = []
    for pid, paper in by_id.items():
        if pid in outcomes:
            continue
        if not resolved[pid]:
            outcomes[pid] = {"status": "no-oa-candidate", "attempts": [], "transport": "aria2"}
        else:
            paper["_aria2_attempts"] = attempts[pid]
            fallback.append(paper)
    return outcomes, fallback


def retrieve_all(connection: sqlite3.Connection, root: Path, email: str | None, max_workers: int,
                 limit: int | None = None, retry_failed: bool = True, transport: str = "python",
                 connections: int = 2, python_fallback: bool = True) -> dict[str, Any]:
    query = "SELECT * FROM papers WHERE fulltext_status!='local-fulltext' ORDER BY citation_key"
    papers = [dict(row) for row in connection.execute(query)]
    from .library import record_links
    for paper in papers:
        paper["_links"] = record_links(connection, paper["id"], "pdf")
    if not retry_failed:
        papers = [p for p in papers if p["fulltext_status"] != "oa-retrieval-failed"]
    if limit:
        papers = papers[:limit]
    prefetch_metadata([p["doi"] for p in papers if p.get("doi")], email)
    already = connection.execute("SELECT COUNT(*) FROM papers WHERE fulltext_status='local-fulltext'").fetchone()[0]
    results = []
    from . import transport as tr
    chosen = tr.choose(transport)
    notes: list[str] = []

    def outcomes():
        if chosen == "aria2":
            try:
                done_map, fallback = _aria2_outcomes(papers, root, email, max_workers, connections)
            except tr.Aria2Unavailable as error:
                notes.append(f"{error}; fell back to python transport")
                yield from _python_outcomes(papers, email, max_workers)
                return
            by_id = {p["id"]: p for p in papers}
            for pid, outcome in done_map.items():
                yield by_id[pid], outcome
            if not python_fallback:
                for paper in fallback:
                    yield paper, {"status": "failed", "attempts": paper.pop("_aria2_attempts"), "transport": "aria2"}
                return
            for paper, outcome in _python_outcomes(fallback, email, max_workers):
                earlier = paper.pop("_aria2_attempts", [])
                outcome["attempts"] = earlier + outcome["attempts"]
                outcome["transport"] = "aria2+python-fallback" if outcome["status"] == "downloaded" else "aria2"
                yield paper, outcome
        else:
            yield from _python_outcomes(papers, email, max_workers)

    for done, (paper, outcome) in enumerate(outcomes(), 1):
        row = {"citation_key": paper["citation_key"], "doi": paper["doi"], "title": paper["title"], "status": outcome["status"],
               "attempts": outcome["attempts"], "transport": outcome.get("transport", chosen)}
        if outcome["status"] == "downloaded":
            candidate = outcome["candidate"]
            stored = store_file(connection, root, paper, "main", outcome["body"], candidate["url"], candidate["source"],
                                candidate["license"], outcome["content_type"])
            row.update(file=stored["file"], source=candidate["source"], license=candidate["license"])
        else:
            connection.execute("UPDATE papers SET fulltext_status=?,updated_at=? WHERE id=?",
                               ("oa-retrieval-failed" if outcome["status"] == "failed" else "no-oa-candidate", current_time(), paper["id"]))
        connection.commit()  # resumable: every paper is persisted as it finishes
        results.append(row)
        print(f"[{done}/{len(papers)}] {row['status']}: {paper['citation_key']}", file=__import__("sys").stderr)
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    stamp = current_time().replace(":", "").replace("+", "Z")[:17]
    report_path = reports / f"retrieve-all-{stamp}.json"
    failures_path = reports / "fulltext-failures.csv"
    results = sanitize_obj(results)
    failures = [row for row in results if row["status"] != "downloaded"]
    with failures_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["citation_key", "doi", "title", "status", "landing_url", "tried", "last_reason"])
        for row in failures:
            landing = f"https://doi.org/{row['doi']}" if row["doi"] else ""
            writer.writerow([row["citation_key"], row["doi"] or "", row["title"], row["status"], landing,
                             " | ".join(a["url"] for a in row["attempts"]),
                             row["attempts"][-1]["reason"] if row["attempts"] else "no legal OA location known"])
    from .library import write_browser_queue
    if failures:
        write_browser_queue(root, [{"citation_key": row["citation_key"], "doi": row["doi"] or "", "title": row["title"], "kind": "main",
                                    "reason": row["attempts"][-1]["reason"] if row["attempts"] else "no legal OA location known",
                                    "landing_url": f"https://doi.org/{row['doi']}" if row["doi"] else ""} for row in failures])
    summary = {"attempted": len(papers), "downloaded": sum(r["status"] == "downloaded" for r in results),
               "failed": sum(r["status"] == "failed" for r in results),
               "no_oa_candidate": sum(r["status"] == "no-oa-candidate" for r in results),
               "already_local": already, "transport": chosen, **({"notes": notes} if notes else {}),
               "report": report_path.relative_to(root).as_posix(),
               "failures_csv": failures_path.relative_to(root).as_posix()}
    report_path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


# ---------------------------------------------------------------- supplements
SUPPLEMENT_MARKERS = re.compile(r"(supp|suppl|supplement|additional|appendix|si[_-]|mmc\d|esm|moesm|dataset|table_?s\d)", re.I)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".svg", ".webp"}
MAX_MEMBER_BYTES = 500 * 1024 * 1024


def is_supplement_member(member: zipfile.ZipInfo) -> bool:
    """True supplementary files only: article figure renders (e.g. *_Fig3_HTML.jpg) are excluded."""
    if member.is_dir() or member.file_size <= 0 or member.file_size > MAX_MEMBER_BYTES:
        return False
    name = Path(member.filename).name
    if Path(name).suffix.lower() in IMAGE_SUFFIXES and not SUPPLEMENT_MARKERS.search(name):
        return False
    if re.search(r"_HTML\.", name) and not SUPPLEMENT_MARKERS.search(name.replace("_HTML", "")):
        return False
    return True


def supplement_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [member for member in archive.infolist() if is_supplement_member(member)]


def _fetch_supplement(url: str, source: str, license_name: str, filename: str, email: str | None) -> dict[str, Any]:
    from .library import acceptable_supplement
    response = http_get(url, "*/*", email, timeout=120)
    if response.status != 200:
        return {"url": url, "error": response.error or f"HTTP {response.status}"}
    name = filename or Path(urllib.parse.urlparse(response.url or url).path).name
    reason = acceptable_supplement(response.body, response.content_type, name)
    if reason:
        return {"url": url, "error": reason}
    return {"body": response.body, "url": url, "source": source, "license": license_name, "content_type": response.content_type, "name": name}


def _defer_supplement(url: str, source: str, license_name: str, filename: str, email: str | None) -> dict[str, Any]:
    return {"deferred": True, "url": url, "source": source, "license": license_name, "name": filename}


def _supplement_job(paper: dict[str, Any], listed: list[dict[str, str]], email: str | None, apply: bool, publisher_pages: bool,
                    fetch=None) -> dict[str, Any]:
    """Network-only work for one paper (thread-safe); storage happens in the caller.
    ``fetch`` defaults to the Python fetcher; the aria2 transport defers file URLs."""
    fetch = fetch or _fetch_supplement
    from .library import publisher_supplement_links
    job: dict[str, Any] = {"citation_key": paper["citation_key"], "items": [], "errors": [], "sources": []}
    for link in listed:
        job["sources"].append(link.get("source") or "record")
        if apply:
            got = fetch(link["url"], link.get("source") or "record", link.get("license") or "oa-unspecified", link.get("filename") or "", email)
            (job["errors"] if "error" in got else job["items"]).append(got)
    result = europe_pmc_lookup(paper["doi"], email) if paper.get("doi") else None
    if result and result.get("isOpenAccess") == "Y":
        pmcid = result["pmcid"]
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"
        job.update(pmcid=pmcid, source_url=url)
        job["sources"].append(f"Europe PMC {pmcid}")
        if apply:
            response = http_get(url, "application/zip", email, timeout=120)
            if response.status == 200 and response.body.startswith(b"PK"):
                license_name = result.get("license") or "Europe PMC open-access subset"
                try:
                    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
                        for member in supplement_members(archive):
                            job["items"].append({"body": archive.read(member), "url": f"{url}#{member.filename}",
                                                 "source": f"Europe PMC supplementary files: {pmcid}", "license": license_name,
                                                 "content_type": mimetypes.guess_type(member.filename)[0] or "application/octet-stream",
                                                 "name": Path(member.filename).name})
                except zipfile.BadZipFile:
                    job["errors"].append({"url": url, "error": "corrupt supplementary ZIP"})
            elif response.status not in (200, 404):
                job["errors"].append({"url": url, "error": response.error or f"HTTP {response.status}"})
    if publisher_pages and paper.get("doi") and not job["items"] and (apply or not job["sources"]):
        page_url, links, label = publisher_supplement_links(paper["doi"], email)
        if links:
            job["sources"].append(label)
            job["publisher_links"] = links
            if apply:
                for link in links:
                    got = fetch(link, f"{label}: {page_url}", "publisher article terms", "", email)
                    (job["errors"] if "error" in got else job["items"]).append(got)
    return job


def _resolve_deferred(jobs: list[dict[str, Any]], root: Path, email: str | None, max_workers: int, connections: int,
                      python_fallback: bool, notes: list[str]) -> None:
    """Bulk-download deferred supplement URLs with aria2, validate like the Python path,
    and fall back to Python per file (or entirely, if aria2c cannot run)."""
    from . import transport as tr
    from .library import acceptable_supplement
    for job in jobs:
        for index, item in enumerate(job["items"]):
            if item.get("deferred") and tr.python_only(item["url"]):
                job["items"][index] = _fetch_supplement(item["url"], item["source"], item["license"], item["name"], email)
    pending = [(job, index) for job in jobs for index, item in enumerate(job["items"]) if item.get("deferred")]
    if not pending:
        for job in jobs:
            job["errors"] += [item for item in job["items"] if "error" in item]
            job["items"] = [item for item in job["items"] if "error" not in item]
        return
    work = [{"id": str(n), "url": job["items"][index]["url"]} for n, (job, index) in enumerate(pending)]
    try:
        results = tr.aria2_download(work, root / ".staging" / "aria2", user_agent=user_agent(email, api=False), accept="*/*",
                                    max_concurrent=max(1, max_workers) * 2, connections_per_server=connections, split=connections)
    except tr.Aria2Unavailable as error:
        notes.append(f"{error}; fell back to python transport")
        results = {str(n): {"error": "aria2 unavailable"} for n in range(len(pending))}
    for n, (job, index) in enumerate(pending):
        item, result = job["items"][index], results[str(n)]
        replacement: dict[str, Any] | None = None
        if "error" not in result:
            body = result["path"].read_bytes()
            tr.cleanup(result)
            name = item["name"] or Path(urllib.parse.urlparse(item["url"]).path).name
            ctype = mimetypes.guess_type(name)[0] or ""
            reason = acceptable_supplement(body, ctype, name)
            replacement = {"error": reason, "url": item["url"]} if reason else {
                "body": body, "url": item["url"], "source": item["source"], "license": item["license"], "content_type": ctype, "name": name}
        if replacement is None and python_fallback:  # download failed; validation rejects are final
            replacement = _fetch_supplement(item["url"], item["source"], item["license"], item["name"], email)
        if replacement is None:
            replacement = {"url": item["url"], "error": "aria2: " + result["error"]}
        job["items"][index] = replacement
    for job in jobs:
        job["errors"] += [item for item in job["items"] if "error" in item]
        job["items"] = [item for item in job["items"] if "error" not in item]


def supplements(connection: sqlite3.Connection, root: Path, email: str | None, apply: bool, paper_ref: str | None,
                publisher_pages: bool = True, max_workers: int = 4, transport: str = "python", connections: int = 2,
                python_fallback: bool = True) -> list[dict[str, Any]]:
    """Matrix/record supplement URLs, then Europe PMC supplementary ZIP, then publisher-page
    rules (Springer Nature/BMC ESM, Elsevier mmc, ACS/Wiley/PLOS/Frontiers/MDPI/RSC SI).
    Anything still unresolved is written to reports/browser-queue.csv."""
    from .library import record_links, write_browser_queue
    sql = "SELECT * FROM papers" + (" WHERE (id=? OR citation_key=? OR doi=?)" if paper_ref else "") + " ORDER BY citation_key"
    rows = [dict(r) for r in connection.execute(sql, (paper_ref, paper_ref, normalize_doi(paper_ref)) if paper_ref else ())]
    links = {paper["id"]: record_links(connection, paper["id"], "supplement") for paper in rows}
    prefetch_metadata([p["doi"] for p in rows if p.get("doi")], email)
    by_key = {paper["citation_key"]: paper for paper in rows}
    report, queue = [], []
    from . import transport as tr
    chosen = tr.choose(transport) if apply else "python"
    fetch = _defer_supplement if chosen == "aria2" else None
    notes: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [pool.submit(_supplement_job, paper, links[paper["id"]], email, apply, publisher_pages, fetch) for paper in rows]
        jobs = [future.result() for future in as_completed(futures)]
    if chosen == "aria2":
        _resolve_deferred(jobs, root, email, max_workers, connections, python_fallback, notes)
    for job in jobs:
        paper = by_key[job["citation_key"]]
        entry = {k: v for k, v in job.items() if k != "items"}
        entry["errors"] = [{k: v for k, v in e.items() if k != "body"} for e in entry["errors"]]
        if apply:
            entry["transport"] = chosen
        entry["files"] = [store_file(connection, root, paper, "supplement", item["body"], item["url"], item["source"], item["license"],
                                     item["content_type"], item["name"]) for item in job["items"]]
        if entry["files"]:
            entry["status"] = "downloaded"
        elif entry["sources"] and not apply:
            entry["status"] = "available-preview"
        elif entry["errors"]:
            entry["status"] = "failed"
        else:
            entry["status"] = "none-found"
        if apply:
            connection.commit()
            if not entry["files"]:
                queue.append({"citation_key": paper["citation_key"], "doi": paper.get("doi") or "", "title": paper["title"], "kind": "supplement",
                              "reason": entry["status"] + ("; " + entry["errors"][-1]["error"] if entry["errors"] else ""),
                              "landing_url": f"https://doi.org/{paper['doi']}" if paper.get("doi") else (paper.get("url") or "")})
        report.append(entry)
    if apply and queue:
        write_browser_queue(root, queue)
    return sorted(report, key=lambda e: e["citation_key"])


# ---------------------------------------------------------------- publications
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def is_preprint(paper: dict[str, Any]) -> bool:
    doi = paper.get("doi") or ""
    return bool(paper.get("arxiv")) or doi.startswith(("10.48550/", "10.1101/")) or bool(re.search(r"arxiv|rxiv|preprint", paper.get("venue") or "", re.I))


def published_version(paper: dict[str, Any], email: str | None) -> dict[str, Any] | None:
    doi = paper.get("doi") or ""
    arxiv = paper.get("arxiv") or (doi.split("arxiv.", 1)[1] if doi.startswith("10.48550/arxiv.") else None)
    if arxiv:
        response = http_get(f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(arxiv)}", "application/atom+xml", email)
        if response.status == 200:
            try:
                entry = ET.fromstring(response.body).find("a:entry", ATOM)
            except ET.ParseError:
                entry = None
            if entry is not None and entry.findtext("arxiv:doi", namespaces=ATOM):
                return {"doi": normalize_doi(entry.findtext("arxiv:doi", namespaces=ATOM)),
                        "venue": entry.findtext("arxiv:journal_ref", namespaces=ATOM), "evidence": "arXiv record DOI"}
    if doi.startswith("10.1101/"):
        for server in ("biorxiv", "medrxiv"):
            data = get_json(f"https://api.biorxiv.org/pubs/{server}/{doi}", email) or {}
            for item in data.get("collection") or []:
                if item.get("published_doi"):
                    return {"doi": normalize_doi(item["published_doi"]), "venue": item.get("published_journal"),
                            "evidence": f"{server} publication link"}
    found = crossref_published(paper, email)
    if found:
        return found
    query = urllib.parse.urlencode({"search": paper.get("title") or "", "per-page": 5})
    data = get_json(f"https://api.openalex.org/works?{query}", email) or {}
    target = normalized_title(paper.get("title"))
    for work in data.get("results", []):
        work_doi = normalize_doi(work.get("doi")) or ""
        if (normalized_title(work.get("title")) == target and work_doi and work.get("type") != "preprint"
                and not work_doi.startswith(("10.48550/", "10.1101/")) and work_doi != doi):
            return {"doi": work_doi, "venue": ((work.get("primary_location") or {}).get("source") or {}).get("display_name"),
                    "evidence": "OpenAlex exact-title non-preprint match"}
    return None


def _family(name: str) -> str:
    name = name.strip()
    return normalized_title(name.split(",", 1)[0] if "," in name else (name.split() or [""])[-1])


def crossref_published(paper: dict[str, Any], email: str | None) -> dict[str, Any] | None:
    """Crossref bibliographic search; accept only same normalized title, compatible
    first-author family name, and publication year within [preprint year, +3]."""
    title = paper.get("title") or ""
    if not title:
        return None
    query = urllib.parse.urlencode({"query.bibliographic": title, "rows": 5, **({"mailto": email} if email else {})})
    data = get_json(f"https://api.crossref.org/works?{query}", email) or {}
    authors = json.loads(paper.get("authors") or "[]") if isinstance(paper.get("authors"), str) else (paper.get("authors") or [])
    wanted = _family(authors[0]) if authors else ""
    for item in (data.get("message") or {}).get("items", []):
        doi = normalize_doi(item.get("DOI")) or ""
        if not doi or doi == paper.get("doi") or doi.startswith(("10.48550/", "10.1101/")) or item.get("type") == "posted-content":
            continue
        if normalized_title((item.get("title") or [""])[0]) != normalized_title(title):
            continue
        families = {normalized_title(a.get("family") or "") for a in item.get("author") or []}
        if wanted and families and wanted not in families:
            continue
        parts = (item.get("published") or item.get("issued") or {}).get("date-parts") or [[None]]
        year = parts[0][0] if parts and parts[0] else None
        if paper.get("year") and year and not (int(paper["year"]) <= int(year) <= int(paper["year"]) + 3):
            continue
        return {"doi": doi, "venue": (item.get("container-title") or [None])[0], "evidence": "Crossref title+author+year match"}
    return None


PREPRINT_HOSTS = ("arxiv.org", "biorxiv.org", "medrxiv.org", "ssrn.com", "researchsquare.com", "preprints.org", "chemrxiv.org", "osf.io")
MIN_PUBLISHED_BYTES = int(os.environ.get("CORPUS_ATLAS_MIN_PDF_BYTES", "10000"))


def is_preprint_source(url: str, source: str = "", version: str = "") -> bool:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return (version == "submittedVersion" or any(host == h or host.endswith("." + h) for h in PREPRINT_HOSTS)
            or bool(re.search(r"\b(?:arxiv|biorxiv|medrxiv|preprint)\b", source or "", re.I)))


def published_candidate(candidate: dict[str, str]) -> bool:
    return not is_preprint_source(candidate.get("url", ""), candidate.get("source", ""), candidate.get("version", ""))


def pdftotext_path() -> str | None:
    return shutil.which("pdftotext")


def verify_published_pdf(body: bytes, title: str) -> tuple[bool, str]:
    """paper-lib rule: real PDF, >=10 KB, and (when pdftotext is installed) the first three
    pages cover >=72% of the title tokens."""
    if not body.startswith(b"%PDF-"):
        return False, "payload is not a PDF"
    if len(body) < MIN_PUBLISHED_BYTES:
        return False, f"PDF is smaller than {MIN_PUBLISHED_BYTES} bytes"
    tool = pdftotext_path()
    if not tool:
        return True, "pdf magic + size (pdftotext not installed)"
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(body); handle.flush()
        try:
            text = subprocess.run([tool, "-f", "1", "-l", "3", handle.name, "-"], capture_output=True, text=True, timeout=60, check=True).stdout
        except (subprocess.SubprocessError, OSError) as error:
            return False, f"PDF parser failed: {type(error).__name__}"
    expected, actual = normalized_title(title), normalized_title(text)
    tokens = [token for token in expected.split() if len(token) > 2]
    coverage = sum(token in actual for token in tokens) / max(len(tokens), 1)
    if expected not in actual and coverage < 0.72:
        return False, f"title token coverage {coverage:.2f}"
    return True, f"title token coverage {coverage:.2f}"


def _replace_preprint_attachment(connection: sqlite3.Connection, root: Path, paper: dict[str, Any], email: str | None,
                                 prune: bool) -> dict[str, Any]:
    """Download the published version (never from a preprint server) and demote or prune
    the preprint attachment. The record keeps its arXiv ID either way."""
    files = [dict(r) for r in connection.execute("SELECT * FROM paper_files WHERE paper_id=? AND role IN ('main','preprint')", (paper["id"],))]
    mains = [f for f in files if f["role"] == "main"]
    if any(not is_preprint_source(f["source_url"], f["source_name"] or "") for f in mains):
        return {"status": "published-version-local"}
    from .library import record_links
    data = {**paper, "_links": record_links(connection, paper["id"], "pdf")}
    rejected: list[dict[str, str]] = []

    def accept(candidate: dict[str, str]) -> bool:
        return published_candidate(candidate)

    outcome = fetch_fulltext(data, email, accept=accept)
    while outcome["status"] == "downloaded":
        ok, note = verify_published_pdf(outcome["body"], paper["title"])
        if ok:
            break
        rejected.append({"url": outcome["candidate"]["url"], "source": outcome["candidate"]["source"], "reason": note})
        bad = {item["url"] for item in rejected}
        outcome = fetch_fulltext(data, email, accept=lambda c: published_candidate(c) and c["url"] not in bad)
    if outcome["status"] != "downloaded":
        return {"status": "published-version-unavailable", "attempts": outcome["attempts"] + rejected}
    candidate = outcome["candidate"]
    preprints = [f for f in files if f["role"] == "preprint" or is_preprint_source(f["source_url"], f["source_name"] or "")]
    digest = hashlib.sha256(outcome["body"]).hexdigest()
    if any(f["sha256"] == digest for f in preprints):
        return {"status": "published-version-unavailable", "attempts": [{"url": candidate["url"], "source": candidate["source"],
                                                                          "reason": "identical to the preprint PDF"}]}
    for f in preprints:  # demote first so the new file becomes the only main attachment
        connection.execute("UPDATE OR IGNORE paper_files SET role='preprint' WHERE id=?", (f["id"],))
    stored = store_file(connection, root, paper, "main", outcome["body"], candidate["url"], candidate["source"],
                        candidate["license"], outcome["content_type"])
    pruned = []
    if prune:
        for f in connection.execute("SELECT * FROM paper_files WHERE paper_id=? AND role='preprint'", (paper["id"],)).fetchall():
            path = root / f["local_file"]
            shared = connection.execute("SELECT COUNT(*) FROM paper_files WHERE local_file=? AND id!=?", (f["local_file"], f["id"])).fetchone()[0]
            if not shared:
                path.unlink(missing_ok=True)
            connection.execute("DELETE FROM paper_files WHERE id=?", (f["id"],))
            pruned.append(f["local_file"])
    return {"status": "published-version-downloaded", "file": stored["file"], "source": candidate["source"],
            "license": candidate["license"], "preprint_files": [f["local_file"] for f in preprints],
            "preprint_action": ("pruned" if prune else "role=preprint") if preprints else "none", "pruned": pruned}


def publications(connection: sqlite3.Connection, email: str | None, apply: bool, root: Path | None = None,
                 download: bool = True, prune_preprint: bool = False) -> list[dict[str, Any]]:
    """Find published versions of preprints. ``--apply`` records the published DOI/venue
    (keeping the arXiv ID), then fetches the published PDF from a non-preprint legal OA
    location; once verified the preprint attachment becomes role=preprint, or is deleted
    with ``prune_preprint``."""
    report = []
    for paper in [dict(r) for r in connection.execute("SELECT * FROM papers ORDER BY citation_key")]:
        if not is_preprint(paper):
            continue
        found = published_version(paper, email)
        entry = {"citation_key": paper["citation_key"], "preprint_doi": paper["doi"], "arxiv": paper["arxiv"],
                 "published": found, "applied": False}
        if found and apply:
            clash = connection.execute("SELECT citation_key FROM papers WHERE doi=? AND id!=?", (found["doi"], paper["id"])).fetchone()
            if clash:
                entry["note"] = f"published DOI already cached as {clash['citation_key']}; run dedupe"
            else:
                arxiv = paper["arxiv"] or (paper["doi"].split("arxiv.", 1)[1] if (paper["doi"] or "").startswith("10.48550/arxiv.") else None)
                connection.execute("UPDATE papers SET doi=?, arxiv=COALESCE(arxiv,?), venue=COALESCE(?,venue), updated_at=? WHERE id=?",
                                   (found["doi"], arxiv, found.get("venue"), current_time(), paper["id"]))
                entry["applied"] = True
                if download and root is not None:
                    updated = dict(connection.execute("SELECT * FROM papers WHERE id=?", (paper["id"],)).fetchone())
                    entry["fulltext"] = _replace_preprint_attachment(connection, root, updated, email, prune_preprint)
                connection.commit()
        report.append(entry)
    return report


# ---------------------------------------------------------------- integrity
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check(connection: sqlite3.Connection, root: Path, restore_from: Path | None, apply: bool) -> dict[str, Any]:
    problems = []
    for row in connection.execute("SELECT f.*, p.citation_key FROM paper_files f JOIN papers p ON p.id=f.paper_id ORDER BY local_file"):
        path = root / row["local_file"]
        if not path.is_file():
            problems.append({"file": row["local_file"], "sha256": row["sha256"], "problem": "missing"})
        elif file_sha256(path) != row["sha256"]:
            problems.append({"file": row["local_file"], "sha256": row["sha256"], "problem": "sha256-mismatch"})
        elif not Path(row["local_file"]).suffix:
            problems.append({"file": row["local_file"], "sha256": row["sha256"], "problem": "no-extension"})
    claimed = connection.execute("SELECT COUNT(*) FROM papers p WHERE fulltext_status='local-fulltext' AND NOT EXISTS (SELECT 1 FROM paper_files f WHERE f.paper_id=p.id AND f.role='main')").fetchone()[0]
    restores = []
    if restore_from and any(p["problem"] != "no-extension" for p in problems):
        index: dict[str, Path] = {}
        for candidate in restore_from.rglob("*"):
            if candidate.is_file() and candidate.stat().st_size:
                index.setdefault(file_sha256(candidate), candidate)
        for problem in problems:
            source = index.get(problem["sha256"])
            if source and problem["problem"] != "no-extension":
                action = {"file": problem["file"], "restore_from": source.name, "applied": False}
                if apply:
                    target = root / problem["file"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    action["applied"] = True
                restores.append(action)
    files = connection.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0]
    return {"files": files, "ok": files - len(problems), "problems": problems,
            "papers_claiming_fulltext_without_file": claimed, "restore": restores}


# ---------------------------------------------------------------- citations / S2
S2_FIELDS = "title,authors,year,externalIds,venue,abstract,url"


def semantic_scholar_row(item: dict[str, Any]) -> dict[str, Any]:
    ids = item.get("externalIds") or {}
    return {"title": item.get("title"), "authors": [a.get("name") for a in item.get("authors") or []], "year": item.get("year"),
            "doi": ids.get("DOI"), "arxiv": ids.get("ArXiv"), "pmid": ids.get("PubMed"), "semantic_scholar_id": item.get("paperId"),
            "venue": item.get("venue"), "abstract": item.get("abstract"), "url": item.get("url"), "metadata_source": "Semantic Scholar"}


def semantic_scholar_search(query: str, limit: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"query": query, "limit": limit, "fields": S2_FIELDS})
    data = get_json(f"https://api.semanticscholar.org/graph/v1/paper/search?{params}")
    if data is None:
        raise SystemExit("Semantic Scholar request failed (rate limited or unavailable); retry later or use --provider openalex")
    return [semantic_scholar_row(item) for item in data.get("data") or []]


def forward_citations(paper: dict[str, Any], provider: str, limit: int, email: str | None) -> list[dict[str, Any]]:
    if provider == "semanticscholar":
        ident = f"DOI:{paper['doi']}" if paper.get("doi") else paper.get("semantic_scholar_id")
        if not ident:
            return []
        data = get_json(f"https://api.semanticscholar.org/graph/v1/paper/{urllib.parse.quote(ident, safe=':/')}/citations?fields={S2_FIELDS}&limit={limit}") or {}
        return [semantic_scholar_row(item.get("citingPaper") or {}) for item in data.get("data") or []]
    work_id = paper.get("openalex_id")
    if not work_id and paper.get("doi"):
        work_id = (get_json(f"https://api.openalex.org/works/doi:{urllib.parse.quote(paper['doi'], safe='/')}", email) or {}).get("id")
    if not work_id:
        return []
    short = str(work_id).rsplit("/", 1)[-1]
    data = get_json(f"https://api.openalex.org/works?filter=cites:{short}&per-page={limit}", email) or {}
    return [{"title": w.get("title"), "authors": [a.get("author", {}).get("display_name") for a in w.get("authorships", [])],
             "year": w.get("publication_year"), "doi": w.get("doi"), "openalex_id": w.get("id"), "metadata_source": "OpenAlex"}
            for w in data.get("results", [])]

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest

from corpus_atlas import fulltext as ft
from corpus_atlas import scholarly
from corpus_atlas.cli import main

PDF = b"%PDF-1.7\nmocked full text"
HTML = b"<!DOCTYPE html><html><body>Access denied</body></html>"


def run(monkeypatch, capsys, *arguments):
    monkeypatch.setattr("sys.argv", ["corpus-atlas", *map(str, arguments)])
    main()
    return capsys.readouterr().out.strip()


def fake_web(monkeypatch, routes):
    """routes: url-prefix -> (status, content_type, body) or JSON-serialisable object."""
    seen = []

    def http_get(url, accept="application/json", email=None, timeout=60):
        seen.append({"url": url, "accept": accept})
        for prefix, value in routes.items():
            if url.startswith(prefix):
                if isinstance(value, tuple):
                    status, ctype, body = value
                    return ft.Response(status, ctype, body if status == 200 else b"", url, None if status == 200 else f"HTTP {status}")
                return ft.Response(200, "application/json", json.dumps(value).encode(), url)
        return ft.Response(404, "", b"", url, "HTTP 404")

    monkeypatch.setattr(ft, "http_get", http_get)
    return seen


def workspace(tmp_path, monkeypatch, capsys, records):
    root = tmp_path / "ws"
    run(monkeypatch, capsys, "init", root, "--title", "T", "--question", "Q", "--domain", "science")
    source = tmp_path / "records.json"
    source.write_text(json.dumps(records))
    run(monkeypatch, capsys, "paper", "import", root, "--from", source)
    return root


MDPI_DOI = "10.3390/catal14040232"
MDPI_PDF = "https://www.mdpi.com/2073-4344/14/4/232/pdf"


def mdpi_routes(publisher_status=403):
    return {
        f"https://api.openalex.org/works/doi:{MDPI_DOI}": {
            "best_oa_location": {"is_oa": True, "pdf_url": MDPI_PDF, "license": "cc-by", "source": {"display_name": "Catalysts"}},
            "locations": [{"is_oa": True, "pdf_url": "https://repo.example.org/catal.pdf", "license": "cc-by", "source": None}],
        },
        MDPI_PDF: (publisher_status, "application/pdf", PDF),
        "https://repo.example.org/catal.pdf": (200, "application/octet-stream", PDF),
    }


def test_openalex_null_source_does_not_crash(monkeypatch):
    payload = {"results": [{"title": "X", "authorships": [], "publication_year": 2024, "doi": "https://doi.org/10.1/x",
                            "id": "W1", "primary_location": {"source": None, "landing_page_url": "https://e.org"}}]}
    monkeypatch.setattr(scholarly, "request_json", lambda url: payload)
    rows = scholarly.discover("x", "openalex", 1)
    assert rows[0]["venue"] is None and rows[0]["url"] == "https://e.org"


@pytest.mark.parametrize("body,ctype,expected", [
    (PDF, "application/octet-stream", ".pdf"), (HTML, "text/html", ".html"),
    (b"<?xml version='1.0'?><a/>", "", ".xml"), (b"plain", "text/csv", ".csv"), (b"\x00\x01", "", ".bin"),
])
def test_infer_extension(body, ctype, expected):
    assert ft.infer_extension(body, ctype) == expected


def test_infer_extension_office_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<x/>")
    assert ft.infer_extension(buffer.getvalue()) == ".docx"


def test_user_agent_is_descriptive_and_accepts_pdf():
    assert ft.user_agent("me@example.org").startswith("Mozilla/5.0 (compatible; CorpusAtlas/")
    assert "mailto:me@example.org" in ft.user_agent("me@example.org")
    assert ft.PDF_ACCEPT.startswith("application/pdf")


def test_retrieve_extensionless_url_gets_pdf_suffix(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Catalysis", "doi": MDPI_DOI, "year": 2024, "authors": ["A Bee"]}])
    fake_web(monkeypatch, mdpi_routes(publisher_status=200))
    out = json.loads(run(monkeypatch, capsys, "paper", "retrieve", root, "--paper", MDPI_DOI, "--url", MDPI_PDF,
                         "--source", "MDPI", "--license", "CC-BY-4.0"))
    assert out["file"].endswith(".pdf") and Path(out["file"]).read_bytes() == PDF


def test_retrieve_403_falls_back_to_alternative_oa_location(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Catalysis", "doi": MDPI_DOI, "year": 2024, "authors": ["A Bee"]}])
    seen = fake_web(monkeypatch, mdpi_routes())
    out = json.loads(run(monkeypatch, capsys, "paper", "retrieve", root, "--paper", MDPI_DOI, "--url", MDPI_PDF,
                         "--source", "MDPI", "--license", "CC-BY-4.0"))
    assert out["source"] == "OpenAlex: OpenAlex location"
    assert any("HTTP 403" in a["reason"] for a in out["fallback_attempts"])
    assert out["file"].endswith(".pdf")
    assert all(call["accept"] for call in seen)


def test_retrieve_reports_clearly_when_all_fail(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Catalysis", "doi": MDPI_DOI, "year": 2024, "authors": ["A Bee"]}])
    routes = mdpi_routes()
    routes["https://repo.example.org/catal.pdf"] = (200, "text/html", HTML)
    fake_web(monkeypatch, routes)
    with pytest.raises(SystemExit) as error:
        run(monkeypatch, capsys, "paper", "retrieve", root, "--paper", MDPI_DOI, "--url", MDPI_PDF, "--source", "MDPI", "--license", "CC-BY")
    assert "HTTP 403" in str(error.value) and "not a PDF" in str(error.value)


def test_resolver_order_and_sources(monkeypatch):
    doi = "10.1101/2020.01.01.123"
    fake_web(monkeypatch, {
        f"https://api.openalex.org/works/doi:{doi}": {"best_oa_location": None, "locations": []},
        f"https://api.unpaywall.org/v2/{doi}": {"best_oa_location": {"url_for_pdf": "https://u.example/p.pdf", "host_type": "repository", "license": "cc-by"}},
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search": {"resultList": {"result": [{"doi": doi, "pmcid": "PMC1", "isOpenAccess": "Y", "license": "cc by"}]}},
        f"https://api.biorxiv.org/details/biorxiv/{doi}": {"collection": [{"version": "2", "license": "cc_by"}]},
    })
    sources = [c["source"] for c in ft.resolve_candidates({"doi": doi, "arxiv": "2001.00001"}, "me@example.org")]
    assert sources == ["Unpaywall: repository", "Europe PMC: PMC1", "PMC: PMC1", "arXiv: 2001.00001", "biorxiv: " + doi]
    assert not any(c["source"].startswith("Unpaywall") for c in ft.resolve_candidates({"doi": doi}, None))


def test_retrieve_all_batch_report_and_failures_csv(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [
        {"title": "Catalysis", "doi": MDPI_DOI, "year": 2024, "authors": ["A Bee"]},
        {"title": "Closed paper", "doi": "10.1016/closed", "year": 2023, "authors": ["C Dee"]},
        {"title": "Paywalled OA lie", "doi": "10.1/wall", "year": 2022, "authors": ["E Eff"]},
    ])
    routes = mdpi_routes()
    routes["https://api.openalex.org/works/doi:10.1/wall"] = {"best_oa_location": {"pdf_url": "https://wall.example/p", "is_oa": True}}
    routes["https://wall.example/p"] = (200, "text/html", HTML)
    fake_web(monkeypatch, routes)
    summary = json.loads(run(monkeypatch, capsys, "paper", "retrieve-all", root, "--max-workers", "3"))
    assert (summary["attempted"], summary["downloaded"], summary["failed"], summary["no_oa_candidate"]) == (3, 1, 1, 1)
    failures = list(csv.DictReader((root / summary["failures_csv"]).open()))
    assert {row["doi"] for row in failures} == {"10.1016/closed", "10.1/wall"}
    assert failures[0]["landing_url"].startswith("https://doi.org/")
    files = list((root / "attachments").iterdir())
    assert len(files) == 1 and files[0].suffix == ".pdf"
    again = json.loads(run(monkeypatch, capsys, "paper", "retrieve-all", root))
    assert again["already_local"] == 1 and again["attempted"] == 2  # resumable


def test_supplements_preview_then_apply(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Bio", "doi": "10.1/bio", "year": 2021, "authors": ["G H"]}])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mmc1.xlsx", b"PK\x03\x04fake")
        archive.writestr("figure1.jpg", b"\xff\xd8\xffimage")
        archive.writestr("data_s1", b"%PDF-1.4 supp")
    fake_web(monkeypatch, {
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search": {"resultList": {"result": [{"doi": "10.1/bio", "pmcid": "PMC9", "isOpenAccess": "Y", "license": "cc by"}]}},
        "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9/supplementaryFiles": (200, "application/zip", buffer.getvalue()),
    })
    preview = json.loads(run(monkeypatch, capsys, "paper", "supplements", root))
    assert preview[0]["status"] == "available-preview"
    assert not list((root / "attachments").iterdir())
    applied = json.loads(run(monkeypatch, capsys, "paper", "supplements", root, "--apply"))
    names = sorted(Path(f["file"]).name for f in applied[0]["files"])
    assert len(names) == 2 and any(n.endswith(".xlsx") for n in names) and any(n.endswith(".pdf") for n in names)
    snapshot = json.loads((root / "atlas.json").read_text())
    supp = [f for f in snapshot["paper_files"] if f["role"] == "supplement"]
    assert all(f["license"] == "cc by" and f["source_name"].startswith("Europe PMC") and len(f["sha256"]) == 64 for f in supp)


def test_publications_preview_and_apply(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Deep thing", "arxiv": "2101.00001", "year": 2021, "authors": ["I J"]}])
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>
      <arxiv:doi>10.1000/published</arxiv:doi><arxiv:journal_ref>J. Things 1 (2022)</arxiv:journal_ref></entry></feed>"""
    fake_web(monkeypatch, {"https://export.arxiv.org/api/query": (200, "application/atom+xml", atom)})
    preview = json.loads(run(monkeypatch, capsys, "paper", "publications", root))
    assert preview[0]["published"]["doi"] == "10.1000/published" and preview[0]["applied"] is False
    assert json.loads((root / "atlas.json").read_text())["papers"][0]["doi"] is None
    applied = json.loads(run(monkeypatch, capsys, "paper", "publications", root, "--apply"))
    paper = json.loads((root / "atlas.json").read_text())["papers"][0]
    assert applied[0]["applied"] and paper["doi"] == "10.1000/published" and paper["arxiv"] == "2101.00001"


def test_check_detects_damage_and_restores_by_sha(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Catalysis", "doi": MDPI_DOI, "year": 2024, "authors": ["A Bee"]}])
    fake_web(monkeypatch, mdpi_routes(publisher_status=200))
    out = json.loads(run(monkeypatch, capsys, "paper", "retrieve", root, "--paper", MDPI_DOI, "--url", MDPI_PDF, "--source", "MDPI", "--license", "CC-BY"))
    assert json.loads(run(monkeypatch, capsys, "paper", "check", root))["problems"] == []
    backup = tmp_path / "backup"; backup.mkdir(); (backup / "renamed.bin").write_bytes(PDF)
    Path(out["file"]).unlink()
    preview = json.loads(run(monkeypatch, capsys, "paper", "check", root, "--restore-from", backup))
    assert preview["problems"][0]["problem"] == "missing" and preview["restore"][0]["applied"] is False
    assert not Path(out["file"]).exists()
    json.loads(run(monkeypatch, capsys, "paper", "check", root, "--restore-from", backup, "--apply"))
    assert Path(out["file"]).read_bytes() == PDF
    Path(out["file"]).write_bytes(b"%PDF-tampered")
    assert json.loads(run(monkeypatch, capsys, "paper", "check", root))["problems"][0]["problem"] == "sha256-mismatch"


def test_semantic_scholar_discover_and_forward_citations(tmp_path, monkeypatch, capsys):
    item = {"paperId": "s2a", "title": "Citing work", "authors": [{"name": "K L"}], "year": 2025,
            "externalIds": {"DOI": "10.1/citing", "ArXiv": "2501.00001"}, "venue": "V"}
    fake_web(monkeypatch, {
        "https://api.semanticscholar.org/graph/v1/paper/search": {"data": [item]},
        "https://api.openalex.org/works?filter=cites:W42": {"results": [{"title": "OA citer", "doi": "https://doi.org/10.1/oacite", "id": "W7", "authorships": []}]},
    })
    rows = scholarly.discover("q", "semanticscholar", 5)
    assert rows[0]["doi"] == "10.1/citing" and rows[0]["semantic_scholar_id"] == "s2a"
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Seed", "openalex_id": "https://openalex.org/W42", "year": 2020, "authors": ["M N"]}])
    cited = json.loads(run(monkeypatch, capsys, "paper", "citations", root, "--paper", "n2020seed", "--cache"))
    assert cited[0]["title"] == "OA citer"
    status = json.loads(run(monkeypatch, capsys, "status", root))
    assert status["papers"] == 2 and status["paper_relations"] == 1

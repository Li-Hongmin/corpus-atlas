"""paper-lib parity: duplicate preferences, published-version replacement, URL
sanitisation, YAML input, and the aria2 bulk transport (subprocess mocked)."""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from corpus_atlas import fulltext as ft
from corpus_atlas import transport
from corpus_atlas.urls import sanitize_obj, sanitize_text, sanitize_url
from corpus_atlas.yamlio import YAMLError, load_yaml, parse_yaml
from test_fulltext import PDF, HTML, fake_web, run, workspace

BIG_PDF = b"%PDF-1.7\n" + b"published version " * 1000


def db(root):
    connection = sqlite3.connect(root / "atlas.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


# ------------------------------------------------------------ 1. duplicate preferences
DUPES = [
    {"title": "Graph neural networks for chemistry", "year": 2021, "authors": ["Ada Lovelace"], "arxiv": "2101.00001"},
    {"title": "Graph Neural Networks for Chemistry.", "year": 2022, "authors": ["Ada Lovelace"], "doi": "10.9/gnn", "venue": "J. Chem"},
]


def test_dedupe_reports_score_rule_without_preferences(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, DUPES)
    preview = json.loads(run(monkeypatch, capsys, "paper", "dedupe", root))
    assert preview[0]["keep"] == "lovelace2022graph" and preview[0]["rule"] == "score:most-complete-metadata"


def test_dedupe_preference_file_overrides_score_and_apply_honours_it(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, DUPES)
    prefs = root / "config" / "duplicate-preferences.yaml"
    prefs.parent.mkdir()
    prefs.write_text("# keep the preprint record\nduplicates:\n  10.9/gnn:\n    preferred: lovelace2021graph\n")
    preview = json.loads(run(monkeypatch, capsys, "paper", "dedupe", root))
    assert preview[0]["keep"] == "lovelace2021graph" and preview[0]["rule"] == "preference:10.9/gnn"
    assert db(root).execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
    applied = json.loads(run(monkeypatch, capsys, "paper", "dedupe", root, "--apply"))
    assert applied[0]["applied"] and applied[0]["keep"] == "lovelace2021graph"
    row = db(root).execute("SELECT * FROM papers").fetchone()
    assert row["citation_key"] == "lovelace2021graph" and (row["doi"], row["arxiv"]) == ("10.9/gnn", "2101.00001")


def test_dedupe_paper_lib_sha256_preferences_json_and_validation(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [
        {"title": "First distinct title here", "year": 2020, "authors": ["A B"], "doi": "10.9/a"},
        {"title": "Second distinct title here", "year": 2020, "authors": ["C D"], "doi": "10.9/b"}])
    fake_web(monkeypatch, {"https://pub.org/x.pdf": (200, "application/pdf", PDF)})
    for ref in ("10.9/a", "10.9/b"):
        run(monkeypatch, capsys, "paper", "retrieve", root, "--paper", ref, "--url", "https://pub.org/x.pdf", "--source", "S", "--license", "cc-by")
    digest = db(root).execute("SELECT sha256 FROM paper_files LIMIT 1").fetchone()[0]
    keys = [r[0] for r in db(root).execute("SELECT citation_key FROM papers ORDER BY doi")]
    with pytest.raises(SystemExit, match="explicit preference"):
        run(monkeypatch, capsys, "paper", "dedupe", root, "--require-preferences")
    prefs = tmp_path / "prefs.json"
    prefs.write_text(json.dumps({"duplicates": {digest: {"preferred": keys[1]}}}))
    out = json.loads(run(monkeypatch, capsys, "paper", "dedupe", root, "--preferences", prefs, "--require-preferences", "--apply"))
    assert out[0]["keep"] == keys[1] and out[0]["rule"] == f"preference:{digest}"
    bad = tmp_path / "bad.yaml"
    bad.write_text(f"duplicates:\n  {digest}:\n    preferred: someone-else\n")
    root2 = workspace(tmp_path / "two", monkeypatch, capsys, DUPES)
    bad.write_text("duplicates:\n  10.9/gnn: {preferred: not-a-member}\n")
    with pytest.raises(SystemExit, match="not a member"):
        run(monkeypatch, capsys, "paper", "dedupe", root2, "--preferences", bad)


# ------------------------------------------------------------ 2. published version replaces preprint
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>
  <arxiv:doi>10.1000/published</arxiv:doi><arxiv:journal_ref>J. Things 1 (2022)</arxiv:journal_ref></entry></feed>"""


def _preprint_workspace(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Deep thing", "arxiv": "2101.00001", "year": 2021, "authors": ["I J"]}])
    fake_web(monkeypatch, {"https://arxiv.org/pdf/2101.00001": (200, "application/pdf", PDF)})
    run(monkeypatch, capsys, "paper", "retrieve", root, "--paper", "j2021deep")
    return root


def _publication_routes(pdf=BIG_PDF):
    return {"https://export.arxiv.org/api/query": (200, "application/atom+xml", ATOM),
            "https://api.openalex.org/works/doi:10.1000/published": {
                "locations": [{"is_oa": True, "pdf_url": "https://arxiv.org/pdf/2101.00001v2", "version": "submittedVersion"},
                              {"is_oa": True, "pdf_url": "https://journal.example/p.pdf", "version": "publishedVersion",
                               "license": "cc-by", "source": {"display_name": "J. Things"}}]},
            "https://journal.example/p.pdf": (200, "application/pdf", pdf)}


def test_publications_apply_downloads_published_pdf_and_demotes_preprint(tmp_path, monkeypatch, capsys):
    root = _preprint_workspace(tmp_path, monkeypatch, capsys)
    seen = fake_web(monkeypatch, _publication_routes())
    out = json.loads(run(monkeypatch, capsys, "paper", "publications", root, "--apply"))
    assert out[0]["fulltext"]["status"] == "published-version-downloaded", out
    assert out[0]["fulltext"]["preprint_action"] == "role=preprint"
    assert not any(s["url"].startswith("https://arxiv.org/pdf/2101.00001v2") for s in seen)  # never refetch preprint
    files = {r["role"]: dict(r) for r in db(root).execute("SELECT * FROM paper_files")}
    assert set(files) == {"main", "preprint"}
    assert files["main"]["source_url"] == "https://journal.example/p.pdf"
    assert (root / files["preprint"]["local_file"]).read_bytes() == PDF
    paper = db(root).execute("SELECT * FROM papers").fetchone()
    assert (paper["doi"], paper["arxiv"]) == ("10.1000/published", "2101.00001")
    assert json.loads(run(monkeypatch, capsys, "paper", "check", root))["problems"] == []


def test_publications_prune_preprint_only_when_explicit(tmp_path, monkeypatch, capsys):
    root = _preprint_workspace(tmp_path, monkeypatch, capsys)
    preprint = db(root).execute("SELECT local_file FROM paper_files").fetchone()[0]
    fake_web(monkeypatch, _publication_routes())
    out = json.loads(run(monkeypatch, capsys, "paper", "publications", root, "--apply", "--prune-preprint"))
    assert out[0]["fulltext"]["pruned"] == [preprint] and not (root / preprint).exists()
    assert [r[0] for r in db(root).execute("SELECT role FROM paper_files")] == ["main"]
    assert db(root).execute("SELECT arxiv FROM papers").fetchone()[0] == "2101.00001"


def test_publications_unverified_pdf_keeps_preprint_as_main(tmp_path, monkeypatch, capsys):
    root = _preprint_workspace(tmp_path, monkeypatch, capsys)
    fake_web(monkeypatch, _publication_routes(pdf=PDF))  # tiny PDF fails the size check
    out = json.loads(run(monkeypatch, capsys, "paper", "publications", root, "--apply", "--prune-preprint"))
    assert out[0]["fulltext"]["status"] == "published-version-unavailable"
    assert "smaller than" in out[0]["fulltext"]["attempts"][-1]["reason"]
    assert [r[0] for r in db(root).execute("SELECT role FROM paper_files")] == ["main"]
    assert out[0]["applied"]  # DOI still recorded


# ------------------------------------------------------------ 3. URL sanitisation
@pytest.mark.parametrize("raw,clean", [
    ("https://s3.example/p.pdf?X-Amz-Signature=abc&X-Amz-Credential=k&version=2", "https://s3.example/p.pdf?version=2"),
    ("https://x.org/a?token=t&apikey=k&api_key=k&sig=s&Expires=1&Policy=p&Key-Pair-Id=i&session=s&id=7", "https://x.org/a?id=7"),
    ("https://user:pass@x.org/a.pdf", "https://x.org/a.pdf"),
    ("https://x.org/login?access_token=t&redirect_uri=https%3A%2F%2Fy.org%2Ff.pdf%3Fkey%3Dz", "https://y.org/f.pdf"),
    ("https://arxiv.org/pdf/2101.00001", "https://arxiv.org/pdf/2101.00001"),
])
def test_sanitize_url(raw, clean):
    assert sanitize_url(raw) == clean


def test_sanitize_nested_reports_and_text():
    report = {"attempts": [{"url": "https://a.org/f?token=1", "note": "tried https://b.org/g?sig=2 | https://c.org/h"}]}
    assert sanitize_obj(report) == {"attempts": [{"url": "https://a.org/f", "note": "tried https://b.org/g | https://c.org/h"}]}
    assert sanitize_text("no url") == "no url"


def test_signed_urls_never_reach_db_exports_reports_or_queue(tmp_path, monkeypatch, capsys):
    signed = "https://cdn.example/p.pdf?X-Amz-Signature=SECRET&token=SECRET"
    root = workspace(tmp_path, monkeypatch, capsys, [
        {"title": "Signed paper", "year": 2024, "authors": ["A B"], "doi": "10.9/s", "url": "https://pub.example/a?auth=SECRET",
         "open_access_pdf": signed, "supplement_urls": "https://cdn.example/s.xlsx?Signature=SECRET"}])
    fake_web(monkeypatch, {signed: (200, "application/pdf", PDF)})
    run(monkeypatch, capsys, "paper", "retrieve", root, "--paper", "10.9/s", "--url", signed, "--source", "S", "--license", "cc-by")
    run(monkeypatch, capsys, "paper", "import", root, "--from", _record(tmp_path, {"title": "Failing paper", "doi": "10.9/f", "year": 2024,
                                                                              "open_access_pdf": "https://x.example/f.pdf?key=SECRET"}))
    fake_web(monkeypatch, {})
    run(monkeypatch, capsys, "paper", "retrieve-all", root)
    run(monkeypatch, capsys, "paper", "supplements", root, "--apply", "--no-publisher-pages")
    dump = "\n".join(db(root).iterdump())
    texts = [dump, (root / "atlas.json").read_text()] + [p.read_text() for p in (root / "reports").iterdir()]
    assert not any("SECRET" in text for text in texts)
    assert "https://cdn.example/p.pdf" in dump


def _record(tmp_path, record):
    path = tmp_path / f"r{abs(hash(json.dumps(record)))}.json"
    path.write_text(json.dumps([record]))
    return path


# ------------------------------------------------------------ 4. YAML
PAPER_LIB_INFO = """\
# paper-lib style info.yaml
ref: smith2025paper
title: 'Robust clustering: a "practical" view'
author: Smith, Anne and Doe, Bo
author_list:
- family: Smith
  given: Anne
- family: Doe
  given: Bo
year: 2025
doi: 10.1234/ABC.5
type: article
journal: Journal of Tests
tags: [clustering, robust]
abstract: >-
  First line of the abstract
  continues here.

  Second paragraph.
notes: |
  keep: this colon
  # not a comment
files:
  - smith2025paper.pdf
supplementary_files: []
references:
  - doi: 10.1/ref1
  - {title: "A cited work", year: 2001}
date_added: 2025-01-02
empty_field:
"""


def test_builtin_yaml_parses_paper_lib_record():
    data = parse_yaml(PAPER_LIB_INFO)
    assert data["title"] == 'Robust clustering: a "practical" view'
    assert data["author_list"][1] == {"family": "Doe", "given": "Bo"}
    assert data["abstract"] == "First line of the abstract continues here.\nSecond paragraph."
    assert data["notes"] == "keep: this colon\n# not a comment\n"
    assert data["tags"] == ["clustering", "robust"] and data["supplementary_files"] == []
    assert data["references"][1] == {"title": "A cited work", "year": 2001}
    assert data["date_added"] == "2025-01-02" and data["empty_field"] is None and data["year"] == 2025


@pytest.mark.parametrize("text,expected", [
    ("- a\n- - b\n  - c\n-\n  k: v\n", ["a", ["b", "c"], {"k": v}] if (v := "v") else None),
    ("a:\n  b:\n    c: [1, 2.5, true, null, ~]\n", {"a": {"b": {"c": [1, 2.5, True, None, None]}}}),
    ("s: \"tab\\tq\\\"\\u00e9\"\nt: 'it''s'\n", {"s": 'tab\tq"é', "t": "it's"}),
    ("k: |-\n  x\n\n  y\nz: >+\n  a\n  b\n\n", {"k": "x\n\ny", "z": "a b\n\n"}),
    ("long: one\n  two\n  three\nn: -3\n", {"long": "one two three", "n": -3}),
    ("---\nmulti: [a,\n  b]\n...\n", {"multi": ["a", "b"]}),
    ("url: https://x.org/a#frag # comment\n", {"url": "https://x.org/a#frag"}),
])
def test_builtin_yaml_constructs(text, expected):
    assert parse_yaml(text) == expected


@pytest.mark.parametrize("text,message", [
    ("a: &x 1\nb: *x\n", "anchors"), ("a: !!str 1\n", "tags"), ("? a\n: b\n", "complex"),
    ("a: 1\n---\nb: 2\n", "multiple"), ("a: 1\n  b: 2\n", "line 2"), ("a:\n\t- x\n", "tabs"), ("a: 'open\n", "unterminated"),
])
def test_builtin_yaml_errors_are_clear(text, message):
    with pytest.raises(YAMLError, match=message):
        parse_yaml(text)


def test_load_yaml_matches_pyyaml_when_installed(monkeypatch):
    yaml = pytest.importorskip("yaml")
    assert load_yaml(PAPER_LIB_INFO) == parse_yaml(PAPER_LIB_INFO)
    monkeypatch.setenv("CORPUS_ATLAS_YAML", "builtin")
    assert load_yaml("a: [1]\n") == {"a": [1]}


def test_import_paper_lib_yaml_records(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [])
    info = tmp_path / "info.yaml"
    info.write_text(PAPER_LIB_INFO)
    assert json.loads(run(monkeypatch, capsys, "paper", "import", root, "--from", info))["created"] == 1
    paper = db(root).execute("SELECT * FROM papers").fetchone()
    assert paper["citation_key"] == "smith2025robust" and paper["doi"] == "10.1234/abc.5" and paper["venue"] == "Journal of Tests"
    assert json.loads(paper["authors"]) == ["Smith, Anne", "Doe, Bo"]
    many = tmp_path / "many.yml"
    many.write_text("records:\n  - title: One paper title\n    year: 2020\n  - title: Two paper title\n    abstract: |\n      multi\n      line\n")
    assert json.loads(run(monkeypatch, capsys, "paper", "import", root, "--from", many))["created"] == 2
    broken = tmp_path / "broken.yaml"
    broken.write_text("title: x\nref: &anchor y\n")
    monkeypatch.setenv("CORPUS_ATLAS_YAML", "builtin")
    with pytest.raises(SystemExit, match="anchors"):
        run(monkeypatch, capsys, "paper", "import", root, "--from", broken)


# ------------------------------------------------------------ 5. aria2 transport
def fake_aria2(monkeypatch, files, calls, exit_code=0):
    """Mock subprocess.run for aria2c: write `files[url]` for each queued URL."""
    monkeypatch.setattr(transport, "aria2_path", lambda: "/usr/bin/aria2c")

    def runner(command, **kwargs):
        calls.append(command)
        args = dict(part[2:].split("=", 1) for part in command[1:] if part.startswith("--") and "=" in part)
        lines = Path(args["input-file"]).read_text().splitlines()
        for url, out in zip(lines[::2], lines[1::2]):
            if files.get(url) is not None:
                (Path(args["dir"]) / out.strip()[4:]).write_bytes(files[url])
        return subprocess.CompletedProcess(command, exit_code, stdout="")

    monkeypatch.setattr(transport.subprocess, "run", runner)


OPENALEX = "https://api.openalex.org/works/doi:"


def _two_candidates(doi, first, second):
    return {f"{OPENALEX}{doi}": {"best_oa_location": {"is_oa": True, "pdf_url": first, "license": "cc-by", "source": {"display_name": "J"}},
                                  "locations": [{"is_oa": True, "pdf_url": second, "license": "cc-by", "source": {"display_name": "Repo"}}]}}


def test_retrieve_all_aria2_validates_and_records_like_python(tmp_path, monkeypatch, capsys):
    records = [{"title": "Paper A title", "doi": "10.9/a", "year": 2020, "authors": ["A B"]},
               {"title": "Paper B title", "doi": "10.9/b", "year": 2020, "authors": ["C D"]}]
    routes = {**_two_candidates("10.9/a", "https://pub.example/a.pdf", "https://repo.example/a.pdf"),
              **_two_candidates("10.9/b", "https://pub.example/b.pdf", "https://repo.example/b.pdf")}
    python_root = workspace(tmp_path / "py", monkeypatch, capsys, records)
    fake_web(monkeypatch, {**routes, "https://pub.example/a.pdf": (200, "text/html", HTML), "https://repo.example/a.pdf": (200, "application/pdf", PDF),
                           "https://pub.example/b.pdf": (200, "application/pdf", PDF + b"b")})
    py = json.loads(run(monkeypatch, capsys, "paper", "retrieve-all", python_root, "--transport", "python"))

    aria_root = workspace(tmp_path / "ar", monkeypatch, capsys, records)
    seen = fake_web(monkeypatch, routes)  # metadata only: file bytes must come from aria2
    calls = []
    fake_aria2(monkeypatch, {"https://pub.example/a.pdf": HTML, "https://repo.example/a.pdf": PDF,
                             "https://pub.example/b.pdf": PDF + b"b"}, calls)
    ar = json.loads(run(monkeypatch, capsys, "paper", "retrieve-all", aria_root, "--transport", "aria2", "--connections", "3"))
    assert py["downloaded"] == ar["downloaded"] == 2 and ar["transport"] == "aria2"
    assert len(calls) == 2  # pass 1: both first candidates; pass 2: a's second candidate
    assert "--max-connection-per-server=3" in calls[0] and "--continue=true" in calls[0]
    assert not any(s["url"].endswith(".pdf") for s in seen)
    cols = "role, source_url, source_name, license, sha256, bytes, content_type"
    rows = lambda root: sorted(tuple(r) for r in db(root).execute(f"SELECT {cols} FROM paper_files"))
    assert rows(python_root) == rows(aria_root)
    assert not list((aria_root / ".staging" / "aria2").glob("*.part"))


def test_retrieve_all_aria2_failures_fall_back_to_python(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Paper A title", "doi": "10.9/a", "year": 2020, "authors": ["A B"]}])
    fake_web(monkeypatch, {**_two_candidates("10.9/a", "https://pub.example/a.pdf", "https://repo.example/a.pdf"),
                           "https://pub.example/a.pdf": (200, "application/pdf", PDF)})
    fake_aria2(monkeypatch, {}, [], exit_code=3)  # aria2 runs but fetches nothing
    out = json.loads(run(monkeypatch, capsys, "paper", "retrieve-all", root, "--transport", "aria2"))
    report = json.loads((root / out["report"]).read_text())["results"][0]
    assert out["downloaded"] == 1 and report["transport"] == "aria2+python-fallback"
    assert any(a["reason"].startswith("aria2:") for a in report["attempts"])


def test_retrieve_all_aria2_unrunnable_uses_python(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Paper A title", "doi": "10.9/a", "year": 2020, "authors": ["A B"]}])
    fake_web(monkeypatch, {**_two_candidates("10.9/a", "https://pub.example/a.pdf", "https://repo.example/a.pdf"),
                           "https://pub.example/a.pdf": (200, "application/pdf", PDF)})
    monkeypatch.setattr(transport, "aria2_path", lambda: "/nonexistent/aria2c")

    def boom(*a, **k):
        raise FileNotFoundError("aria2c")
    monkeypatch.setattr(transport.subprocess, "run", boom)
    out = json.loads(run(monkeypatch, capsys, "paper", "retrieve-all", root, "--transport", "auto"))
    assert out["downloaded"] == 1 and "fell back to python" in out["notes"][0]


def test_transport_choice(monkeypatch):
    assert transport.choose("auto") == "python" and transport.choose("python") == "python"
    with pytest.raises(SystemExit, match="aria2c is not on PATH"):
        transport.choose("aria2")
    monkeypatch.setattr(transport, "aria2_path", lambda: "/usr/bin/aria2c")
    assert transport.choose("auto") == "aria2"


def test_supplements_aria2_validates_and_falls_back(tmp_path, monkeypatch, capsys):
    good, html, flaky = "https://repo.example/s1.xlsx", "https://repo.example/s2.pdf", "https://repo.example/s3.csv"
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Supp paper", "year": 2020, "authors": ["A B"],
                                                      "supplement_urls": json.dumps([good, html, flaky])}])
    fake_web(monkeypatch, {flaky: (200, "text/csv", b"a,b\n1,2\n")})
    calls = []
    fake_aria2(monkeypatch, {good: b"PK\x03\x04xlsx-bytes", html: HTML}, calls)
    out = json.loads(run(monkeypatch, capsys, "paper", "supplements", root, "--apply", "--transport", "aria2", "--no-publisher-pages"))
    assert len(calls) == 1 and out[0]["transport"] == "aria2"
    stored = sorted(Path(f["file"]).suffix for f in out[0]["files"])
    assert stored == [".csv", ".xlsx"]  # html rejected, flaky one fetched by the python fallback
    assert any("HTML" in e["error"] for e in out[0]["errors"])


def test_published_pdf_title_check_with_pdftotext(monkeypatch):
    monkeypatch.setattr(ft, "pdftotext_path", lambda: "/usr/bin/pdftotext")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="Deep Thing\nI. J."))
    assert ft.verify_published_pdf(BIG_PDF, "Deep thing")[0]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="Unrelated cover page"))
    ok, note = ft.verify_published_pdf(BIG_PDF, "Deep learning of things")
    assert not ok and "coverage" in note


def test_aria2_routes_python_only_hosts_to_python(tmp_path, monkeypatch, capsys):
    mdpi = "https://www.mdpi.com/x/pdf"
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Paper A title", "doi": "10.9/a", "year": 2020, "authors": ["A B"]}])
    fake_web(monkeypatch, {**_two_candidates("10.9/a", mdpi, "https://repo.example/a.pdf"), mdpi: (200, "application/pdf", PDF)})
    calls = []
    fake_aria2(monkeypatch, {}, calls)
    out = json.loads(run(monkeypatch, capsys, "paper", "retrieve-all", root, "--transport", "aria2"))
    assert out["downloaded"] == 1 and calls == []  # never sent to aria2
    assert db(root).execute("SELECT source_url FROM paper_files").fetchone()[0] == mdpi

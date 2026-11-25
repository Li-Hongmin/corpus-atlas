import pytest

from corpus_atlas import fulltext as ft
from corpus_atlas import transport


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    # tests are offline: never pick up a real aria2c from PATH unless a test opts in
    monkeypatch.setattr(transport, "aria2_path", lambda: None)
    monkeypatch.setattr(ft, "pdftotext_path", lambda: None)
    ft._EPMC_CACHE.clear(); ft._OPENALEX_CACHE.clear()
    yield
    ft._EPMC_CACHE.clear(); ft._OPENALEX_CACHE.clear()

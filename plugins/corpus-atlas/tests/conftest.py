import pytest

from corpus_atlas import fulltext as ft


@pytest.fixture(autouse=True)
def _clear_caches():
    ft._EPMC_CACHE.clear(); ft._OPENALEX_CACHE.clear()
    yield
    ft._EPMC_CACHE.clear(); ft._OPENALEX_CACHE.clear()

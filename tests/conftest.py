import pytest


@pytest.fixture(autouse=True)
def private_xdg_dirs(tmp_path, monkeypatch):
    """Tests must never read the user's settings or write into their cache (session saving, for one)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

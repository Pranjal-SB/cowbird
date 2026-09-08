"""Repo-wide test isolation.

The CLI persists measured provider health to `~/.cowbird/health.json` unless
COWBIRD_HEALTH_PATH says otherwise. Without this fixture, running the suite
writes fake providers -- `fake`, `drifted`, `bad` -- into the real file a
developer's own `cowbird providers` then reads back. A test that quarantines a
fake provider would be writing a quarantine into live state.

Individual tests already override the variable where they think to. This makes
it unconditional, because the failure is silent and lands outside the repo.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_health_file(tmp_path, monkeypatch):
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))

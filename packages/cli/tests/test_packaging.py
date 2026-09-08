"""What `pip install cowbird` actually gets.

The fleet exists in this checkout because `uv sync --all-packages` installs the
whole workspace. That says nothing about an install from an index: the `cowbird`
distribution only pulls the providers it declares, and a provider added to the
workspace without a line here is invisible to every user who did not clone the
repo. The suite would stay green the whole time.
"""

from __future__ import annotations

from importlib.metadata import entry_points, requires

import pytest
from packaging.requirements import Requirement


def declared(distribution: str) -> set[str]:
    return {
        Requirement(raw).name
        for raw in requires(distribution) or []
        if "extra ==" not in raw
    }


def installed_provider_distributions() -> set[str]:
    return {ep.dist.name for ep in entry_points(group="cowbird.providers") if ep.dist}


@pytest.mark.parametrize("distribution", ["cowbird", "cowbird-server"])
def test_the_shipped_distributions_depend_on_every_installed_provider(distribution):
    missing = installed_provider_distributions() - declared(distribution)
    assert not missing, (
        f"{sorted(missing)} are installed here but not declared by {distribution}, "
        f"so `pip install {distribution}` would not get them"
    )

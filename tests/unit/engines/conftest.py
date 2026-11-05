"""Fixtures shared by the engine tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def ruby():
    """Build a Ruby object with ``@``-prefixed ivars, the way Marshal holds one."""
    from rubymarshal.classes import RubyObject

    def build(class_name: str, **ivars):
        obj = RubyObject(class_name)
        obj.attributes = {f"@{key}": value for key, value in ivars.items()}
        return obj

    return build


@pytest.fixture
def text():
    """A Ruby String carrying its encoding ivar, as a real .rvdata2 does."""
    from rubymarshal.classes import RubyString

    def build(value: str):
        string = RubyString(value)
        string.attributes = {"E": True}
        return string

    return build

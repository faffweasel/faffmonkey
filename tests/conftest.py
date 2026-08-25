"""Fixtures shared across the test suite.

The project-root conftest.py handles sys.path; this one holds fixtures.
"""

import pytest


@pytest.fixture
def ws(tmp_path):
    """An empty workspace directory under tmp_path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace

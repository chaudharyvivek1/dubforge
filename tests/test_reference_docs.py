"""The generated reference docs must match the code that produces them.

Hand-written reference tables rot. These docs are generated from `Settings`, the live MCP
tool list and the argparse tree, and CI runs the same check — so a setting, a tool or a
flag that is added without being documented fails the build.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gen_reference  # noqa: E402

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_configuration_reference_is_current():
    current = (DOCS / "configuration.md").read_text(encoding="utf-8")
    assert current == gen_reference.configuration_doc(), (
        "docs/configuration.md is stale — run `python scripts/gen_reference.py`"
    )


def test_cli_reference_is_current():
    current = (DOCS / "cli.md").read_text(encoding="utf-8")
    assert current == gen_reference.cli_doc(), (
        "docs/cli.md is stale — run `python scripts/gen_reference.py`"
    )


def test_mcp_reference_is_current():
    pytest.importorskip("fastmcp", reason="the MCP extra is not installed")
    current = (DOCS / "mcp-tools.md").read_text(encoding="utf-8")
    assert current == gen_reference.mcp_doc(), (
        "docs/mcp-tools.md is stale — run `python scripts/gen_reference.py`"
    )


def test_the_generator_covers_every_setting_that_matters():
    from dubforge.config import Settings

    doc = gen_reference.configuration_doc()
    for field in Settings.__dataclass_fields__:
        if field == "env_warnings":
            continue
        assert f"`{field}`" in doc, f"{field} is missing from docs/configuration.md"


def test_every_tool_appears_with_its_parameters():
    pytest.importorskip("fastmcp", reason="the MCP extra is not installed")
    import asyncio

    from dubforge.server import build_server

    doc = gen_reference.mcp_doc()
    for tool in asyncio.run(build_server().list_tools()):
        assert f"## `{tool.name}`" in doc
        for parameter in (getattr(tool, "parameters", None) or {}).get("properties", {}):
            assert f"`{parameter}`" in doc

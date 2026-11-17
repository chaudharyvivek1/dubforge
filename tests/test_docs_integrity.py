"""Documentation integrity: links resolve, counts agree, nothing is promised that is gone.

The generated reference pages are checked against their source by
`tests/test_reference_docs.py`. This file checks the *hand-written* documentation: every
relative link points at a file that exists, every skill reference listed in SKILL.md is
present, the version is consistent everywhere, and the numbers the docs claim match the
numbers the code produces.

A doc that links to a deleted page is worse than no link: it reads as evidence that the
thing still exists.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN = sorted(
    [
        ROOT / "README.md",
        ROOT / "FAQ.md",
        ROOT / "ROADMAP.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CREDITS.md",
        ROOT / "SECURITY.md",
    ]
    + list((ROOT / "docs").glob("*.md"))
    + list((ROOT / "skill").rglob("*.md"))
    + list((ROOT / ".github").rglob("*.md"))
)

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _relative_links(path: Path) -> list[str]:
    links = []
    for target in LINK.findall(path.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = target.split("#", 1)[0].strip()
        if target:
            links.append(target)
    return links


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_resolves(path):
    missing = [target for target in _relative_links(path) if not (path.parent / target).exists()]
    assert not missing, f"{path.relative_to(ROOT)} links to missing files: {missing}"


def test_the_version_is_the_same_everywhere():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    init = (ROOT / "src" / "dubforge" / "__init__.py").read_text(encoding="utf-8")
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    versions = {
        re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1),
        re.search(r'__version__ = "([^"]+)"', init).group(1),
        re.search(r'^version: "([^"]+)"', citation, re.M).group(1),
    }
    assert len(versions) == 1, f"version mismatch: {versions}"


def test_the_changelog_has_an_entry_for_the_current_version():
    from dubforge import __version__

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in changelog


def test_the_skill_lists_exactly_the_references_that_exist():
    skill = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"references/([a-z-]+\.md)", skill))
    present = {path.name for path in (ROOT / "skill" / "references").glob("*.md")}
    assert listed == present, f"SKILL.md lists {listed ^ present} differently from disk"


def test_the_readme_docs_table_covers_every_docs_page():
    linked = {Path(target).name for target in _relative_links(ROOT / "README.md")}
    pages = {path.name for path in (ROOT / "docs").glob("*.md")}
    missing = pages - linked
    assert not missing, f"docs pages not linked from the README: {sorted(missing)}"


def test_the_qc_check_count_in_the_docs_matches_the_code(demo_clip, tmp_path):
    """The docs claim a number of QC checks; the code produces one. They must agree."""
    from dubforge import demo, pipeline
    from dubforge.config import Settings

    settings = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "w"),
        cache=False,
    )
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "count.mp4")
    produced = len(result.qc["checks"])

    words = {20: "twenty", 21: "twenty-one", 15: "fifteen", 10: "ten"}
    claim = words.get(produced, str(produced))
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    # The phrase, not a bare number: "20" appears inside dates and versions.
    assert f"{claim} checks" in readme, (
        f"the pipeline produced {produced} QC checks but README.md does not say '{claim} checks'"
    )


def test_no_doc_promises_a_tool_that_does_not_exist():
    """Every MCP tool name mentioned in the docs must exist on the server."""
    pytest.importorskip("fastmcp", reason="the MCP extra is not installed")
    import asyncio

    from dubforge.server import build_server

    real = {tool.name for tool in asyncio.run(build_server().list_tools())}
    # A doc may name a tool it is *explaining the absence of*; only a promise counts.
    negations = (
        "not implemented",
        "not added",
        "deliberately",
        "skipped",
        "rejected",
        "does not exist",
        "no such tool",
    )
    missing: dict[str, str] = {}
    for path in MARKDOWN:
        lines = path.read_text(encoding="utf-8").splitlines()
        heading = ""
        for index, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                heading = line
            for name in re.findall(
                r"`(dubforge_[a-z_]+|dub_video|plan_dub|probe_media|"
                r"get_qc|list_voices|list_providers|write_subtitles|"
                r"transcribe_video)`",
                line,
            ):
                if name in real:
                    continue
                # Prose wraps: a bullet may name a tool and explain its absence on the next
                # line, so look at a small window rather than the single line.
                # Markdown emphasis splits phrases ("**not** added"), so strip it, and take
                # the enclosing heading as context — "Not implemented on purpose" governs
                # every bullet beneath it.
                window = heading + " " + " ".join(lines[max(0, index - 2) : index + 3])
                window = window.lower().replace("*", "").replace("_", "")
                if any(word in window for word in negations):
                    continue
                missing.setdefault(name, f"{path.relative_to(ROOT)}:{index + 1}")
    assert not missing, f"documented but not implemented: {missing}"

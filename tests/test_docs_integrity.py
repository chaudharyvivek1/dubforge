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
import sys
from pathlib import Path

import pytest

from dubforge import __version__

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


def _inventory() -> dict[str, dict]:
    """The generated QC inventory: the authoritative list, read from `docs/qc-inventory.md`.

    Generated from `qc.py` by `scripts/gen_reference.py` and gated by `--check` in CI, so this
    is a reading of the *source*, not a second hand-maintained list. Comparing against it is
    what makes the count check structural rather than a search for a number.
    """
    text = (ROOT / "docs" / "qc-inventory.md").read_text(encoding="utf-8")
    rows: dict[str, dict] = {}
    for line in text.splitlines():
        match = re.match(r"^\| `([a-z_]+)` \| (fail|warn) \| (.+?) \|$", line.strip())
        if match:
            rows[match.group(1)] = {"severity": match.group(2), "when": match.group(3)}
    assert rows, "docs/qc-inventory.md has no rows — run scripts/gen_reference.py"
    return rows


def _stated_counts() -> dict[str, list[str]]:
    """Every page that states a QC count, with the phrase it used.

    Matches the *phrase*, never a bare number: "23" appears inside dates, versions and
    release notes, which is how a stale count survived four releases.
    """
    words = {
        23: "twenty-three",
        24: "twenty-four",
        20: "twenty",
        21: "twenty-one",
        15: "fifteen",
        10: "ten",
    }
    pattern = re.compile(
        r"\b(" + "|".join(words.values()) + r"|\d+)\s+(?:qc\s+)?checks\b", re.IGNORECASE
    )
    stated: dict[str, list[str]] = {}
    for page in _documentation_pages():
        if page.name.startswith("release-"):
            continue  # a dated record of what was true then, not a claim about now
        for match in pattern.finditer(page.read_text(encoding="utf-8")):
            try:
                label = str(page.relative_to(ROOT))
            except ValueError:  # a page outside the repo, e.g. a fixture in a test
                label = page.name
            stated.setdefault(label, []).append(match.group(0))
    return stated


def _documentation_pages() -> list[Path]:
    """Every markdown page under docs/, discovered rather than listed.

    A new page is covered the moment it exists — that is the whole point, because the stale
    "15 checks" row lived in a page the old test never read.
    """
    return sorted(path for path in (ROOT / "docs").glob("*.md"))


def _plain_line(line: str) -> str:
    """Strip markdown emphasis so "was **not** added" reads as "was not added"."""
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", line)).lower()


def _documented_checks() -> dict[str, str]:
    """Every QC check named in docs/quality.md, with the severity the table claims."""
    text = (ROOT / "docs" / "quality.md").read_text(encoding="utf-8")
    documented: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\| `([a-z_]+)` \| (fail|warn) \|", line.strip())
        if match:
            documented[match.group(1)] = match.group(2)
    return documented


def test_every_documented_check_exists_and_every_check_is_documented(demo_clip, tmp_path):
    """Compare names, not counts: a count can be right while a name is wrong."""
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
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "checks.mp4")
    produced = {check["check"]: check["declared"] for check in result.qc["checks"]}
    documented = _documented_checks()

    undocumented = set(produced) - set(documented)
    assert not undocumented, f"produced but not documented: {sorted(undocumented)}"

    # `multiple_audio_tracks` is conditional, so it may be documented without being produced.
    conditional = {"multiple_audio_tracks"}
    phantom = set(documented) - set(produced) - conditional
    assert not phantom, f"documented but never produced: {sorted(phantom)}"

    wrong_severity = {
        name: (produced[name], documented[name])
        for name in set(produced) & set(documented)
        if produced[name] != documented[name]
    }
    assert not wrong_severity, f"severity disagrees (code, docs): {wrong_severity}"


def test_each_check_appears_once_in_the_docs():
    """A duplicate row is how one check ends up documented with two different behaviours."""
    text = (ROOT / "docs" / "quality.md").read_text(encoding="utf-8")
    names = re.findall(r"^\| `([a-z_]+)` \| (?:fail|warn) \|", text, re.M)
    duplicates = {name for name in names if names.count(name) > 1}
    assert not duplicates, f"documented more than once: {sorted(duplicates)}"


def test_the_documented_count_matches_the_conditional_reality(demo_clip, tmp_path):
    """The prose says twenty (twenty-one for a multi-track source); the code must agree."""
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
    assert produced == 23, f"a single-track source produced {produced} checks, expected 23"

    words = {23: "twenty-three", 24: "twenty-four", 20: "twenty", 21: "twenty-one"}
    claim = words[produced]
    quality = (ROOT / "docs" / "quality.md").read_text(encoding="utf-8").lower()
    assert f"{claim} checks" in quality, (
        f"the pipeline produced {produced} checks but docs/quality.md does not say '{claim} checks'"
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


# ---------------------------------------------------------------------------------------
# Phase 6: the inventory is generated, and every page is checked against it.
# ---------------------------------------------------------------------------------------


def test_the_quality_table_matches_the_generated_inventory_exactly():
    """Both directions, names and severities — a table row is a claim about the code."""
    inventory = _inventory()
    documented = _documented_checks()
    missing = sorted(set(inventory) - set(documented))
    phantom = sorted(set(documented) - set(inventory))
    assert not missing, f"in the code but not documented in docs/quality.md: {missing}"
    assert not phantom, f"documented but not in the code: {phantom}"
    wrong = {
        name: (inventory[name]["severity"], documented[name])
        for name in set(inventory) & set(documented)
        if inventory[name]["severity"] != documented[name]
    }
    assert not wrong, f"severity disagrees (code, docs/quality.md): {wrong}"


def test_a_conditional_check_is_documented_as_conditional():
    """A check that depends on the *input* must be documented as conditional.

    Several checks sit inside `if original and output …`, which a normal run always satisfies —
    labelling those "conditional" in prose would be noise. `multiple_audio_tracks` depends on
    the source itself, so a reader deciding whether it applies to them needs to be told.
    """
    inventory = _inventory()
    input_dependent = {name for name, row in inventory.items() if "source_info" in row["when"]}
    assert "multiple_audio_tracks" in input_dependent, (
        "the inventory lost the input-dependent check"
    )
    conditional = input_dependent
    quality = (ROOT / "docs" / "quality.md").read_text(encoding="utf-8")
    for name in sorted(conditional):
        row = next(
            (line for line in quality.splitlines() if line.strip().startswith(f"| `{name}`")), ""
        )
        assert row, f"{name} has no row in docs/quality.md"
        assert (
            "conditional" in row.lower() or "only when" in row.lower() or "when the" in row.lower()
        ), f"docs/quality.md does not say that {name} is conditional: {row[:90]}"


def test_no_page_states_a_qc_count_that_the_code_does_not_produce(demo_clip, tmp_path):
    """The defect this phase exists for: one page said 15, another 23.

    The number is read from the *code*, then every page that states a count is required to
    agree. Release notes are excluded: they record what was true at the time.
    """
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
    produced = len(pipeline.dub(demo_clip, settings, output=tmp_path / "count.mp4").qc["checks"])
    words = {produced: {23: "twenty-three", 24: "twenty-four"}.get(produced, str(produced))}
    allowed = {str(produced), *words.values()}
    # a page may also mention the multi-track figure, one higher
    allowed |= {str(produced + 1), {23: "twenty-four"}.get(produced, str(produced + 1))}

    wrong: dict[str, list[str]] = {}
    for page, phrases in _stated_counts().items():
        bad = [phrase for phrase in phrases if phrase.split()[0].lower() not in allowed]
        if bad:
            wrong[page] = bad
    assert not wrong, f"a page states a QC count the code does not produce ({produced}): {wrong}"


def test_every_docs_page_is_scanned_by_these_tests():
    """The old test read one page by name, which is why a stale row survived four releases."""
    scanned = {page.name for page in _documentation_pages()}
    on_disk = {path.name for path in (ROOT / "docs").glob("*.md")}
    assert scanned == on_disk, "a docs page exists that the tests do not see"
    assert len(scanned) > 5, "the glob is not finding the pages"


def test_documented_provider_counts_match_the_registry():
    """A page may state a provider total; it must be the number the registry has."""
    from dubforge.providers import registry

    total = sum(len(registry.names(kind)) for kind in registry.REGISTRY)
    claims: dict[str, list[str]] = {}
    # Only *totals* are claims about the registry: "a probe for 7 adapters" is a subset and
    # perfectly true, so the pattern asks for the words that mean "all of them".
    pattern = re.compile(
        r"\b(?:all\s+)?(\d+)\s+adapters\s+behind\b|\ball\s+(\d+)\s+adapters\b", re.IGNORECASE
    )
    for page in _documentation_pages():
        if page.name.startswith("release-"):
            continue
        for match in pattern.finditer(page.read_text(encoding="utf-8")):
            claimed = int(match.group(1) or match.group(2))
            if claimed != total:
                claims.setdefault(page.name, []).append(match.group(0))
    assert not claims, f"the registry has {total} adapters; these pages disagree: {claims}"


def test_every_adapter_named_in_the_provider_page_exists():
    """The support table is a promise; a stale row promises an adapter that is gone."""
    from dubforge.providers import registry

    known = {name for kind in registry.REGISTRY for name in registry.names(kind)}
    text = (ROOT / "docs" / "providers.md").read_text(encoding="utf-8")
    named = set(re.findall(r"^\| `([a-z0-9_]+)` \|", text, re.M))
    assert named, "docs/providers.md has no adapter rows"
    unknown = sorted(named - known)
    assert not unknown, f"docs/providers.md names adapters that do not exist: {unknown}"


def test_cli_commands_named_in_the_docs_exist():
    """Every `dubforge <command>` in the docs must be a real subcommand."""
    from dubforge.cli import build_parser

    parser = build_parser()
    subcommands = set()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            subcommands |= set(choices)
    assert subcommands, "could not read the CLI subcommands"
    named: dict[str, set[str]] = {}
    pattern = re.compile(r"`dubforge ([a-z][a-z-]*)")
    for page in _documentation_pages():
        for match in pattern.finditer(page.read_text(encoding="utf-8")):
            command = match.group(1)
            if command not in subcommands:
                named.setdefault(page.name, set()).add(command)
    assert not named, f"docs name CLI commands that do not exist: {named}"


def test_the_committed_verification_record_is_what_the_writer_produces():
    """A stale record is a lie about what has been verified — it must not rot silently."""
    import json

    from dubforge import verify

    committed = json.loads(verify.RECORD.read_text(encoding="utf-8"))
    verdict = verify.validate(committed)
    assert verdict["ok"], f"docs/verification.json does not validate: {verdict['problems']}"
    assert committed.get("schema") == verify.SCHEMA, "the record has no schema version"

    if not committed.get("results"):
        # never run: the file must be exactly what `save()` writes today, so a version bump or
        # a schema change cannot leave it describing a release that no longer exists
        fresh = {
            "schema": verify.SCHEMA,
            "generated_at": None,
            "dubforge_version": __version__,
            "note": committed["note"],
            "results": [],
        }
        assert committed["dubforge_version"] == __version__, (
            f"the empty record says {committed['dubforge_version']} but this is {__version__}"
        )
        assert committed == fresh, "the committed record differs from what the writer produces"


# ---------------------------------------------------------------------------------------
# Regression tests for the defect this phase fixed: the checker must fail on it.
# ---------------------------------------------------------------------------------------


def test_the_checker_would_have_caught_the_stale_15_checks_row(tmp_path, monkeypatch):
    """Proof, not assertion: reconstruct the defect and watch the check fail.

    `docs/assessment.md` said "15 checks" in one table while another section said 23, and the
    old test — which read only `docs/quality.md` — never looked. This builds a page with the
    old row and requires the scanner to see it.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "assessment.md").write_text(
        "# Assessment\n\n| QC | 15 checks, metrics, `qc.json`, `report.md` | `qc.py` | stable |\n",
        encoding="utf-8",
    )
    (docs / "fine.md").write_text(
        "# Fine\n\nThe pipeline runs twenty-three checks.\n", encoding="utf-8"
    )
    # patch this module's own helper: the test module is imported as `test_docs_integrity`
    monkeypatch.setattr(
        sys.modules[__name__], "_documentation_pages", lambda: sorted(docs.glob("*.md"))
    )
    stated = _stated_counts()
    assert "assessment.md" in stated, "the scanner missed a page that states a count"
    assert stated["assessment.md"] == ["15 checks"]
    assert "fine.md" in stated and stated["fine.md"] == ["twenty-three checks"]


def test_the_quality_table_comparison_would_catch_a_phantom_row(tmp_path, monkeypatch):
    """A row for a check that no longer exists must fail, not pass quietly."""
    quality = tmp_path / "quality.md"
    quality.write_text(
        "| check | severity | passes when |\n|---|---|---|\n"
        "| `every_line_has_audio` | fail | audio exists |\n"
        "| `a_check_that_was_removed` | fail | nothing |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_documented_checks",
        lambda: dict(
            re.findall(
                r"^\| `([a-z_]+)` \| (fail|warn) \|", quality.read_text(encoding="utf-8"), re.M
            )
        ),
    )
    documented = _documented_checks()
    inventory = _inventory()
    phantom = sorted(set(documented) - set(inventory))
    assert phantom == ["a_check_that_was_removed"], (
        "the comparison did not notice a documented check that is not in the code"
    )


def test_the_documented_python_and_platform_support_matches_pyproject_and_ci():
    """README, pyproject and the CI matrix must agree about what is supported."""
    # A regex, not `tomllib`: this project supports Python 3.10, where `tomllib` does not
    # exist. CI on 3.10 caught that, which is the whole reason the matrix has a 3.10 row.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^requires-python\s*=\s*"([^"]+)"', pyproject, re.M)
    assert match, "pyproject.toml has no requires-python"
    requires = match.group(1)
    assert requires == ">=3.10", f"pyproject says {requires}; the docs and CI say 3.10+"

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for version in ("3.10", "3.11", "3.12"):
        assert f'"{version}"' in workflow, f"CI does not test Python {version}"
    for runner in ("ubuntu-latest", "macos-latest", "windows"):
        assert runner in workflow, f"CI does not test {runner}"

    platforms = (ROOT / "docs" / "platforms.md").read_text(encoding="utf-8")
    for name in ("Linux", "macOS", "Windows"):
        assert name in platforms, f"docs/platforms.md does not mention {name}"

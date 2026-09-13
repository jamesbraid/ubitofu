# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""The per-PR mutation gate judges only the lines a PR changed."""

from __future__ import annotations

import textwrap

from ci.mutation_gate import (
    changed_lines_from_diff,
    mutant_location,
    scoped_survivors,
    survivors_from_results,
)

DIFF = textwrap.dedent(
    """\
    diff --git a/src/ubitofu/hcl_writer.py b/src/ubitofu/hcl_writer.py
    --- a/src/ubitofu/hcl_writer.py
    +++ b/src/ubitofu/hcl_writer.py
    @@ -10,0 +11,3 @@ import re
    +def _render_value(value: object, indent: int) -> str:
    +    pad = "  " * indent
    +    return pad
    @@ -40 +43 @@ def render_resource(
    -    body = old()
    +    body = new()
    @@ -50,2 +53,0 @@ def render_resource(
    -    gone = 1
    -    gone = 2
    diff --git a/src/ubitofu/generate.py b/src/ubitofu/generate.py
    --- a/src/ubitofu/generate.py
    +++ b/src/ubitofu/generate.py
    @@ -636,9 +636 @@ def collect_generate_snapshot(
    -    blocking_findings.extend(
    +    findings = []
    """
)


def test_changed_lines_are_the_added_and_replaced_lines_of_each_file() -> None:
    lines = changed_lines_from_diff(DIFF)

    assert lines == {
        "src/ubitofu/hcl_writer.py": {11, 12, 13, 43},
        "src/ubitofu/generate.py": {636},
    }


def test_pure_deletions_add_no_lines() -> None:
    lines = changed_lines_from_diff(
        "--- a/x.py\n+++ b/x.py\n@@ -5,2 +4,0 @@ def f():\n-    a\n-    b\n"
    )

    assert lines == {"x.py": set()}


SHOW = textwrap.dedent(
    """\
    # ubitofu.hcl_writer.x__render_value__mutmut_4: survived
    --- src/ubitofu/hcl_writer.py
    +++ src/ubitofu/hcl_writer.py
    @@ -1,6 +1,6 @@
     def _render_value(value: object, indent: int) -> str:
         \"\"\"Render one attribute value; a multi-line value closes at ``indent``.\"\"\"
    -    pad = "  " * indent
    +    pad = "  " / indent
         if isinstance(value, VarRef):
             return value.expr
         if isinstance(value, bool):
    """
)

SOURCE = textwrap.dedent(
    """\
    import re


    def _q(s: str) -> str:
        return s


    def _render_value(value: object, indent: int) -> str:
        \"\"\"Render one attribute value; a multi-line value closes at ``indent``.\"\"\"
        pad = "  " * indent
        if isinstance(value, VarRef):
            return value.expr
        if isinstance(value, bool):
            return "true"
        return pad
    """
)


def test_mutant_location_maps_the_mutated_line_back_to_the_source_file() -> None:
    location = mutant_location(
        "ubitofu.hcl_writer.x__render_value__mutmut_4", SHOW, SOURCE
    )

    assert location == ("src/ubitofu/hcl_writer.py", 10)


def test_mutant_location_is_none_when_the_function_cannot_be_found() -> None:
    show = SHOW.replace("_render_value", "_vanished")

    assert mutant_location("ubitofu.hcl_writer.x__vanished__mutmut_4", show, SOURCE) is None


def test_survivors_are_read_from_the_results_listing() -> None:
    results = textwrap.dedent(
        """\
        Survived 🙁 (2)

            ubitofu.generate.x_parse_generated_resources__mutmut_3: survived
            ubitofu.hcl_writer.x__q__mutmut_1: survived

        Killed 🎉 (10)
            ubitofu.hcl_writer.x__q__mutmut_2: killed
        """
    )

    assert survivors_from_results(results) == [
        "ubitofu.generate.x_parse_generated_resources__mutmut_3",
        "ubitofu.hcl_writer.x__q__mutmut_1",
    ]


def test_scoped_survivors_keep_only_mutants_on_changed_lines_and_unlocatable_ones() -> None:
    changed = {"src/ubitofu/hcl_writer.py": {10, 11}}
    located = {
        "on-changed": ("src/ubitofu/hcl_writer.py", 10),
        "elsewhere": ("src/ubitofu/hcl_writer.py", 40),
        "other-file": ("src/ubitofu/generate.py", 10),
        "unknown": None,
    }

    scoped = scoped_survivors(located, changed)

    assert scoped == {
        "on-changed": ("src/ubitofu/hcl_writer.py", 10),
        "unknown": None,
    }


def test_target_fetch_uses_the_forge_token_only_when_one_is_set() -> None:
    from ci.mutation_gate import fetch_target_command

    plain = fetch_target_command("main", token=None)
    with_token = fetch_target_command("main", token="s3cret")

    assert plain[:2] == ["git", "fetch"]
    assert "credential.helper" not in " ".join(plain)
    assert with_token[:2] == ["git", "-c"]
    assert "credential.helper=" in with_token[2]
    assert "s3cret" not in " ".join(with_token)
    assert with_token[-2:] == ["origin", "main"]

"""Fake worker executables and the rule that every one names an independent source.

A fake imitates a real CLI, so it must be anchored to something the CLI really
produced: a captured output or a documentation excerpt, together with the CLI
version it came from. A URL alone is not enough.
"""

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HELPER_PATH = "tests/support/fake_executable.py"
SOURCE_KINDS = ("captured_output", "spec_excerpt")
_VERSION = re.compile(r"\d+(\.\d+)+([-+.][0-9A-Za-z.]+)?")
_EXECUTABLE_BITS = 0o111
_POSITIONAL_SOURCE_INDEX = 3


@dataclass(frozen=True)
class SourceRef:
    """Where a fake's behaviour comes from."""

    kind: str
    cli_name: str
    cli_version: str
    excerpt: str
    url: str = ""

    def __post_init__(self) -> None:
        """Reject a source that does not anchor the fake to a versioned excerpt."""
        problems = []
        if self.kind not in SOURCE_KINDS:
            problems.append(f"kind must be one of {SOURCE_KINDS}, got {self.kind!r}")
        if not self.cli_name.strip():
            problems.append("cli_name is empty")
        if not _VERSION.fullmatch(self.cli_version):
            problems.append(f"cli_version {self.cli_version!r} is not a concrete version")
        if not self.excerpt.strip():
            problems.append("excerpt is empty; a URL alone is not an independent source")
        if problems:
            raise ValueError("; ".join(problems))


class FakeExecutable:
    """A script that stands in for a worker CLI in tests."""

    def __init__(self, name: str, body: str, source: SourceRef) -> None:
        """Keep the shell ``body`` and the independent ``source`` it imitates."""
        self.name = name
        self.body = body
        self.source = source

    def write_to(self, directory: Path) -> Path:
        """Write the script into ``directory`` and make it runnable by its owner only."""
        path = directory / self.name
        path.write_text("#!/bin/sh\n" + self.body, encoding="utf-8")
        path.chmod(0o700)
        return path


def unsourced_fake_executables(sources: Mapping[str, str]) -> list[str]:
    """Report fakes built without a source, and executable files made by hand.

    ``sources`` maps a repository-relative path to the file text. The helper
    itself is exempt, because it is the one place allowed to set the bit.
    """
    findings = []
    for path, text in sorted(sources.items()):
        if path == HELPER_PATH:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError as error:
            findings.append(f"{path}: cannot be parsed ({error.msg})")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                finding = _call_finding(node)
                if finding:
                    findings.append(f"{path}:{node.lineno}: {finding}")
    return findings


def _call_finding(call: ast.Call) -> str:
    name = _called_name(call)
    if name == "FakeExecutable":
        positional = len(call.args) >= _POSITIONAL_SOURCE_INDEX
        has_source = positional or any(keyword.arg == "source" for keyword in call.keywords)
        return "" if has_source else "FakeExecutable without a source"
    if name == "chmod" and _sets_executable_bit(call):
        return "executable file made outside FakeExecutable"
    return ""


def _called_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _sets_executable_bit(call: ast.Call) -> bool:
    return any(
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, int)
        and arg.value & _EXECUTABLE_BITS
        for arg in call.args
    )

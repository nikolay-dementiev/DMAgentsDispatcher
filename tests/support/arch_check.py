"""Static architecture checks over source text, using only the standard library.

The checker takes a mapping of source paths (relative to the source root, for example
``agents_dispatcher/domain/model.py``) to source text, so it can be tested on synthetic
snippets and then pointed at the real tree. It is a convention gate: it cannot prove
arbitrary dynamic behaviour.
"""

import ast
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

ROOT = "agents_dispatcher"
ROOT_LAYER = "root"

LAYER_MATRIX: Mapping[str, frozenset[str]] = {
    ROOT_LAYER: frozenset(),
    "domain": frozenset({"domain"}),
    "application": frozenset({"application", "domain"}),
    "adapters": frozenset({"adapters", "application", "domain"}),
    "interfaces": frozenset({"interfaces", "application", "domain"}),
    "composition": frozenset({"composition", "adapters", "application", "domain", "interfaces"}),
}

_MAX_REEXPORT_DEPTH = 8


@dataclass(frozen=True)
class Violation:
    """One broken rule."""

    rule: str
    module: str
    detail: str
    line: int = 0


@dataclass(frozen=True)
class _Module:
    name: str
    is_package: bool
    tree: ast.Module


@dataclass(frozen=True)
class _Edge:
    source: str
    target: str
    line: int


def layer_of(module_name: str) -> str | None:
    """Return the layer of a dotted module name, or None for modules outside the package."""
    parts = module_name.split(".")
    if parts[0] != ROOT:
        return None
    if len(parts) > 1 and parts[1] in LAYER_MATRIX:
        return parts[1]
    return ROOT_LAYER


def _adapter_package(module_name: str) -> str | None:
    parts = module_name.split(".")
    return parts[2] if len(parts) > 2 else None  # noqa: PLR2004 - root, layer, package


def _parse(path: str, source: str) -> _Module:
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        return _Module(".".join(parts[:-1]), is_package=True, tree=ast.parse(source))
    return _Module(".".join(parts), is_package=False, tree=ast.parse(source))


def _absolute_base(module: _Module, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    package = module.name.split(".")
    if not module.is_package:
        package = package[:-1]
    keep = len(package) - (node.level - 1)
    if keep <= 0:
        return None
    base = ".".join(package[:keep])
    return f"{base}.{node.module}" if node.module else base


class ImportGraph:
    """The resolved import graph of a source tree."""

    def __init__(self, sources: Mapping[str, str]) -> None:
        """Parse every source and resolve its imports."""
        parsed = (_parse(path, source) for path, source in sources.items())
        self.modules = {module.name: module for module in parsed}
        self._bindings = self._collect_bindings()
        self.edges = list(self._collect_edges())

    def _collect_bindings(self) -> dict[str, dict[str, tuple[str, str]]]:
        result: dict[str, dict[str, tuple[str, str]]] = {}
        for module in self.modules.values():
            names: dict[str, tuple[str, str]] = {}
            for node in ast.walk(module.tree):
                base = _absolute_base(module, node) if isinstance(node, ast.ImportFrom) else None
                if isinstance(node, ast.ImportFrom) and base is not None:
                    for alias in node.names:
                        names[alias.asname or alias.name] = (base, alias.name)
            result[module.name] = names
        return result

    def _origin(self, base: str, name: str, depth: int = 0) -> str:
        """Return the module that really provides ``name`` imported from ``base``."""
        submodule = f"{base}.{name}"
        if submodule in self.modules:
            return submodule
        binding = self._bindings.get(base, {}).get(name)
        if binding is not None and depth < _MAX_REEXPORT_DEPTH:
            return self._origin(binding[0], binding[1], depth + 1)
        return base

    def _collect_edges(self) -> Iterator[_Edge]:
        for module in self.modules.values():
            for node in ast.walk(module.tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        yield _Edge(module.name, alias.name, node.lineno)
                elif isinstance(node, ast.ImportFrom):
                    base = _absolute_base(module, node)
                    if base is not None:
                        for alias in node.names:
                            yield _Edge(module.name, self._origin(base, alias.name), node.lineno)


def _layer_violations(graph: ImportGraph) -> Iterator[Violation]:
    for edge in graph.edges:
        source_layer, target_layer = layer_of(edge.source), layer_of(edge.target)
        if source_layer is None or target_layer is None or target_layer == ROOT_LAYER:
            continue
        if target_layer not in LAYER_MATRIX[source_layer]:
            detail = f"imports {edge.target} ({source_layer} -> {target_layer} is not allowed)"
            yield Violation("layer-matrix", edge.source, detail, edge.line)
        elif source_layer == target_layer == "adapters":
            own, other = _adapter_package(edge.source), _adapter_package(edge.target)
            if own is not None and other is not None and own != other:
                detail = f"imports {edge.target} (adapter package {own} -> {other})"
                yield Violation("adapter-reference", edge.source, detail, edge.line)


def _strongly_connected(graph: Mapping[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for following in sorted(graph.get(node, set())):
            if following not in index:
                visit(following)
                low[node] = min(low[node], low[following])
            elif following in on_stack:
                low[node] = min(low[node], index[following])
        if low[node] == index[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            components.append(component)

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return components


def _cycle_violations(graph: ImportGraph) -> Iterator[Violation]:
    adjacency: dict[str, set[str]] = {name: set() for name in graph.modules}
    for edge in graph.edges:
        if edge.target in adjacency:
            adjacency[edge.source].add(edge.target)
    for component in _strongly_connected(adjacency):
        members = sorted(component)
        if len(members) > 1 or members[0] in adjacency[members[0]]:
            yield Violation("cycle", members[0], "import cycle: " + " <-> ".join(members))


def check_architecture(sources: Mapping[str, str]) -> list[Violation]:
    """Return every violation found in the source tree, in a stable order."""
    graph = ImportGraph(sources)
    found = [*_layer_violations(graph), *_cycle_violations(graph)]
    return sorted(found, key=lambda v: (v.rule, v.module, v.line, v.detail))

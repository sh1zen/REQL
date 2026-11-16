"""Helpers for resolving symbols inside one parse result."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from .models import CodeParseResult, CodeSymbol


def is_computed_reference(target: str) -> bool:
    """Identify receiver expressions whose result type is absent from a name table."""
    return any(character in target for character in "()[]")


@dataclass(frozen=True, slots=True)
class SymbolResolution:
    target_id: str | None
    status: Literal["resolved", "ambiguous", "unresolved", "dynamic"]
    evidence: tuple[str, ...] = ()


class SymbolTable:
    """Resolve references within the parser's module and lexical scopes."""

    def __init__(self, result: CodeParseResult) -> None:
        self.by_qualified_name = {symbol.qualified_name: symbol for symbol in result.symbols}
        self.by_id = {symbol.id: symbol for symbol in result.symbols}
        self.module = result.module.name
        self.language = result.module.language.casefold()
        self._definitions: dict[str, list[CodeSymbol]] = defaultdict(list)
        for symbol in result.symbols:
            self._definitions[symbol.qualified_name].append(symbol)
        self.ambiguous_names = frozenset(name for name, candidates in self._definitions.items() if len(candidates) > 1)

    def resolve_call_target(self, target: str, *, caller: str | None = None) -> CodeSymbol | None:
        """Return a local declaration when its binding can be established."""
        result = self.resolve(target, caller=caller)
        return self.by_id.get(result.target_id) if result.target_id is not None else None

    def resolve(self, target: str, *, caller: str | None = None, callable_only: bool = False) -> SymbolResolution:
        """Track why a binding resolved and distinguish ambiguity from dynamic calls."""
        if is_computed_reference(target):
            return SymbolResolution(None, "dynamic")
        root, dot, member = target.partition(".")
        scopes = [caller] if caller else []
        while scopes and scopes[-1] != self.module:
            parent = self.by_qualified_name.get(scopes[-1])
            parent_name = parent.parent_qualified_name if parent else scopes[-1].rpartition(".")[0]
            if not parent_name or parent_name in scopes:
                break
            scopes.append(parent_name)
        if self.module not in scopes:
            scopes.append(self.module)
        owner = self.by_qualified_name.get(caller or "")
        if dot and owner and owner.kind in {"method", "async_method"}:
            decorators = {item.rsplit(".", 1)[-1] for item in owner.decorators}
            receiver = (root == "this" and self.language in {"javascript", "typescript", "tsx"}
                        or self.language == "python" and owner.args and root == owner.args[0]
                        and "staticmethod" not in decorators)
            if receiver:
                name = f"{owner.parent_qualified_name}.{member}"
                candidates = self._definitions.get(name, [])
                if not candidates:
                    return SymbolResolution(None, "unresolved", ("class receiver",))
                return self._select(candidates, "class receiver", callable_only)
        if target.startswith(f"{self.module}."):
            return self._select(self._definitions.get(target, []), "qualified name", callable_only)

        for scope in scopes:
            parent = self.by_qualified_name.get(scope)
            if parent:
                if root in parent.args:
                    return SymbolResolution(None, "dynamic", ("parameter binding",))
                if parent.kind == "class" and self.language == "python":
                    continue
            name = f"{scope}.{target}"
            if names := self._definitions.get(name):
                return self._select(names, "lexical scope", callable_only)
            if dot and self._definitions.get(f"{scope}.{root}"):
                return SymbolResolution(None, "dynamic", ("receiver binding",))
        return SymbolResolution(None, "unresolved")

    @staticmethod
    def _select(candidates: list[CodeSymbol], evidence: str, callable_only: bool) -> SymbolResolution:
        if len(candidates) > 1:
            return SymbolResolution(None, "ambiguous", (evidence,))
        if not candidates:
            return SymbolResolution(None, "unresolved")
        if callable_only and candidates[0].kind == "variable":
            return SymbolResolution(None, "dynamic", ("variable binding",))
        return SymbolResolution(candidates[0].id, "resolved", (evidence,))

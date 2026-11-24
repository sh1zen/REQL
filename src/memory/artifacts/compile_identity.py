"""Per-artifact compilation fingerprints."""
from __future__ import annotations

from functools import cached_property
import sys

from config import VERSION

from ..code_analysis.base import TreeSitterCodeParser, artifact_grammar
from ..code_analysis.catalog import grammar_packages
from ..code_analysis.parser_base import CodeParserRegistry
from ..domain.ids import stable_id
from .fingerprint import tree_sitter_versions
from .models import SourceArtifact


_COMPILER_CONTRACT_VERSION = 3


class ArtifactCompileIdentity:
    """Resolve and memoize the compiler contract for each artifact's parser."""

    def __init__(
        self, options_digest: str, *,
        scoped: bool = True, scoped_documents: bool = True, code_parsers: CodeParserRegistry | None = None,
    ) -> None:
        self.options_digest = options_digest
        self.scoped = scoped
        self.scoped_documents = scoped_documents
        self.code_parsers = code_parsers
        self._by_grammar: dict[str, str] = {}

    @cached_property
    def _custom_contract(self) -> str:
        """Invalidate custom parsers on any grammar-package change."""
        return stable_id(
            "compilation", "custom", VERSION, _COMPILER_CONTRACT_VERSION, self.options_digest,
            sorted(tree_sitter_versions().items()), sys.version_info[:2],
        )

    def current(self, artifact: SourceArtifact) -> str:
        """Return the identity to persist with successful compilation."""
        return self._contract(artifact) if self._scoped(artifact) else self._custom_contract

    def _scoped(self, artifact: SourceArtifact) -> bool:
        """Select built-in contracts even when callers supply their own parser registry."""
        if not self.scoped:
            return False
        if artifact.artifact_type != "code":
            return self.scoped_documents
        return self.code_parsers is None or type(self.code_parsers.parser_for(artifact)) is TreeSitterCodeParser

    def _contract(self, artifact: SourceArtifact) -> str:
        grammar = artifact_grammar(artifact) if artifact.artifact_type == "code" else ""
        existing = self._by_grammar.get(grammar)
        if existing is not None:
            return existing
        extractor_name: tuple[str, ...] = ()
        versions: tuple[tuple[str, str | None], ...] = ()
        if grammar:
            from ..code_analysis.factory import EXTRACTOR_BY_LANGUAGE

            extractor = EXTRACTOR_BY_LANGUAGE.get(grammar)
            if extractor is not None:
                extractor_name = (extractor.__module__, extractor.__qualname__)
            packages = tree_sitter_versions()
            versions = tuple((package, packages.get(package)) for package in grammar_packages(grammar))
        fingerprint = stable_id(
            "compilation", "scoped", VERSION, _COMPILER_CONTRACT_VERSION, self.options_digest,
            grammar, extractor_name, versions, sys.version_info[:2] if grammar == "python" else (),
        )
        self._by_grammar[grammar] = fingerprint
        return fingerprint

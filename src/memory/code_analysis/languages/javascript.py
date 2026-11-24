"""JavaScript Tree-sitter extraction."""
from __future__ import annotations

from typing import Any

from ...artifacts.context_scope import artifact_context_scope
from ..base import (
    TreeSitterExtractorBase,
    _children,
    _clean_type_text,
    _end_line,
    _field,
    _has_direct_child,
    _import,
    _last_quoted,
    _line,
    _named_children,
    _node_text,
    _owner,
    _params,
    _parent,
    _string_literal,
    _symbol,
    _text,
)
from ..models import CodeSymbol, SymbolKind

_TEST_MODULES = {"@jest/globals", "node:test", "vitest"}
_TEST_FUNCTIONS = {"it", "test"}
_TEST_MODIFIERS = {"concurrent", "only", "skip", "todo", "fails", "failing", "serial", "each", "skipIf", "runIf"}


class JavaScriptTreeSitterExtractor(TreeSitterExtractorBase):
    language_key = "javascript"
    tree_sitter_module = "tree_sitter_javascript"

    def _walk_root(self, root: Any) -> None:
        """Resolve supported test registration bindings before visiting calls."""
        self._test_functions: set[tuple[str, ...]] = set()
        if artifact_context_scope(self.artifact) == "test":
            self._test_functions.update((name,) for name in _TEST_FUNCTIONS)
            for node in _named_children(root):
                if node.type != "import_statement":
                    continue
                module = _string_literal(_node_text(self.source, _field(node, "source")))
                for name, alias in self._javascript_import_bindings(node):
                    binding = alias or name
                    self._test_functions.discard((binding,))
                    if module not in _TEST_MODULES:
                        continue
                    if name == "*":
                        self._test_functions.update((binding, function) for function in _TEST_FUNCTIONS)
                    elif name in _TEST_FUNCTIONS or (module == "node:test" and name == "default"):
                        self._test_functions.add((binding,))
        self._walk_javascript(root, [])

    def _walk_javascript(self, node: Any, scope: list[CodeSymbol]) -> None:
        node_type = str(getattr(node, "type", ""))
        if node_type == "comment":
            self.comments.append(_text(self.artifact, _owner(scope), _node_text(self.source, node), _line(node), _end_line(node), "comment"))
            return
        if node_type in {"import_statement", "export_statement"}:
            self._javascript_import(node)
        if node_type in {"function_declaration", "generator_function_declaration"}:
            symbol = self._javascript_function_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_javascript_children(node, [*scope, symbol])
                return
        if node_type == "class_declaration":
            symbol = self._javascript_class_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_javascript_children(node, [*scope, symbol])
                return
        if node_type in {"method_definition", "method_signature"}:
            symbol = self._javascript_method_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_javascript_children(node, [*scope, symbol])
                return
        if node_type in {"variable_declarator", "public_field_definition", "assignment_expression"}:
            symbol = self._javascript_variable_function_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_javascript_children(node, [*scope, symbol])
                return
        if node_type == "call_expression":
            self._record_call(node, scope, function_field="function")
            test = self._javascript_test_callback(node, scope)
            if test is not None:
                symbol, callback = test
                self.symbols.append(symbol)
                if callback is not None:
                    self._walk_javascript_children(callback, [*scope, symbol])
                return
        self._walk_javascript_children(node, scope)

    def _walk_javascript_children(self, node: Any, scope: list[CodeSymbol]) -> None:
        for child in _children(node):
            self._walk_javascript(child, scope)

    def _javascript_test_callback(self, node: Any, scope: list[CodeSymbol]) -> tuple[CodeSymbol, Any] | None:
        """Use literal test registrations as callback reference owners.

        See docs/PIPELINE.md#changing-an-extractor for versions and tests.
        """
        if not self._test_functions:
            return None
        callee = _field(node, "function")
        wrapped = callee is not None and callee.type == "call_expression"
        if wrapped:
            callee = _field(callee, "function")
        names = []
        while callee is not None and callee.type == "member_expression":
            names.append(_node_text(self.source, _field(callee, "property")))
            callee = _field(callee, "object")
        if callee is None or callee.type != "identifier":
            return None
        names.append(_node_text(self.source, callee))
        names.reverse()
        binding_length = 2 if tuple(names[:2]) in self._test_functions else 1
        modifiers = names[binding_length:]
        if (tuple(names[:binding_length]) not in self._test_functions
                or not _TEST_MODIFIERS.issuperset(modifiers) or (wrapped and not modifiers)):
            return None
        arguments = _field(node, "arguments")
        items = _named_children(arguments) if arguments is not None else []
        if not items or items[0].type != "string":
            return None
        name = _string_literal(_node_text(self.source, items[0]))
        if not name or len(name) > 160:
            return None
        callback = items[-1] if items[-1].type in {"arrow_function", "function_expression"} else None
        if callback is None and (len(items) != 1 or not modifiers or modifiers[-1] != "todo"):
            return None
        params = _params(self.source, _field(callback, "parameters")) if callback is not None else []
        symbol = _symbol(self.artifact, "function", _parent(self.module, scope), name,
                         _line(node), _end_line(node), args=params)
        symbol.metadata["test_case"] = True
        if modifiers:
            symbol.metadata["test_modifiers"] = modifiers
        return symbol, callback

    def _javascript_import(self, node: Any) -> None:
        source_node = _field(node, "source")
        module = _string_literal(_node_text(self.source, source_node)) if source_node is not None else None
        if module is None and str(getattr(node, "type", "")) == "import_statement":
            module = _last_quoted(_node_text(self.source, node))
        if module is None:
            for clause in _named_children(node):
                if clause.type == "export_clause":
                    for entry in _named_children(clause):
                        name = _node_text(self.source, _field(entry, "name"))
                        alias = _node_text(self.source, _field(entry, "alias")) or name
                        if name:
                            self.module.metadata.setdefault("local_exports", []).append([name, alias])
            return
        raw = _node_text(self.source, node).strip()
        exported = node.type == "export_statement"
        bindings = self._javascript_import_bindings(node)
        for name, alias in bindings or [(None, None)]:
            item = _import(self.artifact, module, name, alias, _line(node), raw)
            if exported:
                item.metadata["is_re_export"] = True
            elif not bindings:
                item.metadata["import_form"] = "side_effect"
            self.imports.append(item)

    def _javascript_import_bindings(self, node: Any) -> list[tuple[str, str | None]]:
        """Read binding names from imports and re-exports in source order."""
        bindings: list[tuple[str, str | None]] = []
        for child in _named_children(node):
            if child.type not in {"import_clause", "export_clause", "namespace_export"}:
                continue
            for entry in _named_children(child):
                if entry.type in {"import_specifier", "export_specifier", "named_imports"}:
                    specifiers = _named_children(entry) if entry.type == "named_imports" else [entry]
                    for specifier in specifiers:
                        name = _node_text(self.source, _field(specifier, "name"))
                        alias = _node_text(self.source, _field(specifier, "alias")) or None
                        if name:
                            bindings.append((name, alias))
                elif entry.type == "namespace_import":
                    bindings.append(("*", _node_text(self.source, _named_children(entry)[0])))
                elif entry.type == "identifier" and node.type != "export_statement":
                    bindings.append(("default", _node_text(self.source, entry)))
        return bindings

    def _javascript_class_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _field(node, "name")
        if name_node is None:
            return None
        base = _javascript_class_base(self.source, node)
        symbol = _symbol(self.artifact, "class", _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node), bases=[base] if base else [])
        return self._javascript_exported_symbol(symbol, node)

    def _javascript_function_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _field(node, "name")
        if name_node is None:
            return None
        kind: SymbolKind = "async_function" if _has_direct_child(node, "async") else "function"
        symbol = _symbol(self.artifact, kind, _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node), args=_params(self.source, _field(node, "parameters")), returns=_clean_type_text(_node_text(self.source, _field(node, "return_type"))))
        return self._javascript_exported_symbol(symbol, node)

    def _javascript_method_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _field(node, "name")
        if name_node is None:
            return None
        in_class = any(symbol.kind == "class" for symbol in scope)
        kind: SymbolKind = "async_method" if _has_direct_child(node, "async") else "method" if in_class else "function"
        return _symbol(self.artifact, kind, _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node), args=_params(self.source, _field(node, "parameters")), returns=_clean_type_text(_node_text(self.source, _field(node, "return_type"))))

    def _javascript_variable_function_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        """Extract function bindings, including static CommonJS/property assignments."""
        assignment = str(getattr(node, "type", "")) == "assignment_expression"
        value = _field(node, "right" if assignment else "value")
        if value is None or str(getattr(value, "type", "")) not in {"arrow_function", "function", "function_expression"}:
            return None
        name_node = _field(node, "left" if assignment else "name")
        if name_node is None:
            return None
        name = _javascript_binding_name(self.source, name_node) if assignment else _node_text(self.source, name_node)
        if not name:
            return None
        kind: SymbolKind = "async_function" if _has_direct_child(value, "async") else "function"
        symbol = _symbol(self.artifact, kind, _parent(self.module, scope), name, _line(node), _end_line(value), args=_params(self.source, _field(value, "parameters")), returns=_clean_type_text(_node_text(self.source, _field(value, "return_type"))))
        return self._javascript_exported_symbol(symbol, node.parent)

    @staticmethod
    def _javascript_exported_symbol(symbol: CodeSymbol, declaration: Any) -> CodeSymbol:
        """Record the public name from an enclosing export declaration."""
        parent = declaration.parent if declaration is not None else None
        if parent is not None and parent.type == "export_statement":
            symbol.metadata["export_name"] = "default" if _has_direct_child(parent, "default") else symbol.name
        return symbol


def _javascript_binding_name(source: bytes, node: Any) -> str | None:
    """Resolve a static assignment path without inventing computed receiver identities."""
    node_type = str(getattr(node, "type", ""))
    if node_type in {"identifier", "property_identifier", "this"}:
        return _node_text(source, node)
    if node_type == "member_expression":
        receiver = _field(node, "object")
        property_node = _field(node, "property")
        if receiver is not None and property_node is not None:
            owner = _javascript_binding_name(source, receiver)
            name = _javascript_binding_name(source, property_node)
            if owner and name:
                return f"{owner}.{name}"
    return None


def _javascript_class_base(source: bytes, node: Any) -> str | None:
    superclass = _field(node, "superclass")
    if superclass is not None:
        return _node_text(source, superclass).strip() or None
    for child in _named_children(node):
        if str(getattr(child, "type", "")) == "class_heritage":
            value = _node_text(source, child).replace("extends", "", 1).strip()
            return value or None
    return None

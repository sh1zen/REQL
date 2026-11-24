# Python API

Install `reql` in your Python environment and import the public API from
the [`reql` module](../src/reql.py). Use `MemoryGraph` to index a project or query
an existing index.

The compatibility imports `reql.config` and `reql.query` resolve to
`memory.config` and `memory.query` at runtime. The standalone `reql` module
falls outside the `py.typed` package convention.

## Search a project

Build the index with the [quick start](../README.md#quick-start). Run the
example from the project root. In the REQL checkout, it finds
`MemoryGraph.locate`. In another project, search for one of its symbols
or features.

```python
from reql import MemoryGraph, QueryContextRequest

graph = MemoryGraph.open(".reql/memory.reql", read_only=True)
try:
    request = QueryContextRequest(
        text="MemoryGraph locate",
        scopes=frozenset({"code"}),
    )
    print(graph.query_context_result(request).to_dict())
finally:
    graph.close()
```

`QueryContextRequest` accepts a `RetrievalBudget` to limit results. The response
includes the source revision, freshness, confidence and context payload. See
[retrieval options and response fields](CLI.md#retrieval-commands).

## Reference

| Method | Use it to |
| --- | --- |
| `compile_project(path)` | Build or refresh a project's index using the graph's scan and cache configuration |
| `query_context_result(request)` | Retrieve bounded context as a typed result |
| `find_tests_for(file=...)` or `find_tests_for(symbol_id=...)` | Look up indexed test evidence |
| `failure_context(query, files=[...])` | Recover recorded failed approaches for affected files |
| `workspace_status(path)` | Compare current files with the last compiled revision |
| `project_report(path, output_dir=...)` | Write Markdown reports while reading the graph |

Use `MemoryGraph.open(path, read_only=True, allow_empty_read_only=True)` to
generate scan and cache reports before creating a graph store.

By default, `MemoryGraph.open` raises `StorageBusyError` if an incompatible
lock is held. Pass `wait_for_lock=True` to wait until the native lock becomes
available. Pass `lock_timeout_seconds` for a bounded wait.
See [storage locking](STORAGE.md#readerwriter-locking).

See the [MemoryGraph source reference](../src/api/memory_graph.py) for full method
signatures and the [configuration guide](CONFIGURATION.md#loader) for typed
settings. For tool clients, see the [MCP API](MCP.md#tools).

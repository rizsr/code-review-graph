# Setting Up code-review-graph for Dynamics 365 F&O / X++

This guide covers installing and using `code-review-graph` against a D365 Finance & Operations
extension repository. Once set up, AI coding tools (Claude Code, Copilot, Cursor, etc.) can
query the indexed graph instead of scanning thousands of XML files on every request.

All commands below are for **Windows Command Prompt** (`cmd.exe`).

---

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed
- Git installed and on PATH
- `code-review-graph` source cloned locally (e.g. `C:\GitRepos\code-review-graph`)
- Your D365 extension repository cloned locally (e.g. `C:\GitRepos\RARnDInitiatives`)
- *(Optional but recommended)* The Microsoft base packages installed locally under
  `PackagesLocalDirectory` — this enables cross-reference resolution into base platform code

---

## 1. Build the graph (first time)

Open **Command Prompt**, navigate to your D365 repo, and run:

```cmd
cd C:\GitRepos\RARnDInitiatives

C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph build --xpp-base-root "C:\Users\<you>\AppData\Local\Microsoft\Dynamics365\10.0.2527.78\PackagesLocalDirectory"
```

Replace `<you>` with your Windows username.

This will:
1. Walk all `Metadata\<Package>\<Model>\Ax*\*.xml` files in your repo
2. Parse embedded X++ from each metadata XML
3. Store the graph in `.code-review-graph\graph.db` inside your repo (excluded from git)
4. Run the X++ resolver to link extension → base class references and generate WRAPS edges

> **Note:** The first build takes a few minutes — it walks both your repo and the base packages
> directory (typically 350k+ files). Subsequent builds are much faster.

**Expected repo layout:**

```
Metadata\
  <Package>\
    <Model>\
      AxClass\MyClass.xml
      AxTable\SalesLineExtension.xml
      AxForm\SalesOrder.xml
      AxEventSubscription\OnInserted.xml
      ...
```

Check what was built:

```cmd
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph status
```

---

## 2. Wire up the MCP server to your AI tool

Still from inside your D365 repo, run:

```cmd
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph install
```

This auto-detects which AI tools you have installed (Claude Code, Copilot, Cursor, Windsurf,
etc.) and writes the correct MCP configuration for each one.

**Restart your editor/AI tool after running this.**

To target a specific tool only:

```cmd
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph install --platform claude-code
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph install --platform cursor
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph install --platform copilot
```

---

## 3. Configure the X++ base root

The base root is `PackagesLocalDirectory` from your local D365 install. It lets the graph
resolve references from your extension code into Microsoft's base classes and tables.

**Auto-detection:** If D365 is installed under the standard path
(`%LOCALAPPDATA%\Microsoft\Dynamics365\<version>\PackagesLocalDirectory`), the tool finds it
automatically — no configuration needed.

**To set it explicitly** (pass it on the `build` command as shown in step 1):

```cmd
... code-review-graph build --xpp-base-root "C:\Users\<you>\AppData\Local\Microsoft\Dynamics365\10.0.2527.78\PackagesLocalDirectory"
```

**Via environment variable** (useful in CI):

```cmd
set CRG_XPP_BASE_ROOTS=C:\...\PackagesLocalDirectory
```

**Multiple roots** (semicolon-separated):

```cmd
set CRG_XPP_BASE_ROOTS=C:\...\PackagesLocalDirectory;D:\...\AnotherRoot
```

---

## 4. Incremental update (after making changes)

After changing, adding, or deleting XML files, run from your repo directory:

```cmd
cd C:\GitRepos\RARnDInitiatives
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph update
```

This detects changed files via `git diff HEAD~1`, re-parses only those files, and re-runs
the X++ resolver. Much faster than a full build.

---

## 5. What gets indexed

### Metadata objects

All recognized `Ax*` folder types are parsed, including:

| Folder | What it produces |
|--------|-----------------|
| `AxClass` | `Class` node + `Function` nodes per method |
| `AxTable` / `AxTableExtension` | `Type` node + `Field` nodes + relation edges |
| `AxForm` / `AxFormExtension` | `Type` node + datasource methods + control event methods |
| `AxQuery` / `AxQuerySimpleExtension` | `Type` node + table references |
| `AxView` / `AxViewExtension` | `Type` node + table references |
| `AxMap` / `AxMapExtension` | `Type` node + field/mapping references |
| `AxEnum` / `AxEnumExtension` | `Type` node + `Field` nodes per enum value |
| `AxEdt` / `AxEdtExtension` | `Type` node + EDT inheritance |
| `AxEventSubscription` | `HANDLES` edge with `Pre`/`Post`/`Delegate` annotation |

### Edge kinds

| Edge | Meaning |
|------|---------|
| `EXTENDS` | Class / table CoC extension (`[ExtensionOf(classStr(Foo))]`) |
| `WRAPS` | CoC method wrapping a base method (generated by resolver) |
| `IMPLEMENTS` | Class implementing an interface |
| `INHERITS` | Table / EDT inheritance chain |
| `HANDLES` | Event subscriber → publisher |
| `CALLS` | Method calling another method (static `::` and instance `.`) |
| `ACCESSES` | Method selecting from / DML on a table; `tableNum`/`fieldNum` refs |
| `REFERENCES` | Compile-time string references (`classStr`, `tableStr`, `formStr`, ...) |
| `CONTAINS` | Parent artifact → child node |

### Extracted X++ patterns

- `select [mods] Table` and `select Field1, Field2 from Table`
- `while select`, `insert_recordset`, `update_recordset`, `delete_from`
- `join`, `exists join`, `outer join`
- `sum(f)`, `count(f)`, `max(f)` aggregates; `order by` / `group by` fields
- `tableNum(T)` / `fieldNum(T, F)` — QueryBuildDataSource / QueryBuildRange
- `new SysDaQueryObject(...)` and related SysDa API calls
- `[DataEventHandler(...)]`, `[FormDataSourceEventHandler(...)]`, `[FormControlEventHandler(...)]`
- `[ExtensionOf(classStr(...))]`, `implements`, `extends`
- All `*Str()` compile-time functions: `classStr`, `tableStr`, `formStr`, `fieldStr`, `methodStr`, `enumStr`, etc.

---

## 6. Querying the graph

Once built and installed, use these patterns inside your AI tool:

```
# All classes that extend SalesTable
query_graph(target="SalesTable", pattern="extensions_of")

# All CoC wrappers for a base method
query_graph(target="SalesTable.validateWrite", pattern="wrapped_by")

# All event handlers registered for a publisher method
query_graph(target="SalesTable.onInserted", pattern="handlers_for")

# All methods that SELECT from or DML on a table
query_graph(target="SalesTable", pattern="accesses_of")

# All callers of a method
query_graph(target="MyClass.run", pattern="callers_of")

# Impact radius of changing a method
get_impact_radius(node="MyClass.run")

# Semantic search
semantic_search_nodes_tool(query="sales order validation")
```

Or from the command line:

```cmd
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph status
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph detect-changes
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph visualize
```

---

## 7. Typical daily workflow

```cmd
rem Morning: update the graph with any overnight changes
cd C:\GitRepos\RARnDInitiatives
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph update

rem While coding: AI tool queries the graph automatically via MCP

rem Before committing: review your changes
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph detect-changes
```

---

## 8. Troubleshooting

**`program not found` when running `code-review-graph`**

The `code-review-graph` command is not on your PATH. Always use the full `uv run --project` form
shown in this guide:

```cmd
C:\Users\<you>\.local\bin\uv.exe run --project "C:\GitRepos\code-review-graph" code-review-graph <command>
```

**Graph not found / MCP server fails to start**

Make sure you ran `build` at least once from the repo root. The database is stored at
`.code-review-graph\graph.db` relative to the repo root. Also confirm the `install` step
completed and you restarted your AI tool.

**X++ XML files not being parsed**

The parser only recognises files whose path contains `Metadata` or `PackagesLocalDirectory`
and whose immediate parent folder matches a known `Ax*` pattern. Verify your directory
structure matches the expected layout shown in section 1.

**Base root not detected automatically**

Check that D365 is installed under `%LOCALAPPDATA%\Microsoft\Dynamics365\`. If it is installed
elsewhere, pass `--xpp-base-root` explicitly or set `CRG_XPP_BASE_ROOTS`.

**Slow first build**

The base index walk over `PackagesLocalDirectory` (typically 350k+ files) takes 30–90 seconds
on first run. The index is cached for the process lifetime. Subsequent resolver runs in the
same session are fast.

**Windows file lock errors during teardown**

Known issue with SQLite WAL files on Windows + Python 3.14. Does not affect correctness.
See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the workaround.

---

## Further reading

- [USAGE.md](USAGE.md) — general usage guide
- [COMMANDS.md](COMMANDS.md) — full MCP tool and CLI reference
- [FEATURES.md](FEATURES.md) — all features
- [XPP-HANDOFF.md](XPP-HANDOFF.md) — implementation notes and development history for the X++ parser

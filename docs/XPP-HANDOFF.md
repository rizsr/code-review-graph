# X++ / D365 FO Support Handoff

## Goal

Add X++ / Dynamics 365 Finance & Operations support to `code-review-graph` in a way that reduces token consumption for code agents by indexing the D365 metadata model and embedded X++ code instead of forcing broad XML/file scans.

The key discovery is that in the target repos, X++ source is primarily embedded inside metadata XML files, not standalone `.xpp` files.

Relevant repo shapes:

- Extension/source repos:
  - `Metadata\<Package>\<Model>\Ax*\*.xml`
- Microsoft base code on the machine:
  - `...\PackagesLocalDirectory\<Package>\<Model>\Ax*\*.xml`

Examples used during planning:

- `C:\GitRepos\RARnDInitiatives\Metadata\RnD\RnD`
- `C:\Users\Adminb76b72ac39\AppData\Local\Microsoft\Dynamics365\10.0.2527.78\PackagesLocalDirectory`

## What Was Implemented

### 1. XML-first X++ detection and parsing

Implemented in:

- [code_review_graph/parser.py](/abs/path/C:/GitRepos/code-review-graph/code_review_graph/parser.py:1)

Key behavior:

- `.xml` is **not** globally treated as a language.
- D365 metadata XML is detected only when the path matches known D365 layout markers:
  - `Metadata`
  - `PackagesLocalDirectory`
- Current metadata object folder detection includes:
  - `AxClass`
  - `AxTable`
  - `AxTableExtension`
  - `AxForm`
  - `AxFormExtension`
  - `AxMap`
  - `AxMapExtension`
  - `AxQuery`
  - `AxQuerySimpleExtension`
  - `AxView`
  - `AxViewExtension`
  - `AxDataEntityView`
  - `AxDataEntityViewExtension`
  - `AxEnum`
  - `AxEnumExtension`
  - `AxEdt`
  - `AxEdtExtension`
  - `AxEventSubscription`

New parser path:

- `detect_language()` returns `xpp-metadata` for supported D365 XML files.
- `parse_bytes()` dispatches to `_parse_xpp_metadata()` for those files.

What `_parse_xpp_metadata()` currently does:

- Parses metadata XML with `xml.etree.ElementTree`
- Emits:
  - `File` node with language `xpp-metadata`
  - primary artifact node (`Class` or `Type`)
  - embedded method nodes with language `xpp`
- Extracts package/model/object-type metadata into `extra`
- Extracts embedded X++ from:
  - `<SourceCode><Declaration><![CDATA[...]]></Declaration>`
  - `<SourceCode><Methods><Method><Source><![CDATA[...]]]>`

### 2. Initial embedded X++ extraction

Current snippet parsing is heuristic/regex-based, not grammar-based.

Implemented extraction includes:

- class declarations
- `extends`
- `[ExtensionOf(...)]`
- method signatures
- static calls like `Class::method()`
- compile-time functions:
  - `classStr`
  - `tableStr`
  - `formStr`
  - `fieldStr`
  - `methodStr`
  - `enumStr`
  - `identifierStr`
  - `queryStr`
  - `mapStr`
  - `extendedTypeStr`
- simple data-access detection:
  - `select`
  - `while select`
  - `insert_recordset`
  - `update_recordset`
  - `delete_from`
- `next <method>()` marker for Chain of Command wrapping

### 3. New edge kinds and query support

Implemented edges:

- `EXTENDS`
- `WRAPS`
- `HANDLES`
- `ACCESSES`

Implemented query patterns in:

- [code_review_graph/tools/query.py](/abs/path/C:/GitRepos/code-review-graph/code_review_graph/tools/query.py:1)

Added query patterns:

- `extensions_of`
- `wrapped_by`
- `handlers_for`
- `accesses_of`

Also updated MCP tool docs in:

- [code_review_graph/main.py](/abs/path/C:/GitRepos/code-review-graph/code_review_graph/main.py:1)

### 4. X++ resolver and lazy base-code loading

Implemented in:

- [code_review_graph/xpp_resolver.py](/abs/path/C:/GitRepos/code-review-graph/code_review_graph/xpp_resolver.py:1)

Current resolver behavior:

- Resolves targets from:
  - `EXTENDS`
  - `REFERENCES`
  - `ACCESSES`
  - `HANDLES`
- Resolves local artifacts first
- If not found locally, lazily searches configured base roots for matching artifact XML and parses just those files
- Generates `WRAPS` edges for CoC-style methods when:
  - the method had `next ...`
  - the extension target is known
  - a base method with the same name exists

### 5. User-scoped X++ base-root configuration

Implemented in:

- [code_review_graph/xpp_config.py](/abs/path/C:/GitRepos/code-review-graph/code_review_graph/xpp_config.py:1)

Behavior:

- User-scoped config file:
  - `~/.code-review-graph/config.json`
- Stores:
  - `xpp_base_roots`
- Fallback sources:
  - `CRG_XPP_BASE_ROOTS`
  - auto-detection under:
    - `C:\Users\<user>\AppData\Local\Microsoft\Dynamics365\*\PackagesLocalDirectory`

### 6. Build/update integration

Integrated in:

- [code_review_graph/incremental.py](/abs/path/C:/GitRepos/code-review-graph/code_review_graph/incremental.py:1)
- [code_review_graph/tools/build.py](/abs/path/C:/GitRepos/code-review-graph/code_review_graph/tools/build.py:1)
- [code_review_graph/cli.py](/abs/path/C:/GitRepos/code-review-graph/code_review_graph/cli.py:1)
- [code_review_graph/main.py](/abs/path/C:/GitRepos/code-review-graph/code_review_graph/main.py:1)

Current behavior:

- `full_build()` and `incremental_update()` now accept `xpp_base_roots`
- X++ resolver runs after build/update
- CLI now supports:
  - `code-review-graph build --xpp-base-root <path>`
  - `code-review-graph update --xpp-base-root <path>`
- `build_or_update_graph_tool()` also accepts `xpp_base_root`

## Tests Added / Updated

New tests:

- [tests/test_xpp.py](/abs/path/C:/GitRepos/code-review-graph/tests/test_xpp.py:1)

Coverage added there:

- D365 XML detection
- embedded X++ parsing from metadata XML
- metadata reference extraction
- lazy loading of external Microsoft base artifact
- `WRAPS` generation
- new query patterns:
  - `extensions_of`
  - `wrapped_by`
  - `accesses_of`

Updated tests:

- [tests/test_cli.py](/abs/path/C:/GitRepos/code-review-graph/tests/test_cli.py:1)
- [tests/test_incremental.py](/abs/path/C:/GitRepos/code-review-graph/tests/test_incremental.py:1)

## Validation Status

Focused validation completed successfully.

Commands used:

```powershell
$env:UV_CACHE_DIR='C:\GitRepos\code-review-graph\.uv-cache'
$env:UV_PYTHON_INSTALL_DIR='C:\GitRepos\code-review-graph\.uv-python'
& 'C:\Users\Adminb76b72ac39\.local\bin\uv.exe' run pytest tests/test_xpp.py tests/test_cli.py tests/test_incremental.py -q
```

Result:

- `72 passed, 1 warning`

Additional shared-surface validation:

```powershell
$env:UV_CACHE_DIR='C:\GitRepos\code-review-graph\.uv-cache'
$env:UV_PYTHON_INSTALL_DIR='C:\GitRepos\code-review-graph\.uv-python'
& 'C:\Users\Adminb76b72ac39\.local\bin\uv.exe' run pytest tests/test_tools.py -k "TestQueryGraphCallTargetFallbacks or TestBuildPostprocess" -q
```

Result:

- `5 passed, 75 deselected, 1 warning`

## Known Non-X++ Test Issue

A broader run including all of `tests/test_tools.py` exposed a separate Windows/CPython 3.14 SQLite temp-file teardown issue:

- `PermissionError: [WinError 32] The process cannot access the file because it is being used by another process`

This showed up in temp DB cleanup during teardown and does **not** appear tied to the X++ changes directly.

This should be investigated separately if broader Windows/3.14 validation is needed.

## Git Commits

Current work is saved in:

- `2607ac4 Add XML-first X++ metadata support`
- `d4662f1 Fix X++ access query coverage`

## What Is Still Missing For Full X++

The current implementation is a solid base, but it is **not** full X++ support yet.

### 1. More metadata object coverage

Still needed across additional `Ax*` folders, for example:

- menus / menu items
- security artifacts
- services / service groups
- reports
- workflow artifacts
- pages / parts / cues
- label/resources/macros/references
- more data entity and aggregate families

Each family likely needs explicit extraction rules from XML.

### 2. Better X++ syntax understanding

Current parser is heuristic only.

Still needed:

- more reliable method signature parsing
- instance-call inference
- attributes beyond `ExtensionOf`
- interfaces
- inheritance variants
- macros
- exception constructs
- more control-flow robustness
- form methods / datasource methods / control methods

### 3. Better data-access semantics

Still needed:

- full `select` forms
- `exists join`, `notexists join`, `outer join`
- `group by`, aggregates
- `firstOnly`, `forUpdate`, `crossCompany`, etc.
- SysDa coverage
- `Query*` object semantics
- mapping access edges to exact table/query/view artifacts with higher confidence

### 4. Stronger extension and CoC resolution

Still needed:

- better signature-aware method matching
- more extension target kinds
- more reliable `next` resolution
- handling more extension naming/layout variations

### 5. Better event support

Still needed:

- form control events
- datasource events
- attribute-based handler patterns
- richer `AxEventSubscription` extraction
- canonical publisher/member resolution

### 6. Richer metadata graph extraction

Still needed from XML:

- table fields
- relations
- indexes
- field groups
- form controls
- form datasources
- query datasource trees
- enum values
- EDT inheritance / references

Some of these may deserve explicit child nodes instead of only `REFERENCES`.

### 7. Real-repo validation on Microsoft + extension code

Still needed:

- run against large real D365 repos
- measure parse coverage
- check false positives / false negatives
- check token-savings improvements
- validate incremental update behavior on real package trees

## Suggested Next Priority

Best next steps, in order:

1. Deepen `AxTable`, `AxForm`, `AxQuery`, `AxView`, and event extraction
2. Improve X++ method/call/data-access parsing
3. Expand resolver coverage for more compile-time functions and extension targets
4. Run large-repo validation against the local `PackagesLocalDirectory` plus one real extension repo

## Resume Checklist

When resuming:

1. Ensure `uv` is on PATH or call it directly:
   - `C:\Users\Adminb76b72ac39\.local\bin\uv.exe`
2. Set workspace-local `uv` dirs before tests:

```powershell
$env:UV_CACHE_DIR='C:\GitRepos\code-review-graph\.uv-cache'
$env:UV_PYTHON_INSTALL_DIR='C:\GitRepos\code-review-graph\.uv-python'
```

3. Start from the current commits:
   - `2607ac4`
   - `d4662f1`
4. Re-run the focused validation first:

```powershell
& 'C:\Users\Adminb76b72ac39\.local\bin\uv.exe' run pytest tests/test_xpp.py tests/test_cli.py tests/test_incremental.py -q
```

5. Then expand implementation coverage by artifact family.

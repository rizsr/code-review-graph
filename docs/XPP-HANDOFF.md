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

## Session 2 — What Was Completed (commit `e6f6511`)

All four suggested next priorities were addressed.

### 1. AxTable / AxForm / AxQuery / AxView / AxMap / AxEventSubscription extraction (DONE)

- **AxTable**: `Field` child nodes (name + type), `<Relations>/<AxTableRelation>/<RelatedTable>` → `REFERENCES(table_relation)`, `<Extends>` → `INHERITS`
- **AxForm**: `<DataSources>/<AxFormDataSource>/<Table>` → `REFERENCES(datasource_table)`, datasource method nodes extracted with full CALLS/ACCESSES tracing
- **AxQuery / AxQuerySimpleExtension**: recursively finds `<AxQuerySimpleDataSource>/<Table>` → `REFERENCES(query_table)`
- **AxView / AxDataEntityView and extensions**: same pattern → `REFERENCES(view_table)`
- **AxMap**: `Field` child nodes from `<AxMapField>`, `<Mappings>/<AxMapMapping>/<MappingTable>` → `REFERENCES(map_table)`
- **AxEventSubscription**: replaced generic tag-walk with structured `<Publisher>.<PublisherMethod>` → `HANDLES(event)`, `<EventHandler>` → `REFERENCES(class)`
- **New `_extract_xpp_artifact_children`** method for Field/method child node emission
- **`_XPP_METADATA_OBJECT_KINDS`** expanded from 18 to 68 entries — covers all `Ax*` folders seen in the real repo (AxMenu*, AxMenuItem*, AxSecurityRole*, AxWorkflow*, AxReport, AxTile, AxPage, AxKPI, AxService*, AxAggregate*, AxCompositeDataEntityView, AxFormPart, AxConfigurationKey, AxLicenseCode, AxReference, AxRuleSet, etc.)

### 2. Better X++ syntax parsing (DONE)

- **`_XPP_COMPILETIME_RE`**: added `dataEntityStr`, `menuStr`, `menuItemDisplayStr`, `menuItemOutputStr`, `menuItemActionStr`, `reportStr`, `ssrsReportStr`, `securityRoleStr`, `securityDutyStr`, `securityPrivilegeStr`, `workflowStr`, `configurationKeyStr`, `licenseCodeStr`, `tileStr`, `pageStr`, `resourceStr`, `varStr`
- **`_XPP_JOIN_RE`**: new regex for `exists join`, `notexists join`, `outer join`, `join` table capture → `ACCESSES(join)` edges
- **`_XPP_IMPLEMENTS_RE`** + updated **`_XPP_DECL_RE`**: `implements IFace1, IFace2` → `IMPLEMENTS` edges per interface
- **`_XPP_DECL_RE`**: extended to match `interface` keyword alongside `class`
- **`_XPP_KEYWORDS`**: extended with common X++ built-ins (super, this, null, true, false, str, int, real, boolean, retry, crosscompany, firstonly, maxof, etc.) to suppress noise in call extraction
- **`XPP_METADATA_OBJECT_KINDS`**: public alias added so resolver can import without using private name

### 3. Resolver coverage expansion (DONE)

- **`_XPP_ARTIFACT_FOLDERS`**: 20+ new ref-kind → folder mappings: `table_relation`, `datasource_table`, `query_table`, `view_table`, `join`, `map_table`, `event`, `field`, `securityrole`, `securityduty`, `securityprivilege`, `workflow`, `report`, `ssrsreport`, `menu`, `menuitemdisplay/output/action`, `configurationkey`, `tile`, `page`, `resource`
- **Resolution loop**: `INHERITS` and `IMPLEMENTS` edges now resolved in addition to EXTENDS/REFERENCES/ACCESSES/HANDLES
- **`_find_local_artifact`**: now resolves `Field` nodes (for `fieldStr` refs and table fields) in addition to Function/Class/Type
- **Performance**: `_get_base_index()` pre-indexes all Ax* XML files by artifact name once per process using module-level cache keyed on base roots; converts N × rglob(356k files) to 1 × walk + O(1) dict lookups

### 4. Real-repo validation (DONE)

Validated against `C:\GitRepos\RARnDInitiatives\Metadata\RnD\RnD` (real D365 extension repo):

- **30 files parsed, 0 errors**
- Edge breakdown: CALLS 234, CONTAINS 116, REFERENCES 93, ACCESSES 9, INHERITS 8, IMPLEMENTS 2
- IMPLEMENTS edges confirmed live on real extension code (`RndMultiTaskJobController implements BatchRetryable`)
- All 50+ recognized Ax* folder types detected without error

Test suite: **81 passed** (was 72), +9 new test cases in `TestXppArtifactDepth` and `TestXppSyntaxParsing`.

---

## What Is Still Missing For Full X++

The current implementation is substantially deeper, but still not full X++ support.

### 1. Instance-call inference — **DONE in session 3** (`ce1bfc1`)

### 2. Better data-access semantics — **DONE in session 4** (`aecc75e`)

Remaining gap: `select Field, Field from Table` syntax and `Query*` object table extraction (see session 4 section).

### 3. Richer form/control extraction — **DONE in session 3** (`65f51aa`)

### 4. Table/EDT inheritance + field groups — **DONE in session 3** (`3d45e67`)

### 5. Stronger extension and CoC resolution — **DONE in session 4** (`e4220b8`)

### 6. Better event support — **DONE in session 4** (`13a25c6`)

### 7. Full-pipeline large-repo validation with base roots — **DONE in session 3** (`e84b66e`)

See session 3 and 4 sections for details. WRAPS edge count from large-repo run still needs measurement (see resume checklist step 5).

---

## Session 3 — What Was Completed (commits `ce1bfc1` → `e84b66e`)

All four items from the session 2 resume checklist were addressed.

### 1. Instance-call inference (DONE — `ce1bfc1`)

- Added `_XPP_VAR_DECL_RE` to capture `TypeName varName;` / `TypeName varName =` declarations in method bodies (leading keyword guard prevents false positives).
- Added `_XPP_INSTANCE_CALL_RE` to capture `obj.method(` patterns.
- In `_parse_xpp_method`, build a `local_var_types: dict[str, str]` from declarations, then use it to resolve `custTable.insert()` → `CALLS CustTable.insert` instead of the raw unresolved form.
- `this`/`super` suppressed via existing `_XPP_KEYWORDS` set.
- De-duplication: positions consumed by static (`::`) and instance (`.`) calls are tracked; `_XPP_CALL_RE` skips them so plain `obj` never appears as a spurious call target.
- **+5 tests** in `TestXppInstanceCallInference`.

Real-repo validation (RARnDInitiatives):
- **5,892 instance call edges** generated.

### 2. Richer form control/event extraction (DONE — `65f51aa`)

- Added form control method extraction: walks `<Design>/<Controls>/.../AxFormControl/<Methods>`, emits `Function` nodes qualified as `ControlName_methodName`.
- Known event method names (clicked, modified, lookup, init, run, close, enter, leave, etc.) annotated with `xpp_control_event=True`.
- Datasource event methods (init, active, validateWrite, validateDelete, write, delete, refresh, reread, selectionChanged, etc.) annotated with `xpp_ds_event=True` in both node extra and the `ds_extra` passed to `_extract_xpp_method`.
- **+4 tests** in `TestXppFormControlExtraction`.

### 3. Table field groups, EDT inheritance chains, enum value nodes (DONE — `3d45e67`)

- **FieldGroups**: `<FieldGroups>/<AxTableFieldGroup>/<Fields>/<AxTableFieldGroupField>/<DataField>` → `REFERENCES(field_group)` edges with `xpp_field_group=<group name>`.
- **EDT INHERITS**: `AxEdt`/`AxEdtExtension` `<Extends>` → `INHERITS` edge with `xpp_ref_kind=edt`.
- **Enum value nodes**: `AxEnum`/`AxEnumExtension` `<EnumValues>/<AxEnumValue>` → `Field` node with `xpp_enum_value=True` + `CONTAINS` edge.
- **+5 tests** in `TestXppTableFieldGroupsEdtEnum`.

Real-repo validation:
- **306 enum value nodes**, **2,647 field group ref edges**.

### 4. Full-pipeline large-repo validation with base roots (DONE — `e84b66e`)

Full `build` run against `C:\GitRepos\RARnDInitiatives` with base root `PackagesLocalDirectory` (172 packages, 356k XML files).

**Graph stats after build** (resolver ran partially due to lock on first attempt; see note below):

| Metric | Value |
|--------|-------|
| Files parsed | 82 |
| Parse errors | 0 |
| Nodes | 10,010 |
| Edges | 56,333 |
| CALLS | 35,841 |
| CONTAINS | 9,257 |
| REFERENCES | 8,660 |
| ACCESSES | 2,207 |
| INHERITS | 355 |
| IMPLEMENTS | 13 |
| Instance call edges | 5,892 |
| Enum value nodes | 306 |
| Field group ref edges | 2,647 |
| Resolved edges (partial) | 2,326 |

**Base index build note**: walking 356k XML files via `Path.rglob` was the bottleneck. Replaced with `os.walk` in `e84b66e` — avoids per-entry `Path` instantiation. The index uses ~500 MB RSS and is cached per-process via `_BASE_INDEX_CACHE`.

**WRAPS edges**: resolver ran in background with full base root after session ended; see next-session checklist for how to capture final count.

#### os.walk optimization

`_build_base_index` in `xpp_resolver.py` now uses `os.walk` with early folder filtering (only descends into `Ax*` recognized folders at leaf level). This avoids creating `Path` objects for all 356k entries. +1 test in `TestXppBaseIndexPerformance`.

### Test suite after session 3

```powershell
& 'C:\Users\Adminb76b72ac39\.local\bin\uv.exe' run pytest tests/test_xpp.py tests/test_cli.py tests/test_incremental.py -q
```

Result: **96 passed** (was 81 at start of session 3; +15 new tests).

---

## Resume Checklist

When resuming:

1. Ensure `uv` is on PATH or call it directly:
   - `C:\Users\Adminb76b72ac39\.local\bin\uv.exe`
2. Set workspace-local `uv` dirs before tests:

```powershell
$env:UV_CACHE_DIR='C:\GitRepos\code-review-graph\.uv-cache'
$env:UV_PYTHON_INSTALL_DIR='C:\GitRepos\code-review-graph\.uv-python'
```

3. Start from commit `e84b66e`.
4. Re-run the focused validation first:

```powershell
& 'C:\Users\Adminb76b72ac39\.local\bin\uv.exe' run pytest tests/test_xpp.py tests/test_cli.py tests/test_incremental.py -q
```

Expected: **96 passed**.

5. To get final WRAPS edge count from the large-repo build, run the resolver directly:

```powershell
Set-Location "C:\GitRepos\RARnDInitiatives"
& 'C:\Users\Adminb76b72ac39\.local\bin\uv.exe' run --project "C:\GitRepos\code-review-graph" python -c "
from code_review_graph.graph import GraphStore
from code_review_graph.xpp_resolver import resolve_xpp_metadata
store = GraphStore('.code-review-graph/graph.db')
stats = resolve_xpp_metadata(store, base_roots=['C:/Users/Adminb76b72ac39/AppData/Local/Microsoft/Dynamics365/10.0.2527.78/PackagesLocalDirectory'])
print(stats)
"
```

6. Remaining lower-priority items (see "What Is Still Missing"):
   - Better data-access semantics (group by, aggregates, SysDa API)
   - Stronger extension/CoC resolution (signature-aware WRAPS matching)
   - Better event support (PreEventHandler/PostEventHandler distinction)
   - Incremental update behavior on real package trees

---

## Session 4 — What Was Completed (commits `aecc75e` → `13a25c6`)

All three remaining items from the session 3 checklist were addressed.

### 1. Better data-access semantics (DONE — `aecc75e`)

- **SELECT modifiers**: `firstOnly`, `forUpdate`, `crossCompany`, `reverse`, `nofetch`, etc. captured in `xpp_select_modifiers: [...]` on ACCESSES edges. Plain selects carry no key.
- **DML op tracking**: `insert_recordset`/`update_recordset`/`delete_from` set `xpp_select_op` on the ACCESSES edge.
- **Aggregates**: `sum(field)`, `count(field)`, `maxof(field)`, `sumof(field)`, etc. → `ACCESSES(aggregate)` edges with `xpp_aggregate_fn`.
- **`order by` / `group by`**: each referenced field emits `ACCESSES(order_field)` / `ACCESSES(group_field)`.
- **SysDa query API**: `new SysDaQueryObject(...)`, `new SysDaSelectParameters(...)`, etc. → `ACCESSES(sysdaquery)` edges.
- Rewrote `_XPP_SELECT_RE` with named groups (`op`, `mods`, `table`) and explicit lookahead for valid terminators (`;`, `(`, `where`, `join`, `order`, `group`, etc.).
- Added `_XPP_SELECT_MODIFIERS` frozenset for reliable modifier vs. table-name disambiguation.
- **+10 tests** in `TestXppDataAccessSemantics`.

### 2. Stronger extension/CoC resolution (DONE — `e4220b8`)

- **Naming fallback**: classes named `FooBar_Extension` (or `FooBarExtension`) without `[ExtensionOf(...)]` attribute now have their CoC target inferred by stripping the suffix. WRAPS edges are generated without requiring the attribute.
- **Signature-aware candidate selection**: new `_pick_best_wraps_candidate()` prefers the base method whose parameter count matches the extension method; falls back to first candidate (name-only) when no count-match exists.
- **Confidence annotation**: WRAPS edges carry `xpp_wraps_confidence: "exact"` (param-count matched) or `"name_only"` (fell back).
- **+3 tests** in `TestXppCoCResolution`.

### 3. Better event support (DONE — `13a25c6`)

- **`AxEventSubscription <EventType>`**: `Pre`/`Post`/`Delegate` extracted → `xpp_event_type` on HANDLES edges.
- **`<PublisherEvent>` vs `<PublisherMethod>`**: delegate events preferred; `xpp_event_kind: delegate|method` set on each HANDLES edge.
- **Attribute-based event handlers**: `[DataEventHandler(...)]`, `[FormDataSourceEventHandler(...)]`, `[FormControlEventHandler(...)]`, `[FormDataFieldEventHandler(...)]`, `[SysDelegate(...)]` on methods → HANDLES edges + `xpp_event_handler=True` on Function node.
- Fixed nested-paren args regex bug: changed `[^)]*` to `[^()]*` in `_XPP_EVENT_HANDLER_ATTR_RE` so the greedy outer match doesn't swallow opening parens.
- **+6 tests** in `TestXppEventSupport`.

### Test suite after session 4

```powershell
& 'C:\Users\Adminb76b72ac39\.local\bin\uv.exe' run pytest tests/test_xpp.py tests/test_cli.py tests/test_incremental.py -q
```

Result: **115 passed** (was 96 at session start; +19 new tests).

---

## What Is Still Missing (updated after session 4)

### 1. `Query*` object semantics

`QueryRun`, `QueryBuildDataSource.addDataSource()`, `QueryBuildRange` etc. are class instantiation + instance-call patterns. Instance-call inference now covers these partially (variable declarations → `QueryRun.next()` resolves to `QueryRun.next`), but the *table name* inside `addDataSource(tableNum(SalesTable))` is not yet extracted as an ACCESSES edge.

### 2. Higher-confidence table-name capture from complex select forms

Complex selects like `select Field1, Field2 from SalesTable` (explicit field list, `from` keyword variant) are not currently captured. The D365 X++ `select` statement has two forms:
- `select [mods] recBuf ...`  ← current regex handles
- `select Field, Field from recBuf ...`  ← not yet handled

### 3. Incremental update behavior on real package trees

The `update` command (`incremental_update`) with `xpp_base_roots` has not been validated against the real `RARnDInitiatives` repo. Known unknowns: file-change detection for XML metadata, stale-node cleanup on rename.

### 4. WRAPS edge count from large-repo run

The background resolver (launched in session 3) completed but output was not captured. See resume checklist step 5 to measure WRAPS edges generated for the real repo.

---

## Resume Checklist

When resuming:

1. Ensure `uv` is on PATH or call it directly:
   - `C:\Users\Adminb76b72ac39\.local\bin\uv.exe`
2. Set workspace-local `uv` dirs before tests:

```powershell
$env:UV_CACHE_DIR='C:\GitRepos\code-review-graph\.uv-cache'
$env:UV_PYTHON_INSTALL_DIR='C:\GitRepos\code-review-graph\.uv-python'
```

3. Start from commit `13a25c6`.
4. Re-run the focused validation first:

```powershell
& 'C:\Users\Adminb76b72ac39\.local\bin\uv.exe' run pytest tests/test_xpp.py tests/test_cli.py tests/test_incremental.py -q
```

Expected: **115 passed**.

5. To get final WRAPS edge count from the large-repo build (and re-run with new CoC improvements):

```powershell
Set-Location "C:\GitRepos\RARnDInitiatives"
& 'C:\Users\Adminb76b72ac39\.local\bin\uv.exe' run --project "C:\GitRepos\code-review-graph" python -c "
from code_review_graph.graph import GraphStore
from code_review_graph.xpp_resolver import resolve_xpp_metadata
store = GraphStore('.code-review-graph/graph.db')
stats = resolve_xpp_metadata(store, base_roots=['C:/Users/Adminb76b72ac39/AppData/Local/Microsoft/Dynamics365/10.0.2527.78/PackagesLocalDirectory'])
print(stats)
"
```

6. Suggested next work (in priority order):
   1. `select Field from Table` syntax support (explicit field list + `from` keyword)
   2. `QueryRun`/`QueryBuildDataSource` table-name extraction from `addDataSource(tableNum(X))`
   3. Incremental update validation against `RARnDInitiatives`

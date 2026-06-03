"""X++ / D365 metadata resolver.

Canonicalizes X++ metadata references and lazily loads matching artifacts
from configured external metadata roots such as PackagesLocalDirectory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import sys
from pathlib import Path
from typing import Optional

from .graph import GraphStore
from .parser import XPP_METADATA_OBJECT_KINDS, CodeParser, EdgeInfo

logger = logging.getLogger(__name__)

try:
    from tqdm import tqdm as _tqdm
    _HAS_TQDM = True
except ImportError:  # pragma: no cover
    _HAS_TQDM = False


def _progress(iterable, total: int, desc: str, unit: str = "it"):
    if _HAS_TQDM:
        return _tqdm(iterable, total=total, desc=desc, unit=unit, dynamic_ncols=True)
    def _plain(it):
        for i, item in enumerate(it, 1):
            yield item
            if i % 200 == 0 or i == total:
                print(f"\r  {desc}: {i}/{total}", end="", flush=True, file=sys.stderr)
        print(file=sys.stderr)
    return _plain(iterable)


def _phase(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr, flush=True)

# Module-level cache so large base roots are only indexed once per process.
_BASE_INDEX_CACHE: dict[tuple[str, ...], dict[str, list[tuple[str, Path]]]] = {}

_XPP_ARTIFACT_FOLDERS = {
    "class": ["AxClass"],
    "table": ["AxTable"],
    "form": ["AxForm"],
    "enum": ["AxEnum", "AxEnumExtension"],
    "edt": ["AxEdt", "AxEdtExtension"],
    "query": ["AxQuery"],
    "view": ["AxView"],
    "map": ["AxMap"],
    "dataentityview": ["AxDataEntityView"],
    "dataentity": ["AxDataEntityView"],
    # table-derived ref kinds all resolve to AxTable
    "table_relation": ["AxTable"],
    "datasource_table": ["AxTable"],
    "query_table": ["AxTable"],
    "view_table": ["AxTable"],
    "join": ["AxTable"],
    "map_table": ["AxTable"],
    # field-level references resolve from the owning artifact
    "field": ["AxTable", "AxMap", "AxDataEntityView"],
    # tableNum/fieldNum — QueryBuilder API intrinsics referencing tables/fields
    "tablenum": ["AxTable"],
    "fieldnum": ["AxTable", "AxMap", "AxDataEntityView"],
    # event subscriptions: publisher can be class or table
    "event": ["AxClass", "AxTable"],
    # security / workflow / other metadata artifacts
    "securityrole": ["AxSecurityRole"],
    "securityduty": ["AxSecurityDuty"],
    "securityprivilege": ["AxSecurityPrivilege"],
    "workflow": ["AxWorkflow"],
    "report": ["AxReport"],
    "ssrsreport": ["AxReport"],
    "menu": ["AxMenu"],
    "menuitemdisplay": ["AxMenuItem"],
    "menuitemoutput": ["AxMenuItem"],
    "menuitemaction": ["AxMenuItem"],
    "configurationkey": ["AxConfigurationKey"],
    "licensecodestr": ["AxLicenseCode"],
    "tile": ["AxTile"],
    "page": ["AxPage"],
    "resource": ["AxResource"],
}


def _param_count(params_str: str) -> int:
    """Return the number of parameters from a comma-separated params string."""
    stripped = params_str.strip()
    if not stripped:
        return 0
    return stripped.count(",") + 1


def _pick_best_wraps_candidate(
    candidates: list,
    ext_params: str,
) -> Optional[object]:
    """Pick the base method that best matches the extension method's parameter count.

    Prefers exact param-count match; falls back to first candidate (name-only match).
    """
    if not candidates:
        return None
    ext_count = _param_count(ext_params)
    for cand in candidates:
        if _param_count(cand["params"] or "") == ext_count:
            return cand
    return candidates[0]


def resolve_xpp_metadata(
    store: GraphStore,
    base_roots: list[str] | None = None,
) -> dict:
    """Resolve X++ metadata references and load missing external artifacts."""
    parser = CodeParser()
    normalized_roots = [
        str(Path(root).expanduser().resolve())
        for root in (base_roots or [])
        if root and Path(root).exists()
    ]
    stats = {
        "files_indexed": 0,
        "edges_rewritten": 0,
        "wrappers_resolved": 0,
        "external_artifacts_loaded": 0,
        "base_roots": normalized_roots,
    }

    base_index: Optional[dict[str, list[tuple[str, Path]]]] = None
    if normalized_roots:
        _phase("Building X++ base index…")
        base_index = _get_base_index(normalized_roots)

    _phase("Resolving X++ references…")
    # Opt 2: track which artifact names have been loaded so each external XML
    # is parsed exactly once regardless of how many edges reference it.
    loaded_artifacts: set[str] = set()

    # Opt 3: use max_seen_id instead of a full seen-ID set so the SQL query
    # uses the primary-key B-tree to skip already-processed edges — O(log N)
    # instead of O(N) Python filtering of a full fetchall.
    max_seen_id: int = 0

    iteration = 0
    changed = True
    while changed:
        changed = False
        iteration += 1
        cur = store._conn.cursor()

        # Opt 3: only fetch edges with id > max previously seen id.
        rows = cur.execute(
            "SELECT id, kind, source_qualified, target_qualified, file_path, line, extra "
            "FROM edges WHERE kind IN "
            "('EXTENDS', 'REFERENCES', 'ACCESSES', 'HANDLES', 'INHERITS', 'IMPLEMENTS') "
            "AND id > ?",
            (max_seen_id,)
        ).fetchall()
        if rows:
            max_seen_id = max(r["id"] for r in rows)

        # Opt 4: build in-memory node existence structures once per pass.
        known_nodes: set[str] = {
            r[0] for r in cur.execute("SELECT qualified_name FROM nodes").fetchall()
        }
        artifact_to_qn: dict[str, str] = {}
        for qn in known_nodes:
            parts = qn.split("::")
            if len(parts) == 2:
                artifact_to_qn.setdefault(parts[1].split(".")[0], qn)

        desc = f"Resolving (pass {iteration}, {len(rows)} new edges)"
        # Opt 5: collect edge updates; flush with executemany() after the loop.
        pending_updates: list[tuple[str, str, int]] = []
        for row in _progress(rows, total=len(rows), desc=desc, unit="edge"):
            edge_id = row["id"]
            target = row["target_qualified"]
            try:
                extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                extra = {}
            resolved = _resolve_target(
                store, parser, target, extra, normalized_roots, stats, base_index,
                loaded_artifacts, known_nodes, artifact_to_qn,
            )
            if resolved and resolved != target:
                pending_updates.append(
                    (resolved, json.dumps({**extra, "xpp_resolved": True}), edge_id)
                )
                stats["edges_rewritten"] += 1
                changed = True
        if pending_updates:
            cur.executemany(
                "UPDATE edges SET target_qualified=?, extra=? WHERE id=?",
                pending_updates,
            )

        # Opt 7: replace LIKE scan with Python filter on a full xpp Function fetch.
        all_xpp_fns = cur.execute(
            "SELECT qualified_name, name, parent_name, file_path, params, extra "
            "FROM nodes WHERE kind='Function' AND language='xpp'"
        ).fetchall()
        wrap_rows = [r for r in all_xpp_fns if "xpp_calls_next" in (r["extra"] or "")]
        for row in _progress(wrap_rows, total=len(wrap_rows), desc=f"Resolving WRAPS (pass {iteration})", unit="fn"):
            try:
                extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                extra = {}
            if not extra.get("xpp_calls_next"):
                continue
            target_artifact = extra.get("xpp_extension_target")
            # Fallback: infer target from *_Extension naming convention when no
            # [ExtensionOf(...)] attribute was present.
            if not target_artifact:
                parent = row["parent_name"] or ""
                for suffix in ("_Extension", "Extension"):
                    if parent.endswith(suffix):
                        target_artifact = parent[: -len(suffix)]
                        break
            if not isinstance(target_artifact, str) or not target_artifact:
                continue
            resolved_artifact = _resolve_target(
                store,
                parser,
                target_artifact,
                {"xpp_ref_kind": extra.get("xpp_extension_kind", "")},
                normalized_roots,
                stats,
                base_index,
                loaded_artifacts,
                known_nodes,
                artifact_to_qn,
            )
            if not resolved_artifact:
                continue
            base_name = resolved_artifact.split("::")[-1].split(".")[0]
            # Signature-aware candidate selection: prefer param-count match.
            candidates = store._conn.execute(
                "SELECT qualified_name, params FROM nodes "
                "WHERE kind='Function' AND parent_name=? AND name=?",
                (base_name, row["name"]),
            ).fetchall()
            if not candidates:
                continue
            best_candidate = _pick_best_wraps_candidate(
                candidates, row["params"] or ""
            )
            if not best_candidate:
                continue
            exists = store._conn.execute(
                "SELECT 1 FROM edges "
                "WHERE kind='WRAPS' AND source_qualified=? AND target_qualified=?",
                (row["qualified_name"], best_candidate["qualified_name"]),
            ).fetchone()
            if exists:
                continue
            # Record confidence: exact (param count matched) vs name_only.
            ext_params = row["params"] or ""
            base_params = best_candidate["params"] or ""
            confidence = (
                "exact"
                if _param_count(ext_params) == _param_count(base_params)
                else "name_only"
            )
            store.upsert_edge(EdgeInfo(
                kind="WRAPS",
                source=row["qualified_name"],
                target=best_candidate["qualified_name"],
                file_path=row["file_path"],
                extra={"xpp_resolved": True, "xpp_wraps_confidence": confidence},
            ))
            stats["wrappers_resolved"] += 1
            changed = True

        if changed:
            store.commit()

    return stats


def _resolve_target(
    store: GraphStore,
    parser: CodeParser,
    target: str,
    extra: dict,
    base_roots: list[str],
    stats: dict,
    base_index: Optional[dict[str, list[tuple[str, Path]]]] = None,
    loaded_artifacts: Optional[set[str]] = None,
    known_nodes: Optional[set[str]] = None,
    artifact_to_qn: Optional[dict[str, str]] = None,
) -> Optional[str]:
    if not target:
        return None
    # Opt 4: check in-memory set before hitting DB.
    if known_nodes is not None:
        if target in known_nodes:
            return target
    else:
        direct = store.get_node(target)
        if direct:
            return direct.qualified_name

    artifact_name, member_name = _split_target(target)
    ref_kind = str(extra.get("xpp_ref_kind", "")).lower()
    candidate = _find_local_artifact(store, artifact_name, member_name, artifact_to_qn)
    if candidate:
        return candidate

    if base_roots:
        loaded = _load_external_artifact(
            store, parser, artifact_name, ref_kind, base_roots, base_index,
            loaded_artifacts,
        )
        if loaded:
            stats["external_artifacts_loaded"] += loaded
            candidate = _find_local_artifact(store, artifact_name, member_name, artifact_to_qn)
            if candidate:
                return candidate
    return target if "::" in target else None


def _split_target(target: str) -> tuple[str, Optional[str]]:
    if "." in target and "::" not in target:
        artifact, member = target.split(".", 1)
        return artifact, member
    return target, None


def _find_local_artifact(
    store: GraphStore,
    artifact_name: str,
    member_name: Optional[str],
    artifact_to_qn: Optional[dict[str, str]] = None,
) -> Optional[str]:
    if member_name:
        row = store._conn.execute(
            "SELECT qualified_name FROM nodes "
            "WHERE kind IN ('Function', 'Field') AND parent_name=? AND name=?",
            (artifact_name, member_name),
        ).fetchone()
        if row:
            return row["qualified_name"]

    # Opt 4: use pre-built in-memory dict instead of FTS5 search_nodes query.
    if artifact_to_qn is not None:
        cached = artifact_to_qn.get(artifact_name)
        if cached:
            return cached
        # Dict may be stale for nodes loaded within the current pass — fall back
        # to a direct indexed SQL query (not FTS) for just this artifact name.
        row = store._conn.execute(
            "SELECT qualified_name FROM nodes "
            "WHERE name=? AND kind IN ('Class', 'Type', 'Field') LIMIT 1",
            (artifact_name,),
        ).fetchone()
        return row["qualified_name"] if row else None

    candidates = store.search_nodes(artifact_name, limit=10)
    for node in candidates:
        if node.name == artifact_name and node.kind in ("Class", "Type", "Field"):
            return node.qualified_name
    return None


def _build_base_index(base_roots: list[str]) -> dict[str, list[tuple[str, Path]]]:
    """Index all recognized Ax* XML files in base roots by stem (artifact name).

    Uses os.walk rather than Path.rglob for speed on large trees (e.g. 356k-file
    PackagesLocalDirectory); os.walk avoids per-entry Path instantiation overhead.
    """
    all_folders = set(XPP_METADATA_OBJECT_KINDS.keys())
    index: dict[str, list[tuple[str, Path]]] = {}
    total_files = 0
    for root in base_roots:
        if not os.path.isdir(root):
            continue
        _phase(f"Indexing base root: {root} …")
        packages_done = 0
        for dirpath, dirnames, filenames in os.walk(root):
            folder = os.path.basename(dirpath)
            # Count top-level packages for progress feedback
            depth = dirpath[len(root):].count(os.sep)
            if depth == 1:
                packages_done += 1
                print(f"\r  Indexing packages: {packages_done}", end="", flush=True, file=sys.stderr)
            if folder not in all_folders:
                continue
            for fname in filenames:
                if not fname.endswith(".xml"):
                    continue
                stem = fname[:-4]  # strip .xml
                entry = (folder, Path(dirpath) / fname)
                if stem not in index:
                    index[stem] = [entry]
                else:
                    index[stem].append(entry)
                total_files += 1
        print(file=sys.stderr)  # newline after package counter
    _phase(f"Base index ready: {len(index)} artifact names across {total_files} files")
    return index


def _get_base_index(base_roots: list[str]) -> dict[str, list[tuple[str, Path]]]:
    key = tuple(sorted(base_roots))
    if key in _BASE_INDEX_CACHE:
        return _BASE_INDEX_CACHE[key]

    # Opt 6: persist to disk so the 277k-file walk only runs once across sessions.
    roots_sig = hashlib.md5(str(key).encode()).hexdigest()[:12]
    cache_dir = Path.home() / ".code-review-graph" / "cache"
    cache_path = cache_dir / f"xpp_base_index_{roots_sig}.pkl"

    if cache_path.exists() and not os.environ.get("CRG_XPP_REBUILD_INDEX"):
        try:
            roots_mtime = max(
                Path(r).stat().st_mtime for r in base_roots if Path(r).exists()
            )
            if roots_mtime <= cache_path.stat().st_mtime:
                with open(cache_path, "rb") as f:
                    index = pickle.load(f)
                _BASE_INDEX_CACHE[key] = index
                _phase(
                    f"Loaded base index from cache "
                    f"({len(index)} artifact names — set CRG_XPP_REBUILD_INDEX=1 to force rebuild)"
                )
                return index
        except Exception:
            pass  # Fall through to rebuild

    index = _build_base_index(base_roots)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        logger.debug("Could not persist base index cache: %s", exc)
    _BASE_INDEX_CACHE[key] = index
    return index


def _load_external_artifact(
    store: GraphStore,
    parser: CodeParser,
    artifact_name: str,
    ref_kind: str,
    base_roots: list[str],
    base_index: Optional[dict[str, list[tuple[str, Path]]]] = None,
    loaded_artifacts: Optional[set[str]] = None,
) -> int:
    # Opt 2: skip if this artifact name was already loaded in a previous call.
    if loaded_artifacts is not None and artifact_name in loaded_artifacts:
        return 0

    folders = _XPP_ARTIFACT_FOLDERS.get(ref_kind, [])
    if not folders:
        folders = [folder for group in _XPP_ARTIFACT_FOLDERS.values() for folder in group]
    folder_set = set(folders)
    seen: set[str] = set()
    # Opt 1B: collect all parse results and write in one batch transaction.
    parse_results: list[tuple[str, list, list, str]] = []

    if base_index is not None:
        candidates = base_index.get(artifact_name, [])
        for folder, xml_path in candidates:
            if folder not in folder_set:
                continue
            path_str = str(xml_path)
            if path_str in seen:
                continue
            try:
                nodes, edges = parser.parse_file(xml_path)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to parse X++ metadata file %s: %s", xml_path, exc)
                continue
            if not nodes:
                continue
            fhash = parser.compute_content_hash(xml_path)
            parse_results.append((path_str, nodes, edges, fhash))
            seen.add(path_str)
    else:
        # Fallback: per-artifact rglob (slow, used when no index)
        for root in base_roots:
            root_path = Path(root)
            if not root_path.exists():
                continue
            for xml_path in root_path.rglob(f"{artifact_name}.xml"):
                path_str = str(xml_path)
                if path_str in seen:
                    continue
                if xml_path.parent.name not in folder_set:
                    continue
                try:
                    nodes, edges = parser.parse_file(xml_path)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Failed to parse X++ metadata file %s: %s", xml_path, exc)
                    continue
                if not nodes:
                    continue
                fhash = parser.compute_content_hash(xml_path)
                parse_results.append((path_str, nodes, edges, fhash))
                seen.add(path_str)

    if parse_results:
        store.store_file_batch(parse_results)

    if loaded_artifacts is not None:
        loaded_artifacts.add(artifact_name)

    return len(parse_results)

"""X++ / D365 metadata resolver.

Canonicalizes X++ metadata references and lazily loads matching artifacts
from configured external metadata roots such as PackagesLocalDirectory.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .graph import GraphStore
from .parser import XPP_METADATA_OBJECT_KINDS, CodeParser, EdgeInfo

logger = logging.getLogger(__name__)

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
    "licensecodesstr": ["AxLicenseCode"],
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
        base_index = _get_base_index(normalized_roots)

    changed = True
    while changed:
        changed = False
        cur = store._conn.cursor()
        rows = cur.execute(
            "SELECT id, kind, source_qualified, target_qualified, file_path, line, extra "
            "FROM edges WHERE kind IN "
            "('EXTENDS', 'REFERENCES', 'ACCESSES', 'HANDLES', 'INHERITS', 'IMPLEMENTS')"
        ).fetchall()
        for row in rows:
            edge_id = row["id"]
            target = row["target_qualified"]
            try:
                extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                extra = {}
            resolved = _resolve_target(
                store, parser, target, extra, normalized_roots, stats, base_index,
            )
            if resolved and resolved != target:
                cur.execute(
                    "UPDATE edges SET target_qualified=?, extra=? WHERE id=?",
                    (resolved, json.dumps({**extra, "xpp_resolved": True}), edge_id),
                )
                stats["edges_rewritten"] += 1
                changed = True

        wrap_rows = cur.execute(
            "SELECT qualified_name, name, parent_name, file_path, params, extra FROM nodes "
            "WHERE kind='Function' AND language='xpp' AND extra LIKE '%xpp_calls_next%'"
        ).fetchall()
        for row in wrap_rows:
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
) -> Optional[str]:
    if not target:
        return None
    direct = store.get_node(target)
    if direct:
        return direct.qualified_name

    artifact_name, member_name = _split_target(target)
    ref_kind = str(extra.get("xpp_ref_kind", "")).lower()
    candidate = _find_local_artifact(store, artifact_name, member_name)
    if candidate:
        return candidate

    if base_roots:
        loaded = _load_external_artifact(
            store, parser, artifact_name, ref_kind, base_roots, base_index,
        )
        if loaded:
            stats["external_artifacts_loaded"] += loaded
            candidate = _find_local_artifact(store, artifact_name, member_name)
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
) -> Optional[str]:
    if member_name:
        row = store._conn.execute(
            "SELECT qualified_name FROM nodes "
            "WHERE kind IN ('Function', 'Field') AND parent_name=? AND name=?",
            (artifact_name, member_name),
        ).fetchone()
        if row:
            return row["qualified_name"]

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
    for root in base_roots:
        if not os.path.isdir(root):
            continue
        logger.debug("Building X++ base index from %s", root)
        for dirpath, dirnames, filenames in os.walk(root):
            folder = os.path.basename(dirpath)
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
    logger.debug("X++ base index built: %d distinct artifact names", len(index))
    return index


def _get_base_index(base_roots: list[str]) -> dict[str, list[tuple[str, Path]]]:
    key = tuple(sorted(base_roots))
    if key not in _BASE_INDEX_CACHE:
        _BASE_INDEX_CACHE[key] = _build_base_index(base_roots)
    return _BASE_INDEX_CACHE[key]


def _load_external_artifact(
    store: GraphStore,
    parser: CodeParser,
    artifact_name: str,
    ref_kind: str,
    base_roots: list[str],
    base_index: Optional[dict[str, list[tuple[str, Path]]]] = None,
) -> int:
    folders = _XPP_ARTIFACT_FOLDERS.get(ref_kind, [])
    if not folders:
        folders = [folder for group in _XPP_ARTIFACT_FOLDERS.values() for folder in group]
    folder_set = set(folders)
    loaded = 0
    seen: set[str] = set()

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
            store.store_file_nodes_edges(path_str, nodes, edges, fhash)
            seen.add(path_str)
            loaded += 1
        return loaded

    # Fallback: per-artifact rglob (slow, used when no index)
    for root in base_roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for xml_path in root_path.rglob(f"{artifact_name}.xml"):
            if str(xml_path) in seen:
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
            store.store_file_nodes_edges(str(xml_path), nodes, edges, fhash)
            seen.add(str(xml_path))
            loaded += 1
    return loaded

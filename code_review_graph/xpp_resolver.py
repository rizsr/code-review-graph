"""X++ / D365 metadata resolver.

Canonicalizes X++ metadata references and lazily loads matching artifacts
from configured external metadata roots such as PackagesLocalDirectory.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .graph import GraphStore
from .parser import CodeParser, EdgeInfo

logger = logging.getLogger(__name__)

_XPP_ARTIFACT_FOLDERS = {
    "class": ["AxClass"],
    "table": ["AxTable"],
    "form": ["AxForm"],
    "enum": ["AxEnum"],
    "edt": ["AxEdt"],
    "query": ["AxQuery"],
    "view": ["AxView"],
    "map": ["AxMap"],
    "dataentityview": ["AxDataEntityView"],
}


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

    changed = True
    while changed:
        changed = False
        cur = store._conn.cursor()
        rows = cur.execute(
            "SELECT id, kind, source_qualified, target_qualified, file_path, line, extra "
            "FROM edges WHERE kind IN ('EXTENDS', 'REFERENCES', 'ACCESSES', 'HANDLES')"
        ).fetchall()
        for row in rows:
            edge_id = row["id"]
            target = row["target_qualified"]
            try:
                extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                extra = {}
            resolved = _resolve_target(store, parser, target, extra, normalized_roots, stats)
            if resolved and resolved != target:
                cur.execute(
                    "UPDATE edges SET target_qualified=?, extra=? WHERE id=?",
                    (resolved, json.dumps({**extra, "xpp_resolved": True}), edge_id),
                )
                stats["edges_rewritten"] += 1
                changed = True

        wrap_rows = cur.execute(
            "SELECT qualified_name, name, parent_name, file_path, extra FROM nodes "
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
            if not isinstance(target_artifact, str) or not target_artifact:
                continue
            resolved_artifact = _resolve_target(
                store,
                parser,
                target_artifact,
                {"xpp_ref_kind": extra.get("xpp_extension_kind", "")},
                normalized_roots,
                stats,
            )
            if not resolved_artifact:
                continue
            base_name = resolved_artifact.split("::")[-1].split(".")[0]
            candidate = store._conn.execute(
                "SELECT qualified_name FROM nodes WHERE kind='Function' AND parent_name=? AND name=?",
                (base_name, row["name"]),
            ).fetchone()
            if not candidate:
                continue
            exists = store._conn.execute(
                "SELECT 1 FROM edges WHERE kind='WRAPS' AND source_qualified=? AND target_qualified=?",
                (row["qualified_name"], candidate["qualified_name"]),
            ).fetchone()
            if exists:
                continue
            store.upsert_edge(EdgeInfo(
                kind="WRAPS",
                source=row["qualified_name"],
                target=candidate["qualified_name"],
                file_path=row["file_path"],
                extra={"xpp_resolved": True},
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
        loaded = _load_external_artifact(store, parser, artifact_name, ref_kind, base_roots)
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
            "SELECT qualified_name FROM nodes WHERE kind='Function' AND parent_name=? AND name=?",
            (artifact_name, member_name),
        ).fetchone()
        if row:
            return row["qualified_name"]

    candidates = store.search_nodes(artifact_name, limit=10)
    for node in candidates:
        if node.name == artifact_name and node.kind in ("Class", "Type"):
            return node.qualified_name
    return None


def _load_external_artifact(
    store: GraphStore,
    parser: CodeParser,
    artifact_name: str,
    ref_kind: str,
    base_roots: list[str],
) -> int:
    folders = _XPP_ARTIFACT_FOLDERS.get(ref_kind, [])
    if not folders:
        folders = [folder for group in _XPP_ARTIFACT_FOLDERS.values() for folder in group]
    loaded = 0
    seen: set[str] = set()
    for root in base_roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for xml_path in root_path.rglob(f"{artifact_name}.xml"):
            if str(xml_path) in seen:
                continue
            if xml_path.parent.name not in folders:
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

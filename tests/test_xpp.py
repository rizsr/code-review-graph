"""Tests for X++ / D365 metadata XML support."""

from pathlib import Path
from unittest.mock import patch

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph


def _write_xpp_class(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestXppMetadataParser:
    def setup_method(self):
        self.parser = CodeParser()

    def test_detects_d365_metadata_xml(self, tmp_path):
        xml_path = (
            tmp_path / "Metadata" / "RnD" / "RnD" / "AxClass" / "Sample.xml"
        )
        _write_xpp_class(
            xml_path,
            """<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>Sample</Name>
</AxClass>
""",
        )
        assert self.parser.detect_language(xml_path) == "xpp-metadata"

    def test_parses_embedded_xpp_and_metadata_refs(self, tmp_path):
        xml_path = (
            tmp_path / "Metadata" / "RnD" / "RnD" / "AxTable" / "MyQueue.xml"
        )
        _write_xpp_class(
            xml_path,
            """<?xml version="1.0" encoding="utf-8"?>
<AxTable>
  <Name>MyQueue</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class MyQueue extends common
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>run</Name>
        <Source><![CDATA[
public static void run()
{
    BatchHeader::getCurrentBatchHeader();
    select firstOnly MyQueue;
    info(classStr(MyQueueWorker));
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
  <FormRef>MyQueue</FormRef>
  <Fields>
    <AxTableField>
      <Name>Status</Name>
      <EnumType>MyQueueStatus</EnumType>
    </AxTableField>
  </Fields>
</AxTable>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        assert any(n.kind == "Class" and n.name == "MyQueue" for n in nodes)
        assert any(n.kind == "Function" and n.name == "run" for n in nodes)
        assert any(e.kind == "INHERITS" and e.target == "common" for e in edges)
        assert any(
            e.kind == "CALLS"
            and e.target == "BatchHeader.getCurrentBatchHeader"
            for e in edges
        )
        assert any(e.kind == "ACCESSES" and e.target == "MyQueue" for e in edges)
        assert any(
            e.kind == "REFERENCES"
            and e.target == "MyQueueWorker"
            and e.extra.get("xpp_ref_kind") == "class"
            for e in edges
        )
        assert any(
            e.kind == "REFERENCES"
            and e.target == "MyQueueStatus"
            and e.extra.get("xpp_ref_kind") == "enum"
            for e in edges
        )


class TestXppResolverAndQueries:
    def _build_repo(self, tmp_path: Path) -> tuple[GraphStore, Path]:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        ext_xml = (
            repo_root / "Metadata" / "ExtPkg" / "ExtPkg" / "AxClass"
            / "InventLocationWarehousePlanning_Extension.xml"
        )
        _write_xpp_class(
            ext_xml,
            """<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>InventLocationWarehousePlanning_Extension</Name>
  <SourceCode>
    <Declaration><![CDATA[
[ExtensionOf(tableStr(InventLocation))]
internal final class InventLocationWarehousePlanning_Extension
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>enableParallelizationForWaveMethods</Name>
        <Source><![CDATA[
internal void enableParallelizationForWaveMethods()
{
    next enableParallelizationForWaveMethods();
    select firstOnly InventLocation;
    InventLocation::find();
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxClass>
""",
        )

        base_root = tmp_path / "PackagesLocalDirectory"
        base_xml = (
            base_root / "WarehousePlanning" / "WarehousePlanning" / "AxTable"
            / "InventLocation.xml"
        )
        _write_xpp_class(
            base_xml,
            """<?xml version="1.0" encoding="utf-8"?>
<AxTable>
  <Name>InventLocation</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class InventLocation extends common
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>enableParallelizationForWaveMethods</Name>
        <Source><![CDATA[
public void enableParallelizationForWaveMethods()
{
}
]]></Source>
      </Method>
      <Method>
        <Name>find</Name>
        <Source><![CDATA[
public static InventLocation find()
{
    return null;
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxTable>
""",
        )

        db_path = repo_root / ".code-review-graph" / "graph.db"
        store = GraphStore(db_path)
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=[str(ext_xml.relative_to(repo_root))],
        ):
            result = full_build(repo_root, store, xpp_base_roots=[str(base_root)])
        assert result["xpp_resolution"] is not None
        return store, repo_root

    def test_resolver_loads_base_artifact_and_adds_wraps(self, tmp_path):
        store, repo_root = self._build_repo(tmp_path)
        try:
            base_nodes = store.search_nodes("InventLocation")
            assert any(n.file_path.endswith("InventLocation.xml") for n in base_nodes)
            wrap_edges = store._conn.execute(
                "SELECT source_qualified, target_qualified FROM edges WHERE kind='WRAPS'"
            ).fetchall()
            assert wrap_edges
            assert any(
                "enableParallelizationForWaveMethods" in row["target_qualified"]
                for row in wrap_edges
            )
        finally:
            store.close()

    def test_query_patterns_cover_xpp_edges(self, tmp_path):
        store, repo_root = self._build_repo(tmp_path)
        try:
            ext_result = query_graph(
                "extensions_of", "InventLocation", repo_root=str(repo_root),
            )
            assert ext_result["status"] == "ok"
            assert any(
                r["name"] == "InventLocationWarehousePlanning_Extension"
                for r in ext_result["results"]
            )

            wrap_target = next(
                row["target_qualified"]
                for row in store._conn.execute(
                    "SELECT target_qualified FROM edges WHERE kind='WRAPS'"
                ).fetchall()
            )
            wrap_result = query_graph(
                "wrapped_by", wrap_target, repo_root=str(repo_root),
            )
            assert wrap_result["status"] == "ok"
            assert any(
                "enableParallelizationForWaveMethods" == r["name"]
                for r in wrap_result["results"]
            )

            access_result = query_graph(
                "accesses_of", "InventLocation", repo_root=str(repo_root),
            )
            assert access_result["status"] == "ok"
            assert any(
                r["name"] == "enableParallelizationForWaveMethods"
                for r in access_result["results"]
            )
        finally:
            store.close()

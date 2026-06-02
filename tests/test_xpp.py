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


class TestXppArtifactDepth:
    def setup_method(self):
        self.parser = CodeParser()

    def test_axtable_fields_and_relations(self, tmp_path):
        xml_path = tmp_path / "Metadata" / "RnD" / "RnD" / "AxTable" / "SalesOrder.xml"
        _write_xpp_class(
            xml_path,
            """<?xml version="1.0" encoding="utf-8"?>
<AxTable>
  <Name>SalesOrder</Name>
  <Extends>SalesOrderBase</Extends>
  <Fields>
    <AxTableField>
      <Name>CustAccount</Name>
      <ExtendedDataType>CustAccount</ExtendedDataType>
    </AxTableField>
    <AxTableField>
      <Name>Status</Name>
      <EnumType>SalesStatus</EnumType>
    </AxTableField>
  </Fields>
  <Relations>
    <AxTableRelation>
      <RelatedTable>CustTable</RelatedTable>
    </AxTableRelation>
  </Relations>
</AxTable>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        assert any(n.kind == "Field" and n.name == "CustAccount" for n in nodes)
        assert any(n.kind == "Field" and n.name == "Status" for n in nodes)
        assert any(e.kind == "CONTAINS" and "CustAccount" in e.target for e in edges)
        assert any(
            e.kind == "REFERENCES" and e.target == "CustTable"
            and e.extra.get("xpp_ref_kind") == "table_relation"
            for e in edges
        )
        assert any(e.kind == "INHERITS" and e.target == "SalesOrderBase" for e in edges)

    def test_axform_datasource_extraction(self, tmp_path):
        xml_path = tmp_path / "Metadata" / "RnD" / "RnD" / "AxForm" / "SalesTable.xml"
        _write_xpp_class(
            xml_path,
            """<?xml version="1.0" encoding="utf-8"?>
<AxForm>
  <Name>SalesTable</Name>
  <DataSources>
    <AxFormDataSource>
      <Name>SalesTable</Name>
      <Table>SalesTable</Table>
      <Methods>
        <Method>
          <Name>init</Name>
          <Source><![CDATA[
public void init()
{
    super();
    SalesTable::find();
}
]]></Source>
        </Method>
      </Methods>
    </AxFormDataSource>
  </DataSources>
</AxForm>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        assert any(
            e.kind == "REFERENCES" and e.target == "SalesTable"
            and e.extra.get("xpp_ref_kind") == "datasource_table"
            for e in edges
        )
        assert any(n.kind == "Function" and n.name == "init" for n in nodes)
        assert any(
            e.kind == "CALLS" and e.target == "SalesTable.find" for e in edges
        )

    def test_axquery_datasource_tables(self, tmp_path):
        xml_path = tmp_path / "Metadata" / "RnD" / "RnD" / "AxQuery" / "SalesQuery.xml"
        _write_xpp_class(
            xml_path,
            """<?xml version="1.0" encoding="utf-8"?>
<AxQuery>
  <Name>SalesQuery</Name>
  <DataSources>
    <AxQuerySimpleDataSource>
      <Table>SalesTable</Table>
      <DataSources>
        <AxQuerySimpleDataSource>
          <Table>CustTable</Table>
        </AxQuerySimpleDataSource>
      </DataSources>
    </AxQuerySimpleDataSource>
  </DataSources>
</AxQuery>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        assert any(
            e.kind == "REFERENCES" and e.target == "SalesTable"
            and e.extra.get("xpp_ref_kind") == "query_table"
            for e in edges
        )
        assert any(
            e.kind == "REFERENCES" and e.target == "CustTable"
            and e.extra.get("xpp_ref_kind") == "query_table"
            for e in edges
        )

    def test_axview_datasource_tables(self, tmp_path):
        xml_path = tmp_path / "Metadata" / "RnD" / "RnD" / "AxView" / "SalesLineView.xml"
        _write_xpp_class(
            xml_path,
            """<?xml version="1.0" encoding="utf-8"?>
<AxView>
  <Name>SalesLineView</Name>
  <Query>
    <DataSources>
      <AxQuerySimpleDataSource>
        <Table>SalesLine</Table>
      </AxQuerySimpleDataSource>
    </DataSources>
  </Query>
</AxView>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        assert any(
            e.kind == "REFERENCES" and e.target == "SalesLine"
            and e.extra.get("xpp_ref_kind") == "view_table"
            for e in edges
        )

    def test_axeventsubscription_publisher_handler(self, tmp_path):
        xml_path = (
            tmp_path / "Metadata" / "RnD" / "RnD" / "AxEventSubscription"
            / "SalesOrder_OnInsert_Handler.xml"
        )
        _write_xpp_class(
            xml_path,
            """<?xml version="1.0" encoding="utf-8"?>
<AxEventSubscription>
  <Name>SalesOrder_OnInsert_Handler</Name>
  <Publisher>SalesOrder</Publisher>
  <PublisherMethod>onInsert</PublisherMethod>
  <EventHandler>SalesOrderHandler</EventHandler>
  <EventHandlerMethod>onSalesOrderInsert</EventHandlerMethod>
  <EventType>PostEventHandler</EventType>
</AxEventSubscription>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        assert any(
            e.kind == "HANDLES" and e.target == "SalesOrder.onInsert" for e in edges
        )
        assert any(
            e.kind == "REFERENCES" and e.target == "SalesOrderHandler"
            and e.extra.get("xpp_ref_kind") == "class"
            for e in edges
        )

    def test_axmap_fields_and_mappings(self, tmp_path):
        xml_path = tmp_path / "Metadata" / "RnD" / "RnD" / "AxMap" / "AddressMap.xml"
        _write_xpp_class(
            xml_path,
            """<?xml version="1.0" encoding="utf-8"?>
<AxMap>
  <Name>AddressMap</Name>
  <Fields>
    <AxMapField>
      <Name>Street</Name>
      <ExtendedDataType>AddressStreet</ExtendedDataType>
    </AxMapField>
  </Fields>
  <Mappings>
    <AxMapMapping>
      <MappingTable>CustTable</MappingTable>
    </AxMapMapping>
    <AxMapMapping>
      <MappingTable>VendTable</MappingTable>
    </AxMapMapping>
  </Mappings>
</AxMap>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        assert any(n.kind == "Field" and n.name == "Street" for n in nodes)
        assert any(
            e.kind == "REFERENCES" and e.target == "CustTable"
            and e.extra.get("xpp_ref_kind") == "map_table"
            for e in edges
        )
        assert any(
            e.kind == "REFERENCES" and e.target == "VendTable"
            and e.extra.get("xpp_ref_kind") == "map_table"
            for e in edges
        )


class TestXppSyntaxParsing:
    def setup_method(self):
        self.parser = CodeParser()

    def _make_class_xml(self, tmp_path, folder, name, declaration, methods=""):
        xml_path = tmp_path / "Metadata" / "RnD" / "RnD" / folder / f"{name}.xml"
        methods_xml = ""
        if methods:
            methods_xml = f"<Methods>{methods}</Methods>"
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<{folder}>
  <Name>{name}</Name>
  <SourceCode>
    <Declaration><![CDATA[
{declaration}
]]></Declaration>
    {methods_xml}
  </SourceCode>
</{folder}>
"""
        _write_xpp_class(xml_path, body)
        return xml_path

    def test_implements_edges(self, tmp_path):
        xml_path = self._make_class_xml(
            tmp_path, "AxClass", "MyRunnable",
            "public class MyRunnable implements Runnable, IDisposable\n{",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        assert any(e.kind == "IMPLEMENTS" and e.target == "Runnable" for e in edges)
        assert any(e.kind == "IMPLEMENTS" and e.target == "IDisposable" for e in edges)

    def test_join_table_access(self, tmp_path):
        xml_path = self._make_class_xml(
            tmp_path, "AxClass", "MyQuery",
            "public class MyQuery\n{",
            methods="""
      <Method>
        <Name>run</Name>
        <Source><![CDATA[
public void run()
{
    while select SalesTable
        join CustTable
        exists join SalesLine
        notexists join SalesParam
    {
    }
}
]]></Source>
      </Method>""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        join_targets = {
            e.target for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_ref_kind") == "join"
        }
        assert "CustTable" in join_targets
        assert "SalesLine" in join_targets
        assert "SalesParam" in join_targets

    def test_extra_compiletime_functions(self, tmp_path):
        xml_path = self._make_class_xml(
            tmp_path, "AxClass", "MyRefClass",
            "public class MyRefClass\n{",
            methods="""
      <Method>
        <Name>refs</Name>
        <Source><![CDATA[
public void refs()
{
    str r = reportStr(SalesInvoice);
    str e = dataEntityStr(SalesOrderEntity);
    str m = menuStr(MainMenu);
    str p = securityRoleStr(SystemAdministrator);
}
]]></Source>
      </Method>""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        ref_targets = {
            (e.extra.get("xpp_ref_kind"), e.target)
            for e in edges if e.kind == "REFERENCES"
        }
        assert ("report", "SalesInvoice") in ref_targets
        assert ("dataentity", "SalesOrderEntity") in ref_targets
        assert ("menu", "MainMenu") in ref_targets
        assert ("securityrole", "SystemAdministrator") in ref_targets


class TestXppInstanceCallInference:
    """Variable declaration tracking enables obj.method() → TypeName.method() resolution."""

    def setup_method(self):
        self.parser = CodeParser()

    def _make_class_xml(
        self,
        tmp_path,
        folder: str,
        name: str,
        declaration: str,
        methods: str = "",
    ):
        xml_path = tmp_path / "Metadata" / "Pkg" / "Pkg" / folder / f"{name}.xml"
        methods_xml = f"<Methods>{methods}</Methods>" if methods else ""
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<{folder}>
  <Name>{name}</Name>
  <SourceCode>
    <Declaration><![CDATA[
{declaration}
]]></Declaration>
    {methods_xml}
  </SourceCode>
</{folder}>
"""
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(body, encoding="utf-8")
        return xml_path

    def test_resolved_instance_call(self, tmp_path):
        """custTable.find() with CustTable custTable; → CALLS edge to CustTable.find."""
        xml_path = self._make_class_xml(
            tmp_path, "AxClass", "TestResolved",
            "public class TestResolved\n{",
            methods="""
      <Method>
        <Name>run</Name>
        <Source><![CDATA[
public void run()
{
    CustTable custTable;
    custTable = CustTable::find("C001");
    custTable.insert();
}
]]></Source>
      </Method>""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        call_targets = {e.target for e in edges if e.kind == "CALLS"}
        # Instance call resolved via var-decl type map
        assert "CustTable.insert" in call_targets

    def test_unresolved_instance_call_kept(self, tmp_path):
        """obj.method() without a known var decl still emits an edge (unresolved form)."""
        xml_path = self._make_class_xml(
            tmp_path, "AxClass", "TestUnresolved",
            "public class TestUnresolved\n{",
            methods="""
      <Method>
        <Name>run</Name>
        <Source><![CDATA[
public void run()
{
    unknown.doSomething();
}
]]></Source>
      </Method>""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        call_targets = {e.target for e in edges if e.kind == "CALLS"}
        assert "unknown.doSomething" in call_targets

    def test_instance_call_not_double_emitted_as_plain_call(self, tmp_path):
        """obj.method() must NOT also appear as a plain CALLS edge for 'obj'."""
        xml_path = self._make_class_xml(
            tmp_path, "AxClass", "TestNoDuplicate",
            "public class TestNoDuplicate\n{",
            methods="""
      <Method>
        <Name>run</Name>
        <Source><![CDATA[
public void run()
{
    SalesTable salesTable;
    salesTable.update();
}
]]></Source>
      </Method>""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        calls = [(e.target, e.extra) for e in edges if e.kind == "CALLS"]
        # Resolved target present
        assert any(t == "SalesTable.update" for t, _ in calls)
        # 'salesTable' alone must NOT appear as a plain call target
        assert not any(t == "salesTable" for t, _ in calls)

    def test_multiple_var_declarations(self, tmp_path):
        """Multiple typed vars in one method body are all tracked."""
        xml_path = self._make_class_xml(
            tmp_path, "AxClass", "TestMultiVar",
            "public class TestMultiVar\n{",
            methods="""
      <Method>
        <Name>process</Name>
        <Source><![CDATA[
public void process()
{
    CustTable custTable;
    VendTable vendTable;
    custTable.insert();
    vendTable.delete();
}
]]></Source>
      </Method>""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        call_targets = {e.target for e in edges if e.kind == "CALLS"}
        assert "CustTable.insert" in call_targets
        assert "VendTable.delete" in call_targets

    def test_this_and_super_ignored(self, tmp_path):
        """this.method() and super.method() must not be emitted as CALLS edges."""
        xml_path = self._make_class_xml(
            tmp_path, "AxClass", "TestKeywords",
            "public class TestKeywords\n{",
            methods="""
      <Method>
        <Name>run</Name>
        <Source><![CDATA[
public void run()
{
    this.helper();
    super.run();
}
]]></Source>
      </Method>""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        call_targets = {e.target for e in edges if e.kind == "CALLS"}
        assert "this.helper" not in call_targets
        assert "super.run" not in call_targets

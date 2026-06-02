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


class TestXppCoCResolution:
    """Stronger Chain of Command (CoC) WRAPS resolution: naming fallback + signature awareness."""

    def _build_coc_repo(
        self,
        tmp_path: Path,
        ext_name: str,
        ext_decl: str,
        ext_method_params: str,
        base_methods: list[tuple[str, str]],  # (method_name, params)
    ) -> "GraphStore":
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        ext_xml = (
            repo_root / "Metadata" / "Pkg" / "Pkg" / "AxClass" / f"{ext_name}.xml"
        )
        methods_xml = f"""
      <Method>
        <Name>process</Name>
        <Source><![CDATA[
public void process({ext_method_params})
{{
    next process({ext_method_params.split()[0] if ext_method_params else ""});
}}
]]></Source>
      </Method>"""
        _write_xpp_class(ext_xml, f"""<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>{ext_name}</Name>
  <SourceCode>
    <Declaration><![CDATA[
{ext_decl}
]]></Declaration>
    <Methods>{methods_xml}
    </Methods>
  </SourceCode>
</AxClass>
""")

        base_root = tmp_path / "PackagesLocalDirectory"
        base_method_xml = "".join(
            f"""
      <Method>
        <Name>process</Name>
        <Source><![CDATA[
public void process({params})
{{
}}
]]></Source>
      </Method>"""
            for _, params in base_methods
        )
        base_xml = (
            base_root / "Base" / "Base" / "AxClass" / "BaseService.xml"
        )
        _write_xpp_class(base_xml, f"""<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>BaseService</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class BaseService
{{
}}
]]></Declaration>
    <Methods>{base_method_xml}
    </Methods>
  </SourceCode>
</AxClass>
""")
        db_path = repo_root / ".code-review-graph" / "graph.db"
        store = GraphStore(db_path)
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=[str(ext_xml.relative_to(repo_root))],
        ):
            full_build(repo_root, store, xpp_base_roots=[str(base_root)])
        return store

    def test_extension_naming_fallback(self, tmp_path):
        """Class named BaseService_Extension without [ExtensionOf] still gets WRAPS via name suffix."""
        store = self._build_coc_repo(
            tmp_path,
            ext_name="BaseService_Extension",
            ext_decl="public class BaseService_Extension\n{",
            ext_method_params="str reason",
            base_methods=[("process", "str reason")],
        )
        try:
            wraps = store._conn.execute(
                "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind='WRAPS'"
            ).fetchall()
            assert wraps, "Expected WRAPS edge from _Extension suffix fallback"
            assert any("process" in r["target_qualified"] for r in wraps)
        finally:
            store.close()

    def test_signature_exact_match_preferred(self, tmp_path):
        """When two overloads exist, param-count match wins over name-only."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        ext_xml = (
            repo_root / "Metadata" / "Pkg" / "Pkg" / "AxClass"
            / "BaseService_Extension.xml"
        )
        _write_xpp_class(ext_xml, """<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>BaseService_Extension</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class BaseService_Extension
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>process</Name>
        <Source><![CDATA[
public void process(str reason, int count)
{
    next process(reason, count);
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxClass>
""")
        base_root = tmp_path / "PackagesLocalDirectory"
        base_xml = base_root / "Base" / "Base" / "AxClass" / "BaseService.xml"
        _write_xpp_class(base_xml, """<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>BaseService</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class BaseService
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>process</Name>
        <Source><![CDATA[
public void process()
{
}
]]></Source>
      </Method>
      <Method>
        <Name>process</Name>
        <Source><![CDATA[
public void process(str reason, int count)
{
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxClass>
""")
        db_path = repo_root / ".code-review-graph" / "graph.db"
        store = GraphStore(db_path)
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=[str(ext_xml.relative_to(repo_root))],
        ):
            full_build(repo_root, store, xpp_base_roots=[str(base_root)])
        try:
            wraps = store._conn.execute(
                "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind='WRAPS'"
            ).fetchall()
            assert wraps
            # The matching edge should be exact confidence (2-param version matched).
            import json as _json
            for row in wraps:
                extra = _json.loads(row["extra"] or "{}")
                assert extra.get("xpp_wraps_confidence") == "exact", \
                    f"Expected exact confidence, got: {extra}"
        finally:
            store.close()

    def test_wraps_confidence_name_only_when_no_param_match(self, tmp_path):
        """When param counts don't match, confidence is name_only not exact."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        ext_xml = (
            repo_root / "Metadata" / "Pkg" / "Pkg" / "AxClass"
            / "BaseService_Extension.xml"
        )
        _write_xpp_class(ext_xml, """<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>BaseService_Extension</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class BaseService_Extension
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>process</Name>
        <Source><![CDATA[
public void process(str reason, int count, boolean flag)
{
    next process(reason, count, flag);
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxClass>
""")
        base_root = tmp_path / "PackagesLocalDirectory"
        base_xml = base_root / "Base" / "Base" / "AxClass" / "BaseService.xml"
        _write_xpp_class(base_xml, """<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>BaseService</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class BaseService
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>process</Name>
        <Source><![CDATA[
public void process()
{
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxClass>
""")
        db_path = repo_root / ".code-review-graph" / "graph.db"
        store = GraphStore(db_path)
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=[str(ext_xml.relative_to(repo_root))],
        ):
            full_build(repo_root, store, xpp_base_roots=[str(base_root)])
        try:
            wraps = store._conn.execute(
                "SELECT extra FROM edges WHERE kind='WRAPS'"
            ).fetchall()
            assert wraps
            import json as _json
            for row in wraps:
                extra = _json.loads(row["extra"] or "{}")
                assert extra.get("xpp_wraps_confidence") == "name_only"
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


class TestXppFormControlExtraction:
    """Form control methods and datasource event extraction."""

    def setup_method(self):
        self.parser = CodeParser()

    def _make_form_xml(self, tmp_path, name: str, body: str) -> "Path":
        xml_path = (
            tmp_path / "Metadata" / "Pkg" / "Pkg" / "AxForm" / f"{name}.xml"
        )
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(body, encoding="utf-8")
        return xml_path

    def test_form_control_method_extracted(self, tmp_path):
        """Methods inside <Design>/<Controls>/AxFormControl are extracted as Function nodes."""
        xml_path = self._make_form_xml(
            tmp_path, "SalesForm",
            """<?xml version="1.0" encoding="utf-8"?>
<AxForm>
  <Name>SalesForm</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class SalesForm extends FormRun
{
}
]]></Declaration>
  </SourceCode>
  <Design>
    <Controls>
      <AxFormControl>
        <Name>OKButton</Name>
        <Methods>
          <Method>
            <Name>clicked</Name>
            <Source><![CDATA[
public void clicked()
{
    element.close();
}
]]></Source>
          </Method>
        </Methods>
      </AxFormControl>
    </Controls>
  </Design>
</AxForm>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        fn_names = {n.name for n in nodes if n.kind == "Function"}
        assert "OKButton_clicked" in fn_names

    def test_form_control_event_flagged(self, tmp_path):
        """Event methods (clicked, modified) are annotated with xpp_control_event=True."""
        xml_path = self._make_form_xml(
            tmp_path, "CustForm",
            """<?xml version="1.0" encoding="utf-8"?>
<AxForm>
  <Name>CustForm</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class CustForm extends FormRun
{
}
]]></Declaration>
  </SourceCode>
  <Design>
    <Controls>
      <AxFormControl>
        <Name>NameField</Name>
        <Methods>
          <Method>
            <Name>modified</Name>
            <Source><![CDATA[
public boolean modified()
{
    return super.modified();
}
]]></Source>
          </Method>
        </Methods>
      </AxFormControl>
    </Controls>
  </Design>
</AxForm>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        event_nodes = [
            n for n in nodes
            if n.kind == "Function" and n.extra.get("xpp_control_event")
        ]
        assert any(n.name == "NameField_modified" for n in event_nodes)

    def test_form_control_method_metadata(self, tmp_path):
        """Control method nodes carry xpp_control and xpp_control_method in extra."""
        xml_path = self._make_form_xml(
            tmp_path, "VendForm",
            """<?xml version="1.0" encoding="utf-8"?>
<AxForm>
  <Name>VendForm</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class VendForm extends FormRun
{
}
]]></Declaration>
  </SourceCode>
  <Design>
    <Controls>
      <AxFormControl>
        <Name>SaveBtn</Name>
        <Methods>
          <Method>
            <Name>clicked</Name>
            <Source><![CDATA[
public void clicked()
{
    this.doSave();
}
]]></Source>
          </Method>
        </Methods>
      </AxFormControl>
    </Controls>
  </Design>
</AxForm>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        ctrl_node = next(
            (n for n in nodes if n.kind == "Function" and n.name == "SaveBtn_clicked"),
            None,
        )
        assert ctrl_node is not None
        assert ctrl_node.extra.get("xpp_control") == "SaveBtn"
        assert ctrl_node.extra.get("xpp_control_method") == "clicked"

    def test_datasource_event_flagged(self, tmp_path):
        """Datasource event methods (init, validateWrite, etc.) are annotated."""
        xml_path = self._make_form_xml(
            tmp_path, "ProjForm",
            """<?xml version="1.0" encoding="utf-8"?>
<AxForm>
  <Name>ProjForm</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class ProjForm extends FormRun
{
}
]]></Declaration>
  </SourceCode>
  <DataSources>
    <AxFormDataSource>
      <Name>ProjTable</Name>
      <Table>ProjTable</Table>
      <Methods>
        <Method>
          <Name>validateWrite</Name>
          <Source><![CDATA[
public boolean validateWrite()
{
    return super.validateWrite();
}
]]></Source>
        </Method>
        <Method>
          <Name>customHelper</Name>
          <Source><![CDATA[
public void customHelper()
{
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
        event_nodes = [n for n in nodes if n.extra.get("xpp_ds_event")]
        non_event_nodes = [
            n for n in nodes
            if n.kind == "Function" and n.name == "customHelper"
        ]
        assert any(n.name == "validateWrite" for n in event_nodes)
        assert non_event_nodes  # customHelper present but NOT flagged as event
        assert not non_event_nodes[0].extra.get("xpp_ds_event")


class TestXppTableFieldGroupsEdtEnum:
    """Field group membership, EDT inheritance chains, and enum value nodes."""

    def setup_method(self):
        self.parser = CodeParser()

    def _write_xml(self, tmp_path, folder: str, name: str, body: str):
        xml_path = tmp_path / "Metadata" / "Pkg" / "Pkg" / folder / f"{name}.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(body, encoding="utf-8")
        return xml_path

    def test_table_field_group_references(self, tmp_path):
        """Fields inside a FieldGroup emit REFERENCES(field_group) edges."""
        xml_path = self._write_xml(
            tmp_path, "AxTable", "SalesTable",
            """<?xml version="1.0" encoding="utf-8"?>
<AxTable>
  <Name>SalesTable</Name>
  <FieldGroups>
    <AxTableFieldGroup>
      <Name>AutoIdentification</Name>
      <Fields>
        <AxTableFieldGroupField>
          <DataField>SalesId</DataField>
        </AxTableFieldGroupField>
        <AxTableFieldGroupField>
          <DataField>CustAccount</DataField>
        </AxTableFieldGroupField>
      </Fields>
    </AxTableFieldGroup>
  </FieldGroups>
</AxTable>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        fg_edges = [
            e for e in edges
            if e.kind == "REFERENCES" and e.extra.get("xpp_ref_kind") == "field_group"
        ]
        fg_targets = {e.target for e in fg_edges}
        assert "SalesId" in fg_targets
        assert "CustAccount" in fg_targets
        assert all(e.extra.get("xpp_field_group") == "AutoIdentification" for e in fg_edges)

    def test_edt_inherits_edge(self, tmp_path):
        """AxEdt with <Extends> emits an INHERITS edge to the parent EDT."""
        xml_path = self._write_xml(
            tmp_path, "AxEdt", "SalesIdBase_MY",
            """<?xml version="1.0" encoding="utf-8"?>
<AxEdt>
  <Name>SalesIdBase_MY</Name>
  <Extends>SalesIdBase</Extends>
</AxEdt>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        inherits_edges = [e for e in edges if e.kind == "INHERITS"]
        assert any(e.target == "SalesIdBase" for e in inherits_edges)
        assert any(e.extra.get("xpp_ref_kind") == "edt" for e in inherits_edges)

    def test_edt_extension_inherits(self, tmp_path):
        """AxEdtExtension with <Extends> also emits INHERITS."""
        xml_path = self._write_xml(
            tmp_path, "AxEdtExtension", "VendAccount_Extension",
            """<?xml version="1.0" encoding="utf-8"?>
<AxEdtExtension>
  <Name>VendAccount_Extension</Name>
  <Extends>AccountNum</Extends>
</AxEdtExtension>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        assert any(e.kind == "INHERITS" and e.target == "AccountNum" for e in edges)

    def test_enum_value_nodes_extracted(self, tmp_path):
        """Enum values are extracted as Field nodes with xpp_enum_value=True."""
        xml_path = self._write_xml(
            tmp_path, "AxEnum", "SalesStatus",
            """<?xml version="1.0" encoding="utf-8"?>
<AxEnum>
  <Name>SalesStatus</Name>
  <EnumValues>
    <AxEnumValue>
      <Name>None</Name>
    </AxEnumValue>
    <AxEnumValue>
      <Name>Backorder</Name>
    </AxEnumValue>
    <AxEnumValue>
      <Name>Delivered</Name>
    </AxEnumValue>
    <AxEnumValue>
      <Name>Invoiced</Name>
    </AxEnumValue>
  </EnumValues>
</AxEnum>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        enum_fields = {n.name for n in nodes if n.kind == "Field" and n.extra.get("xpp_enum_value")}
        assert "None" in enum_fields
        assert "Backorder" in enum_fields
        assert "Delivered" in enum_fields
        assert "Invoiced" in enum_fields

    def test_enum_value_contains_edges(self, tmp_path):
        """Each enum value has a CONTAINS edge from the parent artifact."""
        xml_path = self._write_xml(
            tmp_path, "AxEnum", "CustPaymMode",
            """<?xml version="1.0" encoding="utf-8"?>
<AxEnum>
  <Name>CustPaymMode</Name>
  <EnumValues>
    <AxEnumValue>
      <Name>None</Name>
    </AxEnumValue>
    <AxEnumValue>
      <Name>Check</Name>
    </AxEnumValue>
  </EnumValues>
</AxEnum>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        contains_targets = {e.target for e in edges if e.kind == "CONTAINS"}
        assert any("None" in t for t in contains_targets)
        assert any("Check" in t for t in contains_targets)


class TestXppBaseIndexPerformance:
    """Base-index builder uses os.walk so it handles large trees efficiently."""

    def test_index_built_from_simulated_large_tree(self, tmp_path):
        """Simulate a multi-package tree; verify index only contains Ax* folders."""
        from code_review_graph.xpp_resolver import _build_base_index, _BASE_INDEX_CACHE

        base = tmp_path / "PackagesLocalDirectory"
        # Create a realistic 3-package layout with Ax* and non-Ax* folders.
        for pkg in ("ApplicationSuite", "ApplicationFoundation", "Directory"):
            for model in (pkg,):
                for folder in ("AxClass", "AxTable", "SomeOtherFolder"):
                    d = base / pkg / model / folder
                    d.mkdir(parents=True)
                    (d / "Artifact1.xml").write_text("<root/>")
                    (d / "Artifact2.xml").write_text("<root/>")
                    (d / "Artifact2.txt").write_text("ignored")  # non-xml

        # Clear cache so this call goes through _build_base_index fresh.
        key = (str(base.resolve()),)
        _BASE_INDEX_CACHE.pop(key, None)

        index = _build_base_index([str(base)])

        # Only AxClass and AxTable entries should appear (not SomeOtherFolder).
        assert "Artifact1" in index
        assert "Artifact2" in index
        for stem, entries in index.items():
            for folder, _path in entries:
                assert folder in ("AxClass", "AxTable"), f"Unexpected folder: {folder}"

        # Non-xml file should not appear.
        assert all(
            str(path).endswith(".xml")
            for entries in index.values()
            for _, path in entries
        )


class TestXppDataAccessSemantics:
    """Richer SELECT/data-access edge extraction: modifiers, aggregates, order/group by, SysDa."""

    def setup_method(self):
        self.parser = CodeParser()

    def _make_class_xml(self, tmp_path, name: str, method_source: str):
        xml_path = tmp_path / "Metadata" / "Pkg" / "Pkg" / "AxClass" / f"{name}.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(f"""<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>{name}</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class {name}
{{
}}
]]></Declaration>
    <Methods>
      <Method>
        <Name>run</Name>
        <Source><![CDATA[
{method_source}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxClass>
""", encoding="utf-8")
        return xml_path

    def test_select_with_firstonly_modifier(self, tmp_path):
        """firstOnly modifier is captured in xpp_select_modifiers on the ACCESSES edge."""
        xml_path = self._make_class_xml(tmp_path, "TestMod", """
public void run()
{
    select firstOnly CustTable where CustTable.AccountNum == "C001";
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        table_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.target == "CustTable"
            and e.extra.get("xpp_ref_kind") == "table"
        ]
        assert table_edges, "Expected ACCESSES edge for CustTable"
        assert "firstonly" in table_edges[0].extra.get("xpp_select_modifiers", [])

    def test_select_with_forupdate_modifier(self, tmp_path):
        """forUpdate modifier is captured."""
        xml_path = self._make_class_xml(tmp_path, "TestForUpdate", """
public void run()
{
    select forUpdate SalesTable where SalesTable.SalesId == salesId;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        table_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.target == "SalesTable"
        ]
        assert table_edges
        assert "forupdate" in table_edges[0].extra.get("xpp_select_modifiers", [])

    def test_select_multiple_modifiers(self, tmp_path):
        """Multiple modifiers on one select are all captured."""
        xml_path = self._make_class_xml(tmp_path, "TestMultiMod", """
public void run()
{
    select forUpdate crossCompany firstOnly VendTable;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        table_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.target == "VendTable"
        ]
        assert table_edges
        mods = table_edges[0].extra.get("xpp_select_modifiers", [])
        assert "forupdate" in mods
        assert "crosscompany" in mods
        assert "firstonly" in mods

    def test_select_no_modifiers_no_modifier_key(self, tmp_path):
        """Plain select without modifiers does NOT set xpp_select_modifiers."""
        xml_path = self._make_class_xml(tmp_path, "TestNoMod", """
public void run()
{
    select ProjTable;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        table_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.target == "ProjTable"
        ]
        assert table_edges
        assert "xpp_select_modifiers" not in table_edges[0].extra

    def test_aggregate_sum_captured(self, tmp_path):
        """sum(field) emits ACCESSES(aggregate) with xpp_aggregate_fn=sum."""
        xml_path = self._make_class_xml(tmp_path, "TestAggSum", """
public void run()
{
    select sum(Amount) from SalesLine where SalesLine.SalesId == salesId;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        agg_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_ref_kind") == "aggregate"
        ]
        assert any(
            e.target == "Amount" and e.extra.get("xpp_aggregate_fn") == "sum"
            for e in agg_edges
        )

    def test_aggregate_count_captured(self, tmp_path):
        """countof(field) emits ACCESSES(aggregate)."""
        xml_path = self._make_class_xml(tmp_path, "TestAggCount", """
public void run()
{
    select countof(RecId) from InventTable;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        agg_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_ref_kind") == "aggregate"
        ]
        assert any(e.target == "RecId" for e in agg_edges)

    def test_order_by_field_captured(self, tmp_path):
        """order by fields emit ACCESSES(order_field) edges."""
        xml_path = self._make_class_xml(tmp_path, "TestOrderBy", """
public void run()
{
    select SalesTable order by SalesId, CustAccount;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        order_targets = {
            e.target for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_ref_kind") == "order_field"
        }
        assert "SalesId" in order_targets
        assert "CustAccount" in order_targets

    def test_group_by_field_captured(self, tmp_path):
        """group by fields emit ACCESSES(group_field) edges."""
        xml_path = self._make_class_xml(tmp_path, "TestGroupBy", """
public void run()
{
    select CustAccount from CustTable group by CustAccount;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        group_targets = {
            e.target for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_ref_kind") == "group_field"
        }
        assert "CustAccount" in group_targets

    def test_sysdaquery_api_detected(self, tmp_path):
        """new SysDaQueryObject(...) emits ACCESSES(sysdaquery) edge."""
        xml_path = self._make_class_xml(tmp_path, "TestSysDa", """
public void run()
{
    SysDaQueryObject query = new SysDaQueryObject(tableNum(CustTable));
    SysDaSelectParameters params = new SysDaSelectParameters();
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        sysdaquery_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_ref_kind") == "sysdaquery"
        ]
        targets = {e.target for e in sysdaquery_edges}
        assert "SysDaQueryObject" in targets
        assert "SysDaSelectParameters" in targets

    def test_insert_recordset_op_captured(self, tmp_path):
        """insert_recordset sets xpp_select_op on the ACCESSES edge."""
        xml_path = self._make_class_xml(tmp_path, "TestInsertRec", """
public void run()
{
    insert_recordset DestTable(Field1) select Field1 from SrcTable;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        dml_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_select_op") == "insert_recordset"
        ]
        assert dml_edges, "Expected insert_recordset ACCESSES edge"

    def test_select_from_explicit_fields_captures_table(self, tmp_path):
        """select Field1, Field2 from Table captures the table as ACCESSES(table)."""
        xml_path = self._make_class_xml(tmp_path, "TestFromTable", """
public void run()
{
    select SalesId, CustAccount from SalesTable where SalesTable.SalesStatus == SalesStatus::Open;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        table_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.target == "SalesTable"
            and e.extra.get("xpp_ref_kind") == "table"
        ]
        assert table_edges, "Expected ACCESSES(table) edge for SalesTable"

    def test_select_from_explicit_fields_emits_select_fields(self, tmp_path):
        """select Field1, Field2 from Table emits ACCESSES(select_field) for each field."""
        xml_path = self._make_class_xml(tmp_path, "TestFromFields", """
public void run()
{
    select SalesId, CustAccount from SalesTable;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        field_targets = {
            e.target for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_ref_kind") == "select_field"
        }
        assert "SalesId" in field_targets
        assert "CustAccount" in field_targets

    def test_select_aggregate_from_captures_table(self, tmp_path):
        """select sum(Field) from Table captures the table even when aggregate precedes FROM."""
        xml_path = self._make_class_xml(tmp_path, "TestAggFrom", """
public void run()
{
    select sum(LineAmount) from SalesLine where SalesLine.SalesId == salesId;
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        table_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.target == "SalesLine"
            and e.extra.get("xpp_ref_kind") == "table"
        ]
        assert table_edges, "Expected ACCESSES(table) edge for SalesLine"

    def test_tablenum_emits_accesses_tablenum(self, tmp_path):
        """tableNum(TableName) in QueryBuildDataSource emits ACCESSES(tablenum) edge."""
        xml_path = self._make_class_xml(tmp_path, "TestTableNum", """
public void buildQuery()
{
    Query query = new Query();
    QueryBuildDataSource qbds = query.addDataSource(tableNum(SalesTable));
    qbds.addRange(fieldNum(SalesTable, SalesId));
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        tablenum_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_ref_kind") == "tablenum"
        ]
        assert any(e.target == "SalesTable" for e in tablenum_edges), \
            "Expected ACCESSES(tablenum) edge for SalesTable"

    def test_fieldnum_emits_accesses_fieldnum(self, tmp_path):
        """fieldNum(Table, Field) emits ACCESSES(fieldnum) with xpp_table annotation."""
        xml_path = self._make_class_xml(tmp_path, "TestFieldNum", """
public void buildQuery()
{
    QueryBuildDataSource qbds = query.addDataSource(tableNum(CustTable));
    qbds.addRange(fieldNum(CustTable, AccountNum));
}
""")
        nodes, edges = self.parser.parse_file(xml_path)
        fieldnum_edges = [
            e for e in edges
            if e.kind == "ACCESSES" and e.extra.get("xpp_ref_kind") == "fieldnum"
        ]
        assert any(
            e.target == "CustTable.AccountNum" and e.extra.get("xpp_table") == "CustTable"
            for e in fieldnum_edges
        ), "Expected ACCESSES(fieldnum) edge for CustTable.AccountNum"


class TestXppEventSupport:
    """PreEventHandler/PostEventHandler distinction and attribute-based event handlers."""

    def setup_method(self):
        self.parser = CodeParser()

    def _write_xml(self, tmp_path, folder: str, name: str, body: str):
        xml_path = tmp_path / "Metadata" / "Pkg" / "Pkg" / folder / f"{name}.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(body, encoding="utf-8")
        return xml_path

    def test_axeventsubscription_pre_event_type(self, tmp_path):
        """AxEventSubscription with EventType=Pre sets xpp_event_type=pre on HANDLES edge."""
        xml_path = self._write_xml(
            tmp_path, "AxEventSubscription", "OnInsertPre",
            """<?xml version="1.0" encoding="utf-8"?>
<AxEventSubscription>
  <Name>OnInsertPre</Name>
  <Publisher>SalesTable</Publisher>
  <PublisherMethod>onInserted</PublisherMethod>
  <EventType>Pre</EventType>
  <EventHandler>MyHandler</EventHandler>
</AxEventSubscription>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        handles_edges = [e for e in edges if e.kind == "HANDLES"]
        assert handles_edges
        assert handles_edges[0].extra.get("xpp_event_type") == "pre"
        assert handles_edges[0].target == "SalesTable.onInserted"

    def test_axeventsubscription_post_event_type(self, tmp_path):
        """AxEventSubscription with EventType=Post sets xpp_event_type=post."""
        xml_path = self._write_xml(
            tmp_path, "AxEventSubscription", "OnInsertPost",
            """<?xml version="1.0" encoding="utf-8"?>
<AxEventSubscription>
  <Name>OnInsertPost</Name>
  <Publisher>SalesTable</Publisher>
  <PublisherMethod>onInserted</PublisherMethod>
  <EventType>Post</EventType>
  <EventHandler>MyHandler</EventHandler>
</AxEventSubscription>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        handles_edges = [e for e in edges if e.kind == "HANDLES"]
        assert handles_edges
        assert handles_edges[0].extra.get("xpp_event_type") == "post"

    def test_axeventsubscription_delegate_via_publisher_event(self, tmp_path):
        """PublisherEvent (delegate) is preferred over PublisherMethod; sets xpp_event_kind=delegate."""
        xml_path = self._write_xml(
            tmp_path, "AxEventSubscription", "DelegateHandler",
            """<?xml version="1.0" encoding="utf-8"?>
<AxEventSubscription>
  <Name>DelegateHandler</Name>
  <Publisher>SalesFormLetter</Publisher>
  <PublisherEvent>onPrintingDelegate</PublisherEvent>
  <EventType>Delegate</EventType>
  <EventHandler>PrintHandler</EventHandler>
</AxEventSubscription>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        handles_edges = [e for e in edges if e.kind == "HANDLES"]
        assert handles_edges
        assert handles_edges[0].extra.get("xpp_event_kind") == "delegate"
        assert handles_edges[0].target == "SalesFormLetter.onPrintingDelegate"

    def test_attribute_data_event_handler(self, tmp_path):
        """[DataEventHandler(classStr(SalesTable), enumStr(DataEventType, Inserted))]
        on a method emits a HANDLES edge and sets xpp_event_handler=True on the node."""
        xml_path = self._write_xml(
            tmp_path, "AxClass", "SalesTableEventHandler",
            """<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>SalesTableEventHandler</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class SalesTableEventHandler
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>onInserted</Name>
        <Source><![CDATA[
[DataEventHandler(classStr(SalesTable), enumStr(DataEventType, Inserted))]
public static void onInserted(Common sender, DataEventArgs e)
{
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxClass>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        handles_edges = [
            e for e in edges
            if e.kind == "HANDLES"
            and e.extra.get("xpp_event_handler_kind") == "dataeventhandler"
        ]
        assert handles_edges, "Expected HANDLES edge from DataEventHandler attribute"
        assert "SalesTable" in handles_edges[0].target
        fn_node = next(
            (n for n in nodes if n.kind == "Function" and n.name == "onInserted"), None
        )
        assert fn_node is not None
        assert fn_node.extra.get("xpp_event_handler")

    def test_attribute_form_datasource_event_handler(self, tmp_path):
        """[FormDataSourceEventHandler(...)] emits HANDLES with xpp_event_handler_kind=formdatasourceeventhandler."""
        xml_path = self._write_xml(
            tmp_path, "AxClass", "SalesOrderFormHandler",
            """<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>SalesOrderFormHandler</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class SalesOrderFormHandler
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>onValidateWrite</Name>
        <Source><![CDATA[
[FormDataSourceEventHandler(formStr(SalesOrder), formDataSourceStr(SalesOrder, SalesTable), FormDataSourceEventType::ValidateWrite)]
public static boolean onValidateWrite(FormDataSource sender, FormDataSourceEventArgs e)
{
    return true;
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxClass>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        handles_edges = [
            e for e in edges
            if e.kind == "HANDLES"
            and e.extra.get("xpp_event_handler_kind") == "formdatasourceeventhandler"
        ]
        assert handles_edges, "Expected HANDLES edge from FormDataSourceEventHandler"
        assert "SalesOrder" in handles_edges[0].target

    def test_attribute_form_control_event_handler(self, tmp_path):
        """[FormControlEventHandler(...)] emits HANDLES with xpp_event_handler_kind=formcontroleventhandler."""
        xml_path = self._write_xml(
            tmp_path, "AxClass", "SalesFormControlHandler",
            """<?xml version="1.0" encoding="utf-8"?>
<AxClass>
  <Name>SalesFormControlHandler</Name>
  <SourceCode>
    <Declaration><![CDATA[
public class SalesFormControlHandler
{
}
]]></Declaration>
    <Methods>
      <Method>
        <Name>onOkClicked</Name>
        <Source><![CDATA[
[FormControlEventHandler(formStr(SalesOrder), FormControlStr(SalesOrder, OKButton), FormControlEventType::Clicked)]
public static void onOkClicked(FormControl sender, FormControlEventArgs e)
{
}
]]></Source>
      </Method>
    </Methods>
  </SourceCode>
</AxClass>
""",
        )
        nodes, edges = self.parser.parse_file(xml_path)
        handles_edges = [
            e for e in edges
            if e.kind == "HANDLES"
            and e.extra.get("xpp_event_handler_kind") == "formcontroleventhandler"
        ]
        assert handles_edges, "Expected HANDLES edge from FormControlEventHandler"

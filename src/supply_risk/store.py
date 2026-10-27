import json
import os
from contextlib import contextmanager

from neo4j import GraphDatabase
from neo4j.exceptions import ConfigurationError, Neo4jError, ServiceUnavailable, SessionExpired

from .errors import DataError, InfrastructureError
from .loader import LABELS, RELATIONS, canonical, validate
from .overview import build_overview


@contextmanager
def connect():
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise InfrastructureError("Задайте NEO4J_PASSWORD в окружении")
    try:
        with GraphDatabase.driver(os.environ.get("NEO4J_URI", "bolt://127.0.0.1:17687"),
                                  auth=(os.environ.get("NEO4J_USER", "neo4j"), password),
                                  connection_timeout=5, max_transaction_retry_time=5) as driver:
            driver.verify_connectivity()
            yield Neo4jStore(driver)
    except (ConfigurationError, Neo4jError, ServiceUnavailable, SessionExpired, OSError) as exc:
        raise InfrastructureError(f"Neo4j недоступна или отклонила операцию: {type(exc).__name__}") from exc


def migrations():
    statements = [f"CREATE CONSTRAINT {label.lower()}_identity IF NOT EXISTS "
                  f"FOR (n:{label}) REQUIRE (n.dataset_id, n.id) IS UNIQUE"
                  for label in sorted(LABELS | {"Entity", "Run"})]
    statements += ["CREATE CONSTRAINT dataset_version IF NOT EXISTS FOR (n:DatasetVersion) "
                   "REQUIRE (n.dataset_id, n.version) IS UNIQUE",
                   "CREATE CONSTRAINT revision_identity IF NOT EXISTS FOR (n:Revision) "
                   "REQUIRE (n.dataset_id, n.version, n.id) IS UNIQUE",
                   "CREATE CONSTRAINT impact_summary_identity IF NOT EXISTS FOR (n:ImpactSummary) "
                   "REQUIRE (n.dataset_id, n.version, n.id) IS UNIQUE"]
    return statements


class Neo4jStore:
    def __init__(self, driver):
        self.driver = driver

    def migrate(self):
        with self.driver.session() as session:
            for statement in migrations():
                session.run(statement).consume()

    def import_dataset(self, dataset):
        validate(dataset)
        self.migrate()
        with self.driver.session() as session:
            return session.execute_write(self._import, dataset)

    @staticmethod
    def _import(tx, data):
        ds, version = data["dataset_id"], data["version"]
        # MERGE acquires the version lock before checking the immutable digest.
        row = tx.run("MERGE (d:DatasetVersion {dataset_id:$ds, version:$version}) "
                     "ON CREATE SET d.sha256=$hash, d.payload=$payload "
                     "RETURN d.sha256 AS hash, d.payload AS payload",
                     ds=ds, version=version, hash=data["sha256"], payload=canonical(data)).single()
        if row["hash"] != data["sha256"]:
            raise DataError(f"Версия {ds}/{version} уже существует с другим SHA-256")
        created, existing = 0, 0
        for e in data["entities"]:
            label = e["label"]
            previous = tx.run("MATCH (n:Entity {dataset_id:$ds,id:$id}) RETURN n.label AS label",
                              ds=ds, id=e["id"]).single()
            if previous and previous["label"] != label:
                raise DataError(f"Тип объекта {e['id']} изменился; используйте новый идентификатор")
            props = {k: v for k, v in e.items() if isinstance(v, (str, int, float, bool))}
            result = tx.run(f"MERGE (n:Entity:{label} {{dataset_id:$ds, id:$id}}) "
                            "ON CREATE SET n += $props "
                            "MERGE (v:Revision {dataset_id:$ds, version:$version, id:$id}) "
                            "ON CREATE SET v.payload=$payload "
                            "MERGE (v)-[:REVISION_OF {dataset_id:$ds, version:$version}]->(n)",
                            ds=ds, version=version, id=e["id"], props=props, payload=canonical(e)).consume()
            created += result.counters.nodes_created
            existing += 0 if result.counters.nodes_created else 1
        relationships_created = 0
        for e in data["entities"]:
            if e["label"] == "Observation":
                result = tx.run("MATCH (o:Observation {dataset_id:$ds,id:$id}), "
                                "(s:Scenario {dataset_id:$ds,id:$scenario}), "
                                "(p:Process {dataset_id:$ds,id:$process}) "
                                "MERGE (o)-[:ABOUT_SCENARIO {dataset_id:$ds,version:$version}]->(s) "
                                "MERGE (o)-[:OBSERVED_IN {dataset_id:$ds,version:$version}]->(p)",
                                ds=ds, version=version, id=e["id"], scenario=e["scenario_id"],
                                process=e["process_id"]).consume()
                relationships_created += result.counters.relationships_created
            elif e["label"] == "Assertion":
                result = tx.run(f"MATCH (a:Entity {{dataset_id:$ds,id:$subject}}), "
                                "(b:Entity {dataset_id:$ds,id:$object}), "
                                "(q:Assertion {dataset_id:$ds,id:$assertion}) "
                                f"MERGE (a)-[:{e['predicate']} {{dataset_id:$ds,version:$version, "
                                "assertion_id:$assertion}]->(b) "
                                "MERGE (q)-[:SUBJECT {dataset_id:$ds,version:$version}]->(a) "
                                "MERGE (q)-[:OBJECT {dataset_id:$ds,version:$version}]->(b)",
                                ds=ds, version=version, subject=e["subject_id"], object=e["object_id"],
                                assertion=e["id"]).consume()
                relationships_created += result.counters.relationships_created
            else:
                continue
            for source in e["evidence_ids"]:
                result = tx.run("MATCH (a:Entity {dataset_id:$ds,id:$a}), "
                                "(s:Source {dataset_id:$ds,id:$source}) "
                                "MERGE (a)-[:SUPPORTED_BY {dataset_id:$ds,version:$version}]->(s)",
                                ds=ds, version=version, a=e["id"], source=source).consume()
                relationships_created += result.counters.relationships_created
        return {"dataset_id": ds, "version": version, "sha256": data["sha256"],
                "created_nodes": created, "existing_objects": existing,
                "created_relationships": relationships_created,
                "overview": Neo4jStore._sync_overview(tx, data)["changes"]}

    @staticmethod
    def _sync_overview(tx, data):
        summaries = build_overview(data)
        changes = {"summaries": len(summaries), "created_nodes": 0, "created_relationships": 0}
        ds, version = data["dataset_id"], data["version"]
        for summary in summaries:
            props = {k: v for k, v in summary.items() if not isinstance(v, dict) and
                     (not isinstance(v, list) or all(isinstance(item, str) for item in v))}
            props["payload"] = canonical(summary)
            result = tx.run("MATCH (p:Process {dataset_id:$ds,id:$process}) "
                            "MERGE (s:ImpactSummary {dataset_id:$ds,version:$version,id:$id}) "
                            "SET s=$props "
                            "MERGE (p)-[:HAS_IMPACT_SUMMARY {dataset_id:$ds,version:$version}]->(s) "
                            "RETURN s.id AS id", ds=ds, version=version, id=summary["id"],
                            process=summary["process_id"], props=props)
            if result.single() is None:
                raise DataError("В графе отсутствует процесс сводки; повторите import исходного набора")
            counters = result.consume().counters
            changes["created_nodes"] += counters.nodes_created
            changes["created_relationships"] += counters.relationships_created
            for relation, label, ids in (("SUMMARIZES", "Observation", summary["observation_ids"]),
                                         ("BASED_ON", "Source", summary["evidence_ids"]),
                                         ("FOR_SCENARIO", "Scenario", summary["scenario_ids"])):
                tx.run(f"MATCH (s:ImpactSummary {{dataset_id:$ds,version:$version,id:$id}})-[r:{relation}]->(n) "
                       "WHERE r.dataset_id=$ds AND r.version=$version "
                       "AND (n.dataset_id<>$ds OR NOT n.id IN $ids) DELETE r",
                       ds=ds, version=version, id=summary["id"], ids=ids).consume()
                result = tx.run(f"UNWIND $ids AS target_id "
                                "MATCH (s:ImpactSummary {dataset_id:$ds,version:$version,id:$id}), "
                                f"(n:{label} {{dataset_id:$ds,id:target_id}}) "
                                f"MERGE (s)-[:{relation} {{dataset_id:$ds,version:$version}}]->(n) "
                                "RETURN count(n) AS matched", ds=ds, version=version,
                                id=summary["id"], ids=ids)
                if result.single()["matched"] != len(ids):
                    raise DataError("В графе отсутствуют данные сводки; повторите import исходного набора")
                changes["created_relationships"] += result.consume().counters.relationships_created
        return {"summaries": summaries, "changes": changes}

    def refresh_overview(self, dataset_id, version):
        data = self.load(dataset_id, version)
        self.migrate()
        with self.driver.session() as session:
            return session.execute_write(self._sync_overview, data)

    def load(self, dataset_id, version):
        with self.driver.session() as session:
            row = session.run("MATCH (d:DatasetVersion {dataset_id:$ds,version:$version}) "
                              "RETURN d.payload AS payload", ds=dataset_id, version=version).single()
        if row is None:
            raise DataError(f"Набор {dataset_id}/{version} не найден; сначала выполните import")
        return validate(json.loads(row["payload"]))

    def candidates(self, dataset, scenario, max_depth=5, max_paths=100):
        if type(max_depth) is not int or not 1 <= max_depth <= 20 or type(max_paths) is not int or not 1 <= max_paths <= 10000:
            raise DataError("max_depth: 1–20; max_paths: 1–10000")
        relations = "|".join(sorted(RELATIONS))
        # Bounds are validated integers, all identifiers and versions are parameters.
        query = (f"MATCH p=(s:Entity {{dataset_id:$ds,id:$start}})-[:{relations}*1..{max_depth}]-(t:Process) "
                 "WHERE ALL(n IN nodes(p) WHERE n.dataset_id=$ds) "
                 "AND ALL(r IN relationships(p) WHERE r.dataset_id=$ds AND r.version=$version) "
                 "AND ALL(n IN nodes(p) WHERE single(m IN nodes(p) WHERE m=n)) "
                 "AND ANY(n IN nodes(p) WHERE n.id=$affected) "
                 "RETURN [n IN nodes(p) | n.id] AS nodes, "
                 "[r IN relationships(p) | {assertion_id:r.assertion_id, type:type(r), "
                 "subject:startNode(r).id, object:endNode(r).id}] AS edges "
                 "ORDER BY nodes, [r IN relationships(p) | r.assertion_id] LIMIT $limit")
        with self.driver.session() as session:
            rows = [dict(r) for r in session.run(query, ds=dataset["dataset_id"], version=dataset["version"],
                                                start=scenario["origin_id"], affected=scenario["affected_service_id"],
                                                limit=max_paths + 1)]
            # Detect any simple prefix at the depth boundary, including ones not yet reaching a process.
            boundary = session.run(
                f"MATCH p=(s:Entity {{dataset_id:$ds,id:$start}})-[:{relations}*{max_depth}]-(t) "
                "WHERE ALL(n IN nodes(p) WHERE n.dataset_id=$ds) "
                "AND ALL(r IN relationships(p) WHERE r.dataset_id=$ds AND r.version=$version) "
                "AND ALL(n IN nodes(p) WHERE single(m IN nodes(p) WHERE m=n)) "
                "RETURN count(p)>0 AS hit", ds=dataset["dataset_id"], version=dataset["version"],
                start=scenario["origin_id"]).single()["hit"]
        return {"paths": rows[:max_paths], "max_paths": max_paths, "max_depth": max_depth,
                "paths_limit_reached": len(rows) > max_paths, "depth_limit_reached": boundary,
                "incomplete": len(rows) > max_paths or boundary}

    def save_run(self, report, output_ref):
        with self.driver.session() as session:
            session.run("CREATE (r:Run {dataset_id:$ds,id:$id,dataset_version:$version, "
                        "rules_version:$rules,input_hash:$hash,mode:$mode,parameters:$params, "
                        "output_ref:$ref,payload:$payload})", ds=report["dataset_id"], id=report["run_id"],
                        version=report["dataset_version"], rules=report["rules_version"], hash=report["input_hash"],
                        mode=report["mode"], params=canonical(report["effective_parameters"]),
                        ref=str(output_ref), payload=canonical(report)).consume()

    def load_run(self, dataset_id, version, run_id):
        with self.driver.session() as session:
            row = session.run("MATCH (r:Run {dataset_id:$ds,id:$id,dataset_version:$version}) "
                              "RETURN r.payload AS payload", ds=dataset_id, id=run_id, version=version).single()
        if row is None:
            raise DataError(f"Расчёт {run_id} не найден в выбранной версии")
        return json.loads(row["payload"])

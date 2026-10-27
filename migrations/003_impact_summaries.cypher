CREATE CONSTRAINT impact_summary_identity IF NOT EXISTS
FOR (n:ImpactSummary) REQUIRE (n.dataset_id, n.version, n.id) IS UNIQUE;

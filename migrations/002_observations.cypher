CREATE CONSTRAINT observation_identity IF NOT EXISTS
FOR (n:Observation) REQUIRE (n.dataset_id, n.id) IS UNIQUE;

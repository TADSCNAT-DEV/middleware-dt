"""Run with python3 scenarios/test_neo4j_preflight.py; no database or network required."""
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from test_discovery_auth import load_function
from pathlib import Path


class Neo4jPreflightTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.settings = SimpleNamespace(USE_NEO4J=True)
        self.check = load_function(
            Path('orchestrator/management/commands/generate_digitaltwins_from_devices.py'),
            'verify_neo4j_connection',
            {'db': self.db, 'settings': self.settings, 'CommandError': RuntimeError},
        )

    def test_connection_is_checked(self):
        self.check(False)
        self.db.cypher_query.assert_called_once_with('RETURN 1')

    def test_dry_run_does_not_connect(self):
        self.check(True)
        self.db.cypher_query.assert_not_called()

    def test_disabled_graph_does_not_connect(self):
        self.settings.USE_NEO4J = False
        self.check(False)
        self.db.cypher_query.assert_not_called()

    def test_failure_stops_generation_without_exposing_credentials(self):
        self.db.cypher_query.side_effect = Exception('bolt://user:secret@neo4j')
        with self.assertRaises(RuntimeError) as result:
            self.check(False)
        self.assertIn('nenhuma instancia', str(result.exception))
        self.assertNotIn('secret', str(result.exception))


if __name__ == '__main__':
    unittest.main()

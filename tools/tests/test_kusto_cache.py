#!/usr/bin/env python3
"""Deterministic Kusto metadata TTL and invalidation tests."""
import os
import sys
import io
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from bridge import state as _st
from bridge import kusto
from bridge.cognition import _cached_metadata_rows


class KustoMetadataCacheTests(unittest.TestCase):
    def setUp(self):
        with _st.kusto_metadata_cache_lock:
            _st.kusto_metadata_cache.clear()
        _st.kusto_table_columns_cache.clear()

    def tearDown(self):
        with _st.kusto_metadata_cache_lock:
            _st.kusto_metadata_cache.clear()
        _st.kusto_table_columns_cache.clear()

    def test_stable_metadata_is_cached_and_emits_hit_miss(self):
        calls = []
        events = []

        def query(*_args, **_kwargs):
            calls.append(1)
            return [{"Relation": "name", "Value": "Eva"}]

        with patch("bridge.cognition._kusto_query_direct", side_effect=query), patch(
            "bridge.kusto._kusto_query_direct", side_effect=query), patch(
            "bridge.kusto._telemetry_emit", side_effect=lambda event, **fields: events.append((event, fields))
        ):
            first = _cached_metadata_rows("profile", "https://example.com", "Eva", "profile query")
            second = _cached_metadata_rows("profile", "https://example.com", "Eva", "profile query")

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual([fields["hit"] for event, fields in events if event == "kusto_metadata_cache"], [False, True])

    def test_schema_cache_has_ttl_and_write_invalidation(self):
        calls = []

        def query(*_args, **kwargs):
            calls.append(kwargs.get("is_mgmt"))
            return [{"Schema": "Timestamp:datetime,Value:string"}]

        with patch("bridge.kusto._kusto_query_direct", side_effect=query):
            self.assertEqual(kusto._get_table_columns("https://example.com", "Eva", "Knowledge"), ["Timestamp", "Value"])
            self.assertEqual(kusto._get_table_columns("https://example.com", "Eva", "Knowledge"), ["Timestamp", "Value"])
            self.assertEqual(len(calls), 1)
            kusto._invalidate_kusto_metadata_cache(include_schema=True)
            self.assertEqual(kusto._get_table_columns("https://example.com", "Eva", "Knowledge"), ["Timestamp", "Value"])
        self.assertEqual(len(calls), 2)

    def test_metadata_entry_expires_and_concurrent_reads_are_safe(self):
        calls = []

        def loader():
            calls.append(1)
            return [{"Value": len(calls)}]

        with patch("bridge.kusto.time.monotonic", side_effect=[0.0, 0.5, 2.0]):
            self.assertEqual(kusto._kusto_metadata_cached("cluster", "db", "emotion", "q", loader, 1), [{"Value": 1}])
            self.assertEqual(kusto._kusto_metadata_cached("cluster", "db", "emotion", "q", loader, 1), [{"Value": 1}])
            self.assertEqual(kusto._kusto_metadata_cached("cluster", "db", "emotion", "q", loader, 1), [{"Value": 2}])
        self.assertEqual(len(calls), 2)

        with ThreadPoolExecutor(max_workers=8) as executor:
            values = list(executor.map(
                lambda _: kusto._kusto_metadata_cached("cluster", "db", "goals", "q2", loader, 60),
                range(8),
            ))
        self.assertTrue(all(value == values[0] for value in values))
        with _st.kusto_metadata_cache_lock:
            self.assertIn(next(key for key in _st.kusto_metadata_cache if key[2] == "goals"), _st.kusto_metadata_cache)

    def test_write_invalidation_preserves_schema_cache_but_drops_rows(self):
        loader = lambda: [{"Value": "stable"}]
        schema_loader = lambda: ["Timestamp"]
        kusto._kusto_metadata_cached("cluster", "db", "profile", "q", loader, 60)
        kusto._kusto_metadata_cached("cluster", "db", "schema", "schema-q", schema_loader, 60)
        kusto._invalidate_kusto_metadata_cache(include_schema=False)
        with _st.kusto_metadata_cache_lock:
            kinds = {key[2] for key in _st.kusto_metadata_cache}
        self.assertEqual(kinds, {"schema"})

    def test_token_refresh_invalidates_all_metadata(self):
        kusto._kusto_metadata_cached("cluster", "db", "profile", "q", lambda: [], 60)
        kusto._kusto_metadata_cached("cluster", "db", "schema", "schema-q", lambda: [], 60)
        with patch.object(_st, "kusto_credential", None):
            with patch.object(_st, "kusto_token_cache", "old"):
                kusto._invalidate_kusto_metadata_cache(include_schema=True)
        with _st.kusto_metadata_cache_lock:
            self.assertFalse(_st.kusto_metadata_cache)

    def test_query_failure_omits_untrusted_response_content(self):
        class Response:
            status_code = 500
            text = "private query literal from an upstream error"

        class Session:
            def post(self, *_args, **_kwargs):
                return Response()

            def close(self):
                pass

        output = io.StringIO()
        with patch.object(_st, "kusto_token_cache", "test-token"), \
                patch("requests.Session", return_value=Session()), redirect_stdout(output):
            self.assertIsNone(kusto._kusto_query_direct("https://example.com", "Eva", "private query literal"))
        self.assertNotIn("private query literal", output.getvalue())

    def test_seed_query_and_ingest_failures_omit_response_content(self):
        class Response:
            status_code = 500
            text = "private Kusto response literal"

            def json(self):
                return {"Exceptions": ["private Kusto response literal"]}

        class Session:
            def post(self, *_args, **_kwargs):
                return Response()

            def close(self):
                pass

        output = io.StringIO()
        with patch.object(_st, "kusto_token_cache", "test-token"), \
                patch("requests.Session", return_value=Session()), \
                patch.object(kusto, "_get_table_columns", return_value=None), redirect_stdout(output):
            _rows, error = kusto._kusto_query_with_error("https://example.com", "Eva", "private query literal")
            self.assertFalse(kusto._kusto_ingest_direct("https://example.com", "Eva", "Table", ["Value"], [{"Value": "private query literal"}]))
        self.assertNotIn("private Kusto response literal", error)
        self.assertNotIn("private Kusto response literal", output.getvalue())

    def test_recall_queries_are_not_using_metadata_cache(self):
        with open(os.path.join(TOOLS_DIR, "bridge", "cognition.py"), encoding="utf-8") as handle:
            source = handle.read()
        recall_start = source.index("# ── 5. Message-relevant knowledge")
        recall_end = source.index("# ── 6. Proactive data retrieval", recall_start)
        recall_slice = source[recall_start:recall_end]
        self.assertNotIn("_cached_metadata_rows", recall_slice)
        self.assertIn("lexical_query", recall_slice)
        self.assertIn("pool_query", recall_slice)


if __name__ == "__main__":
    unittest.main()
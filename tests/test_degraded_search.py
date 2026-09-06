import unittest

from gcores_crawler.query_core.config import SearchIndexConfig
from gcores_crawler.query_core.runtime import (
    run_search_query_with_runtime,
    semantic_degradation_reason,
)


class EmptyCatalog:
    def search_documents_by_terms(self, **_kwargs):
        return []


class FailingQdrant:
    def __init__(self, error):
        self.error = error

    def embed_query(self, _query):
        raise RuntimeError(self.error)


class DegradedSearchTest(unittest.TestCase):
    def setUp(self):
        self.config = SearchIndexConfig(root="data", collection_name="test")

    def run_degraded_search(self, error):
        return run_search_query_with_runtime(
            "test query",
            catalog=EmptyCatalog(),
            qdrant=FailingQdrant(error),
            config=self.config,
            alias_map={"__test__": "__test__"},
            limit=3,
        )

    def test_quota_errors_have_machine_readable_reason(self):
        for error in ("code 1113", "余额不足", "资源包已用完"):
            with self.subTest(error=error):
                self.assertEqual(semantic_degradation_reason(error), "embedding_quota_exhausted")

    def test_quota_degradation_asks_for_gcores_private_message(self):
        payload = self.run_degraded_search('HTTP 429: {"code":"1113","msg":"余额不足"}')

        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["degraded_reason"], "embedding_quota_exhausted")
        self.assertIn("机核私信 YQBelmont贲", payload["degraded_detail"])

    def test_other_semantic_failures_keep_generic_degradation(self):
        payload = self.run_degraded_search("connection refused")

        self.assertEqual(payload["degraded_reason"], "semantic_recall_unavailable")
        self.assertNotIn("机核私信", payload["degraded_detail"])


if __name__ == "__main__":
    unittest.main()

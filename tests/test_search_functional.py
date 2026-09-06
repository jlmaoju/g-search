import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gcores_crawler.query_core.catalog import QueryCatalog
from gcores_crawler.query_core.config import SearchTuning
from gcores_crawler.query_core.runtime import extract_query_terms, normalize_query_for_terms, rerank_hits


class TestCatalog:
    """An isolated catalog with the same query columns; no production data."""

    def __init__(self, root):
        self.db_path = Path(root) / "catalog.sqlite"
        self.records = {}
        self.participants = [{"name": f"host-{i}", "count": 1} for i in range(645)]
        self.record_reads = 0
        with self._connect() as db:
            columns = "doc_id TEXT, title TEXT, item_title TEXT, keyword_text TEXT, text_search TEXT, payload_json TEXT, doc_type TEXT, item_key TEXT, doc_index INTEGER"
            db.execute(f"CREATE TABLE search_documents ({columns})")
            db.execute(f"CREATE TABLE search_documents_scoped ({columns}, collection_name TEXT)")

    def _connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def get_item_record(self, *, item_key):
        self.record_reads += 1
        return self.records.get(item_key)

    def list_radio_participants(self, *, limit=None):
        return self.participants[:limit] if limit is not None else self.participants


class CatalogRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = TestCatalog(self.temp.name)
        self.catalog = QueryCatalog(self.temp.name, catalog=self.raw)

    def add_rows(self, collection=None):
        table = "search_documents_scoped" if collection else "search_documents"
        rows = []
        for i in range(1210):
            payload = {"category": "wanted" if i >= 1200 else "other", "users": ["rare", "cohost"] if i >= 1200 else ["common"]}
            row = (str(i), "price", "episode", "price", "price", json.dumps(payload), "scene_summary", f"{i:05}", i)
            rows.append((*row, collection) if collection else row)
        with self.raw._connect() as db:
            db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in rows[0])})", rows)

    def test_filters_apply_before_top_k_for_legacy_and_scoped_collections(self):
        for collection in (None, "test-release"):
            with self.subTest(collection=collection):
                self.add_rows(collection)
                rows = self.catalog.search_documents_by_terms(query_terms=["price"], collection_name=collection, categories=["wanted"], participants=["rare", "cohost"], limit=3)
                self.assertEqual([row["doc_id"] for row in rows], ["1200", "1201", "1202"])
                self.assertEqual(self.catalog.search_documents_by_terms(query_terms=["price"], collection_name=collection, participants=["rare", "absent"], limit=3), [])

    def test_like_literals_and_malformed_payloads(self):
        with self.raw._connect() as db:
            for doc_id, text, payload in [("literal", "100%_done", '{"category":"wanted","users":["rare"]}'), ("distractor", "100xxdone", "{}"), ("invalid", "100%_done", "invalid")]:
                db.execute("INSERT INTO search_documents VALUES (?,?,?,?,?,?,?,?,?)", (doc_id, text, "episode", text, text, payload, "scene_summary", doc_id, 0))
        rows = self.catalog.search_documents_by_terms(query_terms=["100%_"], categories=["wanted"], participants=["rare"], limit=1)
        self.assertEqual([row["doc_id"] for row in rows], ["literal"])
        self.assertEqual(self.catalog.search_documents_by_terms(query_terms=["price"], limit=0), [])

    def test_missing_item_is_not_cached_forever(self):
        self.assertIsNone(self.catalog.get_item_display_record(item_key="new"))
        self.raw.records["new"] = {"title": "new title"}
        self.assertEqual(self.catalog.get_item_display_record(item_key="new")["title"], "new title")

    def test_display_cache_expires_and_is_invalidated_by_database_revision(self):
        self.raw.records["a"] = {"title": "old"}
        with patch("gcores_crawler.query_core.catalog.time.monotonic", return_value=1):
            self.assertEqual(self.catalog.get_item_display_record(item_key="a")["title"], "old")
            self.catalog.get_item_display_record(item_key="a")
            self.assertEqual(self.raw.record_reads, 1)
            self.raw.records["a"] = {"title": "new"}
            stat = self.raw.db_path.stat()
            os.utime(self.raw.db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertEqual(self.catalog.get_item_display_record(item_key="a")["title"], "new")
        self.raw.records["a"] = {"title": "newer"}
        with patch("gcores_crawler.query_core.catalog.time.monotonic", return_value=32):
            self.assertEqual(self.catalog.get_item_display_record(item_key="a")["title"], "newer")

    def test_normalized_file_creation_and_update_are_visible(self):
        self.raw.records["a"] = {"title": "db title", "normalized_path": "item.json"}
        self.assertEqual(self.catalog.get_item_display_record(item_key="a")["title"], "db title")
        path = Path(self.temp.name) / "item.json"
        path.write_text('{"title":"normalized title"}', encoding="utf-8")
        self.assertEqual(self.catalog.get_item_display_record(item_key="a")["title"], "normalized title")
        path.write_text('{"title":"updated normalized title"}', encoding="utf-8")
        self.assertEqual(self.catalog.get_item_display_record(item_key="a")["title"], "updated normalized title")
        path.unlink()
        self.assertEqual(self.catalog.get_item_display_record(item_key="a")["title"], "db title")

    def test_cache_is_bounded_and_participant_limit_does_not_poison_full_list(self):
        self.catalog._cache_capacity = 3
        for i in range(5):
            self.raw.records[str(i)] = {"title": str(i)}
            self.catalog.get_item_display_record(item_key=str(i))
        self.assertLessEqual(len(self.catalog._cache), 3)
        self.assertEqual(len(self.catalog.list_radio_participants(limit=160)), 160)
        self.assertEqual(len(self.catalog.list_radio_participants()), 645)


class QueryClueRegressionTests(unittest.TestCase):
    def test_memory_boilerplate_is_not_a_ranking_clue(self):
        query = "我记得聊过涨价和行业压力的那期"
        terms = extract_query_terms(query)
        self.assertEqual(normalize_query_for_terms(query), "涨价和行业压力")
        self.assertTrue({"涨价", "行业压力"}.issubset(terms))
        self.assertFalse({"我记", "记得", "聊过", "那期"}.intersection(terms))

    def test_titles_negation_quotes_and_names_are_preserved(self):
        cases = {
            "不能结婚的男人": "不能结婚的男人",
            "应用商店": "应用商店",
            "如何用人工智能做游戏": "如何用人工智能做游戏",
            "节目制作": "节目制作",
            "《我记得》": "我记得",
            "我记得": "我记得",
            "我记得 Nadya 聊过游科和 ROG 的那一期": "Nadya",
        }
        for query, clue in cases.items():
            with self.subTest(query=query):
                self.assertIn(clue, extract_query_terms(query))
        self.assertIn("ROG", extract_query_terms("我记得 Nadya 聊过游科和 ROG 的那一期"))

    def test_relevant_price_fragment_outranks_equal_vector_score_boilerplate(self):
        hits = [
            {"doc_id": "noise", "doc_type": "scene_summary", "score": .6, "payload": {"item_key": "noise", "title": "我记得聊过", "keyword_text": "我记得聊过那期节目"}},
            {"doc_id": "price", "doc_type": "scene_summary", "score": .6, "payload": {"item_key": "price", "title": "PS5涨价", "keyword_text": "涨价与行业压力"}},
        ]
        result = rerank_hits(raw_hits=hits, query_terms=extract_query_terms("我记得聊过涨价和行业压力的那期"), tuning=SearchTuning())
        self.assertEqual(result[0]["doc_id"], "price")
        self.assertEqual(result[1]["matched_terms"], [])


if __name__ == "__main__":
    unittest.main()

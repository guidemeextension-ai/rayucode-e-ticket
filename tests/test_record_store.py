import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import record_store as store
import ticket_core as core
from scripts import migrate_records


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "records.json"
        self.contacts = Path(self.temp.name) / "contacts.json"
        self.data = patch.object(core, "DATA_FILE", self.path)
        self.data.start()
        self.contacts_patch = patch.object(store, "CONTACTS_FILE", self.contacts)
        self.contacts_patch.start()
        self.http = patch.object(store.requests, "post", side_effect=AssertionError("Network forbidden"))
        self.http.start()
        store._LEASES.clear()

    def tearDown(self):
        self.http.stop()
        self.contacts_patch.stop()
        self.data.stop()
        self.temp.cleanup()
        self.env.stop()

    def test_facade_crud_and_patchable_data_file(self):
        self.assertEqual(core.load_records(), [])
        core.append_record({"id": "a", "name": "ក"})
        core.append_record({"id": "b"})
        self.assertEqual(core.update_record("a", name="new")["name"], "new")
        self.assertIsNone(core.update_record("missing", name="ignored"))
        self.assertEqual([r["id"] for r in core.load_records()], ["a", "b"])
        self.assertTrue(core.delete_record("a"))
        self.assertFalse(core.delete_record("a"))
        core.save_records([])
        self.assertEqual(json.loads(self.path.read_text()), [])

    def test_invalid_file_is_not_silently_overwritten(self):
        self.path.write_text("not json")
        with self.assertRaises(store.StorageError) as raised:
            core.append_record({"id": "new"})
        self.assertEqual(str(raised.exception), "Local record read failed")
        self.assertNotIn(str(self.path), str(raised.exception))
        self.assertEqual(self.path.read_text(), "not json")

    def test_duplicate_and_id_change_rejected(self):
        core.append_record({"id": "a"})
        with self.assertRaises(store.StorageError):
            core.append_record({"id": "a"})
        with self.assertRaises(store.StorageError):
            core.update_record("a", id="b")

    def test_token_backfill_does_not_overwrite_concurrent_fields(self):
        core.save_records([{"id": "a"}])
        stale = core.load_records()
        core.update_record("a", name="concurrent", qr_token="existing")
        self.assertIs(core.ensure_qr_tokens(stale), stale)
        self.assertEqual(stale[0]["qr_token"], "existing")
        self.assertEqual(core.load_records()[0]["name"], "concurrent")
        core.append_record({"id": "b"})
        self.assertTrue(core.ensure_qr_tokens(core.load_records())[1]["qr_token"])

    def test_local_leases_owner_expiry_and_completion(self):
        with patch.object(store.time, "monotonic", return_value=100):
            self.assertEqual(store.acquire_update("bot", 1, "one"), "acquired")
            self.assertEqual(store.acquire_update("bot", 1, "two"), "processing")
            store.complete_update("bot", 1, "two")
            store.release_update("bot", 1, "two")
            self.assertEqual(store.acquire_update("bot", 1, "two"), "processing")
            self.assertEqual(store.acquire_update("other", 1, "two"), "acquired")
        with patch.object(store.time, "monotonic", return_value=191):
            self.assertEqual(store.acquire_update("bot", 1, "two"), "acquired")
            store.complete_update("bot", 1, "one")
            self.assertEqual(store.acquire_update("bot", 1, "three"), "processing")
            store.complete_update("bot", 1, "two")
            self.assertEqual(store.acquire_update("bot", 1, "three"), "completed")
        with patch.object(store.time, "monotonic", return_value=191 + 604801):
            self.assertEqual(store.acquire_update("bot", 1, "three"), "acquired")
            store.release_update("bot", 1, "three")
            self.assertEqual(store.acquire_update("bot", 1, "four"), "acquired")
        for operation in (store.acquire_update, store.complete_update, store.release_update):
            with self.assertRaises(store.StorageError):
                operation("bot", 1, "")

    def test_validate_records_contract(self):
        for invalid in ({}, [None], [{}], [{"id": ""}], [{"id": "   "}],
                        [{"id": "a"}, {"id": "a"}], [{"id": "a", "x": float("nan")}]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(store.StorageError):
                    store.validate_records(invalid)
        self.assertEqual(json.loads(store.validate_records([{"id": "a"}])), [{"id": "a"}])

    def test_validate_configuration_contract(self):
        with patch.dict(os.environ, {}, clear=True):
            store.validate_configuration()
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=True):
            with self.assertRaises(store.StorageError):
                store.validate_configuration()
        for env in ({"UPSTASH_REDIS_REST_URL": "https://example.invalid"},
                    {"UPSTASH_REDIS_REST_TOKEN": "test-only-token"}):
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(store.StorageError):
                    store.validate_configuration()
        for url in ("http://example.invalid", "https://user:pass@example.invalid",
                    "https://example.invalid?query=1", "https://example.invalid#fragment"):
            with patch.dict(os.environ, {
                "UPSTASH_REDIS_REST_URL": url,
                "UPSTASH_REDIS_REST_TOKEN": "test-only-token",
            }, clear=True):
                with self.assertRaises(store.StorageError):
                    store.validate_configuration()
        with patch.dict(os.environ, {
            "UPSTASH_REDIS_REST_URL": "https://example.invalid",
            "UPSTASH_REDIS_REST_TOKEN": "test-only-token",
        }, clear=True):
            store.validate_configuration()

    def test_migration_requires_durable_storage(self):
        with self.assertRaisesRegex(store.StorageError, "requires durable storage"):
            store.import_records([{"id": "a"}])

    def test_vercel_fails_closed_and_partial_configuration_rejected(self):
        for env in ({"VERCEL": "1"}, {"UPSTASH_REDIS_REST_TOKEN": "fake"},
                    {"UPSTASH_REDIS_REST_URL": "https://example.invalid"}):
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(store.StorageError):
                    core.load_records()
                with self.assertRaises(store.StorageError):
                    store.acquire_update("bot", 1, "one")

    def test_migration_dry_run_validates_without_requests(self):
        self.path.write_text('[{"id":"a"}]')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(migrate_records.main([str(self.path)]), 0)
        self.assertIn("Dry run", output.getvalue())
        for invalid in ('{}', '[{}]', '[{"id":"a"},{"id":"a"}]', '[{"id":"a","x":NaN}]'):
            self.path.write_text(invalid)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(migrate_records.main([str(self.path)]), 1)

    def test_contacts_persist_separately_and_normalize_username(self):
        core.save_records([{"id": "a"}])
        store.remember_contact(" @MiXeD ", "123")
        self.assertEqual(store.find_contact("mixed"), "123")
        self.assertEqual(store.find_contact("@MIXED"), "123")
        self.assertIsNone(store.find_contact("unknown"))
        self.assertEqual(json.loads(self.contacts.read_text()), {"mixed": "123"})
        self.assertEqual(core.load_records(), [{"id": "a"}])
        self.assertNotIn("mixed", self.path.read_text())
        store.remember_contact("mixed", 456)
        self.assertEqual(store.find_contact("mixed"), "456")
        for invalid in ("", "@", None):
            with self.assertRaises(store.StorageError):
                store.find_contact(invalid)
        for chat_id in ("", "   ", None, True):
            with self.assertRaises(store.StorageError):
                store.remember_contact("mixed", chat_id)
        self.contacts.write_text("broken")
        with self.assertRaises(store.StorageError) as raised:
            store.remember_contact("mixed", "789")
        self.assertEqual(str(raised.exception), "Local contact read failed")
        self.assertNotIn(str(self.contacts), str(raised.exception))
        self.assertEqual(self.contacts.read_text(), "broken")

    def test_portable_assets(self):
        self.assertTrue(core.TEMPLATE_PATH.is_absolute())
        self.assertTrue(core.TEMPLATE_PATH.is_file())
        self.assertTrue((core.BASE_DIR / "fonts/Arial Bold.ttf").is_file())


class TicketRenderCacheTests(unittest.TestCase):
    """Ticket rendering caches the decoded template and fonts without altering output."""

    def reset_caches(self):
        with core._TEMPLATE_CACHE_LOCK:
            core._TEMPLATE_CACHE = None
        with core._FONT_CACHE_LOCK:
            core._FONT_CACHE.clear()

    def setUp(self):
        self.reset_caches()

    def tearDown(self):
        self.reset_caches()

    def test_template_is_decoded_once_across_renders(self):
        self.reset_caches()
        with patch.object(core.Image, "open", wraps=core.Image.open) as opened:
            first = core.generate_ticket("CACHE-TEST").getvalue()
            second = core.generate_ticket("CACHE-TEST").getvalue()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(first, second)

    def test_fonts_are_cached_per_size(self):
        self.reset_caches()
        with patch.object(core.ImageFont, "truetype",
                          wraps=core.ImageFont.truetype) as loaded:
            first = core.find_font(55)
            second = core.find_font(55)
        self.assertEqual(loaded.call_count, 1)
        self.assertIs(first, second)

    def test_identical_renders_are_byte_identical_jpegs(self):
        self.reset_caches()
        first = core.generate_ticket("CACHE-TEST").getvalue()
        second = core.generate_ticket("CACHE-TEST").getvalue()
        self.assertEqual(first, second)
        self.assertEqual(first[:2], b"\xff\xd8")
        self.assertEqual(first[-2:], b"\xff\xd9")
        with core.Image.open(io.BytesIO(first)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (1920, 1080))

    def test_template_cache_invalidates_when_file_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "template.jpg"
            shutil.copyfile(core.TEMPLATE_PATH, copy)
            with patch.object(core, "TEMPLATE_PATH", copy), \
                    patch.object(core.Image, "open", wraps=core.Image.open) as opened:
                core.generate_ticket("CACHE-TEST")
                core.generate_ticket("CACHE-TEST")
                self.assertEqual(opened.call_count, 1)
                stat = copy.stat()
                os.utime(copy, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
                core.generate_ticket("CACHE-TEST")
                self.assertEqual(opened.call_count, 2)


class RedisContractTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "UPSTASH_REDIS_REST_URL": "https://redis.example.invalid",
            "UPSTASH_REDIS_REST_TOKEN": "test-only-token",
        }, clear=True)
        self.env.start()
        self.response = Mock()
        self.response.json.return_value = {"result": 1}
        self.http = patch.object(store.requests, "post", return_value=self.response)
        self.post = self.http.start()

    def tearDown(self):
        self.http.stop()
        self.env.stop()

    def result(self, value):
        self.response.json.return_value = {"result": value}

    def command(self):
        args, kwargs = self.post.call_args
        self.assertEqual(args, ("https://redis.example.invalid",))
        self.assertEqual(kwargs["timeout"], (3, 10))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-only-token"})
        return kwargs["json"]

    def test_atomic_record_commands(self):
        core.append_record({"id": "a"})
        command = self.command()
        self.assertEqual(command[:3], ["EVAL", store._RECORD_SCRIPT, 3])
        self.assertEqual(command[3:6], [store._INDEX, store._SEQUENCE, store._RECORDS])
        self.assertEqual(command[6], "append")
        self.assertEqual(json.loads(command[7]), {"id": "a"})
        self.assertEqual(self.post.call_count, 1)
        self.result('{"id":"a","name":"new"}')
        self.assertEqual(core.update_record("a", name="new")["name"], "new")
        self.assertEqual(self.command()[6:], ["update", "a", '{"name": "new"}'])
        self.result(None)
        self.assertIsNone(core.update_record("missing", name="new"))
        self.result('[{"id":"a"}]')
        self.assertEqual(core.load_records(), [{"id": "a"}])
        self.assertEqual(self.command()[6:], ["load"])
        self.result(1)
        self.assertTrue(core.delete_record("a"))
        self.assertEqual(self.command()[6:], ["delete", "a"])
        core.save_records([])
        self.assertEqual(self.command()[6:], ["save", "[]"])

    def test_redis_contacts_contract(self):
        key = store._PREFIX + "contacts"
        store.remember_contact("@MiXeD", "123")
        self.assertEqual(self.command(), ["HSET", key, "mixed", "123"])
        self.result("123")
        self.assertEqual(store.find_contact("@MIXED"), "123")
        self.assertEqual(self.command(), ["HGET", key, "mixed"])
        self.result(None)
        self.assertIsNone(store.find_contact("unknown"))
        self.result({"unexpected": "shape"})
        with self.assertRaises(store.StorageError):
            store.find_contact("mixed")
        self.post.side_effect = requests.Timeout("sensitive")
        with self.assertRaisesRegex(store.StorageError, "^Durable storage operation failed$"):
            store.remember_contact("mixed", "123")

    def test_redis_lease_contract(self):
        self.result("acquired")
        self.assertEqual(store.acquire_update("bot", 7, "owner"), "acquired")
        command = self.command()
        self.assertEqual(command[:3], ["EVAL", store._LEASE_SCRIPT, 2])
        self.assertEqual(command[4], command[3] + ":done")
        self.assertEqual(command[5:], ["acquire", "owner"])
        self.assertIn("'EX', 90", command[1])
        self.assertIn("'EX', 604800", command[1])
        self.assertIn("~= owner", command[1])
        self.result(1)
        store.complete_update("bot", 7, "owner")
        self.assertEqual(self.command()[5:], ["complete", "owner"])
        store.release_update("bot", 7, "owner")
        self.assertEqual(self.command()[5:], ["release", "owner"])

    def test_sanitized_errors_and_no_fallback(self):
        for body in ({"error": "sensitive-remote-detail"}, {}, [], {"result": "invalid json"}):
            self.response.json.return_value = body
            with self.assertRaises(store.StorageError) as raised:
                core.load_records()
            self.assertNotIn("sensitive", str(raised.exception))
        self.post.side_effect = requests.Timeout("https://secret/token")
        with self.assertRaises(store.StorageError) as raised:
            core.append_record({"id": "a"})
        self.assertEqual(str(raised.exception), "Durable storage operation failed")

    def test_command_failures_never_leak_url_or_token(self):
        token = os.environ["UPSTASH_REDIS_REST_TOKEN"]
        secret = os.environ["UPSTASH_REDIS_REST_URL"] + "/?token=" + token
        self.post.side_effect = requests.RequestException(secret)
        with self.assertRaises(store.StorageError) as raised:
            store.find_contact("user")
        self.assertEqual(str(raised.exception), "Durable storage operation failed")
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn("example.invalid", str(raised.exception))
        for failure in ("http", "json"):
            with self.subTest(failure=failure):
                self.post.side_effect = None
                response = Mock()
                if failure == "http":
                    response.raise_for_status.side_effect = requests.HTTPError(secret)
                else:
                    response.json.side_effect = ValueError(secret)
                self.post.return_value = response
                with self.assertRaises(store.StorageError) as raised:
                    store.remember_contact("user", "123")
                self.assertEqual(str(raised.exception), "Durable storage operation failed")
                self.assertNotIn(token, str(raised.exception))
                self.assertNotIn("example.invalid", str(raised.exception))

    def test_migration_atomic_empty_guard(self):
        store.import_records([{"id": "a"}])
        self.assertEqual(self.command()[6], "import")
        self.assertIn("redis.call('EXISTS', index)", store._RECORD_SCRIPT)
        self.assertIn("redis.call('SCAN'", store._RECORD_SCRIPT)
        self.result(0)
        with self.assertRaisesRegex(store.StorageError, "not empty"):
            store.import_records([{"id": "a"}])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "records.json"
            source.write_text('[{"id":"a"}]')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(migrate_records.main([str(source), "--apply"]), 1)
            self.result(1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(migrate_records.main([str(source), "--apply"]), 0)
            self.assertEqual(source.read_text(), '[{"id":"a"}]')


if __name__ == "__main__":
    unittest.main()

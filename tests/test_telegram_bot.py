import contextlib
import io
import traceback
import unittest
from unittest.mock import Mock, patch

import requests

import telegram_bot as bot


class TelegramClientTests(unittest.TestCase):
    def setUp(self):
        self.token = "test-token-not-a-credential"
        self.token_patch = patch.object(bot, "BOT_TOKEN", self.token)
        self.dry_patch = patch.object(bot, "DRY_RUN", False)
        self.http_patch = patch.object(bot.requests, "post")
        self.get_patch = patch.object(bot.requests, "get")
        self.token_patch.start()
        self.dry_patch.start()
        self.post = self.http_patch.start()
        self.get = self.get_patch.start()
        self.addCleanup(patch.stopall)
        self.post.return_value = Mock()
        self.post.return_value.json.return_value = {"ok": True, "result": True}

    def test_set_webhook_posts_native_list_and_boolean_without_dropping(self):
        self.assertTrue(bot.set_webhook("https://example.invalid/telegram/webhook", "test-secret"))
        args, kwargs = self.post.call_args
        self.assertTrue(args[0].endswith("/setWebhook"))
        self.assertEqual(kwargs["json"], {
            "url": "https://example.invalid/telegram/webhook",
            "secret_token": "test-secret", "allowed_updates": ["message"],
            "drop_pending_updates": False,
        })
        self.assertEqual(kwargs["timeout"], (5, 15))
        self.get.assert_not_called()

    def test_delete_webhook_preserves_pending_updates(self):
        bot.delete_webhook()
        self.assertIs(self.post.call_args.kwargs["json"]["drop_pending_updates"], False)

    def test_control_and_message_calls_use_json(self):
        self.post.return_value.json.return_value = {"ok": True, "result": {"url": ""}}
        self.assertEqual(bot.get_webhook_info(), {"url": ""})
        bot.send_message(123, "Hello")
        self.assertEqual(self.post.call_args.kwargs["json"], {
            "chat_id": "123", "text": "Hello", "parse_mode": "HTML",
        })

    def test_polling_timeout_allows_telegram_wait(self):
        self.post.return_value.json.return_value = {"ok": True, "result": []}
        self.assertEqual(bot._api("getUpdates", timeout=25, offset=123), [])
        self.assertEqual(self.post.call_args.kwargs["timeout"], (5, 30))
        self.assertEqual(self.post.call_args.kwargs["json"], {"timeout": 25, "offset": 123})
        bot._api("getUpdates", timeout=50)
        self.assertEqual(self.post.call_args.kwargs["timeout"], (5, 55))

    def test_invalid_polling_timeout_is_sanitized_without_http(self):
        for value in (self.token, -1, 51, float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(bot.TelegramError) as error:
                    bot._api("getUpdates", timeout=value)
                self.assertNotIn(self.token, str(error.exception))
        self.post.assert_not_called()

    def test_photo_is_one_multipart_attempt(self):
        bot.send_photo(123, b"jpeg", filename="ticket.jpg", caption="Ticket")
        self.post.assert_called_once()
        kwargs = self.post.call_args.kwargs
        self.assertEqual(kwargs["timeout"], (3, 8))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["files"], {"photo": ("ticket.jpg", b"jpeg", "image/jpeg")})
        self.assertEqual(kwargs["data"], {"chat_id": "123", "caption": "Ticket"})

    def test_errors_are_sanitized_for_all_request_types(self):
        sensitive = f"https://api.telegram.org/bot{self.token}/sendPhoto"
        calls = [lambda: bot.get_me(), lambda: bot.send_message(1, "Hello"),
                 lambda: bot.send_photo(1, b"jpeg")]
        failures = ["network", "http", "json", "api", "shape", "missing", "bad_ok"]
        for call in calls:
            for failure in failures:
                with self.subTest(call=call, failure=failure):
                    self.post.reset_mock()
                    self.post.side_effect = None
                    response = Mock()
                    response.json.return_value = {"ok": True, "result": True}
                    self.post.return_value = response
                    if failure == "network":
                        self.post.side_effect = requests.Timeout(sensitive)
                    elif failure == "http":
                        response.raise_for_status.side_effect = requests.HTTPError(sensitive)
                    elif failure == "json":
                        response.json.side_effect = ValueError(sensitive)
                    elif failure == "api":
                        response.json.return_value = {"ok": False, "description": sensitive}
                    elif failure == "shape":
                        response.json.return_value = [sensitive]
                    elif failure == "missing":
                        response.json.return_value = {"ok": True}
                    else:
                        response.json.return_value = {"ok": sensitive, "result": True}
                    output = io.StringIO()
                    with patch.object(bot.time, "sleep") as sleep, contextlib.redirect_stdout(output):
                        try:
                            call()
                        except bot.TelegramError:
                            formatted = traceback.format_exc()
                        else:
                            self.fail("Expected TelegramError")
                        sleep.assert_not_called()
                    self.post.assert_called_once()
                    self.assertNotIn(self.token, formatted)
                    self.assertNotIn(sensitive, formatted)
                    self.assertEqual(output.getvalue(), "")

    def test_missing_token_does_not_make_request(self):
        with patch.object(bot, "BOT_TOKEN", ""):
            with self.assertRaises(bot.TelegramError):
                bot.get_me()
            with self.assertRaises(bot.TelegramError):
                bot.send_photo(1, b"jpeg")
        self.post.assert_not_called()

    def test_dry_run_skips_network_even_without_token(self):
        with patch.object(bot, "DRY_RUN", True), patch.object(bot, "BOT_TOKEN", ""):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(bot.send_message(1, "Hello")["dry_run"])
                self.assertTrue(bot.send_photo(1, b"jpeg")["dry_run"])
        self.post.assert_not_called()


class TicketBackupTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(bot.os.environ, {}, clear=True)
        self.env.start()
        patch.object(bot, "DRY_RUN", False).start()
        self.find = patch.object(bot.record_store, "find_contact", side_effect=["101", "202"]).start()
        self.render = patch.object(bot, "generate_ticket", return_value=io.BytesIO(b"jpeg")).start()
        self.send = patch.object(bot, "send_photo", return_value={"message_id": 1}).start()
        self.update = patch.object(bot, "update_record").start()
        self.addCleanup(patch.stopall)
        self.record = {
            "promo_code": "CODE", "customer_name": "<b>Customer</b>",
            "phone": "+855123", "discount": 10, "created_at": "2026-11-11T10:00:00",
            "revenue": "MUST NOT SEND", "telegram_chat_id": "customer",
        }

    def test_default_admins_receive_only_ticket_details_without_metadata_changes(self):
        before = dict(self.record)
        self.assertEqual(bot.send_ticket_backups(self.record), [
            {"username": "President_Alien", "status": "sent"},
            {"username": "SassdahRAK", "status": "sent"},
        ])
        self.assertEqual([call.args for call in self.find.call_args_list],
                         [("President_Alien",), ("SassdahRAK",)])
        self.assertEqual([call.args[0] for call in self.send.call_args_list], ["101", "202"])
        self.render.assert_called_once_with("CODE", "<b>Customer</b>")
        caption = self.send.call_args.kwargs["caption"]
        for value in ("CODE", "<b>Customer</b>", "+855123", "10", "2026-11-11T10:00:00"):
            self.assertIn(value, caption)
        self.assertNotIn("MUST NOT SEND", caption)
        self.assertNotIn("parse_mode", self.send.call_args.kwargs)
        self.update.assert_not_called()
        self.assertEqual(self.record, before)

    def test_blank_configuration_disables_backups(self):
        bot.os.environ["TELEGRAM_BACKUP_USERNAMES"] = "  "
        self.assertEqual(bot.send_ticket_backups(self.record), [])
        self.find.assert_not_called()
        self.render.assert_not_called()
        self.send.assert_not_called()

    def test_configuration_deduplicates_casefolded_usernames(self):
        bot.os.environ["TELEGRAM_BACKUP_USERNAMES"] = "@Admin,admin, ,Other"
        self.assertEqual([r["username"] for r in bot.send_ticket_backups(self.record)],
                         ["Admin", "Other"])

    def test_unknown_contact_is_unavailable_not_a_username_send(self):
        self.find.side_effect = [None, "202"]
        results = bot.send_ticket_backups(self.record)
        self.assertEqual([r["status"] for r in results], ["unavailable", "sent"])
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], "202")

    def test_storage_and_send_failures_are_isolated_and_sanitized(self):
        for storage_failure in (True, False):
            with self.subTest(storage_failure=storage_failure):
                self.find.side_effect = [RuntimeError("secret-token-url"), "202"] if storage_failure else ["101", "202"]
                self.send.side_effect = None if storage_failure else [RuntimeError("secret-token-url"), {"message_id": 1}]
                results = bot.send_ticket_backups(self.record)
                self.assertEqual([r["status"] for r in results], ["failed", "sent"])
                self.assertNotIn("secret-token-url", str(results))

    def test_deadline_prevents_starting_another_upload(self):
        with patch.object(bot.time, "monotonic", side_effect=[0, 0, 1, 25]):
            results = bot.send_ticket_backups(self.record)
        self.assertEqual([r["status"] for r in results], ["sent", "unavailable"])
        self.find.assert_called_once_with("President_Alien")
        self.assertEqual(results[1]["error"], "Backup time budget exhausted")
        self.send.assert_called_once()

    def test_shared_deadline_skips_all_network_when_less_than_24_seconds_remain(self):
        with patch.object(bot.time, "monotonic", return_value=27):
            results = bot.send_ticket_backups(self.record, deadline=50)
        self.assertTrue(all(r["status"] == "unavailable" for r in results))
        self.assertTrue(all("budget" in r["error"] for r in results))
        self.find.assert_not_called()
        self.render.assert_not_called()
        self.send.assert_not_called()

    def test_shared_deadline_allows_both_admins_on_fast_path(self):
        with patch.object(bot.time, "monotonic", side_effect=[10, 10, 11, 12, 13]):
            results = bot.send_ticket_backups(self.record, deadline=50)
        self.assertEqual([r["status"] for r in results], ["sent", "sent"])
        self.assertEqual(self.send.call_count, 2)

    def test_shared_deadline_is_not_reset_for_second_recipient(self):
        with patch.object(bot.time, "monotonic", side_effect=[10, 10, 23, 27]):
            results = bot.send_ticket_backups(self.record, deadline=50)
        self.assertEqual([r["status"] for r in results], ["sent", "unavailable"])
        self.find.assert_called_once()
        self.send.assert_called_once()

    def test_local_48_second_cap_applies_even_with_later_shared_deadline(self):
        with patch.object(bot.time, "monotonic", side_effect=[0, 25, 25]):
            results = bot.send_ticket_backups(self.record, deadline=100)
        self.assertTrue(all(r["status"] == "unavailable" for r in results))
        self.find.assert_not_called()
        self.send.assert_not_called()

    def test_elapsed_lookup_and_render_time_prevents_late_upload(self):
        with patch.object(bot.time, "monotonic", side_effect=[0, 0, 40, 40]):
            results = bot.send_ticket_backups(self.record, deadline=50)
        self.assertTrue(all(r["status"] == "unavailable" for r in results))
        self.find.assert_called_once()
        self.send.assert_not_called()

    def test_caption_is_bounded_even_with_astral_unicode(self):
        self.record = {key: "\U0001f600" * 2000 for key in self.record}
        bot.send_ticket_backups(self.record)
        caption = self.send.call_args.kwargs["caption"]
        self.assertLessEqual(len(caption.encode("utf-16-le")) // 2, 1024)

    def test_dry_run_never_claims_delivery_or_resolves_contacts(self):
        with patch.object(bot, "DRY_RUN", True):
            results = bot.send_ticket_backups(self.record)
        self.assertTrue(all(r["status"] == "unavailable" for r in results))
        self.find.assert_not_called()
        self.send.assert_not_called()


class TicketDeliveryHardeningTests(unittest.TestCase):
    def _send_customer_ticket(self, record):
        with patch.object(bot, "generate_ticket", return_value=io.BytesIO(b"jpeg")), \
                patch.object(bot, "send_photo", return_value={"message_id": 1}) as send:
            self.assertTrue(bot.send_ticket_to_chat(123, record))
        return send.call_args

    def test_customer_caption_uses_html_and_escapes_user_values(self):
        record = {
            "id": "r1", "promo_code": '<b>"CODE"</b>',
            "customer_name": "<script>alert(1)</script>", "discount": 10,
        }
        args = self._send_customer_ticket(record)
        self.assertEqual(args.kwargs["parse_mode"], "HTML")
        caption = args.kwargs["caption"]
        self.assertIn("&lt;b&gt;", caption)
        self.assertIn("&lt;script&gt;", caption)
        self.assertNotIn("<script>", caption)
        self.assertNotIn('<b>"CODE"</b>', caption)
        # The static formatting tags themselves survive escaping.
        self.assertIn("<b>Your E-Ticket</b>", caption)

    def test_customer_photo_sends_parse_mode_in_multipart_only_when_requested(self):
        token_patch = patch.object(bot, "BOT_TOKEN", "test-token-not-a-credential")
        dry_patch = patch.object(bot, "DRY_RUN", False)
        post_patch = patch.object(bot.requests, "post")
        token_patch.start()
        dry_patch.start()
        post = post_patch.start()
        self.addCleanup(patch.stopall)
        response = Mock()
        response.json.return_value = {"ok": True, "result": True}
        post.return_value = response

        bot.send_photo(1, b"jpeg", caption="c", parse_mode="HTML")
        self.assertEqual(post.call_args.kwargs["data"],
                         {"chat_id": "1", "caption": "c", "parse_mode": "HTML"})

        post.reset_mock()
        bot.send_photo(1, b"jpeg", caption="c")
        self.assertNotIn("parse_mode", post.call_args.kwargs["data"])

    def test_backup_photo_still_has_no_parse_mode(self):
        with patch.dict(bot.os.environ, {"TELEGRAM_BACKUP_USERNAMES": "Admin"}), \
                patch.object(bot, "DRY_RUN", False), \
                patch.object(bot.record_store, "find_contact", return_value="42"), \
                patch.object(bot, "generate_ticket", return_value=io.BytesIO(b"jpeg")), \
                patch.object(bot, "send_photo", return_value={"message_id": 1}) as send:
            results = bot.send_ticket_backups(
                {"promo_code": "C", "customer_name": "N", "discount": 5})
        self.assertEqual(results[0]["status"], "sent")
        self.assertNotIn("parse_mode", send.call_args.kwargs)

    def test_non_numeric_discount_never_raises(self):
        for value in ("fifty", object()):
            with self.subTest(value=value):
                caption = bot.ticket_caption(
                    {"promo_code": "C", "customer_name": "N", "discount": value})
                self.assertIn("Discount:", caption)
        # None is treated as "no discount", but must not raise either.
        self.assertNotIn("Discount:", bot.ticket_caption(
            {"promo_code": "C", "discount": None}))
        self.assertEqual(bot._discount_text(None), "None")
        self.assertNotIn("<b>", bot._discount_text("<b>x</b>"))
        self.assertEqual(bot._discount_text(10), "10")
        self.assertEqual(bot._discount_text("10.5"), "10.5")
        self.assertEqual(bot._discount_text("inf"), "inf")

    def test_filename_is_sanitized_against_multipart_injection(self):
        self.assertEqual(bot._safe_filename(None), "ticket")
        self.assertEqual(bot._safe_filename("..."), "ticket")
        self.assertEqual(bot._safe_filename("a/b\r\nc"), "a_b_c")
        self.assertLessEqual(len(bot._safe_filename("x" * 500)), 64)

        record = {"id": "r1", "promo_code": 'a"\r\nInjected: yes',
                  "customer_name": "N"}
        filename = self._send_customer_ticket(record).kwargs["filename"]
        self.assertTrue(filename.startswith("ticket_"))
        self.assertTrue(filename.endswith(".jpg"))
        self.assertNotIn('"', filename)
        self.assertNotIn("\r", filename)
        self.assertNotIn("\n", filename)
        self.assertIn(bot._safe_filename(record["promo_code"]), filename)

    def test_remember_chat_swallows_storage_errors_only(self):
        output = io.StringIO()
        with patch.object(bot, "DRY_RUN", False), \
                patch.object(bot, "update_record",
                             side_effect=bot.record_store.StorageError("secret-url")), \
                contextlib.redirect_stdout(output):
            bot._remember_chat({"id": "r1"}, 5, "name", "user")
        self.assertNotIn("secret-url", output.getvalue())

        with patch.object(bot, "DRY_RUN", False), \
                patch.object(bot, "update_record", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                bot._remember_chat({"id": "r1"}, 5, "name", "user")

    def test_group_chat_never_looks_up_or_sends_a_ticket(self):
        update = {"message": {"text": "0123456789",
                              "chat": {"id": -100123, "type": "group", "title": "G"}}}
        with patch.object(bot, "send_ticket_to_chat") as send_ticket, \
                patch.object(bot, "load_records") as load, \
                patch.object(bot, "send_message") as message:
            log = bot.handle_update(update)
        self.assertIn("non-private", log)
        send_ticket.assert_not_called()
        load.assert_not_called()
        message.assert_called_once()

    def test_edited_message_is_ignored(self):
        update = {"edited_message": {"text": "CODE",
                                     "chat": {"id": 1, "type": "private"}}}
        with patch.object(bot, "send_ticket_to_chat") as send_ticket, \
                patch.object(bot, "send_message") as message:
            self.assertEqual(bot.handle_update(update), "ignored: no message")
        send_ticket.assert_not_called()
        message.assert_not_called()

    def test_missing_or_bool_chat_id_is_ignored(self):
        for chat in ({}, {"id": True}, {"id": "123"}):
            with self.subTest(chat=chat):
                with patch.object(bot, "send_message") as message:
                    self.assertEqual(bot.handle_update(
                        {"message": {"text": "x", "chat": chat}}),
                        "ignored: no chat id")
                message.assert_not_called()

    def test_promo_code_branch_shares_the_lookup_throttle(self):
        now = bot.time.time()
        update = {"message": {"text": "CODE123",
                              "chat": {"id": 77, "type": "private"}}}
        with patch.dict(bot.os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": ""}), \
                patch.object(bot, "_lookups", {77: [now] * bot.LOOKUP_LIMIT}), \
                patch.object(bot, "load_records") as load, \
                patch.object(bot, "send_ticket_to_chat") as send_ticket, \
                patch.object(bot, "send_message") as message:
            log = bot.handle_update(update)
        self.assertTrue(log.startswith("rate limited"))
        load.assert_not_called()
        send_ticket.assert_not_called()
        message.assert_called_once()

    def test_throttle_prunes_stale_chats_when_table_is_large(self):
        stale = {i: [0.0] for i in range(bot.MAX_LOOKUP_CHATS + 1)}
        with patch.object(bot, "_lookups", stale), \
                patch.object(bot.time, "time", return_value=1_000_000.0):
            self.assertFalse(bot._too_many_lookups(99999))
            self.assertEqual(len(bot._lookups), 1)
            self.assertIn(99999, bot._lookups)

    def test_throttle_hard_caps_unique_chat_growth(self):
        active = {i: [1_000_000.0] for i in range(bot.MAX_LOOKUP_CHATS)}
        with patch.object(bot, "_lookups", active), \
                patch.object(bot.time, "time", return_value=1_000_000.5):
            bot._too_many_lookups(50000)
            self.assertLessEqual(len(bot._lookups), bot.MAX_LOOKUP_CHATS)
            self.assertIn(50000, bot._lookups)


class StartTokenThrottleTests(unittest.TestCase):
    def setUp(self):
        bot._lookups.clear()
        self.addCleanup(bot._lookups.clear)
        patch.dict(bot.os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": ""}).start()
        self.find = patch.object(bot, "find_record_by_token").start()
        self.message = patch.object(bot, "send_message").start()
        self.send = patch.object(bot, "send_ticket_to_chat").start()
        patch.object(bot, "_remember_chat").start()
        self.addCleanup(patch.stopall)

    def _update(self, text, chat_id=77):
        return {"message": {"text": text, "chat": {
            "id": chat_id, "type": "private", "first_name": "V"}}}

    def _exhaust(self, chat_id=77):
        bot._lookups[chat_id] = [bot.time.time()] * bot.LOOKUP_LIMIT

    def test_throttled_start_token_never_scans_records(self):
        self._exhaust()
        log = bot.handle_update(self._update("/start bogus-token"))
        self.assertEqual(log, "rate limited 77")
        self.find.assert_not_called()
        self.send.assert_not_called()
        self.message.assert_called_once_with(77, bot.RATE_LIMITED)

    def test_bare_start_and_help_are_never_rate_limited(self):
        self._exhaust()
        self.assertEqual(bot.handle_update(self._update("/start")), "/start from 77")
        self.message.assert_called_once_with(77, bot.WELCOME)

        self.message.reset_mock()
        self.assertEqual(bot.handle_update(self._update("/help")), "/help from 77")
        self.message.assert_called_once_with(77, bot.HELP)
        self.find.assert_not_called()

    def test_valid_start_token_still_sends_ticket_under_budget(self):
        record = {"id": "r1", "promo_code": "CODE", "customer_name": "N"}
        self.find.return_value = record
        log = bot.handle_update(self._update("/start good-token"))
        self.find.assert_called_once_with("good-token")
        self.send.assert_called_once_with(77, record)
        self.assertEqual(log, "sent ticket CODE to 77")

    def test_start_token_consumes_the_shared_budget(self):
        self.find.return_value = None
        for _ in range(bot.LOOKUP_LIMIT):
            bot.handle_update(self._update("/start bogus-token"))
        self.assertEqual(self.find.call_count, bot.LOOKUP_LIMIT)

        self.find.reset_mock()
        self.message.reset_mock()
        log = bot.handle_update(self._update("/start bogus-token"))
        self.assertEqual(log, "rate limited 77")
        self.find.assert_not_called()
        self.message.assert_called_once_with(77, bot.RATE_LIMITED)


if __name__ == "__main__":
    unittest.main()

"""Offline web integration tests; no live Telegram, Redis, or local record writes."""
import io
import os
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-only", "SECRET_KEY": "test-session"}):
    import app as web
import telegram_bot


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "test-secret"})
        self.env.start()
        self.token = patch.object(telegram_bot, "BOT_TOKEN", "123:dummy")
        self.token.start()
        self.client = web.app.test_client()
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
        self.update = {"update_id": 12, "message": {"chat": {"id": 42, "type": "private", "username": "untrusted"}, "from": {"id": 42, "username": "Trusted"}, "text": "/start"}}

    def tearDown(self):
        self.env.stop()
        self.token.stop()

    def post(self, update=None):
        return self.client.post("/telegram/webhook", json=self.update if update is None else update, headers=self.headers)

    def test_fail_closed_auth_and_invalid_body(self):
        self.assertEqual(self.client.post("/telegram/webhook", json={}).status_code, 403)
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": ""}):
            self.assertEqual(self.post().status_code, 503)
        for body in ([], {}, {"update_id": True}, {"update_id": -1}, {"update_id": 1, "message": []}, {"update_id": 1, "message": {"chat": {"id": 1}, "text": 7}}):
            self.assertEqual(self.post(body).status_code, 400)

    def test_owner_lease_and_authenticated_private_contact(self):
        with patch.object(web.record_store, "acquire_update", return_value="acquired") as acquire, patch.object(web.record_store, "complete_update") as complete, patch.object(web.record_store, "remember_contact") as remember, patch.object(telegram_bot, "handle_update") as handle:
            self.assertEqual(self.post().status_code, 200)
            remember.assert_called_once_with("Trusted", "42")
            complete.assert_called_once_with(*acquire.call_args.args)
            handle.assert_called_once()

    def test_untrusted_and_group_contacts_not_remembered(self):
        for chat_type, sender_id in (("group", 42), ("private", 43)):
            self.update["message"]["chat"]["type"] = chat_type
            self.update["message"]["from"]["id"] = sender_id
            with patch.object(web.record_store, "acquire_update", return_value="acquired"), patch.object(web.record_store, "complete_update"), patch.object(web.record_store, "remember_contact") as remember, patch.object(telegram_bot, "handle_update"):
                self.assertEqual(self.post().status_code, 200)
                remember.assert_not_called()

    def test_duplicate_and_concurrent_retry(self):
        for state, expected in (("completed", 200), ("processing", 503)):
            with patch.object(web.record_store, "acquire_update", return_value=state), patch.object(telegram_bot, "handle_update") as handle:
                self.assertEqual(self.post().status_code, expected)
                handle.assert_not_called()

    def test_failure_releases_owned_lease(self):
        with patch.object(web.record_store, "acquire_update", return_value="acquired") as acquire, patch.object(web.record_store, "release_update") as release, patch.object(web.record_store, "complete_update") as complete, patch.object(telegram_bot, "handle_update", side_effect=RuntimeError("sensitive")):
            response = self.post()
            self.assertGreaterEqual(response.status_code, 500)
            self.assertNotIn(b"sensitive", response.data)
            release.assert_called_once_with(*acquire.call_args.args)
            complete.assert_not_called()

    def test_contact_memory_failure_keeps_delivered_update_200(self):
        with patch.object(web.record_store, "acquire_update", return_value="acquired") as acquire, patch.object(web.record_store, "complete_update") as complete, patch.object(web.record_store, "remember_contact", side_effect=web.StorageError("secret-token")), patch.object(telegram_bot, "handle_update") as handle:
            response = self.post()
            self.assertEqual(response.status_code, 200)
            handle.assert_called_once()
            complete.assert_called_once_with(*acquire.call_args.args)
            self.assertNotIn(b"secret-token", response.data)

    def test_completion_failure_keeps_delivered_update_200(self):
        with patch.object(web.record_store, "acquire_update", return_value="acquired"), patch.object(web.record_store, "complete_update", side_effect=web.StorageError("secret-token")), patch.object(web.record_store, "remember_contact"), patch.object(telegram_bot, "handle_update"):
            response = self.post()
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b"secret-token", response.data)

    def test_admin_ticket_and_qr_errors_are_sanitized(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
        record = {"id": "rec-1", "promo_code": "TEST", "customer_name": "Name", "qr_token": "token"}
        with patch.object(web, "load_records", return_value=[record]), patch.object(web, "generate_ticket", side_effect=RuntimeError("secret-token")):
            ticket = self.client.get("/admin/ticket/rec-1")
            self.assertEqual(ticket.status_code, 500)
            self.assertNotIn(b"secret-token", ticket.data)
        with patch.object(web, "load_records", return_value=[record]), patch.object(telegram_bot, "qr_png", side_effect=RuntimeError("secret-token")):
            qr = self.client.get("/admin/qr/rec-1.png")
            self.assertEqual(qr.status_code, 500)
            self.assertNotIn(b"secret-token", qr.data)

    def test_storage_failure_sanitized(self):
        with patch.object(web.record_store, "acquire_update", side_effect=web.StorageError("private-url")):
            response = self.post()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b"private-url", response.data)

    def test_admin_auth_and_atomic_delete(self):
        self.assertEqual(self.client.post("/admin/delete/one").status_code, 302)
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
        with patch.object(web, "delete_record") as delete:
            self.assertEqual(self.client.post("/admin/delete/one").status_code, 302)
            delete.assert_called_once_with("one")

    def test_public_creation_delivery_and_backup_failure_preserves_record(self):
        with patch.object(web, "generate_ticket", return_value=io.BytesIO(b"test")), patch.object(web, "append_record") as append, patch.object(web.record_store, "find_contact", return_value="42"), patch.object(telegram_bot, "send_ticket_to_chat", side_effect=telegram_bot.TelegramError("private")) as send, patch.object(telegram_bot, "send_ticket_backups", side_effect=RuntimeError("private"), create=True) as backups, patch.object(telegram_bot, "BOT_USERNAME", "TestBot"), patch.object(telegram_bot, "DRY_RUN", False):
            response = self.client.post("/generate", data={"promo_code": "TEST", "discount": "20", "telegram_username": "@Trusted"})
            self.assertEqual(response.status_code, 200)
            append.assert_called_once()
            send.assert_called_once()
            backups.assert_called_once()
            for internal in (b"Ticket saved", b"Best-effort admin backups", b"not delivered", b"admin backups failed", b"SassdahRAK", b"President_Alien"):
                self.assertNotIn(internal, response.data)
            self.assertNotIn(b"private", response.data.split(b"<body>", 1)[1].split(b"<form", 1)[0])

    def test_admin_creation_calls_backups_and_known_customer(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
        with patch.object(web, "load_records", return_value=[]), patch.object(web, "append_record") as append, patch.object(web, "generate_ticket", return_value=io.BytesIO(b"test")), patch.object(web.record_store, "find_contact", return_value="42"), patch.object(telegram_bot, "send_ticket_to_chat", return_value=True) as send, patch.object(web, "update_record"), patch.object(telegram_bot, "send_ticket_backups", return_value=[{"username": "President_Alien", "status": "sent"}, {"username": "SassdahRAK", "status": "unavailable"}]) as backups, patch.object(telegram_bot, "DRY_RUN", False):
            response = self.client.post("/admin/create", data={"promo_code": "TEST", "discount": "20", "telegram_username": "@Trusted"})
            self.assertEqual(response.status_code, 200)
            record = append.call_args.args[0]
            send.assert_called_once_with("42", record)
            backups.assert_called_once()
            self.assertEqual(backups.call_args.args, (record,))
            self.assertIsInstance(backups.call_args.kwargs["deadline"], float)
            self.assertIn(b"Best-effort admin backups", response.data)
            self.assertIn(b"SassdahRAK", response.data)

    def test_shared_deadline_slow_storage_and_uploads_both_routes(self):
        # Exercise the real backup helper against the same virtual clock as the
        # route. No actual sleep, service requests, or record writes occur.
        for route, username in (("/generate", "@Trusted"), ("/admin/create", "@Trusted"), ("/generate", ""), ("/admin/create", "")):
            with self.subTest(route=route, username=username):
                now = [0.0]
                calls = []

                def slow(name, seconds, result):
                    def run(*args, **kwargs):
                        calls.append((name, now[0]))
                        now[0] += seconds
                        return result
                    return run

                with self.client.session_transaction() as session:
                    session["admin_logged_in"] = True
                with patch.object(web.time, "monotonic", side_effect=lambda: now[0]), patch.object(web, "load_records", side_effect=slow("load", 12, [])), patch.object(web, "append_record", side_effect=slow("append", 12, None)) as append, patch.object(web, "ensure_qr_tokens") as backfill, patch.object(web, "generate_ticket", return_value=io.BytesIO(b"test")), patch.object(web.record_store, "find_contact", side_effect=slow("contact", 12, "42")), patch.object(telegram_bot, "send_ticket_to_chat", side_effect=slow("customer-upload", 10, True)), patch.object(telegram_bot, "send_photo", side_effect=slow("backup-upload", 10, {})), patch.object(web, "update_record", side_effect=slow("metadata", 12, {})), patch.object(telegram_bot, "DRY_RUN", False), patch.dict(os.environ, {"TELEGRAM_BACKUP_USERNAMES": "President_Alien,SassdahRAK"}):
                    response = self.client.post(route, data={"promo_code": "TEST", "discount": "20", "telegram_username": username})
                    self.assertEqual(response.status_code, 200)
                    append.assert_called_once()
                    backfill.assert_not_called()
                    self.assertLess(now[0], 60, calls)
                    self.assertLessEqual(now[0], 50, calls)
                    self.assertTrue(all(start < 50 for _, start in calls), calls)
                    if route == "/admin/create":
                        self.assertEqual(sum(name == "load" for name, _ in calls), 1)
                    if username and route == "/generate":
                        self.assertEqual(now[0], 46)
                        self.assertEqual([name for name, _ in calls], ["append", "contact", "customer-upload", "metadata"])
                    if username and route == "/admin/create":
                        self.assertIn(b"Customer delivery skipped", response.data)
                        self.assertNotIn("customer-upload", [name for name, _ in calls])
                    if route == "/admin/create":
                        self.assertIn(b"time budget", response.data)
                    else:
                        self.assertNotIn(b"time budget", response.data)
                        self.assertNotIn(b"Ticket saved", response.data)

    def test_expired_budget_makes_no_best_effort_calls(self):
        with patch.object(web.time, "monotonic", return_value=51), patch.object(web.record_store, "find_contact") as contact, patch.object(telegram_bot, "send_ticket_to_chat") as send, patch.object(web, "update_record") as metadata, patch.object(telegram_bot, "send_ticket_backups") as backups:
            feedback = web.creation_feedback({"id": "1", "telegram_username": "@Known"}, deadline=50)
            self.assertIn("time budget", feedback["delivery_status"])
            self.assertIn("time budget", feedback["backup_error"])
            for operation in (contact, send, metadata, backups):
                operation.assert_not_called()

    def test_public_discount_validation_precedes_side_effects(self):
        with patch.object(web, "append_record") as append:
            for discount in ("nan", "inf", "101", "-1", "invalid"):
                response = self.client.post("/generate", data={"promo_code": "TEST", "discount": discount})
                self.assertIn(b"Discount must be", response.data)
            append.assert_not_called()

    def test_unknown_contact_never_sent_directly(self):
        with patch.object(web.record_store, "find_contact", return_value=None), patch.object(telegram_bot, "send_ticket_to_chat") as send, patch.object(telegram_bot, "BOT_USERNAME", "TestBot"):
            feedback = web.delivery_feedback({"id": "1", "telegram_username": "@Unknown", "qr_token": "test"})
            self.assertFalse(feedback["delivery_ok"])
            self.assertIn("https://t.me/", feedback["start_link"])
            send.assert_not_called()

    def test_production_requires_stable_credentials(self):
        with patch.object(web, "PRODUCTION", True), patch.dict(os.environ, {"ADMIN_PASSWORD": "", "SECRET_KEY": ""}):
            with self.assertRaises(RuntimeError):
                web.initialize()

    def test_theme_assets_and_all_templates(self):
        from pathlib import Path
        for name in ("theme.css", "theme.js"):
            with self.client.get("/static/" + name) as response:
                self.assertEqual(response.status_code, 200)
            base = Path(web.__file__).parent
            self.assertEqual((base / "static" / name).read_bytes(), (base / "public" / "static" / name).read_bytes())
        with web.app.test_request_context("/"):
            for template in ("index.html", "login.html", "admin.html", "report.html"):
                html = web.render_template(template, records=[], total=0)
                self.assertIn('class="theme-toggle"', html)
                self.assertIn('/static/theme.js', html)


if __name__ == "__main__":
    unittest.main()

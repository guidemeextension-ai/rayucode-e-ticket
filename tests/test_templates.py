"""Regression tests for admin/report template output escaping and logo assets."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-only", "SECRET_KEY": "test-session"}):
    import app as web


MALICIOUS_CODE = "x');alert(1);//"
MALICIOUS_CHAT_NAME = "'><img src=x onerror=alert(1)>"


def _record():
    return {
        "id": "abc",
        "promo_code": MALICIOUS_CODE,
        "customer_name": "",
        "phone": "",
        "discount": 20,
        "telegram_chat_id": "",
        "telegram_chat_name": MALICIOUS_CHAT_NAME,
        "created_at": "2026-01-01T00:00:00",
    }


class TemplateEscapingTests(unittest.TestCase):
    def render(self, template):
        with web.app.test_request_context("/"):
            return web.render_template(template, records=[_record()], total=1)

    def test_admin_inline_handlers_are_json_escaped(self):
        html = self.render("admin.html")
        self.assertNotIn('onclick="showQR(\'', html)
        self.assertNotIn('onclick="askSend(\'', html)
        self.assertNotIn('onclick="askDelete(\'', html)
        self.assertNotIn("');alert(1);//", html)
        self.assertNotIn("'><img", html)
        self.assertIn("\\u0027", html)

    def test_report_inline_handler_is_json_escaped(self):
        html = self.render("report.html")
        self.assertNotIn('onclick="confirmDelete(\'', html)
        self.assertNotIn("');alert(1);//", html)
        self.assertNotIn("'><img", html)
        self.assertIn("\\u0027", html)

    def test_templates_render_empty_with_theme_toggle(self):
        with web.app.test_request_context("/"):
            for template in ("admin.html", "report.html"):
                html = web.render_template(template, records=[], total=0)
                self.assertIn('class="theme-toggle"', html)

    def test_telegram_status_innerhtml_values_are_escaped(self):
        html = self.render("admin.html")
        self.assertIn("function esc(value)", html)
        for expression in (
            "esc(botUsername)",
            "esc(d.username || '?')",
            "esc(wh.url)",
            "esc(wh.pending_update_count)",
            "esc(wh.last_error_message)",
            "esc(e)",
        ):
            self.assertIn(expression, html)


class LogoAssetTests(unittest.TestCase):
    def test_logo_copies_are_small_and_identical(self):
        base = Path(web.__file__).parent
        static_logo = base / "static" / "img" / "rayucode-logo.png"
        public_logo = base / "public" / "static" / "img" / "rayucode-logo.png"

        for path in (static_logo, public_logo):
            self.assertTrue(path.is_file(), path)
            self.assertLess(path.stat().st_size, 60000, path)
            with Image.open(path) as image:
                self.assertEqual(image.size, (160, 160), path)
                self.assertEqual(image.format, "PNG", path)

        self.assertEqual(static_logo.read_bytes(), public_logo.read_bytes())


if __name__ == "__main__":
    unittest.main()

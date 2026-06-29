from __future__ import annotations

import unittest

from nav_html import header_nav, pipeline_health_icons


class NavigationTests(unittest.TestCase):
    def test_pipeline_icons_follow_settings_and_replace_text_health_link(self) -> None:
        html = header_nav("/")

        self.assertLess(html.index('id="nav-settings"'), html.index('class="nav-pipeline"'))
        self.assertNotIn("Health check", html)
        self.assertEqual(html.count('href="/healthcheck"'), 3)

    def test_pipeline_icon_tones_reflect_stage_statuses(self) -> None:
        html = pipeline_health_icons("/", {
            "morning": {"status": "SUCCESS", "message": "Ready"},
            "confirmation": {"status": "DEGRADED", "message": "Partial"},
            "report": {"status": "BLOCKED", "message": "Waiting"},
        })

        self.assertIn("pipeline-success", html)
        self.assertIn("pipeline-warning", html)
        self.assertIn("pipeline-failed", html)
        self.assertIn("Morning: SUCCESS", html)


if __name__ == "__main__":
    unittest.main()

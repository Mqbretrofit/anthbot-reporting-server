from __future__ import annotations

import unittest

import diagnostics_dashboard


class CloudApiDashboardTests(unittest.TestCase):
    def test_cloud_api_error_has_distinct_dashboard_identity(self) -> None:
        report = {
            "schema": "anthbot-firmware-diagnostics-v1",
            "device": {
                "model": "Anthbot M9 Pro",
                "serial_suffix": "0110",
                "serial_sha256": "072c8a8474d40082669ff6bf41b08ce0",
            },
            "cloud_api_error": {
                "category": "anthbot_cloud",
                "operation": "iot_sts",
                "api_code": 500,
                "http_status": None,
                "temporary": True,
                "attempts": 3,
                "message": "ANTHBOT API returned code=500",
            },
        }

        identity = diagnostics_dashboard._report_identity(report)
        summary = diagnostics_dashboard._cloud_api_error_summary(report)

        self.assertEqual(identity["report_type"], "robot")
        self.assertEqual(identity["report_type_label"], "ANTHBOT cloud/API hiba")
        self.assertIn("…0110", identity["robot_id"])
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["operation"], "iot_sts")
        self.assertEqual(summary["api_code"], 500)
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["message"], "ANTHBOT API returned code=500")

    def test_non_cloud_report_keeps_existing_robot_label(self) -> None:
        identity = diagnostics_dashboard._report_identity(
            {
                "schema": "anthbot-firmware-diagnostics-v1",
                "device": {"model": "Anthbot Genie 3000"},
            }
        )

        self.assertEqual(identity["report_type_label"], "Robot diagnosztika")


if __name__ == "__main__":
    unittest.main()

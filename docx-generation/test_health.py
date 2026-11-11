"""docx-generation health check."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app import app

CLIENT = TestClient(app)


class HealthTests(unittest.TestCase):
    def test_health(self) -> None:
        resp = CLIENT.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()

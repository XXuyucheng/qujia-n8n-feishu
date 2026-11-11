"""HTTP coverage for POST /api/generate."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi.testclient import TestClient

from app import app
from test_fill import FILLED, build_sample_docx, paragraph_texts, parse_xml, zip_part

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class GenerateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_templates = os.environ.get("DOCX_TEMPLATES_DIR")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DOCX_TEMPLATES_DIR"] = str(self.root)
        (self.root / "合同.docx").write_bytes(build_sample_docx())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self._old_templates is None:
            os.environ.pop("DOCX_TEMPLATES_DIR", None)
        else:
            os.environ["DOCX_TEMPLATES_DIR"] = self._old_templates
        self.tmp.cleanup()

    def test_json_fills_split_run_and_warns(self) -> None:
        resp = self.client.post("/api/generate", json=self._body())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wordprocessingml", resp.headers["content-type"])
        self.assertIn(quote("合同.docx", safe=""), resp.headers["content-disposition"])
        self.assertIn("filename*=UTF-8''", resp.headers["content-disposition"])
        texts = paragraph_texts(parse_xml(zip_part(resp.content, "word/document.xml")))
        self.assertIn("桐庐", texts)
        self.assertIn("{{保险名称}}", texts)
        self.assertEqual(
            json.loads(unquote(resp.headers["x-docx-unresolved"])),
            ["保险名称"],
        )

    def test_no_unresolved_header_when_every_key_is_given(self) -> None:
        placeholders = dict(FILLED)
        placeholders["保险名称"] = "平安行"
        resp = self.client.post("/api/generate", json=self._body(placeholders))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("x-docx-unresolved", resp.headers)

    def test_multipart_upload(self) -> None:
        resp = self.client.post(
            "/api/generate",
            data={
                "placeholders": json.dumps(FILLED, ensure_ascii=False),
                "required": "[]",
                "empty_optional": "/",
            },
            files={"file": ("上传合同.docx", build_sample_docx(), DOCX_TYPE)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(quote("上传合同.docx", safe=""), resp.headers["content-disposition"])
        texts = paragraph_texts(parse_xml(zip_part(resp.content, "word/document.xml")))
        self.assertIn("桐庐", texts)

    def test_path_escape(self) -> None:
        for template in ("../secret.docx", "/tmp/secret.docx"):
            resp = self.client.post("/api/generate", json=self._body(template=template))
            self.assertEqual(resp.status_code, 400, template)
            self.assertEqual(resp.json()["error"], "template_path_invalid")

    def test_missing_template(self) -> None:
        resp = self.client.post("/api/generate", json=self._body(template="missing.docx"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json(),
            {"error": "template_not_found", "template": "missing.docx"},
        )

    def test_missing_required_returns_json(self) -> None:
        resp = self.client.post(
            "/api/generate",
            json=self._body(
                {"甲方名称": "", "甲方税号": ""},
                required=["甲方名称"],
            ),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json(),
            {"error": "missing_required", "missing": ["甲方名称"]},
        )
        self.assertFalse(resp.content.startswith(b"PK"))

    def test_non_string_placeholder_is_400(self) -> None:
        resp = self.client.post(
            "/api/generate",
            json=self._body({"甲方名称": ["不是字符串"]}),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_placeholders")

    def _body(
        self,
        placeholders: dict | None = None,
        template: str = "合同.docx",
        required: list[str] | None = None,
    ) -> dict:
        return {
            "template": template,
            "placeholders": FILLED if placeholders is None else placeholders,
            "required": [] if required is None else required,
            "empty_optional": "/",
        }


if __name__ == "__main__":
    unittest.main()

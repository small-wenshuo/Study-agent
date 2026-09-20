from __future__ import annotations

import unittest

from app.services.citations import append_source_links, validate_citations


class CitationTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            "S1": {"title": "Official docs", "url": "https://example.com/docs", "content": "evidence"}
        }

    def test_rejects_unknown_source(self):
        valid, reason = validate_citations("结论 [S2]", self.sources, True)
        self.assertFalse(valid)
        self.assertIn("未知", reason)

    def test_requires_citation_when_a_page_was_read(self):
        valid, reason = validate_citations("无引用结论", self.sources, False)
        self.assertFalse(valid)
        self.assertIn("没有引用", reason)

    def test_appends_only_cited_source_links(self):
        answer = append_source_links("结论 [S1]", self.sources)
        self.assertIn("[Official docs](https://example.com/docs)", answer)


if __name__ == "__main__":
    unittest.main()

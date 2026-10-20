import json
import os
from pathlib import Path
import subprocess
import unittest

import yaml

import helpers  # noqa: F401  (sets sys.path)
import dy_author

SKILL = Path(__file__).resolve().parents[1]
LUMA = ("6.99 复制打开抖音，看看【示例作者的作品】面试官：业务问为什么跌了，Agent怎么查数不翻车... "
        "https://v.douyin.com/exampleVideo/ K@W.mQ :3pm 02/28 BGV:/")


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


class IntegrationTests(unittest.TestCase):
    def test_skill_and_command_frontmatter(self):
        skill = frontmatter(SKILL / "SKILL.md")
        self.assertEqual(skill["name"], "dy-author")
        self.assertIn("/dy-author", skill["description"])
        command = frontmatter(SKILL / "integrations/cc-connect/dy-author.md")
        self.assertTrue(command["disable-model-invocation"])
        self.assertIn("补录", command["argument-hint"])
        for ref in ("workflow.md", "knowledge-notes.md", "interview-notes.md", "recovery.md"):
            self.assertTrue((SKILL / "references" / ref).is_file(), ref)

    def test_command_marker_isolates_raw_arguments(self):
        template = (SKILL / "integrations/cc-connect/dy-author.md").read_text(encoding="utf-8")
        self.assertTrue(template.rstrip().endswith("DYA_RAW_ARGUMENTS_BEGIN"))
        expanded = template + LUMA + "\n"
        raw = expanded.split("\nDYA_RAW_ARGUMENTS_BEGIN\n", 1)[1].strip()
        self.assertEqual(raw, LUMA)
        mode, text = dy_author.parse_input("/dy-author " + raw)
        self.assertEqual(mode, "author")
        import author_works
        self.assertEqual(author_works.select_official_url(text), "https://v.douyin.com/exampleVideo/")



if __name__ == "__main__":
    unittest.main()

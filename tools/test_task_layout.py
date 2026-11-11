#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Keep task specifications, completion reports and entry points discoverable."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent


class TestTaskLayout(unittest.TestCase):
    def test_specs_are_in_task_directory_only(self):
        for number in range(1, 8):
            name = f'TASK{number}.md'
            self.assertTrue((ROOT / 'TASK' / name).is_file(), name)
            self.assertFalse((ROOT / name).exists(), name)

    def test_completion_reports_preserve_original_requirements(self):
        for number in range(4, 8):
            report = ROOT / 'TASK-HISTORY' / f'TASK{number}-COMPLETED.md'
            text = report.read_text(encoding='utf-8')
            self.assertIn(f'../TASK/TASK{number}.md', text)
            self.assertTrue(text.strip())

    def test_local_documentation_links_resolve(self):
        pages = [ROOT / 'README.md', ROOT / 'TASK' / 'README.md',
                 *sorted((ROOT / 'TASK-HISTORY').glob('*.md'))]
        for page in pages:
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)',
                                     page.read_text(encoding='utf-8')):
                if '://' in target or target.startswith('#'):
                    continue
                path = target.split('#', 1)[0]
                self.assertTrue((page.parent / path).exists(),
                                f'{page.relative_to(ROOT)}: {target}')


if __name__ == '__main__':
    unittest.main()

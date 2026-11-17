"""Wiki configuration is local, portable, and retained on reconfiguration."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_portable import clone_package


class WikiPortableTests(unittest.TestCase):
    def run_python(self, root, *args):
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', *args], cwd=root,
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_new_root_preserved_without_downloading_or_changing_other_scopes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'Workbench'
            root.mkdir()
            clone_package(root)
            corpus = Path(folder) / 'external wiki'
            self.run_python(root, 'configure.py', '--wiki-root', str(corpus))
            core = json.loads((root / 'core/config.json').read_text('utf-8'))
            self.assertEqual(core['scopes']['wiki']['root'], str(corpus.resolve()))
            self.assertNotEqual(core['scopes']['dayz']['root'], str(corpus))
            self.assertFalse(corpus.exists())
            self.run_python(root, 'configure.py')
            again = json.loads((root / 'core/config.json').read_text('utf-8'))
            self.assertEqual(again['scopes']['wiki'], core['scopes']['wiki'])
            output = self.run_python(root, 'bobr.py', 'context', '--topic', 'community-wiki', '--scope', 'wiki', '--budget', '2500')
            self.assertIn('community-wiki', json.dumps(output))
            self.assertFalse(corpus.exists())


if __name__ == '__main__':
    unittest.main()

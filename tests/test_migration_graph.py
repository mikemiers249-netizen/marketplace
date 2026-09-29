"""Validate the migration graph and the revision used by container startup."""
import ast
from pathlib import Path
import re
import unittest
import warnings

from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]


class MigrationGraphTests(unittest.TestCase):
    def test_unique_revisions_and_valid_startup_target(self):
        revisions = {}
        for path in (ROOT / 'migrations' / 'versions').glob('*.py'):
            for node in ast.parse(path.read_text(encoding='utf-8-sig')).body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == 'revision'
                    for target in node.targets
                ):
                    revision = ast.literal_eval(node.value)
                    self.assertNotIn(revision, revisions, f'Duplicate revision in {path}')
                    revisions[revision] = path
        with warnings.catch_warnings():
            warnings.simplefilter('error', UserWarning)
            scripts = ScriptDirectory(str(ROOT / 'migrations'))
            history = list(scripts.walk_revisions())
            self.assertEqual(len(history), len(revisions))
            command = next(line for line in (ROOT / 'Dockerfile').read_text(encoding='utf-8').splitlines()
                           if line.startswith('CMD '))
            target = re.search(r'flask db stamp ([a-zA-Z0-9_]+)', command).group(1)
            self.assertIn(target, scripts.get_heads())
            self.assertEqual(scripts.get_revision(target).down_revision, 't2u3v4w5x6y7')


if __name__ == '__main__':
    unittest.main()

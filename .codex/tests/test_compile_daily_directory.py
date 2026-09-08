from pathlib import Path
import os
import subprocess
import tempfile
import unittest

import _fixtures  # noqa: F401
import compile_state


class CompileDailyDirectoryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows directory junction')
    def test_broken_daily_junction_is_not_an_empty_compile_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = vault / 'daily'
            subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(daily), str(vault / 'missing-target')],
                check=True, capture_output=True,
            )
            with self.assertRaisesRegex(compile_state.PolicyError, 'unsafe-daily-directory'):
                compile_state.changed_dailies(vault, compile_state.CompileState())
            with self.assertRaisesRegex(compile_state.PolicyError, 'unsafe-daily-directory'):
                compile_state.has_changes(vault)
            self.assertTrue(daily.is_junction())

    def test_broken_daily_directory_link_is_not_an_empty_compile_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            target = vault / 'missing-daily-target'
            daily = vault / 'daily'
            daily.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(compile_state.PolicyError, 'unsafe-daily-directory'):
                compile_state.changed_dailies(vault, compile_state.CompileState())
            with self.assertRaisesRegex(compile_state.PolicyError, 'unsafe-daily-directory'):
                compile_state.has_changes(vault)

            self.assertTrue(daily.is_symlink())
            self.assertFalse(target.exists())

    def test_absent_daily_directory_is_a_valid_empty_compile_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            self.assertEqual(compile_state.changed_dailies(vault, compile_state.CompileState()), [])
            self.assertFalse(compile_state.has_changes(vault))
            self.assertEqual(list(vault.iterdir()), [])


if __name__ == '__main__':
    unittest.main()

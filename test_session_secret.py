"""Session secret persistence: a restart must not orphan existing workspaces/conversations."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent


def boot_secret(workdir, env_extra=None):
    env = dict(os.environ)
    env.pop('GROK_SESSION_SECRET', None)
    env['GROK_WORKSPACE_DIR'] = str(Path(workdir) / 'workspaces')
    if env_extra:
        env.update(env_extra)
    out = subprocess.run([sys.executable, '-c', 'import webui; print(webui.app.secret_key)'],
                         cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1], out


class SessionSecretTests(unittest.TestCase):
    def test_generated_secret_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            first, _ = boot_secret(d)
            second, _ = boot_secret(d)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 64)
            secret_file = Path(d) / '.session_secret'
            self.assertTrue(secret_file.is_file())
            self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)

    def test_env_secret_wins_and_writes_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            secret, _ = boot_secret(d, {'GROK_SESSION_SECRET': 'mine'})
            self.assertEqual(secret, 'mine')
            self.assertFalse((Path(d) / '.session_secret').exists())

    @unittest.skipIf(os.name != 'posix', 'chmod-based read-only dir needs posix')
    def test_unwritable_dir_falls_back_to_ephemeral_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o500)
            try:
                secret, out = boot_secret(d)
            finally:
                os.chmod(d, 0o700)
            self.assertEqual(len(secret), 64)
            self.assertIn('sessions are ephemeral', out.stdout)


if __name__ == '__main__':
    unittest.main()

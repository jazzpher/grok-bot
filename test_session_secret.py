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


@unittest.skipIf(os.name == 'nt', 'XDG default is POSIX-only')
class DefaultDataDirTests(unittest.TestCase):
    def test_default_dir_is_per_user_not_the_working_directory(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as elsewhere:
            env = {k: v for k, v in os.environ.items()
                   if k not in ('GROK_WORKSPACE_DIR', 'GROK_SESSION_SECRET')}
            env['XDG_DATA_HOME'] = data
            # No HOME override: it would also move the user site-packages (deps installed with
            # --user become invisible) and break the subprocess on machines without a venv.
            # XDG_DATA_HOME alone keeps the secret and the workspaces inside `data`.
            env['PYTHONPATH'] = str(REPO) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
            out = subprocess.run([sys.executable, '-c', 'import webui; print(webui.WORKSPACES)'],
                                 cwd=elsewhere, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(Path(out.stdout.strip().splitlines()[-1]), Path(data) / 'grokbot' / 'workspaces')
            self.assertTrue((Path(data) / 'grokbot' / '.session_secret').is_file())
            # Starting from another directory must not scatter data next to it.
            self.assertEqual(list(Path(elsewhere).iterdir()), [])

    def test_concurrent_boots_share_one_secret(self):
        """O_CREAT|O_EXCL: parallel workers converge on one secret instead of racing."""
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ)
            env.pop('GROK_SESSION_SECRET', None)
            env['GROK_WORKSPACE_DIR'] = str(Path(d) / 'workspaces')
            code = 'import webui; print(webui.app.secret_key)'
            procs = [subprocess.Popen([sys.executable, '-c', code], cwd=REPO, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(6)]
            keys = set()
            for proc in procs:
                out, err = proc.communicate(timeout=120)
                self.assertEqual(proc.returncode, 0, err)
                keys.add(out.strip().splitlines()[-1])
            self.assertEqual(len(keys), 1, keys)
            self.assertEqual(len(keys.pop()), 64)


if __name__ == '__main__':
    unittest.main()

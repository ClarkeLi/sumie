"""Installer logic that does not need a game, a .NET SDK or a network."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import install


def make_mac_game(root: Path, branch='beta'):
    managed = root / branch / 'Draw&Guess.app' / 'Contents' / 'Resources' / 'Data' / 'Managed'
    managed.mkdir(parents=True)
    (managed / 'Assembly-CSharp.dll').write_bytes(b'x')
    return root / branch


def make_windows_game(root: Path):
    managed = root / 'DrawGuess_Data' / 'Managed'
    managed.mkdir(parents=True)
    (managed / 'Assembly-CSharp.dll').write_bytes(b'x')
    (root / 'DrawGuess.exe').write_bytes(b'x')
    return root


class FindGameTest(unittest.TestCase):
    def test_mac_game_in_branch_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(install, 'IS_WINDOWS', False):
            folder = make_mac_game(Path(tmp))
            found, launcher, managed = install.find_game(tmp)
            self.assertEqual(found, folder)
            self.assertEqual(launcher.name, 'Draw&Guess.app')
            self.assertTrue((managed / 'Assembly-CSharp.dll').is_file())

    def test_windows_game_directly_in_folder(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(install, 'IS_WINDOWS', True):
            make_windows_game(Path(tmp))
            found, launcher, managed = install.find_game(tmp)
            self.assertEqual(found, Path(tmp))
            self.assertEqual(launcher.name, 'DrawGuess.exe')
            self.assertEqual(managed.name, 'Managed')

    def test_missing_game_gives_a_helpful_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(install, 'IS_WINDOWS', False):
            with self.assertRaises(SystemExit) as raised:
                install.find_game(tmp)
            self.assertIn('--game', str(raised.exception))

    def test_il2cpp_build_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed = Path(tmp) / 'Game_Data' / 'Managed'
            managed.mkdir(parents=True)
            (managed.parent / 'il2cpp_data').mkdir()
            with self.assertRaises(SystemExit) as raised:
                install.check_mono(managed)
            self.assertIn('IL2CPP', str(raised.exception))


class StudioConfigTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(install, 'project_python', return_value=Path('/venv/bin/python'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, game):
        return (game / 'BepInEx' / 'config' / install.CONFIG_NAME).read_text(encoding='utf-8')

    def test_creates_a_new_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            install.write_studio_config(Path(tmp))
            text = self.read(Path(tmp))
            self.assertIn('[Studio]', text)
            self.assertIn('Python = ' + str(Path('/venv/bin/python')), text)
            self.assertIn('WorkingDirectory = ' + str(install.ROOT), text)

    def test_updates_values_and_keeps_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'BepInEx' / 'config' / install.CONFIG_NAME
            config.parent.mkdir(parents=True)
            config.write_text('[Server]\nPort = 9000\n\n[Studio]\n\nEnabled = false\n'
                              'Python = /old/python\nPort = 8771\n\n[Drawing]\nStrokesPerFrame = 5\n',
                              encoding='utf-8')
            install.write_studio_config(Path(tmp))
            install.write_studio_config(Path(tmp))          # twice: stays stable
            text = self.read(Path(tmp))
            self.assertIn('Port = 9000', text)
            self.assertIn('Enabled = false', text)
            self.assertIn('Port = 8771', text)
            self.assertIn('StrokesPerFrame = 5', text)
            self.assertNotIn('/old/python', text)
            self.assertEqual(text.count('Python ='), 1)
            self.assertEqual(text.count('Script ='), 1)


class PinnedDownloadsTest(unittest.TestCase):
    def test_every_platform_has_a_pinned_checksum(self):
        for label, digest in install.BEPINEX.values():
            self.assertEqual(len(digest), 64)
            self.assertTrue(label)


if __name__ == '__main__':
    unittest.main()


def make_linux_game(root: Path):
    managed = root / 'DrawGuess_Data' / 'Managed'
    managed.mkdir(parents=True)
    (managed / 'Assembly-CSharp.dll').write_bytes(b'x')
    (root / 'DrawGuess.x86_64').write_bytes(b'x')
    return root


class LinuxLayoutTest(unittest.TestCase):
    def test_linux_game_found(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(install, 'IS_WINDOWS', False), mock.patch.object(install, 'IS_LINUX', True):
            make_linux_game(Path(tmp))
            found, launcher, managed = install.find_game(tmp)
            self.assertEqual(found, Path(tmp))
            self.assertEqual(launcher.name, 'DrawGuess.x86_64')
            self.assertTrue((managed / 'Assembly-CSharp.dll').is_file())

    def test_linux_script_uses_executable_name(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as bep, \
                mock.patch.object(install, 'IS_WINDOWS', False), mock.patch.object(install, 'IS_MAC', False), \
                mock.patch.object(install, 'IS_LINUX', True):
            (Path(bep) / 'BepInEx' / 'core').mkdir(parents=True)
            for name in ('libdoorstop.so', '.doorstop_version'):
                (Path(bep) / name).write_text('x')
            (Path(bep) / 'run_bepinex.sh').write_text('#!/bin/sh\nexecutable_name=""\n')
            game = Path(tmp)
            install.install_bepinex(Path(bep), game, game / 'DrawGuess.x86_64')
            self.assertIn('executable_name="DrawGuess.x86_64"', (game / 'run_bepinex.sh').read_text())

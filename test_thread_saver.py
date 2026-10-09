import ast
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

import dvach


ROOT = Path(__file__).resolve().parent


class StopPolling(BaseException):
    """Stop after the first completed save, without entering network retries."""


class TestThreadSaver(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        # HtmlGenerator reads its real templates relative to CWD.
        shutil.copytree(ROOT / 'page_gen', self.work / 'page_gen')
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(self.work)

        self.network_guard = patch(
            'dvach.requests.get', side_effect=AssertionError('Unexpected network request'))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

        # Load definitions and the unchanged main block separately so script
        # settings and network boundaries can be patched before main executes.
        # Each run gets fresh globals, including deleted_posts and FOLDER.
        source = ast.parse((ROOT / 'thread_saver.py').read_text(encoding='utf-8'))
        self.assertIsInstance(source.body[-1], ast.If)
        self.definitions = compile(
            ast.Module(body=source.body[:-1], type_ignores=[]),
            str(ROOT / 'thread_saver.py'), 'exec')
        self.main = compile(
            ast.Module(body=[source.body[-1]], type_ignores=[]),
            str(ROOT / 'thread_saver.py'), 'exec')

    def make_thread(self, board, number, names):
        post = dvach.Post({
            'num': number, 'comment': 'Local OP', 'date': '09/10/26',
            'email': '', 'op': 1,
            'files': [
                {'displayname': name, 'name': name,
                 'path': f'/{board}/src/{number}/{name}',
                 'width': 1, 'height': 1, 'size': 1}
                for name in names
            ],
        })
        thread = dvach.Thread(board)
        thread.num = number
        thread.lasthit = 0
        thread.comment_html = post.comment_html
        thread.posts = [post]
        return thread

    def run_saver(self, arguments=('b', '123'), names=('image.jpg',),
                  save_media=True, interactive=False):
        board, number = ('b', '123') if interactive else arguments[:2]
        thread = self.make_thread(board, number, names)
        namespace = {'__name__': '__main__'}
        exec(self.definitions, namespace)
        self.get_board = Mock(return_value=dvach.Board(board, {}))
        self.get_thread = Mock(return_value=thread)
        namespace.update(get_board=self.get_board, get_thread=self.get_thread,
                         SAVE_MEDIA=save_media)
        argv = ['thread_saver.py'] + ([] if interactive else list(arguments))
        with patch.object(sys, 'argv', argv), \
                patch('builtins.input', side_effect=[board, number]) as user_input, \
                patch.object(dvach.Thread, 'posts', []), \
                patch('time.sleep', side_effect=StopPolling) as sleep, \
                patch('dvach.download_link', return_value=SimpleNamespace(content=b'media')) as download, \
                patch.object(dvach.Post_file, 'save', autospec=True,
                             side_effect=dvach.Post_file.save) as media_save, \
                patch.object(dvach.Thread, 'save', autospec=True,
                             side_effect=dvach.Thread.save) as html_save, \
                redirect_stdout(io.StringIO()):
            self.download = download
            self.media_save = media_save
            self.html_save = html_save
            if interactive:
                self.assertEqual(argv, ['thread_saver.py'])
            try:
                exec(self.main, namespace)
            except StopPolling:
                sleep.assert_called_once_with(namespace['DELAY'])
            else:
                self.fail('Main block did not reach the polling interval')
            if interactive:
                self.assertEqual(user_input.call_count, 2)
            else:
                user_input.assert_not_called()
        self.get_board.assert_called_once_with(board)
        self.get_thread.assert_called_once_with(self.get_board.return_value, number)
        self.assertEqual(thread.posts, [thread.get_op_post])
        return namespace

    def assert_media_paths(self, *paths):
        self.assertEqual(
            [call.args[1] for call in self.media_save.call_args_list],
            [os.path.normpath(str(path)) for path in paths])
        self.assertEqual(self.download.call_count, len(paths))
        for path in paths:
            self.assertEqual(Path(path).read_bytes(), b'media')

    def test_s1_first_run(self):
        self.assertFalse(Path('saver').exists())
        namespace = self.run_saver()
        self.assertEqual(namespace['FOLDER'], os.path.join('saver', 'b', '123'))
        self.assert_media_paths('saver/b/123/image.jpg')
        self.assertTrue(Path('saver/b/123/thread_123.html').is_file())
        self.assertFalse(Path('saver/image.jpg').exists())
        self.assertFalse(Path('saver/thread_123.html').exists())

    def test_s2_repeat_run_preserves_existing_media_and_saves_new(self):
        self.run_saver()
        existing = Path('saver/b/123/image.jpg')
        existing.write_bytes(b'existing archive')
        self.run_saver(names=('image.jpg', 'new.jpg'))
        self.assert_media_paths('saver/b/123/new.jpg')
        self.assertEqual(existing.read_bytes(), b'existing archive')

    def test_s3_custom_relative_and_absolute_bases_with_spaces(self):
        for base in ('nested/custom archive', str(self.work / 'absolute' / 'custom archive')):
            with self.subTest(base=base):
                self.assertFalse(Path(base).exists())
                self.run_saver(arguments=('b', '123', base))
                destination = Path(base) / 'b' / '123'
                self.assert_media_paths(destination / 'image.jpg')
                self.html_save.assert_called_once()
                self.assertEqual(self.html_save.call_args.args[1], str(destination))
                self.assertTrue((destination / 'thread_123.html').is_file())
                self.assertFalse((Path(base) / 'image.jpg').exists())

    def test_s4_isolation_and_legacy_archive(self):
        Path('saver').mkdir()
        legacy = Path('saver/image.jpg')
        legacy.write_bytes(b'legacy archive')
        destinations = []
        for board, number in (('b', '123'), ('b', '456'), ('news', '123')):
            with self.subTest(board=board, number=number):
                self.run_saver(arguments=(board, number))
                destination = Path('saver') / board / number / 'image.jpg'
                self.assert_media_paths(destination)
                destination.write_bytes(f'{board}/{number}'.encode())
                destinations.append(destination)
        for destination in destinations:
            self.assertEqual(destination.read_bytes(),
                             f'{destination.parent.parent.name}/{destination.parent.name}'.encode())
        self.assertEqual(legacy.read_bytes(), b'legacy archive')

    def test_s5_interactive_input(self):
        self.run_saver(interactive=True)
        self.assert_media_paths('saver/b/123/image.jpg')
        self.assertTrue(Path('saver/b/123/thread_123.html').is_file())

    def test_s6_real_html_has_resolvable_local_op_image(self):
        self.run_saver()
        html_path = Path('saver/b/123/thread_123.html')
        page = BeautifulSoup(html_path.read_text(encoding='utf-8'), 'html.parser')
        image = page.find('img', src='image.jpg')
        self.assertIsNotNone(image)
        self.assertEqual((html_path.parent / image['src']).read_bytes(), b'media')
        self.html_save.assert_called_once()
        self.assertEqual(self.html_save.call_args.args[1], os.path.join('saver', 'b', '123'))

    def test_s6_disabled_media_still_saves_html(self):
        self.run_saver(save_media=False)
        self.media_save.assert_not_called()
        self.download.assert_not_called()
        self.html_save.assert_called_once()
        self.assertTrue(Path('saver/b/123/thread_123.html').is_file())
        self.assertFalse(Path('saver/b/123/image.jpg').exists())

    def assert_no_saving_started(self):
        self.get_board.assert_not_called()
        self.get_thread.assert_not_called()
        self.media_save.assert_not_called()
        self.download.assert_not_called()
        self.html_save.assert_not_called()

    def test_s7_file_conflict_is_not_hidden(self):
        for component in ('saver', 'saver/b', 'saver/b/123'):
            with self.subTest(component=component):
                conflict = Path(component)
                conflict.parent.mkdir(parents=True, exist_ok=True)
                conflict.write_bytes(b'not a directory')
                before = set(Path('.').rglob('*'))
                with self.assertRaises((FileExistsError, NotADirectoryError)):
                    self.run_saver()
                self.assert_no_saving_started()
                self.assertEqual(set(Path('.').rglob('*')), before)
                self.assertEqual(conflict.read_bytes(), b'not a directory')
                conflict.unlink()

    def test_s7_permission_error_is_not_hidden(self):
        before = set(Path('.').rglob('*'))
        error = PermissionError('directory creation denied')
        with patch('os.makedirs', side_effect=error) as makedirs:
            with self.assertRaises(PermissionError) as raised:
                self.run_saver()
            self.assertIs(raised.exception, error)
            makedirs.assert_called_once_with(os.path.join('saver', 'b', '123'), exist_ok=True)
        self.assert_no_saving_started()
        self.assertEqual(set(Path('.').rglob('*')), before)


if __name__ == '__main__':
    unittest.main()

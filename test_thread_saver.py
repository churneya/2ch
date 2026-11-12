import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from bs4 import BeautifulSoup

import dvach
import thread_saver
from test_op_post_files import OfflineTestCase, make_thread


ROOT = Path(__file__).resolve().parent


class StopPolling(BaseException):
    """Stop after a completed save, without entering network retries."""


class TestGetThread(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((ROOT / 'test_files/posts_json.json').read_text(encoding='utf-8'))
        posts = fixture['threads'][0]['posts'][:2]
        for number, post in enumerate(posts, 2341729):
            post['num'] = number
        posts[0]['comment'] = '<b>Direct OP</b>'
        self.payload = {'unique_posters': 2, 'threads': [{'posts': posts}]}
        self.url = f'https://2ch{dvach.domain}/gg/res/2341729.json'
        # Fail fast even if the helper accidentally bypasses download_link.
        for target in ('dvach.requests.get', 'dvach.Board.json_download',
                       'dvach.Board.update_threads'):
            guard = patch(target, side_effect=AssertionError('Board/network call forbidden'))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)

    def response(self, payload):
        return SimpleNamespace(text=json.dumps(payload))

    def test_direct_thread_uses_real_parser_without_board_metadata(self):
        with patch('dvach.download_link', return_value=self.response(self.payload)) as download, \
                patch('time.sleep', side_effect=StopPolling) as sleep:
            thread = thread_saver.get_thread('gg', '2341729')
        download.assert_called_once_with(self.url)
        sleep.assert_not_called()
        self.assertIsInstance(thread, dvach.Thread)
        self.assertEqual(thread.num, 2341729)
        self.assertEqual(thread.board_name, 'gg')
        self.assertEqual(thread.unique_posters, 2)
        self.assertEqual([post.num for post in thread.posts], ['2341729', '2341730'])
        self.assertEqual(thread.comment_html, '<b>Direct OP</b>')
        self.assertEqual(thread.comment_html, thread.get_op_post.comment_html)
        self.assertEqual(thread.get_op_post.comment, 'Direct OP')
        self.assertEqual(thread.posts[1].comment_html,
                         self.payload['threads'][0]['posts'][1]['comment'])
        self.assertFalse(hasattr(thread, 'lasthit'))

    def test_failed_download_or_parsing_retries_only_direct_url(self):
        failures = (
            dvach.requests.exceptions.HTTPError('404 Not Found'),
            dvach.requests.exceptions.HTTPError('503 Service Unavailable'),
            SimpleNamespace(text='{invalid JSON'),
            self.response({'unique_posters': 0, 'threads': [{'posts': []}]}),
        )
        for failure in failures:
            with self.subTest(failure=failure), \
                    patch('dvach.download_link', side_effect=[failure, self.response(self.payload)]) as download, \
                    patch('time.sleep', side_effect=[None, StopPolling]) as sleep, \
                    redirect_stdout(io.StringIO()) as output:
                thread = thread_saver.get_thread('gg', '2341729')
                self.assertEqual(download.call_args_list, [call(self.url), call(self.url)])
                sleep.assert_called_once_with(5)
                self.assertEqual(output.getvalue(), 'Не удалось получить тред, повтор\n')
                self.assertEqual(thread.comment_html, '<b>Direct OP</b>')
                self.assertEqual(len(thread.posts), 2)

    def test_invalid_number_fails_before_download_or_retry(self):
        with patch('dvach.download_link') as download, \
                patch('time.sleep', side_effect=StopPolling) as sleep:
            with self.assertRaises(ValueError):
                thread_saver.get_thread('gg', 'not-a-number')
        download.assert_not_called()
        sleep.assert_not_called()


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
        for target in ('dvach.Board.json_download', 'dvach.Board.update_threads'):
            guard = patch(target, side_effect=AssertionError('Board call forbidden'))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)

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
        thread.comment_html = post.comment_html
        thread.posts = [post]
        return thread

    def run_saver(self, arguments=('b', '123'), names=('image.jpg',),
                  save_media=True, interactive=False):
        board, number = ('b', '123') if interactive else arguments[:2]
        thread = self.make_thread(board, number, names)
        namespace = {'__name__': '__main__'}
        exec(self.definitions, namespace)
        self.get_thread = Mock(return_value=thread)
        namespace.update(get_thread=self.get_thread, SAVE_MEDIA=save_media)
        argv = ['thread_saver.py'] + ([] if interactive else list(arguments))
        with patch.object(sys, 'argv', argv), \
                patch('builtins.input', side_effect=[board, number]) as user_input, \
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
        self.get_thread.assert_called_once_with(board, number)
        self.assertEqual(thread.posts, [thread.get_op_post])
        return namespace

    def assert_media_paths(self, *paths):
        self.assertEqual(
            [call.args[1] for call in self.media_save.call_args_list],
            [os.path.normpath(str(path)) for path in paths])
        self.assertEqual(self.download.call_count, len(paths))
        for path in paths:
            self.assertEqual(Path(path).read_bytes(), b'media')

    def direct_payload(self, names=('image.jpg',), reply_number=2341730):
        op = {
            'num': 2341729, 'comment': '<b>Direct OP</b>', 'date': '09/10/26',
            'email': '', 'op': 1,
            'files': [
                {'displayname': name, 'name': name,
                 'path': f'/gg/src/2341729/{name}',
                 'width': 1, 'height': 1, 'size': 1}
                for name in names
            ],
        }
        reply = {
            'num': reply_number, 'comment': f'Reply {reply_number}',
            'date': '09/10/26', 'email': '', 'op': 0, 'files': [],
        }
        return {'unique_posters': 2, 'threads': [{'posts': [op, reply]}]}

    def run_direct_saver(self, payloads):
        namespace = {'__name__': '__main__'}
        exec(self.definitions, namespace)
        url = f'https://2ch{dvach.domain}/gg/res/2341729.json'
        snapshots = iter(payloads)

        def download(link):
            if link == url:
                return SimpleNamespace(text=json.dumps(next(snapshots)))
            self.assertEqual(link, f'https://2ch{dvach.domain}/gg/src/2341729/image.jpg')
            return SimpleNamespace(content=b'direct media')

        with patch.object(sys, 'argv', ['thread_saver.py', 'gg', '2341729']), \
                patch('dvach.download_link', side_effect=download) as http, \
                patch('time.sleep', side_effect=[None] * (len(payloads) - 1) + [StopPolling]) as sleep, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(StopPolling):
                exec(self.main, namespace)
        self.assertEqual(sleep.call_args_list, [call(namespace['DELAY'])] * len(payloads))
        self.assertEqual([request for request in http.call_args_list if request == call(url)],
                         [call(url)] * len(payloads))
        return namespace, http, output.getvalue()

    def test_direct_json_saves_real_html_with_and_without_op_media(self):
        for names in (('image.jpg',), ()):
            with self.subTest(names=names):
                namespace, http, _ = self.run_direct_saver([self.direct_payload(names)])
                destination = Path('saver/gg/2341729')
                html_path = destination / 'thread_2341729.html'
                page = BeautifulSoup(html_path.read_text(encoding='utf-8'), 'html.parser')
                self.assertIn('Direct OP', page.get_text())
                self.assertIn('2341729', page.get_text())
                self.assertIn('Reply 2341730', page.get_text())
                self.assertIsNotNone(page.find('b', string='Direct OP'))
                self.assertFalse(hasattr(namespace['thread'], 'lasthit'))
                self.assertFalse(hasattr(namespace['safe_thread'], 'lasthit'))
                self.assertIsNot(namespace['safe_thread'].posts, namespace['thread'].posts)
                if names:
                    image = page.find('img', src='./image.jpg')
                    self.assertIsNotNone(image)
                    self.assertEqual((destination / image['src']).read_bytes(), b'direct media')
                    self.assertEqual(http.call_count, 2)
                else:
                    self.assertFalse(page.select('.op_post_file'))
                    self.assertEqual(list(destination.iterdir()), [html_path])
                    self.assertEqual(http.call_count, 1)
                shutil.rmtree('saver')

    def test_direct_polling_accumulates_posts_and_reports_deleted_reply(self):
        namespace, http, output = self.run_direct_saver([
            self.direct_payload(), self.direct_payload(reply_number=2341731),
        ])
        url = f'https://2ch{dvach.domain}/gg/res/2341729.json'
        media_url = f'https://2ch{dvach.domain}/gg/src/2341729/image.jpg'
        self.assertEqual(http.call_args_list, [call(url), call(media_url), call(url)])
        self.assertEqual([post.num for post in namespace['safe_thread'].posts],
                         ['2341729', '2341730', '2341731'])
        self.assertEqual([post.num for post in namespace['thread'].posts],
                         ['2341729', '2341731'])
        self.assertEqual(namespace['deleted_posts'], ['2341730'])
        for number in ('2341729', '2341730', '2341731'):
            self.assertEqual(output.count(f'Новый пост: {number}\n'), 1)
        self.assertEqual(output.count('Удаленный пост: 2341730\n'), 1)
        page = BeautifulSoup(Path('saver/gg/2341729/thread_2341729.html').read_text(
            encoding='utf-8'), 'html.parser')
        self.assertIn('Direct OP', page.get_text())
        self.assertIn('Reply 2341730', page.get_text())
        self.assertIn('Reply 2341731', page.get_text())

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
        image = page.find('img', src='./image.jpg')
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


class TestThreadSaverAttachments(OfflineTestCase):
    def test_all_downloaded_attachments_resolve_next_to_html(self):
        names = ['one.jpg', 'two.png', 'clip.mp4', 'clip.webm', 'notes & #.pdf']
        downloaded = make_thread(names)
        safe = make_thread([])
        safe.posts = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(thread_saver, 'FOLDER', folder), \
                patch.object(thread_saver, 'SAVE_MEDIA', True):
            self.http.side_effect = None
            self.http.return_value = Mock(content=b'offline attachment')
            thread_saver.add_new_posts(safe, downloaded.posts[:1])
            path = Path(safe.save(folder))
            self.assertEqual(path.parent, Path(folder))
            self.assert_local_urls(path, names)
            for name in names:
                self.assertEqual((path.parent / name).read_bytes(), b'offline attachment')
            self.assertEqual([call.args[0] for call in self.http.call_args_list],
                             [file.download_link for file in downloaded.get_op_post.files])

    def test_save_media_false_renders_without_download_or_local_files(self):
        names = ['image.jpg', 'clip.webm', 'README']
        downloaded = make_thread(names)
        safe = make_thread([])
        safe.posts = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(thread_saver, 'FOLDER', folder), \
                patch.object(thread_saver, 'SAVE_MEDIA', False), \
                patch.object(thread_saver, 'save_post_files', side_effect=AssertionError('Download forbidden')) as save_files:
            thread_saver.add_new_posts(safe, downloaded.posts[:1])
            path = Path(safe.save(folder))
            soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
            self.assertEqual([item.a['href'] for item in soup.select('.op_post_file')],
                             ['./' + name for name in names])
            self.assertEqual(list(Path(folder).iterdir()), [path])
            save_files.assert_not_called()
            self.http.assert_not_called()

    def test_media_failure_does_not_prevent_attachment_links(self):
        downloaded = make_thread(['image.jpg', 'clip.mp4'])
        safe = make_thread([])
        safe.posts = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(thread_saver, 'FOLDER', folder), \
                patch.object(thread_saver, 'SAVE_MEDIA', True), \
                patch('dvach.download_link', side_effect=OSError('Offline download failed')):
            thread_saver.add_new_posts(safe, downloaded.posts[:1])
            path = Path(safe.save(folder))
            soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
            self.assertEqual(len(soup.select('.op_post_file')), 2)
            self.assertEqual(list(Path(folder).iterdir()), [path])
        self.http.assert_not_called()


if __name__ == '__main__':
    unittest.main()

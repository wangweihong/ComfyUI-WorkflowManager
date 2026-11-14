"""Exercise real routes against disposable directories, without a running ComfyUI."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = dict(nodes=[dict(id=1, type='Note', widgets_values=['测试'])], links=[], version=0.4)


class RoutesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workflow-manager-unit-')
        self.user = Path(self.temp.name) / 'user'
        self.base = self.user / 'default/workflows'
        self.base.mkdir(parents=True)
        routes = web.RouteTableDef()
        sys.modules['folder_paths'] = types.SimpleNamespace(get_user_directory=lambda: str(self.user))
        sys.modules['server'] = types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=routes)))
        spec = importlib.util.spec_from_file_location('workflow_manager_test', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.get_config_path = lambda: str(Path(self.temp.name) / 'config.json')
        self.module = module
        self.helpers = sys.modules['workflow_manager_test.file_browser']
        app = web.Application()
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    def write(self, name, content):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
        return path

    async def get(self, route, path='', status=200, **params):
        response = await self.client.get('/workflow-manager/' + route, params=dict(path=path, **params))
        self.assertEqual(response.status, status, await response.text())
        return await response.json()

    async def post(self, route, data, status=200):
        response = await self.client.post('/workflow-manager/' + route, json=data)
        self.assertEqual(response.status, status, await response.text())
        return await response.json()

    async def test_modes_include_all_json_without_parsing(self):
        for name in ('正常.json', '配置.JSON', '损坏.json', '说明.md', '图.png'):
            self.write(name, 'not necessarily json')
        (self.base / '空目录').mkdir()
        default = await self.get('browse')
        self.assertEqual({x['name'] for x in default['items']}, {'正常.json', '配置.JSON', '损坏.json', '空目录'})
        all_files = await self.get('browse', mode='all')
        self.assertEqual(len(all_files['items']), 6)
        self.assertEqual(next(x['type'] for x in all_files['items'] if x['name'] == '说明.md'), 'file')
        await self.get('browse', mode='invalid', status=400)

    async def test_workflow_validation_and_text_fallback(self):
        for name, value in [('画布.JSON', WORKFLOW), ('配置.json', {'seed': 1}), ('api.json', {'1': {'class_type': 'Note', 'inputs': {}}}), ('伪装.json', {'nodes': [1], 'links': []})]:
            self.write(name, json.dumps(value))
        self.write('损坏.json', '{bad')
        self.assertEqual((await self.get('read-workflow', '画布.JSON'))['workflow'], WORKFLOW)
        for name in ('配置.json', 'api.json', '伪装.json', '损坏.json'):
            self.assertEqual((await self.get('read-workflow', name, status=422))['code'], 'not_workflow')
            self.assertEqual((await self.get('read-file', name))['kind'], 'text')

    async def test_text_encodings_binary_and_limit(self):
        cases = {'utf8.md': '中文 <script>alert(1)</script>'.encode(), 'bom.txt': b'\xef\xbb\xbf' + '带 BOM'.encode(), 'gb.txt': '中文编码'.encode('gb18030'), 'empty': b'', 'pixel.png': b'\x89PNG\r\n\x1a\n', 'binary.bin': b'a\x00b'}
        for name, content in cases.items():
            self.write(name, content)
            result = await self.get('read-file', name)
            self.assertEqual(result['kind'], 'binary' if name in ('pixel.png', 'binary.bin') else 'text')
        self.assertEqual((await self.get('read-file', 'gb.txt'))['content'], '中文编码')
        self.assertEqual((await self.get('read-file', 'bom.txt'))['content'], '带 BOM')
        # A UTF-8 character straddles the limit; do not mis-detect GB18030.
        self.write('large.txt', b'x' * (self.helpers.PREVIEW_LIMIT - 1) + '中文'.encode())
        result = await self.get('read-file', 'large.txt')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['encoding'], 'utf-8-sig')
        self.assertLessEqual(len(result['content'].encode()), self.helpers.PREVIEW_LIMIT)

    async def test_generic_file_operations_and_conflicts(self):
        self.write('旧父目录/子目录/说明.md', '保留内容')
        self.write('旧父目录/工作流.json', json.dumps(WORKFLOW))
        await self.post('rename', dict(old_path='旧父目录', new_name='新父目录 中文'))
        self.assertEqual((self.base / '新父目录 中文/子目录/说明.md').read_text(), '保留内容')
        await self.post('create-folder', dict(name='目标'))
        src = '新父目录 中文/子目录/说明.md'
        await self.post('copy', dict(source_path=src, target_dir='目标'))
        await self.post('copy', dict(source_path=src, target_dir='目标'), status=409)
        await self.post('move', dict(source_path=src, target_dir='目标'), status=409)
        await self.post('rename', dict(old_path='目标/说明.md', new_name='改名.txt'))
        await self.post('move', dict(source_path=src, target_dir='目标'))
        self.assertFalse((self.base / src).exists())
        await self.post('delete', dict(path='目标/改名.txt'))
        self.assertTrue((self.base / '目标/说明.md').exists())
        await self.post('copy', dict(source_path='新父目录 中文', target_dir='新父目录 中文/子目录'), status=400)

    async def test_paths_root_and_symbolic_link_boundaries(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('outside')
        (self.base / 'link').symlink_to(outside, target_is_directory=True)
        self.write('inside/keep.txt', 'keep')
        for route in ('browse', 'read-file', 'read-workflow'):
            for bad in ('../outside', 'link/secret.txt', str(outside / 'secret.txt')):
                await self.get(route, bad, status=400)
        for route, data in [('delete', {'path': '.'}), ('rename', {'old_path': '.', 'new_name': 'oops'}), ('copy', {'source_path': 'link', 'target_dir': ''}), ('move', {'source_path': 'inside', 'target_dir': 'link'}), ('create-folder', {'name': '..'}), ('save-last-path', {'lastPath': '../bad'})]:
            await self.post(route, data, status=400)
        self.assertNotIn('link', {x['name'] for x in (await self.get('browse', mode='all'))['items']})
        # A copy must not follow an escaping link nested in a directory either.
        (self.base / 'inside/nested').symlink_to(outside)
        (self.base / 'destination').mkdir()
        await self.post('copy', {'source_path': 'inside', 'target_dir': 'destination'}, status=400)
        self.assertEqual((outside / 'secret.txt').read_text(), 'outside')

    async def test_companion_conflicts_never_overwrite(self):
        self.write('a.json', json.dumps(WORKFLOW))
        self.write('a.webp', b'RIFFcover')
        self.write('b.webp', b'RIFFkeep')
        await self.post('rename', dict(old_path='a.json', new_name='b'), status=409)
        self.assertTrue((self.base / 'a.json').exists())
        await self.post('rename', dict(old_path='a.json', new_name='c'))
        self.assertTrue((self.base / 'c.json').exists())
        self.assertTrue((self.base / 'c.webp').exists())
        self.write('config.json', '{"normal": true}')
        self.write('config.png', b'untouched')
        await self.post('delete', dict(path='config.json'))
        self.assertTrue((self.base / 'config.png').exists())

    async def test_persist_preferences_and_legacy_default(self):
        await self.post('save-view-mode', dict(fileMode='all', viewMode='grid'))
        await self.post('save-last-path', dict(lastPath=''))
        self.write('text.md', 'hello')
        result = await self.get('browse')
        self.assertEqual(result['config']['fileMode'], 'all')
        self.assertEqual(result['items'], [])  # omitted mode stays backwards compatible

    async def test_upload_json_only_and_no_overwrite(self):
        async def upload(name, data, status):
            form = FormData()
            form.add_field('target_dir', '')
            form.add_field('workflow_files', data, filename=name)
            response = await self.client.post('/workflow-manager/upload-workflow', data=form)
            self.assertEqual(response.status, status, await response.text())
        await upload('notes.md', b'text', 400)
        await upload('../escape.json', b'{}', 400)
        await upload('ok.JSON', b'{}', 200)
        await upload('ok.JSON', b'{"overwrite": true}', 409)
        self.assertEqual((self.base / 'ok.JSON').read_bytes(), b'{}')
        await upload('broken.json', b'{', 400)


if __name__ == '__main__':
    unittest.main()

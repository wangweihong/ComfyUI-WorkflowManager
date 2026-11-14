"""ComfyUI Workflow Manager: workflows and accompanying local files."""

import asyncio
import functools
import json
import logging
import os
from pathlib import Path
import shutil
from urllib.parse import unquote

from aiohttp import web
import folder_paths
from server import PromptServer

from .file_browser import (
    PREVIEW_EXTENSIONS, browse, companion_files, is_safe_path, preview_file,
    read_workflow as parse_workflow, resolve_path, validate_name, validate_tree,
)

WEB_DIRECTORY = './js'
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__version__ = '1.1.0-local'


def get_workflows_directory():
    return os.path.join(folder_paths.get_user_directory(), 'default', 'workflows')


def ensure_workflows_directory():
    directory = get_workflows_directory()
    os.makedirs(directory, exist_ok=True)
    return directory


def get_config_path():
    return os.path.join(os.path.dirname(__file__), '.workflow_manager_config.json')


def load_config():
    defaults = dict(viewMode='list', sortBy='name', sortOrder='asc', fileMode='json')
    try:
        with open(get_config_path(), encoding='utf-8') as source:
            return {**defaults, **json.load(source)}
    except (OSError, ValueError):
        return defaults


def save_config(config):
    target = Path(get_config_path())
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)
    return True


def endpoint(handler):
    @functools.wraps(handler)
    async def wrapped(request):
        try:
            return await handler(request)
        except (ValueError, TypeError) as error:
            return web.json_response(dict(success=False, error=str(error)), status=400)
        except FileNotFoundError as error:
            return web.json_response(dict(success=False, error=str(error)), status=404)
        except FileExistsError as error:
            return web.json_response(dict(success=False, error=str(error)), status=409)
        except PermissionError as error:
            return web.json_response(dict(success=False, error=str(error)), status=403)
        except OSError as error:
            logging.exception('Workflow Manager filesystem operation failed')
            return web.json_response(dict(success=False, error=str(error)), status=500)
    return wrapped


def path_for(relative='', *, allow_root=False):
    return resolve_path(ensure_workflows_directory(), relative, allow_root=allow_root)


def require_file(relative):
    path = path_for(relative)
    if not path.is_file():
        raise FileNotFoundError('文件不存在')
    return path


def require_absent(path):
    if os.path.lexists(path):
        raise FileExistsError('目标位置已存在同名项目')


@PromptServer.instance.routes.post('/workflow-manager/save-view-mode')
@endpoint
async def save_view_mode(request):
    data = await request.json()
    config = load_config()
    for key, choices in (('viewMode', ('list', 'grid')), ('fileMode', ('json', 'all'))):
        if key in data:
            if data[key] not in choices:
                raise ValueError('无效的显示模式')
            config[key] = data[key]
    save_config(config)
    return web.json_response(dict(success=True))


@PromptServer.instance.routes.post('/workflow-manager/save-last-path')
@endpoint
async def save_last_path(request):
    data = await request.json()
    relative = data.get('lastPath', '')
    path_for(relative, allow_root=True)
    config = load_config()
    config['lastPath'] = relative
    save_config(config)
    return web.json_response(dict(success=True))


@PromptServer.instance.routes.get('/workflow-manager/browse')
@endpoint
async def browse_directory(request):
    relative = request.query.get('path', '')
    mode = request.query.get('mode', 'json')
    items = await asyncio.to_thread(browse, ensure_workflows_directory(), relative, mode)
    return web.json_response(dict(success=True, current_path=relative, items=items, config=load_config()))


@PromptServer.instance.routes.get('/workflow-manager/read-file')
@endpoint
async def read_file(request):
    path = require_file(request.query.get('path', ''))
    info = await asyncio.to_thread(preview_file, path)
    return web.json_response(dict(success=True, **info))


@PromptServer.instance.routes.get('/workflow-manager/read-workflow')
@endpoint
async def read_workflow(request):
    path = require_file(request.query.get('path', ''))
    workflow = await asyncio.to_thread(parse_workflow, path)
    if workflow is None:
        return web.json_response(dict(success=False, code='not_workflow', error='此文件不是画布工作流，可作为文本预览'), status=422)
    return web.json_response(dict(success=True, workflow=workflow))


@PromptServer.instance.routes.post('/workflow-manager/create-folder')
@endpoint
async def create_folder(request):
    data = await request.json()
    name = validate_name(data.get('name', ''))
    parent = path_for(data.get('parent_path', ''), allow_root=True)
    if not parent.is_dir():
        raise FileNotFoundError('父目录不存在')
    target = path_for(os.path.relpath(parent / name, ensure_workflows_directory()))
    require_absent(target)
    target.mkdir()
    return web.json_response(dict(success=True, path=os.path.relpath(target, ensure_workflows_directory())))


def operation_paths(data, operation):
    base = ensure_workflows_directory()
    source = path_for(data.get('old_path' if operation == 'rename' else 'source_path', ''))
    if not source.exists():
        raise FileNotFoundError('源文件或目录不存在')
    if operation == 'rename':
        target = source.with_name(validate_name(data.get('new_name', '')))
        # Retain the workflow extension when the rename dialog omits it.
        if parse_workflow(source) is not None and target.suffix.lower() != '.json':
            target = target.with_name(target.name + '.json')
    else:
        directory = path_for(data.get('target_dir', ''), allow_root=True)
        if not directory.is_dir():
            raise FileNotFoundError('目标目录不存在')
        target = directory / source.name
    resolve_path(base, os.path.relpath(target, base))
    require_absent(target)
    if source.is_dir() and is_safe_path(source, target):
        raise ValueError('不能将目录放入自身或其子目录')
    if operation == 'copy':
        validate_tree(base, source)
    companions = companion_files(base, source) if data.get('sync_preview', True) else []
    pairs = [(p, target.with_suffix(p.suffix)) for p in companions]
    for _, destination in pairs:
        resolve_path(base, os.path.relpath(destination, base))
        require_absent(destination)
    return source, target, pairs


def operate(data, operation):
    source, target, pairs = operation_paths(data, operation)
    pairs = [(source, target), *pairs]
    completed = []
    try:
        for src, dest in pairs:
            if operation == 'copy':
                if src.is_dir():
                    shutil.copytree(src, dest, symlinks=True)
                else:
                    # Exclusive creation prevents silently overwriting a new target.
                    with open(src, 'rb') as reader, open(dest, 'xb') as writer:
                        shutil.copyfileobj(reader, writer)
            else:
                require_absent(dest)
                shutil.move(str(src), str(dest))
            completed.append((src, dest))
    except OSError:
        for src, dest in reversed(completed):
            if operation == 'copy':
                shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
            else:
                shutil.move(str(dest), str(src))
        raise
    return os.path.relpath(target, ensure_workflows_directory()).replace(os.sep, '/')


@PromptServer.instance.routes.post('/workflow-manager/rename')
@endpoint
async def rename_item(request):
    data = await request.json()
    return web.json_response(dict(success=True, new_path=await asyncio.to_thread(operate, data, 'rename')))


@PromptServer.instance.routes.post('/workflow-manager/move')
@endpoint
async def move_item(request):
    data = await request.json()
    return web.json_response(dict(success=True, new_path=await asyncio.to_thread(operate, data, 'move')))


@PromptServer.instance.routes.post('/workflow-manager/copy')
@endpoint
async def copy_item(request):
    data = await request.json()
    return web.json_response(dict(success=True, new_path=await asyncio.to_thread(operate, data, 'copy')))


def remove_item(data):
    path = path_for(data.get('path', ''))
    if not path.exists():
        raise FileNotFoundError('文件或目录不存在')
    companions = companion_files(ensure_workflows_directory(), path) if data.get('sync_preview', True) else []
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    for companion in companions:
        companion.unlink()


@PromptServer.instance.routes.post('/workflow-manager/delete')
@endpoint
async def delete_item(request):
    await asyncio.to_thread(remove_item, await request.json())
    return web.json_response(dict(success=True))


@PromptServer.instance.routes.get('/workflow-manager/preview')
@endpoint
async def get_workflow_preview(request):
    path = require_file(request.query.get('path', ''))
    companions = await asyncio.to_thread(companion_files, ensure_workflows_directory(), path)
    if not companions:
        raise FileNotFoundError('Preview not found')
    return web.FileResponse(companions[0], headers={'Cache-Control': 'no-store'})


@PromptServer.instance.routes.post('/workflow-manager/upload-preview')
@endpoint
async def upload_workflow_preview(request):
    data = await request.post()
    path = require_file(data.get('workflow_path', ''))
    if await asyncio.to_thread(parse_workflow, path) is None:
        raise ValueError('仅画布工作流支持封面')
    upload = data.get('preview_file')
    if not isinstance(upload, web.FileField):
        raise ValueError('缺少封面文件')
    target = path.with_suffix('.webp')
    resolve_path(ensure_workflows_directory(), os.path.relpath(target, ensure_workflows_directory()))
    # Cover replacement is an explicit action of the existing preview UI.
    with open(target, 'wb') as writer:
        shutil.copyfileobj(upload.file, writer)
    return web.json_response(dict(success=True))


@PromptServer.instance.routes.post('/workflow-manager/upload-workflow')
@endpoint
async def upload_workflow_file(request):
    reader = await request.multipart()
    uploads = []
    relative = ''
    create_dirs = False
    async for field in reader:
        if field.name == 'target_dir':
            relative = (await field.read()).decode('utf-8')
        elif field.name == 'create_dirs':
            create_dirs = (await field.read()).decode('utf-8').lower() == 'true'
        elif field.name == 'workflow_files' and field.filename:
            name = validate_name(unquote(field.filename))
            if not name.lower().endswith('.json'):
                raise ValueError('只支持导入 .json 文件')
            content = await field.read()
            json.loads(content.decode('utf-8-sig'))
            uploads.append((name, content))
    if not uploads:
        raise ValueError('没有有效的 JSON 文件')
    directory = path_for(relative, allow_root=True)
    if create_dirs:
        directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise FileNotFoundError('目标目录不存在')
    targets = [path_for(os.path.relpath(directory / name, ensure_workflows_directory())) for name, _ in uploads]
    if len(set(targets)) != len(targets):
        raise FileExistsError('上传文件重名')
    for target in targets:
        require_absent(target)
    written = []
    try:
        for target, (_, content) in zip(targets, uploads):
            with open(target, 'xb') as writer:
                written.append(target)
                writer.write(content)
    except OSError:
        for target in written:
            target.unlink()
        raise
    return web.json_response(dict(success=True, uploaded=len(written), errors=[],
                                 message=f'成功上传 {len(written)} 个 JSON 文件',
                                 uploaded_files=[dict(filename=p.name, path=os.path.relpath(p, ensure_workflows_directory())) for p in written]))


print(f'ComfyUI Workflow Manager v{__version__} loaded')

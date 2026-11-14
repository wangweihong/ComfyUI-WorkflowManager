"""Filesystem helpers shared by the ComfyUI routes and standalone tests."""

import codecs
import json
import mimetypes
import os
from pathlib import Path

PREVIEW_LIMIT = 1024 * 1024
PREVIEW_EXTENSIONS = ('.webp', '.png', '.jpg', '.jpeg', '.gif', '.bmp')


def is_safe_path(base, target):
    try:
        base = os.path.realpath(base)
        return os.path.commonpath((base, os.path.realpath(target))) == base
    except (ValueError, OSError):
        return False


def resolve_path(base, relative='', *, allow_root=False):
    if not isinstance(relative, str) or '\x00' in relative or os.path.isabs(relative):
        raise ValueError('无效的路径')
    target = Path(os.path.abspath(os.path.join(base, relative)))
    if not is_safe_path(base, target):
        raise ValueError('路径超出工作流目录')
    if not allow_root and target.resolve() == Path(base).resolve():
        raise ValueError('不能操作工作流根目录')
    return target


def validate_name(name):
    if not isinstance(name, str) or not name.strip() or name in ('.', '..'):
        raise ValueError('名称不能为空或为 . / ..')
    if any(c in name for c in '<>:"/\\|?*\x00'):
        raise ValueError('名称包含非法字符')
    return name


def is_workflow(data):
    # Both LiteGraph 0.4 and ComfyUI 1.x serialize nodes and links as arrays.
    # Missing custom nodes are still valid: availability is checked by ComfyUI.
    return (
        isinstance(data, dict)
        and isinstance(data.get('nodes'), list)
        and isinstance(data.get('links'), list)
        and all(isinstance(n, dict) and isinstance(n.get('id'), (int, str))
                and isinstance(n.get('type'), str) for n in data['nodes'])
        and all(isinstance(link, (list, dict)) for link in data['links'])
    )


def read_workflow(path):
    if Path(path).suffix.lower() != '.json':
        return None
    try:
        with open(path, encoding='utf-8-sig') as source:
            data = json.load(source)
        return data if is_workflow(data) else None
    except (ValueError, UnicodeError, OSError):
        return None


def file_info(path):
    path = Path(path)
    stat = path.stat()
    return dict(name=path.name, size=stat.st_size, modified=stat.st_mtime,
                extension=path.suffix.lower(),
                mime=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')


def preview_file(path):
    info = file_info(path)
    with open(path, 'rb') as source:
        raw = source.read(PREVIEW_LIMIT)
    truncated = info['size'] > PREVIEW_LIMIT
    info.update(kind='binary', truncated=truncated)
    # Known media/archive signatures must never be decoded as GB18030 text.
    binary_signatures = (b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'RIFF', b'PK\x03\x04',
                         b'%PDF-', b'\x1f\x8b', b'\x7fELF', b'ID3', b'fLaC', b'OggS')
    if (b'\x00' in raw or raw.startswith(binary_signatures)
            or info['mime'].split('/')[0] in ('image', 'audio', 'video')):
        return info
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            # final=False tolerates a multi-byte character cut at the byte limit.
            decoder = codecs.getincrementaldecoder(encoding)(errors='strict')
            content = decoder.decode(raw, final=not truncated)
        except UnicodeError:
            continue
        if any(ord(c) < 32 and c not in '\t\r\n\f' for c in content):
            return info
        return {**info, 'kind': 'text', 'encoding': encoding, 'content': content}
    return info


def browse(base, relative='', mode='json'):
    if mode not in ('json', 'all'):
        raise ValueError('无效的显示模式')
    directory = resolve_path(base, relative, allow_root=True)
    if not directory.is_dir():
        raise FileNotFoundError('目录不存在')
    items = []
    for path in sorted(directory.iterdir()):
        if not is_safe_path(base, path) or not path.exists():
            continue
        is_dir = path.is_dir()
        is_json = path.suffix.lower() == '.json'
        if not is_dir and (not path.is_file() or (mode == 'json' and not is_json)):
            continue
        try:
            info = file_info(path)
            info.update(path=path.relative_to(base).as_posix(),
                        type='directory' if is_dir else 'workflow' if is_json else 'file')
            if is_dir:
                entries = [p for p in path.iterdir() if is_safe_path(base, p) and p.is_file()]
                info.update(size=0, workflow_count=sum(p.suffix.lower() == '.json' for p in entries),
                            file_count=len(entries))
            items.append(info)
        except OSError:
            continue
    return items


def validate_tree(base, path):
    """Don't follow escaping links while copying a whole directory."""
    if Path(path).is_dir():
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                if not is_safe_path(base, os.path.join(root, name)):
                    raise ValueError('目录中存在指向工作流目录外的符号链接')


def companion_files(base, path):
    if not Path(path).is_file() or read_workflow(path) is None:
        return []
    companions = [Path(path).with_suffix(ext) for ext in PREVIEW_EXTENSIONS]
    return [p for p in companions if p.is_file() and is_safe_path(base, p)]

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import struct
import time
from urllib.parse import urljoin, urlsplit

import aiohttp
from safetensors import safe_open, SafetensorError


EXTENSIONS = {'.safetensors', '.gguf', '.ckpt', '.pt', '.pth', '.bin', '.onnx', '.sft'}
ALIASES = {'clip': 'text_encoders', 'unet': 'diffusion_models'}
ACTIVE = {'queued', 'downloading', 'verifying'}
MAX_BYTES = 120 * 1024 ** 3


def strong_etag(value):
    # If-Range requires a strong quoted entity tag, never a weak tag or a date
    # guessed from unvalidated download metadata.
    if isinstance(value, str) and re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', value):
        return value
    return None


def validate_url(url):
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError('模型下载地址无效。')
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    trusted = host in {'huggingface.co', 'hf.co', 'github.com', 'raw.githubusercontent.com',
                       'objects.githubusercontent.com', 'release-assets.githubusercontent.com'}
    trusted = trusted or host.endswith('.huggingface.co') or host.endswith('.hf.co')
    if parsed.scheme != 'https' or not trusted or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('暂只支持 Hugging Face 和 GitHub 的 HTTPS 模型链接。')
    return url


def validate_name(name):
    if not isinstance(name, str) or not name or len(name) > 220:
        raise ValueError('模型文件名无效。')
    normalized = name.replace('\\', '/')
    parts = normalized.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if any(p in ('', '.', '..') or re.search(r'[<>:"|?*\x00-\x1f]', p) or p.endswith((' ', '.'))
           or p.split('.')[0].upper() in reserved for p in parts):
        raise ValueError('模型文件名包含不允许的路径。')
    if PurePosixPath(normalized).suffix.lower() not in EXTENSIONS:
        raise ValueError('该链接不是支持的模型权重文件。')
    return normalized


def verify_file(path, model, total=None, digest=None):
    size = path.stat().st_size
    if size < 16 or (total is not None and size != total):
        raise ValueError('下载文件不完整，请重试。')
    if model.get('hash'):
        if digest is None:
            hasher = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        if digest.lower() != model['hash'].lower():
            raise ValueError('模型 SHA256 校验失败，未加入模型库。')
    extension = Path(model['name']).suffix.lower()
    if extension in {'.safetensors', '.sft'}:
        with path.open('rb') as stream:
            length = struct.unpack('<Q', stream.read(8))[0]
        if length < 2 or length > min(size - 8, 100_000_000):
            raise ValueError('文件不是完整的 safetensors 模型。')
        with safe_open(str(path), framework='pt', device='cpu') as weights:
            if not weights.keys():
                raise ValueError('模型文件没有权重。')
    elif extension == '.gguf':
        with path.open('rb') as stream:
            if stream.read(4) != b'GGUF':
                raise ValueError('文件不是 GGUF 模型。')
    else:
        with path.open('rb') as stream:
            prefix = stream.read(64).lstrip().lower()
        if prefix.startswith((b'<!doctype', b'<html', b'{"error', b'version https://git-lfs')):
            raise ValueError('下载地址返回了网页或错误信息，未加入模型库。')


class ModelDownloads:
    def __init__(self, roots, central, history, on_complete=None):
        self.central = Path(central).resolve()
        self.roots = {key: [Path(p).resolve() for p in paths] for key, paths in roots.items()}
        self.history = Path(history)
        self.on_complete = on_complete
        self.jobs = {}
        self.tasks = {}
        self.lock = asyncio.Semaphore(1)
        if self.history.is_file():
            for job in json.loads(self.history.read_text(encoding='utf-8')):
                if job['state'] in ACTIVE:
                    job.update(state='cancelled', error='服务已重启，点击重试继续下载。')
                self.jobs[job['id']] = job

    def save(self):
        self.history.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history.with_suffix('.tmp')
        temporary.write_text(json.dumps(list(self.jobs.values()), ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.history)

    def prepare(self, value):
        if not isinstance(value, dict):
            raise ValueError('模型信息无效。')
        name = validate_name(value.get('name'))
        url = validate_url(value.get('url'))
        category = value.get('directory')
        if not isinstance(category, str):
            raise ValueError('模型没有指定分类。')
        category = ALIASES.get(category, category)
        if category in {'custom_nodes', 'configs', 'datasets'} or category not in self.roots:
            raise ValueError('此模型分类尚未配置到统一目录。')
        roots = self.roots[category]
        central_roots = [p for p in roots if p.is_relative_to(self.central)]
        if not central_roots:
            raise ValueError('此模型分类没有统一保存目录。')
        # Prefer the matching category leaf rather than an alias such as unet.
        preferred = next((p for p in central_roots if p == self.central / category / 'comfyui'), central_roots[0])
        destination = (preferred / name).resolve()
        if not destination.is_relative_to(preferred):
            raise ValueError('模型目标路径越出了分类目录。')
        model = {'name': name, 'directory': category, 'url': url}
        if value.get('hash'):
            hash_type = str(value.get('hash_type', value.get('hashType', 'sha256'))).lower().replace('-', '')
            if hash_type != 'sha256' or not re.fullmatch('[0-9a-fA-F]{64}', str(value['hash'])):
                raise ValueError('模型校验信息无效或不是 SHA256。')
            model.update(hash=value['hash'].lower(), hash_type='sha256')
        existing = next(((p / name).resolve() for p in roots if (p / name).is_file()
                         and (p / name).resolve().is_relative_to(self.central)), None)
        return model, destination, existing

    def enqueue(self, values):
        if not isinstance(values, list) or not 1 <= len(values) <= 32:
            raise ValueError('每次可下载 1 至 32 个模型。')
        prepared = [self.prepare(value) for value in values]
        if sum(j['state'] in ACTIVE for j in self.jobs.values()) + len(prepared) > 32:
            raise ValueError('下载队列已满，请等待现有任务完成。')
        candidates = {}
        for model, target, existing in prepared:
            key = hashlib.sha256(str(target).lower().encode()).hexdigest()[:24]
            old = self.jobs.get(key)
            other = candidates.get(key)
            if (old and old['state'] in ACTIVE and old['model'] != model) or (other and other != model):
                raise ValueError('同名模型有不同下载来源，请分别确认。')
            candidates[key] = model
        added = []
        for model, target, existing in prepared:
            key = hashlib.sha256(str(target).lower().encode()).hexdigest()[:24]
            old = self.jobs.get(key)
            if old and old['state'] in ACTIVE:
                if old['model'] != model:
                    raise ValueError('同名模型已有另一个来源正在下载。')
                added.append(old)
                continue
            job = {'id': key, 'name': model['name'], 'directory': model['directory'], 'model': model,
                   'state': 'queued', 'downloaded': 0, 'total': None, 'error': '',
                   'destination': str(existing or target), 'updated': time.time()}
            self.jobs[key] = job
            self.tasks[key] = asyncio.create_task(self.run(job, target, existing))
            added.append(job)
        # Retain recent history without allowing repeated requests to grow it indefinitely.
        removable = sorted((j for j in self.jobs.values() if j['state'] not in ACTIVE), key=lambda j: j['updated'])
        for job in removable[:max(0, len(self.jobs) - 64)]:
            self.jobs.pop(job['id'], None)
        self.save()
        return added

    def cancel(self, key):
        task = self.tasks.get(key)
        job = self.jobs.get(key)
        if job and job['state'] in ACTIVE and task:
            task.cancel()
            job.update(state='cancelled', error='已暂停，点击重试可继续下载。', updated=time.time())
            self.save()
        return job

    async def request(self, session, url, headers):
        for _ in range(8):
            validate_url(url)
            response = await session.get(url, headers=headers, allow_redirects=False)
            if response.status not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get('Location')
            response.release()
            if not location:
                raise ValueError('下载服务器未提供有效跳转地址。')
            url = urljoin(url, location)
        raise ValueError('下载地址跳转次数过多。')

    async def transfer(self, job, target):
        model = job['model']
        partial = target.with_name(target.name + '.partial')
        metadata_path = target.with_name(target.name + '.partial.json')
        for path in (partial, metadata_path):
            if path.resolve().parent != target.parent or path.is_symlink():
                raise ValueError('临时下载路径无效。')
        target.parent.mkdir(parents=True, exist_ok=True)
        metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.is_file() else {}
        if partial.exists() and metadata.get('model') != model:
            raise ValueError('存在来源不同的同名临时文件，请先检查下载目录。')
        offset = partial.stat().st_size if partial.exists() else 0
        etag = strong_etag(metadata.get('etag'))
        complete_partial = offset and offset == metadata.get('total')
        if complete_partial and model.get('hash'):
            job.update(downloaded=offset, total=offset)
            await self.finish(job, target, partial, metadata_path, offset)
            return
        # A complete partial can be reused offline only against an expected
        # SHA256. Otherwise restart to avoid HTTP 416 and stale mutable URLs.
        # An incomplete partial needs a hash or strong ETag to prevent mixing
        # two structurally valid safetensors versions after the URL changes.
        if complete_partial or (offset and not model.get('hash') and not etag):
            offset = 0
        headers = {'Accept-Encoding': 'identity', 'User-Agent': 'ComfyUI-Workflow-Model-Download/1.0'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
            if etag:
                headers['If-Range'] = etag
        hasher = hashlib.sha256()
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=90)
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False, trust_env=True) as session:
            response = await self.request(session, model['url'], headers)
            async with response:
                if response.status in (401, 403):
                    raise ValueError('模型需要网站授权或登录，请先在模型网站确认访问权限。')
                if response.status == 404:
                    raise ValueError('模型文件不存在，下载链接可能已更新。')
                if response.status not in (200, 206):
                    raise ValueError(f'模型服务器返回 HTTP {response.status}，请稍后重试。')
                if response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise ValueError('下载服务器返回了不支持的压缩格式。')
                length = response.headers.get('Content-Length')
                if response.status == 206:
                    response_etag = response.headers.get('ETag')
                    if offset and etag and response_etag is not None and response_etag != etag:
                        # These are our validated staging files. Discard the
                        # incompatible prefix so a retry starts a fresh request.
                        partial.unlink(missing_ok=True)
                        metadata_path.unlink(missing_ok=True)
                        raise ValueError('模型来源已变化，服务器未正确处理续传。点击重试可重新完整下载。')
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match or int(match[1]) != offset:
                        raise ValueError('续传位置不匹配，请重试。')
                    total = int(match[3])
                    if int(match[2]) + 1 != total:
                        raise ValueError('下载服务器只提供了部分文件，请重试。')
                else:
                    offset = 0
                    total = int(length) if length else None
                if total is not None and (total < 16 or total > MAX_BYTES):
                    raise ValueError('模型文件大小异常。')
                if total and shutil.disk_usage(target.parent).free < total - offset + 512 * 1024 ** 2:
                    raise ValueError('统一模型目录所在磁盘空间不足。')
                metadata = {'model': model, 'total': total, 'etag': response.headers.get('ETag')}
                metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
                job.update(state='downloading', downloaded=offset, total=total)
                self.save()
                if offset:
                    with partial.open('rb') as previous:
                        while chunk := previous.read(4 * 1024 * 1024):
                            hasher.update(chunk)
                            await asyncio.sleep(0)
                with partial.open('ab' if offset else 'wb') as output:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if job['downloaded'] + len(chunk) > (total or MAX_BYTES):
                            raise ValueError('下载数据超过声明的文件大小。')
                        output.write(chunk)
                        hasher.update(chunk)
                        job['downloaded'] += len(chunk)
                        job['updated'] = time.time()
                    output.flush()
                    os.fsync(output.fileno())
        await self.finish(job, target, partial, metadata_path, total or job['downloaded'], hasher.hexdigest())

    async def finish(self, job, target, partial, metadata_path, total, digest=None):
        job.update(state='verifying', total=total)
        self.save()
        try:
            await asyncio.to_thread(verify_file, partial, job['model'], total, digest)
        except (ValueError, SafetensorError):
            # These are this downloader's incomplete files, never an existing model.
            partial.unlink()
            metadata_path.unlink(missing_ok=True)
            raise ValueError('模型文件校验失败，未加入模型库。点击重试可重新下载。') from None
        if not target.resolve().is_relative_to(self.central):
            raise ValueError('保存目录已发生变化，请重试。')
        # Create the final name exclusively: a concurrent download must never be overwritten.
        os.link(partial, target)
        partial.unlink()
        metadata_path.unlink(missing_ok=True)

    async def run(self, job, target, existing):
        try:
            async with self.lock:
                if existing:
                    job.update(state='verifying', downloaded=existing.stat().st_size, total=existing.stat().st_size)
                    await asyncio.to_thread(verify_file, existing, job['model'])
                else:
                    await self.transfer(job, target)
                job.update(state='completed', error='')
                if self.on_complete:
                    self.on_complete(job['directory'])
        except asyncio.CancelledError:
            job.update(state='cancelled', error='已暂停，点击重试可继续下载。')
        except (ValueError, SafetensorError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as error:
            if isinstance(error, (aiohttp.ClientConnectorCertificateError, ssl.SSLCertVerificationError)):
                message = 'TLS 证书验证失败。请检查系统时间及代理、证书配置后重试。'
            elif isinstance(error, asyncio.TimeoutError):
                message = '连接或读取超时。请检查 ComfyUI 所在电脑的网络和代理后重试。'
            elif isinstance(error, (aiohttp.ClientProxyConnectionError, aiohttp.ClientHttpProxyError)):
                message = '无法连接代理或代理拒绝连接。请检查 ComfyUI 所用代理后重试。'
            elif isinstance(error, (aiohttp.ClientSSLError, ssl.SSLError)):
                message = 'TLS 连接失败。请检查网络或代理的 HTTPS 连接后重试。'
            elif isinstance(error, (aiohttp.ClientConnectionError, ConnectionError)):
                message = '连接被中断或无法连接模型服务器。请检查网络和代理后重试。'
            elif isinstance(error, ValueError):
                message = str(error)
            elif isinstance(error, OSError) and error.errno == 28:
                message = '磁盘空间不足。'
            elif isinstance(error, FileExistsError):
                message = '同名文件已存在，未覆盖。请刷新检查。'
            else:
                message = '下载或校验失败，点击重试继续；若反复失败，请检查网络和模型链接。'
            job.update(state='error', error=message)
        finally:
            job['updated'] = time.time()
            self.save()

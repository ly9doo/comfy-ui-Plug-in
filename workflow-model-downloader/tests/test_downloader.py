import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import ssl
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('workflow_downloader_tested', Path(__file__).resolve().parents[1] / 'downloader.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def weights():
    header = json.dumps({'x': {'dtype': 'F32', 'shape': [1], 'data_offsets': [0, 4]}}).encode()
    header += b' ' * (-len(header) % 8)
    return struct.pack('<Q', len(header)) + header + struct.pack('<f', 1.0)


class Response:
    def __init__(self, data=b'', status=200, headers=None, wait=None):
        self.data = data
        self.status = status
        self.headers = {'Content-Length': str(len(data)), **(headers or {})}
        self.content = self
        self.wait = wait
        self.released = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.release()

    def release(self):
        self.released = True

    async def iter_chunked(self, size):
        if self.wait:
            await self.wait.wait()
        yield self.data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError('Unexpected extra network request')
        return self.responses.pop(0)


class DownloaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='workflow-download-test-')
        self.base = Path(self.tmp.name)
        self.central = self.base / 'models'
        self.root = self.central / 'checkpoints' / 'comfyui'
        self.root.mkdir(parents=True)
        self.completed = []
        self.manager = module.ModelDownloads({'checkpoints': [self.root]}, self.central,
                                             self.base / 'history.json', self.completed.append)
        self.data = weights()
        self.model = {'name': 'sample.safetensors', 'directory': 'checkpoints',
                      'url': 'https://huggingface.co/example/repo/resolve/main/sample.safetensors'}

    async def asyncTearDown(self):
        for task in self.manager.tasks.values():
            if not task.done():
                task.cancel()
        if self.manager.tasks:
            await asyncio.gather(*self.manager.tasks.values(), return_exceptions=True)
        self.tmp.cleanup()

    async def execute(self, response, model=None):
        session = Session(response if isinstance(response, list) else [response])
        with patch.object(module.aiohttp, 'ClientSession', return_value=session):
            job = self.manager.enqueue([model or self.model])[0]
            await self.manager.tasks[job['id']]
        return job, session

    def partial(self, data, model=None, etag='"v1"', total=None):
        prepared, target, _ = self.manager.prepare(model or self.model)
        target.with_name(target.name + '.partial').write_bytes(data)
        target.with_name(target.name + '.partial.json').write_text(json.dumps({
            'model': prepared, 'total': len(self.data) if total is None else total, 'etag': etag}))
        return target

    def test_reject_escaping_paths_and_untrusted_urls(self):
        for name in ('../escape.safetensors', 'a/../../escape.safetensors', 'C:\\escape.safetensors',
                     'x.safetensors:stream', '/root.safetensors', 'NUL.safetensors', 'a./x.safetensors',
                     'a//x.safetensors', 'x.exe', 'x.safetensors '):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.manager.prepare({**self.model, 'name': name})
        for url in ('http://huggingface.co/a', 'https://huggingface.co.evil.test/a', 'https://127.0.0.1/a',
                    'https://user:pass@huggingface.co/a', 'https://huggingface.co:8443/a', 'file:///tmp/x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.manager.prepare({**self.model, 'url': url})
        with self.assertRaises(ValueError):
            self.manager.prepare({**self.model, 'directory': 'custom_nodes'})

    async def test_success_publishes_exact_verified_bytes_and_refreshes(self):
        job, _ = await self.execute(Response(self.data), {**self.model, 'hash': hashlib.sha256(self.data).hexdigest()})
        self.assertEqual(job['state'], 'completed')
        self.assertEqual((self.root / self.model['name']).read_bytes(), self.data)
        self.assertEqual(self.completed, ['checkpoints'])
        self.assertFalse((self.root / (self.model['name'] + '.partial')).exists())

    async def test_bad_hash_or_short_body_never_publishes(self):
        job, _ = await self.execute(Response(self.data), {**self.model, 'hash': '0' * 64})
        self.assertEqual(job['state'], 'error')
        self.assertFalse((self.root / self.model['name']).exists())
        other = {**self.model, 'name': 'short.safetensors'}
        job, _ = await self.execute(Response(self.data[:-4], headers={'Content-Length': str(len(self.data))}), other)
        self.assertEqual(job['state'], 'error')
        self.assertFalse((self.root / other['name']).exists())
        self.assertEqual(self.completed, [])

    async def test_corrupt_safetensor_is_reported_as_error(self):
        malformed = struct.pack('<Q', 32) + b'not valid JSON'.ljust(32, b' ') + b'xxxx'
        job, _ = await self.execute(Response(malformed))
        self.assertEqual(job['state'], 'error')
        self.assertFalse((self.root / self.model['name']).exists())

    async def test_existing_model_is_verified_without_network_or_overwrite(self):
        target = self.root / self.model['name']
        target.write_bytes(self.data)
        job, session = await self.execute([])
        self.assertEqual(job['state'], 'completed')
        self.assertEqual(session.calls, [])
        self.assertEqual(target.read_bytes(), self.data)

    async def test_valid_range_resume_and_range_ignored_restart(self):
        target = self.partial(self.data[:24])
        job, session = await self.execute(Response(self.data[24:], status=206,
            headers={'Content-Range': f'bytes 24-{len(self.data)-1}/{len(self.data)}'}))
        self.assertEqual(job['state'], 'completed')
        self.assertEqual(session.calls[0][1]['headers']['Range'], 'bytes=24-')
        self.assertEqual(session.calls[0][1]['headers']['If-Range'], '"v1"')
        self.assertEqual(target.read_bytes(), self.data)
        second = {**self.model, 'name': 'restart.safetensors'}
        target = self.partial(self.data[:24], second)
        job, _ = await self.execute(Response(self.data), second)
        self.assertEqual(job['state'], 'completed')
        self.assertEqual(target.read_bytes(), self.data)

    async def test_wrong_range_cannot_publish(self):
        target = self.partial(self.data[:24])
        job, _ = await self.execute(Response(self.data[16:], status=206,
            headers={'Content-Range': f'bytes 16-{len(self.data)-1}/{len(self.data)}'}))
        self.assertEqual(job['state'], 'error')
        self.assertFalse(target.exists())

    async def test_complete_partial_is_verified_without_http_range_416(self):
        model = {**self.model, 'hash': hashlib.sha256(self.data).hexdigest()}
        target = self.partial(self.data, model)
        job, session = await self.execute([], model)
        self.assertEqual(job['state'], 'completed')
        self.assertEqual(session.calls, [])
        self.assertEqual(target.read_bytes(), self.data)

    async def test_unidentified_or_weak_etag_partial_restarts_without_mixing_weights(self):
        # Both old and new files are valid with identical layout and size. A
        # structure-only validator cannot detect a hybrid of these two versions.
        header = json.dumps({'x': {'dtype': 'F32', 'shape': [2], 'data_offsets': [0, 8]}}).encode()
        header += b' ' * (-len(header) % 8)
        prefix = struct.pack('<Q', len(header)) + header
        old = prefix + struct.pack('<2f', 1, 2)
        current = prefix + struct.pack('<2f', 3, 4)
        for index, etag in enumerate((None, 'W/"v1"', 'unquoted', '"bad\r\ntag"')):
            with self.subTest(etag=etag):
                model = {**self.model, 'name': f'changed-{index}.safetensors'}
                target = self.partial(old[:-4], model, etag=etag, total=len(old))
                job, session = await self.execute(Response(current), model)
                self.assertEqual(job['state'], 'completed')
                self.assertNotIn('Range', session.calls[0][1]['headers'])
                self.assertNotIn('If-Range', session.calls[0][1]['headers'])
                self.assertEqual(target.read_bytes(), current)

    async def test_expected_hash_allows_resume_without_etag(self):
        model = {**self.model, 'hash': hashlib.sha256(self.data).hexdigest()}
        target = self.partial(self.data[:24], model, etag=None)
        job, session = await self.execute(Response(self.data[24:], status=206,
            headers={'Content-Range': f'bytes 24-{len(self.data)-1}/{len(self.data)}'}), model)
        self.assertEqual(job['state'], 'completed')
        self.assertEqual(session.calls[0][1]['headers']['Range'], 'bytes=24-')
        self.assertNotIn('If-Range', session.calls[0][1]['headers'])
        self.assertEqual(target.read_bytes(), self.data)

    async def test_changed_etag_on_partial_response_cannot_publish(self):
        target = self.partial(self.data[:24])
        job, session = await self.execute(Response(self.data[24:], status=206,
            headers={'Content-Range': f'bytes 24-{len(self.data)-1}/{len(self.data)}', 'ETag': '"v2"'}))
        self.assertEqual(job['state'], 'error')
        self.assertEqual(session.calls[0][1]['headers']['If-Range'], '"v1"')
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name(target.name + '.partial').exists())
        job, session = await self.execute(Response(self.data, headers={'ETag': '"v2"'}))
        self.assertEqual(job['state'], 'completed')
        self.assertNotIn('Range', session.calls[0][1]['headers'])

    async def test_complete_partial_without_expected_hash_downloads_again(self):
        for index, etag in enumerate((None, '"v1"', 'W/"v1"')):
            with self.subTest(etag=etag):
                model = {**self.model, 'name': f'complete-{index}.safetensors'}
                target = self.partial(self.data, model, etag=etag)
                current = self.data[:-4] + struct.pack('<f', 2.0)
                job, session = await self.execute(Response(current), model)
                self.assertEqual(job['state'], 'completed')
                self.assertEqual(len(session.calls), 1)
                self.assertNotIn('Range', session.calls[0][1]['headers'])
                self.assertEqual(target.read_bytes(), current)

    async def test_failed_integrity_can_retry_with_clean_download(self):
        bad = struct.pack('<Q', 32) + b'not valid JSON'.ljust(32, b' ') + b'xxxx'
        job, _ = await self.execute(Response(bad))
        self.assertEqual(job['state'], 'error')
        self.assertFalse((self.root / (self.model['name'] + '.partial')).exists())
        job, session = await self.execute(Response(self.data))
        self.assertEqual(job['state'], 'completed')
        self.assertNotIn('Range', session.calls[0][1]['headers'])

    async def test_concurrent_final_file_is_never_overwritten(self):
        prepared, target, existing = self.manager.prepare(self.model)
        target.write_bytes(b'concurrent user-owned data')
        job = {'id': 'race', 'model': prepared, 'directory': 'checkpoints',
               'state': 'queued', 'downloaded': 0, 'total': None}
        self.manager.jobs[job['id']] = job
        session = Session([Response(self.data)])
        with patch.object(module.aiohttp, 'ClientSession', return_value=session):
            await self.manager.run(job, target, existing)
        self.assertEqual(job['state'], 'error')
        self.assertEqual(target.read_bytes(), b'concurrent user-owned data')
        self.assertEqual(self.completed, [])

    async def test_untrusted_redirect_is_not_followed(self):
        response = Response(status=302, headers={'Location': 'http://127.0.0.1/private'})
        job, session = await self.execute(response)
        self.assertEqual(job['state'], 'error')
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(response.released)

    async def test_duplicate_targets_are_deduplicated_and_conflicts_atomic(self):
        blocker = asyncio.Event()
        session = Session([Response(self.data, wait=blocker)])
        with patch.object(module.aiohttp, 'ClientSession', return_value=session):
            jobs = self.manager.enqueue([self.model, self.model])
            self.assertEqual(jobs[0]['id'], jobs[1]['id'])
            self.assertEqual(len(self.manager.tasks), 1)
            self.manager.cancel(jobs[0]['id'])
            await asyncio.gather(*self.manager.tasks.values(), return_exceptions=True)
        self.manager.jobs.clear()
        self.manager.tasks.clear()
        with self.assertRaises(ValueError):
            self.manager.enqueue([self.model, {**self.model, 'url': 'https://github.com/owner/repo/other.safetensors'}])
        self.assertEqual(self.manager.jobs, {})
        self.assertEqual(self.manager.tasks, {})

    async def test_cancel_does_not_publish_and_history_survives_restart(self):
        blocker = asyncio.Event()
        session = Session([Response(self.data, wait=blocker)])
        with patch.object(module.aiohttp, 'ClientSession', return_value=session):
            job = self.manager.enqueue([self.model])[0]
            for _ in range(20):
                if job['state'] == 'downloading':
                    break
                await asyncio.sleep(0)
            self.manager.cancel(job['id'])
            await self.manager.tasks[job['id']]
        self.assertEqual(job['state'], 'cancelled')
        self.assertFalse((self.root / self.model['name']).exists())
        restarted = module.ModelDownloads({'checkpoints': [self.root]}, self.central, self.base / 'history.json')
        self.assertEqual(restarted.jobs[job['id']]['state'], 'cancelled')

    async def assert_failure_message(self, errors, expected):
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch.object(self.manager, 'transfer', side_effect=error):
                    job = self.manager.enqueue([self.model])[0]
                    await self.manager.tasks[job['id']]
                self.assertEqual(job['state'], 'error')
                self.assertIn(expected, job['error'])
                self.assertNotIn('secret-token', job['error'])
                self.assertNotIn('https://', job['error'])
                self.assertFalse((self.root / self.model['name']).exists())
                self.assertEqual(self.completed, [])

    async def test_timeouts_report_network_timeout_without_remote_details(self):
        await self.assert_failure_message([
            asyncio.TimeoutError('https://cdn.example/file?token=secret-token'),
            module.aiohttp.ServerTimeoutError('https://cdn.example/file?token=secret-token'),
        ], '连接或读取超时')

    async def test_proxy_failures_report_proxy_without_remote_details(self):
        key = SimpleNamespace(host='proxy.example', port=7890, ssl=True)
        await self.assert_failure_message([
            module.aiohttp.ClientProxyConnectionError(key, ConnectionRefusedError(111, 'secret-token')),
            module.aiohttp.ClientHttpProxyError(
                SimpleNamespace(real_url='https://cdn.example/file?token=secret-token'), (),
                status=502, message='secret-token'),
        ], '无法连接代理或代理拒绝连接')

    async def test_certificate_and_tls_errors_have_distinct_safe_messages(self):
        key = SimpleNamespace(host='cdn.example', port=443, ssl=True, is_ssl=True)
        await self.assert_failure_message([
            module.aiohttp.ClientConnectorCertificateError(
                key, ssl.SSLCertVerificationError(1, 'secret-token')),
            ssl.SSLCertVerificationError(1, 'secret-token'),
        ], 'TLS 证书验证失败')
        await self.assert_failure_message([
            module.aiohttp.ClientConnectorSSLError(key, ssl.SSLError(1, 'secret-token')),
            ssl.SSLError(1, 'secret-token'),
        ], 'TLS 连接失败')

    async def test_connection_reset_and_connector_errors_hide_remote_details(self):
        key = SimpleNamespace(host='cdn.example', port=443, ssl=True)
        await self.assert_failure_message([
            module.aiohttp.ClientConnectorError(key, ConnectionResetError(104, 'secret-token')),
            module.aiohttp.ClientOSError(104, 'secret-token'),
            ConnectionResetError(104, 'https://cdn.example/file?token=secret-token'),
            module.aiohttp.ServerDisconnectedError('secret-token'),
        ], '连接被中断或无法连接模型服务器')

    async def test_existing_validation_disk_and_file_conflict_messages_are_preserved(self):
        await self.assert_failure_message([ValueError('模型文件不完整，请重试。')], '模型文件不完整，请重试。')
        await self.assert_failure_message([OSError(28, 'disk full')], '磁盘空间不足。')
        await self.assert_failure_message([FileExistsError('already exists')], '同名文件已存在，未覆盖。')


if __name__ == '__main__':
    unittest.main(verbosity=2)

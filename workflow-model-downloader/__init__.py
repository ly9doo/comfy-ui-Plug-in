from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
import folder_paths
from server import PromptServer

from .downloader import ModelDownloads
from .config import load_model_root

WEB_DIRECTORY = './web'
NODE_CLASS_MAPPINGS = {}


def refresh_models(category):
    folder_paths.filename_list_cache.pop(folder_paths.map_legacy(category), None)
    PromptServer.instance.model_file_manager.clear_cache()


downloads = ModelDownloads(
    {name: folder_paths.get_folder_paths(name) for name in folder_paths.folder_names_and_paths},
    load_model_root(Path(__file__).parent),
    Path(folder_paths.get_user_directory()) / '__workflow_model_downloads' / 'jobs.json',
    refresh_models,
)
routes = PromptServer.instance.routes


def check_write(request):
    if request.content_type != 'application/json' or request.headers.get('X-Workflow-Model-Download') != '1':
        raise web.HTTPForbidden(text='请求必须来自 ComfyUI 模型下载界面。')
    origin = request.headers.get('Origin')
    if origin and urlsplit(origin).netloc.lower() != request.host.lower():
        raise web.HTTPForbidden(text='不接受跨网站下载请求。')
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        raise web.HTTPForbidden(text='不接受跨网站下载请求。')


@routes.get('/workflow-model-download/jobs')
async def list_jobs(request):
    return web.json_response({'jobs': list(downloads.jobs.values()), 'version': '1.0'})


@routes.post('/workflow-model-download/jobs')
async def add_jobs(request):
    check_write(request)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('下载请求无效。')
        jobs = downloads.enqueue(data.get('models'))
        return web.json_response({'jobs': jobs})
    except (ValueError, KeyError, TypeError) as error:
        return web.json_response({'error': str(error)}, status=400)


@routes.post('/workflow-model-download/jobs/{job_id}/cancel')
async def cancel_job(request):
    check_write(request)
    job = downloads.cancel(request.match_info['job_id'])
    if job is None:
        raise web.HTTPNotFound()
    return web.json_response({'job': job})

"""Local settings, kept separate from version-controlled plugin code."""

import json
from pathlib import Path


def load_model_root(plugin_directory):
    config_path = Path(plugin_directory) / 'config.json'
    config = {}
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as error:
            raise ValueError('无法读取插件 config.json，请检查 JSON 格式和文件权限。') from error
        if not isinstance(config, dict) or set(config) - {'central_model_root'}:
            raise ValueError('config.json 只接受 central_model_root 字段。')
    value = config.get('central_model_root', 'D:/ai_models/image_models')
    if not isinstance(value, str) or not value.strip():
        raise ValueError('central_model_root 必须是统一模型库的绝对路径。')
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ValueError('central_model_root 必须是绝对路径；非 Windows 环境请创建 config.json。')
    return root.resolve()

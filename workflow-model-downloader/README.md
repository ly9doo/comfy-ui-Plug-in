# 工作流缺失模型下载器

将 ComfyUI 原生“缺失模型”面板中的“下载”和“全部下载”接到服务端后台。模型直接存入已配置的统一模型库，完成后自动刷新当前工作流的模型列表和缺失检查。

合集中的功能目录为 `workflow-model-downloader`，建议安装目录名为 `ComfyUI-WorkflowModelDownload`。

## 功能

- 点击原生单个“下载”或“全部下载”即可提交后台任务，左下角面板显示排队、下载和校验进度。
- 根据工作流中的模型分类保存，支持分类内的合法子目录。
- 支持取消、重试及条件允许时的断点续传；关闭网页不影响已经提交的后台任务。
- 相同任务去重，已有同名模型先检查，绝不覆盖现有文件。
- 下载先写入临时文件，完成校验后才加入模型库。支持已知文件长度、可选 SHA256 和 safetensors 格式检查。
- 下载完成自动刷新模型列表，不自动运行工作流。打开或切换工作流本身也不会开始下载。

插件独立安装，不替换 ComfyUI 前端或 ComfyUI-Manager 的文件，也不调整 Manager 的安全级别。Manager 的 Model Manager／Install 功能可以继续使用。

## 兼容性与依赖

| 项目 | 已验证版本 |
|---|---|
| ComfyUI 核心 | 0.43.0 |
| ComfyUI 前端 | 1.57.4 |
| 共存环境中的 ComfyUI-Manager | 4.2.2 |

后端使用 ComfyUI 环境已有的 `aiohttp` 和 `safetensors`，通常不需要额外安装依赖。Manager 不是这个插件的必需依赖。

前端接入依赖上述版本的缺失模型界面和刷新接口，其他版本尚未逐一验证。升级 ComfyUI 前端后，如果按钮或面板行为变化，应重新检查兼容性。模型架构兼容性仍由 ComfyUI、节点和工作流决定。

## 手动安装

**本仓库是插件合集，不要把整个合集仓库直接克隆到 `ComfyUI/custom_nodes` 后当成单个插件使用。**

1. 下载本仓库 ZIP，或克隆到 `custom_nodes` 以外的位置。
2. 把仓库中的 `workflow-model-downloader` 整个子目录复制到 `ComfyUI/custom_nodes`，并命名为 `ComfyUI-WorkflowModelDownload`。
3. 按下一节配置统一模型目录。
4. 重新启动 ComfyUI。已有网页先保存工作流，再刷新页面。

安装后应具有以下结构，避免多套一层合集目录：

```text
ComfyUI/
└─ custom_nodes/
   └─ ComfyUI-WorkflowModelDownload/
      ├─ __init__.py
      ├─ downloader.py
      ├─ config.py
      ├─ config.example.json
      ├─ config.json          # 本地配置，自行创建
      └─ web/
         └─ bridge.js
```

## 配置统一模型目录

插件需要两处配置相互对应：插件知道统一模型库的根目录，ComfyUI 已经注册该根目录下的模型分类。

### 1. 插件配置

在插件目录中，将 `config.example.json` 复制为 `config.json`，填写本机的绝对路径：

```json
{
  "central_model_root": "D:/ai_models/image_models"
}
```

Windows 路径建议使用 `/`。Windows 上没有 `config.json` 时默认使用 `D:/ai_models/image_models`；其他系统必须在 `config.json` 中填写自己的绝对路径。目前只接受 `central_model_root` 这个配置字段，未知字段会报错。`config.json` 是本地配置，不应提交到仓库；更新插件时请保留它。

### 2. ComfyUI 模型分类配置

ComfyUI 的 `extra_model_paths.yaml` 必须已经包含上述根目录下的分类。下面是示例，可合并到现有文件中；已有配置正确时无需重复添加，也不要覆盖其他模型路径。

```yaml
workflow_shared_models:
  base_path: D:/ai_models/image_models
  is_default: true
  checkpoints: checkpoints/comfyui
  loras: loras/comfyui
  vae: vae/comfyui
  diffusion_models: diffusion_models/comfyui
  text_encoders: text_encoders/comfyui
  clip_vision: clip_vision/comfyui
  controlnet: controlnet/shared
  upscale_models: upscale_models/comfyui
  embeddings: embeddings/comfyui
```

保存配置后重启 ComfyUI。插件仅接受已经注册且位于统一根目录内的模型分类，不会自行搬移现有模型、创建跨软件共享规则或修改 `extra_model_paths.yaml`。

| 工作流分类 | 处理方式 |
|---|---|
| `checkpoints`、`loras`、`vae` 等 | 使用该分类在统一根目录中的配置路径；优先选择 `<分类>/comfyui` |
| `unet` | 对应 `diffusion_models` |
| `clip` | 对应 `text_encoders` |
| `controlnet` | 使用已配置的统一分类路径，例如 `controlnet/shared` |

如果同名模型已存在于该分类的其他已配置共享路径中，会先检查现有文件。模型库共享只共享存储，不会让 SD WebUI 自动支持 FLUX、Wan 等其他架构。

## 使用

1. 加载工作流，打开原生缺失模型面板。
2. 点击某个模型的“下载”，或点击“全部下载”。后者处理当前面板中可下载的缺失项。
3. 在左下角“模型下载”面板查看状态。任务在服务端依次执行，ComfyUI 服务需保持运行。
4. 文件完成并通过检查后，插件自动刷新当前工作流的模型下拉列表和缺失状态。检查模型选择后，再手动运行工作流。

工作流必须提供模型名称、分类和下载 URL。插件优先使用前端缺失模型扫描结果，也可读取工作流及节点中的模型元数据；仅有文件名时不能推断真实下载来源。来源或校验信息冲突时会显示错误，需先核对工作流。

“复制 URL”、需要授权的模型网页入口等其他原生操作保持原有用途。

## 来源、续传与限制

- 支持 Hugging Face、GitHub 及允许范围内相关下载域名的 HTTPS 文件直链；重定向后的地址也会检查。仓库首页、普通模型介绍页和其他网盘地址不能直接当作模型文件使用。
- 支持 `.safetensors`、`.sft`、`.gguf`、`.ckpt`、`.pt`、`.pth`、`.bin`、`.onnx`。每批最多 32 个模型，单文件上限 120 GiB。
- 下载临时数据使用目标文件旁的 `.partial` 和 `.partial.json`。它们不会被当作正式模型；不要将它们手动改名为权重文件。
- 点击“取消”暂停，点击“重试”继续。续传要求服务器支持 Range，且有强 ETag 文件标识或工作流提供的期望 SHA256；无法确认文件身份、只有弱 ETag 或服务器忽略续传时，会重新下载。已收齐的临时文件只有在提供 SHA256 时才离线复用，否则重新下载。完整性校验失败的本次临时数据会清理，重试时重新下载。
- 服务重启后，未完成任务保留在历史中，需要手动点击重试。任务记录位于 ComfyUI 用户目录的 `__workflow_model_downloads/jobs.json`。
- 需要登录、接受许可或访问授权的文件可能返回 401／403。插件不会继承浏览器登录状态，也不提供网站登录或令牌管理界面；应使用服务端能够访问的有效模型文件链接。
- 下载使用运行 ComfyUI 的机器的网络环境。网络、代理或链接错误需要在服务端排查；此前浏览器中的未完成下载不会自动接管。
- 下载成功不等于模型来源、生成质量或所有工作流兼容性已得到验证。除 safetensors、GGUF 和常见错误响应检查外，不会加载执行其他权重格式来验证其内容；只有工作流提供 SHA256 时才进行对应哈希匹配。

完成后如果缺失列表未刷新，可点击面板中的“刷新状态”重试，或保存工作流后重新打开页面。

## 已验证的行为

运行后端测试（在仓库根目录，用 ComfyUI 环境的 Python 替换下面的 `python`）：

```shell
python -m unittest discover -s workflow-model-downloader/tests -p "test_*.py" -v
```

所需包列在插件的 `requirements.txt` 中。ComfyUI 环境通常已有这些包；只有缺少依赖时才需要安装。测试使用临时目录和模拟网络响应，不下载大型模型。

使用 Node.js 18 或更高版本运行前端逻辑测试（不需要额外 npm 包）：

```shell
node workflow-model-downloader/tests/test_bridge.mjs
```

当前发布代码通过 20 项后端和配置测试，以及 9 项前端逻辑测试。

后端自动测试覆盖路径与来源限制、重定向、SHA256／长度／safetensors 校验、Range 续传、取消和重试、同目标去重、现有文件及并发文件保护、任务记录和完成回调。

原生单个“下载”和“全部下载”按钮已使用合法的小型样本验证：服务端识别统一目录中的文件后，完成状态显示正常，工作流缺失项依次消失。网络传输由模拟响应测试覆盖，并对真实 Hugging Face 链接进行了小范围 HTTP 206 连通检查。未进行整套大模型下载或遍历所有模型工作流的验收；这也不代表所有网络、代理或新前端版本均受支持。

## 更新与回退

更新前保存工作流，取消未完成下载并停止 ComfyUI。备份当前插件目录及本地 `config.json`，用合集中的新版 `workflow-model-downloader` 内容替换已安装插件，保留自己的配置，然后重启服务并刷新网页。不要把示例配置覆盖到本地配置上。

需要退回旧版时，停止服务并恢复备份的插件目录。需要完全停用时，把 `ComfyUI-WorkflowModelDownload` 移出 `custom_nodes`，再启动服务并刷新网页。已下载的模型、统一目录配置和任务记录不会因此自动删除；原生缺失模型按钮恢复前端本来的行为。

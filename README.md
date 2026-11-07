# ethan-tts

在自己的 Mac 上运行 **Qwen3-TTS** 的轻量语音工作台。灵感来自这篇[笔记](https://www.biji.com/note/share_note/AgMplzvne3OA2)，但部署方式针对 **Apple Silicon** 重新设计：不用 NVIDIA 显卡、不用 Docker，默认使用 **MLX 0.6B 4bit** 权重，以较低内存获得秒级合成。

**本仓库不包含模型权重。** 下载脚本会从 Hugging Face / hf-mirror 获取权重，模型、音频、参考录音、日志和数据库均已加入 `.gitignore`。

## 功能

- **语音工作台**：中文长文本自动切句、预设音色、方言、语气指令、采样参数、完整 WAV 合成与边生成边播放。流式试听可**暂停、继续、停止**；自动裁去各分句首尾重复的模型静音，仅保留按标点控制的拼接停顿，不削句中自然停顿。支持 **0.5×–2.0× 保音高变速**。
- **音色库**：9 个内置音色；收藏、搜索、编辑备注；自定义「文字设计声音」与「参考录音克隆」。
- **音频产物**：本地持久化，在线试听、WAV 下载、按文本/音色搜索、删除与存储统计。
- **任务队列**：后台任务状态、进度记录、取消、失败/中断重试。单任务串行，避免推理并发导致内存峰值叠加。
- **运行日志**：按级别、模块、关键词与任务筛选，查看结构化错误和模型下载记录，可清空。
- **设置与资源**：默认发音人、文本长度、切句长度、流式间隔、自动播放、模型按需下载、主动释放内存。
- **API**：FastAPI 提供完整 HTTP 接口；启动后可打开 `http://127.0.0.1:8765/docs`。
- **CLI**：面向 agent 与脚本的 `ethan-tts` 命令，支持合成、克隆、音色/产物/模型/日志管理，全部命令可输出 JSON。

## 实测性能

测试机：**Mac mini / Apple M4 / 24 GB / macOS arm64**，`mlx-audio 0.6.4`，模型 `Qwen3-TTS-12Hz-0.6B-CustomVoice-4bit`。

| 指标 | 实测 |
| --- | ---: |
| 模型加载（热缓存） | 约 **3.6 秒** |
| 流式首包延迟 | 约 **0.15–0.23 秒** |
| 10.64 秒音频的完整生成 | 约 **4.03 秒** |
| 首次请求（含加载，12.32 秒音频） | 约 **9.15 秒** |
| 常驻后 9.28 秒音频 | 约 **3.60 秒** |
| Metal 活跃内存 | 约 **1.76 GB** |
| 测试过程 Metal 峰值 | 约 **4.4–4.7 GB** |

实际结果随文本、语气、采样参数、机器负载与磁盘缓存而变化。第一次使用需先加载模型；长文本按句生成，不能把整篇文稿都理解成固定 10 秒完成。默认配置的目标是**常用短文本 10 秒级、流式试听亚秒级首包**，而不是用更高内存换极限速度。

### 声音克隆实测

用一段 **9.28 秒**的真实人声转成 M4A 上传，作为克隆参考（同时提供准确逐字稿）：

| 环节 | 实测 |
| --- | ---: |
| M4A 上传 + 解码 + 转码为 24 kHz 单声道 WAV | 成功，参考音频 9.28 秒 |
| 冷启动克隆合成（含 Base 模型加载） | 约 **7.9 秒** |
| 冷启动产物 | 4.80 秒音频 |
| 71 秒散文长文（克隆音色，分句合成） | 生成 **93.25 秒**，产物 71.34 秒 |

克隆**不是训练**：上传只做解码、转码和时长校验，音色特征在每次合成时由 Base 模型零样本提取，所以保存后立刻可用，没有“训练进度”。注意 CustomVoice（预设音色）与 Base（克隆）是两份不同权重，互不替代。

### 语速实测

Qwen3-TTS 本身**没有原生语速参数**（其 `speed` 形参在源码中标注为 *not directly supported yet*，传入不会改变时长）。本项目改为在生成后做保音高的 tempo 调整（ffmpeg `atempo`），支持 **0.5×–2.0×**。

用同一段 4.08 秒音频实测：

| 设定语速 | 输出时长 | 实际倍率 |
| --- ---: | ---: | ---: |
| 0.8× | 5.07 秒 | 0.80 |
| 1.0× | 4.08 秒 | 1.00 |
| 1.25× | 3.25 秒 | 1.26 |
| 1.5× | 2.70 秒 | 1.51 |

误差在 1% 内，频谱主峰保持在 300 Hz 附近，**音高不变**。语速调整需要 `ffmpeg`；未安装时 `speed=1.0` 仍可正常工作。

### 分句拼接与静音

Qwen3-TTS 每段独立生成时，首尾各带约 **300 ms** 静音。早期版本直接拼接，导致长文中间出现 **0.6–0.9 秒**的不自然空白（用户实测 71 秒音频中可听到多处）。现在每段只裁掉首尾边缘静音，再按标点补回停顿（逗号 0.10 秒、分号 0.20 秒、句号 0.26 秒），**句中自然停顿完全不削**。

## 快速开始

### 环境要求

- Apple Silicon Mac（M1 及更新），建议 16 GB 及以上统一内存；开发测试机为 24 GB。
- Python **3.10–3.12**，推荐 3.12。MLX 不支持 Intel Mac。
- 安装 [uv](https://docs.astral.sh/uv/)（推荐）和 `ffmpeg`（仅克隆录音转码需要）。
- 默认模型下载约 **1.6 GB**，准备约 3 GB 空闲空间。可选模型需要额外磁盘与内存。

```bash
# 1. 创建隔离环境，不修改系统 Python
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 2. 只下载默认的轻量模型
.venv/bin/python download_model.py

# 3. 启动
.venv/bin/python app.py
```

打开 **<http://127.0.0.1:8765>**。模型不在服务启动时加载，第一次合成时才加载；之后保持常驻以消除重复加载延迟。停止服务用 `Ctrl+C`。

国内网络下载超时时，可使用本机代理（脚本也会自动尝试 `127.0.0.1:7890` 和 hf-mirror）：

```bash
HTTPS_PROXY=http://127.0.0.1:7890 .venv/bin/python download_model.py
```

## 命令行（推荐给 agent 用）

CLI 默认连到已常驻的服务，所以**不会重复加载模型**，热态下几秒就能返回。服务没开时它会自动在后台拉起并等待就绪，不需要你手动先启动。

安装（独立工具环境，不污染系统 Python，也不占模型内存）：

```bash
cd /Volumes/SSDExt/code/life/ethan-tts
uv tool install --python 3.12 --editable .
# 之后任意目录都能直接用 ethan-tts
```

```bash
# 最常用：合成一句话，存成 WAV
ethan-tts say "你好，这是本地合成的语音。" -o hello.wav

# 给 agent 用：JSON 输出 + 指定音色 + 立即播放
ethan-tts say "今天先不急。" --voice 老凌的声音 --json --play

# 长文本从文件读，导出 mp3
ethan-tts say --file script.txt --format mp3 -o out.mp3

# 管道输入
echo "从标准输入来的文本。" | ethan-tts say - -o pipe.wav

# 调语速（0.5–2.0×，保持音高）
ethan-tts say "这段慢一点。" -s 0.85 -o slow.wav
ethan-tts say "这段快一点。" --speed 1.4 -o fast.wav

# 用参考录音克隆音色（逐字稿必须准确）
ethan-tts clone --audio my.m4a --text "录音里说的原话" --name 我的声音

# 用文字描述创建音色（需先下载 VoiceDesign 模型）
ethan-tts design --name 深夜电台 --instruct "成熟温柔的女声，语速偏慢"

# 管理与排查
ethan-tts voices list --json
ethan-tts artifacts list
ethan-tts artifacts get <id> -o copy.wav
ethan-tts models list
ethan-tts logs --level ERROR
ethan-tts status --json
ethan-tts serve            # 后台启动服务
ethan-tts stop             # 停止服务
```

`say --json` 返回示例，agent 可直接解析：

```json
{
  "path": "/Users/me/hello.wav",
  "artifact_id": "3c63a3b197b5",
  "audio_seconds": 4.4,
  "generation_seconds": 4.26,
  "realtime_factor": 0.97,
  "wall_seconds": 4.28
}
```

约定：成功时退出码 `0`；参数或服务错误时退出码 `2`，并在 `--json` 模式下输出 `{"ok": false, "error": "..."}`。所有命令都支持 `--base` 指向远端实例、`--no-autostart` 禁止自动拉起服务。

环境变量：`ETHAN_TTS_URL`（默认 `http://127.0.0.1:8765`）、`ETHAN_TTS_STARTUP_TIMEOUT`（自动启动等待秒数，默认 90）。

### 可选模型与三种玩法

| 玩法 | 所需权重 | 使用方式 |
| --- | --- | --- |
| 预设音色 / 方言（默认） | `custom-0.6b-4bit` | 选择内置发音人，可补充语气指令 |
| 一句话设计声音 | `design-1.7b-4bit` | 音色库 → 新建音色 → 文字设计 |
| 参考录音克隆 | `base-0.6b-4bit` | 音色库 → 新建音色 → 声音克隆 → 上传录音和准确逐字稿 |

可在管理台「系统设置 → 模型与资源」中后台下载，也可使用命令：

```bash
.venv/bin/python download_model.py --model design-1.7b-4bit
.venv/bin/python download_model.py --model base-0.6b-4bit
```

可选模型**按需下载、按需加载，切模型时释放旧模型引用**，不会为了功能齐全而默认占用三份模型内存。参考音频建议 10–20 秒、单人、清晰无背景噪声，支持 WAV/MP3/M4A/FLAC/OGG，最大 20 MB，上传后转为单声道 24 kHz WAV。

**CustomVoice 与 Base 都是 Qwen3-TTS，但属于不同权重：**前者用于官方预设音色，后者用于零样本声音克隆，不能相互替代。上传参考录音时不会训练或微调模型，只在本地完成解码、转码和时长校验；真正的音色提取发生在每次合成时，因此上传完成后不需要等待一个“训练过程”。M4A/MP4 会先写入本地临时文件再解码，避免非可寻址管道导致空 WAV。有效参考录音至少需要 3 秒。

> 请仅克隆自己或已获得明确授权的声音，并在发布合成内容时按适用法律和平台规则进行标识。

## 使用建议

- **要快**：保留默认 4bit 模型，开启流式试听；短句先行，长文按标点自动拆分。
- **要省资源**：不下载可选模型，避免同时运行其他大型模型；在设置页可主动释放模型内存。退出服务后没有推理进程残留。
- **要保存**：使用「合成音频」，WAV 会进入音频产物。「流式试听」只用于即时播放，不落盘。
- **要排队**：`POST /api/tasks` 创建后台任务；页面可管理任务。不要同时发起多个浏览器合成来追求吞吐，系统会有意串行执行。
- **克隆/设计效果不佳**：先检查对应模型是否下载、录音逐字稿是否准确，再尝试调整语气描述与温度。
- **想暂停流式试听**：点「❚❚ 暂停」挂起播放（后台仍在继续生成），「▶ 继续」接着播，暂停期间不会丢音频；「■ 停止」直接中止这次试听。
- **要调语速**：优先用 0.85×–1.3× 区间，超出后母音与呼吸节奏会开始失真；语速是生成后处理，**不增加模型耗时**，但需要 `ffmpeg`。

## API 示例

```bash
# 同步合成，保存并返回 WAV
curl -X POST http://127.0.0.1:8765/api/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"你好，这是本地语音。","voice_id":"preset:Vivian"}' \
  --output hello.wav -D headers.txt

# 流式 WAV（PCM16 / 单声道 / 24 kHz）
curl -N -X POST http://127.0.0.1:8765/api/tts/stream \
  -H 'Content-Type: application/json' \
  -d '{"text":"第一句先开始播放。后面的声音继续生成。","voice_id":"preset:Serena"}' \
  --output stream.wav

# 创建异步任务
curl -X POST http://127.0.0.1:8765/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"text":"这是一个后台任务。","voice_id":"preset:Vivian"}'

curl http://127.0.0.1:8765/api/artifacts
curl http://127.0.0.1:8765/api/logs
curl http://127.0.0.1:8765/health
```

## 本地数据与隐私

默认仅监听 `127.0.0.1`，无账号、无云端推理、无分析上报。网络仅用于 pip 依赖与 Hugging Face 模型下载。

```text
models/               # 权重，Git 忽略
data/ethan_tts.db     # 音色、设置、任务、产物元数据和结构化日志
data/artifacts/       # 生成的 WAV
data/voices/          # 用户上传并转码的参考录音
```

删除 `data/` 会清空个人设置、音色、任务、日志与生成音频；请先备份需要的内容。`models/` 可独立删除并重新下载。

**不要直接把服务暴露到公网。** 当前未加入身份认证，公网部署前必须添加认证、TLS、反向代理和请求限流。即使指定 `--host 0.0.0.0`，也不代表已完成安全部署。

## 开发与测试

```bash
.venv/bin/python -m pytest              # 离线接口测试，不加载真实模型
.venv/bin/python -m py_compile app.py config.py db.py engine.py download_model.py
node --check static/app.js             # 如果本机安装了 Node.js
```

项目由标准库 SQLite、FastAPI、MLX Audio 和原生 HTML/CSS/JavaScript 组成，无前端构建步骤、无 Node 服务、无 Docker 容器、无外部数据库。

```text
app.py                # HTTP API、请求校验与生命周期
engine.py             # 模型管理、单并发推理、切句、PCM/WAV、后台队列
db.py                 # SQLite 持久化
config.py             # 模型目录、默认音色与设置
static/               # 中文管理台（合成/音色/产物/任务/日志/设置）
download_model.py     # 可断点续传的模型下载器
tests/                # 使用假引擎的快速测试
```

## 许可与致谢

- 本项目的应用代码使用 [MIT License](LICENSE)。
- [Qwen3-TTS](https://huggingface.co/Qwen) 模型权重由其原作者以 **Apache-2.0** 提供；模型和打包工具不是同一许可对象，使用时以各模型仓库的许可文件为准。
- 推理基于 [mlx-audio](https://pypi.org/project/mlx-audio/) 和 Apple MLX。
- MLX 量化权重来自 [mlx-community](https://huggingface.co/mlx-community)。

# Long Audio ASR

一个带 GUI 的长音频/视频 ASR 转写工具。它会把长媒体文件切成带 overlap 的音频片段，逐段调用多模态模型转写，再可选调用另一个文本模型对完整转写做输出后处理。

![Long Audio ASR GUI](assets/gui-screenshot.png)

## 功能

- 支持音频输入：`.aac`, `.mp3`, `.wav`, `.m4a`, `.flac`, `.ogg`
- 支持视频输入：`.mp4`, `.mkv`, `.mov`, `.avi`, `.webm`, `.flv`, `.wmv`, `.m4v`
- 视频会自动用 `ffmpeg` 抽取音频
- 相邻音频片段支持 overlap，降低切词风险
- ASR 模型和输出后处理模型使用独立 API Key、Base URL 和模型名
- 输出 Markdown 文件
- 输出后处理支持 Prompt Preset，GUI 中可新建、编辑、复制、删除并命名多个 System Prompt
- 支持断点继续，尽量复用已完成的抽音频、切片、逐段转写和输出后处理结果
- 支持拖拽导入音视频文件

## 安装

先确保系统已安装：

- Python 3.9+
- Git
- ffmpeg，并已加入 `PATH`

Windows 下可以直接运行：

```bat
install.bat
```

安装脚本会：

- 创建 `.venv`
- 升级 `pip`
- 安装 `requirements.txt` 中的依赖
- 检查 `ffmpeg` 是否可用

## 启动

双击：

```bat
run_gui.bat
```

或在 PowerShell 中运行：

```powershell
.\.venv\Scripts\python.exe asr_gui.py
```

命令行模式：

```powershell
.\.venv\Scripts\python.exe asr_transcriber.py
```

## 配置

GUI 会把配置保存到：

```text
asr_config.json
```

这个文件包含 API Key，已经被 `.gitignore` 忽略，不要提交到 Git。

主要配置项：

```json
{
  "asr_api_key": "",
  "asr_base_url": "",
  "asr_model": "gemini-3.1-pro-preview",
  "asr_reasoning_effort": "None",
  "format_api_key": "",
  "format_base_url": "",
  "format_model": "",
  "format_reasoning_effort": "None",
  "input_file": "audio.aac",
  "output_file": "transcript.md",
  "temp_dir": "temp_audio_segments",
  "segment_length_min": 5,
  "overlap_seconds": 10,
  "enable_parallel_asr": false,
  "parallel_submit_interval_seconds": 0,
  "enable_postprocess": true,
  "postprocess_preset": "",
  "enable_resume": true,
  "clear_resume_cache": false
}
```

推荐模型使用 `gemini-3.1-pro-preview`，推荐片段长度为 `5` 分钟。较长片段可以减少分段数量和 overlap 重复，但太长会导致识别准确度下降。默认使用串行 ASR，会把上一段转写末尾提交给下一段辅助去除 overlap 重复；启用 `enable_parallel_asr` 后会并行提交各片段，并使用不依赖上下文的独立 ASR prompt。`parallel_submit_interval_seconds` 用于控制并行提交 ASR 请求时相邻请求的启动间隔，接口有 rate limit 时可调大。

## Prompt Presets

输出后处理的 System Prompt 通过 Prompt Preset 管理。GUI 中可以选择当前使用的 preset，并点击「编辑…」打开管理窗口，新建、编辑、复制、删除并命名多个 preset。

Preset 存储在：

```text
asr_presets.json
```

内置有三个 preset，分别是完全保留原文的「转写」、润色为可读连续文本的「润色」和对课程结构化处理，便于快速复习~~预习~~的「课程大纲」的。切换或编辑 preset 后，输出后处理的断点缓存会自动失效，无需手动清缓存。Preset 的 prompt 为空时会跳过输出后处理。

## 断点继续

断点缓存默认放在：

```text
temp_audio_segments\.asr_state\
```

缓存分三层：

- `media`：复用视频抽音频和音频切片
- `asr`：复用每个片段的转写结果
- `format`：复用输出后处理结果

如果中途中断，下次启动同一任务会自动复用已完成部分。GUI 中可以关闭“启用断点继续”，也可以勾选“清除本任务缓存后重新开始”。

## Git 和隐私

以下文件不会被提交：

- `.venv/`
- `asr_config.json`
- `temp_audio_segments/`
- 音频/视频文件
- 转写输出文件
- Python 缓存文件

## API 测试

测试 ASR 接口：

```powershell
.\.venv\Scripts\python.exe api_test.py asr
```

测试输出后处理接口：

```powershell
.\.venv\Scripts\python.exe api_test.py format
```

不传参数时默认测试 `format`。

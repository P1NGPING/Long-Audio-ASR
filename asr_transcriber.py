import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI
from pydub import AudioSegment


DEFAULT_CONFIG = {
    "asr_api_key": "",
    "asr_base_url": "",
    "asr_model": "",
    "format_api_key": "",
    "format_base_url": "",
    "format_model": "",
    "asr_reasoning_effort": "None",
    "format_reasoning_effort": "None",
    "input_file": "audio.aac",
    "output_file": "meeting_transcript.md",
    "temp_dir": "temp_audio_segments",
    "segment_length_min": 2,
    "overlap_seconds": 5,
    "enable_markdown_format": True,
    "enable_resume": True,
    "clear_resume_cache": False,
}

CONFIG_FILE = "asr_config.json"
AUDIO_EXTENSIONS = {".aac", ".mp3", ".wav", ".m4a", ".flac", ".ogg"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v"}
STATE_DIR_NAME = ".asr_state"
MEDIA_STATE_VERSION = "media-v1"
ASR_STATE_VERSION = "asr-v1"
FORMAT_STATE_VERSION = "format-v1"


SYSTEM_PROMPT = """
# Role
你是一个专业的 ASR 转写员。你的任务是把音频内容忠实转写成文字。

# Input Data
1. **上一段转写末尾**：用于识别当前音频开头因 overlap 产生的重复内容。
2. **当前音频片段**：待转写的音频。相邻片段之间存在少量 overlap。

# Goals
输出当前音频片段中的逐字转写文本，并尽量保证与上一段自然衔接。

# Constraints
1. **只转写，不总结**：不要概括、改写、润色或补充原音频中没有的信息。
2. **去除 overlap 重复**：如果当前音频开头与【上一段转写末尾】重复，只输出尚未转写过的新增内容。
3. **文字连贯**：保留术语、数字、专有名词；去除结巴、无意义的重复、无意义的语气词等，使得句子连贯。听不清的内容用「[听不清]」标记。
4. **格式严格**：只输出转写正文。不要输出“以下是转写”“好的”“本段内容”等任何引导语、标题、编号或解释。
5. **忠于音频**：如果音频为空，不要编造内容。

# Workflow
参考上一段转写末尾 -> 听取当前音频 -> 去掉开头重复 overlap -> 输出当前片段新增的忠实转写文本。
"""


FORMAT_PROMPT = """
# Role
你是一个专业的文本编辑。你的任务是将一段连续的转写文本整理成结构清晰的 Markdown。

# Input Data
一段完整的转写正文（所有语音片段拼接后的结果）。

# Goals
1. 根据**语义**将文本切分成逻辑段落，段落之间用空行分隔。
2. 当话题发生明显切换时，添加 `##` 二级标题概括该部分主题。
3. 当同一大话题下有子话题切换时，使用 `###` 三级标题。
4. 保持原文的措辞不变，不要改写、润色或总结。

# Constraints
1. **只做分段和加标题**：不要添加原文没有的信息、不要评论、不要总结。
2. **保留原文**：标题之下的正文必须和原文一致，只做段落切分。
3. **标题精简**：每个标题控制在 10 个字以内。
4. **格式严格**：只输出整理好的 Markdown 文本。不要输出"好的""整理如下"等任何引导语。
5. 如果全文没有明显的多话题切换，可以用少量 `##` 标题划分阶段（如开场、核心讨论、总结），不必强行拆得太碎。

# Workflow
阅读全文 -> 识别语义边界和话题切换点 -> 插入标题和段落分隔 -> 输出 Markdown 文本。
"""


@dataclass
class ModelConfig:
    api_key: str
    base_url: str
    model: str
    reasoning_effort: Optional[str] = None


def _log(message, on_log=None):
    if on_log:
        on_log(message)
    else:
        print(message)


def normalize_reasoning_effort(value):
    if value in (None, "", "None", "none"):
        return None
    return value


def make_client(model_config):
    return OpenAI(api_key=model_config.api_key, base_url=model_config.base_url)


def setup_directories(temp_dir, on_log=None):
    os.makedirs(temp_dir, exist_ok=True)
    _log(f"[Init] 已准备临时目录: {temp_dir}", on_log)


def make_dirs(path, on_log=None, fallback_path=None):
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except PermissionError as e:
        if not fallback_path:
            raise
        _log(f"[Warning] 无法访问目录，将使用备用目录: {path} -> {fallback_path}。错误: {e}", on_log)
        os.makedirs(fallback_path, exist_ok=True)
        return fallback_path


def handle_remove_readonly(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        raise


def atomic_write_text(file_path, text):
    make_dirs(os.path.dirname(os.path.abspath(file_path)))
    temp_path = f"{file_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(temp_path, file_path)


def atomic_write_json(file_path, data):
    atomic_write_text(file_path, json.dumps(data, ensure_ascii=False, indent=2))


def load_json(file_path, default=None):
    if not os.path.exists(file_path):
        return default
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def file_exists(file_path):
    try:
        return os.path.exists(file_path) and os.path.getsize(file_path) > 0
    except OSError:
        return False


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def get_input_signature(input_file):
    stat = os.stat(input_file)
    return {
        "path": os.path.abspath(input_file),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def compute_media_job_id(config):
    signature = {
        "version": MEDIA_STATE_VERSION,
        "input": get_input_signature(config["input_file"]),
        "segment_length_min": float(config["segment_length_min"]),
        "overlap_seconds": float(config["overlap_seconds"]),
    }
    return sha256_text(json.dumps(signature, ensure_ascii=False, sort_keys=True))


def compute_asr_job_id(config, media_job_id):
    signature = {
        "version": ASR_STATE_VERSION,
        "media_job_id": media_job_id,
        "asr_base_url": config["asr_base_url"],
        "asr_model": config["asr_model"],
        "asr_reasoning_effort": normalize_reasoning_effort(config.get("asr_reasoning_effort")),
    }
    return sha256_text(json.dumps(signature, ensure_ascii=False, sort_keys=True))


def compute_format_job_id(config, asr_job_id):
    signature = {
        "version": FORMAT_STATE_VERSION,
        "asr_job_id": asr_job_id,
        "enable_markdown_format": bool(config.get("enable_markdown_format", True)),
        "format_base_url": config.get("format_base_url"),
        "format_model": config.get("format_model"),
        "format_reasoning_effort": normalize_reasoning_effort(config.get("format_reasoning_effort")),
    }
    return sha256_text(json.dumps(signature, ensure_ascii=False, sort_keys=True))


def get_state_paths(config):
    state_root = os.path.join(config["temp_dir"], STATE_DIR_NAME)
    media_job_id = compute_media_job_id(config)
    asr_job_id = compute_asr_job_id(config, media_job_id)
    format_job_id = compute_format_job_id(config, asr_job_id)
    return {
        "state_root": state_root,
        "media_job_id": media_job_id,
        "asr_job_id": asr_job_id,
        "format_job_id": format_job_id,
        "media_dir": os.path.join(state_root, "media", media_job_id),
        "asr_dir": os.path.join(state_root, "asr", asr_job_id),
        "format_dir": os.path.join(state_root, "format", format_job_id),
    }


def clear_current_resume_cache(paths, on_log=None):
    for key in ("format_dir", "asr_dir", "media_dir"):
        path = paths[key]
        if os.path.exists(path):
            try:
                shutil.rmtree(path, onerror=handle_remove_readonly)
                _log(f"[Resume] 已清除缓存: {path}", on_log)
            except PermissionError as e:
                renamed_path = f"{path}.old.{int(time.time())}"
                try:
                    os.rename(path, renamed_path)
                    _log(f"[Resume] 缓存目录被占用，已改名隔离: {renamed_path}", on_log)
                except OSError:
                    _log(f"[Warning] 无法清除缓存目录，将继续复用已有缓存: {path}。错误: {e}", on_log)
            except OSError as e:
                _log(f"[Warning] 无法清除缓存目录，将继续复用已有缓存: {path}。错误: {e}", on_log)


def get_media_type(file_path):
    extension = os.path.splitext(file_path)[1].lower()
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def extract_audio_from_video(video_path, temp_dir, on_log=None):
    """使用 ffmpeg 从视频中抽取适合 ASR 的音频。"""
    output_audio = os.path.join(temp_dir, "extracted_audio.mp3")
    _log("[Video] 检测到视频文件，正在抽取音频...", on_log)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-vn",
        "-acodec",
        "libmp3lame",
        "-ar",
        "16000",
        "-ac",
        "1",
        output_audio,
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        raise RuntimeError("未找到 ffmpeg，请先安装 ffmpeg 并加入 PATH。") from e
    except subprocess.CalledProcessError as e:
        error_text = e.stderr.strip() if e.stderr else str(e)
        raise RuntimeError(f"视频抽取音频失败: {error_text}") from e

    _log(f"[Video] 音频已抽取至: {output_audio}", on_log)
    return output_audio


def prepare_audio_input(input_file, temp_dir, on_log=None):
    media_type = get_media_type(input_file)
    if media_type == "audio":
        return input_file
    if media_type == "video":
        return extract_audio_from_video(input_file, temp_dir, on_log)
    supported_audio = ", ".join(sorted(AUDIO_EXTENSIONS))
    supported_video = ", ".join(sorted(VIDEO_EXTENSIONS))
    raise ValueError(f"不支持的文件格式。支持音频: {supported_audio}; 支持视频: {supported_video}")


def prepare_audio_input_with_resume(config, paths, on_log=None):
    media_dir = make_dirs(
        paths["media_dir"],
        on_log,
        fallback_path=os.path.join(paths["state_root"], "media", f"{paths['media_job_id']}.retry.{os.getpid()}"),
    )
    paths["media_dir"] = media_dir
    media_state_path = os.path.join(media_dir, "media.json")
    media_state = load_json(media_state_path, {}) or {}
    media_type = get_media_type(config["input_file"])

    if media_type == "audio":
        audio_input = os.path.abspath(config["input_file"])
    elif media_type == "video":
        audio_input = os.path.join(media_dir, "extracted_audio.mp3")
        if file_exists(audio_input):
            _log(f"[Resume] 复用已抽取音频: {audio_input}", on_log)
        else:
            audio_input = extract_audio_from_video(config["input_file"], media_dir, on_log)
    else:
        supported_audio = ", ".join(sorted(AUDIO_EXTENSIONS))
        supported_video = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise ValueError(f"不支持的文件格式。支持音频: {supported_audio}; 支持视频: {supported_video}")

    media_state.update({
        "version": MEDIA_STATE_VERSION,
        "media_job_id": paths["media_job_id"],
        "input_file": os.path.abspath(config["input_file"]),
        "media_type": media_type,
        "audio_input": audio_input,
        "status": "audio_ready",
    })
    atomic_write_json(media_state_path, media_state)
    return audio_input


def split_audio(file_path, segment_min, overlap_seconds, temp_dir, on_log=None):
    """将音频切分为带重叠的小段"""
    _log(f"[Process] 正在加载音频文件: {file_path} ...", on_log)
    try:
        audio = AudioSegment.from_file(file_path)
    except Exception as e:
        _log(f"[Error] 无法加载音频，请检查文件路径或 ffmpeg 是否安装。错误: {e}", on_log)
        return []

    segment_length_ms = int(float(segment_min) * 60 * 1000)
    overlap_ms = int(float(overlap_seconds) * 1000)

    if segment_length_ms <= 0:
        _log("[Error] SEGMENT_LENGTH_MIN 必须大于 0。", on_log)
        return []

    if overlap_ms >= segment_length_ms:
        _log("[Error] OVERLAP_SECONDS 必须小于 SEGMENT_LENGTH_MIN 对应的秒数。", on_log)
        return []

    if overlap_ms < 0:
        _log("[Error] OVERLAP_SECONDS 不能为负数。", on_log)
        return []

    segments = []
    total_length = len(audio)
    _log(f"[Process] 音频总时长: {total_length / 1000 / 60:.2f} 分钟", on_log)

    for i, new_start_ms in enumerate(range(0, total_length, segment_length_ms)):
        start_ms = max(0, new_start_ms - overlap_ms)
        end_ms = min(new_start_ms + segment_length_ms, total_length)
        segment = audio[start_ms:end_ms]
        segment_filename = os.path.join(temp_dir, f"segment_{i + 1:03d}.mp3")
        segment.export(segment_filename, format="mp3")
        segments.append({
            "path": segment_filename,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "new_start_ms": new_start_ms,
            "new_end_ms": end_ms,
        })
        _log(
            f"  - 已生成音频片段: {segment_filename} "
            f"({start_ms / 1000:.1f}s - {end_ms / 1000:.1f}s, "
            f"新增从 {new_start_ms / 1000:.1f}s 开始)",
            on_log,
        )

    return segments


def split_audio_with_resume(audio_input, config, paths, on_log=None):
    media_dir = make_dirs(
        paths["media_dir"],
        on_log,
        fallback_path=os.path.join(paths["state_root"], "media", f"{paths['media_job_id']}.retry.{os.getpid()}"),
    )
    paths["media_dir"] = media_dir
    segments_dir = os.path.join(media_dir, "segments")
    segments_dir = make_dirs(
        segments_dir,
        on_log,
        fallback_path=os.path.join(media_dir, f"segments.retry.{os.getpid()}"),
    )
    media_state_path = os.path.join(media_dir, "media.json")
    media_state = load_json(media_state_path, {}) or {}

    segment_length_ms = int(float(config["segment_length_min"]) * 60 * 1000)
    overlap_ms = int(float(config["overlap_seconds"]) * 1000)
    if segment_length_ms <= 0:
        raise ValueError("SEGMENT_LENGTH_MIN 必须大于 0。")
    if overlap_ms < 0:
        raise ValueError("OVERLAP_SECONDS 不能为负数。")
    if overlap_ms >= segment_length_ms:
        raise ValueError("OVERLAP_SECONDS 必须小于 SEGMENT_LENGTH_MIN 对应的秒数。")

    existing_segments = media_state.get("segments") or []
    if existing_segments and all(file_exists(item["path"]) for item in existing_segments):
        _log(f"[Resume] 复用已切分音频片段: {len(existing_segments)} 个", on_log)
        return existing_segments

    _log(f"[Process] 正在加载音频文件: {audio_input} ...", on_log)
    try:
        audio = AudioSegment.from_file(audio_input)
    except Exception as e:
        _log(f"[Error] 无法加载音频，请检查文件路径或 ffmpeg 是否安装。错误: {e}", on_log)
        return []

    total_length = len(audio)
    _log(f"[Process] 音频总时长: {total_length / 1000 / 60:.2f} 分钟", on_log)
    segments = []
    for i, new_start_ms in enumerate(range(0, total_length, segment_length_ms)):
        start_ms = max(0, new_start_ms - overlap_ms)
        end_ms = min(new_start_ms + segment_length_ms, total_length)
        segment_filename = os.path.join(segments_dir, f"segment_{i + 1:03d}.mp3")
        if file_exists(segment_filename):
            _log(f"  - 复用音频片段: {segment_filename}", on_log)
        else:
            segment = audio[start_ms:end_ms]
            segment.export(segment_filename, format="mp3")
            _log(
                f"  - 已生成音频片段: {segment_filename} "
                f"({start_ms / 1000:.1f}s - {end_ms / 1000:.1f}s, "
                f"新增从 {new_start_ms / 1000:.1f}s 开始)",
                on_log,
            )
        segments.append({
            "index": i + 1,
            "path": segment_filename,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "new_start_ms": new_start_ms,
            "new_end_ms": end_ms,
        })

    media_state.update({
        "version": MEDIA_STATE_VERSION,
        "media_job_id": paths["media_job_id"],
        "input_file": os.path.abspath(config["input_file"]),
        "audio_input": audio_input,
        "segment_length_min": float(config["segment_length_min"]),
        "overlap_seconds": float(config["overlap_seconds"]),
        "segments": segments,
        "status": "segments_ready",
    })
    atomic_write_json(media_state_path, media_state)
    return segments


def encode_audio_base64(file_path):
    with open(file_path, "rb") as audio_file:
        return base64.b64encode(audio_file.read()).decode("utf-8")


def completion_params(model_config, messages):
    params = {"model": model_config.model, "messages": messages}
    reasoning_effort = normalize_reasoning_effort(model_config.reasoning_effort)
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort
    return params


def get_transcript(client, asr_config, current_audio_path, previous_transcript_tail, overlap_seconds):
    base64_audio = encode_audio_base64(current_audio_path)
    if previous_transcript_tail:
        context_str = previous_transcript_tail
    else:
        context_str = "（这是第一个片段，没有上一段转写末尾，请完整转写当前音频。）"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"【上一段转写末尾】\n{context_str}\n\n"
                        f"相邻音频片段有约 {overlap_seconds} 秒 overlap。"
                        "请转写当前音频；如果开头与上一段末尾重复，请跳过重复内容，只输出新增转写正文。"
                    ),
                },
                {
                    "type": "input_audio",
                    "input_audio": {"data": base64_audio, "format": "mp3"},
                },
            ],
        },
    ]

    completion = client.chat.completions.create(**completion_params(asr_config, messages))
    return completion.choices[0].message.content


def get_transcript_tail(transcripts, max_chars=500):
    return "\n".join(transcripts)[-max_chars:]


def remove_repeated_prefix(transcript, previous_transcript_tail, min_overlap_chars=12):
    if not transcript or not previous_transcript_tail:
        return transcript

    current = transcript.lstrip()
    max_overlap_chars = min(len(current), len(previous_transcript_tail), 200)
    for overlap_chars in range(max_overlap_chars, min_overlap_chars - 1, -1):
        if previous_transcript_tail.endswith(current[:overlap_chars]):
            return current[overlap_chars:].lstrip()

    return transcript


def format_transcript_markdown(client, format_config, raw_text, on_log=None):
    if not raw_text.strip():
        return raw_text

    _log("\n[Format] 正在对完整转写文本做语义分段和 Markdown 格式化...", on_log)
    messages = [
        {"role": "system", "content": FORMAT_PROMPT},
        {"role": "user", "content": f"以下是完整的转写文本，请整理为 Markdown：\n\n{raw_text}"},
    ]
    completion = client.chat.completions.create(**completion_params(format_config, messages))
    return completion.choices[0].message.content


def transcribe_segments_with_resume(client, asr_config, segments, config, paths, on_log=None, on_progress=None, should_stop=None):
    asr_dir = make_dirs(
        paths["asr_dir"],
        on_log,
        fallback_path=os.path.join(paths["state_root"], "asr", f"{paths['asr_job_id']}.retry.{os.getpid()}"),
    )
    paths["asr_dir"] = asr_dir
    transcripts_dir = os.path.join(asr_dir, "transcripts")
    transcripts_dir = make_dirs(
        transcripts_dir,
        on_log,
        fallback_path=os.path.join(asr_dir, f"transcripts.retry.{os.getpid()}"),
    )
    asr_state_path = os.path.join(asr_dir, "asr.json")
    raw_transcript_path = os.path.join(asr_dir, "raw_transcript.txt")
    asr_state = load_json(asr_state_path, {}) or {}

    all_transcripts = []
    total = len(segments)
    _log(f"\n[Start] 开始处理 {total} 个音频片段...\n", on_log)

    for index, segment_info in enumerate(segments):
        seq_num = index + 1
        transcript_path = os.path.join(transcripts_dir, f"segment_{seq_num:03d}.txt")
        previous_context = get_transcript_tail(all_transcripts)

        if file_exists(transcript_path):
            with open(transcript_path, "r", encoding="utf-8") as f:
                transcript = f.read().strip()
            _log(f"[Resume] 复用音频片段 {seq_num}/{total} 的转写", on_log)
        else:
            if should_stop and should_stop():
                _log("[Stop] 已停止，正在保存已完成的转写。", on_log)
                break

            _log(f"正在处理音频片段 {seq_num}/{total} ...", on_log)
            try:
                transcript = get_transcript(
                    client,
                    asr_config,
                    segment_info["path"],
                    previous_context,
                    config["overlap_seconds"],
                )
                transcript = remove_repeated_prefix(transcript, previous_context)
                atomic_write_text(transcript_path, transcript.strip() + "\n")
            except Exception as e:
                _log(f"[API Error] 音频片段 {seq_num} 转写失败，下次续跑会重试: {e}", on_log)
                break

        all_transcripts.append(transcript)
        _log(f"完成音频片段 {seq_num}/{total}", on_log)
        if on_progress:
            on_progress(seq_num, total)

        asr_state.update({
            "version": ASR_STATE_VERSION,
            "asr_job_id": paths["asr_job_id"],
            "media_job_id": paths["media_job_id"],
            "asr_base_url": config["asr_base_url"],
            "asr_model": config["asr_model"],
            "completed_segments": len([name for name in os.listdir(transcripts_dir) if name.endswith(".txt")]),
            "total_segments": total,
            "status": "asr_running",
        })
        atomic_write_json(asr_state_path, asr_state)

    raw_text = "\n".join(t.strip() for t in all_transcripts if t.strip())
    if raw_text.strip():
        atomic_write_text(raw_transcript_path, raw_text.strip() + "\n")

    completed_count = len([name for name in os.listdir(transcripts_dir) if name.endswith(".txt")])
    is_complete = completed_count == total
    asr_state.update({
        "raw_transcript_path": raw_transcript_path,
        "completed_segments": completed_count,
        "total_segments": total,
        "status": "asr_done" if is_complete else "asr_partial",
    })
    atomic_write_json(asr_state_path, asr_state)
    return raw_text, is_complete


def format_markdown_with_resume(client, format_config, raw_text, config, paths, on_log=None):
    format_dir = make_dirs(
        paths["format_dir"],
        on_log,
        fallback_path=os.path.join(paths["state_root"], "format", f"{paths['format_job_id']}.retry.{os.getpid()}"),
    )
    paths["format_dir"] = format_dir
    format_state_path = os.path.join(format_dir, "format.json")
    formatted_path = os.path.join(format_dir, "formatted_transcript.md")

    if file_exists(formatted_path):
        _log(f"[Resume] 复用已完成的 Markdown 修整结果: {formatted_path}", on_log)
        with open(formatted_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    formatted_text = format_transcript_markdown(client, format_config, raw_text, on_log)
    atomic_write_text(formatted_path, formatted_text.strip() + "\n")
    atomic_write_json(format_state_path, {
        "version": FORMAT_STATE_VERSION,
        "format_job_id": paths["format_job_id"],
        "asr_job_id": paths["asr_job_id"],
        "format_base_url": config.get("format_base_url"),
        "format_model": config.get("format_model"),
        "formatted_transcript_path": formatted_path,
        "status": "format_done",
    })
    return formatted_text


def write_final_output_with_resume(final_text, output_file, on_log=None):
    if file_exists(output_file):
        _log(f"[Resume] 输出文件已存在，将覆盖写入最新结果: {output_file}", on_log)
    atomic_write_text(output_file, final_text.strip() + "\n")


def validate_config(config):
    required = ["asr_api_key", "asr_base_url", "asr_model", "input_file", "output_file"]
    for key in required:
        if not str(config.get(key, "")).strip():
            raise ValueError(f"缺少配置: {key}")

    if config.get("enable_markdown_format", True):
        for key in ["format_api_key", "format_base_url", "format_model"]:
            if not str(config.get(key, "")).strip():
                raise ValueError(f"启用 Markdown 语义分段时必须填写 {key}")

    if not os.path.exists(config["input_file"]):
        raise FileNotFoundError(f"输入文件不存在: {config['input_file']}")

    if get_media_type(config["input_file"]) == "unknown":
        raise ValueError("输入文件必须是支持的音频或视频格式。")

    if not str(config["output_file"]).lower().endswith(".md"):
        raise ValueError("输出文件必须是 .md")


def run_asr_job(config, on_log=None, on_progress=None, should_stop=None):
    validate_config(config)
    paths = get_state_paths(config)
    use_resume = bool(config.get("enable_resume", True))

    asr_config = ModelConfig(
        api_key=config["asr_api_key"],
        base_url=config["asr_base_url"],
        model=config["asr_model"],
        reasoning_effort=config.get("asr_reasoning_effort"),
    )
    format_config = ModelConfig(
        api_key=config.get("format_api_key") or config["asr_api_key"],
        base_url=config.get("format_base_url") or config["asr_base_url"],
        model=config.get("format_model") or config["asr_model"],
        reasoning_effort=config.get("format_reasoning_effort"),
    )

    setup_directories(config["temp_dir"], on_log)
    if config.get("clear_resume_cache"):
        clear_current_resume_cache(paths, on_log)

    if use_resume:
        _log(f"[Resume] 当前任务缓存 ID: media={paths['media_job_id']}, asr={paths['asr_job_id']}, format={paths['format_job_id']}", on_log)
        audio_input = prepare_audio_input_with_resume(config, paths, on_log)
        audio_segments = split_audio_with_resume(audio_input, config, paths, on_log)
    else:
        audio_input = prepare_audio_input(config["input_file"], config["temp_dir"], on_log)
        audio_segments = split_audio(
            audio_input,
            config["segment_length_min"],
            config["overlap_seconds"],
            config["temp_dir"],
            on_log,
        )

    if not audio_segments:
        raise RuntimeError("没有生成音频片段，程序终止。")

    asr_client = make_client(asr_config)
    if use_resume:
        raw_text, asr_complete = transcribe_segments_with_resume(
            asr_client,
            asr_config,
            audio_segments,
            config,
            paths,
            on_log,
            on_progress,
            should_stop,
        )
    else:
        all_transcripts = []
        total = len(audio_segments)
        _log(f"\n[Start] 开始处理 {total} 个音频片段...\n", on_log)
        for index, segment_info in enumerate(audio_segments):
            if should_stop and should_stop():
                _log("[Stop] 已停止，正在保存已完成的转写。", on_log)
                break
            seq_num = index + 1
            _log(f"正在处理音频片段 {seq_num}/{total} ...", on_log)
            previous_context = get_transcript_tail(all_transcripts)
            try:
                transcript = get_transcript(asr_client, asr_config, segment_info["path"], previous_context, config["overlap_seconds"])
                transcript = remove_repeated_prefix(transcript, previous_context)
            except Exception as e:
                transcript = "[转写失败]"
                _log(f"[API Error] 音频片段 {seq_num} 转写失败: {e}", on_log)
            all_transcripts.append(transcript)
            _log(f"完成音频片段 {seq_num}/{total}", on_log)
            if on_progress:
                on_progress(seq_num, total)
        raw_text = "\n".join(t.strip() for t in all_transcripts if t.strip())
        asr_complete = len(all_transcripts) == len(audio_segments)

    if config.get("enable_markdown_format", True) and raw_text.strip() and asr_complete and not (should_stop and should_stop()):
        try:
            format_client = make_client(format_config)
            if use_resume:
                formatted_text = format_markdown_with_resume(format_client, format_config, raw_text, config, paths, on_log)
            else:
                formatted_text = format_transcript_markdown(format_client, format_config, raw_text, on_log)
        except Exception as e:
            _log(f"[API Error] Markdown 格式化失败，将使用原始文本: {e}", on_log)
            formatted_text = raw_text
    else:
        if raw_text.strip() and not asr_complete:
            _log("[Resume] ASR 尚未完整完成，暂不进行 Markdown 修整。", on_log)
        formatted_text = raw_text

    write_final_output_with_resume(formatted_text, config["output_file"], on_log)

    _log(f"[Done] 结果已保存至: {config['output_file']}", on_log)
    _log(f"临时音频片段文件保留在: {config['temp_dir']}", on_log)
    return config["output_file"]


def load_config(config_file=CONFIG_FILE):
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            config.update(json.load(f))
    migrate_legacy_config(config)
    return config


def migrate_legacy_config(config):
    api_key = config.get("api_key")
    base_url = config.get("base_url")
    if api_key:
        config.setdefault("asr_api_key", api_key)
        config.setdefault("format_api_key", api_key)
    if base_url:
        config.setdefault("asr_base_url", base_url)
        config.setdefault("format_base_url", base_url)
    config.pop("api_key", None)
    config.pop("base_url", None)


def main():
    run_asr_job(load_config())


if __name__ == "__main__":
    main()

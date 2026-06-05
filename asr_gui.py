import json
import os
import queue
import subprocess
import threading
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox

import customtkinter as ctk
from tkinterdnd2 import DND_FILES, TkinterDnD

from asr_transcriber import AUDIO_EXTENSIONS, DEFAULT_CONFIG, VIDEO_EXTENSIONS, migrate_legacy_config, run_asr_job


CONFIG_FILE = "asr_config.json"
FONT_FAMILY = "Microsoft YaHei UI"
FONT_TITLE = (FONT_FAMILY, 32, "bold")
FONT_SECTION = (FONT_FAMILY, 18, "bold")
FONT_BODY = (FONT_FAMILY, 13)
FONT_BUTTON = (FONT_FAMILY, 14, "bold")
FONT_LOG = (FONT_FAMILY, 12)
MEDIA_FILETYPES = (
    ("Media", "*.aac *.mp3 *.wav *.m4a *.flac *.ogg *.mp4 *.mkv *.mov *.avi *.webm *.flv *.wmv *.m4v"),
    ("Audio", "*.aac *.mp3 *.wav *.m4a *.flac *.ogg"),
    ("Video", "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.wmv *.m4v"),
    ("All files", "*.*"),
)


class DnDCTk(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)


def load_config():
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            config.update(saved)
            migrate_legacy_config(config)
        except Exception:
            pass
    return config


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


class AsrApp(DnDCTk):
    def __init__(self):
        super().__init__()
        self.title("Long Audio ASR")
        self.geometry("1100x760")
        self.minsize(980, 680)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.config_data = load_config()
        self.events = queue.Queue()
        self.worker = None
        self.stop_requested = False
        self.output_file = self.config_data.get("output_file", "meeting_transcript.md")
        self.save_after_id = None

        self.vars = {}
        self.create_widgets()
        self.load_vars(self.config_data)
        self.bind_auto_save()
        self.register_drop_target(self)
        self.after(100, self.poll_events)

    def create_widgets(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, padx=24, pady=(22, 10), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(header, text="Long Audio ASR", font=FONT_TITLE)
        title.grid(row=0, column=0, sticky="w")
        subtitle = ctk.CTkLabel(
            header,
            text="支持音频/视频输入，自动抽取音频后转写，并用独立模型整理为 Markdown 语义段落",
            text_color=("gray35", "gray70"),
            font=FONT_BODY,
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(4, 0))

        left = ctk.CTkScrollableFrame(self, label_text="配置")
        left.grid(row=1, column=0, padx=(24, 12), pady=12, sticky="nsew")
        left.grid_columnconfigure(1, weight=1)

        right = ctk.CTkFrame(self)
        right.grid(row=1, column=1, padx=(12, 24), pady=12, sticky="nsew")
        right.grid_rowconfigure(3, weight=1)
        right.grid_columnconfigure(0, weight=1)

        section1 = ctk.CTkLabel(left, text="语音识别接口", font=FONT_SECTION)
        section1.grid(row=0, column=0, columnspan=3, sticky="w", pady=(4, 8))
        self.add_entry(left, "ASR API Key", "asr_api_key", 1, show="*")
        self.add_entry(left, "ASR Base URL", "asr_base_url", 2)
        self.add_entry(left, "ASR Model", "asr_model", 3)
        self.add_option(left, "ASR Reasoning", "asr_reasoning_effort", 4)

        section2 = ctk.CTkLabel(left, text="Markdown 修整接口", font=FONT_SECTION)
        section2.grid(row=5, column=0, columnspan=3, sticky="w", pady=(24, 8))
        self.add_entry(left, "Format API Key", "format_api_key", 6, show="*")
        self.add_entry(left, "Format Base URL", "format_base_url", 7)
        self.add_entry(left, "Format Model", "format_model", 8)
        self.add_option(left, "Format Reasoning", "format_reasoning_effort", 9)
        self.vars["enable_markdown_format"] = ctk.BooleanVar(value=True)
        markdown_switch = ctk.CTkSwitch(left, text="启用 Markdown 语义分段", variable=self.vars["enable_markdown_format"], font=FONT_BODY)
        markdown_switch.grid(row=10, column=1, columnspan=2, sticky="w", pady=8)

        section3 = ctk.CTkLabel(left, text="文件", font=FONT_SECTION)
        section3.grid(row=11, column=0, columnspan=3, sticky="w", pady=(24, 8))
        self.input_file_entry = self.add_file_row(left, "输入媒体", "input_file", 12, self.choose_input_file)
        self.add_file_row(left, "输出 Markdown", "output_file", 13, self.choose_output_file)
        self.add_entry(left, "临时目录", "temp_dir", 14)

        section4 = ctk.CTkLabel(left, text="切分", font=FONT_SECTION)
        section4.grid(row=15, column=0, columnspan=3, sticky="w", pady=(24, 8))
        self.add_entry(left, "片段分钟", "segment_length_min", 16)
        self.add_entry(left, "Overlap 秒", "overlap_seconds", 17)

        section5 = ctk.CTkLabel(left, text="断点继续", font=FONT_SECTION)
        section5.grid(row=18, column=0, columnspan=3, sticky="w", pady=(24, 8))
        self.vars["enable_resume"] = ctk.BooleanVar(value=True)
        resume_switch = ctk.CTkSwitch(left, text="启用断点继续", variable=self.vars["enable_resume"], font=FONT_BODY)
        resume_switch.grid(row=19, column=1, columnspan=2, sticky="w", pady=8)
        self.vars["clear_resume_cache"] = ctk.BooleanVar(value=False)
        clear_switch = ctk.CTkSwitch(left, text="清除本任务缓存后重新开始", variable=self.vars["clear_resume_cache"], font=FONT_BODY)
        clear_switch.grid(row=20, column=1, columnspan=2, sticky="w", pady=8)

        controls = ctk.CTkFrame(right)
        controls.grid(row=0, column=0, padx=18, pady=18, sticky="ew")
        controls.grid_columnconfigure((0, 1), weight=1)
        self.start_button = ctk.CTkButton(controls, text="开始转写", height=42, command=self.start_job, font=FONT_BUTTON)
        self.start_button.grid(row=0, column=0, padx=(0, 8), pady=10, sticky="ew")
        self.stop_button = ctk.CTkButton(controls, text="停止", height=42, fg_color="#8a1f1f", hover_color="#6e1818", command=self.stop_job, state="disabled", font=FONT_BUTTON)
        self.stop_button.grid(row=0, column=1, padx=(8, 0), pady=10, sticky="ew")

        self.progress_label = ctk.CTkLabel(right, text="等待开始", anchor="w", font=FONT_BODY)
        self.progress_label.grid(row=1, column=0, padx=18, pady=(4, 0), sticky="ew")
        self.progress = ctk.CTkProgressBar(right)
        self.progress.grid(row=2, column=0, padx=18, pady=(8, 14), sticky="ew")
        self.progress.set(0)

        self.log_box = ctk.CTkTextbox(right, wrap="word", font=FONT_LOG)
        self.log_box.grid(row=3, column=0, padx=18, pady=(0, 14), sticky="nsew")
        self.log_box.insert("end", "可将音频/视频文件拖放到窗口或输入媒体框。\n")

        open_controls = ctk.CTkFrame(right)
        open_controls.grid(row=4, column=0, padx=18, pady=(0, 18), sticky="ew")
        open_controls.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(open_controls, text="打开输出文件", command=self.open_output_file, font=FONT_BUTTON).grid(row=0, column=0, padx=(0, 8), pady=10, sticky="ew")
        ctk.CTkButton(open_controls, text="打开输出目录", command=self.open_output_dir, font=FONT_BUTTON).grid(row=0, column=1, padx=(8, 0), pady=10, sticky="ew")

        self.register_drop_target(left)
        self.register_drop_target(right)
        self.register_drop_target(self.input_file_entry)
        self.register_drop_target(self.log_box)

    def add_entry(self, parent, label, key, row, show=None):
        ctk.CTkLabel(parent, text=label, font=FONT_BODY).grid(row=row, column=0, padx=(8, 14), pady=7, sticky="w")
        self.vars[key] = ctk.StringVar()
        entry = ctk.CTkEntry(parent, textvariable=self.vars[key], show=show, font=FONT_BODY)
        entry.grid(row=row, column=1, columnspan=2, padx=(0, 8), pady=7, sticky="ew")

    def add_option(self, parent, label, key, row):
        ctk.CTkLabel(parent, text=label, font=FONT_BODY).grid(row=row, column=0, padx=(8, 14), pady=7, sticky="w")
        self.vars[key] = ctk.StringVar()
        option = ctk.CTkOptionMenu(parent, variable=self.vars[key], values=["None", "low", "medium", "high"], font=FONT_BODY, dropdown_font=FONT_BODY)
        option.grid(row=row, column=1, columnspan=2, padx=(0, 8), pady=7, sticky="ew")

    def add_file_row(self, parent, label, key, row, command):
        ctk.CTkLabel(parent, text=label, font=FONT_BODY).grid(row=row, column=0, padx=(8, 14), pady=7, sticky="w")
        self.vars[key] = ctk.StringVar()
        entry = ctk.CTkEntry(parent, textvariable=self.vars[key], font=FONT_BODY)
        entry.grid(row=row, column=1, padx=(0, 8), pady=7, sticky="ew")
        ctk.CTkButton(parent, text="选择", width=72, command=command, font=FONT_BODY).grid(row=row, column=2, padx=(0, 8), pady=7)
        return entry

    def load_vars(self, config):
        for key, var in self.vars.items():
            value = config.get(key, DEFAULT_CONFIG.get(key, ""))
            if isinstance(var, ctk.BooleanVar):
                var.set(bool(value))
            else:
                var.set("None" if value is None else str(value))

    def collect_config(self, strict=True):
        config = {}
        for key, var in self.vars.items():
            value = var.get()
            if key in ("segment_length_min", "overlap_seconds"):
                try:
                    value = float(value)
                except ValueError as e:
                    if strict:
                        raise ValueError(f"{key} 必须是数字") from e
                    return None
            config[key] = value
        return config

    def bind_auto_save(self):
        for var in self.vars.values():
            var.trace_add("write", self.schedule_config_save)

    def schedule_config_save(self, *_):
        if self.save_after_id:
            self.after_cancel(self.save_after_id)
        self.save_after_id = self.after(400, self.auto_save_config)

    def auto_save_config(self):
        self.save_after_id = None
        config = self.collect_config(strict=False)
        if config is None:
            return
        try:
            save_config(config)
            self.config_data = config
        except Exception as e:
            self.log(f"[Config] 自动保存失败: {e}")

    def choose_input_file(self):
        path = filedialog.askopenfilename(
            title="选择音频或视频文件",
            filetypes=MEDIA_FILETYPES,
        )
        if path:
            self.set_input_file(path)

    def register_drop_target(self, widget):
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", self.handle_drop)

    def handle_drop(self, event):
        paths = self.tk.splitlist(event.data)
        if not paths:
            return
        self.set_input_file(paths[0])

    def set_input_file(self, path):
        path = os.path.abspath(path)
        extension = os.path.splitext(path)[1].lower()
        if extension not in AUDIO_EXTENSIONS and extension not in VIDEO_EXTENSIONS:
            messagebox.showwarning("不支持的文件", "请拖入支持的音频或视频文件。")
            return

        self.vars["input_file"].set(path)
        base = os.path.splitext(os.path.basename(path))[0]
        self.vars["output_file"].set(os.path.abspath(f"{base}_transcript.md"))
        self.log(f"[Drop] 已导入媒体文件: {path}")

    def choose_output_file(self):
        path = filedialog.asksaveasfilename(
            title="选择输出 Markdown 文件",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
        )
        if path:
            self.vars["output_file"].set(path)

    def start_job(self):
        if self.worker and self.worker.is_alive():
            return

        try:
            config = self.collect_config()
        except Exception as e:
            messagebox.showerror("配置错误", str(e))
            return
        if config is None:
            messagebox.showerror("配置错误", "配置保存失败")
            return

        self.output_file = config["output_file"]
        self.stop_requested = False
        self.progress.set(0)
        self.progress_label.configure(text="准备中")
        self.log_box.delete("1.0", "end")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        self.worker = threading.Thread(target=self.run_worker, args=(config,), daemon=True)
        self.worker.start()

    def run_worker(self, config):
        try:
            output = run_asr_job(
                config,
                on_log=lambda msg: self.events.put(("log", msg)),
                on_progress=lambda done, total: self.events.put(("progress", done, total)),
                should_stop=lambda: self.stop_requested,
            )
            self.events.put(("done", output))
        except Exception as e:
            self.events.put(("error", str(e)))

    def stop_job(self):
        self.stop_requested = True
        self.progress_label.configure(text="正在停止，等待当前请求结束")
        self.log("[Stop] 已请求停止。")

    def poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log(event[1])
                elif kind == "progress":
                    done, total = event[1], event[2]
                    self.progress.set(done / total if total else 0)
                    self.progress_label.configure(text=f"已完成 {done}/{total}")
                elif kind == "done":
                    self.output_file = event[1]
                    self.progress_label.configure(text="完成")
                    self.progress.set(1)
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.log(f"[Done] 输出文件: {self.output_file}")
                elif kind == "error":
                    self.progress_label.configure(text="失败")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.log(f"[Error] {event[1]}")
                    messagebox.showerror("运行失败", event[1])
        except queue.Empty:
            pass
        self.after(100, self.poll_events)

    def log(self, text):
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")

    def open_output_file(self):
        path = self.vars["output_file"].get() or self.output_file
        if os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showwarning("文件不存在", path)

    def open_output_dir(self):
        path = self.vars["output_file"].get() or self.output_file
        directory = os.path.dirname(os.path.abspath(path)) or os.getcwd()
        if os.path.exists(directory):
            subprocess.Popen(["explorer", directory])


if __name__ == "__main__":
    app = AsrApp()
    app.mainloop()

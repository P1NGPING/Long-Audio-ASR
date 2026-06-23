import json
import os
import queue
import subprocess
import threading
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox

import customtkinter as ctk
from tkinterdnd2 import DND_FILES, TkinterDnD

from asr_transcriber import (
    AUDIO_EXTENSIONS,
    DEFAULT_CONFIG,
    VIDEO_EXTENSIONS,
    load_presets,
    migrate_legacy_config,
    run_asr_job,
    save_presets,
)


CONFIG_FILE = "asr_config.json"
FONT_FAMILY = "Microsoft YaHei UI"
FONT_TITLE = (FONT_FAMILY, 32, "bold")
FONT_SECTION = (FONT_FAMILY, 18, "bold")
FONT_BODY = (FONT_FAMILY, 13)
FONT_BUTTON = (FONT_FAMILY, 14, "bold")
FONT_LOG = (FONT_FAMILY, 12)
PROMPT_PLACEHOLDER = "Prompt 为空时将跳过输出后处理。"
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


class PresetEditorWindow(ctk.CTkToplevel):
    def __init__(self, master, presets_data):
        super().__init__(master)
        self.title("Prompt Preset 管理")
        self.geometry("880x600")
        self.minsize(720, 520)
        self.transient(master)
        self.grab_set()

        self.master_app = master
        self.data = json.loads(json.dumps(presets_data, ensure_ascii=False))
        self.editing_index = None
        self.suppress_listbox_callback = False
        self.prompt_placeholder_visible = False

        self.grid_columnconfigure(0, weight=0, minsize=240)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self)
        left.grid(row=0, column=0, padx=(16, 8), pady=16, sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)

        self.list_frame = ctk.CTkScrollableFrame(left, label_text="Presets")
        self.list_frame.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)

        buttons = ctk.CTkFrame(left, fg_color="transparent")
        buttons.grid(row=1, column=0, padx=8, pady=(4, 8), sticky="ew")
        buttons.grid_columnconfigure((0, 1, 2), weight=1)
        ctk.CTkButton(buttons, text="新建", font=FONT_BODY, command=self.on_new).grid(row=0, column=0, padx=2, pady=4, sticky="ew")
        ctk.CTkButton(buttons, text="复制", font=FONT_BODY, command=self.on_duplicate).grid(row=0, column=1, padx=2, pady=4, sticky="ew")
        ctk.CTkButton(buttons, text="删除", font=FONT_BODY, fg_color="#8a1f1f", hover_color="#6e1818", command=self.on_delete).grid(row=0, column=2, padx=2, pady=4, sticky="ew")

        right = ctk.CTkFrame(self)
        right.grid(row=0, column=1, padx=(8, 16), pady=(16, 8), sticky="nsew")
        right.grid_columnconfigure(1, weight=1)
        right.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(right, text="名称", font=FONT_BODY).grid(row=0, column=0, padx=(10, 8), pady=(10, 6), sticky="w")
        self.name_var = ctk.StringVar()
        self.name_entry = ctk.CTkEntry(right, textvariable=self.name_var, font=FONT_BODY)
        self.name_entry.grid(row=0, column=1, padx=(0, 10), pady=(10, 6), sticky="ew")

        ctk.CTkLabel(right, text="System Prompt", font=FONT_BODY).grid(row=1, column=0, padx=(10, 8), pady=(4, 4), sticky="nw")
        self.prompt_box = ctk.CTkTextbox(right, wrap="word", font=FONT_LOG)
        self.prompt_text_color = self.prompt_box.cget("text_color")
        self.prompt_box.grid(row=2, column=0, columnspan=2, padx=(10, 10), pady=(4, 10), sticky="nsew")

        save_row = ctk.CTkFrame(self, fg_color="transparent")
        save_row.grid(row=1, column=1, padx=(8, 16), pady=(0, 16), sticky="ew")
        save_row.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(save_row, text="保存", font=FONT_BUTTON, command=self.on_save).grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkButton(save_row, text="取消", font=FONT_BUTTON, fg_color="gray", hover_color="gray30", command=self.on_close).grid(row=0, column=1, padx=(6, 0), sticky="ew")
        self.status_var = ctk.StringVar(value="")
        ctk.CTkLabel(save_row, textvariable=self.status_var, text_color=("gray45", "gray65"), font=FONT_BODY).grid(row=1, column=0, columnspan=2, pady=(6, 0), sticky="w")

        self.name_var.trace_add("write", self.on_field_edited)
        self.prompt_box.bind("<<Modified>>", self.on_prompt_modified)
        self.prompt_box.bind("<KeyPress>", self.on_prompt_keypress)
        self.prompt_box.bind("<<Paste>>", self.on_prompt_paste)
        self.prompt_box.bind("<<Cut>>", self.on_prompt_cut)

        self.render_list()
        if self.data.get("presets") and not self.select_preset(self.data.get("selected")):
            self.select_preset_by_index(0)

    def render_list(self):
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.list_buttons = {}
        for idx, item in enumerate(self.data.get("presets", [])):
            name = item.get("name", "")
            is_selected = (idx == self.editing_index)
            btn = ctk.CTkButton(
                self.list_frame,
                text=name or "(未命名)",
                anchor="w",
                font=FONT_BODY,
                command=lambda i=idx: self.select_preset_by_index(i),
                fg_color="#1f6aa8" if is_selected else None,
                hover_color="#144c7a" if is_selected else None,
            )
            btn.grid(row=idx, column=0, padx=2, pady=2, sticky="ew")
            self.list_buttons[idx] = btn

    def highlight_selected(self):
        self.render_list()

    def select_preset(self, name):
        for idx, item in enumerate(self.data.get("presets", [])):
            if item.get("name") == name:
                self.select_preset_by_index(idx)
                return True
        return False

    def select_preset_by_index(self, idx):
        presets = self.data.get("presets", [])
        if idx < 0 or idx >= len(presets):
            return
        item = presets[idx]
        self.editing_index = idx
        self.suppress_listbox_callback = True
        self.name_var.set(item.get("name", ""))
        self._set_prompt_text(item.get("prompt", ""))
        self.suppress_listbox_callback = False
        self.highlight_selected()

    def _prompt_text(self):
        if self.prompt_placeholder_visible:
            return ""
        text = self.prompt_box.get("1.0", "end-1c")
        return text if text.strip() else ""

    def _set_prompt_text(self, text):
        self.prompt_placeholder_visible = False
        self.prompt_box.configure(text_color=self.prompt_text_color)
        self.prompt_box.delete("1.0", "end")
        if text:
            self.prompt_box.insert("1.0", str(text))
        self.prompt_box.edit_modified(False)
        self._show_prompt_placeholder_if_empty()

    def _hide_prompt_placeholder(self):
        if not self.prompt_placeholder_visible:
            return
        self.suppress_listbox_callback = True
        self.prompt_placeholder_visible = False
        self.prompt_box.configure(text_color=self.prompt_text_color)
        self.prompt_box.delete("1.0", "end")
        self.prompt_box.edit_modified(False)
        self.suppress_listbox_callback = False

    def _show_prompt_placeholder_if_empty(self):
        if self.prompt_placeholder_visible:
            return
        if self.prompt_box.get("1.0", "end-1c").strip():
            return
        self.suppress_listbox_callback = True
        self.prompt_placeholder_visible = True
        self.prompt_box.configure(text_color=("gray45", "gray60"))
        self.prompt_box.delete("1.0", "end")
        self.prompt_box.insert("1.0", PROMPT_PLACEHOLDER)
        self.prompt_box.mark_set("insert", "1.0")
        self.prompt_box.edit_modified(False)
        self.suppress_listbox_callback = False

    def on_prompt_keypress(self, event):
        if self.prompt_placeholder_visible and event.keysym in ("BackSpace", "Delete"):
            return "break"
        is_text_input = (event.char and event.char.isprintable()) or event.keysym in ("Return", "KP_Enter", "Tab")
        if self.prompt_placeholder_visible and is_text_input and not (event.state & 0x0C):
            self._hide_prompt_placeholder()

    def on_prompt_paste(self, _=None):
        self._hide_prompt_placeholder()

    def on_prompt_cut(self, _=None):
        if self.prompt_placeholder_visible:
            return "break"

    def _clear_status(self):
        self.status_var.set("")

    def _editing_item(self):
        idx = self.editing_index
        if idx is None:
            return None
        presets = self.data.get("presets", [])
        if 0 <= idx < len(presets):
            return presets[idx]
        return None

    def on_field_edited(self, *_):
        if self.suppress_listbox_callback:
            return
        item = self._editing_item()
        if not item:
            return
        new_name = self.name_var.get()
        if new_name != item.get("name"):
            old_name = item.get("name")
            item["name"] = new_name
            if self.data.get("selected") == old_name:
                self.data["selected"] = new_name
            self.highlight_selected()
            self._clear_status()

    def on_prompt_modified(self, _=None):
        if self.suppress_listbox_callback:
            self.prompt_box.edit_modified(False)
            return
        item = self._editing_item()
        if not item:
            self.prompt_box.edit_modified(False)
            return
        if self.prompt_placeholder_visible:
            text = self.prompt_box.get("1.0", "end-1c")
            if text != PROMPT_PLACEHOLDER:
                text = text.replace(PROMPT_PLACEHOLDER, "", 1)
                self.suppress_listbox_callback = True
                self.prompt_placeholder_visible = False
                self.prompt_box.configure(text_color=self.prompt_text_color)
                self.prompt_box.delete("1.0", "end")
                if text:
                    self.prompt_box.insert("1.0", text)
                self.suppress_listbox_callback = False
        item["prompt"] = self._prompt_text()
        self.prompt_box.edit_modified(False)
        self._show_prompt_placeholder_if_empty()
        self._clear_status()

    def _unique_new_name(self, base):
        names = {item.get("name", "") for item in self.data.get("presets", [])}
        if base not in names:
            return base
        i = 2
        while f"{base} {i}" in names:
            i += 1
        return f"{base} {i}"

    def on_new(self):
        name = self._unique_new_name("新预设")
        item = {"name": name, "prompt": ""}
        self.data.setdefault("presets", []).append(item)
        self.render_list()
        self.select_preset_by_index(len(self.data["presets"]) - 1)
        self.name_entry.focus_set()
        self._clear_status()

    def on_duplicate(self):
        item = self._editing_item()
        if not item:
            return
        new_name = self._unique_new_name(item.get("name", "预设"))
        new_item = {"name": new_name, "prompt": item.get("prompt", "")}
        self.data.setdefault("presets", []).append(new_item)
        self.render_list()
        self.select_preset_by_index(len(self.data["presets"]) - 1)
        self._clear_status()

    def on_delete(self):
        idx = self.editing_index
        if idx is None:
            return
        presets = self.data.get("presets", [])
        if idx < 0 or idx >= len(presets):
            return
        item = presets[idx]
        name = item.get("name", "")
        if not messagebox.askyesno("确认删除", f"确定删除 preset「{name}」？", parent=self):
            return
        presets.remove(item)
        if self.data.get("selected") == name:
            self.data["selected"] = presets[0]["name"] if presets else ""
        self.editing_index = None
        self.render_list()
        if presets:
            self.select_preset_by_index(min(idx, len(presets) - 1))
        else:
            self.name_var.set("")
            self._set_prompt_text("")
        self._clear_status()

    def on_save(self):
        item = self._editing_item()
        if item:
            item["prompt"] = self._prompt_text()
        if not self.data.get("presets"):
            messagebox.showwarning("无法保存", "至少保留一个 preset。", parent=self)
            return
        for item in self.data["presets"]:
            if not item.get("name", "").strip():
                messagebox.showwarning("名称不能为空", "所有 preset 的名称必须非空。", parent=self)
                return
        seen = set()
        for item in self.data["presets"]:
            name = item.get("name", "")
            if name in seen:
                messagebox.showwarning("名称冲突", f"存在同名 preset: {name}，请先修改后再保存。", parent=self)
                return
            seen.add(name)
        if not self.data.get("selected") or not any(p.get("name") == self.data.get("selected") for p in self.data["presets"]):
            self.data["selected"] = self.data["presets"][0]["name"]
        try:
            saved_data = json.loads(json.dumps(self.data, ensure_ascii=False))
            save_presets(saved_data)
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self)
            return
        self.master_app.on_presets_saved(saved_data)
        self.status_var.set("已保存")

    def on_close(self):
        try:
            self.master_app.preset_editor = None
        except Exception:
            pass
        self.destroy()


class AsrApp(DnDCTk):
    def __init__(self):
        super().__init__()
        self.title("Long Audio ASR")
        self.geometry("1100x760")
        self.minsize(980, 680)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.config_data = load_config()
        self.presets_data = load_presets()
        self.events = queue.Queue()
        self.worker = None
        self.stop_requested = False
        self.output_file = self.config_data.get("output_file", "meeting_transcript.md")
        self.save_after_id = None

        self.vars = {}
        self.preset_editor = None
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
            text="支持音频/视频输入，自动抽取音频后转写，并用独立模型进行输出后处理",
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

        section2 = ctk.CTkLabel(left, text="输出后处理接口", font=FONT_SECTION)
        section2.grid(row=5, column=0, columnspan=3, sticky="w", pady=(24, 8))
        self.add_entry(left, "Format API Key", "format_api_key", 6, show="*")
        self.add_entry(left, "Format Base URL", "format_base_url", 7)
        self.add_entry(left, "Format Model", "format_model", 8)
        self.add_option(left, "Format Reasoning", "format_reasoning_effort", 9)
        self.vars["enable_postprocess"] = ctk.BooleanVar(value=True)
        postprocess_switch = ctk.CTkSwitch(left, text="启用输出后处理", variable=self.vars["enable_postprocess"], font=FONT_BODY)
        postprocess_switch.grid(row=10, column=1, columnspan=2, sticky="w", pady=8)
        self.add_preset_row(left, 11)

        section3 = ctk.CTkLabel(left, text="文件", font=FONT_SECTION)
        section3.grid(row=12, column=0, columnspan=3, sticky="w", pady=(24, 8))
        self.input_file_entry = self.add_file_row(left, "输入媒体", "input_file", 13, self.choose_input_file)
        self.add_file_row(left, "输出 Markdown", "output_file", 14, self.choose_output_file)
        self.add_entry(left, "临时目录", "temp_dir", 15)

        section4 = ctk.CTkLabel(left, text="切分", font=FONT_SECTION)
        section4.grid(row=16, column=0, columnspan=3, sticky="w", pady=(24, 8))
        self.add_entry(left, "片段分钟", "segment_length_min", 17)
        self.add_entry(left, "Overlap 秒", "overlap_seconds", 18)
        self.vars["enable_parallel_asr"] = ctk.BooleanVar(value=False)
        parallel_switch = ctk.CTkSwitch(left, text="启用并行 ASR", variable=self.vars["enable_parallel_asr"], font=FONT_BODY)
        parallel_switch.grid(row=19, column=1, columnspan=2, sticky="w", pady=8)
        self.parallel_interval_widgets = self.add_entry(left, "并行提交间隔秒", "parallel_submit_interval_seconds", 20)

        section5 = ctk.CTkLabel(left, text="断点继续", font=FONT_SECTION)
        section5.grid(row=21, column=0, columnspan=3, sticky="w", pady=(24, 8))
        self.vars["enable_resume"] = ctk.BooleanVar(value=True)
        resume_switch = ctk.CTkSwitch(left, text="启用断点继续", variable=self.vars["enable_resume"], font=FONT_BODY)
        resume_switch.grid(row=22, column=1, columnspan=2, sticky="w", pady=8)
        self.vars["clear_resume_cache"] = ctk.BooleanVar(value=False)
        clear_switch = ctk.CTkSwitch(left, text="清除本任务缓存后重新开始", variable=self.vars["clear_resume_cache"], font=FONT_BODY)
        clear_switch.grid(row=23, column=1, columnspan=2, sticky="w", pady=8)

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
        label_widget = ctk.CTkLabel(parent, text=label, font=FONT_BODY)
        label_widget.grid(row=row, column=0, padx=(8, 14), pady=7, sticky="w")
        self.vars[key] = ctk.StringVar()
        entry = ctk.CTkEntry(parent, textvariable=self.vars[key], show=show, font=FONT_BODY)
        entry.grid(row=row, column=1, columnspan=2, padx=(0, 8), pady=7, sticky="ew")
        return label_widget, entry

    def add_option(self, parent, label, key, row):
        ctk.CTkLabel(parent, text=label, font=FONT_BODY).grid(row=row, column=0, padx=(8, 14), pady=7, sticky="w")
        self.vars[key] = ctk.StringVar()
        option = ctk.CTkComboBox(parent, variable=self.vars[key], values=["None", "low", "medium", "high", "自定义"], font=FONT_BODY, dropdown_font=FONT_BODY)
        option.configure(command=lambda value, var=self.vars[key], widget=option: self.handle_custom_option(var, widget, value))
        option.grid(row=row, column=1, columnspan=2, padx=(0, 8), pady=7, sticky="ew")

    def handle_custom_option(self, var, widget, value):
        if value == "自定义":
            var.set("")
            widget.focus_set()

    def add_file_row(self, parent, label, key, row, command):
        ctk.CTkLabel(parent, text=label, font=FONT_BODY).grid(row=row, column=0, padx=(8, 14), pady=7, sticky="w")
        self.vars[key] = ctk.StringVar()
        entry = ctk.CTkEntry(parent, textvariable=self.vars[key], font=FONT_BODY)
        entry.grid(row=row, column=1, padx=(0, 8), pady=7, sticky="ew")
        ctk.CTkButton(parent, text="选择", width=72, command=command, font=FONT_BODY).grid(row=row, column=2, padx=(0, 8), pady=7)
        return entry

    def add_preset_row(self, parent, row):
        ctk.CTkLabel(parent, text="Preset", font=FONT_BODY).grid(row=row, column=0, padx=(8, 14), pady=7, sticky="w")
        self.vars["postprocess_preset"] = ctk.StringVar()
        self.preset_combo = ctk.CTkComboBox(
            parent,
            variable=self.vars["postprocess_preset"],
            values=self.preset_names(),
            font=FONT_BODY,
            dropdown_font=FONT_BODY,
        )
        self.preset_combo.grid(row=row, column=1, padx=(0, 8), pady=7, sticky="ew")
        ctk.CTkButton(parent, text="编辑…", width=72, command=self.open_preset_editor, font=FONT_BODY).grid(row=row, column=2, padx=(0, 8), pady=7)

    def preset_names(self):
        return [item.get("name", "") for item in self.presets_data.get("presets", [])]

    def refresh_preset_combo(self):
        names = self.preset_names()
        self.preset_combo.configure(values=names)
        current = self.vars["postprocess_preset"].get()
        if current not in names:
            new_value = self.presets_data.get("selected") or (names[0] if names else "")
            self.vars["postprocess_preset"].set(new_value)

    def open_preset_editor(self):
        if self.preset_editor is not None and self.preset_editor.winfo_exists():
            self.preset_editor.focus_set()
            self.preset_editor.lift()
            return
        selected = self.vars["postprocess_preset"].get()
        if selected:
            self.presets_data["selected"] = selected
        self.preset_editor = PresetEditorWindow(self, self.presets_data)
        self.preset_editor.protocol("WM_DELETE_WINDOW", self.preset_editor.on_close)

    def on_presets_saved(self, new_data):
        self.presets_data = new_data
        self.refresh_preset_combo()
        if self.save_after_id:
            self.after_cancel(self.save_after_id)
            self.save_after_id = None
        self.auto_save_config()

    def load_vars(self, config):
        for key, var in self.vars.items():
            value = config.get(key, DEFAULT_CONFIG.get(key, ""))
            if isinstance(var, ctk.BooleanVar):
                var.set(bool(value))
            else:
                var.set("None" if value is None else str(value))
        self.update_parallel_interval_visibility()
        if not self.vars["postprocess_preset"].get():
            self.vars["postprocess_preset"].set(self.presets_data.get("selected") or (self.preset_names()[0] if self.preset_names() else ""))
        self.refresh_preset_combo()

    def collect_config(self, strict=True):
        config = {}
        for key, var in self.vars.items():
            value = var.get()
            if key in ("segment_length_min", "overlap_seconds", "parallel_submit_interval_seconds"):
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
        self.vars["enable_parallel_asr"].trace_add("write", self.update_parallel_interval_visibility)

    def update_parallel_interval_visibility(self, *_):
        if self.vars["enable_parallel_asr"].get():
            for widget in self.parallel_interval_widgets:
                widget.grid()
        else:
            for widget in self.parallel_interval_widgets:
                widget.grid_remove()

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

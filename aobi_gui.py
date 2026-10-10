"""奥比岛修炼助手：便携 Windows UI 及隔离的游戏工作进程。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

APP_NAME = "奥比岛修炼助手"
BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
TEAMS = {"沿用记录 / 入场自动识别": None, "符文勇士队 · 左水晶": "rune", "火焰先锋队 · 右水晶": "fire"}
TEAM_NAMES = {"rune": "符文勇士队 · 左水晶", "fire": "火焰先锋队 · 右水晶", None: "等待入场分队"}


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class NamedMutex:
    def __init__(self, name):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateMutexW(None, False, "Local\\"+name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.existed = ctypes.get_last_error() == 183

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class RecordStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.mkdir(parents=True, exist_ok=True)

    def progress(self):
        return read_json(self.path/"progress.json", {"wins": 0, "round_active": False, "team": None, "next_arena": 1})

    def prepare(self, target, team=None):
        progress = self.progress()
        if not 1 <= target <= 9999:
            raise ValueError("目标胜场请输入 1 到 9999 的整数。")
        if target <= progress["wins"]:
            raise ValueError(f"已完成 {progress['wins']} 胜。请提高目标，或点击“新一轮计数”。")
        if team is not None:
            progress.update(team=team, round_active=True)
        write_json(self.path/"progress.json", progress)
        return progress

    def reset(self):
        # 历史目录与当前目录为同一受控父目录，不删除已有记录。
        history = self.path.parent/"历史记录"
        history.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")+f"-{time.time_ns()%1000000:06d}"
        destination = history/stamp
        assert destination.resolve().parent == history.resolve()
        self.path.rename(destination)
        self.path.mkdir()
        write_json(self.path/"progress.json", {"wins": 0, "round_active": False, "team": None, "next_arena": 1})
        return destination


class WorkerController:
    def __init__(self, store):
        self.store = store
        self.process = None
        self.output = None
        self.stopping = False
        self.last_exit = None

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, target, team):
        if self.running():
            raise ValueError("已有任务在运行，请先停止当前任务。")
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise ValueError("游戏以管理员身份运行。请关闭本程序，右键选择“以管理员身份运行”。")
        guard = NamedMutex("AobiTrainingAssistantWorker")
        busy = guard.existed
        guard.close()
        if busy:
            raise ValueError("已有修炼助手任务在运行，请先停止另一个任务。")
        self.store.prepare(target, team)
        # 新进程启动前清掉旧指令，但保留历史计数与证据。
        (self.store.path/"control.txt").write_text("", encoding="utf-8")
        write_json(self.store.path/"status.json", {"reason": "starting", "paused": True, "time": time.strftime("%F %T")})
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--worker"]
        else:
            command = [sys.executable, str(Path(__file__).resolve()), "--worker"]
        command += ["--data-dir", str(self.store.path), "--target", str(target)]
        self.output = (self.store.path/"process.log").open("a", encoding="utf-8", buffering=1)
        self.process = subprocess.Popen(command, cwd=str(BASE), stdin=subprocess.DEVNULL,
                                        stdout=self.output, stderr=self.output,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        self.stopping = False
        self.last_exit = None

    def command(self, value):
        if not self.running():
            return False
        if value not in ("pause", "resume", "stop"):
            raise ValueError(value)
        path = self.store.path/"control.txt"
        temp = path.with_suffix(".tmp")
        temp.write_text(value, encoding="utf-8")
        temp.replace(path)
        if value == "stop":
            self.stopping = True
        return True

    def poll(self):
        if self.process is None:
            return None
        result = self.process.poll()
        if result is not None:
            self.last_exit = result
            self.process = None
            if self.output:
                self.output.close()
                self.output = None
            self.stopping = False
        return result


def worker_main(records, target):
    records.mkdir(parents=True, exist_ok=True)
    stream = (records/"process.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = stream
    mutex = NamedMutex("AobiTrainingAssistantWorker")
    if mutex.existed:
        write_json(records/"status.json", {"paused": True, "reason": "另一个任务正在运行，请先停止。"})
        mutex.close()
        return 2
    os.environ["AOBI_RECORDS_DIR"] = str(records)
    bot = None
    try:
        progress = read_json(records/"progress.json", {})
        if progress.get("wins", 0) >= target:
            write_json(records/"status.json", {"time":time.strftime("%F %T"), "paused":True,
                       "wins":progress["wins"], "team":progress.get("team"),
                       "scene":None, "reason":"complete", "casts":0, "repair":None})
            return 0
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("请以管理员身份运行程序。")
        import aobi_bot
        args = SimpleNamespace(target=target, team=None, click_gap=.09, auto_start=True, max_casts=0)
        bot = aobi_bot.Bot(args)
        bot.run()
        return 0
    except Exception as exc:
        traceback.print_exc()
        progress = read_json(records/"progress.json", {})
        write_json(records/"status.json", {"time": time.strftime("%F %T"), "paused": True,
                                           "wins": progress.get("wins", 0), "team": progress.get("team"),
                                           "reason": str(exc)})
        return 1
    finally:
        if bot is not None:
            bot.stop.set()
        mutex.close()
        stream.flush()


class App:
    def __init__(self, root, records):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.store = RecordStore(records)
        self.controller = WorkerController(self.store)
        self.closing = False
        self.log_offset = 0
        self.preview_mtime = 0
        self.preview_photo = None
        self.target = tk.StringVar(value=str(read_json(records.parent/"settings.json", {}).get("target", 30)))
        self.team = tk.StringVar(value=list(TEAMS)[0])
        self.count = tk.StringVar(value="0")
        self.status = tk.StringVar(value="就绪")
        self.detail = tk.StringVar()
        self.current_team = tk.StringVar()
        self.cast_text = tk.StringVar(value="本局施法 0 次")
        self.viewport_text = tk.StringVar(value="游戏画面：启动后自动定位")
        self.root.title(APP_NAME)
        width = min(1030, max(740, self.root.winfo_screenwidth()-80))
        height = min(860, max(500, self.root.winfo_screenheight()-100))
        self.root.geometry(f"{width}x{height}+30+30")
        self.root.minsize(740, 500)
        self.root.configure(bg="#eef3f3")
        icon = Path(__file__).resolve().parent/"app.ico"
        if icon.exists(): self.root.iconbitmap(str(icon))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._styles()
        self._layout()
        self._append_log("界面已就绪。点击开始后才会操作游戏。")
        self.refresh()

    def _styles(self):
        ttk = self.ttk
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#eef3f3")
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background="white", foreground="#233b42", font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", foreground="#687f84", font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Count.TLabel", foreground="#087f77", font=("Segoe UI", 46, "bold"))
        style.configure("Status.TLabel", foreground="#087f77", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(10, 9), background="#edf3f3", borderwidth=0)
        style.map("TButton", background=[("active", "#dce9e8"), ("disabled", "#f1f4f4")], foreground=[("disabled", "#a6b4b5")])
        style.configure("Primary.TButton", background="#087f77", foreground="white", font=("Microsoft YaHei UI", 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#096961"), ("disabled", "#b9d7d4")], foreground=[("disabled", "#f3f8f7")])
        style.configure("Stop.TButton", foreground="#b4474b")
        style.configure("TCombobox", padding=7, font=("Microsoft YaHei UI", 10))
        style.configure("TSpinbox", padding=7, font=("Microsoft YaHei UI", 11))
        style.configure("Horizontal.TProgressbar", background="#0b998c", troughcolor="#e7f1ef", borderwidth=0, thickness=7)

    def _layout(self):
        tk, ttk = self.tk, self.ttk
        # 较小屏幕可滚动查看全部控件，避免固定高度把“停止”等按钮裁掉。
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        shell.rowconfigure(0, weight=1); shell.columnconfigure(0, weight=1)
        canvas = tk.Canvas(shell, highlightthickness=0, bg="#eef3f3")
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(shell, orient="horizontal", command=canvas.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        page = ttk.Frame(canvas)
        page_id = canvas.create_window(0, 0, window=page, anchor="nw")
        def fit(event):
            canvas.itemconfigure(page_id, width=max(960, event.width), height=max(850, event.height))
            canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.bind("<Configure>", fit)
        page.bind("<Configure>", lambda event:canvas.configure(scrollregion=canvas.bbox("all")))
        header = tk.Frame(page, bg="#143e43", padx=26, pady=19)
        header.pack(fill="x")
        tk.Label(header, text=APP_NAME, bg="#143e43", fg="white", font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w")
        tk.Label(header, text="修炼场自动施法 · 胜利记录 · 达标停止", bg="#143e43", fg="#b9d9d6", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(6, 0))
        body = ttk.Frame(page, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=330)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body, style="Card.TFrame", padding=20)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        left.columnconfigure(0, weight=1)
        ttk.Label(left, text="本轮已确认胜利", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(left, textvariable=self.count, style="Count.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 1))
        self.progress = ttk.Progressbar(left, maximum=30)
        self.progress.grid(row=2, column=0, sticky="ew", pady=(0, 17))
        ttk.Label(left, textvariable=self.status, style="Status.TLabel").grid(row=3, column=0, sticky="w")
        ttk.Label(left, textvariable=self.detail, wraplength=282, style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(7, 17))
        ttk.Separator(left).grid(row=5, column=0, sticky="ew", pady=(0, 15))
        ttk.Label(left, text="目标累计胜场", style="Section.TLabel").grid(row=6, column=0, sticky="w")
        self.target_input = ttk.Spinbox(left, from_=1, to=9999, textvariable=self.target, width=10)
        self.target_input.grid(row=7, column=0, sticky="ew", pady=(7, 13))
        ttk.Label(left, text="启动时的队伍", style="Section.TLabel").grid(row=8, column=0, sticky="w")
        self.team_input = ttk.Combobox(left, textvariable=self.team, values=list(TEAMS), state="readonly", width=23)
        self.team_input.grid(row=9, column=0, sticky="ew", pady=(7, 6))
        ttk.Label(left, text="在战场中可手动校正队伍。\n从学院进入时会自动识别分队。", style="Muted.TLabel", wraplength=282).grid(row=10, column=0, sticky="w", pady=(0, 15))
        self.start_button = ttk.Button(left, text="开始任务", style="Primary.TButton", command=self.start)
        self.start_button.grid(row=11, column=0, sticky="ew")
        buttons = ttk.Frame(left, style="Card.TFrame")
        buttons.grid(row=12, column=0, sticky="ew", pady=9)
        for col in range(3): buttons.columnconfigure(col, weight=1)
        self.pause_button = ttk.Button(buttons, text="暂停", command=lambda:self.send("pause"))
        self.pause_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.resume_button = ttk.Button(buttons, text="继续", command=lambda:self.send("resume"))
        self.resume_button.grid(row=0, column=1, sticky="ew", padx=5)
        self.stop_button = ttk.Button(buttons, text="停止", style="Stop.TButton", command=lambda:self.send("stop"))
        self.stop_button.grid(row=0, column=2, sticky="ew", padx=(5, 0))
        self.reset_button = ttk.Button(left, text="新一轮计数", command=self.reset)
        self.reset_button.grid(row=13, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(left, text="运行时 F8 暂停 / 继续，Esc 停止。\n切回游戏前台后会自动恢复因切窗导致的暂停。", style="Muted.TLabel", wraplength=282).grid(row=14, column=0, sticky="w")
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        preview = ttk.Frame(right, style="Card.TFrame", padding=16)
        preview.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(preview, text="最近游戏画面", style="Section.TLabel").pack(anchor="w")
        ttk.Label(preview, textvariable=self.current_team, style="Muted.TLabel").pack(anchor="w", pady=(4, 8))
        ttk.Label(preview, textvariable=self.viewport_text, style="Muted.TLabel").pack(anchor="w", pady=(0, 7))
        self.preview = tk.Canvas(preview, height=260, width=570, bg="#edf3f3", highlightthickness=0)
        self.preview.pack(fill="x")
        self.preview.bind("<Configure>", lambda event:self.preview.coords("frame",event.width/2,130))
        self.preview.create_text(285, 125, text="开始后显示游戏截图", fill="#71878c", font=("Microsoft YaHei UI", 11), tags="preview")
        ttk.Label(preview, textvariable=self.cast_text, style="Muted.TLabel").pack(anchor="w", pady=(7, 0))
        log_card = ttk.Frame(right, style="Card.TFrame", padding=16)
        log_card.grid(row=1, column=0, sticky="nsew")
        log_card.rowconfigure(1, weight=1); log_card.columnconfigure(0, weight=1)
        ttk.Label(log_card, text="运行日志", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 9))
        ttk.Button(log_card, text="打开记录", command=lambda:os.startfile(self.store.path)).grid(row=0, column=1, sticky="e", pady=(0, 9))
        self.logs = tk.Text(log_card, height=6, width=55, relief="flat", bg="#f6f8f8", fg="#40595f", padx=10, pady=10,
                            font=("Microsoft YaHei UI", 9), wrap="word", state="disabled")
        self.logs.grid(row=1, column=0, columnspan=2, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_card, command=self.logs.yview)
        scrollbar.grid(row=1, column=2, sticky="ns")
        self.logs.configure(yscrollcommand=scrollbar.set)
        self.footer = tk.Label(page, text="自动适配游戏位置与大小 · 保持游戏完整可见 · 胜利截图留存 · 达到目标自动停止", bg="#eef3f3", fg="#6e8388", font=("Microsoft YaHei UI", 9))
        self.footer.pack(anchor="w", padx=23, pady=(0, 13))

    def _append_log(self, value):
        self.logs.configure(state="normal")
        self.logs.insert("end", value.rstrip()+"\n")
        if int(self.logs.index("end-1c").split(".")[0]) > 800:
            self.logs.delete("1.0", "200.0")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def start(self):
        from tkinter import messagebox
        try:
            target = int(self.target.get())
            self.controller.start(target, TEAMS[self.team.get()])
            write_json(self.store.path.parent/"settings.json", {"target": target})
            self._append_log("开始任务，正在连接游戏窗口。")
        except (ValueError, OSError) as exc:
            messagebox.showinfo(APP_NAME, str(exc), parent=self.root)

    def send(self, command):
        try:
            if self.controller.command(command):
                self._append_log({"pause":"已请求暂停。", "resume":"已请求继续，将重新核对站位。", "stop":"已请求停止，正在保存计数。"}[command])
        except OSError as exc:
            self._append_log("发送操作失败："+str(exc))

    def reset(self):
        from tkinter import messagebox
        if self.controller.running():
            return
        if not messagebox.askyesno("新一轮计数", "将当前记录归档，新的胜场从 0 开始。历史截图和日志会保留。\n\n是否新建？", parent=self.root):
            return
        try:
            destination = self.store.reset()
            self.log_offset = self.preview_mtime = 0
            self.preview.delete("all")
            self.preview.create_text(285,125,text="开始后显示游戏截图",fill="#71878c",font=("Microsoft YaHei UI",11))
            self.team.set(list(TEAMS)[0])
            self.controller.last_exit = None
            self._append_log("已创建新一轮，旧记录保存在："+str(destination))
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    def refresh(self):
        self.controller.poll()
        if self.closing and not self.controller.running():
            self.root.destroy()
            return
        progress = self.store.progress()
        state = read_json(self.store.path/"status.json", {})
        wins = progress.get("wins", 0)
        try: target = max(1, int(self.target.get()))
        except ValueError: target = 30
        self.count.set(f"{wins} / {target}")
        self.progress.configure(maximum=target, value=min(wins, target))
        active = self.controller.running()
        paused = state.get("paused", True)
        reason = state.get("reason", "")
        if self.controller.stopping:
            title, detail = "正在停止", "等待本次操作结束并保存记录…"
        elif active:
            if reason in ("starting", "startup"):
                title, detail = "正在启动", "正在连接游戏窗口和识别服务。"
            elif paused:
                title = {"focus":"等待游戏前台", "calibration":"正在定位游戏画面"}.get(reason, "已暂停")
                detail = {"focus":"切回游戏窗口后自动继续。", "manual":"点击“继续”或按 F8 恢复。", "calibration":state.get("calibration_detail", "画面稳定后会自动重新定位并继续。")}.get(reason, reason or "请检查游戏画面后继续。")
            else:
                title, detail = "正在修炼", "只在游戏前台操作，达到目标后自动停止。"
        elif self.controller.last_exit not in (None, 0):
            title, detail = "启动或运行失败", reason or "请打开记录查看 process.log。"
        elif wins >= target:
            title, detail = "目标已完成", "提高目标可接着打；“新一轮计数”会归档本轮记录。"
        elif reason and reason not in ("complete", "stopped", "manual", "running", "focus", "starting", "startup"):
            title, detail = "已停止", reason
        else:
            title, detail = "就绪", "请先打开游戏并进入青木魔法学院或修炼场。"
        self.status.set(title); self.detail.set(detail)
        self.current_team.set(TEAM_NAMES.get(progress.get("team"), "等待入场分队"))
        self.cast_text.set(f"本局施法 {state.get('casts', 0)} 次  ·  最近更新 {state.get('time', '—')}")
        viewport = state.get("viewport")
        self.viewport_text.set(f"游戏画面：{viewport['width']} × {viewport['height']} 像素 · 自动定位" if viewport else "游戏画面：启动后自动定位")
        for widget in (self.start_button, self.reset_button, self.target_input):
            widget.configure(state="disabled" if active else "normal")
        self.team_input.configure(state="disabled" if active else "readonly")
        self.pause_button.configure(state="normal" if active and not self.controller.stopping else "disabled")
        self.resume_button.configure(state="normal" if active and paused and not self.controller.stopping else "disabled")
        self.stop_button.configure(state="normal" if active and not self.controller.stopping else "disabled")
        path = self.store.path/"run.log"
        try:
            if path.stat().st_size < self.log_offset: self.log_offset = 0
            with path.open("rb") as file:
                file.seek(self.log_offset)
                text = file.read()
                self.log_offset = file.tell()
            if text: self._append_log(text.decode("utf-8", errors="replace"))
        except OSError: pass
        image_path = self.store.path/"latest.png"
        try:
            mtime = image_path.stat().st_mtime_ns
            if mtime != self.preview_mtime:
                from PIL import Image, ImageTk
                with Image.open(image_path) as source:
                    preview = source.convert("RGB")
                preview.thumbnail((570, 260), Image.Resampling.LANCZOS)
                self.preview_photo = ImageTk.PhotoImage(preview)
                self.preview.delete("all")
                self.preview.create_image(max(1,self.preview.winfo_width())/2,130,image=self.preview_photo,anchor="center",tags="frame")
                self.preview_mtime = mtime
        except (OSError, ValueError): pass
        self.root.after(450, self.refresh)

    def close(self):
        if self.controller.running():
            self.closing = True
            self.send("stop")
            self.status.set("正在停止并退出")
        else:
            self.root.destroy()


def self_test(path):
    """检查打包依赖与 OCR；不创建 Desktop，不发送键盘鼠标输入。"""
    os.environ["AOBI_RECORDS_DIR"] = str(path.parent/"selftest-records")
    import aobi_bot
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    result = {"frozen": bool(getattr(sys,"frozen",False)), "vision_assets": False, "ocr": False, "game_input": False}
    aobi_bot.Vision()
    result["vision_assets"] = True
    from viewport import ViewportLocator
    locator = ViewportLocator()
    with Image.open(aobi_bot.ROOT/"assets"/"viewport_arena.png") as reference:
        canvas = np.asarray(reference.convert("RGB"))
    normalized = locator.locate(canvas)
    result["viewport"] = locator.mapping.info()
    result["viewport_detection"] = aobi_bot.Vision().scene(normalized) == "arena"
    user = ctypes.windll.user32
    user.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    result["dpi_awareness"] = user.GetAwarenessFromDpiAwarenessContext(user.GetThreadDpiAwarenessContext())
    folder = path.parent/"selftest-records"
    folder.mkdir(exist_ok=True)
    image = Image.new("RGB", (750,120), "white")
    ImageDraw.Draw(image).text((25,30), "目标不在攻击范围内", font=ImageFont.truetype("C:/Windows/Fonts/msyh.ttc",32), fill="black")
    ocr = aobi_bot.OCR(folder)
    try:
        text = ocr.read(np.asarray(image)).get("text", "")
        result["ocr_text"] = text
        result["ocr"] = "范围" in aobi_bot.compact(text)
    finally:
        ocr.close()
    write_json(path, result)
    return 0 if result["ocr"] and result["viewport_detection"] else 1


def main():
    from viewport import enable_dpi_awareness
    enable_dpi_awareness()
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--target", type=int, default=30)
    parser.add_argument("--data-dir", type=Path, default=BASE/"records")
    parser.add_argument("--self-test", type=Path)
    parser.add_argument("--ui-smoke-test", type=Path)
    args = parser.parse_args()
    if args.worker: return worker_main(args.data_dir.resolve(), args.target)
    if args.self_test:
        try:
            return self_test(args.self_test.resolve())
        except Exception:
            write_json(args.self_test.resolve(), {"error": traceback.format_exc(), "game_input": False})
            return 1
    mutex = NamedMutex("AobiTrainingAssistantUI")
    if mutex.existed:
        ctypes.windll.user32.MessageBoxW(None, "修炼助手已打开，请切换到已有窗口。", APP_NAME, 0x40)
        mutex.close()
        return 0
    import tkinter as tk
    root = tk.Tk()
    app = App(root, args.data_dir.resolve())
    smoke = {"success": False}
    if args.ui_smoke_test:
        def snapshot():
            try:
                from PIL import ImageGrab
                root.update_idletasks()
                path = args.ui_smoke_test.resolve()
                ImageGrab.grab(bbox=(root.winfo_rootx(),root.winfo_rooty(),root.winfo_rootx()+root.winfo_width(),root.winfo_rooty()+root.winfo_height())).save(path)
                write_json(path.with_suffix(".json"), {"worker_started": app.controller.process is not None, "wins": app.store.progress()["wins"], "status": app.status.get(), "width": root.winfo_width(), "height": root.winfo_height()})
                smoke["success"] = True
            except Exception:
                write_json(args.ui_smoke_test.with_suffix(".json"), {"error": traceback.format_exc()})
            finally:
                root.destroy()
        root.after(1800, snapshot)
    try:
        root.mainloop()
    finally:
        if app.controller.running(): app.controller.command("stop")
        mutex.close()
    return 0 if not args.ui_smoke_test or smoke["success"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        path = BASE/"startup-error.log"
        path.write_text(traceback.format_exc(), encoding="utf-8")
        ctypes.windll.user32.MessageBoxW(None, "程序启动失败，详情已保存到：\n"+str(path), APP_NAME, 0x10)
        raise

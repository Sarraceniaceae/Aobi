"""奥比岛修炼场：屏幕识别、快速施法、胜利计数。F8 开始/暂停，Esc 停止。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as wt
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from PIL import Image, ImageGrab
from viewport import ViewportLocator, ViewportNotFound, enable_dpi_awareness

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
RECORDS_DIR = Path(os.environ.get("AOBI_RECORDS_DIR", str(ROOT / "records"))).resolve()
W, H = 1071, 627
BOOKS = [(45, 155), (45, 248), (45, 345)]
CRYSTALS = {"rune": (354, 245), "fire": (759, 243)}
GROUND = {"rune": (420, 565), "fire": (745, 565)}
NAMES = {"rune": "符文勇士队", "fire": "火焰先锋队"}
OTHER = {"rune": "fire", "fire": "rune"}


def compact(s):
    return re.sub(r"\s+", "", s)


def log(s):
    print(time.strftime("%H:%M:%S"), s, flush=True)
    records = RECORDS_DIR
    if records.is_dir():
        with (records / "run.log").open("a", encoding="utf-8") as file:
            file.write(time.strftime("%H:%M:%S")+" "+s+"\n")


class OCR:
    def __init__(self, folder):
        self.folder = folder
        self.sequence = 0
        self.pending_files = []
        source = (ROOT / "ocr_worker.ps1").read_text(encoding="utf-8")
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows"))/"System32"/"WindowsPowerShell"/"v1.0"/"powershell.exe"
        self.proc = subprocess.Popen(
            [str(powershell), "-NoLogo", "-NoProfile", "-Command", source],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=0x08000000,
        )
        self.replies = queue.Queue()
        self.errors = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._errors, daemon=True).start()
        first = self._reply()
        if not first.get("ready"):
            raise RuntimeError(f"OCR 启动失败：{first}")

    def _read(self):
        for line in self.proc.stdout:
            if line.strip():
                try:
                    self.replies.put(json.loads(line))
                except ValueError:
                    self.errors.append(line)
        self.replies.put({"error": "OCR 进程已退出。"})

    def _errors(self):
        for line in self.proc.stderr:
            self.errors.append(line)

    def _reply(self):
        try:
            result = self.replies.get(timeout=12)
        except queue.Empty:
            raise RuntimeError("OCR 超时。" + "".join(self.errors[-5:]))
        if "error" in result:
            raise RuntimeError(result["error"] + "".join(self.errors[-5:]))
        return result

    def read(self, rgb):
        self.sequence += 1
        path = self.folder / f"ocr_input_{os.getpid()}_{self.sequence}.png"
        self.pending_files.append(path)
        Image.fromarray(rgb).save(path)
        self.proc.stdin.write(json.dumps({"path": str(path)}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return self._reply()

    def dialog(self, rgb):
        x, y = 270, 160
        crop = cv2.resize(rgb[y:555, x:815], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        result = self.read(crop)
        for line in result.get("lines", []):
            line["x"] = line["x"]/2+x
            line["y"] = line["y"]/2+y
            line["w"] /= 2
            line["h"] /= 2
        return result

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=2)
        for path in self.pending_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


class Vision:
    def __init__(self):
        self.wand = np.asarray(Image.open(ROOT / "assets" / "wand.png").convert("RGB"))
        self.wand_mask = np.repeat(np.asarray(Image.open(ROOT / "assets" / "wand_mask.png"))[:, :, None], 3, axis=2)
        self.templates = {}
        self.team_prompt = np.asarray(Image.open(ROOT / "assets" / "team_prompt.png").convert("L"))
        self.team_button = np.asarray(Image.open(ROOT / "assets" / "team_button.png").convert("L"))
        for name in ("arena", "lobby", "selection"):
            path = ROOT / "assets" / (name + ".png")
            self.templates[name] = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if self.templates[name] is None:
                raise RuntimeError(f"无法读取模板：{path}")

    def scores(self, rgb):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        return {name: float(cv2.minMaxLoc(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED))[1])
                for name, t in self.templates.items()}

    def scene(self, rgb):
        scores = self.scores(rgb)
        if scores["selection"] > .83:
            return "selection"
        if scores["arena"] > .78:
            return "arena"
        if scores["lobby"] > .78:
            return "lobby"
        return "unknown"

    def selected(self, rgb, index):
        return self.cursor_selected(rgb, BOOKS[index])

    def cursor_selected(self, rgb, point):
        x, y = point
        patch = rgb[max(0,y-16):min(H,y+40), max(0,x-17):min(W,x+38)]
        if patch.shape[0] < self.wand.shape[0] or patch.shape[1] < self.wand.shape[1]:
            return False
        result = cv2.matchTemplate(patch, self.wand, cv2.TM_SQDIFF_NORMED, mask=self.wand_mask)
        return float(np.nanmin(result)) < .065

    @staticmethod
    def modal(rgb):
        hsv = cv2.cvtColor(rgb[190:445, 315:745], cv2.COLOR_RGB2HSV)
        pale = (hsv[:, :, 1] < 105) & (hsv[:, :, 2] > 175)
        return float(pale.mean()) > .69

    @staticmethod
    def ready(rgb):
        result = []
        for i, (x, y) in enumerate(BOOKS):
            hsv = cv2.cvtColor(rgb[y-10:y+11, x-10:x+11], cv2.COLOR_RGB2HSV)
            h, s, v = cv2.split(hsv)
            hue = ((h < 13) | (h > 170)) if i == 0 else ((h > 3) & (h < 30)) if i == 1 else ((h > 113) & (h < 165))
            result.append(float((hue & (s > 125) & (v > 90)).mean()) > .22)
        return result

    def team_icon(self, rgb):
        # 必须同时匹配正式分队文案及确认按钮；橙色分队帽没有这两个特征。
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        prompt_score = cv2.minMaxLoc(cv2.matchTemplate(gray[235:280,375:550], self.team_prompt, cv2.TM_CCOEFF_NORMED))[1]
        button_score = cv2.minMaxLoc(cv2.matchTemplate(gray[420:490,455:615], self.team_button, cv2.TM_CCOEFF_NORMED))[1]
        if prompt_score < .93 or button_score < .93:
            return None
        hsv = cv2.cvtColor(rgb[270:408, 462:612], cv2.COLOR_RGB2HSV)
        h, s, v = cv2.split(hsv)
        purple = int(((h > 125) & (h < 175) & (s > 150) & (v > 95)).sum())
        orange = int(((h > 4) & (h < 37) & (s > 155) & (v > 145)).sum())
        if purple > 1800 and purple > orange*1.4:
            return "rune"
        if orange > 2400 and orange > purple*2:
            return "fire"
        return None

    @staticmethod
    def health(rgb, team):
        x0, x1, y0, y1 = (299, 414, 194, 215) if team == "rune" else (704, 817, 187, 208)
        hsv = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2HSV)
        h, s, v = cv2.split(hsv)
        color = (((h < 38) | (h > 172)) & (s > 140) & (v > 135))
        return float((color.sum(axis=0) >= 3).mean())

    @staticmethod
    def player_bar(rgb):
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        h, s, v = cv2.split(hsv)
        mask = ((((h < 38) | (h > 172)) & (s > 140) & (v > 135))).astype(np.uint8)
        mask[:110] = 0
        mask[194:215, 290:425] = 0
        mask[187:208, 690:830] = 0
        mask[600:] = 0
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        bars = [(int(x), int(y), int(w), int(h)) for x,y,w,h,area in stats[1:]
                if 35 <= w <= 65 and 3 <= h <= 9 and w/h > 6 and area/(w*h) > .8]
        if bars:
            return bars[0] if len(bars) == 1 else None
        # 缩小后血条可能与背景粘连。断开细连接后还须核验上下深色边框。
        horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, 25), np.uint8))
        _, _, stats, _ = cv2.connectedComponentsWithStats(horizontal, 8)
        for x, y, w, h, area in stats[1:]:
            if not (35 <= w <= 65 and 3 <= h <= 9 and w/h > 6 and area/(w*h) > .8):
                continue
            above = (v[y-3:y+1, x+3:x+33].min(axis=0) < 110).mean()
            below = (v[y+h-1:y+h+3, x+3:x+33].min(axis=0) < 110).mean()
            if above > .85 and below > .85:
                bars.append((int(x), int(y), int(w), int(h)))
        return bars[0] if len(bars) == 1 else None

    @staticmethod
    def near_player(point, bar):
        if bar is None:
            return True
        x,y,w,h = bar
        return abs(point[0]-(x+w/2)) < 130 and y-145 < point[1] < y+65

    @staticmethod
    def magic_change(previous, rgb, team):
        if previous is None:
            return False
        x, y = GROUND[team]
        x0, x1, y0, y1 = max(0, x-150), min(W, x+70), max(0, y-130), min(H, y+15)
        a, b = previous[y0:y1, x0:x1], rgb[y0:y1, x0:x1]
        hsv = cv2.cvtColor(b, cv2.COLOR_RGB2HSV)
        yellow = (hsv[:, :, 0] > 18) & (hsv[:, :, 0] < 40) & (hsv[:, :, 1] > 165) & (hsv[:, :, 2] > 190)
        change = np.max(np.abs(a.astype(np.int16)-b.astype(np.int16)), axis=2) > 60
        return int((yellow & change).sum()) > 50


class PauseState:
    """区分人工暂停、异常暂停及暂时失去前台。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.reason = "manual"
        self.generation = 0

    def is_set(self):
        with self.lock:
            return self.reason is not None

    def set(self, reason="manual"):
        with self.lock:
            self.reason = reason

    def clear(self):
        with self.lock:
            self.reason = None
            self.generation += 1

    def focus_lost(self):
        with self.lock:
            if self.reason is not None:
                return False
            self.reason = "focus"
            return True

    def resume_focus(self):
        with self.lock:
            if self.reason != "focus":
                return False
            self.reason = None
            self.generation += 1
            return True


    def calibration_wait(self):
        with self.lock:
            if self.reason not in (None, "calibration"):
                return False
            self.reason = "calibration"
            return True

    def resume_calibration(self):
        with self.lock:
            if self.reason != "calibration":
                return False
            self.reason = None
            self.generation += 1
            return True


class Desktop:
    def __init__(self, stop, paused):
        self.stop, self.paused = stop, paused
        self.synthetic_escape = threading.Event()
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        enable_dpi_awareness()
        self.locator = ViewportLocator()
        self.capture_rect = None
        self.user.GetForegroundWindow.restype = wt.HWND
        self.user.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        self.user.GetWindowTextLengthW.argtypes = [wt.HWND]
        self.user.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        self.user.IsWindowVisible.argtypes = [wt.HWND]
        self.user.IsIconic.argtypes = [wt.HWND]
        self.user.SetForegroundWindow.argtypes = [wt.HWND]
        self.user.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.user.mouse_event.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
        self.user.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
        found = []
        callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        @callback_type
        def collect(hwnd, _):
            if self.user.IsWindowVisible(hwnd):
                buffer = ctypes.create_unicode_buffer(self.user.GetWindowTextLengthW(hwnd)+1)
                self.user.GetWindowTextW(hwnd, buffer, len(buffer))
                if "百田游戏安全管家" in buffer.value:
                    found.append(hwnd)
            return True

        self.user.EnumWindows(collect, 0)
        if len(found) != 1:
            raise RuntimeError(f"需要唯一的百田游戏窗口，目前找到 {len(found)} 个。")
        self.hwnd = found[0]
        threading.Thread(target=self.hotkeys, daemon=True).start()

    def hotkeys(self):
        held = False
        while not self.stop.is_set():
            if self.user.GetAsyncKeyState(0x1B) & 0x8000 and not self.synthetic_escape.is_set():
                self.stop.set()
                break
            now = bool(self.user.GetAsyncKeyState(0x77) & 0x8000)
            if now and not held:
                if self.paused.is_set():
                    self.user.ShowWindow(self.hwnd, 9)
                    self.user.SetForegroundWindow(self.hwnd)
                    self.paused.clear()
                    log("开始/继续。F8 暂停，Esc 停止。")
                else:
                    self.paused.set()
                    log("已暂停。")
            held = now
            time.sleep(.025)

    def window_rect(self):
        rect = wt.RECT()
        if not self.user.GetWindowRect(self.hwnd, ctypes.byref(rect)):
            raise RuntimeError("游戏窗口已关闭。")
        return rect.left, rect.top, rect.right, rect.bottom

    def capture(self):
        rect = self.window_rect()
        if not self.foreground():
            raise ViewportNotFound("游戏不在前台，等待游戏画面。")
        if rect[2]-rect[0] < 640 or rect[3]-rect[1] < 390:
            raise ViewportNotFound("游戏窗口过小，请放大游戏画面。")
        try:
            shot = np.asarray(ImageGrab.grab(bbox=rect, all_screens=True).convert("RGB"))
        except OSError as exc:
            raise ViewportNotFound("暂时无法截取游戏窗口，将自动重试。") from exc
        previous = self.locator.mapping
        rgb = self.locator.locate(shot)
        m = self.locator.mapping
        if rect != self.window_rect():
            raise ViewportNotFound("窗口正在移动或缩放，等待窗口稳定。")
        vx, vy = self.user.GetSystemMetrics(76), self.user.GetSystemMetrics(77)
        vw, vh = self.user.GetSystemMetrics(78), self.user.GetSystemMetrics(79)
        l, t = rect[0]+m.tx, rect[1]+m.ty
        r, b = l+m.sx*W, t+m.sy*H
        if l < vx-2 or t < vy-2 or r > vx+vw+2 or b > vy+vh+2:
            raise ViewportNotFound("游戏画面超出屏幕，请将完整画面移回屏幕内。")
        self.capture_rect = rect
        self.box = l, t, r, b
        if previous and max(abs(previous.sx-m.sx)*W, abs(previous.sy-m.sy)*H,
                            abs(previous.tx-m.tx), abs(previous.ty-m.ty)) > 3:
            raise ViewportNotFound("已检测到游戏画面尺寸变化，正在重新核对站位。")
        return rgb

    def check_geometry(self):
        if self.capture_rect is None or self.locator.mapping is None or self.window_rect() != self.capture_rect:
            raise ViewportNotFound("窗口位置或尺寸已变化，正在重新定位。")

    def foreground(self):
        return self.user.GetForegroundWindow() == self.hwnd and not self.user.IsIconic(self.hwnd)

    def can_input(self):
        if self.stop.is_set() or self.paused.is_set():
            return False
        if not self.foreground():
            if self.paused.focus_lost():
                log("游戏离开前台，暂停点击；回到前台后自动继续。")
            return False
        return True

    def click(self, point):
        if not self.can_input():
            return False
        if point in GROUND.values() and not self.cancel_magic():
            return False
        cursor = wt.POINT()
        self.user.GetCursorPos(ctypes.byref(cursor))
        vx, vy = self.user.GetSystemMetrics(76), self.user.GetSystemMetrics(77)
        if vx <= cursor.x < vx+3 and vy <= cursor.y < vy+3:
            self.stop.set()
            return False
        self.check_geometry()
        x, y = self.locator.mapping.screen_point(point, self.capture_rect[:2])
        self.user.SetCursorPos(x, y)
        time.sleep(.25)
        if not self.can_input():
            return False
        self.check_geometry()
        self.user.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(.1)
        self.user.mouse_event(0x0004, 0, 0, 0, 0)
        return True

    def cancel_magic(self):
        # 已实测 Escape 只取消战场中的残留瞄准光标。
        if not self.can_input():
            return False
        self.synthetic_escape.set()
        try:
            self.user.keybd_event(0x1B, 0, 0, 0)
            time.sleep(.03)
            self.user.keybd_event(0x1B, 0, 0x0002, 0)
            time.sleep(.05)
        finally:
            self.synthetic_escape.clear()
        return self.can_input()


def classify_dialog(text, team):
    t = compact(text)
    if "选择你的队伍" in t or ("魔法帽" in t and "稍后" in t):
        return "assigning"
    if ("攻击" in t and "范围" in t) or "有效攻击" in t:
        return "range"
    if "加入" in t and (("符文" in t) != ("火焰" in t)):
        return "team_rune" if "符文" in t else "team_fire"
    if "恭喜你" in t and "取得胜利" in t:
        return "win"
    if any(s in t for s in ("冷却", "稍后", "休息", "下一场", "秒后", "尚未开始")):
        return "cooldown"
    has_result = any(s in t for s in ("胜利", "获胜", "胜出", "赢了", "赢得"))
    if has_result:
        if any(s in t for s in ("规则", "如果", "将会", "才能", "可以获得")):
            return "unknown"
        negative = any(s in t for s in ("失败", "战败", "落败", "输了")) or bool(re.search(r"(?:对方|对手|敌方).{0,4}(?:胜利|获胜|胜出|赢了)", t))
        other_name = "火焰" if team == "rune" else "符文"
        our_name = "符文" if team == "rune" else "火焰"
        if negative or (other_name in t and our_name not in t):
            return "loss"
        if other_name in t and our_name in t:
            return "unknown"
        if team and (our_name in t or not ("符文" in t or "火焰" in t)):
            return "win"
    if "修炼场" in t and any(s in t for s in ("进入", "前往", "确定")):
        return "entry"
    return "unknown"


class Bot:
    def __init__(self, args):
        self.args = args
        self.run_dir = RECORDS_DIR
        self.run_dir.mkdir(exist_ok=True)
        self.progress_path = self.run_dir / "progress.json"
        self.progress = {"wins": 0, "round_active": False, "team": args.team, "next_arena": 1}
        if self.progress_path.exists():
            self.progress.update(json.loads(self.progress_path.read_text(encoding="utf-8")))
        self.team = self.progress.get("team")
        self.stop, self.paused = threading.Event(), PauseState()
        self.paused.set()
        self.vision, self.ocr = Vision(), OCR(self.run_dir)
        self.desktop = Desktop(self.stop, self.paused)
        self.last_cast = [0., 0., 0.]
        self.skill_armed = [True, True, True]
        self.gray_seen = [False, False, False]
        self.cast_failures = [0, 0, 0]
        self.skill = 0
        self.previous = None
        self.best_health = 1.
        self.last_damage = self.last_magic = time.monotonic()
        self.wait_until = 0.
        self.repair = None
        self.repair_count = 0
        self.arena_seen = False
        self.last_scene = None
        self.last_report = 0.
        self.cooldown_until = {1: 0., 2: 0.}
        self.cast_count = 0
        self.await_team = False
        self.unknown_since = None
        self.control_path = self.run_dir / "control.txt"
        self.control_path.write_text("", encoding="utf-8")
        self.last_snapshot = 0.
        self.status_reason = "starting"
        self.move_goal = None
        self.move_deadline = 0.
        self.missing_player_since = None
        self.unknown_dialog_since = None
        self.foreground_since = None
        self.resume_generation = self.paused.generation
        self.calibration_retry = 0.
        self.calibration_detail = "正在定位游戏画面。"

    def control(self):
        command = self.control_path.read_text(encoding="utf-8").strip()
        if not command:
            return
        self.control_path.write_text("", encoding="utf-8")
        if command == "stop":
            self.stop.set()
        elif command == "pause":
            self.paused.set()
            log("已通过控制文件暂停。")
        elif command == "resume":
            self.desktop.user.ShowWindow(self.desktop.hwnd, 9)
            self.desktop.user.SetForegroundWindow(self.desktop.hwnd)
            self.paused.clear()
            self.last_damage = self.last_magic = time.monotonic()
            log("已通过控制文件继续。")

    def save(self):
        self.progress["team"] = self.team
        temp = self.progress_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.progress, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.progress_path)

    def evidence(self, rgb, label):
        filename = time.strftime("%Y%m%d-%H%M%S") + f"-{label}.png"
        Image.fromarray(rgb).save(self.run_dir / filename)

    def pause(self, rgb, reason):
        if self.paused.reason == "focus":
            return
        self.evidence(rgb, "need-check")
        self.paused.set("error")
        self.status_reason = reason
        log(reason + " 已暂停，截图在 records 中。")

    def resume_when_foreground(self, now):
        if self.paused.reason != "focus" or not self.desktop.foreground():
            self.foreground_since = None
            return False
        if self.foreground_since is None:
            self.foreground_since = now
        if now-self.foreground_since < .3 or not self.paused.resume_focus():
            return False
        self.foreground_since = None
        self.status_reason = "running"
        self.last_damage = self.last_magic = now
        self.skill_armed = [True, True, True]
        self.gray_seen = [False, False, False]
        self.previous = None
        self.missing_player_since = self.unknown_since = self.unknown_dialog_since = None
        if self.move_goal:
            self.move_deadline = now+12
        if self.repair == "await_range":
            self.repair_deadline = now+5
        self.latest_due = True
        log("游戏已回到前台，自动继续。F8 可手动暂停。")
        return True

    def refresh_after_resume(self, now):
        if self.resume_generation == self.paused.generation:
            return False
        self.resume_generation = self.paused.generation
        self.status_reason = "running"
        self.move_goal = None
        self.move_deadline = self.repair_deadline = 0.
        self.repair = None
        self.arena_seen = False
        self.wait_until = 0.
        self.last_damage = self.last_magic = now
        self.missing_player_since = self.unknown_since = self.unknown_dialog_since = None
        self.previous = None
        self.skill_armed = [True, True, True]
        self.gray_seen = [False, False, False]
        self.cast_failures = [0, 0, 0]
        self.latest_due = True
        log("恢复时已清除过期等待，重新核对队伍对应站位。")
        return True

    def click_button(self, data, labels, fallback=None):
        for line in data.get("lines", []):
            t = compact(line["text"])
            if any(label in t for label in labels) and len(t) < 12:
                # OCR 输入是全幅标准化游戏截图。
                return self.desktop.click((line["x"]+line["w"]/2, line["y"]+line["h"]/2))
        return self.desktop.click(fallback) if fallback else False

    def begin_round(self, team):
        self.team = team
        self.progress["round_active"] = True
        self.best_health = 1.
        self.last_damage = self.last_magic = time.monotonic()
        self.cast_count = self.repair_count = 0
        self.skill_armed = [True, True, True]
        self.gray_seen = [False, False, False]
        self.arena_seen = False
        self.repair = None
        self.save()
        log(f"本局：{NAMES[team]}，攻击{'左' if team == 'rune' else '右'}水晶。")

    def dialog(self, rgb):
        data = self.ocr.dialog(rgb)
        text = compact(data.get("text", ""))
        kind = classify_dialog(text, self.team)
        now = time.monotonic()
        if kind == "unknown":
            icon_team = self.vision.team_icon(rgb)
            if icon_team:
                kind = "team_" + icon_team
        if kind != "unknown":
            self.unknown_dialog_since = None
        if kind == "assigning":
            self.await_team = True
            self.wait_until = now+.35
            return
        if kind.startswith("team_"):
            self.evidence(rgb, kind)
            log("队伍提示："+text)
            self.await_team = False
            self.begin_round(kind.split("_")[1])
            self.click_button(data, ("知道了", "确定"), (535, 460))
            self.wait_until = now+.6
        elif kind == "range":
            if not self.click_button(data, ("知道了", "确定", "确认"), (537, 438)):
                self.pause(rgb, "已识别范围提示，但未识别到确认按钮。")
                return
            self.repair = "return"
            self.arena_seen = True
            self.skill_armed = [True, True, True]
            self.gray_seen = [False, False, False]
            self.wait_until = now+.3
        elif kind == "win":
            if self.progress["round_active"]:
                self.progress["wins"] += 1
                self.progress["round_active"] = False
                self.save()
                self.evidence(rgb, f"victory-{self.progress['wins']:02d}")
                with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps({"time": time.strftime("%F %T"), "win": self.progress["wins"], "text": text}, ensure_ascii=False)+"\n")
                log(f"胜利 {self.progress['wins']}/{self.args.target}。")
            if self.progress["wins"] >= self.args.target:
                self.stop.set()
                return
            self.click_button(data, ("知道了", "确定", "确认"))
            self.wait_until = now+1.
        elif kind == "loss":
            self.pause(rgb, "识别到非本队胜利结果，不计数。")
        elif kind == "entry":
            if not self.click_button(data, ("确定", "确认"), (470, 433)):
                self.pause(rgb, "入场弹窗没有识别到确定按钮。")
            self.wait_until = now+.5
        elif kind == "cooldown":
            arena = 3-self.progress["next_arena"]
            seconds = re.search(r"(\d{1,3})秒", text)
            self.cooldown_until[arena] = now+(int(seconds.group(1))+1 if seconds else 15)
            if not self.click_button(data, ("知道了", "确定", "确认"), (537, 438)):
                self.pause(rgb, "冷却弹窗未识别到按钮。")
            self.wait_until = now+.5
        else:
            if self.unknown_dialog_since is None:
                self.unknown_dialog_since = now
            if now-self.unknown_dialog_since < 8:
                self.wait_until = now+.4
                return
            self.pause(rgb, "未识别的弹窗："+text[:140])

    def cast(self, index):
        current = self.desktop.capture()
        bar = self.vision.player_bar(current)
        if self.vision.scene(current) != "arena" or self.vision.modal(current):
            return
        if bar is None:
            if self.missing_player_since is None:
                self.missing_player_since = time.monotonic()
            elif time.monotonic()-self.missing_player_since > 5:
                self.pause(current, "持续无法确认角色位置，未点击水晶。")
            return
        self.missing_player_since = None
        if self.vision.near_player(CRYSTALS[self.team], bar):
            self.pause(current, "角色靠近水晶，未点击水晶。")
            return
        if not self.desktop.cancel_magic():
            return
        if self.cast_count < 6:
            Image.fromarray(current).save(self.run_dir/f"before-cast-{self.cast_count:02d}.png")
            log(f"施法书 {index+1}，角色血条 {bar}，目标 {CRYSTALS[self.team]}。")
        if self.desktop.click(BOOKS[index]):
            if index != 1:
                time.sleep(self.args.click_gap)
                if not self.desktop.can_input():
                    return
                selected = self.desktop.capture()
                if not self.vision.selected(selected, index):
                    time.sleep(.12)
                    if not self.desktop.can_input():
                        return
                    selected = self.desktop.capture()
                if not self.vision.selected(selected, index):
                    self.pause(selected, "没有检测到已选中魔法的光标，未点击水晶。")
                    return
                time.sleep(.08)
                if not self.desktop.can_input():
                    return
                selected = self.desktop.capture()
                if not self.vision.selected(selected, index):
                    self.pause(selected, "选书画面尚未稳定，未点击水晶。")
                    return
                if self.cast_count < 3:
                    Image.fromarray(selected).save(self.run_dir/f"selected-book-{index+1}.png")
                if not self.desktop.click(CRYSTALS[self.team]):
                    return
            self.last_cast[index] = time.monotonic()
            self.skill_armed[index] = False
            self.gray_seen[index] = False
            time.sleep(.2)
            if not self.desktop.can_input():
                return
            result = self.desktop.capture()
            response_deadline = time.monotonic()+1.6
            retry_after = time.monotonic()+.35
            retries = 0
            while self.vision.ready(result)[index] and not self.vision.modal(result) and time.monotonic() < response_deadline:
                if not self.desktop.can_input():
                    return
                if index != 1 and retries < 2 and time.monotonic() >= retry_after and self.vision.cursor_selected(result, CRYSTALS[self.team]):
                    bar_after = self.vision.player_bar(result)
                    if bar_after is not None and not self.vision.near_player(CRYSTALS[self.team], bar_after):
                        if not self.desktop.click(CRYSTALS[self.team]):
                            return
                        retries += 1
                        retry_after = time.monotonic()+.45
                time.sleep(.12)
                if not self.desktop.can_input():
                    return
                result = self.desktop.capture()
            if not self.vision.ready(result)[index]:
                self.gray_seen[index] = True
                self.cast_failures[index] = 0
            elif not self.vision.modal(result):
                self.cast_failures[index] += 1
                if self.cast_failures[index] >= 3:
                    self.pause(result, "连续三次未确认魔法进入冷却，停止后续点击。")
                    return
                self.skill_armed[index] = True
                self.last_cast[index] = time.monotonic()+1.5
                log(f"魔法书 {index+1} 暂未响应，稍后重试。")
                return
            self.cast_count += 1
            if self.args.max_casts and self.cast_count >= self.args.max_casts:
                time.sleep(.4)
                Image.fromarray(self.desktop.capture()).save(self.run_dir / "probe-after.png")
                self.stop.set()

    def fight(self, rgb):
        now = time.monotonic()
        if not self.progress["round_active"]:
            if self.progress["wins"] == 0 and self.team:
                self.begin_round(self.team)
            elif self.team is None and not self.await_team:
                self.pause(rgb, "当前已在战场，请停止后在界面选择当前队伍，再点击开始。")
                return
            else:
                return
        if not self.team:
            self.pause(rgb, "不知道当前队伍，请用 --team rune 或 --team fire 指定。")
            return
        current_bar = self.vision.player_bar(rgb)
        if current_bar is None:
            if self.missing_player_since is None:
                self.missing_player_since = now
            elif now-self.missing_player_since > 5:
                self.pause(rgb, "无法确认角色位置，未移动或点击水晶。")
            return
        self.missing_player_since = None
        if not self.move_goal and self.vision.near_player(CRYSTALS[self.team], current_bar):
            self.arena_seen = False
        if not self.arena_seen:
            bar = self.vision.player_bar(rgb)
            already_safe = bar is not None and bar[1] > 475 and abs(bar[0]+bar[2]/2-GROUND[self.team][0]) < 55
            if not already_safe:
                if bar and self.vision.near_player(GROUND[self.team], bar):
                    self.pause(rgb, "移动落点靠近角色，已停止点击。")
                    return
                if not self.desktop.click(GROUND[self.team]):
                    return
                self.move_goal = self.team
                self.move_deadline = now+12
            self.arena_seen = True
            self.wait_until = now+.2
            return
        if self.move_goal:
            bar = self.vision.player_bar(rgb)
            if bar and bar[1] > 475 and abs(bar[0]+bar[2]/2-GROUND[self.move_goal][0]) < 55:
                self.move_goal = None
                self.last_damage = self.last_magic = now
            elif now > self.move_deadline:
                self.pause(rgb, "未确认角色抵达下方安全站位，停止施法。")
            return
        if self.repair == "return":
            bar = self.vision.player_bar(rgb)
            already_safe = bar is not None and bar[1] > 475 and abs(bar[0]+bar[2]/2-GROUND[self.team][0]) < 55
            if not already_safe:
                if bar and self.vision.near_player(GROUND[self.team], bar):
                    self.pause(rgb, "恢复移动落点靠近角色，已停止点击。")
                    return
                if not self.desktop.click(GROUND[self.team]):
                    return
                self.move_goal = self.team
                self.move_deadline = now+12
            self.repair = None
            self.last_damage = self.last_magic = now
            self.best_health = self.vision.health(rgb, self.team)
            self.wait_until = now+.2
            return
        ready = self.vision.ready(rgb)
        for i in range(3):
            if not ready[i]:
                self.gray_seen[i] = True
            elif self.gray_seen[i] and now-self.last_cast[i] > .3:
                self.skill_armed[i] = True
        if self.repair == "cast_other":
            for i in (0, 2):
                if ready[i] and now-self.last_cast[i] > 1.:
                    self.cast(i)
                    self.repair = "await_range"
                    self.wait_until = now+.5
                    self.repair_deadline = now+5
                    return
            return
        if self.repair == "await_range":
            if now > self.repair_deadline:
                self.pause(rgb, "换边后未出现范围弹窗，停止重试。")
            return
        health = self.vision.health(rgb, self.team)
        if health < self.best_health-.005:
            self.best_health, self.last_damage = health, now
            self.repair_count = 0
        if self.vision.magic_change(self.previous, rgb, self.team):
            self.last_magic = now
        stalled = now-self.last_damage > 14 or (now-self.last_damage > 7 and now-self.last_magic > 5)
        if stalled and self.cast_count >= 5:
            if self.repair_count >= 3:
                self.pause(rgb, "已换边重置三次，水晶仍没有掉血，避免继续空打。")
                return
            if self.vision.near_player(GROUND[OTHER[self.team]], current_bar):
                self.pause(rgb, "换边落点靠近角色，未点击。")
                return
            if not self.desktop.click(GROUND[OTHER[self.team]]):
                return
            self.repair_count += 1
            self.move_goal = OTHER[self.team]
            self.move_deadline = now+12
            self.repair = "cast_other"
            self.wait_until = now+.2
            log(f"水晶未掉血，执行换边重置（{self.repair_count}/3）。")
            return
        for offset in range(3):
            i = (self.skill+offset) % 3
            if ready[i] and self.skill_armed[i] and now-self.last_cast[i] > .3:
                self.cast(i)
                self.skill = (i+1) % 3
                break

    def wait_for_viewport(self, error):
        if self.paused.calibration_wait():
            detail = str(error)
            if detail != self.calibration_detail:
                log(detail+" 暂停点击，画面稳定后自动重试。")
            self.calibration_detail = detail
            self.calibration_retry = time.monotonic()+.7

    def retry_viewport(self, now):
        if now < self.calibration_retry or not self.desktop.foreground():
            return
        self.calibration_retry = now+1.
        try:
            self.desktop.capture()
        except ViewportNotFound as exc:
            self.calibration_detail = str(exc)
            return
        if self.paused.resume_calibration():
            self.status_reason = "running"
            log("已重新定位完整游戏画面，自动继续并核对站位。")

    def run(self):
        if self.args.auto_start:
            self.paused.set("startup")
            last_activate = 0.
            log(f"已载入胜利计数 {self.progress['wins']}/{self.args.target}，等待游戏窗口到前台后自动开始。")
        else:
            log(f"已载入胜利计数 {self.progress['wins']}/{self.args.target}。当前暂停，按 F8 开始。")
        try:
            while not self.stop.is_set() and self.progress["wins"] < self.args.target:
                self.control()
                if self.stop.is_set():
                    break
                now = time.monotonic()
                if now-self.last_snapshot > 1.:
                    status = {"time":time.strftime("%F %T"), "paused":self.paused.is_set(), "wins":self.progress["wins"], "team":self.team,"scene":self.last_scene,"reason":self.paused.reason if self.paused.is_set() and self.paused.reason != "error" else self.status_reason,"casts":self.cast_count,"repair":self.repair,"viewport":self.desktop.locator.mapping.info() if self.desktop.locator.mapping else None,"calibration_detail":self.calibration_detail}
                    (self.run_dir/"status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
                    if not self.paused.is_set():
                        self.latest_due = True
                    self.last_snapshot = now
                if self.paused.is_set():
                    if self.paused.reason == "focus":
                        self.resume_when_foreground(now)
                    elif self.paused.reason == "calibration":
                        self.retry_viewport(now)
                    elif self.paused.reason == "startup":
                        if self.desktop.foreground():
                            self.paused.clear()
                            self.status_reason = "running"
                            log("游戏已在前台，自动开始。")
                        elif time.monotonic()-last_activate > 1.:
                            self.desktop.user.ShowWindow(self.desktop.hwnd, 9)
                            self.desktop.user.SetForegroundWindow(self.desktop.hwnd)
                            last_activate = time.monotonic()
                    time.sleep(.08)
                    self.last_damage = self.last_magic = time.monotonic()
                    continue
                if not self.desktop.can_input():
                    continue
                try:
                    self.refresh_after_resume(now)
                    rgb = self.desktop.capture()
                    if getattr(self, "latest_due", False):
                        Image.fromarray(rgb).save(self.run_dir / "latest.png")
                        self.latest_due = False
                    now = time.monotonic()
                    if now < self.wait_until:
                        time.sleep(.08)
                        continue
                    scene = self.vision.scene(rgb)
                    if scene != "unknown":
                        self.unknown_since = None
                    if scene == "selection":
                        eligible = [i for i in (self.progress["next_arena"], 3-self.progress["next_arena"]) if self.cooldown_until[i] <= now]
                        if eligible:
                            arena = eligible[0]
                            if not self.desktop.click((544, 310) if arena == 1 else (408, 388)):
                                continue
                            self.progress["round_active"] = False
                            self.team = None
                            self.progress["current_arena"] = arena
                            self.progress["next_arena"] = 3-arena
                            log(f"进入 {arena} 号修炼场，等待正式队伍提示。")
                            self.await_team = True
                            self.save()
                            self.wait_until = now+.7
                    elif self.vision.modal(rgb):
                        self.dialog(rgb)
                    elif scene == "lobby":
                        self.arena_seen = False
                        self.desktop.click((683, 122))
                        self.wait_until = now+.6
                    elif scene == "arena":
                        self.fight(rgb)
                    else:
                        if self.unknown_since is None:
                            self.unknown_since = now
                        elif now-self.unknown_since > 6:
                            self.pause(rgb, "画面不匹配修炼场或学院模板，请检查游戏场景和遮挡。")
                    self.previous = rgb
                    self.last_scene = scene
                    time.sleep(.065)
                except ViewportNotFound as exc:
                    self.wait_for_viewport(exc)
        finally:
            self.stop.set()
            self.save()
            final_status = {"time":time.strftime("%F %T"), "paused":True, "wins":self.progress["wins"], "team":self.team, "scene":self.last_scene, "reason":"complete" if self.progress["wins"] >= self.args.target else "stopped", "casts":self.cast_count, "repair":self.repair}
            (self.run_dir/"status.json").write_text(json.dumps(final_status, ensure_ascii=False), encoding="utf-8")
            self.ocr.close()
            log(f"停止。已确认胜利 {self.progress['wins']}/{self.args.target}。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=30)
    parser.add_argument("--team", choices=("rune", "fire"), default="rune", help="仅用于启动时已在战场的队伍")
    parser.add_argument("--click-gap", type=float, default=.09, help="点书和点水晶之间的秒数")
    parser.add_argument("--auto-start", action="store_true", help="启动后立即运行")
    parser.add_argument("--max-casts", type=int, default=0, help="诊断时限制施法次数；0 不限制")
    parser.add_argument("--inspect-file", type=Path, help="仅识别图片，不控制鼠标")
    args = parser.parse_args()
    if args.target < 1 or not .015 <= args.click_gap <= .5:
        parser.error("target 必须大于 0；click-gap 范围是 0.015–0.5。")
    if args.inspect_file:
        rgb = np.asarray(Image.open(args.inspect_file).convert("RGB"))
        canvas = np.zeros((H, W, 3), dtype=np.uint8)
        canvas[:min(H, rgb.shape[0]), :min(W, rgb.shape[1])] = rgb[:H, :W]
        vision = Vision()
        print(json.dumps({"scene": vision.scene(canvas), "scores": vision.scores(canvas), "modal": vision.modal(canvas), "ready": vision.ready(canvas)}, ensure_ascii=False, indent=2))
        return
    if os.name != "nt":
        raise RuntimeError("本脚本用于 Windows。")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("本次游戏窗口以管理员运行。请右键启动.cmd，选择“以管理员身份运行”。")
    Bot(args).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已停止。")
    except Exception as exc:
        print("错误：", exc, file=sys.stderr)
        sys.exit(1)

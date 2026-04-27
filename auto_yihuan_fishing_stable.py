# -*- coding: utf-8 -*-
"""
auto_yihuan_fishing_stable.py

稳定版：
- 窗口标题：异环
- F8 开始/暂停
- F9 强制按一次 F，并进入 WAIT_HOOK
- F10 退出
- 上鱼检测：右下角 F 按钮蓝色激活环，连续多帧确认
- 拉条控制：绿色 bar + 黄色光标，连续确认后才进入 BAR
- 结算跳过：慢速随机点击/空格
- WAIT_HOOK 看门狗：20 秒不上鱼，随机点击两次，再按 F
"""

import time
import random
import ctypes

import cv2
import numpy as np
from mss import mss
import pydirectinput

import win32gui
import win32con
import win32process


# ============================================================
# DPI 处理，避免 Windows 缩放导致坐标不准
# ============================================================

try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass


# ============================================================
# 基础配置
# ============================================================

GAME_WINDOW_TITLE = "异环"

# 调试识别时改 True，稳定后改 False
DEBUG_VIEW = False

# 热键
VK_F8 = 0x77      # 开始 / 暂停
VK_F9 = 0x78      # 强制按一次 F
VK_F10 = 0x79     # 退出

# 游戏按键
LEFT_KEY = "a"
RIGHT_KEY = "d"
FISH_KEY = "f"

# 主循环频率，稳定版不用太高
LOOP_FPS = 45

# ROI 都基于游戏客户区，不包含标题栏和边框
BAR_ROI_REL = (0.08, 0.04, 0.86, 0.25)

# 右下角 F 按钮区域
HOOK_BUTTON_ROI_REL = (0.855, 0.765, 0.135, 0.215)

# 上鱼蓝环识别阈值
# 识别不到上鱼：调低
# 平时误判上鱼：调高
HOOK_BLUE_RATIO_THRESHOLD = 0.006
HOOK_BLUE_AREA_THRESHOLD = 180
HOOK_CONFIRM_FRAMES = 3

# WAIT_HOOK 看门狗
# 20 秒不上鱼，随机点两次，再按 F
NO_HOOK_WATCHDOG_SEC = 20.0
WATCHDOG_CLICK_TIMES = 2

# 拉条控制参数：稳定保守版
PREDICT_SEC = 0.08
DEADBAND_RATIO = 0.14
LOST_RELEASE_SEC = 0.25
BAR_END_LOST_SEC = 0.75
BAR_CONFIRM_FRAMES = 3
BAR_REENTER_BLOCK_SEC = 5.0

# 结算跳过：慢速，宁可慢，不要乱切状态
SETTLEMENT_AFTER_BAR_DELAY = 2.5
SETTLEMENT_SKIP_MIN_INTERVAL = 1.4
SETTLEMENT_SKIP_MAX_INTERVAL = 2.5
SETTLEMENT_MIN_SEC = 10.0
SETTLEMENT_MAX_SEC = 24.0
SETTLEMENT_MIN_ACTIONS = 6
SETTLEMENT_MAX_ACTIONS = 12
SETTLEMENT_SKIP_KEYS = ["space"]

# 结算完成后，多久开始下一杆
NEXT_CAST_DELAY_MIN = 1.2
NEXT_CAST_DELAY_MAX = 2.5

# 按 F 最小间隔，防止重复触发
F_PRESS_COOLDOWN = 0.9

# 窗口位置刷新间隔
WINDOW_REFRESH_INTERVAL = 1.0


pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False

held_key = None
last_f_press_time = 0.0


# ============================================================
# Win32 工具
# ============================================================

def is_key_down(vk):
    return ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000


def find_game_window():
    found = []

    def enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return

        title = win32gui.GetWindowText(hwnd)
        if title and GAME_WINDOW_TITLE in title:
            found.append(hwnd)

    win32gui.EnumWindows(enum_handler, None)

    if not found:
        raise RuntimeError("没有找到标题包含 [%s] 的窗口" % GAME_WINDOW_TITLE)

    return found[0]


def focus_window(hwnd):
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.2)

        foreground = win32gui.GetForegroundWindow()
        current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
        foreground_thread = win32process.GetWindowThreadProcessId(foreground)[0]

        try:
            ctypes.windll.user32.AttachThreadInput(current_thread, target_thread, True)
            ctypes.windll.user32.AttachThreadInput(current_thread, foreground_thread, True)

            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)

        finally:
            ctypes.windll.user32.AttachThreadInput(current_thread, target_thread, False)
            ctypes.windll.user32.AttachThreadInput(current_thread, foreground_thread, False)

        time.sleep(0.10)

    except Exception as e:
        print("[警告] 置前台失败：%s" % e)


def get_client_rect_on_screen(hwnd):
    left, top, right, bottom = win32gui.GetClientRect(hwnd)

    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
    screen_right, screen_bottom = win32gui.ClientToScreen(hwnd, (right, bottom))

    width = screen_right - screen_left
    height = screen_bottom - screen_top

    if width <= 0 or height <= 0:
        raise RuntimeError("游戏窗口客户区尺寸异常，可能窗口被最小化了")

    return {
        "left": int(screen_left),
        "top": int(screen_top),
        "width": int(width),
        "height": int(height),
    }


def make_roi(client_rect, rel):
    l, t, w, h = rel

    return {
        "left": client_rect["left"] + int(client_rect["width"] * l),
        "top": client_rect["top"] + int(client_rect["height"] * t),
        "width": max(1, int(client_rect["width"] * w)),
        "height": max(1, int(client_rect["height"] * h)),
    }


# ============================================================
# 按键控制
# ============================================================

def tap_key(key, duration=0.07):
    global last_f_press_time

    now = time.perf_counter()

    if key == FISH_KEY:
        if now - last_f_press_time < F_PRESS_COOLDOWN:
            return False
        last_f_press_time = now

    pydirectinput.keyDown(key)
    time.sleep(duration)
    pydirectinput.keyUp(key)
    return True


def set_hold_key(key):
    global held_key

    if held_key == key:
        return

    if held_key is not None:
        pydirectinput.keyUp(held_key)
        held_key = None

    if key is not None:
        pydirectinput.keyDown(key)
        held_key = key


def release_all():
    set_hold_key(None)


def safe_random_click(client_rect, y_min=0.38, y_max=0.72):
    """
    点击游戏画面中部安全区域，避开右上角关闭、右下角技能区。
    """
    x = client_rect["left"] + int(client_rect["width"] * random.uniform(0.35, 0.68))
    y = client_rect["top"] + int(client_rect["height"] * random.uniform(y_min, y_max))

    pydirectinput.moveTo(x, y, duration=random.uniform(0.10, 0.25))
    time.sleep(random.uniform(0.08, 0.20))
    pydirectinput.click(x=x, y=y)

    return "click:%d,%d" % (x, y)


def watchdog_recover_clicks(client_rect):
    """
    WAIT_HOOK 超时恢复：
    可能卡在结算界面、钓鱼开始界面、或者 F 没按下去。
    随机点屏幕安全区域两次，然后重新按 F。
    """
    actions = []

    for _ in range(WATCHDOG_CLICK_TIMES):
        action = safe_random_click(client_rect, y_min=0.38, y_max=0.72)
        actions.append(action)
        time.sleep(random.uniform(0.45, 0.90))

    return actions


def random_skip_settlement(client_rect):
    """
    跳过结算界面。
    慢速点击/按键，避免结算动画阶段吞输入。
    """
    action_type = random.choice(["click", "click", "click", "space"])

    time.sleep(random.uniform(0.15, 0.35))

    if action_type == "space":
        pydirectinput.keyDown("space")
        time.sleep(random.uniform(0.10, 0.20))
        pydirectinput.keyUp("space")
        time.sleep(random.uniform(0.20, 0.45))
        return "key:space"

    action = safe_random_click(client_rect, y_min=0.46, y_max=0.78)
    time.sleep(random.uniform(0.20, 0.45))
    return action


# ============================================================
# 上鱼识别：右下角 F 按钮蓝色圆弧
# ============================================================

def detect_hook_by_button_color(frame_bgr):
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]

    # 只看 ROI 中下部，避免水面、文字干扰
    region_mask = np.zeros((h, w), dtype=np.uint8)
    x1 = int(w * 0.20)
    x2 = int(w * 0.90)
    y1 = int(h * 0.32)
    y2 = int(h * 0.95)
    region_mask[y1:y2, x1:x2] = 255

    # 上鱼状态下，F 按钮外圈的深蓝 / 蓝紫圆弧
    blue_mask = cv2.inRange(
        hsv,
        np.array([96, 80, 80], dtype=np.uint8),
        np.array([135, 255, 255], dtype=np.uint8),
    )
    blue_mask = cv2.bitwise_and(blue_mask, region_mask)

    kernel = np.ones((3, 3), np.uint8)
    blue_mask = cv2.morphologyEx(blue_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    blue_mask = cv2.morphologyEx(blue_mask, cv2.MORPH_OPEN, kernel, iterations=1)

    blue_pixels = cv2.countNonZero(blue_mask)
    blue_ratio = blue_pixels / float(max(1, h * w))

    contours, _ = cv2.findContours(
        blue_mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    max_blue_area = 0.0
    best_box = None

    for c in contours:
        area = cv2.contourArea(c)
        bx, by, bw, bh = cv2.boundingRect(c)

        if area < 40:
            continue
        if bw < 12 or bh < 12:
            continue

        box_ratio = bw / float(max(1, bh))
        if box_ratio < 0.35 or box_ratio > 2.8:
            continue

        if area > max_blue_area:
            max_blue_area = area
            best_box = (bx, by, bw, bh)

    ok = (
        blue_ratio > HOOK_BLUE_RATIO_THRESHOLD
        and max_blue_area > HOOK_BLUE_AREA_THRESHOLD
    )

    return ok, {
        "button_frame": frame_bgr,
        "blue_ratio": blue_ratio,
        "max_blue_area": max_blue_area,
        "blue_mask": blue_mask,
        "best_box": best_box,
    }


def detect_hook_prompt(sct, hook_button_roi):
    raw_btn = np.array(sct.grab(hook_button_roi))
    btn = cv2.cvtColor(raw_btn, cv2.COLOR_BGRA2BGR)
    return detect_hook_by_button_color(btn)


# ============================================================
# 拉条识别：绿色 bar
# ============================================================

def pick_green_bar(hsv):
    lower = np.array([35, 50, 90], dtype=np.uint8)
    upper = np.array([95, 255, 255], dtype=np.uint8)

    mask = cv2.inRange(hsv, lower, upper)

    kernel_close = np.ones((3, 13), np.uint8)
    kernel_open = np.ones((3, 3), np.uint8)

    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_close, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel_open, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    roi_h, roi_w = hsv.shape[:2]

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = cv2.contourArea(c)

        if w < 80:
            continue
        if w > roi_w * 0.55:
            continue
        if h < 6 or h > 35:
            continue
        if w / max(h, 1) < 5.0:
            continue
        if area < 350:
            continue

        cy = y + h / 2.0
        if cy < roi_h * 0.08 or cy > roi_h * 0.78:
            continue

        sub = hsv[y:y + h, x:x + w]
        mean_s = float(np.mean(sub[:, :, 1]))
        mean_v = float(np.mean(sub[:, :, 2]))

        if mean_s < 70 or mean_v < 110:
            continue

        score = w * 20 + area + mean_s + mean_v
        candidates.append((score, x, y, w, h))

    if not candidates:
        return None

    _, x, y, w, h = max(candidates, key=lambda item: item[0])
    return x, y, x + w, y + h


# ============================================================
# 拉条识别：黄色光标
# ============================================================

def pick_yellow_cursor(hsv, green_rect):
    gx1, gy1, gx2, gy2 = green_rect

    img_h, img_w = hsv.shape[:2]

    search_x1 = max(0, gx1 - 90)
    search_x2 = min(img_w, gx2 + 90)
    search_y1 = max(0, gy1 - 45)
    search_y2 = min(img_h, gy2 + 45)

    hsv_box = hsv[search_y1:search_y2, search_x1:search_x2]

    lower = np.array([16, 60, 120], dtype=np.uint8)
    upper = np.array([42, 255, 255], dtype=np.uint8)

    mask = cv2.inRange(hsv_box, lower, upper)

    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    green_cy = (gy1 + gy2) / 2.0

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = cv2.contourArea(c)

        if w < 3 or w > 26:
            continue
        if h < 10 or h > 60:
            continue
        if h / max(w, 1) < 1.15:
            continue
        if area < 18:
            continue

        cx = search_x1 + x + w / 2.0
        cy = search_y1 + y + h / 2.0

        if cx < gx1 - 70 or cx > gx2 + 70:
            continue
        if abs(cy - green_cy) > 45:
            continue

        dist_y = abs(cy - green_cy)
        dist_x = 0 if gx1 <= cx <= gx2 else min(abs(cx - gx1), abs(cx - gx2))

        score = area * 4 + h * 3 - dist_y * 4 - dist_x * 2
        candidates.append((score, cx, cy, search_x1 + x, search_y1 + y, w, h))

    if candidates:
        _, cx, cy, x, y, w, h = max(candidates, key=lambda item: item[0])
        return cx, cy, x, y, w, h

    # 兜底：用列投影找黄色竖线
    if mask.shape[0] <= 0 or mask.shape[1] <= 0:
        return None

    col_sum = np.sum(mask > 0, axis=0)
    if col_sum.size <= 0:
        return None

    best_col = int(np.argmax(col_sum))
    best_count = int(col_sum[best_col])
    if best_count < 8:
        return None

    ys = np.where(mask[:, best_col] > 0)[0]
    if ys.size <= 0:
        return None

    y1 = int(np.min(ys))
    y2 = int(np.max(ys))
    if y2 - y1 < 8:
        return None

    cx = search_x1 + best_col
    cy = search_y1 + (y1 + y2) / 2.0

    if cx < gx1 - 70 or cx > gx2 + 70:
        return None

    return cx, cy, int(cx - 4), int(search_y1 + y1), 8, int(y2 - y1 + 1)


# ============================================================
# 拉条控制
# ============================================================

def new_bar_state():
    return {
        "ever_seen": False,
        "last_seen": 0.0,
        "last_green_center": None,
        "green_velocity": 0.0,
        "last_time": None,
    }


def detect_real_bar(sct, bar_roi):
    """
    真实拉条必须同时识别到绿色 bar 和黄色光标。
    """
    raw = np.array(sct.grab(bar_roi))
    frame = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    green_rect = pick_green_bar(hsv)
    if green_rect is None:
        return False

    yellow = pick_yellow_cursor(hsv, green_rect)
    if yellow is None:
        return False

    return True


def control_bar_once(sct, bar_roi, bar_state):
    now = time.perf_counter()

    raw = np.array(sct.grab(bar_roi))
    frame = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    green_rect = pick_green_bar(hsv)

    if green_rect is None:
        if now - bar_state["last_seen"] > LOST_RELEASE_SEC:
            release_all()

        if DEBUG_VIEW:
            cv2.imshow("bar-debug", frame)
            cv2.waitKey(1)

        if bar_state["ever_seen"] and now - bar_state["last_seen"] > BAR_END_LOST_SEC:
            return False

        return bar_state["ever_seen"]

    yellow = pick_yellow_cursor(hsv, green_rect)

    if yellow is None:
        if now - bar_state["last_seen"] > LOST_RELEASE_SEC:
            release_all()

        if DEBUG_VIEW:
            x1, y1, x2, y2 = green_rect
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.imshow("bar-debug", frame)
            cv2.waitKey(1)

        return True

    bar_state["ever_seen"] = True
    bar_state["last_seen"] = now

    gx1, gy1, gx2, gy2 = green_rect
    cursor_x, cursor_y, cx, cy, cw, ch = yellow

    green_center = (gx1 + gx2) / 2.0
    green_width = gx2 - gx1

    last_time = bar_state["last_time"]
    last_green_center = bar_state["last_green_center"]

    if last_time is not None and last_green_center is not None:
        dt = max(now - last_time, 0.001)
        instant_v = (green_center - last_green_center) / dt
        bar_state["green_velocity"] = bar_state["green_velocity"] * 0.75 + instant_v * 0.25

    bar_state["last_time"] = now
    bar_state["last_green_center"] = green_center

    target_x = green_center + bar_state["green_velocity"] * PREDICT_SEC

    safe_margin = max(12.0, green_width * 0.20)
    target_x = max(gx1 + safe_margin, min(gx2 - safe_margin, target_x))

    deadband = max(8.0, green_width * DEADBAND_RATIO)
    err = target_x - cursor_x

    edge_margin = max(10.0, green_width * 0.10)

    if cursor_x < gx1 + edge_margin:
        set_hold_key(RIGHT_KEY)
        action = RIGHT_KEY + ":edge-left"
    elif cursor_x > gx2 - edge_margin:
        set_hold_key(LEFT_KEY)
        action = LEFT_KEY + ":edge-right"
    else:
        if abs(err) <= deadband:
            set_hold_key(None)
            action = "release"
        elif err > 0:
            set_hold_key(RIGHT_KEY)
            action = RIGHT_KEY
        else:
            set_hold_key(LEFT_KEY)
            action = LEFT_KEY

    if DEBUG_VIEW:
        cv2.rectangle(frame, (gx1, gy1), (gx2, gy2), (0, 255, 0), 2)
        cv2.rectangle(frame, (cx, cy), (cx + cw, cy + ch), (0, 255, 255), 2)

        cv2.putText(
            frame,
            "cursor=%.1f target=%.1f err=%.1f %s" % (cursor_x, target_x, err, action),
            (20, 32),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
        )

        cv2.imshow("bar-debug", frame)
        cv2.waitKey(1)

    return True


# ============================================================
# 主状态机
# ============================================================

def main():
    print("正在查找游戏窗口：%s" % GAME_WINDOW_TITLE)

    hwnd = find_game_window()
    print("找到窗口：%s" % win32gui.GetWindowText(hwnd))
    focus_window(hwnd)

    print("")
    print("F8  ：开始 / 暂停")
    print("F9  ：强制按一次 F，并进入 WAIT_HOOK")
    print("F10 ：退出")
    print("")
    print("状态流：NEED_CAST -> WAIT_HOOK -> WAIT_BAR -> BAR -> SETTLEMENT_SKIP -> NEED_CAST")
    print("看门狗：WAIT_HOOK 超过 %.1f 秒不上鱼，会随机点击 %d 次后重新按 F" % (
        NO_HOOK_WATCHDOG_SEC,
        WATCHDOG_CLICK_TIMES,
    ))
    print("")

    running = False
    state = "NEED_CAST"

    last_f8 = False
    last_f9 = False
    last_f10 = False

    next_cast_time = 0.0
    wait_hook_start_time = 0.0

    hook_confirm_count = 0
    bar_confirm_count = 0
    bar_reenter_block_until = 0.0
    bar_state = new_bar_state()

    settlement_start_time = 0.0
    next_settlement_skip_time = 0.0
    settlement_action_count = 0
    settlement_target_actions = 0

    last_window_refresh = 0.0

    client_rect = None
    bar_roi = None
    hook_button_roi = None

    with mss() as sct:
        while True:
            now = time.perf_counter()

            f8 = bool(is_key_down(VK_F8))
            f9 = bool(is_key_down(VK_F9))
            f10 = bool(is_key_down(VK_F10))

            if f8 and not last_f8:
                running = not running
                release_all()

                if running:
                    focus_window(hwnd)
                    state = "NEED_CAST"
                    next_cast_time = 0.0
                    hook_confirm_count = 0
                    bar_confirm_count = 0
                    print("[状态] 开始")
                else:
                    print("[状态] 暂停")

                time.sleep(0.25)

            if f9 and not last_f9:
                focus_window(hwnd)
                release_all()
                tap_key(FISH_KEY)
                state = "WAIT_HOOK"
                wait_hook_start_time = time.perf_counter()
                hook_confirm_count = 0
                bar_confirm_count = 0
                print("[手动] 强制按 F，进入 WAIT_HOOK")
                time.sleep(0.25)

            if f10 and not last_f10:
                release_all()
                print("退出")
                break

            last_f8 = f8
            last_f9 = f9
            last_f10 = f10

            if not running:
                time.sleep(0.03)
                continue

            # 刷新窗口坐标
            if now - last_window_refresh > WINDOW_REFRESH_INTERVAL:
                last_window_refresh = now

                try:
                    if not win32gui.IsWindow(hwnd):
                        hwnd = find_game_window()
                        print("[窗口] 重新找到窗口：%s" % win32gui.GetWindowText(hwnd))

                    client_rect = get_client_rect_on_screen(hwnd)
                    bar_roi = make_roi(client_rect, BAR_ROI_REL)
                    hook_button_roi = make_roi(client_rect, HOOK_BUTTON_ROI_REL)

                except Exception as e:
                    release_all()
                    print("[窗口异常] %s" % e)
                    time.sleep(1.0)
                    continue

            if client_rect is None:
                time.sleep(0.05)
                continue

            # 只有 WAIT_BAR 才允许检测进入 BAR，避免结算/其他界面误抢占
            if state == "WAIT_BAR" and now >= bar_reenter_block_until:
                try:
                    real_bar = detect_real_bar(sct, bar_roi)
                except Exception as e:
                    release_all()
                    print("[截图异常] %s" % e)
                    time.sleep(0.5)
                    continue

                if real_bar:
                    bar_confirm_count += 1
                else:
                    bar_confirm_count = 0

                if bar_confirm_count >= BAR_CONFIRM_FRAMES:
                    release_all()
                    state = "BAR"
                    bar_state = new_bar_state()
                    bar_state["ever_seen"] = True
                    bar_state["last_seen"] = time.perf_counter()
                    bar_confirm_count = 0
                    print("[状态] 确认进入 BAR 拉条")
            else:
                bar_confirm_count = 0

            if state == "NEED_CAST":
                if now >= next_cast_time:
                    focus_window(hwnd)
                    release_all()

                    if tap_key(FISH_KEY):
                        state = "WAIT_HOOK"
                        wait_hook_start_time = time.perf_counter()
                        hook_confirm_count = 0
                        print("[动作] 按 F 开始钓鱼，等待上鱼")

            elif state == "WAIT_HOOK":
                ok_hook, debug_info = detect_hook_prompt(sct, hook_button_roi)

                if ok_hook:
                    hook_confirm_count += 1
                else:
                    hook_confirm_count = 0

                if hook_confirm_count >= HOOK_CONFIRM_FRAMES:
                    focus_window(hwnd)
                    release_all()

                    if tap_key(FISH_KEY):
                        state = "WAIT_BAR"
                        hook_confirm_count = 0
                        bar_confirm_count = 0
                        print("[动作] 确认上鱼蓝环，按 F 上鱼，等待拉条")
                        time.sleep(0.35)

                else:
                    waited = time.perf_counter() - wait_hook_start_time

                    if waited >= NO_HOOK_WATCHDOG_SEC:
                        focus_window(hwnd)
                        release_all()

                        print("[恢复] WAIT_HOOK 超过 %.1fs 未上鱼，随机点击后重新按 F" % waited)

                        actions = watchdog_recover_clicks(client_rect)
                        print("[恢复] 点击动作：%s" % ", ".join(actions))

                        time.sleep(random.uniform(0.5, 1.0))

                        tap_key(FISH_KEY)

                        wait_hook_start_time = time.perf_counter()
                        hook_confirm_count = 0
                        bar_confirm_count = 0

                        print("[恢复] 已重新按 F，继续等待上鱼")

                if DEBUG_VIEW:
                    btn = debug_info["button_frame"].copy()

                    if debug_info.get("best_box") is not None:
                        bx, by, bw, bh = debug_info["best_box"]
                        cv2.rectangle(btn, (bx, by), (bx + bw, by + bh), (0, 255, 255), 2)

                    cv2.putText(
                        btn,
                        "blue=%.4f area=%.1f confirm=%d/%d" % (
                            debug_info["blue_ratio"],
                            debug_info["max_blue_area"],
                            hook_confirm_count,
                            HOOK_CONFIRM_FRAMES,
                        ),
                        (10, 25),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.65,
                        (255, 255, 255),
                        2,
                    )

                    cv2.imshow("hook-button-debug", btn)
                    cv2.imshow("hook-blue-mask", debug_info["blue_mask"])
                    cv2.waitKey(1)

            elif state == "WAIT_BAR":
                # 稳定版不做时间强制跳转，等待真实 BAR 出现
                pass

            elif state == "BAR":
                still_bar = control_bar_once(sct, bar_roi, bar_state)

                if not still_bar:
                    release_all()

                    bar_reenter_block_until = time.perf_counter() + BAR_REENTER_BLOCK_SEC
                    bar_confirm_count = 0

                    state = "SETTLEMENT_SKIP"
                    settlement_start_time = time.perf_counter()
                    next_settlement_skip_time = settlement_start_time + SETTLEMENT_AFTER_BAR_DELAY
                    settlement_action_count = 0
                    settlement_target_actions = random.randint(
                        SETTLEMENT_MIN_ACTIONS,
                        SETTLEMENT_MAX_ACTIONS,
                    )

                    print("[状态] 拉条结束，进入结算跳过")

            elif state == "SETTLEMENT_SKIP":
                release_all()

                elapsed = now - settlement_start_time

                if now >= next_settlement_skip_time:
                    focus_window(hwnd)

                    action = random_skip_settlement(client_rect)
                    settlement_action_count += 1

                    print("[结算] 跳过动作 %d/%d，用时 %.1fs：%s" % (
                        settlement_action_count,
                        settlement_target_actions,
                        elapsed,
                        action,
                    ))

                    next_settlement_skip_time = time.perf_counter() + random.uniform(
                        SETTLEMENT_SKIP_MIN_INTERVAL,
                        SETTLEMENT_SKIP_MAX_INTERVAL,
                    )

                can_leave_by_actions = (
                    elapsed >= SETTLEMENT_MIN_SEC
                    and settlement_action_count >= settlement_target_actions
                )
                can_leave_by_timeout = elapsed >= SETTLEMENT_MAX_SEC

                if can_leave_by_actions or can_leave_by_timeout:
                    focus_window(hwnd)
                    final_action = random_skip_settlement(client_rect)
                    print("[结算] 退出前补一次跳过：%s" % final_action)

                    time.sleep(random.uniform(0.8, 1.2))

                    release_all()
                    state = "NEED_CAST"
                    next_cast_time = time.perf_counter() + random.uniform(
                        NEXT_CAST_DELAY_MIN,
                        NEXT_CAST_DELAY_MAX,
                    )
                    hook_confirm_count = 0
                    bar_confirm_count = 0

                    print("[状态] 结算跳过完成，准备下一杆")

            time.sleep(1.0 / LOOP_FPS)


if __name__ == "__main__":
    try:
        main()
    finally:
        release_all()
        cv2.destroyAllWindows()
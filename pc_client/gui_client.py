import socket
import struct
import threading
import time
import csv
import os
import sys
import configparser
from datetime import datetime
from collections import deque
import subprocess

import matplotlib as mpl
# --- OS判定でバックエンド切り替え ---
if sys.platform == 'darwin':
    mpl.use('macosx')  # macOS native backend
else:
    mpl.use('TkAgg')   # Windows / Linux compatible backend
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import matplotlib.dates as mdates
from matplotlib.widgets import Button, TextBox

# ==========================================
# 設定ファイル読み込み
# ==========================================
config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')
config.read(config_path, encoding='utf-8')

# --- ネットワーク設定 ---
DATA_PORT = config.getint('network', 'data_port', fallback=8000)
CMD_PORT = config.getint('network', 'cmd_port', fallback=8001)
PACKET_SIZE = config.getint('network', 'packet_size', fallback=37)
BROADCAST_ADDR = config.get('network', 'broadcast_addr', fallback="192.168.179.255")

# --- グラフ設定 ---
DISPLAY_TIME_WINDOW = config.getfloat('graph', 'display_time_window', fallback=10.0)
MAX_DISPLAY_POINTS = config.getint('graph', 'max_display_points', fallback=500)
MAX_POINTS = config.getint('graph', 'max_buffer_points', fallback=100000)
Y_AXIS_MIN = config.getfloat('graph', 'y_axis_min', fallback=-3.0)
Y_AXIS_MAX = config.getfloat('graph', 'y_axis_max', fallback=3.0)
ANIMATION_INTERVAL = config.getint('graph', 'animation_interval', fallback=100)

# 電圧表示用Y軸範囲（オフセット差分済み）
V_AXIS_MIN = -3.3
V_AXIS_MAX = 3.3

# --- デバイス設定 ---
KNOWN_DEVICES = {}
if config.has_section('devices'):
    for key, value in config.items('devices'):
        parts = [p.strip() for p in value.split(',')]
        if len(parts) == 2:
            mac = parts[0].upper()
            dev_id = int(parts[1])
            KNOWN_DEVICES[mac] = dev_id

# ==========================================
# グローバル状態
# ==========================================
running = True
is_measuring = False
is_free_run = False
current_data_dir = None
save_base_dir = os.getcwd()  # Default save location

# 表示モード: 'force' (荷重 N) or 'voltage' (生電圧 V)
display_mode = 'force'

csv_files = {}
csv_writers = {}
discovered_devices = {}  # {MAC: {'id': ID, 'ip': IP}}
ip_to_mac = {}

# plot_data に生電圧バッファ (vx, vy, vz) も追加
plot_data = {}  # { assigned_id: {'t': deque, 'fx': deque, 'fy': deque, 'fz': deque, 'vx': deque, 'vy': deque, 'vz': deque} }
time_origin = {}  # { assigned_id: first_timestamp } for relative time
data_lock = threading.Lock()
log_lines = deque(maxlen=8)

# UDPソケット
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
# SO_REUSEPORT は macOS / Linux のみ
if sys.platform != 'win32':
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
except:
    pass
sock.bind(('0.0.0.0', DATA_PORT))


# ==========================================
# ロジック
# ==========================================
def log_msg(msg):
    """ログメッセージをバッファに追加（GUIに表示される）"""
    log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_device_buffers(assigned_id):
    with data_lock:
        if assigned_id not in plot_data:
            plot_data[assigned_id] = {
                't': deque(maxlen=MAX_POINTS),
                'fx': deque(maxlen=MAX_POINTS),
                'fy': deque(maxlen=MAX_POINTS),
                'fz': deque(maxlen=MAX_POINTS),
                'vx': deque(maxlen=MAX_POINTS),
                'vy': deque(maxlen=MAX_POINTS),
                'vz': deque(maxlen=MAX_POINTS),
            }
        return plot_data[assigned_id]


def receive_loop():
    global current_data_dir, is_measuring
    log_msg("Receiver started")
    while running:
        try:
            sock.settimeout(0.5)
            data, addr = sock.recvfrom(1024)
            ip = addr[0]

            if len(data) == PACKET_SIZE:
                unpacked = struct.unpack(">H B Q 12h H", data)
                header, _cpp_dev_id, ts, *channels, footer = unpacked
                if header == 0xAAAA and footer == 0x5555:

                    if ip not in ip_to_mac:
                        sock.sendto(b"SEARCH", (ip, CMD_PORT))
                        continue

                    mac = ip_to_mac[ip]
                    if mac not in KNOWN_DEVICES:
                        continue

                    assigned_id = KNOWN_DEVICES[mac]
                    bufs = get_device_buffers(assigned_id)
                    with data_lock:
                        # グラフ用: PC側の壁時計時間（絶対値）
                        now = time.time()
                        bufs['t'].append(now)
                        # 荷重データ (ForceX/Y/Z)
                        bufs['fx'].append(channels[9] / 1000.0)
                        bufs['fy'].append(channels[10] / 1000.0)
                        bufs['fz'].append(channels[11] / 1000.0)
                        # 電圧データ (ch1/ch2/ch3) - オフセット差分済み電圧
                        bufs['vx'].append(channels[0] / 1000.0)
                        bufs['vy'].append(channels[1] / 1000.0)
                        bufs['vz'].append(channels[2] / 1000.0)

                    if is_measuring and current_data_dir:
                        if assigned_id not in csv_writers:
                            base_name = f"device{assigned_id}.csv"
                            filename = os.path.join(current_data_dir, base_name)
                            # 重複時は device1(1).csv, device1(2).csv, ... で保存
                            if os.path.exists(filename):
                                suffix = 1
                                while os.path.exists(os.path.join(current_data_dir, f"device{assigned_id}({suffix}).csv")):
                                    suffix += 1
                                base_name = f"device{assigned_id}({suffix}).csv"
                                filename = os.path.join(current_data_dir, base_name)
                            f = open(filename, 'x', newline='')  # 'x' = exclusive create
                            w = csv.writer(f)
                            w.writerow(["Time_ms", "DeviceID", "ch1", "ch2", "ch3",
                                        "AccX", "AccY", "AccZ", "GyroX", "GyroY", "GyroZ",
                                        "ForceX", "ForceY", "ForceZ"])
                            csv_files[assigned_id] = f
                            csv_writers[assigned_id] = w
                            log_msg(f"CSV created: {base_name}")

                        scaled = [channels[0]/1000.0, channels[1]/1000.0, channels[2]/1000.0,
                                  channels[3]/1000.0, channels[4]/1000.0, channels[5]/1000.0,
                                  channels[6]/10.0,   channels[7]/10.0,   channels[8]/10.0,
                                  channels[9]/1000.0, channels[10]/1000.0, channels[11]/1000.0]
                        csv_writers[assigned_id].writerow([ts, assigned_id] + scaled)
            else:
                try:
                    text = data.decode('utf-8').strip()
                    if text.startswith("SEARCH_ACK"):
                        parts = text.split(" ")
                        if len(parts) >= 3:
                            mac = parts[1].upper()
                            dev_name = parts[3] if len(parts) >= 4 else ""
                            ip_to_mac[ip] = mac
                            if mac in KNOWN_DEVICES:
                                assigned_id = KNOWN_DEVICES[mac]
                                if mac not in discovered_devices:
                                    discovered_devices[mac] = {'id': assigned_id, 'ip': ip, 'name': dev_name}
                                    log_msg(f"Found ID:{assigned_id} [{dev_name}] IP:{ip}")
                                    get_device_buffers(assigned_id)
                                elif dev_name and not discovered_devices[mac].get('name'):
                                    discovered_devices[mac]['name'] = dev_name
                    elif text.startswith("DEVICE_NAME"):
                        parts = text.split(" ")
                        if len(parts) >= 3:
                            dev_id = int(parts[1])
                            dev_name = parts[2]
                            # Update name in discovered_devices
                            for mac, info in discovered_devices.items():
                                if info['id'] == dev_id:
                                    info['name'] = dev_name
                                    break
                            log_msg(f"Device ID:{dev_id} name: {dev_name}")
                except UnicodeDecodeError:
                    pass

        except socket.timeout:
            continue
        except Exception as e:
            if running:
                log_msg(f"Error: {e}")
            break


def close_all_csvs():
    global csv_files, csv_writers
    for f in csv_files.values():
        f.close()
    csv_files.clear()
    csv_writers.clear()


def _reset_plot_buffers():
    """グラフバッファをリセット"""
    with data_lock:
        for buf in plot_data.values():
            buf['t'].clear()
            buf['fx'].clear()
            buf['fy'].clear()
            buf['fz'].clear()
            buf['vx'].clear()
            buf['vy'].clear()
            buf['vz'].clear()


def cmd_start(event=None):
    global is_measuring, is_free_run, current_data_dir
    if is_measuring or is_free_run:
        return
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = os.path.join(save_base_dir, now_str)
    # 重複時は (1), (2), ... を付与
    if os.path.exists(candidate):
        suffix = 1
        while os.path.exists(f"{candidate}({suffix})"):
            suffix += 1
        candidate = f"{candidate}({suffix})"
    current_data_dir = candidate
    os.makedirs(current_data_dir)

    _reset_plot_buffers()

    now_ms = int(time.time() * 1000)
    cmd = f"START {now_ms}".encode()
    log_msg(f"START (REC) -> {os.path.basename(current_data_dir)}/")
    sock.sendto(cmd, (BROADCAST_ADDR, CMD_PORT))
    sock.sendto(cmd, ("255.255.255.255", CMD_PORT))
    is_measuring = True


def cmd_free_run(event=None):
    global is_free_run, is_measuring
    if is_measuring or is_free_run:
        return

    _reset_plot_buffers()

    now_ms = int(time.time() * 1000)
    cmd = f"START {now_ms}".encode()
    log_msg("FREE RUN (no recording)")
    sock.sendto(cmd, (BROADCAST_ADDR, CMD_PORT))
    sock.sendto(cmd, ("255.255.255.255", CMD_PORT))
    is_free_run = True


def cmd_stop(event=None):
    global is_measuring, is_free_run
    if not is_measuring and not is_free_run:
        return
    sock.sendto(b"STOP", (BROADCAST_ADDR, CMD_PORT))
    sock.sendto(b"STOP", ("255.255.255.255", CMD_PORT))
    was_recording = is_measuring
    is_measuring = False
    is_free_run = False
    close_all_csvs()
    if was_recording:
        log_msg("STOP -> CSV saved")
    else:
        log_msg("STOP (free run)")


def cmd_search(event=None):
    log_msg("Searching...")
    sock.sendto(b"SEARCH", (BROADCAST_ADDR, CMD_PORT))
    sock.sendto(b"SEARCH", ("255.255.255.255", CMD_PORT))


def cmd_rate(text):
    try:
        hz = int(text)
        if hz < 1 or hz > 1000:
            log_msg("Rate: 1-1000Hz")
            return
        cmd = f"RATE {hz}".encode()
        sock.sendto(cmd, (BROADCAST_ADDR, CMD_PORT))
        sock.sendto(cmd, ("255.255.255.255", CMD_PORT))
        log_msg(f"Rate → {hz} Hz")
    except ValueError:
        log_msg("Rate: integer only")


def cmd_toggle_display(event=None):
    """荷重(N) ↔ 電圧(V) の表示切替"""
    global display_mode, current_device_count
    if display_mode == 'force':
        display_mode = 'voltage'
        btn_toggle.label.set_text('Mode: V')
        log_msg("Display → Voltage (V)")
    else:
        display_mode = 'force'
        btn_toggle.label.set_text('Mode: N')
        log_msg("Display → Force (N)")
    # グラフを再構築（Y軸範囲・ラベル変更のため）
    with data_lock:
        rebuild_graphs(current_device_count)


# ==========================================
# GUI (matplotlib only)
# ==========================================
# レイアウト：グラフ領域を最大化、下部にコンパクトなコントロールパネル
mpl.rcParams['toolbar'] = 'None'
fig = plt.figure(figsize=(14, 9), facecolor='#f5f5f5')
fig.canvas.manager.set_window_title('M5Stack Multi-Device Monitor v3')

# ---------- ボタン配置 ----------
# ボタンの色
COLOR_START = '#4CAF50'
COLOR_START_HOVER = '#66BB6A'
COLOR_STOP = '#F44336'
COLOR_STOP_HOVER = '#EF5350'
COLOR_SEARCH = '#2196F3'
COLOR_SEARCH_HOVER = '#42A5F5'
COLOR_RATE = '#FF9800'
COLOR_RATE_HOVER = '#FFA726'
COLOR_FREERUN = '#9C27B0'
COLOR_FREERUN_HOVER = '#AB47BC'
COLOR_TOGGLE = '#00897B'
COLOR_TOGGLE_HOVER = '#26A69A'

# =================================================================
# 下部レイアウト設計 (6台接続時も埋もれないよう3行に整理)
# =================================================================
# Row 3 (最下段): y=0.01  デバイス一覧 + ログ (h=0.05相当テキスト)
# Row 2 (中段):   y=0.07  Browse + Save path + Indicator
# Row 1 (上段):   y=0.13  START / FREE RUN / STOP / SEARCH / V/N切替 / Rate / Window
# グラフ領域:     y=0.21 ~ 0.97
# =================================================================

BTN_H = 0.035       # ボタン高さ（コンパクト化）
ROW1_Y = 0.135      # ボタン行
ROW2_Y = 0.085      # Browse行
ROW3_Y = 0.005      # ステータス行

# --- Row 1: START / FREE RUN / STOP / SEARCH / V/N切替 ---
ax_start = fig.add_axes([0.06, ROW1_Y, 0.07, BTN_H])
btn_start = Button(ax_start, 'START', color=COLOR_START, hovercolor=COLOR_START_HOVER)
btn_start.label.set_fontsize(9)
btn_start.label.set_color('white')
btn_start.label.set_fontweight('bold')
btn_start.on_clicked(cmd_start)

ax_freerun = fig.add_axes([0.135, ROW1_Y, 0.085, BTN_H])
btn_freerun = Button(ax_freerun, 'FREE RUN', color=COLOR_FREERUN, hovercolor=COLOR_FREERUN_HOVER)
btn_freerun.label.set_fontsize(9)
btn_freerun.label.set_color('white')
btn_freerun.label.set_fontweight('bold')
btn_freerun.on_clicked(cmd_free_run)

ax_stop = fig.add_axes([0.225, ROW1_Y, 0.07, BTN_H])
btn_stop = Button(ax_stop, 'STOP', color=COLOR_STOP, hovercolor=COLOR_STOP_HOVER)
btn_stop.label.set_fontsize(9)
btn_stop.label.set_color('white')
btn_stop.label.set_fontweight('bold')
btn_stop.on_clicked(cmd_stop)

ax_search = fig.add_axes([0.30, ROW1_Y, 0.07, BTN_H])
btn_search = Button(ax_search, 'SEARCH', color=COLOR_SEARCH, hovercolor=COLOR_SEARCH_HOVER)
btn_search.label.set_fontsize(9)
btn_search.label.set_color('white')
btn_search.label.set_fontweight('bold')
btn_search.on_clicked(cmd_search)

# V/N 切替ボタン
ax_toggle = fig.add_axes([0.375, ROW1_Y, 0.07, BTN_H])
btn_toggle = Button(ax_toggle, 'Mode: N', color=COLOR_TOGGLE, hovercolor=COLOR_TOGGLE_HOVER)
btn_toggle.label.set_fontsize(9)
btn_toggle.label.set_color('white')
btn_toggle.label.set_fontweight('bold')
btn_toggle.on_clicked(cmd_toggle_display)

# --- Row 1 right side: Rate / Time Window ---
fig.text(0.46, ROW1_Y + BTN_H / 2, 'Rate:', fontsize=9, fontweight='bold', va='center')
ax_rate_input = fig.add_axes([0.49, ROW1_Y, 0.04, BTN_H])
textbox_rate = TextBox(ax_rate_input, '', initial='200')
textbox_rate.label.set_fontsize(9)
fig.text(0.533, ROW1_Y + BTN_H / 2, 'Hz', fontsize=9, va='center')

ax_rate_btn = fig.add_axes([0.55, ROW1_Y, 0.04, BTN_H])
btn_rate = Button(ax_rate_btn, 'Set', color=COLOR_RATE, hovercolor=COLOR_RATE_HOVER)
btn_rate.label.set_fontsize(9)
btn_rate.label.set_fontweight('bold')
btn_rate.on_clicked(lambda event: cmd_rate(textbox_rate.text))

# --- Time Window control ---
def cmd_set_time_window(text):
    global DISPLAY_TIME_WINDOW
    try:
        val = float(text)
        if val < 1.0 or val > 300.0:
            log_msg("Time window: 1-300s")
            return
        DISPLAY_TIME_WINDOW = val
        log_msg(f"Time window -> {val}s")
    except ValueError:
        log_msg("Time window: number only")

fig.text(0.60, ROW1_Y + BTN_H / 2, 'Window:', fontsize=9, fontweight='bold', va='center')
ax_tw_input = fig.add_axes([0.65, ROW1_Y, 0.04, BTN_H])
textbox_tw = TextBox(ax_tw_input, '', initial=str(int(DISPLAY_TIME_WINDOW)))
textbox_tw.label.set_fontsize(9)
fig.text(0.693, ROW1_Y + BTN_H / 2, 's', fontsize=9, va='center')

ax_tw_btn = fig.add_axes([0.705, ROW1_Y, 0.04, BTN_H])
btn_tw = Button(ax_tw_btn, 'Set', color='#607D8B', hovercolor='#78909C')
btn_tw.label.set_fontsize(9)
btn_tw.label.set_fontweight('bold')
btn_tw.label.set_color('white')
btn_tw.on_clicked(lambda event: cmd_set_time_window(textbox_tw.text))

# --- Row 2: Save folder ---
def cmd_browse(event=None):
    """Open native folder picker (cross-platform)"""
    global save_base_dir
    try:
        if sys.platform == 'darwin':
            # macOS: osascript でネイティブフォルダ選択
            result = subprocess.run(
                ['osascript', '-e',
                 'POSIX path of (choose folder with prompt "Select save folder")'],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0 and result.stdout.strip():
                save_base_dir = result.stdout.strip()
                save_path_text.set_text(f'Save to: {save_base_dir}')
                log_msg(f"Save dir: {save_base_dir}")
        else:
            # Windows / Linux: tkinter でフォルダ選択
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            folder = filedialog.askdirectory(
                title="Select save folder",
                initialdir=save_base_dir
            )
            root.destroy()
            if folder:
                save_base_dir = folder
                save_path_text.set_text(f'Save to: {save_base_dir}')
                log_msg(f"Save dir: {save_base_dir}")
    except Exception as e:
        log_msg(f"Browse error: {e}")

ax_browse = fig.add_axes([0.06, ROW2_Y, 0.07, BTN_H])
btn_browse = Button(ax_browse, 'Browse...', color='#607D8B', hovercolor='#78909C')
btn_browse.label.set_fontsize(9)
btn_browse.label.set_color('white')
btn_browse.label.set_fontweight('bold')
btn_browse.on_clicked(cmd_browse)

save_path_text = fig.text(0.135, ROW2_Y + BTN_H / 2, f'Save to: {save_base_dir}',
                          fontsize=8, fontfamily='monospace', va='center',
                          color='#333333')

# 計測状態インジケータ (Row 2 右端)
indicator_text = fig.text(0.85, ROW2_Y + BTN_H / 2, '', fontsize=11, fontweight='bold',
                          va='center', color='#888888')

# ---------- ステータス表示エリア (Row 3) ----------
# デバイス一覧テキスト（下部左） - 小さいフォントで1行コンパクト表示
status_text = fig.text(0.06, ROW3_Y + 0.06, '', fontsize=7, fontfamily='monospace',
                       va='top', color='#333333')
# ログテキスト（下部右）
log_text_obj = fig.text(0.55, ROW3_Y + 0.06, '', fontsize=7, fontfamily='monospace',
                        va='top', color='#555555')

# ---------- グラフ領域 ----------
current_device_count = 0
device_lines = {}
graph_axes = []

# グラフ領域の範囲定義（限界まで大きく）
GRAPH_BOTTOM = 0.19  # ボタン行のすぐ上
GRAPH_TOP = 0.97     # ウィンドウ上端ギリギリ


def rebuild_graphs(num_devices):
    """デバイス数に合わせてサブプロットを再構築"""
    # 既存のグラフ用axesだけを削除
    for ax in graph_axes:
        fig.delaxes(ax)
    device_lines.clear()
    graph_axes.clear()

    if num_devices == 0:
        return

    graph_height = GRAPH_TOP - GRAPH_BOTTOM
    # デバイス数が増えてもギャップを最小限に
    gap = max(0.01, 0.03 - 0.004 * num_devices)
    each_height = (graph_height - gap * (num_devices - 1)) / num_devices

    # 表示モードに応じたY軸範囲とラベル
    if display_mode == 'voltage':
        y_min, y_max = V_AXIS_MIN, V_AXIS_MAX
        unit_label = '[V]'
        line_labels = ('ch1 (V)', 'ch2 (V)', 'ch3 (V)')
    else:
        y_min, y_max = Y_AXIS_MIN, Y_AXIS_MAX
        unit_label = '[N]'
        line_labels = ('Force X', 'Force Y', 'Force Z')

    sorted_ids = sorted(plot_data.keys())
    for idx, assigned_id in enumerate(sorted_ids):
        bottom_pos = GRAPH_TOP - (idx + 1) * each_height - idx * gap
        ax = fig.add_axes([0.06, bottom_pos, 0.92, each_height])
        l_x, = ax.plot([], [], '#FF5252', label=line_labels[0], linewidth=1.2)
        l_y, = ax.plot([], [], '#4CAF50', label=line_labels[1], linewidth=1.2)
        l_z, = ax.plot([], [], '#2196F3', label=line_labels[2], linewidth=1.2)
        ax.set_ylim(y_min, y_max)
        # Build label with device name if available
        dev_label = f"ID:{assigned_id}"
        for mac, info in discovered_devices.items():
            if info['id'] == assigned_id and info.get('name'):
                dev_label = f"{info['name']} (ID:{assigned_id})"
                break
        ax.set_ylabel(f"{dev_label} {unit_label}", fontsize=9)
        ax.grid(True, linestyle='--', alpha=0.5)
        ax.set_facecolor('#fafafa')
        # X軸を時刻表示 (HH:MM:SS)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.tick_params(axis='both', labelsize=8)
        if idx == 0:
            ax.legend(loc='upper right', fontsize=8)
        if idx == num_devices - 1:
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=0, ha='center', fontsize=8)
        else:
            ax.set_xticklabels([])

        device_lines[assigned_id] = (l_x, l_y, l_z, ax)
        graph_axes.append(ax)


def update(frame):
    """アニメーション更新関数"""
    global current_device_count

    # 表示するデータキーを選択
    if display_mode == 'voltage':
        key_x, key_y, key_z = 'vx', 'vy', 'vz'
    else:
        key_x, key_y, key_z = 'fx', 'fy', 'fz'

    # --- データのスナップショット取得 ---
    plot_copies = {}
    now_abs = time.time()
    with data_lock:
        if len(plot_data) > current_device_count:
            current_device_count = len(plot_data)
            rebuild_graphs(current_device_count)

        for assigned_id in device_lines.keys():
            if assigned_id in plot_data:
                t_all = list(plot_data[assigned_id]['t'])
                dx_all = list(plot_data[assigned_id][key_x])
                dy_all = list(plot_data[assigned_id][key_y])
                dz_all = list(plot_data[assigned_id][key_z])

                # 時間窓フィルタ: 最新から DISPLAY_TIME_WINDOW 秒分だけ取得
                if len(t_all) > 0:
                    t_min = now_abs - DISPLAY_TIME_WINDOW
                    # bisect的に開始インデックスを探す
                    start_idx = 0
                    for i, tv in enumerate(t_all):
                        if tv >= t_min:
                            start_idx = i
                            break
                    t_win = t_all[start_idx:]
                    dx_win = dx_all[start_idx:]
                    dy_win = dy_all[start_idx:]
                    dz_win = dz_all[start_idx:]

                    # 自動間引き: 絶対時間グリッド方式（過去の点が変わらない）
                    n_win = len(t_win)
                    if n_win > MAX_DISPLAY_POINTS:
                        # 時間グリッドの間隔を算出
                        t_grid_step = DISPLAY_TIME_WINDOW / MAX_DISPLAY_POINTS
                        t_dec = []
                        dx_dec = []
                        dy_dec = []
                        dz_dec = []
                        # 次にサンプルを取るべき時刻（グリッドをt_min基準で固定）
                        next_grid = t_min + t_grid_step
                        # 最初の点は必ず含める
                        t_dec.append(t_win[0])
                        dx_dec.append(dx_win[0])
                        dy_dec.append(dy_win[0])
                        dz_dec.append(dz_win[0])
                        for i in range(1, n_win):
                            if t_win[i] >= next_grid:
                                t_dec.append(t_win[i])
                                dx_dec.append(dx_win[i])
                                dy_dec.append(dy_win[i])
                                dz_dec.append(dz_win[i])
                                # 次のグリッド点へ（スキップされたグリッドも飛ばす）
                                while next_grid <= t_win[i]:
                                    next_grid += t_grid_step
                    else:
                        t_dec = t_win
                        dx_dec = dx_win
                        dy_dec = dy_win
                        dz_dec = dz_win

                    # Unix時間をmatplotlibのdatetimeに変換
                    t_dates = [datetime.fromtimestamp(ts) for ts in t_dec]
                    t_mpl = mdates.date2num(t_dates)
                    plot_copies[assigned_id] = {
                        't': t_mpl,
                        'dx': dx_dec,
                        'dy': dy_dec,
                        'dz': dz_dec
                    }

    # --- グラフ更新（ロック外） ---
    # X軸範囲を現在時刻基準で固定
    t_end_dt = datetime.fromtimestamp(now_abs)
    t_start_dt = datetime.fromtimestamp(now_abs - DISPLAY_TIME_WINDOW)
    xlim_start = mdates.date2num(t_start_dt)
    xlim_end = mdates.date2num(t_end_dt)

    for assigned_id, (l_x, l_y, l_z, ax) in device_lines.items():
        if assigned_id in plot_copies:
            t = plot_copies[assigned_id]['t']
            if len(t) > 1:
                l_x.set_data(t, plot_copies[assigned_id]['dx'])
                l_y.set_data(t, plot_copies[assigned_id]['dy'])
                l_z.set_data(t, plot_copies[assigned_id]['dz'])
        # 常に現在時刻基準でX軸を設定
        ax.set_xlim(xlim_start, xlim_end)

    # --- ステータス表示更新 ---
    # デバイス一覧（コンパクト1行形式）
    if discovered_devices:
        dev_parts = []
        for mac, info in discovered_devices.items():
            name_str = f"[{info.get('name', '')}]" if info.get('name') else ""
            dev_parts.append(f"ID:{info['id']}{name_str} {info['ip']}")
        status_text.set_text("Devices: " + "  |  ".join(dev_parts))
    else:
        status_text.set_text("Devices: (none found)")

    # ログ表示（最新4行のみコンパクト表示）
    recent_logs = list(log_lines)[-4:]
    log_text_obj.set_text('\n'.join(recent_logs))

    # 計測状態インジケータ
    if is_measuring:
        indicator_text.set_text('\u25cf MEASURING (REC)')
        indicator_text.set_color('#F44336')
    elif is_free_run:
        indicator_text.set_text('\u25cf FREE RUN')
        indicator_text.set_color('#9C27B0')
    else:
        indicator_text.set_text('\u25cf IDLE')
        indicator_text.set_color('#888888')

    return [l for tup in device_lines.values() for l in tup[:3]]


# アニメーション（10FPS）
ani = animation.FuncAnimation(fig, update, interval=ANIMATION_INTERVAL, blit=False, cache_frame_data=False)


def on_close(event):
    """ウィンドウを閉じたときの後処理"""
    global running
    running = False
    close_all_csvs()
    try:
        sock.close()
    except:
        pass


fig.canvas.mpl_connect('close_event', on_close)

# 受信スレッド開始
threading.Thread(target=receive_loop, daemon=True).start()

# 1秒後に自動検索
def delayed_search():
    time.sleep(1.0)
    cmd_search()

threading.Thread(target=delayed_search, daemon=True).start()

# GUI起動
if __name__ == '__main__':
    log_msg("Ready - waiting for devices")
    plt.show()

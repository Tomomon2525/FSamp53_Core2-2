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

csv_files = {}
csv_writers = {}
discovered_devices = {}  # {MAC: {'id': ID, 'ip': IP}}
ip_to_mac = {}

plot_data = {}  # { assigned_id: {'t': deque, 'fx': deque, 'fy': deque, 'fz': deque} }
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
                'fz': deque(maxlen=MAX_POINTS)
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
                        bufs['fx'].append(channels[9] / 1000.0)
                        bufs['fy'].append(channels[10] / 1000.0)
                        bufs['fz'].append(channels[11] / 1000.0)

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


# ==========================================
# GUI (matplotlib only)
# ==========================================
# レイアウト：上部にグラフ領域、下部にボタン＆情報
mpl.rcParams['toolbar'] = 'None'
fig = plt.figure(figsize=(12, 7), facecolor='#f5f5f5')
fig.canvas.manager.set_window_title('M5Stack Multi-Device Monitor')

# グラフ領域を確保（下部にボタン用スペースを空ける）
fig.subplots_adjust(left=0.08, right=0.98, top=0.95, bottom=0.32)

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

# --- Row 1: START / FREE RUN / STOP / SEARCH ---
ax_start = fig.add_axes([0.08, 0.20, 0.10, 0.06])
btn_start = Button(ax_start, 'START', color=COLOR_START, hovercolor=COLOR_START_HOVER)
btn_start.label.set_fontsize(11)
btn_start.label.set_color('white')
btn_start.label.set_fontweight('bold')
btn_start.on_clicked(cmd_start)

ax_freerun = fig.add_axes([0.19, 0.20, 0.12, 0.06])
btn_freerun = Button(ax_freerun, 'FREE RUN', color=COLOR_FREERUN, hovercolor=COLOR_FREERUN_HOVER)
btn_freerun.label.set_fontsize(11)
btn_freerun.label.set_color('white')
btn_freerun.label.set_fontweight('bold')
btn_freerun.on_clicked(cmd_free_run)

ax_stop = fig.add_axes([0.32, 0.20, 0.10, 0.06])
btn_stop = Button(ax_stop, 'STOP', color=COLOR_STOP, hovercolor=COLOR_STOP_HOVER)
btn_stop.label.set_fontsize(11)
btn_stop.label.set_color('white')
btn_stop.label.set_fontweight('bold')
btn_stop.on_clicked(cmd_stop)

ax_search = fig.add_axes([0.43, 0.20, 0.10, 0.06])
btn_search = Button(ax_search, 'SEARCH', color=COLOR_SEARCH, hovercolor=COLOR_SEARCH_HOVER)
btn_search.label.set_fontsize(11)
btn_search.label.set_color('white')
btn_search.label.set_fontweight('bold')
btn_search.on_clicked(cmd_search)

# --- Row 1 right side: Rate / Time Window ---
fig.text(0.56, 0.225, 'Rate:', fontsize=10, fontweight='bold', va='center')
ax_rate_input = fig.add_axes([0.60, 0.20, 0.06, 0.06])
textbox_rate = TextBox(ax_rate_input, '', initial='200')
textbox_rate.label.set_fontsize(10)
fig.text(0.665, 0.225, 'Hz', fontsize=10, va='center')

ax_rate_btn = fig.add_axes([0.69, 0.20, 0.06, 0.06])
btn_rate = Button(ax_rate_btn, 'Set', color=COLOR_RATE, hovercolor=COLOR_RATE_HOVER)
btn_rate.label.set_fontsize(10)
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

fig.text(0.77, 0.225, 'Window:', fontsize=10, fontweight='bold', va='center')
ax_tw_input = fig.add_axes([0.83, 0.20, 0.06, 0.06])
textbox_tw = TextBox(ax_tw_input, '', initial=str(int(DISPLAY_TIME_WINDOW)))
textbox_tw.label.set_fontsize(10)
fig.text(0.895, 0.225, 's', fontsize=10, va='center')

ax_tw_btn = fig.add_axes([0.91, 0.20, 0.05, 0.06])
btn_tw = Button(ax_tw_btn, 'Set', color='#607D8B', hovercolor='#78909C')
btn_tw.label.set_fontsize(10)
btn_tw.label.set_fontweight('bold')
btn_tw.label.set_color('white')
btn_tw.on_clicked(lambda event: cmd_set_time_window(textbox_tw.text))

# ---------- ステータス表示エリア ----------
# デバイス一覧テキスト（下部左）
status_text = fig.text(0.08, 0.07, '', fontsize=9, fontfamily='monospace',
                       va='top', color='#333333')
log_text_obj = fig.text(0.55, 0.1, '', fontsize=9, fontfamily='monospace',
                        va='top', color='#555555')
# 計測状態インジケータ
indicator_text = fig.text(0.85, 0.14, '', fontsize=13, fontweight='bold',
                          va='center', color='#888888')

# ---------- Save folder row ----------
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

ax_browse = fig.add_axes([0.08, 0.13, 0.10, 0.05])
btn_browse = Button(ax_browse, 'Browse...', color='#607D8B', hovercolor='#78909C')
btn_browse.label.set_fontsize(10)
btn_browse.label.set_color('white')
btn_browse.label.set_fontweight('bold')
btn_browse.on_clicked(cmd_browse)

save_path_text = fig.text(0.19, 0.155, f'Save to: {save_base_dir}',
                          fontsize=9, fontfamily='monospace', va='center',
                          color='#333333')

# ---------- グラフ領域 ----------
current_device_count = 0
device_lines = {}
graph_axes = []


def rebuild_graphs(num_devices):
    """デバイス数に合わせてサブプロットを再構築"""
    # 既存のグラフ用axesだけを削除
    for ax in graph_axes:
        fig.delaxes(ax)
    device_lines.clear()
    graph_axes.clear()

    if num_devices == 0:
        return

    # グラフ領域の上下 (bottom=0.28, top=0.95)
    graph_bottom = 0.32
    graph_top = 0.95
    graph_height = graph_top - graph_bottom
    gap = 0.04
    each_height = (graph_height - gap * (num_devices - 1)) / num_devices

    sorted_ids = sorted(plot_data.keys())
    for idx, assigned_id in enumerate(sorted_ids):
        bottom_pos = graph_top - (idx + 1) * each_height - idx * gap
        ax = fig.add_axes([0.08, bottom_pos, 0.90, each_height])
        l_x, = ax.plot([], [], '#FF5252', label='Force X', linewidth=1.2)
        l_y, = ax.plot([], [], '#4CAF50', label='Force Y', linewidth=1.2)
        l_z, = ax.plot([], [], '#2196F3', label='Force Z', linewidth=1.2)
        ax.set_ylim(Y_AXIS_MIN, Y_AXIS_MAX)
        # Build label with device name if available
        dev_label = f"ID:{assigned_id}"
        for mac, info in discovered_devices.items():
            if info['id'] == assigned_id and info.get('name'):
                dev_label = f"{info['name']} (ID:{assigned_id})"
                break
        ax.set_ylabel(f"{dev_label} [N]", fontsize=11)
        ax.grid(True, linestyle='--', alpha=0.5)
        ax.set_facecolor('#fafafa')
        # X軸を時刻表示 (HH:MM:SS)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        if idx == 0:
            ax.legend(loc='upper right', fontsize=9)
        if idx == num_devices - 1:
            ax.set_xlabel("Time", fontsize=11)
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=0, ha='center', fontsize=9)
        else:
            ax.set_xticklabels([])

        device_lines[assigned_id] = (l_x, l_y, l_z, ax)
        graph_axes.append(ax)


def update(frame):
    """アニメーション更新関数"""
    global current_device_count

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
                fx_all = list(plot_data[assigned_id]['fx'])
                fy_all = list(plot_data[assigned_id]['fy'])
                fz_all = list(plot_data[assigned_id]['fz'])

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
                    fx_win = fx_all[start_idx:]
                    fy_win = fy_all[start_idx:]
                    fz_win = fz_all[start_idx:]

                    # 自動間引き: 絶対時間グリッド方式（過去の点が変わらない）
                    n_win = len(t_win)
                    if n_win > MAX_DISPLAY_POINTS:
                        # 時間グリッドの間隔を算出
                        t_grid_step = DISPLAY_TIME_WINDOW / MAX_DISPLAY_POINTS
                        t_dec = []
                        fx_dec = []
                        fy_dec = []
                        fz_dec = []
                        # 次にサンプルを取るべき時刻（グリッドをt_min基準で固定）
                        next_grid = t_min + t_grid_step
                        # 最初の点は必ず含める
                        t_dec.append(t_win[0])
                        fx_dec.append(fx_win[0])
                        fy_dec.append(fy_win[0])
                        fz_dec.append(fz_win[0])
                        for i in range(1, n_win):
                            if t_win[i] >= next_grid:
                                t_dec.append(t_win[i])
                                fx_dec.append(fx_win[i])
                                fy_dec.append(fy_win[i])
                                fz_dec.append(fz_win[i])
                                # 次のグリッド点へ（スキップされたグリッドも飛ばす）
                                while next_grid <= t_win[i]:
                                    next_grid += t_grid_step
                    else:
                        t_dec = t_win
                        fx_dec = fx_win
                        fy_dec = fy_win
                        fz_dec = fz_win

                    # Unix時間をmatplotlibのdatetimeに変換
                    t_dates = [datetime.fromtimestamp(ts) for ts in t_dec]
                    t_mpl = mdates.date2num(t_dates)
                    plot_copies[assigned_id] = {
                        't': t_mpl,
                        'fx': fx_dec,
                        'fy': fy_dec,
                        'fz': fz_dec
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
                l_x.set_data(t, plot_copies[assigned_id]['fx'])
                l_y.set_data(t, plot_copies[assigned_id]['fy'])
                l_z.set_data(t, plot_copies[assigned_id]['fz'])
        # 常に現在時刻基準でX軸を設定
        ax.set_xlim(xlim_start, xlim_end)

    # --- ステータス表示更新 ---
    # デバイス一覧
    if discovered_devices:
        dev_lines = ["Devices:"]
        for mac, info in discovered_devices.items():
            name_str = f"  [{info.get('name', '')}]" if info.get('name') else ""
            dev_lines.append(f"  ID:{info['id']}{name_str}  IP:{info['ip']}  MAC:{mac}")
        status_text.set_text('\n'.join(dev_lines))
    else:
        status_text.set_text("Devices: (none found)")

    # ログ表示
    log_text_obj.set_text('\n'.join(log_lines))

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

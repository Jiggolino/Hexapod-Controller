#!/usr/bin/env python3
"""
hexapod_control.py - Hexapod Remote Control Client
Runs on laptop/viewing device
Connects to Pi5 relay, sends controller input, receives camera stream & telemetry
Modern dark-mode UI with floating info panels
"""

import tkinter as tk
from tkinter import ttk
import pygame
import threading
import socket
import time
import sys
import struct
import os
import glob
import re
import subprocess
from collections import deque
from datetime import datetime

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo, GLib

Gst.init(None)

# ── Legion Go back-paddle key codes (EV_KEY codes on the "Keyboard" evdev device)
# Run with the debug panel open to see which codes fire when you press each button,
# then update these constants to match.
BACK_KEY_M1 = 152   # KEY_PROG1
BACK_KEY_M2 = 153   # KEY_PROG2
BACK_KEY_M3 = 154   # KEY_PROG3
BACK_KEY_Y1 = 155   # KEY_PROG4
BACK_KEY_Y2 = 156   # KEY_PROG5 (try 140 / KEY_CALC if wrong)
BACK_KEY_Y3 = 157   # KEY_PROG6

# evdev input_event layout on 64-bit Linux: timeval(8+8) + type(2) + code(2) + value(4) = 24 bytes
_EV_FMT  = 'llHHi'
_EV_SIZE = struct.calcsize(_EV_FMT)
EV_KEY   = 0x01

def _find_legion_keyboard_device():
    """Return path to Legion keyboard evdev node, or None."""
    for path in glob.glob('/dev/input/event*'):
        try:
            with open(path, 'rb') as f:
                import fcntl, array
                EVIOCGNAME = 0x81004506
                buf = array.array('B', [0] * 256)
                fcntl.ioctl(f, EVIOCGNAME, buf)
                name = bytes(buf).rstrip(b'\x00').decode('utf-8', errors='ignore')
                if 'legion' in name.lower() and 'keyboard' in name.lower():
                    return path
        except Exception:
            continue
    return None


class HexapodControl:
    # Colors - Dark Mode
    BG_DARK    = "#1e1e1e"
    BG_PANEL   = "#2d2d2d"
    BG_HOVER   = "#3d3d3d"
    FG_TEXT    = "#e0e0e0"
    FG_ACCENT  = "#00d9ff"
    FG_SUCCESS = "#00ff41"
    FG_WARNING = "#ffaa00"
    FG_ERROR   = "#ff3333"
    BORDER_COLOR = "#404040"

    def __init__(self, root):
        self.root = root
        self.root.title("Hexapod Remote Control")
        self.root.configure(bg=self.BG_DARK)
        self.root.attributes('-fullscreen', True)

        # Configuration - Tailscale IPs
        self.RELAY_HOST             = "100.122.9.49"
        self.RELAY_PORT             = 5000
        self.SPEAKER_PORT           = 5001
        self.CAMERA_PORT            = 5005
        self.CONTROLLER_SEND_RATE   = 50   # Hz
        self.BATTERY_UPDATE_INTERVAL = 1.0

        # Video stream
        self._gst_pipeline = None
        self._video_xid    = None

        # Controller axis mapping
        self.DEAD  = 0.12
        self.AX_LX, self.AX_LY = 0, 1
        self.AX_RX, self.AX_RY = 3, 4

        # Battery EMA filter — same principle as IMU complementary filter.
        # alpha near 0 = heavy smoothing; alpha near 1 = fast tracking.
        self.BATT_ALPHA      = 0.1
        self._batt_filtered  = None   # None until first reading

        # State
        self.running         = True
        self.connected       = False
        self.socket          = None
        self.joystick        = None
        self.battery_voltage = 0.0
        self.battery_percent = 0
        self.packet_count    = 0
        self.fps_counter     = 0
        self.msg_log         = deque(maxlen=50)

        # Back-paddle state (read from Legion keyboard evdev device)
        self._back_state = {
            BACK_KEY_M1: False,
            BACK_KEY_M2: False,
            BACK_KEY_M3: False,
            BACK_KEY_Y1: False,
            BACK_KEY_Y2: False,
            BACK_KEY_Y3: False,
        }
        self._last_raw_key = None   # shown in debug panel

        pygame.init()
        pygame.joystick.init()

        self._configure_style()
        self._setup_ui()
        self._find_controller()
        self._connect_relay()
        self._start_threads()

        self._update_fps_display()

    # ──────────────────────────────────────────────
    # UI setup
    # ──────────────────────────────────────────────

    def _configure_style(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame',       background=self.BG_DARK,  foreground=self.FG_TEXT)
        style.configure('TLabel',       background=self.BG_DARK,  foreground=self.FG_TEXT)
        style.configure('TLabelframe',  background=self.BG_DARK,  foreground=self.FG_ACCENT,
                        bordercolor=self.BORDER_COLOR)
        style.configure('TLabelframe.Label', background=self.BG_DARK, foreground=self.FG_ACCENT)
        style.configure('TButton',      background=self.BG_PANEL, foreground=self.FG_TEXT)
        style.map('TButton',
                  background=[('active', self.BG_HOVER)],
                  foreground=[('active', self.FG_ACCENT)])
        style.configure('TProgressbar', background=self.FG_SUCCESS, troughcolor=self.BG_PANEL)

    def _setup_ui(self):
        main_container = ttk.Frame(self.root)
        main_container.pack(fill=tk.BOTH, expand=True)

        # Left – video area
        left_frame = ttk.Frame(main_container)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        video_title = tk.Frame(left_frame, bg=self.BG_PANEL, height=40)
        video_title.pack(fill=tk.X, padx=10, pady=(10, 0))
        video_title.pack_propagate(False)

        tk.Label(video_title, text="CAMERA STREAM", bg=self.BG_PANEL,
                 fg=self.FG_ACCENT, font=("Arial", 12, "bold"), pady=8).pack(side=tk.LEFT, padx=10)

        self.video_status = tk.Label(video_title, text="Waiting for stream...",
                                     bg=self.BG_PANEL, fg=self.FG_WARNING, font=("Arial", 9))
        self.video_status.pack(side=tk.LEFT, padx=10)

        # Leave button — top-right of title bar
        leave_btn = tk.Button(video_title, text="LEAVE", bg=self.FG_ERROR, fg="white",
                              font=("Arial", 10, "bold"), relief=tk.FLAT, padx=12,
                              command=self.on_closing)
        leave_btn.pack(side=tk.RIGHT, padx=10, pady=4)

        video_container = tk.Frame(left_frame, bg=self.BG_PANEL)
        video_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.video_frame = tk.Frame(video_container, bg=self.BG_PANEL, height=500)
        self.video_frame.pack(fill=tk.BOTH, expand=True)

        self.video_label = tk.Label(
            self.video_frame,
            text="[Connecting to stream on port 5005]\nMake sure relay is streaming with GStreamer",
            bg=self.BG_PANEL, fg=self.FG_TEXT, font=("Arial", 16))
        self.video_label.pack(fill=tk.BOTH, expand=True)


        # Command bar — spans full width below video
        cmd_bar = tk.Frame(left_frame, bg=self.BG_PANEL, height=44)
        cmd_bar.pack(fill=tk.X, padx=10, pady=(0, 4))
        cmd_bar.pack_propagate(False)

        tk.Label(cmd_bar, text="CMD:", bg=self.BG_PANEL, fg=self.FG_ACCENT,
                 font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=(10, 4), pady=8)

        self._osk       = None
        self._osk_entry = None
        self._cmd_var   = tk.StringVar()
        self.cmd_entry  = tk.Entry(cmd_bar,
                                   textvariable=self._cmd_var,
                                   bg=self.BG_HOVER, fg=self.FG_TEXT,
                                   insertbackground=self.FG_ACCENT,
                                   font=("Courier", 11), relief=tk.FLAT)
        self.cmd_entry.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=8)
        self.cmd_entry.bind("<Return>",   lambda _: self._send_command())
        self.cmd_entry.bind("<Button-1>", lambda _: self._show_osk())

        send_btn = tk.Button(cmd_bar, text="SEND", bg=self.FG_ACCENT, fg=self.BG_DARK,
                             font=("Arial", 10, "bold"), relief=tk.FLAT, padx=14,
                             command=self._send_command)
        send_btn.pack(side=tk.LEFT, padx=(4, 10), pady=8)

        # Right – info panels
        right_frame = tk.Frame(main_container, bg=self.BG_DARK, width=350)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=10, pady=10)
        right_frame.pack_propagate(False)

        self._create_status_panel(right_frame)
        self._create_battery_panel(right_frame)
        self._create_controller_panel(right_frame)
        self._create_stats_panel(right_frame)
        self._create_log_panel(right_frame)

        self._start_gstreamer_stream()

    def _create_panel(self, parent, title):
        panel = tk.Frame(parent, bg=self.BG_PANEL, relief=tk.SUNKEN, bd=1,
                         highlightthickness=2,
                         highlightbackground=self.BORDER_COLOR,
                         highlightcolor=self.FG_ACCENT)
        panel.pack(fill=tk.X, pady=8, padx=5)
        tk.Label(panel, text=title, bg=self.BG_PANEL, fg=self.FG_ACCENT,
                 font=("Arial", 10, "bold"), pady=5).pack(fill=tk.X, padx=8, pady=(5, 0))
        content = tk.Frame(panel, bg=self.BG_PANEL)
        content.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        return content

    def _create_status_panel(self, parent):
        panel = self._create_panel(parent, "CONNECTION")
        self.status_label = tk.Label(panel, text="DISCONNECTED",
                                     bg=self.BG_PANEL, fg=self.FG_ERROR,
                                     font=("Arial", 13, "bold"), pady=8)
        self.status_label.pack(fill=tk.X)
        self.mode_label = tk.Label(panel, text="Walk:  --",
                                   bg=self.BG_PANEL, fg=self.FG_ACCENT,
                                   font=("Courier", 10))
        self.mode_label.pack(fill=tk.X, pady=(0, 2))
        self.stab_label = tk.Label(panel, text="Stab:  --",
                                   bg=self.BG_PANEL, fg=self.FG_ACCENT,
                                   font=("Courier", 10))
        self.stab_label.pack(fill=tk.X, pady=(0, 2))
        self.roll_label = tk.Label(panel, text="Roll:  -- °",
                                   bg=self.BG_PANEL, fg=self.FG_WARNING,
                                   font=("Courier", 10))
        self.roll_label.pack(fill=tk.X, pady=(0, 2))
        self.pitch_label = tk.Label(panel, text="Pitch: -- °",
                                    bg=self.BG_PANEL, fg=self.FG_WARNING,
                                    font=("Courier", 10))
        self.pitch_label.pack(fill=tk.X, pady=(0, 2))
        self.tof_label = tk.Label(panel, text="Distance: -- mm",
                                  bg=self.BG_PANEL, fg=self.FG_ACCENT,
                                  font=("Courier", 10))
        self.tof_label.pack(fill=tk.X, pady=(0, 2))
        self.ping_label = tk.Label(panel, text="Ping: --",
                                   bg=self.BG_PANEL, fg=self.FG_TEXT,
                                   font=("Courier", 10))
        self.ping_label.pack(fill=tk.X, pady=(0, 4))

    def _create_battery_panel(self, parent):
        panel = self._create_panel(parent, "BATTERY")
        self.volt_label = tk.Label(panel, text="Voltage: -- V",
                                   bg=self.BG_PANEL, fg=self.FG_TEXT, font=("Courier", 10))
        self.volt_label.pack(anchor="w", pady=4)
        self.percent_label = tk.Label(panel, text="Capacity: -- %",
                                      bg=self.BG_PANEL, fg=self.FG_TEXT, font=("Courier", 10))
        self.percent_label.pack(anchor="w", pady=4)
        self.battery_bar = ttk.Progressbar(panel, length=320, mode='determinate', value=0)
        self.battery_bar.pack(fill=tk.X, pady=8)

    def _create_controller_panel(self, parent):
        panel = self._create_panel(parent, "CONTROLLER")

        def row(label_text):
            f = tk.Frame(panel, bg=self.BG_PANEL)
            f.pack(fill=tk.X, pady=2)
            tk.Label(f, text=label_text, bg=self.BG_PANEL, fg=self.FG_TEXT,
                     font=("Arial", 8, "bold")).pack(side=tk.LEFT)
            lbl = tk.Label(f, text="--", bg=self.BG_PANEL, fg=self.FG_ACCENT,
                           font=("Courier", 8))
            lbl.pack(side=tk.LEFT, padx=8)
            return lbl

        self.lstick_label  = row("L-Stick:")
        self.rstick_label  = row("R-Stick:")

        btns_frame = tk.Frame(panel, bg=self.BG_PANEL)
        btns_frame.pack(fill=tk.X, pady=4)
        tk.Label(btns_frame, text="Buttons:", bg=self.BG_PANEL, fg=self.FG_TEXT,
                 font=("Arial", 8, "bold")).pack(anchor="w")
        self.buttons_label = tk.Label(btns_frame, text="None",
                                      bg=self.BG_PANEL, fg=self.FG_TEXT,
                                      font=("Courier", 8), wraplength=300)
        self.buttons_label.pack(anchor="w", pady=2)

        dpad_frame = tk.Frame(panel, bg=self.BG_PANEL)
        dpad_frame.pack(fill=tk.X, pady=4)
        tk.Label(dpad_frame, text="D-Pad:", bg=self.BG_PANEL, fg=self.FG_TEXT,
                 font=("Arial", 8, "bold")).pack(side=tk.LEFT)
        self.dpad_label = tk.Label(dpad_frame, text="◯",
                                   bg=self.BG_PANEL, fg=self.FG_TEXT, font=("Arial", 10))
        self.dpad_label.pack(side=tk.LEFT, padx=10)


    def _create_stats_panel(self, parent):
        panel = self._create_panel(parent, "STATS")
        self.fps_label    = tk.Label(panel, text="FPS: 0",
                                     bg=self.BG_PANEL, fg=self.FG_ACCENT, font=("Courier", 9))
        self.fps_label.pack(anchor="w", pady=2)
        self.packet_label = tk.Label(panel, text="Packets: 0",
                                     bg=self.BG_PANEL, fg=self.FG_TEXT, font=("Courier", 9))
        self.packet_label.pack(anchor="w", pady=2)
        self.uptime_label = tk.Label(panel, text="Uptime: 0s",
                                     bg=self.BG_PANEL, fg=self.FG_TEXT, font=("Courier", 9))
        self.uptime_label.pack(anchor="w", pady=2)
        self.start_time = time.time()

    def _create_log_panel(self, parent):
        panel = self._create_panel(parent, "LOG")
        self.log_text = tk.Text(panel, height=20, width=40,
                                bg=self.BG_PANEL, fg=self.FG_TEXT,
                                insertbackground=self.FG_ACCENT,
                                font=("Courier", 7))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ──────────────────────────────────────────────
    # Startup helpers
    # ──────────────────────────────────────────────

    def _find_controller(self):
        count = pygame.joystick.get_count()
        if count == 0:
            self._log("No controller found")
            return
        for i in range(count):
            js = pygame.joystick.Joystick(i)
            js.init()
            if "legion" in js.get_name().lower():
                self.joystick = js
                self._log(f"Found: {js.get_name()} ({js.get_numbuttons()} btns)")
                return
        self.joystick = pygame.joystick.Joystick(0)
        self.joystick.init()
        self._log(f"Using: {self.joystick.get_name()}")

    def _connect_relay(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(3.0)
            self.socket.connect((self.RELAY_HOST, self.RELAY_PORT))
            self.connected = True
            self.status_label.config(text="CONNECTED", fg=self.FG_SUCCESS)
            self._log(f"Connected to relay {self.RELAY_HOST}:{self.RELAY_PORT}")
        except Exception as e:
            self.connected = False
            self.status_label.config(text="DISCONNECTED", fg=self.FG_ERROR)
            self._log(f"Relay error: {e}")

    # ──────────────────────────────────────────────
    # On-screen keyboard
    # ──────────────────────────────────────────────

    _OSK_ROWS_LOWER = [
        ['1','2','3','4','5','6','7','8','9','0','-','=','BKSP'],
        ['q','w','e','r','t','y','u','i','o','p','[',']'],
        ['a','s','d','f','g','h','j','k','l',';',"'",'ENTER'],
        ['SHIFT','z','x','c','v','b','n','m',',','.','/', 'SHIFT'],
        ['SPACE','@','#','!','?','(',')','+','=','CLOSE'],
    ]
    _OSK_ROWS_UPPER = [
        ['1','2','3','4','5','6','7','8','9','0','_','+','BKSP'],
        ['Q','W','E','R','T','Y','U','I','O','P','{','}'],
        ['A','S','D','F','G','H','J','K','L',':','"','ENTER'],
        ['SHIFT','Z','X','C','V','B','N','M','<','>','?', 'SHIFT'],
        ['SPACE','@','#','!','?','(',')','+','=','CLOSE'],
    ]

    # label, bg, fg, weight  (weight = multiplier of base key width)
    _KEY_STYLE = {
        'BKSP':  ('⌫',       '#c0392b', '#ffffff', 2),
        'ENTER': ('↵  ENTER', '#00b894', '#0d0d0d', 3),
        'SHIFT': ('⇧',        '#2c2c50', '#00d9ff', 2),
        'SPACE': ('SPACE',    '#2e2e2e', '#e0e0e0', 5),
        'CLOSE': ('✕  Close', '#c0392b', '#ffffff', 2),
    }

    def _show_osk(self):
        if self._osk and self._osk.winfo_exists():
            return
        self._osk_shift = False
        self._build_osk_window()

    def _build_osk_window(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        GAP    = 6
        MARGIN = 14
        KEY_H  = 62
        ROW_PY = 4
        HDR_H  = 36
        TXT_H  = 56

        kw   = sw - 20
        rows = self._OSK_ROWS_UPPER if self._osk_shift else self._OSK_ROWS_LOWER

        # Find widest row (most pixel units) and size keys to fill it
        best_units, best_gaps = 0, 0
        for row in rows:
            units = sum(self._KEY_STYLE.get(k, ('', '', '', 1))[3] for k in row)
            gaps  = len(row) - 1
            if units > best_units:
                best_units, best_gaps = units, gaps
        avail = kw - MARGIN * 2
        KEY_W = max(50, (avail - best_gaps * GAP) // best_units)

        n_rows = len(rows)
        kh = HDR_H + TXT_H + n_rows * (KEY_H + ROW_PY * 2) + MARGIN

        x = (sw - kw) // 2
        y = sh - kh - 80   # sit higher — leave 80 px gap at bottom

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.geometry(f"{kw}x{kh}+{x}+{y}")
        self._osk = win

        # ── Outer card ────────────────────────────────────
        card = tk.Frame(win, bg='#181828',
                        highlightbackground='#3a3a5a', highlightthickness=1)
        card.pack(fill=tk.BOTH, expand=True)

        # ── Header ───────────────────────────────────────
        hdr = tk.Frame(card, bg='#12122a', height=HDR_H)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Label(hdr, text='⌨   On-Screen Keyboard',
                 bg='#12122a', fg='#00d9ff',
                 font=('Arial', 11, 'bold')).pack(side=tk.LEFT, padx=14)

        # ── Embedded text bar ────────────────────────────
        txt_frame = tk.Frame(card, bg='#0d0d1a', height=TXT_H)
        txt_frame.pack(fill=tk.X, padx=MARGIN, pady=(8, 4))
        txt_frame.pack_propagate(False)

        # Mirror of cmd_entry — shares the same StringVar
        osk_entry = tk.Entry(
            txt_frame,
            textvariable=self._cmd_var,
            bg='#1e1e38', fg='#e0e0e0',
            insertbackground='#00d9ff',
            font=('Courier', 16),
            relief=tk.FLAT,
            bd=0
        )
        osk_entry.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        osk_entry.focus_set()
        # move cursor to end
        osk_entry.icursor(tk.END)
        self._osk_entry = osk_entry

        # ── Key rows ────────────────────────────────────
        keys_frame = tk.Frame(card, bg='#181828')
        keys_frame.pack(fill=tk.BOTH, expand=True, padx=MARGIN, pady=(4, MARGIN))

        for row_keys in rows:
            row_frame = tk.Frame(keys_frame, bg='#181828')
            row_frame.pack(anchor='center', pady=ROW_PY)
            self._build_osk_row(row_frame, row_keys, KEY_W, KEY_H, GAP)

    def _build_osk_row(self, parent, keys, key_w, key_h, gap):
        for key in keys:
            label, bg, fg, weight = self._KEY_STYLE.get(
                key, (key, '#2a2a40', '#e0e0e0', 1)
            )
            w = key_w * weight + gap * (weight - 1)

            cvs = tk.Canvas(parent, width=w, height=key_h,
                            bg='#181828', highlightthickness=0)
            cvs.pack(side=tk.LEFT, padx=gap // 2)

            rid = self._rounded_rect(cvs, 1, 1, w - 1, key_h - 1, 10,
                                     fill=bg, outline='#3a3a5a')
            tid = cvs.create_text(w // 2, key_h // 2, text=label,
                                  fill=fg, font=('Arial', 13, 'bold'))

            def _press(e, k=key):
                self._osk_press(k)
            def _enter(e, c=cvs, r=rid, orig=bg):
                c.itemconfig(r, fill='#4a4a6a')
            def _leave(e, c=cvs, r=rid, orig=bg):
                c.itemconfig(r, fill=orig)

            cvs.bind('<ButtonPress-1>', _press)
            cvs.bind('<Enter>',         _enter)
            cvs.bind('<Leave>',         _leave)

    @staticmethod
    def _rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
        pts = [
            x1+r, y1,    x2-r, y1,
            x2,   y1,    x2,   y1+r,
            x2,   y2-r,  x2,   y2,
            x2-r, y2,    x1+r, y2,
            x1,   y2,    x1,   y2-r,
            x1,   y1+r,  x1,   y1,
            x1+r, y1,
        ]
        return canvas.create_polygon(pts, smooth=True, **kw)

    def _osk_press(self, key):
        cur = self._cmd_var.get()
        if key == 'BKSP':
            self._cmd_var.set(cur[:-1])
        elif key == 'ENTER':
            self._send_command()
            self._hide_osk()
            return
        elif key == 'CLOSE':
            self._hide_osk()
            self.root.focus_set()   # pull focus away so cmd_entry FocusIn doesn't reopen
            return
        elif key == 'SPACE':
            self._cmd_var.set(cur + ' ')
        elif key == 'SHIFT':
            self._osk_shift = not self._osk_shift
            if self._osk and self._osk.winfo_exists():
                self._osk.destroy()
                self._osk = None
            self._build_osk_window()
            return
        else:
            self._cmd_var.set(cur + key)
            if self._osk_shift:
                self._osk_shift = False
                if self._osk and self._osk.winfo_exists():
                    self._osk.destroy()
                    self._osk = None
                self._build_osk_window()
                return
        # Move cursor to end in the embedded entry
        if hasattr(self, '_osk_entry') and self._osk_entry.winfo_exists():
            self._osk_entry.icursor(tk.END)

    def _hide_osk(self):
        if self._osk and self._osk.winfo_exists():
            self._osk.destroy()
        self._osk = None

    def _send_command(self):
        """Send the text from the command bar to the hexapod on port 5001."""
        msg = self._cmd_var.get().strip()
        if not msg:
            return
        self._cmd_var.set('')
        self._log(f"CMD → {msg}")
        def _do_send():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(5)
                    s.connect((self.RELAY_HOST, self.SPEAKER_PORT))
                    s.sendall(msg.encode('utf-8'))
            except Exception as e:
                self._log(f"CMD error: {e}")
        threading.Thread(target=_do_send, daemon=True).start()

    def _start_threads(self):
        threading.Thread(target=self._controller_thread,    daemon=True).start()
        threading.Thread(target=self._battery_thread,       daemon=True).start()
        threading.Thread(target=self._network_read_thread,  daemon=True).start()
        threading.Thread(target=self._back_paddle_thread,   daemon=True).start()
        threading.Thread(target=self._ping_reconnect_thread, daemon=True).start()

    # ──────────────────────────────────────────────
    # Connection helpers
    # ──────────────────────────────────────────────

    def _mark_disconnected(self):
        """Close the socket and flag as disconnected (safe to call from any thread)."""
        self.connected = False
        try:
            if self.socket:
                self.socket.close()
        except Exception:
            pass
        self.socket = None
        self.root.after(0, lambda: self.status_label.config(
            text="DISCONNECTED", fg=self.FG_ERROR))

    def _ping_reconnect_thread(self):
        """Ping the Pi5 every 2 s, show latency, and auto-reconnect when possible."""
        while self.running:
            ping_ms = self._do_ping()

            # Update ping label
            if ping_ms is not None:
                if ping_ms < 30:
                    color = self.FG_SUCCESS
                elif ping_ms < 80:
                    color = self.FG_WARNING
                else:
                    color = self.FG_ERROR
                self.root.after(0, lambda p=ping_ms, c=color:
                    self.ping_label.config(text=f"Ping: {p:.1f} ms", fg=c))
            else:
                self.root.after(0, lambda: (
                    self.ping_label.config(text="Ping: disconnected", fg=self.FG_ERROR),
                    self.status_label.config(text="DISCONNECTED", fg=self.FG_ERROR),
                ))

            # Auto-reconnect
            if not self.connected and ping_ms is not None:
                self.root.after(0, lambda: self.status_label.config(
                    text="RECONNECTING...", fg=self.FG_WARNING))
                self._try_reconnect()

            time.sleep(2)

    def _do_ping(self):
        """Return RTT in ms to RELAY_HOST, or None if unreachable."""
        try:
            result = subprocess.run(
                ['ping', '-c', '1', '-W', '1', self.RELAY_HOST],
                capture_output=True, text=True, timeout=3
            )
            m = re.search(r'time[=<]([\d.]+)', result.stdout)
            return float(m.group(1)) if m else None
        except Exception:
            return None

    def _try_reconnect(self):
        """Attempt a fresh TCP connection to the relay."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3.0)
            sock.connect((self.RELAY_HOST, self.RELAY_PORT))
            self.socket    = sock
            self.connected = True
            self.root.after(0, lambda: (
                self.status_label.config(text="CONNECTED", fg=self.FG_SUCCESS),
            ))
            self._log("Reconnected to relay")
        except Exception:
            pass   # will retry after next ping

    # ──────────────────────────────────────────────
    # Controller helpers
    # ──────────────────────────────────────────────

    def _axis_value(self, axis):
        if not self.joystick or self.joystick.get_numaxes() <= axis:
            return 0.0
        v = self.joystick.get_axis(axis)
        return 0.0 if abs(v) < self.DEAD else v

    def _button_value(self, button):
        if not self.joystick or self.joystick.get_numbuttons() <= button:
            return False
        return bool(self.joystick.get_button(button))

    def _to_int(self, v):
        return int(max(-100, min(100, v * 100)))

    # ──────────────────────────────────────────────
    # Threads
    # ──────────────────────────────────────────────

    def _controller_thread(self):
        """Read controller and send commands at CONTROLLER_SEND_RATE Hz.

        Packet format (10 fields):
          /CONTROLL/{rsx},{rsy},{lsx},{lsy},{hat_x},{hat_y},{face},{sticks},{trig},{back}

          face   bits: X=0  A=1  B=2  Y=3
          sticks bits: L3=0  R3=1
          trig   bits: LT=0  Back=1  LB=2  RT=3  RB=4  Start=5
          back   bits: M1=0  M2=1  M3=2  Y1=3  Y2=4  Y3=5
        """
        interval = 1.0 / self.CONTROLLER_SEND_RATE

        while self.running:
            start = time.time()

            if self.joystick:
                pygame.event.pump()

                # Analog sticks
                lsx = self._to_int(self._axis_value(0))
                lsy = self._to_int(self._axis_value(1))
                rsx = self._to_int(self._axis_value(3))
                rsy = self._to_int(self._axis_value(4))

                # Triggers (axis 2 = LT, axis 5 = RT; resting at -1, pressed toward +1)
                lt_pressed = 1 if self.joystick.get_axis(2) > -0.9 else 0
                rt_pressed = 1 if self.joystick.get_axis(5) > -0.9 else 0

                # D-pad
                hat = self.joystick.get_hat(0) if self.joystick.get_numhats() > 0 else (0, 0)

                # Face buttons: X(2) A(0) B(1) Y(3)
                face = (
                    (self._button_value(2) << 0) |
                    (self._button_value(0) << 1) |
                    (self._button_value(1) << 2) |
                    (self._button_value(3) << 3)
                )

                # Stick clicks: L3=btn9, R3=btn8 (Legion Go mapping)
                sticks = (
                    (self._button_value(9) << 0) |
                    (self._button_value(8) << 1)
                )

                # Shoulder / system buttons
                trig = (
                    (lt_pressed              << 0) |
                    (self._button_value(6)   << 1) |  # Back/View
                    (self._button_value(4)   << 2) |  # LB
                    (rt_pressed              << 3) |
                    (self._button_value(5)   << 4) |  # RB
                    (self._button_value(7)   << 5)    # Start/Menu
                )

                # Back paddles from evdev thread
                bs = self._back_state
                back = (
                    (bs[BACK_KEY_M1] << 0) |
                    (bs[BACK_KEY_M2] << 1) |
                    (bs[BACK_KEY_M3] << 2) |
                    (bs[BACK_KEY_Y1] << 3) |
                    (bs[BACK_KEY_Y2] << 4) |
                    (bs[BACK_KEY_Y3] << 5)
                )

                pkt = (f"/CONTROLL/{rsx},{rsy},{lsx},{lsy},"
                       f"{hat[0]},{hat[1]},{face},{sticks},{trig},{back}\n")

                if self.connected and self.socket:
                    try:
                        self.socket.sendall(pkt.encode("ascii"))
                        self.packet_count += 1
                        self.fps_counter  += 1
                    except Exception:
                        self._mark_disconnected()

                # Schedule UI update on main thread — tkinter is not thread-safe
                self.root.after(0, lambda lsx=lsx, lsy=lsy, rsx=rsx, rsy=rsy,
                                          hat=hat, face=face, sticks=sticks,
                                          trig=trig, back=back:
                    self._update_controller_display(lsx, lsy, rsx, rsy, hat, face, sticks, trig, back)
                )

            elapsed = time.time() - start
            time.sleep(max(0, interval - elapsed))

    def _back_paddle_thread(self):
        """Read raw evdev events from the Legion keyboard device for M1-M3 / Y1-Y3."""
        dev = _find_legion_keyboard_device()
        if not dev:
            self._log("Back paddles: device not found — add user to 'input' group")
            return

        self._log(f"Back paddles: reading {dev}")
        try:
            with open(dev, 'rb') as f:
                while self.running:
                    raw = f.read(_EV_SIZE)
                    if len(raw) < _EV_SIZE:
                        break
                    _, _, etype, code, value = struct.unpack(_EV_FMT, raw)
                    if etype == EV_KEY:
                        # Always show last key code so user can discover M/Y mappings
                        self._last_raw_key = code
                        # Update back-paddle state if this is a known code
                        if code in self._back_state:
                            self._back_state[code] = bool(value)
        except PermissionError:
            self._log(f"No access to {dev} — run: sudo usermod -aG input $USER  (then re-login)")
        except Exception as e:
            self._log(f"Back-paddle thread: {e}")

    def _battery_thread(self):
        while self.running:
            time.sleep(self.BATTERY_UPDATE_INTERVAL)
            if self.connected and self.socket:
                try:
                    self.socket.sendall(b"/BATTERY/V\n")
                    time.sleep(0.05)
                    self.socket.sendall(b"/BATTERY/P\n")
                    time.sleep(0.05)
                    self.socket.sendall(b"/MODE\n")
                except Exception:
                    pass

    def _network_read_thread(self):
        buf = ""
        while self.running:
            if not self.connected or not self.socket:
                time.sleep(0.5)
                continue
            try:
                data = self.socket.recv(1024)
                if not data:
                    self._mark_disconnected()
                    self._log("Relay closed connection")
                    continue

                buf += data.decode("ascii", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()

                    if line.startswith("/BATTERY/V/"):
                        try:
                            volt = float(line.split("/")[-1])
                            self.battery_voltage = volt
                            self.root.after(0, lambda v=volt: self.volt_label.config(text=f"Voltage: {v:.2f} V"))
                        except Exception:
                            pass
                    elif line.startswith("/BATTERY/P/"):
                        try:
                            raw = float(line.split("/")[-1])
                            # EMA low-pass filter — damps chaotic voltage-derived spikes
                            if self._batt_filtered is None:
                                self._batt_filtered = raw
                            else:
                                self._batt_filtered = (self.BATT_ALPHA * raw
                                                       + (1.0 - self.BATT_ALPHA) * self._batt_filtered)
                            pct = int(round(self._batt_filtered))
                            self.battery_percent = pct
                            self.root.after(0, lambda p=pct: (
                                self.percent_label.config(text=f"Capacity: {p} %"),
                                self.battery_bar.config(value=p)
                            ))
                        except Exception:
                            pass
                    elif line.startswith("/MODE/"):
                        mode = line[6:].strip()
                        self.root.after(0, lambda m=mode: self.mode_label.config(text=f"Walk:  {m}"))
                    elif line.startswith("/STAB/"):
                        stab = line[6:].strip()
                        self.root.after(0, lambda s=stab: self.stab_label.config(text=f"Stab:  {s}"))
                    elif line.startswith("/TILT/"):
                        try:
                            tilt_data = line[6:].strip()
                            parts = tilt_data.split(",")
                            if len(parts) == 2:
                                roll = float(parts[0].strip())
                                pitch = float(parts[1].strip())
                                self.root.after(0, lambda r=roll, p=pitch: (
                                    self.roll_label.config(text=f"Roll:  {r:.2f} °"),
                                    self.pitch_label.config(text=f"Pitch: {p:.2f} °")
                                ))
                        except Exception:
                            pass
                    elif line.startswith("/TOF/"):
                        try:
                            distance = line[5:].strip()
                            distance_mm = float(distance)
                            self.root.after(0, lambda d=distance_mm:
                                self.tof_label.config(text=f"Distance: {d:.0f} mm"))
                        except Exception:
                            pass
                    elif line and not line.startswith("/CONTROLL"):
                        self._log(f"RX: {line[:50]}")

            except socket.timeout:
                pass
            except Exception:
                self._mark_disconnected()
                time.sleep(0.1)

    # ──────────────────────────────────────────────
    # Display updates
    # ──────────────────────────────────────────────

    def _update_controller_display(self, lsx, lsy, rsx, rsy, hat, face, sticks, trig, back):
        self.lstick_label.config(text=f"{lsx:4d},{lsy:4d}")
        self.rstick_label.config(text=f"{rsx:4d},{rsy:4d}")

        buttons = []
        if face   & 0x01: buttons.append("X")
        if face   & 0x02: buttons.append("A")
        if face   & 0x04: buttons.append("B")
        if face   & 0x08: buttons.append("Y")
        if sticks & 0x01: buttons.append("L3")
        if sticks & 0x02: buttons.append("R3")
        if trig   & 0x01: buttons.append("LT")
        if trig   & 0x04: buttons.append("LB")
        if trig   & 0x08: buttons.append("RT")
        if trig   & 0x10: buttons.append("RB")
        if trig   & 0x02: buttons.append("Back")
        if trig   & 0x20: buttons.append("Start")
        if back   & 0x01: buttons.append("M1")
        if back   & 0x02: buttons.append("M2")
        if back   & 0x04: buttons.append("M3")
        if back   & 0x08: buttons.append("Y1")
        if back   & 0x10: buttons.append("Y2")
        if back   & 0x20: buttons.append("Y3")

        self.buttons_label.config(text=", ".join(buttons) if buttons else "None")

        dpad_map = {
            (0, 1): "⬆", (0,-1): "⬇", (-1, 0): "⬅", (1, 0): "➡",
            (-1,1): "↖", (1, 1): "↗", (-1,-1): "↙", (1,-1): "↘", (0,0): "◯"
        }
        self.dpad_label.config(text=dpad_map.get(hat, "?"))


    def _update_fps_display(self):
        self.fps_label.config(text=f"FPS: {self.fps_counter}")
        self.fps_counter = 0
        self.packet_label.config(text=f"Packets: {self.packet_count}")
        uptime  = int(time.time() - self.start_time)
        h, rem  = divmod(uptime, 3600)
        m, s    = divmod(rem, 60)
        self.uptime_label.config(text=f"Uptime: {h}h {m}m {s}s")
        self.root.after(1000, self._update_fps_display)

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.msg_log.append(f"[{ts}] {msg}")
        self.root.after(0, lambda m=msg: (
            self.log_text.config(state=tk.NORMAL),
            self.log_text.insert(tk.END, f"{m}\n"),
            self.log_text.see(tk.END)
        ))

    # ──────────────────────────────────────────────
    # GStreamer video
    # ──────────────────────────────────────────────

    def _start_gstreamer_stream(self):
        self.root.update_idletasks()
        self._video_xid    = self.video_frame.winfo_id()
        self._gst_pipeline = None
        self.video_status.config(text="Connecting...", fg=self.FG_WARNING)
        self._log(f"Starting video on port {self.CAMERA_PORT}...")

        pipeline_str = (
            f"udpsrc port={self.CAMERA_PORT} ! "
            "application/x-rtp,payload=96 ! "
            "rtph264depay ! h264parse ! avdec_h264 ! "
            "videoconvert ! xvimagesink sync=false name=vsink"
        )
        try:
            pipeline = Gst.parse_launch(pipeline_str)
        except Exception as e:
            self._log(f"Pipeline error: {e}")
            self.video_status.config(text="Pipeline error", fg=self.FG_ERROR)
            return

        bus = pipeline.get_bus()
        bus.enable_sync_message_emission()
        bus.add_signal_watch()
        bus.connect("sync-message::element", self._on_gst_sync_message)
        bus.connect("message",               self._on_gst_message)

        self._gst_pipeline = pipeline
        pipeline.set_state(Gst.State.PLAYING)
        self._log("Pipeline started — waiting for stream...")

    def _on_gst_sync_message(self, bus, msg):
        if msg.get_structure().get_name() == "prepare-window-handle":
            msg.src.set_window_handle(self._video_xid)
            self.root.after(0, self._show_video_active)

    def _show_video_active(self):
        self.video_label.pack_forget()
        self.video_status.config(text="Stream active", fg=self.FG_SUCCESS)
        self._log("Stream active — video embedded")

    def _on_gst_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            self.root.after(0, lambda: (
                self._log(f"GST error: {err.message}"),
                self.video_status.config(text="Stream error", fg=self.FG_ERROR)
            ))
        elif t == Gst.MessageType.EOS:
            self.root.after(0, lambda: (
                self._log("GST: end of stream"),
                self.video_status.config(text="Stream ended", fg=self.FG_WARNING)
            ))

    # ──────────────────────────────────────────────
    # Shutdown
    # ──────────────────────────────────────────────

    def on_closing(self):
        self.running = False
        if self._gst_pipeline:
            try:
                self._gst_pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        if self.socket:
            self.socket.close()
        pygame.quit()
        self.root.destroy()


def main():
    root = tk.Tk()
    app  = HexapodControl(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()

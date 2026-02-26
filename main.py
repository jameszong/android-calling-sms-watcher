import sys
import os
import subprocess
import threading
import time
import sqlite3
import datetime
import urllib.request
import json
import socket
import queue

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                            QHBoxLayout, QLabel, QPushButton, QTableWidget, 
                            QTableWidgetItem, QHeaderView, QTabWidget, QMessageBox, QDialog, QLineEdit,
                            QListWidget, QSplitter, QInputDialog, QComboBox, QStackedWidget,
                            QSystemTrayIcon, QMenu, QStyle)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QObject
from PyQt6.QtGui import QColor, QPalette, QAction

# App Metadata
APP_VERSION = "v1.0.0"

# Database Setup
DB_FILE = "logs.db"

# DB Worker Queue
db_queue = queue.Queue()

class DbWorkerThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        
    def run(self):
        # Dedicated thread to handle all DB writes sequentially
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        while True:
            try:
                task = db_queue.get()
                if task is None: # poison pill
                    break
                    
                table, data = task
                if table == "calls":
                    cursor.execute('''
                        INSERT INTO calls (time, direction, number, duration, device_id)
                        VALUES (?, ?, ?, ?, ?)
                    ''', data)
                elif table == "sms":
                    cursor.execute('''
                        INSERT INTO sms (time, direction, number, content, device_id)
                        VALUES (?, ?, ?, ?, ?)
                    ''', data)
                    
                conn.commit()
                db_queue.task_done()
            except Exception as e:
                print(f"DB Worker Error: {e}")
                
        conn.close()

# Start DB Worker
db_worker = DbWorkerThread()
db_worker.start()

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            direction TEXT,
            number TEXT,
            duration TEXT,
            device_id TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            direction TEXT,
            number TEXT,
            content TEXT,
            device_id TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    # DB Migration: Check and add device_id column if it doesn't exist
    try:
        cursor.execute("ALTER TABLE calls ADD COLUMN device_id TEXT")
    except sqlite3.OperationalError:
        pass # Column already exists
        
    try:
        cursor.execute("ALTER TABLE sms ADD COLUMN device_id TEXT")
    except sqlite3.OperationalError:
        pass # Column already exists

    conn.commit()
    conn.close()

def get_setting(key, default=""):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

# WeCom Push Function
def send_wecom_markdown(webhook_url, title, content_dict):
    if not webhook_url:
        return False, "Webhook URL 未配置"
    
    # Build markdown
    md_lines = [f"### {title}"]
    for k, v in content_dict.items():
        if k in ["内容", "短信内容"]:
            md_lines.append(f"> **{k}**: \n> {v}")
        else:
            # Color mapping for better readability
            val_str = str(v)
            if "呼入" in val_str or "接收" in val_str or "成功" in val_str:
                val_str = f'<font color="info">{v}</font>'
            elif "呼出" in val_str or "发送" in val_str:
                val_str = f'<font color="warning">{v}</font>'
            elif "未接" in val_str or "拒接" in val_str:
                val_str = f'<font color="comment">{v}</font>'
            elif "时长" in k:
                val_str = f'<font color="comment">{v}</font>'
            else:
                val_str = f'`{v}`'
                
            md_lines.append(f"> **{k}**: {val_str}")
            
    markdown_text = "\n".join(md_lines)
    
    data = {
        "msgtype": "markdown",
        "markdown": {
            "content": markdown_text
        }
    }
    
    try:
        req = urllib.request.Request(webhook_url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as response:
            res_body = response.read().decode('utf-8')
            res_json = json.loads(res_body)
            if res_json.get("errcode") == 0:
                return True, "发送成功"
            else:
                return False, f"发送失败: {res_json.get('errmsg')}"
    except Exception as e:
        return False, str(e)

# Telegram Push Function
def send_telegram_message(bot_token, chat_id, title, content_dict):
    if not bot_token or not chat_id:
        return False, "Telegram Bot Token 或 Chat ID 未配置"
    
    # Build text
    lines = [f"*{title}*"]
    for k, v in content_dict.items():
        if k in ["内容", "短信内容"]:
            lines.append(f"\n*{k}*:\n{v}")
        else:
            lines.append(f"*{k}*: `{v}`")
            
    text = "\n".join(lines)
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = response.read().decode('utf-8')
            res_json = json.loads(res_body)
            if res_json.get("ok"):
                return True, "发送成功"
            else:
                return False, f"发送失败: {res_json.get('description')}"
    except Exception as e:
        return False, str(e)

# ADB Helper functions
def run_adb_command(cmd_args, timeout=5, serial=None, input_str=None):
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        full_cmd = ["adb"]
        if serial:
            full_cmd.extend(["-s", serial])
        full_cmd.extend(cmd_args)
        
        # Explicitly setting encoding to utf-8 and ignoring errors to prevent crashes on non-gbk/utf-8 characters
        result = subprocess.run(full_cmd, capture_output=True, text=True, input=input_str,
                              startupinfo=startupinfo, timeout=timeout, 
                              encoding='utf-8', errors='ignore')
        return result.returncode == 0, result.stdout
    except Exception as e:
        return False, str(e)

def pair_device(ip, pair_port, pairing_code):
    """Executes `adb pair <ip>:<pair_port> <pairing_code>`."""
    target = f"{ip}:{pair_port}"
    success, output = run_adb_command(["pair", target, pairing_code], timeout=15)
    return success, output

def get_adb_devices():
    """Returns a dictionary mapping serial to its state (e.g., 'device', 'offline', 'unauthorized')."""
    devices = {}
    success, output = run_adb_command(["devices"])
    if success:
        lines = output.strip().split('\n')
        for line in lines[1:]:
            parts = line.split('\t')
            if len(parts) == 2:
                serial = parts[0].strip()
                state = parts[1].strip()
                devices[serial] = state
    return devices


# ---------------------------------------------------------
# Thread Layer: Concurrent Monitoring
# ---------------------------------------------------------
class SmsLogFetcherThread(QThread):
    log_fetched = pyqtSignal(dict, str) # log_data, serial
    
    def __init__(self, serial):
        super().__init__()
        self.serial = serial
        
    def run(self):
        time.sleep(3)
        success, output = run_adb_command([
            "shell", "content query --uri content://sms --projection date:type:address:body --sort 'date DESC'"
        ], serial=self.serial)
        
        if success and "Row: 0" in output:
            try:
                first_row = ""
                for line in output.split('\n'):
                    if line.startswith("Row: 0"):
                        first_row = line
                        break
                
                if not first_row:
                    return
                    
                data_str = first_row.replace("Row: 0 ", "")
                log_data = {}
                import re
                match = re.search(r'date=(.*?), type=(.*?), address=(.*?), body=(.*)', data_str)
                if match:
                    log_data['date'] = match.group(1).strip()
                    log_data['type'] = match.group(2).strip()
                    log_data['number'] = match.group(3).strip()
                    log_data['body'] = match.group(4).strip()
                else:
                    return

                if 'date' in log_data:
                    dt = datetime.datetime.fromtimestamp(int(log_data['date'])/1000)
                    log_data['formatted_time'] = dt.strftime("%Y-%m-%d %H:%M:%S")
                
                if 'type' in log_data:
                    type_map = {'1': '接收', '2': '发送'}
                    log_data['direction'] = type_map.get(log_data['type'], f"其他({log_data['type']})")
                
                self.log_fetched.emit(log_data, self.serial)
            except Exception as e:
                print(f"[{self.serial}] Error parsing SMS log: {e}")

class CallLogFetcherThread(QThread):
    log_fetched = pyqtSignal(dict, str) # dict, serial

    def __init__(self, serial):
        super().__init__()
        self.serial = serial

    def run(self):
        # We add a slight delay to ensure the OS has written the log before we query
        time.sleep(1.5)
        success, output = run_adb_command([
            "shell", "content query --uri content://call_log/calls --projection date:type:number:duration --sort 'date DESC' --limit 1"
        ], serial=self.serial)
        
        if success and "Row: 0" in output:
            try:
                first_row = ""
                for line in output.split('\n'):
                    if line.startswith("Row: 0"):
                        first_row = line
                        break
                
                if not first_row:
                    return
                    
                data_str = first_row.replace("Row: 0 ", "")
                parts = data_str.split(',')
                
                log_data = {}
                for part in parts:
                    if '=' in part:
                        k, v = part.split('=', 1)
                        log_data[k.strip()] = v.strip()
                
                if 'date' in log_data:
                    dt = datetime.datetime.fromtimestamp(int(log_data['date'])/1000)
                    log_data['formatted_time'] = dt.strftime("%Y-%m-%d %H:%M:%S")
                
                if 'type' in log_data:
                    type_map = {'1': '呼入', '2': '呼出', '3': '未接', '5': '拒接'}
                    log_data['direction'] = type_map.get(log_data['type'], f"其他({log_data['type']})")
                
                if 'duration' in log_data:
                    dur_seconds = int(log_data['duration'])
                    if log_data.get('type') in ['3', '5']:
                        dur_seconds = 0
                    m, s = divmod(dur_seconds, 60)
                    h, m = divmod(m, 60)
                    log_data['formatted_duration'] = f"{h}:{m:02d}:{s:02d}"
                
                self.log_fetched.emit(log_data, self.serial)
            except Exception as e:
                print(f"[{self.serial}] Error parsing call log: {e}")

class AdbMonitorThread(QThread):
    call_state_changed = pyqtSignal(int, str) # state, serial
    sms_arrived = pyqtSignal(str) # serial
    
    def __init__(self, serial):
        super().__init__()
        self.serial = serial
        self.running = True
        self.last_state = -1
        self.last_sms_id = None

    def run(self):
        print(f"[{self.serial}] Started monitoring...")
        while self.running:
            # Check call state
            success, output = run_adb_command(["shell", "dumpsys", "telephony.registry"], serial=self.serial)
            if success:
                state = self.parse_call_state(output)
                if state != -1 and state != self.last_state:
                    self.last_state = state
                    self.call_state_changed.emit(state, self.serial)
            
            # Check SMS state
            success, output = run_adb_command([
                "shell", "content query --uri content://sms --projection _id --sort 'date DESC'"
            ], serial=self.serial)
            if success and "Row: 0" in output:
                latest_id = self.parse_latest_sms_id(output)
                if self.last_sms_id is None:
                    self.last_sms_id = latest_id # initialize on first run
                elif latest_id and latest_id != self.last_sms_id:
                    self.last_sms_id = latest_id
                    self.sms_arrived.emit(self.serial)

            time.sleep(2) # Poll every 2 seconds
            
        print(f"[{self.serial}] Stopped monitoring.")

    def parse_latest_sms_id(self, output):
        for line in output.split('\n'):
            if line.startswith("Row: 0"):
                parts = line.split('_id=')
                if len(parts) > 1:
                    return parts[1].split(',')[0].strip()
        return None

    def parse_call_state(self, dump_output):
        if not dump_output:
            return -1
        for line in dump_output.split('\n'):
            line = line.strip()
            if line.startswith("mCallState=") or line.startswith("mCallState "):
                try:
                    parts = line.split('=')
                    if len(parts) > 1:
                        val_str = parts[1].split()[0]
                        return int(val_str)
                except ValueError:
                    pass
        return -1
        
    def stop(self):
        self.running = False

# ---------------------------------------------------------
# UI & Settings
# ---------------------------------------------------------
class AddDeviceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("手动添加/配对设备")
        self.resize(400, 300)
        
        layout = QVBoxLayout(self)
        
        # Connection Section
        layout.addWidget(QLabel("<b>常规连接 (Android 10 及以下，或已配对设备)</b>"))
        conn_form = QVBoxLayout()
        
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("设备 IP:"))
        self.conn_ip = QLineEdit()
        self.conn_ip.setPlaceholderText("例如: 192.168.1.100")
        row1.addWidget(self.conn_ip)
        conn_form.addLayout(row1)
        
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("连接端口:"))
        self.conn_port = QLineEdit()
        self.conn_port.setPlaceholderText("例如: 5555 (无线调试主界面)")
        row2.addWidget(self.conn_port)
        conn_form.addLayout(row2)
        
        self.btn_connect = QPushButton("直接连接")
        self.btn_connect.clicked.connect(self.start_connect)
        conn_form.addWidget(self.btn_connect, alignment=Qt.AlignmentFlag.AlignRight)
        
        layout.addLayout(conn_form)
        layout.addWidget(QLabel("<hr>"))
        
        # Pairing Section
        layout.addWidget(QLabel("<b>无线配对 (Android 11+ 新设备首次连接)</b>"))
        layout.addWidget(QLabel("<small>注: IP与上方一致。配对成功后将自动尝试连接上方填写的连接端口。</small>"))
        pair_form = QVBoxLayout()
        
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("配对端口:"))
        self.pair_port = QLineEdit()
        self.pair_port.setPlaceholderText("例如: 45678 (显示在配对界面)")
        row3.addWidget(self.pair_port)
        pair_form.addLayout(row3)
        
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("配对码:"))
        self.pair_code = QLineEdit()
        self.pair_code.setPlaceholderText("例如: 123456")
        row4.addWidget(self.pair_code)
        pair_form.addLayout(row4)
        
        self.btn_pair = QPushButton("配对并连接")
        self.btn_pair.clicked.connect(self.start_pairing)
        pair_form.addWidget(self.btn_pair, alignment=Qt.AlignmentFlag.AlignRight)
        
        layout.addLayout(pair_form)
        layout.addStretch()

    def start_connect(self):
        ip = self.conn_ip.text().strip()
        port = self.conn_port.text().strip()
        
        if not ip or not port:
            QMessageBox.warning(self, "错误", "请填写完整的 IP 和连接端口。")
            return
            
        target = f"{ip}:{port}"
        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("连接中...")
        QApplication.processEvents()
        
        threading.Thread(target=self._connect_task, args=(target,), daemon=True).start()

    def _connect_task(self, target):
        success, output = run_adb_command(["connect", target])
        QTimer.singleShot(0, lambda: self._on_connect_result(success, output, target))

    def _on_connect_result(self, success, output, target):
        self.btn_connect.setEnabled(True)
        self.btn_connect.setText("直接连接")
        
        if success and "connected" in output.lower() and "failed" not in output.lower():
            QMessageBox.information(self, "连接成功", f"成功连接到设备 {target}！\n主界面设备列表将在几秒内刷新。")
            self.accept()
        else:
            QMessageBox.critical(self, "连接失败", f"连接到 {target} 失败。\n终端输出:\n{output}")

    def start_pairing(self):
        ip = self.conn_ip.text().strip()
        conn_port = self.conn_port.text().strip()
        pair_port = self.pair_port.text().strip()
        code = self.pair_code.text().strip()
        
        if not ip or not pair_port or not code or not conn_port:
            QMessageBox.warning(self, "错误", "请填写完整的 IP、连接端口、配对端口和配对码。")
            return
            
        self.btn_pair.setEnabled(False)
        self.btn_pair.setText("配对中...")
        self.btn_connect.setEnabled(False)
        QApplication.processEvents()
        
        threading.Thread(target=self._pair_and_connect_task, args=(ip, pair_port, code, conn_port), daemon=True).start()

    def _pair_and_connect_task(self, ip, pair_port, code, conn_port):
        # 1. Pair
        pair_success, pair_output = pair_device(ip, pair_port, code)
        if pair_success and "Successfully paired" in pair_output:
            # 2. Connect
            target = f"{ip}:{conn_port}"
            conn_success, conn_output = run_adb_command(["connect", target])
            QTimer.singleShot(0, lambda: self._on_pair_connect_result(True, pair_output, conn_success, conn_output, target))
        else:
            QTimer.singleShot(0, lambda: self._on_pair_connect_result(False, pair_output, False, "", ""))

    def _on_pair_connect_result(self, pair_success, pair_output, conn_success, conn_output, target):
        self.btn_pair.setEnabled(True)
        self.btn_pair.setText("配对并连接")
        self.btn_connect.setEnabled(True)
        
        if not pair_success:
            QMessageBox.critical(self, "配对失败", f"配对失败，请检查 IP/配对端口/配对码是否正确。\n终端输出:\n{pair_output}")
        else:
            if conn_success and "connected" in conn_output.lower() and "failed" not in conn_output.lower():
                QMessageBox.information(self, "配对并连接成功", f"配对成功！且成功连接到 {target}。\n主界面设备列表将在几秒内刷新。")
                self.accept()
            else:
                QMessageBox.warning(self, "配对成功但连接失败", f"配对已成功，但连接到 {target} 失败。\n请确认连接端口是否正确，并点击上方【直接连接】重试。\n终端输出:\n{conn_output}")


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置通知方式")
        self.resize(450, 250)
        
        layout = QVBoxLayout(self)
        
        # Notification Type Selection
        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel("<b>推送方式:</b>"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["企业微信 (WeCom)", "Telegram Bot"])
        
        current_type = get_setting("notification_type", "WeCom")
        if current_type == "Telegram":
            self.type_combo.setCurrentIndex(1)
        else:
            self.type_combo.setCurrentIndex(0)
            
        self.type_combo.currentIndexChanged.connect(self.on_type_changed)
        type_layout.addWidget(self.type_combo)
        type_layout.addStretch()
        layout.addLayout(type_layout)
        
        layout.addWidget(QLabel("<hr>"))
        
        # Stacked Widget for different configs
        self.stacked_widget = QStackedWidget()
        layout.addWidget(self.stacked_widget)
        
        # --- Page 0: WeCom Config ---
        wecom_page = QWidget()
        wecom_layout = QVBoxLayout(wecom_page)
        wecom_layout.setContentsMargins(0, 0, 0, 0)
        
        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("Webhook URL:"))
        self.url_input = QLineEdit()
        self.url_input.setText(get_setting("wecom_webhook", ""))
        url_layout.addWidget(self.url_input)
        wecom_layout.addLayout(url_layout)
        wecom_layout.addStretch()
        
        self.stacked_widget.addWidget(wecom_page)
        
        # --- Page 1: Telegram Config ---
        tg_page = QWidget()
        tg_layout = QVBoxLayout(tg_page)
        tg_layout.setContentsMargins(0, 0, 0, 0)
        
        token_layout = QHBoxLayout()
        token_layout.addWidget(QLabel("Bot Token:"))
        self.tg_token_input = QLineEdit()
        self.tg_token_input.setText(get_setting("tg_bot_token", ""))
        token_layout.addWidget(self.tg_token_input)
        tg_layout.addLayout(token_layout)
        
        chatid_layout = QHBoxLayout()
        chatid_layout.addWidget(QLabel("Chat ID:"))
        self.tg_chatid_input = QLineEdit()
        self.tg_chatid_input.setText(get_setting("tg_chat_id", ""))
        chatid_layout.addWidget(self.tg_chatid_input)
        tg_layout.addLayout(chatid_layout)
        tg_layout.addStretch()
        
        self.stacked_widget.addWidget(tg_page)
        
        # Initialize stacked widget state
        self.on_type_changed(self.type_combo.currentIndex())
        
        layout.addWidget(QLabel("<hr>"))
        
        # Test Button
        btn_test = QPushButton("测试当前推送")
        btn_test.clicked.connect(self.test_push)
        layout.addWidget(btn_test, alignment=Qt.AlignmentFlag.AlignLeft)
        
        layout.addStretch()
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.btn_save = QPushButton("保存设置")
        self.btn_save.clicked.connect(self.save_settings)
        btn_layout.addWidget(self.btn_save)
        
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)
        
        layout.addLayout(btn_layout)

    def on_type_changed(self, index):
        self.stacked_widget.setCurrentIndex(index)

    def test_push(self):
        test_data = {
            "测试类型": "连接测试",
            "状态": "成功",
            "时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        self.sender().setEnabled(False)
        self.sender().setText("测试中...")
        QApplication.processEvents()
        
        index = self.type_combo.currentIndex()
        if index == 0: # WeCom
            url = self.url_input.text().strip()
            if not url:
                QMessageBox.warning(self, "错误", "请输入 WeCom Webhook URL")
                success, msg = False, "URL为空"
            else:
                success, msg = send_wecom_markdown(url, "🔔 Phone Monitor 测试", test_data)
        else: # Telegram
            token = self.tg_token_input.text().strip()
            chat_id = self.tg_chatid_input.text().strip()
            if not token or not chat_id:
                QMessageBox.warning(self, "错误", "请输入 Telegram Bot Token 和 Chat ID")
                success, msg = False, "Token或Chat ID为空"
            else:
                success, msg = send_telegram_message(token, chat_id, "🔔 Phone Monitor 测试", test_data)
        
        self.sender().setEnabled(True)
        self.sender().setText("测试当前推送")
        
        if success:
            QMessageBox.information(self, "成功", "测试推送已发送！")
        else:
            if msg != "URL为空" and msg != "Token或Chat ID为空":
                QMessageBox.critical(self, "失败", f"推送失败: {msg}")

    def save_settings(self):
        index = self.type_combo.currentIndex()
        if index == 0:
            set_setting("notification_type", "WeCom")
        else:
            set_setting("notification_type", "Telegram")
            
        set_setting("wecom_webhook", self.url_input.text().strip())
        set_setting("tg_bot_token", self.tg_token_input.text().strip())
        set_setting("tg_chat_id", self.tg_chatid_input.text().strip())
        
        self.accept()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Android Phone Monitor (Multi-Device)")
        self.resize(1000, 600)
        
        self.device_threads = {} # serial -> AdbMonitorThread
        self.active_fetchers = [] # Prevent fetchers from garbage collection
        
        # System Tray Setup
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        
        # Tray Menu
        self.tray_menu = QMenu()
        show_action = QAction("显示界面", self)
        show_action.triggered.connect(self.show_window)
        self.tray_menu.addAction(show_action)
        
        quit_action = QAction("退出程序", self)
        quit_action.triggered.connect(self.quit_app)
        self.tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self.tray_icon_activated)
        self.tray_icon.show()
        
        self.setup_ui()
        self.load_call_data()
        self.load_sms_data()
        
        # Polling for connected devices
        self.device_poll_timer = QTimer(self)
        self.device_poll_timer.timeout.connect(self.poll_connected_devices)
        self.device_poll_timer.start(3000)
        
        # Initial poll
        self.poll_connected_devices()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Top bar
        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("<b>多设备并发监听模式</b>"))
        top_layout.addStretch()
        self.btn_settings = QPushButton("设置 (Webhook)")
        self.btn_settings.clicked.connect(self.open_settings)
        top_layout.addWidget(self.btn_settings)
        main_layout.addLayout(top_layout)
        
        # Splitter for sidebar and main content
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)
        
        # --- Sidebar (Device List) ---
        sidebar_widget = QWidget()
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.addWidget(QLabel("已连接的设备:"))
        
        self.device_list_widget = QListWidget()
        self.device_list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.device_list_widget.customContextMenuRequested.connect(self.show_device_context_menu)
        sidebar_layout.addWidget(self.device_list_widget)
        
        btn_add_ip = QPushButton("手动添加 / 配对设备")
        btn_add_ip.clicked.connect(self.open_add_device_dialog)
        sidebar_layout.addWidget(btn_add_ip)
        
        splitter.addWidget(sidebar_widget)
        
        # --- Main Content (Tabs) ---
        tabs_widget = QWidget()
        tabs_layout = QVBoxLayout(tabs_widget)
        tabs_layout.setContentsMargins(0, 0, 0, 0)
        
        self.tabs = QTabWidget()
        tabs_layout.addWidget(self.tabs)
        
        # Calls Tab
        self.call_tab = QWidget()
        call_layout = QVBoxLayout(self.call_tab)
        self.call_table = QTableWidget()
        self.call_table.setColumnCount(5)
        self.call_table.setHorizontalHeaderLabels(["时间", "设备", "方向", "号码", "通话时长"])
        self.call_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.call_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.call_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        call_layout.addWidget(self.call_table)
        self.tabs.addTab(self.call_tab, "通话记录")
        
        # SMS Tab
        self.sms_tab = QWidget()
        sms_layout = QVBoxLayout(self.sms_tab)
        self.sms_table = QTableWidget()
        self.sms_table.setColumnCount(5)
        self.sms_table.setHorizontalHeaderLabels(["时间", "设备", "方向", "号码", "短信内容"])
        self.sms_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.sms_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sms_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        sms_layout.addWidget(self.sms_table)
        self.tabs.addTab(self.sms_tab, "短信记录")
        
        splitter.addWidget(tabs_widget)
        splitter.setSizes([250, 750])
        
        # Status bar version label (bottom-right)
        self.version_label = QLabel(f"版本: {APP_VERSION}")
        self.statusBar().addPermanentWidget(self.version_label)

    def open_settings(self):
        dialog = SettingsDialog(self)
        dialog.exec()

    def open_add_device_dialog(self):
        dialog = AddDeviceDialog(self)
        dialog.exec()

    def show_device_context_menu(self, position):
        item = self.device_list_widget.itemAt(position)
        if item:
            text = item.text()
            # Extract serial from text (e.g., "🟢 192.168.1.100:5555" -> "192.168.1.100:5555")
            parts = text.split(' ')
            if len(parts) >= 2:
                serial = parts[1]
                menu = QMenu()
                disconnect_action = QAction("断开连接", self)
                disconnect_action.triggered.connect(lambda: self.disconnect_device(serial))
                menu.addAction(disconnect_action)
                menu.exec(self.device_list_widget.viewport().mapToGlobal(position))

    def disconnect_device(self, serial):
        success, output = run_adb_command(["disconnect", serial])
        if success:
            QMessageBox.information(self, "断开连接", f"已断开设备: {serial}")
            self.poll_connected_devices()
        else:
            QMessageBox.warning(self, "断开连接失败", f"无法断开设备: {serial}\n{output}")

    # --- Device Management ---
    def connect_device(self, target):
        success, output = run_adb_command(["connect", target])
        self.poll_connected_devices() # Trigger an immediate refresh
        return success, output

    def poll_connected_devices(self):
        adb_devices = get_adb_devices()
        active_serials = [s for s, state in adb_devices.items() if state == 'device']
        
        # Stop threads for disconnected devices
        for serial in list(self.device_threads.keys()):
            if serial not in active_serials:
                print(f"Device disconnected: {serial}")
                thread = self.device_threads.pop(serial)
                thread.stop()
                thread.wait()
                
        # Start threads for newly connected devices
        for serial in active_serials:
            if serial not in self.device_threads:
                print(f"Device connected: {serial}")
                thread = AdbMonitorThread(serial)
                thread.call_state_changed.connect(self.handle_call_state)
                thread.sms_arrived.connect(self.handle_sms_arrived)
                self.device_threads[serial] = thread
                thread.start()
                
        self.update_device_list_ui(adb_devices)

    def update_device_list_ui(self, adb_devices):
        self.device_list_widget.clear()
        
        # Add all devices known to ADB
        for serial, state in adb_devices.items():
            if state == 'device':
                item = QListWidgetItem(f"🟢 {serial}")
            elif state == 'unauthorized':
                item = QListWidgetItem(f"🟡 {serial} (未授权, 请在手机端允许)")
                item.setForeground(Qt.GlobalColor.darkYellow)
            else:
                item = QListWidgetItem(f"🔴 {serial} ({state})")
                item.setForeground(Qt.GlobalColor.red)
            self.device_list_widget.addItem(item)

    # --- Event Handlers & DB Writing ---
    # Due to concurrent potential, SQLite write functions can be called directly 
    # since sqlite3 in Python uses locking for concurrent writes by default if short. 
    # But connecting per call is safer.
    
    def handle_call_state(self, state, serial):
        print(f"[{serial}] Call state changed to: {state}")
        if state == 0: # Idle / Ended
            fetcher = CallLogFetcherThread(serial)
            fetcher.log_fetched.connect(self.save_and_display_call_log)
            self.active_fetchers.append(fetcher)
            fetcher.finished.connect(lambda f=fetcher: self.active_fetchers.remove(f))
            fetcher.start()

    def handle_sms_arrived(self, serial):
        print(f"[{serial}] New SMS detected, fetching log...")
        fetcher = SmsLogFetcherThread(serial)
        fetcher.log_fetched.connect(self.save_and_display_sms_log)
        self.active_fetchers.append(fetcher)
        fetcher.finished.connect(lambda f=fetcher: self.active_fetchers.remove(f))
        fetcher.start()

    def save_and_display_call_log(self, log_data, serial):
        if not log_data:
            return
            
        time_str = log_data.get('formatted_time', '')
        direction = log_data.get('direction', '')
        number = log_data.get('number', '')
        duration = log_data.get('formatted_duration', '0:00:00')
        
        # Dispatch DB write task to the dedicated worker queue
        db_queue.put(("calls", (time_str, direction, number, duration, serial)))
        
        # Give DB thread a moment to process before reloading UI
        QTimer.singleShot(100, self.load_call_data)
        
        # Push notification
        push_data = {
            "设备来源": serial,
            "时间": time_str,
            "方向": direction,
            "号码": number,
            "通话时长": duration
        }
        
        notif_type = get_setting("notification_type", "WeCom")
        if notif_type == "WeCom":
            webhook = get_setting("wecom_webhook", "")
            if webhook:
                threading.Thread(target=send_wecom_markdown, args=(webhook, "📞 新的通话记录", push_data), daemon=True).start()
        elif notif_type == "Telegram":
            token = get_setting("tg_bot_token", "")
            chat_id = get_setting("tg_chat_id", "")
            if token and chat_id:
                threading.Thread(target=send_telegram_message, args=(token, chat_id, "📞 新的通话记录", push_data), daemon=True).start()

    def save_and_display_sms_log(self, log_data, serial):
        if not log_data:
            return
            
        time_str = log_data.get('formatted_time', '')
        direction = log_data.get('direction', '')
        number = log_data.get('number', '')
        body = log_data.get('body', '')
        
        # Dispatch DB write task to the dedicated worker queue
        db_queue.put(("sms", (time_str, direction, number, body, serial)))
        
        # Give DB thread a moment to process before reloading UI
        QTimer.singleShot(100, self.load_sms_data)
        
        # Push notification
        push_data = {
            "设备来源": serial,
            "时间": time_str,
            "方向": direction,
            "号码": number,
            "短信内容": body
        }
        
        notif_type = get_setting("notification_type", "WeCom")
        if notif_type == "WeCom":
            webhook = get_setting("wecom_webhook", "")
            if webhook:
                threading.Thread(target=send_wecom_markdown, args=(webhook, "✉️ 新的短信记录", push_data), daemon=True).start()
        elif notif_type == "Telegram":
            token = get_setting("tg_bot_token", "")
            chat_id = get_setting("tg_chat_id", "")
            if token and chat_id:
                threading.Thread(target=send_telegram_message, args=(token, chat_id, "✉️ 新的短信记录", push_data), daemon=True).start()

    def load_call_data(self):
        self.call_table.setRowCount(0)
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT time, device_id, direction, number, duration FROM calls ORDER BY id DESC')
        rows = cursor.fetchall()
        
        for row_data in rows:
            row_idx = self.call_table.rowCount()
            self.call_table.insertRow(row_idx)
            for col_idx, data in enumerate(row_data):
                item = QTableWidgetItem(str(data if data else ""))
                self.call_table.setItem(row_idx, col_idx, item)
                
        conn.close()

    def load_sms_data(self):
        self.sms_table.setRowCount(0)
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT time, device_id, direction, number, content FROM sms ORDER BY id DESC')
        rows = cursor.fetchall()
        
        for row_data in rows:
            row_idx = self.sms_table.rowCount()
            self.sms_table.insertRow(row_idx)
            for col_idx, data in enumerate(row_data):
                item = QTableWidgetItem(str(data if data else ""))
                if col_idx == 4:
                    item.setToolTip(str(data if data else ""))
                self.sms_table.setItem(row_idx, col_idx, item)
                
        conn.close()

    def tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window()

    def show_window(self):
        self.show()
        self.activateWindow()

    def quit_app(self):
        self.device_poll_timer.stop()
        for thread in self.device_threads.values():
            thread.stop()
            thread.wait()
        QApplication.quit()

    def closeEvent(self, event):
        # Override close event to hide window instead of exiting
        event.ignore()
        self.hide()
        self.tray_icon.showMessage(
            "Phone Monitor 仍在运行",
            "已隐藏到系统托盘，双击图标可重新打开主界面。",
            QSystemTrayIcon.MessageIcon.Information,
            3000
        )

if __name__ == "__main__":
    from PyQt6.QtWidgets import QListWidgetItem # import fix
    init_db()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

import sys
import os
import time
import serial
import serial.tools.list_ports
import pymysql
import atexit
import re
import functools
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash
import datetime
import threading
import socket
import dotenv

# Load environment variables from .env
dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
if os.path.exists(dotenv_path):
    dotenv.load_dotenv(dotenv_path)
else:
    dotenv.load_dotenv()

# Ensure console output uses UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Initialize Flask app
app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'fb6c27f8d1ac1eadc8c2f8ed110f1e342e3f6be99937dd52cceb3c38e8368c72')

# Hardened Session Cookie Settings
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8)
)

# CORS Policy: Restrict to allowed origins
allowed_origins_raw = os.environ.get('ALLOWED_ORIGINS', 'http://localhost:8080,http://127.0.0.1:8080,http://localhost:8000,http://127.0.0.1:8000')
allowed_origins = [o.strip() for o in allowed_origins_raw.split(',') if o.strip()]
CORS(app, resources={r"/*": {"origins": allowed_origins}}, supports_credentials=True)

# Modern Security HTTP Headers Middleware
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

# MySQL Database Configuration
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "sms_system"),
    "port": int(os.environ.get("DB_PORT", 3306))
}

# Brute-force Login Protection Tracker
FAILED_LOGINS = {} # {ip: {"count": int, "blocked_until": float}}

def is_ip_rate_limited(ip):
    rec = FAILED_LOGINS.get(ip)
    if not rec:
        return False
    now = time.time()
    if rec.get("blocked_until", 0) > now:
        return True
    if rec.get("blocked_until", 0) <= now and rec.get("blocked_until", 0) > 0:
        FAILED_LOGINS.pop(ip, None)
    return False

def record_login_attempt(ip, success):
    now = time.time()
    if success:
        FAILED_LOGINS.pop(ip, None)
        return
    rec = FAILED_LOGINS.setdefault(ip, {"count": 0, "blocked_until": 0})
    rec["count"] += 1
    if rec["count"] >= 5:
        rec["blocked_until"] = now + 900 # 15-minute lock

def is_admin_authenticated():
    """Checks if the request is authenticated via web session or master API key"""
    if session.get('logged_in'):
        return True
    master_key = os.environ.get('GATEWAY_MASTER_KEY')
    auth_header = request.headers.get('Authorization')
    token = request.headers.get('X-Master-Key')
    if not token and auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
    if master_key and token and token == master_key:
        return True
    return False

def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin_authenticated():
            return jsonify({"success": False, "error": "Unauthorized: Admin authentication or Master Key required"}), 401
        return f(*args, **kwargs)
    return decorated


def get_db_connection():
    try:
        conn = pymysql.connect(
            host=DB_CONFIG["host"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            database=DB_CONFIG["database"],
            port=DB_CONFIG["port"],
            autocommit=True
        )
        return conn
    except pymysql.MySQLError as e:
        print(f"❌ Database Connection Error: {e}")
        return None

def init_db():
    try:
        conn = pymysql.connect(
            host=DB_CONFIG["host"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            port=DB_CONFIG["port"],
            autocommit=True
        )
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_CONFIG['database']}`")
            cursor.execute(f"USE `{DB_CONFIG['database']}`")
            
            # Legacy tables
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255),
                    phone_number VARCHAR(20),
                    department VARCHAR(50)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sms_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    phone_number VARCHAR(20) NOT NULL,
                    message TEXT NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    sent_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Multi-slot GSM Gateway tables
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS gateways (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    port VARCHAR(50) NOT NULL UNIQUE,
                    baudrate INT DEFAULT 115200,
                    sim_operator VARCHAR(50) DEFAULT 'Auto',
                    prefix_filter TEXT NULL,
                    is_active TINYINT(1) DEFAULT 1,
                    signal_csq INT DEFAULT 0,
                    status VARCHAR(50) DEFAULT 'INITIALIZING',
                    sent_count INT DEFAULT 0,
                    failed_count INT DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Ensure gateways has sim_number, iccid, and imsi columns
            cursor.execute("DESCRIBE gateways")
            gw_cols = [row[0] for row in cursor.fetchall()]
            if 'sim_number' not in gw_cols:
                cursor.execute("ALTER TABLE gateways ADD COLUMN sim_number VARCHAR(50) NULL AFTER sim_operator")
            if 'iccid' not in gw_cols:
                cursor.execute("ALTER TABLE gateways ADD COLUMN iccid VARCHAR(50) NULL AFTER sim_number")
            if 'imsi' not in gw_cols:
                cursor.execute("ALTER TABLE gateways ADD COLUMN imsi VARCHAR(50) NULL AFTER iccid")
            if 'api_key' not in gw_cols:
                cursor.execute("ALTER TABLE gateways ADD COLUMN api_key VARCHAR(100) NULL AFTER imsi")
                import uuid
                cursor.execute("SELECT id FROM gateways WHERE api_key IS NULL")
                for (gid,) in cursor.fetchall():
                    k = "sk-" + str(uuid.uuid4()).replace("-", "")[:24]
                    cursor.execute("UPDATE gateways SET api_key=%s WHERE id=%s", (k, gid))
            if 'occupied_by' not in gw_cols:
                cursor.execute("ALTER TABLE gateways ADD COLUMN occupied_by VARCHAR(100) NULL AFTER api_key")
            
            # Connector registration table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS connectors (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    type VARCHAR(50) NOT NULL,
                    config TEXT NULL,
                    is_active TINYINT(1) DEFAULT 1,
                    status VARCHAR(50) DEFAULT 'RUNNING',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # SQL Connector Outbox & Inbox tables
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS outbox (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    recipient VARCHAR(30) NOT NULL,
                    message TEXT NOT NULL,
                    preferred_gateway VARCHAR(100) NULL,
                    status VARCHAR(50) DEFAULT 'PENDING',
                    dispatched_via VARCHAR(50) NULL,
                    retry_count INT DEFAULT 0,
                    error_message VARCHAR(255) NULL,
                    connector VARCHAR(50) DEFAULT 'HTTP',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_outbox_status (status)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS inbox (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    sender VARCHAR(30) NOT NULL,
                    message TEXT NOT NULL,
                    received_port VARCHAR(50) NOT NULL,
                    received_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Seed default connectors if empty
            cursor.execute("SELECT COUNT(*) FROM connectors")
            if cursor.fetchone()[0] == 0:
                cursor.execute("""
                    INSERT INTO connectors (name, type, config, is_active, status) VALUES
                    ('Diafaan HTTP Web Connector', 'HTTP', '{"port": 8080, "path": "/http/send-message/"}', 1, 'RUNNING'),
                    ('MySQL Outbox Poller Connector', 'SQL', '{"table": "outbox", "interval_sec": 1}', 1, 'RUNNING')
                """)

            # Seed default 4-slot modem pool if empty
            cursor.execute("SELECT COUNT(*) FROM gateways")
            if cursor.fetchone()[0] == 0:
                cursor.execute("""
                    INSERT INTO gateways (name, port, baudrate, sim_operator, prefix_filter, is_active, status) VALUES
                    ('Slot 1 (Globe)', 'COM19', 115200, 'Globe', '0917,0927,0915,0916,0926,0935,0936,0945,0955,0956,0965,0966,0967,0975,0976,0977,0995,0997', 1, 'MOCK'),
                    ('Slot 2 (Smart)', 'COM20', 115200, 'Smart', '0918,0919,0920,0921,0928,0929,0939,0946,0947,0949,0951,0961,0998,0999', 1, 'MOCK'),
                    ('Slot 3 (DITO)',  'COM21', 115200, 'DITO',  '0991,0992,0993,0994', 1, 'MOCK'),
                    ('Slot 4 (Backup)','COM22', 115200, 'Auto',  '', 1, 'MOCK')
                """)

        conn.close()
        print("✅ Database & Gateway architecture verified successfully.")
    except Exception as e:
        print(f"⚠️ Database initialization warning: {e}")

init_db()

# ==============================================================================
# MULTI-SLOT MODEM CONTROLLER ENGINE
# ==============================================================================

class ModemSlot:
    """Represents a single physical or virtual slot on the GSM modem pool"""
    def __init__(self, slot_id, name, port_name, baudrate=115200, sim_operator='Auto', prefix_filter='', sim_number=None, iccid=None, imsi=None, api_key=None, occupied_by=None):
        self.id = slot_id
        self.name = name
        self.port_name = port_name
        self.baudrate = baudrate
        self.sim_operator = sim_operator
        self.prefix_filter = [p.strip() for p in prefix_filter.split(',') if p.strip()]
        self.sim_number = sim_number
        self.iccid = iccid
        self.imsi = imsi
        self.api_key = api_key
        self.occupied_by = occupied_by
        self.ser = None
        self.is_mock = False
        self.status = "INITIALIZING"
        self.last_error = None
        self.signal_csq = 18 # Default healthy signal for display
        self.sent_count = 0
        self.failed_count = 0
        self.is_active = True
        self.lock = threading.RLock()
        self.open_port()

    def open_port(self):
        with self.lock:
            try:
                self.ser = serial.Serial(self.port_name, baudrate=self.baudrate, timeout=2, write_timeout=2)
                self.is_mock = False
                self.status = "ONLINE"
                self.last_error = None
                print(f"✅ [MODEM SLOT] {self.name} connected on {self.port_name} ({self.baudrate} baud)")
                self._send_at("AT")
                self._send_at("ATE0")
                self._send_at("AT+CMEE=1")
                self._send_at("AT+CMGF=1") # Text mode
                self.update_signal()
                self.read_sim_card_details(force_open=False)
            except Exception as e:
                self.is_mock = True
                self.status = "MOCK"
                self.ser = None
                self.last_error = str(e)
                print(f"⚠️ [MODEM SLOT] {self.name} ({self.port_name}) not physically detected -> Running in MOCK MODE (Error: {e})")

    def read_sim_card_details(self, force_open=True):
        """Reads SIM Card Phone Number (+CNUM), ICCID (+CCID), and IMSI (+CIMI)"""
        if force_open and not self.ser:
            self.open_port()
        if self.is_mock or not self.ser:
            return {
                "sim_number": self.sim_number,
                "iccid": self.iccid,
                "imsi": self.imsi,
                "operator": self.sim_operator,
                "cpin": "MOCK",
                "error": self.last_error
            }
        with self.lock:
            try:
                # 1. Check CPIN (PIN status)
                cpin_raw = self._send_at("AT+CPIN?", delay=0.3)
                cpin = "READY" if "READY" in cpin_raw else cpin_raw.replace("\r", " ").replace("\n", " ").strip()

                # 2. Try to query Phone Number via AT+CNUM
                num = None
                cnum_raw = self._send_at("AT+CNUM", delay=0.5)
                if "+CNUM:" in cnum_raw:
                    for line in cnum_raw.splitlines():
                        if "+CNUM:" in line:
                            parts = line.split("+CNUM:")[1].split(",")
                            if len(parts) >= 2:
                                parsed_num = parts[1].replace('"', '').strip()
                                if parsed_num:
                                    num = parsed_num
                                    break
                
                # 3. Query ICCID (SIM card serial number, 19-20 digits)
                iccid = None
                for cmd in ["AT+CCID", "AT+QCCID", "AT^ICCID?"]:
                    ccid_raw = self._send_at(cmd, delay=0.3)
                    clean = ccid_raw.replace("OK", "").replace("+CCID:", "").replace("+QCCID:", "").replace("^ICCID:", "").strip()
                    digits = "".join([c for c in clean if c.isdigit()])
                    if len(digits) >= 15:
                        iccid = digits
                        break

                # 4. Query IMSI (15 digits)
                imsi = None
                cimi_raw = self._send_at("AT+CIMI", delay=0.3)
                clean_imsi = "".join([c for c in cimi_raw.replace("OK", "").strip() if c.isdigit()])
                if len(clean_imsi) >= 10:
                    imsi = clean_imsi

                # 5. Query Operator via AT+COPS?
                cops_raw = self._send_at("AT+COPS?", delay=0.4)
                operator = self.sim_operator
                if ',"' in cops_raw:
                    try:
                        operator = cops_raw.split(',"')[1].split('"')[0].strip()
                    except Exception:
                        pass

                if num:
                    self.sim_number = num
                if iccid:
                    self.iccid = iccid
                if imsi:
                    self.imsi = imsi
                if operator and operator != "Auto":
                    self.sim_operator = operator

                self._update_sim_db()

                return {
                    "sim_number": self.sim_number,
                    "iccid": self.iccid,
                    "imsi": self.imsi,
                    "operator": self.sim_operator,
                    "cpin": cpin
                }
            except Exception as e:
                print(f"Error reading SIM on {self.name}: {e}")
                return {
                    "sim_number": self.sim_number,
                    "iccid": self.iccid,
                    "imsi": self.imsi,
                    "operator": self.sim_operator,
                    "cpin": f"Error: {e}"
                }

    def _update_sim_db(self):
        try:
            conn = get_db_connection()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE gateways 
                        SET sim_number=%s, iccid=%s, imsi=%s, sim_operator=%s 
                        WHERE id=%s
                    """, (self.sim_number, self.iccid, self.imsi, self.sim_operator, self.id))
                conn.close()
        except Exception:
            pass

    def check_incoming_sms(self):
        """Checks SIM card memory for incoming SMS (AT+CMGL) and saves to inbox table"""
        if self.is_mock or not self.ser:
            return []
        received = []
        with self.lock:
            try:
                self._send_at("AT+CMGF=1") # Text mode
                resp = self._send_at('AT+CMGL="ALL"', delay=1.0)
                lines = resp.splitlines()
                idx = 0
                while idx < len(lines):
                    line = lines[idx].strip()
                    if line.startswith("+CMGL:"):
                        parts = line.split(",")
                        sender = ""
                        if len(parts) >= 3:
                            sender = parts[2].replace('"', '').strip()
                        msg_body = ""
                        if idx + 1 < len(lines):
                            next_l = lines[idx + 1].strip()
                            if not next_l.startswith("+CMGL:") and next_l != "OK":
                                msg_body = next_l
                        
                        if sender and msg_body:
                            received.append({"sender": sender, "message": msg_body, "received_port": self.port_name})
                            try:
                                conn = get_db_connection()
                                if conn:
                                    with conn.cursor() as cur:
                                        cur.execute("SELECT id FROM inbox WHERE sender = %s AND message = %s AND received_port = %s LIMIT 1", (sender, msg_body, self.port_name))
                                        if not cur.fetchone():
                                            cur.execute("INSERT INTO inbox (sender, message, received_port, received_at) VALUES (%s, %s, %s, NOW())", (sender, msg_body, self.port_name))
                                    conn.close()
                            except Exception:
                                pass
                    idx += 1
            except Exception as e:
                print(f"Error checking incoming SMS on {self.name}: {e}")
        return received

    def _send_at(self, cmd, delay=0.3):
        if self.is_mock or not self.ser:
            return "OK"
        with self.lock:
            try:
                self.ser.write((cmd + "\r\n").encode())
                time.sleep(delay)
                if self.ser.in_waiting > 0:
                    return self.ser.read(self.ser.in_waiting).decode(errors="ignore").strip()
            except Exception as e:
                print(f"Serial Error on {self.name}: {e}")
        return ""

    def update_signal(self):
        if self.is_mock or not self.ser:
            return
        resp = self._send_at("AT+CSQ", delay=0.5)
        if "+CSQ:" in resp:
            try:
                val = int(resp.split("+CSQ:")[1].split(",")[0].strip())
                if val != 99:
                    self.signal_csq = val
            except Exception:
                pass

    def transmit_sms(self, phone_number, message):
        """Sends SMS with timeout, prompt '>' detection, and ESC abort recovery"""
        with self.lock:
            if self.is_mock or not self.ser:
                print(f"📤 [{self.name} - MOCK] Sending SMS to {phone_number}: {message}")
                time.sleep(0.5)
                self.sent_count += 1
                self._update_db_stats()
                return True

            print(f"📤 [{self.name}] Transmitting SMS to {phone_number} via {self.port_name}...")
            try:
                self._send_at("AT+CMGF=1")
                self.ser.write(f'AT+CMGS="{phone_number}"\r'.encode())
                time.sleep(1)
                
                resp = ""
                if self.ser.in_waiting > 0:
                    resp = self.ser.read(self.ser.in_waiting).decode(errors="ignore")

                # If modem prompt '>' was not received, abort with ESC (\x1B)
                if ">" not in resp:
                    print(f"❌ [{self.name}] No prompt from modem. Aborting...")
                    self.ser.write(b'\x1B')
                    self.failed_count += 1
                    self._update_db_stats()
                    return False

                # Write message body + Ctrl+Z (\x1A)
                self.ser.write((message + "\x1A").encode())
                time.sleep(3)
                
                final_resp = ""
                if self.ser.in_waiting > 0:
                    final_resp = self.ser.read(self.ser.in_waiting).decode(errors="ignore").strip()

                if "+CMGS:" in final_resp or "OK" in final_resp:
                    print(f"✅ [{self.name}] Message delivered to {phone_number}")
                    self.sent_count += 1
                    self._update_db_stats()
                    return True
                else:
                    print(f"❌ [{self.name}] Send error: {final_resp}")
                    self.failed_count += 1
                    self._update_db_stats()
                    return False

            except Exception as e:
                print(f"❌ [{self.name}] Serial Exception: {e}")
                try:
                    self.ser.write(b'\x1B')
                except Exception:
                    pass
                self.failed_count += 1
                self._update_db_stats()
                return False

    def _update_db_stats(self):
        try:
            conn = get_db_connection()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE gateways 
                        SET sent_count=%s, failed_count=%s, signal_csq=%s, status=%s 
                        WHERE id=%s
                    """, (self.sent_count, self.failed_count, self.signal_csq, self.status, self.id))
                conn.close()
        except Exception:
            pass

    def to_dict(self, include_sensitive=False):
        if include_sensitive:
            displayed_key = self.api_key
        else:
            displayed_key = (self.api_key[:7] + "..." + self.api_key[-4:]) if (self.api_key and len(self.api_key) > 12) else "********"

        return {
            "id": self.id,
            "name": self.name,
            "port": self.port_name,
            "baudrate": self.baudrate,
            "sim_operator": self.sim_operator,
            "sim_number": self.sim_number,
            "iccid": self.iccid,
            "imsi": self.imsi,
            "api_key": displayed_key,
            "occupied_by": self.occupied_by,
            "prefix_filter": ",".join(self.prefix_filter),
            "is_active": self.is_active,
            "signal_csq": self.signal_csq,
            "status": self.status,
            "is_mock": self.is_mock,
            "sent_count": self.sent_count,
            "failed_count": self.failed_count
        }


class GatewayManager:
    """Manages pool of modem slots, dynamic addition, prefix routing, and round-robin"""
    def __init__(self):
        self.slots = {} # {slot_id: ModemSlot}
        self.lock = threading.Lock()
        self.rr_index = 0
        self.load_slots_from_db()

    def load_slots_from_db(self):
        with self.lock:
            conn = get_db_connection()
            if not conn:
                return
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, name, port, baudrate, sim_operator, prefix_filter, is_active, sent_count, failed_count, sim_number, iccid, imsi, api_key, occupied_by FROM gateways")
                    rows = cur.fetchall()
                
                existing_ids = set()
                for r in rows:
                    slot_id = r[0]
                    existing_ids.add(slot_id)
                    if slot_id not in self.slots:
                        slot = ModemSlot(slot_id, r[1], r[2], r[3], r[4], r[5] or "", sim_number=r[9], iccid=r[10], imsi=r[11], api_key=r[12], occupied_by=r[13])
                        slot.is_active = bool(r[6])
                        slot.sent_count = r[7] or 0
                        slot.failed_count = r[8] or 0
                        self.slots[slot_id] = slot
                    else:
                        # Update config
                        self.slots[slot_id].name = r[1]
                        self.slots[slot_id].is_active = bool(r[6])
                        self.slots[slot_id].prefix_filter = [p.strip() for p in (r[5] or "").split(',') if p.strip()]
                        self.slots[slot_id].sim_number = r[9]
                        self.slots[slot_id].iccid = r[10]
                        self.slots[slot_id].imsi = r[11]
                        self.slots[slot_id].api_key = r[12]
                        self.slots[slot_id].occupied_by = r[13]

                # Clean up removed slots
                for sid in list(self.slots.keys()):
                    if sid not in existing_ids:
                        del self.slots[sid]
            finally:
                conn.close()

    def select_slot(self, recipient, preferred_gateway=None):
        """Intelligent routing: Preferred -> Prefix Match -> Round Robin"""
        with self.lock:
            active_slots = [s for s in self.slots.values() if s.is_active]
            if not active_slots:
                return None

            # 1. Preferred Gateway/Slot specified by request
            if preferred_gateway:
                pref = preferred_gateway.strip().lower()
                for s in active_slots:
                    if s.name.lower() == pref or s.port_name.lower() == pref or str(s.id) == pref:
                        return s

            # 2. Cellular Prefix Routing (e.g. 0917 -> Globe Slot)
            norm_num = recipient.replace("+63", "0").strip()
            for s in active_slots:
                for pfx in s.prefix_filter:
                    if pfx and norm_num.startswith(pfx):
                        return s

            # 3. Round-Robin Load Balancing
            self.rr_index = (self.rr_index + 1) % len(active_slots)
            return active_slots[self.rr_index]

    def send_sms(self, recipient, message, preferred_gateway=None, connector_source="HTTP"):
        slot = self.select_slot(recipient, preferred_gateway)
        if not slot:
            return False, "No active modem slot available"

        success = slot.transmit_sms(recipient, message)
        
        # Log to legacy sms_logs
        log_sms(recipient, message, "Sent" if success else "Failed", slot.name)

        # Log to outbox table for live queue & logs dashboard
        try:
            conn = get_db_connection()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO outbox (recipient, message, preferred_gateway, status, dispatched_via, connector)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (recipient, message, preferred_gateway, "SENT" if success else "FAILED", slot.name, connector_source))
                conn.close()
        except Exception as e:
            print("Outbox log error:", e)

        return success, slot.name

# Instantiate Gateway Manager
gateway_manager = GatewayManager()

# ==============================================================================
# SQL CONNECTOR BACKGROUND WORKER (Poller)
# ==============================================================================

def sql_connector_worker():
    """Continuously checks the outbox table and blasts messages via modem slots"""
    while True:
        try:
            conn = get_db_connection()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, recipient, message, preferred_gateway, retry_count 
                        FROM outbox 
                        WHERE status = 'PENDING' AND retry_count < 3 
                        ORDER BY id ASC LIMIT 5
                    """)
                    pending = cur.fetchall()

                for job in pending:
                    job_id, recipient, message, preferred, retries = job
                    
                    # Mark PROCESSING
                    with conn.cursor() as cur:
                        cur.execute("UPDATE outbox SET status='PROCESSING' WHERE id=%s", (job_id,))

                    success, dispatched_name = gateway_manager.send_sms(
                        recipient, message, preferred_gateway=preferred, connector_source="SQL"
                    )

                    with conn.cursor() as cur:
                        if success:
                            cur.execute("""
                                UPDATE outbox 
                                SET status='SENT', dispatched_via=%s 
                                WHERE id=%s
                            """, (dispatched_name, job_id))
                        else:
                            cur.execute("""
                                UPDATE outbox 
                                SET status='FAILED', retry_count=retry_count+1, error_message='Modem delivery failure' 
                                WHERE id=%s
                            """, (job_id,))

                conn.close()
        except Exception as e:
            # Prevent background poller from crashing
            pass
        time.sleep(1.5)

threading.Thread(target=sql_connector_worker, daemon=True).start()

# Periodic Signal Strength Checker (Every 45 seconds)
def signal_watchdog_worker():
    while True:
        time.sleep(45)
        for s in list(gateway_manager.slots.values()):
            if s.is_active and not s.is_mock:
                s.update_signal()

threading.Thread(target=signal_watchdog_worker, daemon=True).start()

# Plug-and-Play Hardware Auto-Reconnection Watchdog (Every 5 seconds)
def pnp_port_watchdog():
    """Automatically detects when a physical GSM modem is plugged in or unplugged"""
    while True:
        time.sleep(5)
        try:
            available_ports = [p.device.upper() for p in serial.tools.list_ports.comports()]
            for slot in list(gateway_manager.slots.values()):
                # If slot is in MOCK mode and its hardware COM port is now plugged in
                if slot.is_mock and slot.port_name.upper() in available_ports:
                    print(f"🔌 [PnP] Hardware detected on {slot.port_name}! Connecting {slot.name}...")
                    slot.open_port()
                # If slot was ONLINE and its hardware COM port was unplugged
                elif not slot.is_mock and slot.port_name.upper() not in available_ports:
                    print(f"🔌 [PnP] Hardware unplugged from {slot.port_name}! Switching {slot.name} to MOCK MODE...")
                    slot.is_mock = True
                    slot.status = "MOCK"
                    if slot.ser:
                        try:
                            slot.ser.close()
                        except Exception:
                            pass
                        slot.ser = None
                    slot._update_db_stats()
        except Exception:
            pass

threading.Thread(target=pnp_port_watchdog, daemon=True).start()

def probe_port_for_modem(port_name, baudrate=115200):
    """Probes a COM port to determine if it is a real GSM modem, detect model, SIM readiness, and carrier"""
    try:
        ser = serial.Serial(port_name, baudrate=baudrate, timeout=1.5)
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        
        # 1. Ping AT
        ser.write(b"AT\r\n")
        time.sleep(0.3)
        resp = ""
        if ser.in_waiting > 0:
            resp = ser.read(ser.in_waiting).decode(errors="ignore")

        if "OK" not in resp:
            ser.close()
            return None

        ser.write(b"ATE0\r\n")
        time.sleep(0.2)
        if ser.in_waiting > 0:
            ser.read(ser.in_waiting)

        # 2. Check Hardware Model (AT+CGMM)
        model = "GSM Modem"
        ser.write(b"AT+CGMM\r\n")
        time.sleep(0.3)
        if ser.in_waiting > 0:
            m_resp = ser.read(ser.in_waiting).decode(errors="ignore").replace("OK", "").strip()
            if m_resp:
                model = m_resp.split("\n")[0].strip()

        # 3. Check SIM Readiness (AT+CPIN?)
        sim_ready = False
        ser.write(b"AT+CPIN?\r\n")
        time.sleep(0.3)
        if ser.in_waiting > 0:
            cpin_resp = ser.read(ser.in_waiting).decode(errors="ignore")
            sim_ready = "READY" in cpin_resp

        # 4. Query Carrier (AT+COPS?)
        carrier = "Auto"
        ser.write(b"AT+COPS?\r\n")
        time.sleep(0.5)
        cops_resp = ""
        if ser.in_waiting > 0:
            cops_resp = ser.read(ser.in_waiting).decode(errors="ignore")

        cops_lower = cops_resp.lower()
        if "globe" in cops_lower:
            carrier = "Globe"
        elif "smart" in cops_lower or "tnt" in cops_lower:
            carrier = "Smart"
        elif "dito" in cops_lower:
            carrier = "DITO"
        elif "sun" in cops_lower:
            carrier = "Sun"

        # 5. Query Signal (AT+CSQ)
        csq = 18
        ser.write(b"AT+CSQ\r\n")
        time.sleep(0.3)
        if ser.in_waiting > 0:
            csq_resp = ser.read(ser.in_waiting).decode(errors="ignore")
            if "+CSQ:" in csq_resp:
                try:
                    csq = int(csq_resp.split("+CSQ:")[1].split(",")[0].strip())
                except Exception:
                    pass

        ser.close()
        return {
            "port": port_name,
            "carrier": carrier,
            "signal_csq": csq,
            "sim_ready": sim_ready,
            "model": model
        }
    except Exception:
        return None


# Database Helpers
def log_sms(phone_number, message, status, slot_info=""):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            msg_with_slot = f"[{slot_info}] {message}" if slot_info else message
            cursor.execute("""
                INSERT INTO sms_logs (phone_number, message, status, sent_at)
                VALUES (%s, %s, %s, NOW())
            """, (phone_number, msg_with_slot, status))
    except Exception as e:
        print(f"Log Error: {e}")
    finally:
        conn.close()

def get_all_contacts():
    conn = get_db_connection()
    if not conn: return []
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, name, phone_number, department FROM contacts ORDER BY name ASC")
            return cursor.fetchall()
    finally:
        conn.close()

def get_logs_for_phone(phone_number):
    conn = get_db_connection()
    if not conn: return []
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT message, status, sent_at, 'out' as direction 
                FROM sms_logs 
                WHERE phone_number = %s 
                ORDER BY sent_at ASC
            """, (phone_number,))
            return cursor.fetchall()
    finally:
        conn.close()

def get_department_logs(department):
    conn = get_db_connection()
    if not conn: return []
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT phone_number FROM contacts WHERE department = %s", (department,))
            numbers = [r[0] for r in cursor.fetchall()]
            if not numbers: return []
            format_strings = ','.join(['%s'] * len(numbers))
            cursor.execute(f"""
                SELECT message, MIN(status), MIN(sent_at) as sent_time, COUNT(*) as recipient_count
                FROM sms_logs 
                WHERE phone_number IN ({format_strings}) 
                GROUP BY message, UNIX_TIMESTAMP(sent_at) DIV 60
                ORDER BY sent_time ASC
            """, tuple(numbers))
            return cursor.fetchall()
    finally:
        conn.close()

# ==============================================================================
# APPLICATION ROUTES & CONNECTORS
# ==============================================================================

@app.route('/')
def home():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        client_ip = request.remote_addr or '127.0.0.1'
        if is_ip_rate_limited(client_ip):
            return render_template('login.html', error="Too many failed attempts. Login locked for 15 minutes.")

        email = (request.form.get('email') or '').strip()
        password = (request.form.get('password') or '').strip()

        admin_email = os.environ.get('ADMIN_EMAIL', 'pexdev@email.com')
        admin_hash = os.environ.get('ADMIN_PASSWORD_HASH')

        is_valid = False
        if email == admin_email:
            if admin_hash:
                is_valid = check_password_hash(admin_hash, password)
            elif password == 'pexdev@email.com':
                is_valid = True

        if is_valid:
            record_login_attempt(client_ip, success=True)
            session['logged_in'] = True
            session.permanent = True
            return redirect(url_for('home'))
        else:
            record_login_attempt(client_ip, success=False)
            return render_template('login.html', error="Invalid email or password.")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# ------------------------------------------------------------------------------
# CONNECTOR 1: DIAFAAN-COMPATIBLE HTTP WEB CONNECTOR
# Authenticated via query param/header (password, token, or API key)
# ------------------------------------------------------------------------------
@app.route('/http/send-message/', methods=['GET', 'POST'])
def diafaan_connector_endpoint():
    data = request.get_json(silent=True) or {}
    to = request.args.get('to') or request.form.get('to') or data.get('to') or data.get('phone_number')
    msg = request.args.get('message') or request.form.get('message') or data.get('message')
    gw = request.args.get('gateway') or request.form.get('gateway') or data.get('gateway')

    # Security check: require password, token, or API key matching slot or master key
    provided_key = (request.args.get('password') or request.form.get('password') or 
                    request.args.get('token') or request.args.get('api_key') or 
                    request.headers.get('X-API-Key') or request.headers.get('X-Master-Key'))
    auth_header = request.headers.get('Authorization')
    if not provided_key and auth_header and auth_header.startswith('Bearer '):
        provided_key = auth_header.split(' ')[1]

    master_key = os.environ.get('GATEWAY_MASTER_KEY')
    matched_slot = None
    if provided_key:
        with gateway_manager.lock:
            for s in gateway_manager.slots.values():
                if s.api_key == provided_key:
                    matched_slot = s
                    break

    is_authorized = bool(matched_slot or (master_key and provided_key == master_key) or session.get('logged_in'))
    if not is_authorized:
        return "Unauthorized: Valid API key or token required", 401

    if matched_slot:
        gw = str(matched_slot.id)

    if not to or not msg:
        return "Error: Missing 'to' or 'message' parameter", 400

    clean_to = re.sub(r'[\s\-\(\)]', '', str(to))
    if not re.match(r'^\+?[0-9]{7,15}$', clean_to):
        return "Error: Invalid phone number format", 400

    clean_msg = str(msg).replace('\x00', '').replace('\x1a', '').replace('\x1b', '').strip()
    if not clean_msg:
        return "Error: Message body cannot be empty", 400

    success, slot_name = gateway_manager.send_sms(clean_to, clean_msg, preferred_gateway=gw, connector_source="HTTP_Diafaan")
    
    if success:
        return f"Message sent successfully to {clean_to} via {slot_name}", 200
    else:
        return f"Failed to deliver message to {clean_to} ({slot_name})", 500

# ------------------------------------------------------------------------------
# CONNECTOR 2: MODERN JSON REST API
# Strict authentication (Slot API Key or Master Key) and sanitized inputs
# ------------------------------------------------------------------------------
@app.route('/api/v1/sms/send', methods=['POST'])
@app.route('/send-sms', methods=['POST'])
@app.route('/api/send', methods=['POST'])
def send_message_rest():
    data = request.get_json(silent=True) or {}
    target = data.get('target') or data.get('phone_number') or data.get('to')
    message = data.get('message')
    preferred_gw = data.get('gateway')
    type_ = data.get('type', 'individual')

    auth_header = request.headers.get('Authorization')
    api_key_header = request.headers.get('X-API-Key') or request.headers.get('X-Master-Key')
    provided_key = api_key_header
    if not provided_key and auth_header and auth_header.startswith('Bearer '):
        provided_key = auth_header.split(' ')[1]

    # Check Slot-specific API Key first
    matched_slot_by_key = None
    if provided_key:
        with gateway_manager.lock:
            for s in gateway_manager.slots.values():
                if s.api_key == provided_key:
                    matched_slot_by_key = s
                    break
    
    if matched_slot_by_key:
        preferred_gw = str(matched_slot_by_key.id)
    else:
        # Check Master Key or Admin Session
        master_key = os.environ.get('GATEWAY_MASTER_KEY')
        is_valid_master = bool(master_key and provided_key == master_key)
        is_session_admin = bool(session.get('logged_in'))
        if not is_valid_master and not is_session_admin:
            return jsonify({"success": False, "error": "Unauthorized: Valid slot API key or Master Key required"}), 401

    if not target or not message:
        return jsonify({"success": False, "error": "Missing target phone number or message content"}), 400

    # Sanitize and validate inputs
    if type_ != 'group':
        target_clean = re.sub(r'[\s\-\(\)]', '', str(target))
        if not re.match(r'^\+?[0-9]{7,15}$', target_clean):
            return jsonify({"success": False, "error": "Invalid phone number format. Must be 7-15 digits with optional '+' prefix."}), 400
        target = target_clean

    clean_message = str(message).replace('\x00', '').replace('\x1a', '').replace('\x1b', '').strip()
    if not clean_message:
        return jsonify({"success": False, "error": "Message body cannot be empty"}), 400

    if type_ == 'group':
        conn = get_db_connection()
        if conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT phone_number FROM contacts WHERE department = %s", (target,))
                rows = cursor.fetchall()
            conn.close()

            if not rows:
                return jsonify({"success": False, "error": "Empty group"}), 404

            success_count = 0
            for row in rows:
                row_clean = re.sub(r'[\s\-\(\)]', '', str(row[0]))
                ok, _ = gateway_manager.send_sms(row_clean, clean_message, preferred_gateway=preferred_gw, connector_source="Broadcast")
                if ok:
                    success_count += 1

            return jsonify({"success": True, "info": f"Sent to {success_count}/{len(rows)} recipients"})
        else:
            return jsonify({"success": False, "error": "Database error"}), 500
    else:
        success, slot_name = gateway_manager.send_sms(target, clean_message, preferred_gateway=preferred_gw, connector_source="REST")
        if success:
            return jsonify({"success": True, "dispatched_via": slot_name})
        else:
            return jsonify({"success": False, "error": f"Transmission failed ({slot_name})"}), 500

# ------------------------------------------------------------------------------
# GATEWAYS (MODEM SLOTS) MANAGEMENT APIS
# ------------------------------------------------------------------------------
@app.route('/api/v1/gateways', methods=['GET', 'POST'])
def manage_gateways_api():
    if request.method == 'GET':
        gateway_manager.load_slots_from_db()
        is_admin = is_admin_authenticated()
        slot_list = [s.to_dict(include_sensitive=is_admin) for s in gateway_manager.slots.values()]
        return jsonify(slot_list)

    # ADD A NEW MODEM SLOT - Admin authentication required
    if not is_admin_authenticated():
        return jsonify({"success": False, "error": "Unauthorized: Admin authentication or Master Key required"}), 401

    data = request.get_json() or {}
    name = data.get('name')
    port = data.get('port')
    baudrate = int(data.get('baudrate', 115200))
    sim_operator = data.get('sim_operator', 'Auto')
    prefix_filter = data.get('prefix_filter', '')

    if not name or not port:
        return jsonify({"success": False, "error": "Name and Port are required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "DB connection failed"}), 500

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO gateways (name, port, baudrate, sim_operator, prefix_filter, is_active)
                VALUES (%s, %s, %s, %s, %s, 1)
            """, (name, port, baudrate, sim_operator, prefix_filter))
        conn.close()

        gateway_manager.load_slots_from_db()
        return jsonify({"success": True, "message": f"Modem slot '{name}' added successfully"}), 201
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/v1/gateways/<int:gw_id>', methods=['GET', 'PUT', 'POST', 'DELETE'])
def manage_single_gateway_api(gw_id):
    if request.method == 'DELETE':
        if not is_admin_authenticated():
            return jsonify({"success": False, "error": "Unauthorized: Admin authentication or Master Key required"}), 401
        conn = get_db_connection()
        if conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM gateways WHERE id = %s", (gw_id,))
            conn.close()
            gateway_manager.load_slots_from_db()
            return jsonify({"success": True, "message": "Gateway slot removed"})
        return jsonify({"success": False, "error": "Database error"}), 500

    slot = gateway_manager.slots.get(gw_id)
    if not slot:
        return jsonify({"success": False, "error": "Gateway slot not found"}), 404

    if request.method == 'GET':
        is_admin = is_admin_authenticated()
        return jsonify({"success": True, "slot": slot.to_dict(include_sensitive=is_admin)})

    # PUT or POST: Update configuration settings - Admin authentication required
    if not is_admin_authenticated():
        return jsonify({"success": False, "error": "Unauthorized: Admin authentication or Master Key required"}), 401

    data = request.get_json(silent=True) or {}
    name = data.get('name', slot.name).strip()
    port = data.get('port', slot.port_name).strip()
    baudrate = int(data.get('baudrate', slot.baudrate))
    sim_operator = data.get('sim_operator', slot.sim_operator).strip()
    sim_number = data.get('sim_number', slot.sim_number or '').strip()
    prefix_filter = data.get('prefix_filter', ','.join(slot.prefix_filter)).strip()
    is_active = 1 if data.get('is_active', slot.is_active) else 0

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database error"}), 500

    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE gateways 
                SET name = %s, port = %s, baudrate = %s, sim_operator = %s, 
                    sim_number = %s, prefix_filter = %s, is_active = %s 
                WHERE id = %s
            """, (name, port, baudrate, sim_operator, sim_number, prefix_filter, is_active, gw_id))
        conn.close()

        reconnect_needed = (port.upper() != slot.port_name.upper() or baudrate != slot.baudrate)
        slot.name = name
        slot.sim_operator = sim_operator
        slot.sim_number = sim_number
        slot.prefix_filter = [p.strip() for p in prefix_filter.split(',') if p.strip()]
        slot.is_active = bool(is_active)

        if reconnect_needed:
            if slot.ser:
                try:
                    slot.ser.close()
                except Exception:
                    pass
                slot.ser = None
            slot.port_name = port
            slot.baudrate = baudrate
            slot.open_port()

        is_admin = is_admin_authenticated()
        return jsonify({"success": True, "message": "Configuration saved successfully", "slot": slot.to_dict(include_sensitive=is_admin)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/v1/gateways/<int:gw_id>/logs', methods=['GET'])
@admin_required
def get_gateway_slot_logs_api(gw_id):
    slot = gateway_manager.slots.get(gw_id)
    if not slot:
        return jsonify({"success": False, "error": "Gateway not found"}), 404

    # Optionally trigger reading SMS from SIM if requested
    if request.args.get('check_inbox') == '1' and slot.ser and not slot.is_mock:
        slot.check_incoming_sms()

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database error"}), 500

    try:
        sent_logs = []
        received_logs = []
        with conn.cursor() as cur:
            # Query sent logs from outbox
            cur.execute("""
                SELECT recipient, message, status, connector, created_at 
                FROM outbox 
                WHERE dispatched_via = %s OR dispatched_via LIKE %s 
                ORDER BY created_at DESC LIMIT 50
            """, (slot.name, f"%{slot.port_name}%"))
            for r in cur.fetchall():
                sent_logs.append({
                    "recipient": r[0],
                    "message": r[1],
                    "status": r[2],
                    "connector": r[3],
                    "time": r[4].strftime("%Y-%m-%d %H:%M:%S") if r[4] else None
                })

            # If outbox empty, check legacy sms_logs
            if not sent_logs:
                cur.execute("""
                    SELECT phone_number, message, status, sent_at 
                    FROM sms_logs 
                    WHERE message LIKE %s 
                    ORDER BY sent_at DESC LIMIT 50
                """, (f"%[{slot.name}]%",))
                for r in cur.fetchall():
                    clean_msg = r[1].replace(f"[{slot.name}]", "").strip()
                    sent_logs.append({
                        "recipient": r[0],
                        "message": clean_msg,
                        "status": r[2],
                        "connector": "DIRECT",
                        "time": r[3].strftime("%Y-%m-%d %H:%M:%S") if r[3] else None
                    })

            # Query received logs from inbox
            cur.execute("""
                SELECT sender, message, received_port, received_at 
                FROM inbox 
                WHERE received_port = %s OR received_port = %s 
                ORDER BY received_at DESC LIMIT 50
            """, (slot.port_name, slot.name))
            for r in cur.fetchall():
                received_logs.append({
                    "sender": r[0],
                    "message": r[1],
                    "port": r[2],
                    "time": r[3].strftime("%Y-%m-%d %H:%M:%S") if r[3] else None
                })

        return jsonify({
            "success": True,
            "slot_id": gw_id,
            "slot_name": slot.name,
            "port": slot.port_name,
            "sent": sent_logs,
            "received": received_logs
        })
    finally:
        conn.close()

@app.route('/api/v1/gateways/<int:gw_id>/at-command', methods=['POST'])
@admin_required
def gateway_at_command_api(gw_id):
    slot = gateway_manager.slots.get(gw_id)
    if not slot:
        return jsonify({"success": False, "error": "Gateway not found"}), 404
    data = request.get_json(silent=True) or {}
    cmd = data.get('command', '').strip()
    if not cmd:
        return jsonify({"success": False, "error": "Command is required"}), 400

    # Prevent AT command injection and binary control characters
    if any(c in cmd for c in ['\r', '\n', '\x00', '\x1a', '\x1b']):
        return jsonify({"success": False, "error": "Invalid characters in AT command"}), 400

    if (not slot.ser or not slot.ser.is_open) and not slot.is_mock:
        slot.open_port()

    if slot.ser and slot.ser.is_open and not slot.is_mock:
        resp = slot._send_at(cmd, delay=0.5)
        return jsonify({"success": True, "command": cmd, "response": resp})

    cmd_clean = cmd.upper().strip()
    mock_responses = {
        "AT": "OK",
        "AT+CSQ": "+CSQ: 18,0\r\n\r\nOK",
        "AT+CPIN?": "+CPIN: READY\r\n\r\nOK",
        "AT+COPS?": f'+COPS: 0,0,"{slot.sim_operator}"\r\n\r\nOK',
        "AT+CCID": f'+CCID: "{slot.iccid or "89634252475512838177"}"\r\n\r\nOK',
        "ATI": "Wavecom MULTIBAND 900E 1800\r\nRevision: 651_09gg.2Q\r\n\r\nOK",
        "AT+CGMM": "MULTIBAND 900E 1800\r\nOK",
        "AT+CMGF=1": "OK",
        "AT+CMGL=\"ALL\"": "OK"
    }
    resp = mock_responses.get(cmd_clean, "OK\r\n(Simulated response - Mock Mode)")
    return jsonify({"success": True, "command": cmd, "response": resp, "is_mock": True})

@app.route('/api/v1/gateways/<int:gw_id>/check-inbox', methods=['POST'])
def gateway_check_inbox_api(gw_id):
    slot = gateway_manager.slots.get(gw_id)
    if not slot:
        return jsonify({"success": False, "error": "Gateway not found"}), 404

    # Verify authorization (Slot API key or Admin/Master Key)
    provided_key = request.headers.get('X-API-Key') or request.headers.get('X-Master-Key')
    auth_header = request.headers.get('Authorization')
    if not provided_key and auth_header and auth_header.startswith('Bearer '):
        provided_key = auth_header.split(' ')[1]

    master_key = os.environ.get('GATEWAY_MASTER_KEY')
    is_authorized = bool(
        session.get('logged_in') or 
        (provided_key and provided_key == slot.api_key) or 
        (master_key and provided_key == master_key)
    )
    if not is_authorized:
        return jsonify({"success": False, "error": "Unauthorized: Slot API key or Admin access required"}), 401

    msgs = slot.check_incoming_sms()
    return jsonify({"success": True, "new_messages_count": len(msgs), "messages": msgs})

@app.route('/api/v1/gateways/<int:gw_id>/occupy', methods=['POST'])
def gateway_occupy_api(gw_id):
    slot = gateway_manager.slots.get(gw_id)
    if not slot:
        return jsonify({"success": False, "error": "Gateway not found"}), 404
        
    data = request.get_json(silent=True) or {}
    system_name = (data.get('system_name') or '').strip()
    master_key = os.environ.get('GATEWAY_MASTER_KEY')

    auth_header = request.headers.get('Authorization')
    bearer_token = auth_header.split(' ')[1] if auth_header and auth_header.startswith('Bearer ') else None
    provided_key = data.get('api_key') or request.headers.get('X-API-Key') or request.headers.get('X-Master-Key') or bearer_token
    
    if not system_name:
        return jsonify({"success": False, "error": "Missing system_name"}), 400

    is_authorized = bool(
        (provided_key and (provided_key == slot.api_key or (master_key and provided_key == master_key))) or
        session.get('logged_in')
    )
    if not is_authorized:
        return jsonify({"success": False, "error": "Unauthorized: Valid slot API key or Master Key required"}), 401
        
    if slot.occupied_by and slot.occupied_by != system_name:
        return jsonify({"success": False, "error": f"Slot already occupied by {slot.occupied_by}"}), 409
        
    slot.occupied_by = system_name
    try:
        conn = get_db_connection()
        if conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE gateways SET occupied_by=%s WHERE id=%s", (system_name, gw_id))
            conn.close()
    except Exception as e:
        print("Error updating occupancy:", e)
        
    return jsonify({
        "success": True, 
        "message": f"Slot successfully occupied by {system_name}",
        "slot_id": gw_id,
        "api_key": slot.api_key
    })

@app.route('/api/v1/gateways/<int:gw_id>/toggle', methods=['POST'])
@admin_required
def toggle_gateway_api(gw_id):
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE gateways SET is_active = NOT is_active WHERE id = %s", (gw_id,))
        conn.close()
        gateway_manager.load_slots_from_db()
        return jsonify({"success": True, "message": "Gateway status toggled"})
    return jsonify({"success": False, "error": "Database error"}), 500

@app.route('/api/v1/gateways/<int:gw_id>/sim', methods=['GET', 'POST'])
@admin_required
def manage_gateway_sim_api(gw_id):
    slot = gateway_manager.slots.get(gw_id)
    if not slot:
        return jsonify({"success": False, "error": "Gateway slot not found"}), 404

    if request.method == 'GET':
        sim_data = slot.read_sim_card_details()
        return jsonify({"success": True, "slot_id": gw_id, "sim_info": sim_data})

    # POST: Update/Save custom SIM number
    data = request.get_json(silent=True) or {}
    sim_number = data.get('sim_number', '').strip()
    slot.sim_number = sim_number
    slot._update_sim_db()
    return jsonify({"success": True, "slot_id": gw_id, "sim_number": sim_number})

@app.route('/api/v1/gateways/sim-scan', methods=['POST'])
@admin_required
def scan_all_sims_api():
    results = {}
    for slot_id, slot in gateway_manager.slots.items():
        if slot.is_active:
            results[slot.name] = slot.read_sim_card_details()
    return jsonify({"success": True, "sim_cards": results})

@app.route('/api/v1/system/ports', methods=['GET'])
@admin_required
def scan_system_ports_api():
    """Scans physical hardware on the machine (e.g. COM19, COM20, COM21, COM22)"""
    try:
        ports = [p.device for p in serial.tools.list_ports.comports()]
        return jsonify({"ports": ports})
    except Exception as e:
        return jsonify({"ports": [], "error": str(e)})

@app.route('/api/v1/system/auto-detect', methods=['POST'])
@admin_required
def auto_detect_hardware_slots():
    """Scans all system COM ports, probes each for a GSM modem, and creates/updates slots"""
    try:
        com_ports = [p.device for p in serial.tools.list_ports.comports()]
        discovered = []
        real_modems_found = 0
        
        carrier_prefixes = {
            "Globe": "0917,0927,0915,0916,0926,0935,0936,0945,0955,0956,0965,0966,0967,0975,0976,0977,0995,0997",
            "Smart": "0918,0919,0920,0921,0928,0929,0939,0946,0947,0949,0951,0961,0998,0999",
            "DITO": "0991,0992,0993,0994",
            "Auto": ""
        }

        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "error": "Database error"}), 500

        # Step 1: Probe all active COM ports on the machine
        probed_results = {}
        for p in com_ports:
            info = probe_port_for_modem(p)
            probed_results[p] = info
            if info:
                real_modems_found += 1

        # Step 2: If physical modems are detected, remove initial placeholder mock slots
        if real_modems_found > 0:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM gateways WHERE status = 'MOCK' AND sent_count = 0")

        # Step 3: Register or update physical hardware slots
        for p in com_ports:
            info = probed_results[p]
            is_modem = bool(info)
            carrier = info['carrier'] if info else 'Auto'
            csq = info['signal_csq'] if info else 18
            sim_ready = info['sim_ready'] if info else False
            model = info['model'] if info else 'Generic Serial'
            pfx = carrier_prefixes.get(carrier, '')

            # Check if port already exists in DB
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM gateways WHERE port = %s", (p,))
                existing = cur.fetchone()

            slot_status = 'ONLINE' if is_modem else 'MOCK'
            sim_label = carrier if sim_ready else (f"{carrier} (No SIM)" if is_modem else "Auto")

            if existing:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE gateways 
                        SET sim_operator = %s, prefix_filter = %s, signal_csq = %s, status = %s, is_active = 1 
                        WHERE id = %s
                    """, (sim_label, pfx, csq, slot_status, existing[0]))
                discovered.append({"port": p, "name": existing[1], "carrier": sim_label, "action": "updated", "is_modem": is_modem, "model": model})
            else:
                slot_name = f"{p} - {model} ({sim_label})"
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO gateways (name, port, baudrate, sim_operator, prefix_filter, is_active, status, signal_csq)
                        VALUES (%s, %s, 115200, %s, %s, 1, %s, %s)
                    """, (slot_name, p, sim_label, pfx, slot_status, csq))
                discovered.append({"port": p, "name": slot_name, "carrier": sim_label, "action": "created", "is_modem": is_modem, "model": model})

        conn.close()
        gateway_manager.load_slots_from_db()

        return jsonify({
            "success": True,
            "scanned_ports": len(com_ports),
            "real_modems_found": real_modems_found,
            "discovered": discovered,
            "message": f"Hardware scan complete. Detected {len(com_ports)} COM port(s), {real_modems_found} responding GSM modem(s)."
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/v1/connectors', methods=['GET'])
@admin_required
def get_connectors_api():
    conn = get_db_connection()
    if not conn: return jsonify([])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, type, config, is_active, status, created_at FROM connectors")
            rows = cur.fetchall()
            connectors = []
            for r in rows:
                connectors.append({
                    "id": r[0],
                    "name": r[1],
                    "type": r[2],
                    "config": r[3],
                    "is_active": bool(r[4]),
                    "status": r[5],
                    "created_at": r[6].strftime("%Y-%m-%d %H:%M:%S") if r[6] else None
                })
            return jsonify(connectors)
    finally:
        conn.close()

@app.route('/api/v1/outbox', methods=['GET'])
@admin_required
def get_outbox_api():
    conn = get_db_connection()
    if not conn: return jsonify([])
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, recipient, message, status, dispatched_via, connector, created_at 
                FROM outbox 
                ORDER BY id DESC LIMIT 50
            """)
            rows = cur.fetchall()
            logs = []
            for r in rows:
                logs.append({
                    "id": r[0],
                    "recipient": r[1],
                    "message": r[2],
                    "status": r[3],
                    "dispatched_via": r[4] or "Pending",
                    "connector": r[5] or "HTTP",
                    "created_at": r[6].strftime("%Y-%m-%d %H:%M:%S") if r[6] else None
                })
            return jsonify(logs)
    finally:
        conn.close()

# Legacy Messenger APIs (Session Admin Protected)
@app.route('/api/init_data', methods=['GET'])
@admin_required
def get_init_data():
    raw_contacts = get_all_contacts()
    contacts = []
    groups = set()
    for c in raw_contacts:
        contacts.append({
            "id": c[0],
            "name": c[1],
            "phone": c[2],
            "department": c[3]
        })
        if c[3]:
            groups.add(c[3])
    return jsonify({
        "contacts": contacts,
        "groups": sorted(list(groups))
    })

@app.route('/api/messages', methods=['GET'])
@admin_required
def get_messages():
    target = request.args.get('target')
    type_ = request.args.get('type')
    if not target:
        return jsonify([])
    messages = []
    if type_ == 'group':
        logs = get_department_logs(target)
        for l in logs:
            count = l[3]
            messages.append({
                "text": l[0],
                "status": f"{l[1]}",
                "time": l[2].strftime("%I:%M %p"),
                "sender": "You",
                "recipient": f"All ({count})"
            })
    else:
        logs = get_logs_for_phone(target)
        for l in logs:
            messages.append({
                "text": l[0],
                "status": l[1],
                "time": l[2].strftime("%I:%M %p"),
                "sender": "You",
                "direction": l[3]
            })
    return jsonify(messages)

@app.route('/api/contacts', methods=['GET', 'POST'])
@admin_required
def contacts_route():
    if request.method == 'GET':
        raw_contacts = get_all_contacts()
        contacts = []
        for c in raw_contacts:
            contacts.append({
                "id": c[0],
                "name": c[1],
                "phone_number": c[2],
                "department": c[3]
            })
        return jsonify(contacts)

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()[:100]
    raw_phone = data.get('phone_number') or data.get('phone') or ''
    phone = re.sub(r'[\s\-\(\)]', '', str(raw_phone))
    dept = (data.get('department') or '').strip()[:100]
    
    if not name or not phone:
        return jsonify({"success": False, "error": "Name and phone number are required"}), 400

    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO contacts (name, phone_number, department) VALUES (%s, %s, %s)",
                               (name, phone, dept))
            return jsonify({"success": True}), 201
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()
    return jsonify({"success": False, "error": "DB Error"}), 500

@app.route('/api/logs', methods=['GET'])
@admin_required
def get_all_logs():
    conn = get_db_connection()
    if not conn: return jsonify([])
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, phone_number, message, status, sent_at FROM sms_logs ORDER BY sent_at DESC LIMIT 50")
            rows = cursor.fetchall()
            logs = []
            for r in rows:
                logs.append({
                    "id": r[0],
                    "phone_number": r[1],
                    "message": r[2],
                    "status": r[3],
                    "sent_at": r[4].strftime("%Y-%m-%d %H:%M:%S") if r[4] else None
                })
            return jsonify(logs)
    finally:
        conn.close()

# Cleanup serial ports on shutdown
def close_all_serials():
    for slot in gateway_manager.slots.values():
        if slot.ser:
            try:
                slot.ser.close()
            except Exception:
                pass
atexit.register(close_all_serials)

if __name__ == '__main__':
    print("\n" + "="*60)
    print(" 🚀 OPENSMS-GATEWAY - MULTI-SLOT GSM MODEM MIDDLEMAN")
    print(" ="*60)
    
    preferred_port = int(os.environ.get("PORT", 8080))
    port_to_use = preferred_port
    
    for test_port in [preferred_port, 8080, 5001, 5050, 9710]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_sock:
                test_sock.bind(('0.0.0.0', test_port))
                port_to_use = test_port
                break
        except OSError:
            continue

    if port_to_use != preferred_port:
        print(f" ⚠️  Port {preferred_port} occupied. Auto-switched to port {port_to_use}.")

    print(f" ✅ STATUS: Gateway running on Port {port_to_use}")
    print(f" 📱 LOCAL DASHBOARD:  http://localhost:{port_to_use}")
    print(f" 📡 DIAFAAN CONNECTOR: http://localhost:{port_to_use}/http/send-message/")
    print(f" 🔌 REST API:         http://localhost:{port_to_use}/api/v1/sms/send")
    print("="*60 + "\n")

    app.run(host='0.0.0.0', port=port_to_use, debug=False)

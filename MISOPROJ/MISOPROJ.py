import sys
import os
import time
import serial
import serial.tools.list_ports
import pymysql
import atexit
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import datetime
import threading
import socket

# Ensure console output uses UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Initialize Flask app
app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app, resources={r"/*": {"origins": "*"}})

# MySQL Database Configuration
DB_CONFIG = {
    "host": "127.0.0.1",
    "user": "root",
    "password": "",
    "database": "sms_system",
    "port": 3306
}

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
    def __init__(self, slot_id, name, port_name, baudrate=115200, sim_operator='Auto', prefix_filter=''):
        self.id = slot_id
        self.name = name
        self.port_name = port_name
        self.baudrate = baudrate
        self.sim_operator = sim_operator
        self.prefix_filter = [p.strip() for p in prefix_filter.split(',') if p.strip()]
        self.ser = None
        self.is_mock = False
        self.status = "INITIALIZING"
        self.signal_csq = 18 # Default healthy signal for display
        self.sent_count = 0
        self.failed_count = 0
        self.is_active = True
        self.lock = threading.RLock()
        self.open_port()

    def open_port(self):
        with self.lock:
            try:
                self.ser = serial.Serial(self.port_name, baudrate=self.baudrate, timeout=2)
                self.is_mock = False
                self.status = "ONLINE"
                print(f"✅ [MODEM SLOT] {self.name} connected on {self.port_name} ({self.baudrate} baud)")
                self._send_at("AT")
                self._send_at("ATE0")
                self._send_at("AT+CMEE=1")
                self._send_at("AT+CMGF=1") # Text mode
                self.update_signal()
            except Exception as e:
                self.is_mock = True
                self.status = "MOCK"
                self.ser = None
                print(f"⚠️ [MODEM SLOT] {self.name} ({self.port_name}) not physically detected -> Running in MOCK MODE")

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

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "port": self.port_name,
            "baudrate": self.baudrate,
            "sim_operator": self.sim_operator,
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
                    cur.execute("SELECT id, name, port, baudrate, sim_operator, prefix_filter, is_active, sent_count, failed_count FROM gateways")
                    rows = cur.fetchall()
                
                existing_ids = set()
                for r in rows:
                    slot_id = r[0]
                    existing_ids.add(slot_id)
                    if slot_id not in self.slots:
                        slot = ModemSlot(slot_id, r[1], r[2], r[3], r[4], r[5] or "")
                        slot.is_active = bool(r[6])
                        slot.sent_count = r[7] or 0
                        slot.failed_count = r[8] or 0
                        self.slots[slot_id] = slot
                    else:
                        # Update config
                        self.slots[slot_id].name = r[1]
                        self.slots[slot_id].is_active = bool(r[6])
                        self.slots[slot_id].prefix_filter = [p.strip() for p in (r[5] or "").split(',') if p.strip()]

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
    return render_template('index.html')

# ------------------------------------------------------------------------------
# CONNECTOR 1: DIAFAAN-COMPATIBLE HTTP WEB CONNECTOR
# Exactly matches Diafaan URL format expected by Laravel sendsms and external apps
# ------------------------------------------------------------------------------
@app.route('/http/send-message/', methods=['GET', 'POST'])
def diafaan_connector_endpoint():
    data = request.get_json(silent=True) or {}
    to = request.args.get('to') or request.form.get('to') or data.get('to') or data.get('phone_number')
    msg = request.args.get('message') or request.form.get('message') or data.get('message')
    gw = request.args.get('gateway') or request.form.get('gateway') or data.get('gateway')

    if not to or not msg:
        return "Error: Missing 'to' or 'message' parameter", 400

    success, slot_name = gateway_manager.send_sms(to, msg, preferred_gateway=gw, connector_source="HTTP_Diafaan")
    
    if success:
        return f"Message sent successfully to {to} via {slot_name}", 200
    else:
        return f"Failed to deliver message to {to} ({slot_name})", 500

# ------------------------------------------------------------------------------
# CONNECTOR 2: MODERN JSON REST API
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

    if not target or not message:
        return jsonify({"success": False, "error": "Missing target phone number or message content"}), 400

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
                ok, _ = gateway_manager.send_sms(row[0], message, preferred_gateway=preferred_gw, connector_source="Broadcast")
                if ok:
                    success_count += 1

            return jsonify({"success": True, "info": f"Sent to {success_count}/{len(rows)} recipients"})
        else:
            return jsonify({"success": False, "error": "Database error"}), 500
    else:
        success, slot_name = gateway_manager.send_sms(target, message, preferred_gateway=preferred_gw, connector_source="REST")
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
        slot_list = [s.to_dict() for s in gateway_manager.slots.values()]
        return jsonify(slot_list)

    # ADD A NEW MODEM SLOT
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

@app.route('/api/v1/gateways/<int:gw_id>', methods=['DELETE'])
def delete_gateway_api(gw_id):
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gateways WHERE id = %s", (gw_id,))
        conn.close()
        gateway_manager.load_slots_from_db()
        return jsonify({"success": True, "message": "Gateway slot removed"})
    return jsonify({"success": False, "error": "Database error"}), 500

@app.route('/api/v1/gateways/<int:gw_id>/toggle', methods=['POST'])
def toggle_gateway_api(gw_id):
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE gateways SET is_active = NOT is_active WHERE id = %s", (gw_id,))
        conn.close()
        gateway_manager.load_slots_from_db()
        return jsonify({"success": True, "message": "Gateway status toggled"})
    return jsonify({"success": False, "error": "Database error"}), 500

@app.route('/api/v1/system/ports', methods=['GET'])
def scan_system_ports_api():
    """Scans physical hardware on the machine (e.g. COM19, COM20, COM21, COM22)"""
    try:
        ports = [p.device for p in serial.tools.list_ports.comports()]
        return jsonify({"ports": ports})
    except Exception as e:
        return jsonify({"ports": [], "error": str(e)})

@app.route('/api/v1/system/auto-detect', methods=['POST'])
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

# Legacy Messenger APIs
@app.route('/api/init_data', methods=['GET'])
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
    name = data.get('name')
    phone = data.get('phone_number') or data.get('phone')
    dept = data.get('department')
    
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

    app.run(host='0.0.0.0', port=port_to_use, debug=True)

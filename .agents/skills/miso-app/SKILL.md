---
name: miso-app
description: >-
  Operational runbook for the Miso SMS Gateway application. Use this skill when
  the user asks to start, stop, or debug the Flask gateway server, seed or verify
  the MySQL database, configure local domain or network settings, or run API and messaging tests.
---

# Miso SMS Gateway Operations Skill

This skill provides step-by-step operational workflows for running, managing, testing, and configuring the **Miso SMS Gateway** (OpenSMS-Gateway) in this repository.

---

## 1. System Overview & Architecture

- **Main Application**: [`MISOPROJ/MISOPROJ.py`](file:///c:/Projects/MISO-SystemProjectRepo/MisoProject/MISOPROJ/MISOPROJ.py) — Flask server handling GSM modem hardware serial connections, web dashboard, and REST API.
- **Database**: MySQL / MariaDB on `127.0.0.1:3306` with database name `sms_system`.
- **Default Port**: `8080` (auto-falls back to `5001`, `5050`, `9710` if occupied, or overridden via the `PORT` environment variable).
- **Domain Name**: `http://MIS.Messaging.ph` (when local domain and hosts mapping are configured).

---

## 2. Prerequisites & Environment Setup

Before starting the server or running tests, ensure the environment is ready:

1. **Python Dependencies**:
   Install all required packages from [`requirements.txt`](file:///c:/Projects/MISO-SystemProjectRepo/MisoProject/requirements.txt):
   ```powershell
   pip install -r requirements.txt
   ```
   *(Required packages: `Flask`, `flask-cors`, `PyMySQL`, `pyserial`, `requests`)*

2. **MySQL Service**:
   Ensure MySQL (e.g., via XAMPP) is active and running on `127.0.0.1:3306`.

3. **Windows UTF-8 Encoding**:
   Windows console must use UTF-8:
   ```powershell
   $env:PYTHONUTF8=1
   chcp 65001
   ```

---

## 3. Operational Workflows

### Workflow A: Database Seeding & Schema Verification

Use this workflow to initialize database tables (`contacts`, `sms_logs`) and populate sample data.

1. **Test DB Connection**:
   ```powershell
   python MISOPROJ\test_db.py
   ```
2. **Run Seeder**:
   Execute [`seeder.py`](file:///c:/Projects/MISO-SystemProjectRepo/MisoProject/seeder.py) to create the `sms_system` database and tables if missing, and populate test contacts:
   ```powershell
   python seeder.py
   ```
3. **Verify**:
   The output should show:
   - `Creating database 'sms_system' if not exists...`
   - `Successfully seeded contacts.`
   - Output of contacts table rows.

---

### Workflow B: Starting the SMS Gateway Application

1. **Option 1: Using the Batch Launcher**:
   Run the pre-configured [`run_app.bat`](file:///c:/Projects/MISO-SystemProjectRepo/MisoProject/run_app.bat):
   ```cmd
   .\run_app.bat
   ```

2. **Option 2: Running via Python directly**:
   ```powershell
   $env:PYTHONUTF8=1
   python MISOPROJ\MISOPROJ.py
   ```
   To specify a custom port:
   ```powershell
   $env:PORT="8080"
   python MISOPROJ\MISOPROJ.py
   ```

3. **Verify Application Startup**:
   Look for the console banner:
   ```text
   ============================================================
    🚀 OPENSMS-GATEWAY - MULTI-SLOT GSM MODEM MIDDLEMAN
   ============================================================
    ✅ STATUS: Gateway running on Port 8080
    📱 LOCAL DASHBOARD:  http://localhost:8080
    📡 DIAFAAN CONNECTOR: http://localhost:8080/http/send-message/
    🔌 REST API:         http://localhost:8080/api/v1/sms/send
   ============================================================
   ```

---

### Workflow C: Running Verification & API Tests

With the server running, open another shell to execute the automated test suites:

1. **API Endpoints Test**:
   Runs [`test_api_execution.py`](file:///c:/Projects/MISO-SystemProjectRepo/MisoProject/test_api_execution.py), which validates home route, contact creation, contact retrieval, SMS mock send, and logs:
   ```powershell
   python test_api_execution.py
   ```
2. **Messaging Flow Test**:
   Runs [`test_messaging.py`](file:///c:/Projects/MISO-SystemProjectRepo/MisoProject/test_messaging.py):
   ```powershell
   python test_messaging.py
   ```

---

### Workflow D: Domain & Network Configuration

1. **Local Domain Setup** (Run PowerShell as Administrator):
   Executes [`MISOPROJ\setup_local_domain.ps1`](file:///c:/Projects/MISO-SystemProjectRepo/MisoProject/MISOPROJ/setup_local_domain.ps1) to bind `MIS.Messaging.ph` in `C:\Windows\System32\drivers\etc\hosts`:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\MISOPROJ\setup_local_domain.ps1
   ```
2. **Apache Reverse Proxy (Optional / Port 80 Integration)**:
   If using XAMPP Apache as reverse proxy on port 80:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\update_apache_config_v2.ps1
   ```
3. See [`MISOPROJ/NETWORK_GUIDE.md`](file:///c:/Projects/MISO-SystemProjectRepo/MisoProject/MISOPROJ/NETWORK_GUIDE.md) and [`MISOPROJ/README_API.md`](file:///c:/Projects/MISO-SystemProjectRepo/MisoProject/MISOPROJ/README_API.md) for LAN client IP configuration.

---

## 4. Key Endpoints Quick Reference

| Method | Endpoint | Description | Payload / Params |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/send` | Send individual/group SMS | `{"target": "09123456789", "message": "...", "type": "individual"}` |
| `POST` | `/api/v1/sms/send` | Standard REST API Send | `{"phone_number": "...", "message": "..."}` |
| `POST` | `/send-sms` | Dashboard SMS Dispatch | `{"phone_number": "...", "message": "..."}` |
| `GET` | `/api/contacts` | Retrieve all contacts | None |
| `POST` | `/api/contacts` | Add new contact | `{"name": "...", "phone_number": "...", "department": "..."}` |
| `GET` | `/api/logs` | Fetch SMS transmission logs | None |
| `GET` | `/api/messages` | Fetch messages by target | `?target=09123456789&type=individual` |

| `GET` | `/api/v1/gateways/<id>` | Get slot configuration & details | None |
| `POST` | `/api/v1/gateways/<id>` | Save slot configuration (Name, Port, Baud, Carrier, SIM, Prefixes, Active) | `{"name": "...", "port": "COM22", ...}` |
| `GET` | `/api/v1/gateways/<id>/logs` | Fetch Sent and Received logs for slot | `?check_inbox=1` (optional) |
| `POST` | `/api/v1/gateways/<id>/check-inbox` | Read incoming SMS from SIM memory | None |
| `POST` | `/api/v1/gateways/<id>/at-command` | Execute direct AT command on modem | `{"command": "AT+CSQ"}` |
| `GET` | `/api/v1/gateways/<id>/sim` | Query SIM info (CNUM/ICCID/IMSI) | None |
| `POST` | `/api/v1/gateways/<id>/sim` | Update slot SIM phone number | `{"sim_number": "09171234567"}` |
| `POST` | `/api/v1/gateways/sim-scan` | Scan SIMs on all active slots | None |

---

## 5. Troubleshooting & Diagnostics

- **`Database Connection Error: (2003, "Can't connect to MySQL server")`**:
  - Verify that MySQL is running in XAMPP / Windows Services.
  - Verify port `3306` is open.
- **Port Conflict (`Port 8080 occupied`)**:
  - The application automatically switches to fallback ports (`5001`, `5050`, `9710`).
  - To kill a dangling process holding port `8080`:
    ```powershell
    Get-Process -Id (Get-NetTCPConnection -LocalPort 8080).OwningProcess | Stop-Process -Force
    ```
- **GSM Modem / Serial Port Lock (`PermissionError: Access is denied`)**:
  - If **Diafaan SMS Server** is installed and running as a Windows Service (`DiafaanMessageServer`), it locks all physical GSM COM ports (`COM19`-`COM22`).
  - To release the COM ports for direct Python access:
    ```powershell
    Stop-Service DiafaanMessageServer
    ```
- **Blank SIM Phone Number on Detection**:
  - Most Philippine prepaid SIMs (Globe, Smart, DITO) leave the internal MSISDN memory blank. Use the **✏️ Edit** button on the dashboard card to assign the phone number to the COM port.

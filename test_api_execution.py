import sys
import requests
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import os

def get_base_url():
    if "PORT" in os.environ:
        return f"http://127.0.0.1:{os.environ['PORT']}"
    for p in [8080, 5000, 5001]:
        try:
            r = requests.get(f"http://127.0.0.1:{p}/api/init_data", timeout=0.5)
            if r.status_code == 200:
                return f"http://127.0.0.1:{p}"
        except Exception:
            pass
    return "http://127.0.0.1:8080"

BASE_URL = get_base_url()



def test_endpoints():
    print("🚀 Starting API Tests against", BASE_URL)
    
    # 1. Test Home
    try:
        r = requests.get(BASE_URL + "/")
        print(f"Checking Home Page... Status: {r.status_code}")
    except requests.exceptions.ConnectionError:
        print("❌ Could not connect to server. Is it running?")
        sys.exit(1)

    # 2. Add a Contact
    print("\n[TEST] Adding a new contact...")
    new_contact = {"name": "Test User", "phone_number": "09123456789", "department": "TEST_DEPT"}
    r = requests.post(f"{BASE_URL}/api/contacts", json=new_contact)
    print(f"Status: {r.status_code}, Response: {r.json()}")

    # 3. Get Contacts
    print("\n[TEST] Fetching all contacts...")
    r = requests.get(f"{BASE_URL}/api/contacts")
    print(f"Status: {r.status_code}")
    contacts = r.json() if r.status_code == 200 else []
    print(f"Found {len(contacts)} contacts.")

    # 4. Send SMS (Mock)
    print("\n[TEST] Sending SMS (Mock Mode)...")
    sms_data = {"phone_number": "09123456789", "message": "Hello from API Test!"}
    r = requests.post(f"{BASE_URL}/send-sms", json=sms_data)
    print(f"Status: {r.status_code}, Response: {r.json()}")

    # 5. Get Logs
    print("\n[TEST] Fetching SMS logs...")
    r = requests.get(f"{BASE_URL}/api/logs")
    print(f"Status: {r.status_code}")
    logs = r.json() if r.status_code == 200 else []
    print(f"Found {len(logs)} logs.")
    if logs:
        print(f"Latest Log: {logs[0]}")

if __name__ == "__main__":
    # Give the server a moment to start if run consecutively
    time.sleep(2)
    test_endpoints()

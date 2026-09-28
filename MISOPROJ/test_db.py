import sys
import pymysql # type: ignore

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

print("🚀 Starting test_db.py...")


try:
    print("🔍 Attempting to connect to the database...")
    conn = pymysql.connect(host="127.0.0.1", user="root", password="", database="sms_system", port=3306)

    print("✅ Connected to MySQL!")

    cursor = conn.cursor()
    print("📌 Executing query...")
    cursor.execute("SELECT phone_number FROM contacts WHERE department = %s", ("IT",))

    results = cursor.fetchall()
    
    if results:
        print("📞 Phone Numbers:", results)
    else:
        print("⚠️ No phone numbers found.")

    conn.close()
    print("🔌 Connection closed successfully!")

except Exception as e:
    print(f"❌ Error: {e}")

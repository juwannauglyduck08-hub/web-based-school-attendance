import os
import cv2
import requests
import webbrowser
import threading
import csv
import io
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_file,
    session,
    make_response,
)
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
import cloudinary
import cloudinary.uploader
from supabase import create_client, Client

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY", "attendance-system-secure-key"
)

# Cloudinary Config (Para sa Registration)
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
)

# Supabase Config (Para sa Verification Scanned Faces Storage)
SUPABASE_URL = "https://supabase.co"
SUPABASE_KEY = "sb_publishable_4uUNWyLcGU5xe9PSlHBDWQ_aZtCRyIZ"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT UNIQUE, password TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS students (student_id TEXT PRIMARY KEY, name TEXT, grade TEXT, section TEXT, parent TEXT, phone TEXT, face_url TEXT)"
    )
    # Sinisigurado nating may storage link table parameters para sa logs ni ma'am
    cur.execute(
        "CREATE TABLE IF NOT EXISTS attendance (id SERIAL PRIMARY KEY, student_id TEXT, name TEXT, grade TEXT, section TEXT, kind TEXT, timestamp TEXT, scanned_face_url TEXT, dimensions TEXT)"
    )
    conn.commit()
    cur.close()
    conn.close()

init_db()

def get_greeting():
    h = datetime.now().hour
    return "Good morning" if h < 12 else ("Good afternoon" if h < 18 else "Good evening")

TEXTBEE_API_KEY = "txb_t18Sw5sCFGC6J8XkiNmpJUwIIgNflo2t"
TEXTBEE_DEVICE_ID = "6a9c04daccb6c72709bab159"

def send_sms(phone, parent, name, grade, section, kind, ts):
    if not phone:
        return False
    phone = phone.strip().replace("-", "").replace(" ", "")
    if phone.startswith("0"):
        phone = "+63" + phone[1:]
    msg = f"{get_greeting()} {parent or ''}, your child {name} ({grade} - {section}) has recorded {kind} at Payatas B. Elementary School on {ts}."
    try:
        r = requests.post(
            f"https://textbee.dev{TEXTBEE_DEVICE_ID}/send-sms",
            json={"recipients": [phone], "message": msg},
            headers={"x-api-key": TEXTBEE_API_KEY},
            timeout=10,
        )
        return r.status_code == 200
    except:
        return False

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/auth_status")
def auth_status():
    return jsonify({"logged_in": "user" in session})

@app.route("/api/login", methods=["POST"])
def login():
    d = request.json or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT password FROM users WHERE username=%s", (d.get("username"),))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and check_password_hash(row[0], d.get("password")):
        session["user"] = d.get("username")
        return jsonify({"ok": True, "message": "Success"})
    return jsonify({"ok": False, "message": "Invalid credentials."})

@app.route("/api/logout")
def logout():
    session.pop("user", None)
    return jsonify({"ok": True})

@app.route("/api/register_user", methods=["POST"])
def register_user():
    d = request.json or {}
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (username, password) VALUES (%s, %s)",
            (d.get("username"), generate_password_hash(d.get("password"))),
        )
        conn.commit()
        return jsonify({"ok": True, "message": "Account created. Please login."})
    except:
        return jsonify({"ok": False, "message": "Username already exists."})
    finally:
        cur.close()
        conn.close()

@app.route("/api/register", methods=["POST"])
def register_student():
    f = request.form
    if not all([
        f.get("student_id"),
        f.get("name"),
        f.get("grade"),
        f.get("section"),
        f.get("parent"),
        f.get("phone"),
        request.files.get("face_image"),
    ]):
        return jsonify({"ok": False, "message": "All fields are required."})
    sid = f.get("student_id")
    try:
        upload_result = cloudinary.uploader.upload(
            request.files.get("face_image"),
            public_id=f"student_{sid}",
            folder="student_faces",
            overwrite=True,
            transformation=[{"width": 120, "height": 120, "crop": "fill"}],
        )
        face_url = upload_result.get("secure_url")
    except Exception as e:
        return jsonify({"ok": False, "message": f"Upload error: {str(e)}"})

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            
            "INSERT INTO students (student_id, name, grade, section, parent, phone, face_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (student_id) DO UPDATE SET
            name=EXCLUDED.name, grade=EXCLUDED.grade, section=EXCLUDED.section,
            parent=EXCLUDED.parent, phone=EXCLUDED.phone, face_url=EXCLUDED.face_url",
            
                (sid,
                f.get("name"),
                f.get("grade"),
                f.get("section"),
                f.get("parent"),
                f.get("phone"),
                face_url,
            )
        
        conn.commit()
        return jsonify({"ok": True, "message": f"Student {f.get('name')} registered!"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"DB error: {str(e)}"})
    finally:
        cur.close()
        conn.close()

@app.route("/api/verify_face", methods=["POST"])
def verify_face():
    img = request.files.get("face_scan")
    if not img:
        return jsonify({"ok": False, "message": "No image provided."})
    temp_path = "temp.jpg"
    img.save(temp_path)
    target_img = cv2.imread(temp_path, cv2.IMREAD_GRAYSCALE)
    
    if target_img is None:
        if os.path.exists(temp_path): os.remove(temp_path)
        return jsonify({"ok": False, "message": "Image read error."})

    face_cas = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    tf = face_cas.detectMultiScale(target_img, 1.1, 5, minSize=(40, 40))
    if len(tf) == 0:
        if os.path.exists(temp_path): os.remove(temp_path)
        return jsonify({"ok": False, "message": "No face detected."})

    (x, y, w, h) = tf[0]
    
    
    cropped_face = target_img[y : y + h, x : x + w]
    dimensions_str = f"{w}x{h} px"
    t_roi = cv2.resize(cropped_face, (120, 120))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT student_id, name, grade, section, face_url FROM students")
    students_rows = cur.fetchall()
    cur.close()
    conn.close()

    best_sid, max_score, row_data = None, -1.0, None
    for s_row in students_rows:
        sid, name, grade, section, face_url = s_row
        if not face_url:
            continue
        try:
            resp = requests.get(face_url, timeout=5)
            if resp.status_code != 200:
                continue
            k_img = cv2.imdecode(np.asarray(bytearray(resp.content), dtype=np.uint8),
                cv2.IMREAD_GRAYSCALE)
            if k_img is None:
                continue
            if k_img.shape != (120, 120):
                k_img = cv2.resize(k_img, (120, 120))
            res = cv2.matchTemplate(t_roi, k_img, cv2.TM_CCOEFF_NORMED)
            _, val, _, _ = cv2.minMaxLoc(res)
            if val > max_score:
                max_score, best_sid, row_data = val, sid, (sid, name, grade, section)
        except:
            continue

    if max_score < 0.38 or not best_sid:
        if os.path.exists(temp_path): os.remove(temp_path)
        return jsonify({"ok": False, "message": "Face not recognized."}
        
    scanned_face_url = ""
    try:
        crop_path = "crop_temp.jpg"
        cv2.imwrite(crop_path, cv2.resize(cropped_face, (150, 150)))
        filename = f"scan_{best_sid}_{int(datetime.now().timestamp())}.jpg"
        with open(crop_path, 'rb') as f:
            supabase.storage.from_("scanned_faces").upload(path=filename, file=f, file_options={"content-type": "image/jpeg"})
        scanned_face_url = supabase.storage.from_("scanned_faces").get_public_url(filename)
        if os.path.exists(crop_path): os.remove(crop_path)
    except Exception as e:
        print(f"Supabase Upload error: {str(e)}")

    if os.path.exists(temp_path): os.remove(temp_path)
    return jsonify({
        "ok": True,
        "message": f"Verified: {row_data[1]}",
        "student_id": row_data[0],
        "name": row_data[1],
        "grade": row_data[2],
        "section": row_data[3],
        "scanned_face_url": scanned_face_url,
        "dimensions": dimensions_str
    })

@app.route("/api/qr/<sid>")
def get_qr(sid):
    import qrcode
    buf = io.BytesIO()
    qrcode.make(sid).save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

@app.route("/api/verify", methods=["POST"])
def verify():
    sid = request.form.get("student_id")
    kind = request.form.get("kind", "Time In")
    scanned_face_url = request.form.get("scanned_face_url", "")
    dimensions = request.form.get("dimensions", "")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, grade, section, parent, phone FROM students WHERE student_id=%s", (sid,))
    student = cur.fetchone()
    if not student:
         cur.close()
         conn.close()
         return jsonify({"ok": False, "message": "Student not found."})

    name, grade, section, parent, phone = student
    ts = datetime.now(ZoneInfo("Asia/Manila")).strftime("%Y-%m-%d %I:%M %p")
    cur.execute(
         "INSERT INTO attendance (student_id, name, grade, section, kind, timestamp, scanned_face_url, dimensions) VALUES (%S, %S, %S, %S, %S, %S, %S, %S)",
         (sid, name, grade, section, kind, ts, scanned_face_url, dimensions),
         )
    conn. commit()
    cur.close()
    conn.close()
    
   


   

#!/usr/bin/env python3
"""Attendance — face + live-location attendance system (Vercel version).

Hosted on Vercel with a Postgres database. Students check in from their
phones: they scan the rotating QR code on the board, take a selfie, and
share their location. The server verifies three things — the current
code, the face match, and the distance from the classroom — and logs
every attempt.

Design decisions (from the project spec, all settled by the user):
- Geofence radius 30-50 m (default 40 m); GPS drifts 10-20 m indoors, so
  a tighter radius would falsely reject real students.
- Rotating QR code, new code every 60 seconds; the previous code stops
  working the moment the next one appears. Windows are counted from the
  session start, so every code gets a full 60 seconds.
- Static QR + 10-minute window is the fallback for rooms with no display.
- Wi-Fi is a bonus check only — not every classroom has Wi-Fi.
- No model training: face recognition uses a pre-trained model running
  in the browser (face-api.js, bundled locally). Only 128-number face
  descriptors are sent to and stored on the server — never photos.

Storage is Postgres. The connection string comes from the POSTGRES_URL
environment variable (set automatically by Vercel Postgres), falling
back to DATABASE_URL. For local development, point POSTGRES_URL at any
Postgres database and run:

    pip install -r requirements.txt
    POSTGRES_URL=postgresql://... python app.py
"""

import csv
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import time
from datetime import datetime

import psycopg
import qrcode
from flask import Flask, g, jsonify, render_template, request, Response
from openpyxl import Workbook
from psycopg.rows import dict_row

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
FACE_THRESHOLD = 0.6            # max Euclidean distance for a face match
ROTATION_SECONDS = 60           # rotating QR code lifetime
STATIC_WINDOW_SECONDS = 600     # static fallback: 10 minutes
DEFAULT_RADIUS_M = 40           # geofence default (agreed range 30-50 m)

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "public", "static"),
    static_url_path="/static",
)
app.config["JSON_SORT_KEYS"] = False


# ---------------------------------------------------------------- helpers

def base_url():
    """Public URL of this deployment (the QR code points here)."""
    return request.host_url.rstrip("/")


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two lat/lon points."""
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def face_distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def valid_descriptor(value):
    """A face descriptor must be exactly 128 finite numbers."""
    if not isinstance(value, list) or len(value) != 128:
        return False
    return all(isinstance(x, (int, float)) and math.isfinite(x)
               for x in value)


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        if not DATABASE_URL:
            raise RuntimeError(
                "POSTGRES_URL (or DATABASE_URL) environment variable is not set")
        # prepare_threshold=None: Vercel Postgres pooled URLs go through a
        # transaction pooler, which cannot keep server-side prepared
        # statements between requests.
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row,
                               prepare_threshold=None)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if missing. Idempotent — safe on every cold start."""
    if not DATABASE_URL:
        raise RuntimeError(
            "POSTGRES_URL (or DATABASE_URL) environment variable is not set")
    conn = psycopg.connect(DATABASE_URL, prepare_threshold=None)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS classroom (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT NOT NULL DEFAULT 'Classroom',
                    latitude DOUBLE PRECISION NOT NULL,
                    longitude DOUBLE PRECISION NOT NULL,
                    radius_m INTEGER NOT NULL DEFAULT 40,
                    wifi_ssid TEXT NOT NULL DEFAULT ''
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL,
                    roll_number TEXT NOT NULL UNIQUE,
                    descriptor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    mode TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    started_at DOUBLE PRECISION NOT NULL,
                    static_code TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    ended_at DOUBLE PRECISION
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    session_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    checked_in_at TEXT NOT NULL,
                    face_distance DOUBLE PRECISION,
                    match_pct DOUBLE PRECISION,
                    distance_m DOUBLE PRECISION,
                    status TEXT NOT NULL,
                    reason TEXT,
                    code_used TEXT
                )
            """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- sessions

def get_active_session():
    row = get_db().execute(
        "SELECT * FROM sessions WHERE active = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def window_index(session, now):
    """60-second windows counted from the session start, so every code
    gets a full 60 seconds (never a wall-clock-aligned partial window)."""
    return int((now - session["started_at"]) // ROTATION_SECONDS)


def code_for_window(session, window):
    digest = hmac.new(
        session["secret"].encode(),
        f"{session['id']}:{window}".encode(),
        hashlib.sha256,
    ).digest()
    return str(int.from_bytes(digest[:4], "big") % 1000000).zfill(6)


def current_code(session, now):
    if session["mode"] == "static":
        return session["static_code"]
    return code_for_window(session, window_index(session, now))


def seconds_remaining(session, now):
    if session["mode"] == "static":
        return max(0, int(session["started_at"] + STATIC_WINDOW_SECONDS - now))
    return ROTATION_SECONDS - int((now - session["started_at"]) % ROTATION_SECONDS)


def check_code(session, code, now):
    """Returns (ok, reason). Only the current code is ever accepted."""
    if session["mode"] == "static":
        if now > session["started_at"] + STATIC_WINDOW_SECONDS:
            return False, "code expired — the 10-minute window has closed"
        if code != session["static_code"]:
            return False, "wrong code"
        return True, ""
    if code != code_for_window(session, window_index(session, now)):
        return False, "code expired — scan the code on the board again"
    return True, ""


def session_state(session):
    """Public state for the board and check-in pages."""
    if session is None:
        return {"active": False}
    now = time.time()
    code = current_code(session, now)
    db = get_db()
    present = db.execute(
        "SELECT COUNT(DISTINCT student_id) AS n FROM attendance "
        "WHERE session_id = %s AND status = 'present'",
        (session["id"],),
    ).fetchone()["n"]
    total = db.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
    return {
        "active": True,
        "session_id": session["id"],
        "mode": session["mode"],
        "code": code,
        "seconds_remaining": seconds_remaining(session, now),
        "started_at": session["started_at"],
        "expires_at": (session["started_at"] + STATIC_WINDOW_SECONDS
                       if session["mode"] == "static" else None),
        "checkin_url": f"{base_url()}/checkin?code={code}",
        "base_url": base_url(),
        "present_count": present,
        "total_students": total,
    }


# ---------------------------------------------------------------- pages

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/classroom")
def classroom_page():
    return render_template("classroom.html")


@app.route("/students")
def students_page():
    return render_template("students.html")


@app.route("/session")
def session_page():
    return render_template("session.html")


@app.route("/checkin")
def checkin_page():
    return render_template("checkin.html", code=request.args.get("code", ""))


# ---------------------------------------------------------------- classroom

@app.route("/api/classroom", methods=["GET"])
def api_get_classroom():
    row = get_db().execute("SELECT * FROM classroom WHERE id = 1").fetchone()
    if row is None:
        return jsonify({"configured": False})
    data = dict(row)
    data["configured"] = True
    return jsonify(data)


@app.route("/api/classroom", methods=["POST"])
def api_save_classroom():
    data = request.get_json(force=True)
    try:
        lat = float(data["latitude"])
        lon = float(data["longitude"])
        radius = int(data.get("radius_m", DEFAULT_RADIUS_M))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "latitude, longitude and radius are required"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "latitude/longitude out of range"}), 400
    if not (5 <= radius <= 500):
        return jsonify({"error": "radius must be between 5 and 500 metres"}), 400
    name = str(data.get("name") or "Classroom").strip() or "Classroom"
    wifi = str(data.get("wifi_ssid") or "").strip()
    db = get_db()
    db.execute(
        "INSERT INTO classroom (id, name, latitude, longitude, radius_m, wifi_ssid)"
        " VALUES (1, %s, %s, %s, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name,"
        " latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,"
        " radius_m = EXCLUDED.radius_m, wifi_ssid = EXCLUDED.wifi_ssid",
        (name, lat, lon, radius, wifi),
    )
    db.commit()
    return jsonify({"ok": True, "name": name, "latitude": lat,
                    "longitude": lon, "radius_m": radius, "wifi_ssid": wifi})


# ---------------------------------------------------------------- students

@app.route("/api/students", methods=["GET"])
def api_list_students():
    rows = get_db().execute(
        "SELECT id, name, roll_number, created_at FROM students ORDER BY name"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/students", methods=["POST"])
def api_register_student():
    data = request.get_json(force=True)
    name = str(data.get("name") or "").strip()
    roll = str(data.get("roll_number") or "").strip()
    descriptor = data.get("descriptor")
    if not name or not roll:
        return jsonify({"error": "name and roll number are required"}), 400
    if not valid_descriptor(descriptor):
        return jsonify({"error": "a valid face scan is required "
                                 "(128-number descriptor)"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO students (name, roll_number, descriptor, created_at)"
            " VALUES (%s, %s, %s, %s)",
            (name, roll, json.dumps([float(x) for x in descriptor]), now_iso()),
        )
        db.commit()
    except psycopg.errors.UniqueViolation:
        db.rollback()
        return jsonify({"error": f"roll number {roll} is already registered"}), 409
    return jsonify({"ok": True, "name": name, "roll_number": roll})


@app.route("/api/students/<int:student_id>", methods=["DELETE"])
def api_delete_student(student_id):
    db = get_db()
    db.execute("DELETE FROM students WHERE id = %s", (student_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- sessions API

@app.route("/api/session/current", methods=["GET"])
def api_session_current():
    return jsonify(session_state(get_active_session()))


@app.route("/api/session/start", methods=["POST"])
def api_session_start():
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "rotating")
    if mode not in ("rotating", "static"):
        return jsonify({"error": "mode must be 'rotating' or 'static'"}), 400
    if get_db().execute("SELECT * FROM classroom WHERE id = 1").fetchone() is None:
        return jsonify({"error": "set the classroom location first"}), 400
    db = get_db()
    db.execute("UPDATE sessions SET active = 0, ended_at = %s WHERE active = 1",
               (time.time(),))
    secret = secrets.token_hex(16)
    static_code = None
    if mode == "static":
        static_code = str(secrets.randbelow(1000000)).zfill(6)
    cur = db.execute(
        "INSERT INTO sessions (mode, secret, started_at, static_code)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (mode, secret, time.time(), static_code),
    )
    session_id = cur.fetchone()["id"]
    db.commit()
    session = dict(db.execute(
        "SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone())
    return jsonify(session_state(session))


@app.route("/api/session/end", methods=["POST"])
def api_session_end():
    db = get_db()
    db.execute("UPDATE sessions SET active = 0, ended_at = %s WHERE active = 1",
               (time.time(),))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/session/qr.png", methods=["GET"])
def api_session_qr():
    session = get_active_session()
    if session is None:
        return jsonify({"error": "no active session"}), 404
    url = session_state(session)["checkin_url"]
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=12, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1C1B17", back_color="#FFFFFF")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = Response(buf.read(), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- check-in

@app.route("/api/checkin", methods=["POST"])
def api_checkin():
    data = request.get_json(force=True)
    roll = str(data.get("roll_number") or "").strip()
    code = str(data.get("code") or "").strip()
    descriptor = data.get("descriptor")
    db = get_db()
    now = time.time()

    session = get_active_session()
    if session is None:
        return jsonify({"ok": False, "status": "rejected",
                        "reason": "no active session — ask the teacher to start one"}), 200

    student = None
    if roll:
        row = db.execute("SELECT * FROM students WHERE roll_number = %s",
                         (roll,)).fetchone()
        student = dict(row) if row else None

    # Verify in order: code -> student -> face -> location. Every attempt
    # is logged with its scores, whatever the outcome.
    face_dist = None
    match_pct = None
    distance_m = None
    status = "rejected"
    reason = ""

    code_ok, code_reason = check_code(session, code, now)
    if not code_ok:
        reason = code_reason
    elif student is None:
        reason = "roll number not registered"
    elif not valid_descriptor(descriptor):
        reason = "face scan failed — try again with your face well lit"
    else:
        stored = json.loads(student["descriptor"])
        face_dist = face_distance(stored, [float(x) for x in descriptor])
        match_pct = max(0, round((1 - face_dist) * 100))
        room = db.execute("SELECT * FROM classroom WHERE id = 1").fetchone()
        try:
            lat = float(data["latitude"])
            lon = float(data["longitude"])
            distance_m = round(haversine_m(room["latitude"], room["longitude"],
                                           lat, lon), 1)
        except (KeyError, TypeError, ValueError):
            distance_m = None
        if face_dist > FACE_THRESHOLD:
            reason = "face did not match the registered photo"
        elif distance_m is None:
            reason = "location missing — allow location access and retry"
        elif distance_m > room["radius_m"]:
            reason = (f"outside the classroom ({distance_m:.0f} m away, "
                      f"limit {room['radius_m']} m)")
        else:
            already = db.execute(
                "SELECT 1 FROM attendance WHERE session_id = %s AND student_id = %s"
                " AND status = 'present' LIMIT 1",
                (session["id"], student["id"]),
            ).fetchone()
            if already:
                status = "duplicate"
                reason = "attendance already marked for this session"
            else:
                status = "present"

    if student is not None:
        db.execute(
            "INSERT INTO attendance (session_id, student_id, checked_in_at,"
            " face_distance, match_pct, distance_m, status, reason, code_used)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (session["id"], student["id"], now_iso(), face_dist, match_pct,
             distance_m, status, reason or None, code),
        )
        db.commit()

    return jsonify({
        "ok": status == "present",
        "status": status,
        "reason": reason,
        "student": student["name"] if student else None,
        "face_distance": round(face_dist, 3) if face_dist is not None else None,
        "match_pct": match_pct,
        "distance_m": distance_m,
    })


# ---------------------------------------------------------------- register + export

def attendance_rows(session_id=None):
    query = ("SELECT a.id, a.checked_in_at, a.face_distance, a.match_pct,"
             " a.distance_m, a.status, a.reason, a.code_used,"
             " s.name, s.roll_number, a.session_id"
             " FROM attendance a JOIN students s ON s.id = a.student_id")
    params = []
    if session_id is not None:
        query += " WHERE a.session_id = %s"
        params.append(session_id)
    query += " ORDER BY a.checked_in_at DESC, a.id DESC"
    return [dict(r) for r in get_db().execute(query, params or None).fetchall()]


@app.route("/api/attendance", methods=["GET"])
def api_attendance():
    session_id = request.args.get("session_id")
    if session_id == "active":
        session = get_active_session()
        session_id = session["id"] if session else None
        if session_id is None:
            return jsonify([])
    elif session_id:
        session_id = int(session_id)
    else:
        session_id = None
    return jsonify(attendance_rows(session_id))


@app.route("/api/status", methods=["GET"])
def api_status():
    db = get_db()
    room = db.execute("SELECT * FROM classroom WHERE id = 1").fetchone()
    students = db.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
    today = datetime.now().strftime("%Y-%m-%d")
    present_today = db.execute(
        "SELECT COUNT(DISTINCT student_id) AS n FROM attendance"
        " WHERE status = 'present' AND substr(checked_in_at, 1, 10) = %s",
        (today,),
    ).fetchone()["n"]
    return jsonify({
        "classroom": dict(room) if room else None,
        "students": students,
        "present_today": present_today,
        "session": session_state(get_active_session()),
        "base_url": base_url(),
        "https": request.scheme == "https",
    })


@app.route("/export/csv")
def export_csv():
    session_id = request.args.get("session_id", type=int)
    rows = attendance_rows(session_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Roll number", "Date", "Time", "Face match (%)",
                     "Face distance", "Distance from classroom (m)", "Status",
                     "Reason", "Code used"])
    for r in rows:
        date, _, time_part = r["checked_in_at"].partition(" ")
        writer.writerow([r["name"], r["roll_number"], date, time_part,
                         r["match_pct"] if r["match_pct"] is not None else "",
                         r["face_distance"] if r["face_distance"] is not None else "",
                         r["distance_m"] if r["distance_m"] is not None else "",
                         r["status"], r["reason"] or "", r["code_used"] or ""])
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = \
        "attachment; filename=attendance.csv"
    return resp


@app.route("/export/xlsx")
def export_xlsx():
    session_id = request.args.get("session_id", type=int)
    rows = attendance_rows(session_id)
    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    ws.append(["Name", "Roll number", "Date", "Time", "Face match (%)",
               "Face distance", "Distance from classroom (m)", "Status",
               "Reason", "Code used"])
    for r in rows:
        date, _, time_part = r["checked_in_at"].partition(" ")
        ws.append([r["name"], r["roll_number"], date, time_part,
                   r["match_pct"], r["face_distance"], r["distance_m"],
                   r["status"], r["reason"] or "", r["code_used"] or ""])
    for col, width in zip("ABCDEFGHIJ", [22, 14, 12, 10, 15, 14, 26, 11, 40, 10]):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=attendance.xlsx"},
    )


# ---------------------------------------------------------------- run

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    print()
    print("  Attendance server running")
    print(f"  Local:  http://localhost:{port}")
    print()
    app.run(host="0.0.0.0", port=port)
else:
    # Serverless cold start: create tables if missing (idempotent).
    if DATABASE_URL:
        init_db()

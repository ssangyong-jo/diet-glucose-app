from flask import Flask, request, redirect, url_for, render_template_string, flash, jsonify
import sqlite3
import json
import re
from pathlib import Path
import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or os.environ.get("RENDER_DISK_PATH") or BASE_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("DATABASE_PATH") or (DATA_DIR / "diet_glucose_v2.db"))
SETTINGS_PATH = Path(os.environ.get("SETTINGS_PATH") or (DATA_DIR / "diet_glucose_settings.json"))

APP_TITLE = "식단/혈당 모바일 웹앱 v2 - 배포용 완성판 초기화버튼"
DEFAULT_SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "혈당_식단_기록기")
DEFAULT_SERVICE_ACCOUNT_NAME = os.environ.get("SERVICE_ACCOUNT_FILE", "google_service_account.json")
_SERVICE_ACCOUNT_TEMP_PATH = DATA_DIR / "_service_account_from_env.json"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "diet-glucose-v2-dashboard-final-secret-key")

# Gunicorn/Render에서는 __main__ 블록이 실행되지 않을 수 있어서
# import 시점에 DB/설정 기본값을 먼저 준비한다.


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Seoul"))


def now_dt():
    return datetime.now(APP_TIMEZONE)


def now_str():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return now_dt().strftime("%Y-%m-%d")


def dt_local_value():
    return now_dt().strftime("%Y-%m-%dT%H:%M")


def normalize_dt(value):
    value = (value or "").strip().replace("T", " ")
    if len(value) == 16:
        value += ":00"
    return value


def ensure_column(cur, table_name, column_name, definition):
    cur.execute(f"PRAGMA table_info({table_name})")
    cols = [row[1] for row in cur.fetchall()]
    if column_name not in cols:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_time TEXT NOT NULL,
            meal_type TEXT,
            summary TEXT NOT NULL,
            calories INTEGER,
            calorie_source TEXT,
            memo TEXT,
            created_at TEXT NOT NULL,
            synced_to_sheet INTEGER NOT NULL DEFAULT 0,
            synced_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS glucose_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_time TEXT NOT NULL,
            glucose_value INTEGER NOT NULL,
            tag TEXT NOT NULL,
            memo TEXT,
            created_at TEXT NOT NULL,
            synced_to_sheet INTEGER NOT NULL DEFAULT 0,
            synced_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS inbody_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_time TEXT NOT NULL,
            weight REAL,
            skeletal_muscle REAL,
            body_fat_mass REAL,
            body_fat_percent REAL,
            memo TEXT,
            created_at TEXT NOT NULL,
            synced_to_sheet INTEGER NOT NULL DEFAULT 0,
            synced_at TEXT
        )
        """
    )

    ensure_column(cur, "meals", "synced_to_sheet", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(cur, "meals", "synced_at", "TEXT")
    ensure_column(cur, "meals", "calorie_source", "TEXT")
    ensure_column(cur, "glucose_logs", "synced_to_sheet", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(cur, "glucose_logs", "synced_at", "TEXT")
    ensure_column(cur, "inbody_logs", "synced_to_sheet", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(cur, "inbody_logs", "synced_at", "TEXT")

    conn.commit()
    conn.close()


def reset_app_data():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM meals")
    cur.execute("DELETE FROM glucose_logs")
    cur.execute("DELETE FROM inbody_logs")
    cur.execute("DELETE FROM sqlite_sequence WHERE name IN ('meals', 'glucose_logs', 'inbody_logs')")
    conn.commit()
    conn.close()


def load_settings():
    defaults = {
        "spreadsheet_name": DEFAULT_SPREADSHEET_NAME,
        "service_account_file": DEFAULT_SERVICE_ACCOUNT_NAME,
        "auto_create_spreadsheet": False,
    }
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            defaults.update(saved)
        except Exception:
            pass
    return defaults


def save_settings(data):
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_service_account_env_payload():
    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and obj.get("type") == "service_account":
            return obj
    except Exception:
        pass
    return None


def ensure_env_service_account_file():
    obj = get_service_account_env_payload()
    if not obj:
        return None
    _SERVICE_ACCOUNT_TEMP_PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return _SERVICE_ACCOUNT_TEMP_PATH


def detect_service_account_candidates():
    candidates = []
    env_path = ensure_env_service_account_file()
    if env_path and env_path.exists():
        candidates.append(env_path)
    preferred = BASE_DIR / DEFAULT_SERVICE_ACCOUNT_NAME
    if preferred.exists():
        candidates.append(preferred)
    for p in sorted(BASE_DIR.glob("*.json")):
        if p in candidates:
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and obj.get("type") == "service_account" and obj.get("client_email"):
                candidates.append(p)
        except Exception:
            continue
    return candidates


def service_account_path_from_settings():
    env_path = ensure_env_service_account_file()
    if env_path and env_path.exists():
        return env_path

    settings = load_settings()
    raw_name = (settings.get("service_account_file") or "").strip()
    if raw_name:
        p = BASE_DIR / raw_name
        if p.exists():
            return p
    candidates = detect_service_account_candidates()
    if candidates:
        return candidates[0]
    return BASE_DIR / DEFAULT_SERVICE_ACCOUNT_NAME


def read_service_account_meta(path: Path):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return {
            "ok": True,
            "client_email": obj.get("client_email", ""),
            "project_id": obj.get("project_id", ""),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "client_email": "", "project_id": ""}


def parse_calories_from_text(summary: str):
    text = (summary or "").replace(",", "")

    range_patterns = [
        r"(\d{2,5})\s*[~-]\s*(\d{2,5})\s*kcal",
        r"약\s*(\d{2,5})\s*[~-]\s*(\d{2,5})\s*kcal",
        r"총\s*약\s*(\d{2,5})\s*[~-]\s*(\d{2,5})\s*kcal",
    ]
    for pattern in range_patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            low = int(m.group(1))
            high = int(m.group(2))
            if low > high:
                low, high = high, low
            return int(round((low + high) / 2)), f"자동추출(범위평균 {low}~{high}kcal)"

    single_patterns = [
        r"(\d{2,5})\s*kcal",
        r"약\s*(\d{2,5})\s*칼로리",
        r"총\s*약\s*(\d{2,5})",
    ]
    for pattern in single_patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            val = int(m.group(1))
            return val, f"자동추출({val}kcal)"

    return None, None


def get_today_summary():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt, COALESCE(SUM(COALESCE(calories,0)),0) AS kcal FROM meals WHERE substr(meal_time,1,10)=?", (today_str(),))
    meal_row = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS cnt, COALESCE(AVG(glucose_value),0) AS avg_glucose FROM glucose_logs WHERE substr(log_time,1,10)=?", (today_str(),))
    glucose_row = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS cnt FROM inbody_logs WHERE substr(log_time,1,10)=?", (today_str(),))
    inbody_row = cur.fetchone()
    conn.close()
    return {
        "meal_count": meal_row["cnt"],
        "meal_kcal": int(meal_row["kcal"] or 0),
        "glucose_count": glucose_row["cnt"],
        "avg_glucose": int(round(glucose_row["avg_glucose"] or 0)) if glucose_row["cnt"] else 0,
        "inbody_count": inbody_row["cnt"],
    }


def has_fasting_today():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM glucose_logs WHERE tag='공복' AND substr(log_time,1,10)=?", (today_str(),))
    row = cur.fetchone()
    conn.close()
    return (row["cnt"] if row else 0) > 0


def get_recent_rows(table_name, time_col, limit=8):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table_name} ORDER BY {time_col} DESC, id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def check_google_ready():
    try:
        import gspread  # noqa: F401
        from google.oauth2.service_account import Credentials  # noqa: F401
    except Exception:
        return False, "gspread / google-auth 라이브러리 설치 필요"

    settings = load_settings()
    sa_path = service_account_path_from_settings()
    if not sa_path.exists():
        return False, f"서비스계정 파일 없음: {sa_path.name}"

    meta = read_service_account_meta(sa_path)
    if not meta["ok"]:
        return False, f"서비스계정 json 읽기 실패: {meta['error']}"

    spreadsheet_name = (settings.get("spreadsheet_name") or "").strip()
    if not spreadsheet_name:
        return False, "시트 이름이 비어 있음"

    return True, "연동 준비됨"


def get_gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(str(service_account_path_from_settings()), scopes=scopes)
    return gspread.authorize(creds)


def open_or_create_spreadsheet():
    settings = load_settings()
    spreadsheet_name = settings["spreadsheet_name"].strip()
    gc = get_gspread_client()
    try:
        return gc.open(spreadsheet_name)
    except Exception:
        if not settings.get("auto_create_spreadsheet", False):
            raise
        return gc.create(spreadsheet_name)


def get_or_create_worksheet(spreadsheet, title, headers):
    try:
        ws = spreadsheet.worksheet(title)
    except Exception:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=max(20, len(headers) + 4))

    current_headers = ws.row_values(1)
    if not current_headers:
        ws.append_row(headers)
    elif current_headers != headers:
        ws.clear()
        ws.append_row(headers)
    return ws


def get_sync_status():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM meals WHERE synced_to_sheet=0")
    meal_unsynced = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) AS cnt FROM glucose_logs WHERE synced_to_sheet=0")
    glucose_unsynced = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) AS cnt FROM inbody_logs WHERE synced_to_sheet=0")
    inbody_unsynced = cur.fetchone()["cnt"]
    conn.close()

    sa_path = service_account_path_from_settings()
    meta = read_service_account_meta(sa_path) if sa_path.exists() else {"ok": False, "client_email": "", "project_id": ""}
    config_ok, config_msg = check_google_ready()
    return {
        "config_ok": config_ok,
        "config_msg": config_msg,
        "meal_unsynced": meal_unsynced,
        "glucose_unsynced": glucose_unsynced,
        "inbody_unsynced": inbody_unsynced,
        "total_unsynced": meal_unsynced + glucose_unsynced + inbody_unsynced,
        "service_account_file": sa_path.name,
        "service_account_email": meta.get("client_email", "") if meta.get("ok") else "",
        "project_id": meta.get("project_id", "") if meta.get("ok") else "",
        "spreadsheet_name": load_settings().get("spreadsheet_name", DEFAULT_SPREADSHEET_NAME),
    }


def sync_rows_to_sheet():
    ready, msg = check_google_ready()
    if not ready:
        return {"ok": False, "message": msg, "synced": 0}

    try:
        ss = open_or_create_spreadsheet()
        meal_ws = get_or_create_worksheet(
            ss,
            "식단기록",
            ["local_id", "meal_time", "meal_type", "summary", "calories", "calorie_source", "memo", "created_at", "synced_at"],
        )
        glucose_ws = get_or_create_worksheet(
            ss,
            "혈당기록",
            ["local_id", "log_time", "tag", "glucose_value", "memo", "created_at", "synced_at"],
        )
        inbody_ws = get_or_create_worksheet(
            ss,
            "인바디기록",
            ["local_id", "log_time", "weight", "skeletal_muscle", "body_fat_mass", "body_fat_percent", "memo", "created_at", "synced_at"],
        )

        conn = db_conn()
        cur = conn.cursor()
        sync_time = now_str()
        synced_count = 0

        cur.execute("SELECT id, meal_time, meal_type, summary, calories, calorie_source, memo, created_at FROM meals WHERE synced_to_sheet=0 ORDER BY id ASC")
        meal_rows = cur.fetchall()
        if meal_rows:
            batch, ids = [], []
            for r in meal_rows:
                batch.append([
                    r["id"], r["meal_time"], r["meal_type"] or "", r["summary"],
                    "" if r["calories"] is None else r["calories"], r["calorie_source"] or "",
                    r["memo"] or "", r["created_at"], sync_time
                ])
                ids.append(r["id"])
            meal_ws.append_rows(batch, value_input_option="USER_ENTERED")
            cur.executemany("UPDATE meals SET synced_to_sheet=1, synced_at=? WHERE id=?", [(sync_time, i) for i in ids])
            synced_count += len(ids)

        cur.execute("SELECT id, log_time, tag, glucose_value, memo, created_at FROM glucose_logs WHERE synced_to_sheet=0 ORDER BY id ASC")
        glucose_rows = cur.fetchall()
        if glucose_rows:
            batch, ids = [], []
            for r in glucose_rows:
                batch.append([r["id"], r["log_time"], r["tag"], r["glucose_value"], r["memo"] or "", r["created_at"], sync_time])
                ids.append(r["id"])
            glucose_ws.append_rows(batch, value_input_option="USER_ENTERED")
            cur.executemany("UPDATE glucose_logs SET synced_to_sheet=1, synced_at=? WHERE id=?", [(sync_time, i) for i in ids])
            synced_count += len(ids)

        cur.execute("SELECT id, log_time, weight, skeletal_muscle, body_fat_mass, body_fat_percent, memo, created_at FROM inbody_logs WHERE synced_to_sheet=0 ORDER BY id ASC")
        inbody_rows = cur.fetchall()
        if inbody_rows:
            batch, ids = [], []
            for r in inbody_rows:
                batch.append([
                    r["id"], r["log_time"],
                    "" if r["weight"] is None else r["weight"],
                    "" if r["skeletal_muscle"] is None else r["skeletal_muscle"],
                    "" if r["body_fat_mass"] is None else r["body_fat_mass"],
                    "" if r["body_fat_percent"] is None else r["body_fat_percent"],
                    r["memo"] or "", r["created_at"], sync_time
                ])
                ids.append(r["id"])
            inbody_ws.append_rows(batch, value_input_option="USER_ENTERED")
            cur.executemany("UPDATE inbody_logs SET synced_to_sheet=1, synced_at=? WHERE id=?", [(sync_time, i) for i in ids])
            synced_count += len(ids)

        conn.commit()
        conn.close()
        return {
            "ok": True,
            "message": f"저장 반영 완료: {synced_count}건 | 문서명: {ss.title}",
            "synced": synced_count,
            "spreadsheet_url": getattr(ss, "url", ""),
        }
    except Exception as e:
        return {"ok": False, "message": f"시트 반영 실패: {e}", "synced": 0}


def make_sparkline_points(values, width=280, height=76, pad=8):
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return ""
    if len(clean) == 1:
        return f"{pad},{height/2} {width-pad},{height/2}"
    min_v = min(clean)
    max_v = max(clean)
    span = max(max_v - min_v, 1)
    pts = []
    for i, v in enumerate(clean):
        x = pad + (width - pad * 2) * i / (len(clean) - 1)
        y = height - pad - ((v - min_v) / span) * (height - pad * 2)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def get_dashboard_data(days=7):
    conn = db_conn()
    cur = conn.cursor()
    start = (now_dt().date() - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    cur.execute(
        """
        SELECT substr(meal_time,1,10) AS d, COUNT(*) AS meal_count, COALESCE(SUM(COALESCE(calories,0)),0) AS kcal
        FROM meals
        WHERE substr(meal_time,1,10) >= ?
        GROUP BY substr(meal_time,1,10)
        ORDER BY d ASC
        """,
        (start,),
    )
    meal_map = {row["d"]: {"meal_count": row["meal_count"], "kcal": int(row["kcal"] or 0)} for row in cur.fetchall()}

    cur.execute(
        """
        SELECT substr(log_time,1,10) AS d, COUNT(*) AS glucose_count, ROUND(AVG(glucose_value),1) AS glucose_avg
        FROM glucose_logs
        WHERE substr(log_time,1,10) >= ?
        GROUP BY substr(log_time,1,10)
        ORDER BY d ASC
        """,
        (start,),
    )
    glucose_map = {row["d"]: {"glucose_count": row["glucose_count"], "glucose_avg": row["glucose_avg"]} for row in cur.fetchall()}

    cur.execute(
        """
        SELECT log_time, weight, body_fat_percent, skeletal_muscle
        FROM inbody_logs
        WHERE weight IS NOT NULL OR body_fat_percent IS NOT NULL OR skeletal_muscle IS NOT NULL
        ORDER BY log_time DESC, id DESC
        LIMIT 1
        """
    )
    latest_inbody = cur.fetchone()
    conn.close()

    rows = []
    kcal_values, glucose_values = [], []
    for i in range(days):
        d = (now_dt().date() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        m = meal_map.get(d, {})
        g = glucose_map.get(d, {})
        kcal = int(m.get("kcal", 0) or 0)
        gavg = g.get("glucose_avg")
        rows.append({
            "date": d,
            "label": d[5:],
            "meal_count": int(m.get("meal_count", 0) or 0),
            "kcal": kcal,
            "glucose_count": int(g.get("glucose_count", 0) or 0),
            "glucose_avg": "-" if gavg is None else str(int(round(float(gavg)))),
        })
        kcal_values.append(kcal)
        glucose_values.append(None if gavg is None else float(gavg))

    total_week_kcal = sum(kcal_values)
    total_meals = sum(r["meal_count"] for r in rows)
    valid_glucose = [float(r["glucose_avg"]) for r in rows if r["glucose_avg"] != "-"]
    week_avg_glucose = int(round(sum(valid_glucose) / len(valid_glucose))) if valid_glucose else 0

    return {
        "rows": rows,
        "week_kcal": total_week_kcal,
        "week_meals": total_meals,
        "week_avg_glucose": week_avg_glucose,
        "kcal_points": make_sparkline_points(kcal_values),
        "glucose_points": make_sparkline_points(glucose_values),
        "latest_inbody": latest_inbody,
    }


TEMPLATE = r'''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{{ app_title }}</title>
  <style>
    :root{--bg:#f5f7fb;--card:#ffffff;--line:#dbe2ea;--text:#1f2937;--sub:#6b7280;--main:#2563eb;--ok:#16a34a;--warn:#ea580c;--bad:#dc2626;}
    *{box-sizing:border-box;} body{margin:0;background:var(--bg);font-family:Arial,sans-serif;color:var(--text);} .wrap{max-width:760px;margin:0 auto;padding:14px 12px 40px;}
    .title{font-size:24px;font-weight:800;margin:6px 0 10px;} .sub{color:var(--sub);font-size:13px;margin-bottom:14px;}
    .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:12px;box-shadow:0 4px 14px rgba(0,0,0,0.05);} h2{font-size:18px;margin:0 0 12px;} h3{font-size:15px;margin:12px 0 8px;}
    .grid{display:grid;gap:10px;} .grid-2{grid-template-columns:1fr 1fr;} .grid-3{grid-template-columns:repeat(3,1fr);} .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;} @media (max-width:520px){.grid-2,.grid-3,.stats{grid-template-columns:1fr;}}
    label{display:block;font-size:13px;margin-bottom:5px;color:var(--sub);font-weight:700;} input,select,textarea,button{width:100%;font-size:16px;border-radius:12px;border:1px solid var(--line);padding:12px;outline:none;background:#fff;} textarea{min-height:90px;resize:vertical;}
    button{border:none;background:var(--main);color:#fff;font-weight:800;cursor:pointer;} .btn-gray{background:#475569;} .btn-green{background:var(--ok);} .btn-orange{background:var(--warn);} .btn-red{background:var(--bad);} .btn-link{display:inline-block;text-decoration:none;background:#0f172a;color:#fff;padding:10px 12px;border-radius:12px;font-size:14px;font-weight:800;}
    .chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px;} .chip{width:auto;border:none;background:#e8eefc;color:#1e40af;padding:10px 12px;border-radius:999px;font-size:14px;font-weight:700;cursor:pointer;}
    .stat{background:#f8fafc;border:1px solid var(--line);border-radius:14px;padding:12px;text-align:center;} .stat .n{font-size:22px;font-weight:900;margin-top:6px;}
    .row{padding:10px 0;border-top:1px solid #edf2f7;} .row:first-child{border-top:none;} .mini{font-size:12px;color:var(--sub);margin-top:4px;line-height:1.5;word-break:break-word;}
    .badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:800;background:#eef2ff;color:#3730a3;margin-right:6px;}
    .flash{padding:12px;border-radius:12px;background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;margin-bottom:10px;font-size:14px;font-weight:700;} .flash.bad{background:#fef2f2;color:#b91c1c;border-color:#fecaca;}
    .tabs{display:flex;gap:8px;margin-bottom:12px;overflow:auto;} .tablink{text-decoration:none;padding:10px 12px;border-radius:999px;border:1px solid var(--line);color:var(--text);background:#fff;white-space:nowrap;font-size:14px;font-weight:800;} .active{background:var(--main);color:#fff;border-color:var(--main);}
    .status-ok{color:#15803d;font-weight:800;} .status-bad{color:#b91c1c;font-weight:800;} .modal-bg{position:fixed;inset:0;background:rgba(15,23,42,0.55);display:none;align-items:center;justify-content:center;padding:16px;z-index:50;}
    .modal{width:100%;max-width:420px;background:#fff;border-radius:18px;padding:16px;box-shadow:0 18px 48px rgba(0,0,0,0.18);} .modal p{margin:8px 0 12px;color:var(--sub);line-height:1.45;}
    .footer-note{font-size:12px;color:var(--sub);text-align:center;margin-top:14px;} .code{background:#0f172a;color:#e2e8f0;border-radius:12px;padding:10px;font-size:12px;white-space:pre-wrap;word-break:break-all;}
    .trend-table{width:100%;border-collapse:collapse;font-size:13px;} .trend-table th,.trend-table td{padding:9px 6px;border-top:1px solid #edf2f7;text-align:center;} .trend-table th{color:var(--sub);font-size:12px;} .trend-table tr:first-child th,.trend-table tr:first-child td{border-top:none;}
    .spark-wrap{background:#f8fafc;border:1px solid var(--line);border-radius:14px;padding:10px;} .spark-title{font-size:13px;font-weight:800;margin-bottom:6px;} .spark-sub{font-size:12px;color:var(--sub);margin-top:6px;}
  </style>
</head>
<body>
<div class="wrap">
  <div class="title">{{ app_title }}</div>
  <div class="sub">로컬 저장 + 구글시트 반영 + 홈 대시보드</div>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      {% for category, msg in messages %}
        <div class="flash {% if category == 'error' %}bad{% endif %}">{{ msg }}</div>
      {% endfor %}
    {% endif %}
  {% endwith %}

  <div class="tabs">
    <a class="tablink {% if tab=='home' %}active{% endif %}" href="/?tab=home">홈</a>
    <a class="tablink {% if tab=='meal' %}active{% endif %}" href="/?tab=meal">식단</a>
    <a class="tablink {% if tab=='glucose' %}active{% endif %}" href="/?tab=glucose">혈당</a>
    <a class="tablink {% if tab=='inbody' %}active{% endif %}" href="/?tab=inbody">인바디</a>
    <a class="tablink {% if tab=='sync' %}active{% endif %}" href="/?tab=sync">연동</a>
  </div>

  {% if tab == 'home' %}
  <div class="card">
    <h2>오늘 요약</h2>
    <div class="stats">
      <div class="stat"><div>오늘 식단</div><div class="n">{{ today.meal_count }}</div><div class="mini">총 {{ today.meal_kcal }} kcal</div></div>
      <div class="stat"><div>오늘 혈당</div><div class="n">{{ today.glucose_count }}</div><div class="mini">평균 {{ today.avg_glucose if today.glucose_count else '-' }} mg/dL</div></div>
      <div class="stat"><div>오늘 인바디</div><div class="n">{{ today.inbody_count }}</div><div class="mini">최근 체중 {{ dashboard.latest_inbody['weight'] if dashboard.latest_inbody and dashboard.latest_inbody['weight'] is not none else '-' }} kg</div></div>
    </div>
  </div>

  <div class="card">
    <h2>최근 7일 트렌드</h2>
    <div class="grid grid-2">
      <div class="spark-wrap">
        <div class="spark-title">칼로리 추세</div>
        <svg viewBox="0 0 280 76" width="100%" height="90">
          <polyline fill="none" stroke="#2563eb" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" points="{{ dashboard.kcal_points }}"></polyline>
        </svg>
        <div class="spark-sub">7일 총 {{ dashboard.week_kcal }} kcal / 식단 {{ dashboard.week_meals }}건</div>
      </div>
      <div class="spark-wrap">
        <div class="spark-title">혈당 평균 추세</div>
        <svg viewBox="0 0 280 76" width="100%" height="90">
          <polyline fill="none" stroke="#ea580c" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" points="{{ dashboard.glucose_points }}"></polyline>
        </svg>
        <div class="spark-sub">7일 평균 {{ dashboard.week_avg_glucose if dashboard.week_avg_glucose else '-' }} mg/dL</div>
      </div>
    </div>
    <div style="height:10px;"></div>
    <table class="trend-table">
      <tr><th>날짜</th><th>식단</th><th>kcal</th><th>혈당</th><th>평균혈당</th></tr>
      {% for row in dashboard.rows %}
      <tr>
        <td>{{ row.label }}</td><td>{{ row.meal_count }}</td><td>{{ row.kcal }}</td><td>{{ row.glucose_count }}</td><td>{{ row.glucose_avg }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <div class="card">
    <h2>최근 인바디</h2>
    {% if dashboard.latest_inbody %}
    <div class="grid grid-3">
      <div class="stat"><div>체중</div><div class="n">{{ dashboard.latest_inbody['weight'] if dashboard.latest_inbody['weight'] is not none else '-' }}</div><div class="mini">kg</div></div>
      <div class="stat"><div>골격근량</div><div class="n">{{ dashboard.latest_inbody['skeletal_muscle'] if dashboard.latest_inbody['skeletal_muscle'] is not none else '-' }}</div><div class="mini">kg</div></div>
      <div class="stat"><div>체지방률</div><div class="n">{{ dashboard.latest_inbody['body_fat_percent'] if dashboard.latest_inbody['body_fat_percent'] is not none else '-' }}</div><div class="mini">%</div></div>
    </div>
    <div class="mini" style="margin-top:8px;">기준 시각 {{ dashboard.latest_inbody['log_time'] }}</div>
    {% else %}
    <div class="mini">아직 인바디 기록이 없습니다.</div>
    {% endif %}
  </div>
  {% endif %}

  {% if tab == 'meal' %}
  <div class="card">
    <h2>식단 입력</h2>
    <form method="post" action="/save_meal" onsubmit="return beforeMealSubmit();">
      <div class="grid grid-2">
        <div><label>식사 시간</label><input type="datetime-local" name="meal_time" value="{{ now_local }}" required></div>
        <div><label>식사 구분</label><select name="meal_type"><option value="공복">공복</option><option value="아침">아침</option><option value="점심">점심</option><option value="저녁">저녁</option><option value="간식">간식</option><option value="야식">야식</option></select></div>
      </div>
      <h3>반자동 식단 요약</h3>
      <div class="chips">
        <button type="button" class="chip" onclick="addText('밥 ')">밥</button>
        <button type="button" class="chip" onclick="addText('닭가슴살 ')">닭가슴살</button>
        <button type="button" class="chip" onclick="addText('계란 ')">계란</button>
        <button type="button" class="chip" onclick="addText('샐러드 ')">샐러드</button>
        <button type="button" class="chip" onclick="addText('고구마 ')">고구마</button>
        <button type="button" class="chip" onclick="addText('두부 ')">두부</button>
        <button type="button" class="chip" onclick="addText('프로틴 ')">프로틴</button>
        <button type="button" class="chip" onclick="addText('과일 ')">과일</button>
        <button type="button" class="chip" onclick="addText('김치 ')">김치</button>
        <button type="button" class="chip" onclick="addText('커피 ')">커피</button>
      </div>
      <div><label>식단 요약</label><textarea id="meal_summary" name="summary" placeholder="예: 보쌈/삼겹살 + 채소찜 + 계란 1개, 총 약 900~1300kcal" required></textarea></div>
      <div class="grid grid-2">
        <div><label>예상 칼로리(비워두면 문장 속 kcal 자동추출)</label><input type="number" name="calories" min="0" placeholder="예: 550"></div>
        <div><label>메모</label><input type="text" name="memo" placeholder="예: 외식 / 과식 / 운동 후"></div>
      </div>
      <div style="margin-top:10px;"><button class="btn-green" type="submit">식단 저장</button></div>
    </form>
  </div>

  <div class="card">
    <h2>최근 식단</h2>
    {% if recent_meals %}
      {% for row in recent_meals %}
      <div class="row"><div><span class="badge">{{ row['meal_type'] or '식단' }}</span>{{ row['summary'] }}</div><div class="mini">{{ row['meal_time'] }}{% if row['calories'] %} · {{ row['calories'] }}kcal{% endif %}{% if row['calorie_source'] %} · {{ row['calorie_source'] }}{% endif %}{% if row['memo'] %} · {{ row['memo'] }}{% endif %}</div></div>
      {% endfor %}
    {% else %}
      <div class="mini">아직 식단 기록이 없습니다.</div>
    {% endif %}
  </div>
  {% endif %}

  {% if tab == 'glucose' %}
  <div class="card">
    <h2>혈당 입력</h2>
    <form method="post" action="/save_glucose">
      <div class="grid grid-2">
        <div><label>측정 시간</label><input type="datetime-local" name="log_time" value="{{ now_local }}" required></div>
        <div><label>구분</label><select name="tag" required><option value="공복">공복</option><option value="식전">식전</option><option value="식후 2시간">식후 2시간</option><option value="취침 전">취침 전</option><option value="랜덤">랜덤</option></select></div>
      </div>
      <div class="grid grid-2">
        <div><label>혈당 수치</label><input type="number" name="glucose_value" min="1" placeholder="예: 132" required></div>
        <div><label>메모</label><input type="text" name="memo" placeholder="예: 운동 전 / 식후 늦게 측정"></div>
      </div>
      <div style="margin-top:10px;"><button class="btn-orange" type="submit">혈당 저장</button></div>
    </form>
  </div>

  <div class="card">
    <h2>최근 혈당</h2>
    {% if recent_glucose %}
      {% for row in recent_glucose %}
      <div class="row"><div><span class="badge">{{ row['tag'] }}</span>{{ row['glucose_value'] }} mg/dL</div><div class="mini">{{ row['log_time'] }}{% if row['memo'] %} · {{ row['memo'] }}{% endif %}</div></div>
      {% endfor %}
    {% else %}
      <div class="mini">아직 혈당 기록이 없습니다.</div>
    {% endif %}
  </div>
  {% endif %}

  {% if tab == 'inbody' %}
  <div class="card">
    <h2>인바디 입력</h2>
    <form method="post" action="/save_inbody">
      <div class="grid grid-2">
        <div><label>측정 시간</label><input type="datetime-local" name="log_time" value="{{ now_local }}" required></div>
        <div><label>체중(kg)</label><input type="number" name="weight" step="0.1" placeholder="예: 82.4"></div>
      </div>
      <div class="grid grid-2">
        <div><label>골격근량(kg)</label><input type="number" name="skeletal_muscle" step="0.1" placeholder="예: 34.5"></div>
        <div><label>체지방량(kg)</label><input type="number" name="body_fat_mass" step="0.1" placeholder="예: 21.2"></div>
      </div>
      <div class="grid grid-2">
        <div><label>체지방률(%)</label><input type="number" name="body_fat_percent" step="0.1" placeholder="예: 25.7"></div>
        <div><label>메모</label><input type="text" name="memo" placeholder="예: 공복 측정"></div>
      </div>
      <div style="margin-top:10px;"><button class="btn-gray" type="submit">인바디 저장</button></div>
    </form>
  </div>

  <div class="card">
    <h2>최근 인바디</h2>
    {% if recent_inbody %}
      {% for row in recent_inbody %}
      <div class="row"><div>체중 {{ row['weight'] if row['weight'] is not none else '-' }}kg / 골격근량 {{ row['skeletal_muscle'] if row['skeletal_muscle'] is not none else '-' }}kg / 체지방량 {{ row['body_fat_mass'] if row['body_fat_mass'] is not none else '-' }}kg / 체지방률 {{ row['body_fat_percent'] if row['body_fat_percent'] is not none else '-' }}%</div><div class="mini">{{ row['log_time'] }}{% if row['memo'] %} · {{ row['memo'] }}{% endif %}</div></div>
      {% endfor %}
    {% else %}
      <div class="mini">아직 인바디 기록이 없습니다.</div>
    {% endif %}
  </div>
  {% endif %}

  {% if tab == 'sync' %}
  <div class="card">
    <h2>구글시트 연동 상태</h2>
    <div class="mini">시트 이름: <b>{{ sync_status.spreadsheet_name }}</b></div>
    <div class="mini">서비스계정 파일: <b>{{ sync_status.service_account_file }}</b></div>
    <div class="mini">프로젝트: <b>{{ sync_status.project_id or '-' }}</b></div>
    <div class="mini">공유할 이메일: <b>{{ sync_status.service_account_email or '-' }}</b></div>
    <div class="mini" style="margin-top:8px;">상태: {% if sync_status.config_ok %}<span class="status-ok">준비됨</span>{% else %}<span class="status-bad">{{ sync_status.config_msg }}</span>{% endif %}</div>
    <div class="mini" style="margin-top:8px;">미반영 건수: 식단 {{ sync_status.meal_unsynced }} / 혈당 {{ sync_status.glucose_unsynced }} / 인바디 {{ sync_status.inbody_unsynced }} / 총 {{ sync_status.total_unsynced }}</div>
    <div style="height:10px;"></div>
    <div class="grid grid-2">
      <form method="post" action="/sync_now"><button class="btn-gray" type="submit">지금 수동 반영</button></form>
      <form method="post" action="/test_google"><button class="btn-orange" type="submit">구글 연결 테스트</button></form>
    </div>
    <div style="height:10px;"></div>
    <form method="post" action="/reset_app_data" onsubmit="return confirm('앱 기록만 전부 초기화할까? 구글시트 기록은 그대로 남아.');">
      <button class="btn-red" type="submit">앱 기록만 전체 초기화</button>
    </form>
    <div class="mini" style="margin-top:8px;">이 버튼은 Render 앱 DB만 비우고, 구글시트 기록은 지우지 않음</div>
  </div>

  <div class="card">
    <h2>연동 설정</h2>
    <form method="post" action="/save_sync_settings">
      <div class="grid grid-2">
        <div>
          <label>구글시트 이름</label>
          <input type="text" name="spreadsheet_name" value="{{ settings.spreadsheet_name }}" required>
        </div>
        <div>
          <label>서비스계정 파일명</label>
          <input type="text" name="service_account_file" value="{{ settings.service_account_file }}" required>
        </div>
      </div>
      <div class="mini">같은 폴더 안 json 후보: {% if service_candidates %}{{ service_candidates|join(', ') }}{% else %}없음{% endif %}</div>
      <div class="mini" style="margin-top:6px;">시트 자동 생성: <b>{{ '켜짐' if settings.auto_create_spreadsheet else '꺼짐' }}</b></div>
      <div style="height:10px;"></div>
      <button type="submit">연동 설정 저장</button>
    </form>
  </div>

  <div class="card">
    <h2>실행 명령어</h2>
    <div class="code">pip install -r requirements.txt
python diet_glucose_mobile_webapp_v2_deploy_ready_fixed.py</div>
  </div>
  {% endif %}

  <div class="footer-note">연동 실패해도 로컬 DB에는 먼저 저장됨</div>
</div>

<div class="modal-bg" id="fastingModal">
  <div class="modal">
    <h2>오늘 공복 혈당 아직 안 넣었어</h2>
    <p>오늘 첫 접속 기준으로 공복 혈당 기록이 없네. 지금 바로 입력하면 기록이 더 깔끔해져.</p>
    <form method="post" action="/save_glucose">
      <input type="hidden" name="log_time" value="{{ now_local }}">
      <input type="hidden" name="tag" value="공복">
      <label>공복 혈당 수치</label>
      <input type="number" name="glucose_value" min="1" placeholder="예: 128" required>
      <div style="height:8px;"></div>
      <label>메모</label>
      <input type="text" name="memo" placeholder="예: 기상 직후">
      <div style="height:10px;"></div>
      <div class="grid grid-2">
        <button class="btn-orange" type="submit">공복 저장</button>
        <button class="btn-gray" type="button" onclick="closeFastingModal()">나중에</button>
      </div>
    </form>
  </div>
</div>

<script>
  function addText(text){
    const box = document.getElementById('meal_summary');
    box.value = (box.value + text).replace(/\s+/g, ' ').trimStart();
    box.focus();
  }
  async function requestNotificationPermission(){
    if (!("Notification" in window)) return false;
    if (Notification.permission === "granted") return true;
    if (Notification.permission !== "denied") {
      const result = await Notification.requestPermission();
      return result === "granted";
    }
    return false;
  }
  function saveReminderToLocal(reminderObj){
    const arr = JSON.parse(localStorage.getItem("meal_reminders_v2") || "[]");
    arr.push(reminderObj);
    localStorage.setItem("meal_reminders_v2", JSON.stringify(arr));
  }
  async function beforeMealSubmit(){
    const dtInput = document.querySelector('input[name="meal_time"]').value;
    const box = document.getElementById("meal_summary");
    const summary = box ? box.value.trim() : "";
    if (!dtInput || !summary){ alert("식사 시간과 식단 요약을 확인해줘."); return false; }
    const mealDate = new Date(dtInput);
    const remindAt = new Date(mealDate.getTime() + 2 * 60 * 60 * 1000);
    saveReminderToLocal({kind:"meal_2h", summary:summary, meal_time:dtInput, remind_at:remindAt.toISOString(), fired:false});
    await requestNotificationPermission();
    return true;
  }
  function tickReminders(){
    const arr = JSON.parse(localStorage.getItem("meal_reminders_v2") || "[]");
    const now = new Date();
    let changed = false;
    for (const item of arr){
      if (!item.fired && item.remind_at){
        const remindAt = new Date(item.remind_at);
        if (now >= remindAt){
          item.fired = true; changed = true;
          if ("Notification" in window && Notification.permission === "granted"){
            new Notification("식후 2시간 혈당 체크할 시간", {body: item.summary ? `식단: ${item.summary}` : "혈당 측정 기록해줘"});
          } else { alert("식후 2시간 혈당 체크할 시간!"); }
        }
      }
    }
    if (changed){ localStorage.setItem("meal_reminders_v2", JSON.stringify(arr)); }
  }
  function openFastingModal(){ document.getElementById("fastingModal").style.display = "flex"; }
  function closeFastingModal(){ document.getElementById("fastingModal").style.display = "none"; localStorage.setItem("skip_fasting_popup_{{ today_date }}", "1"); }
  document.addEventListener("DOMContentLoaded", function(){
    tickReminders(); setInterval(tickReminders, 30000);
    const shouldShowFasting = {{ "true" if show_fasting_popup else "false" }};
    const skipped = localStorage.getItem("skip_fasting_popup_{{ today_date }}");
    if (shouldShowFasting && !skipped){ setTimeout(openFastingModal, 500); }
  });
</script>
</body>
</html>'''


@app.route("/")
def home():
    tab = request.args.get("tab", "home")
    settings = load_settings()
    candidate_names = [p.name for p in detect_service_account_candidates()]
    return render_template_string(
        TEMPLATE,
        app_title=APP_TITLE,
        tab=tab,
        today=get_today_summary(),
        dashboard=get_dashboard_data(7),
        now_local=dt_local_value(),
        today_date=today_str(),
        show_fasting_popup=(not has_fasting_today()),
        recent_meals=get_recent_rows("meals", "meal_time", 8),
        recent_glucose=get_recent_rows("glucose_logs", "log_time", 8),
        recent_inbody=get_recent_rows("inbody_logs", "log_time", 8),
        sync_status=get_sync_status(),
        settings=settings,
        service_candidates=candidate_names,
    )


@app.post("/save_sync_settings")
def save_sync_settings_route():
    current = load_settings()
    spreadsheet_name = request.form.get("spreadsheet_name", "").strip()
    service_account_file = request.form.get("service_account_file", "").strip()
    current["spreadsheet_name"] = spreadsheet_name or current.get("spreadsheet_name", DEFAULT_SPREADSHEET_NAME)
    current["service_account_file"] = service_account_file or current.get("service_account_file", DEFAULT_SERVICE_ACCOUNT_NAME)
    save_settings(current)
    flash("연동 설정 저장 완료", "info")
    return redirect(url_for("home", tab="sync"))


@app.post("/save_meal")
def save_meal():
    meal_time = normalize_dt(request.form.get("meal_time", ""))
    meal_type = request.form.get("meal_type", "").strip()
    summary = request.form.get("summary", "").strip()
    calories_raw = request.form.get("calories", "").strip()
    memo = request.form.get("memo", "").strip()

    if not meal_time or not summary:
        flash("식단 저장 실패: 시간과 식단 요약은 필수야.", "error")
        return redirect(url_for("home", tab="meal"))

    calorie_source = None
    if calories_raw.isdigit():
        calories = int(calories_raw)
        calorie_source = "직접입력"
    else:
        calories, calorie_source = parse_calories_from_text(summary)

    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO meals (meal_time, meal_type, summary, calories, calorie_source, memo, created_at, synced_to_sheet, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)
        """,
        (meal_time, meal_type, summary, calories, calorie_source, memo, now_str()),
    )
    conn.commit()
    conn.close()

    sync_result = sync_rows_to_sheet()
    if sync_result["ok"]:
        kcal_msg = f" | 칼로리 {calories}kcal" if calories is not None else ""
        flash(f"식단 저장 완료{kcal_msg}", "info")
    else:
        flash(f"식단은 저장 완료, 시트는 아직 안 붙음: {sync_result['message']}", "error")
    return redirect(url_for("home", tab="meal"))


@app.post("/save_glucose")
def save_glucose():
    log_time = normalize_dt(request.form.get("log_time", ""))
    glucose_value_raw = request.form.get("glucose_value", "").strip()
    tag = request.form.get("tag", "").strip()
    memo = request.form.get("memo", "").strip()

    if not log_time or not glucose_value_raw.isdigit() or not tag:
        flash("혈당 저장 실패: 시간/수치/구분을 확인해줘.", "error")
        return redirect(url_for("home", tab="glucose"))

    glucose_value = int(glucose_value_raw)

    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO glucose_logs (log_time, glucose_value, tag, memo, created_at, synced_to_sheet, synced_at)
        VALUES (?, ?, ?, ?, ?, 0, NULL)
        """,
        (log_time, glucose_value, tag, memo, now_str()),
    )
    conn.commit()
    conn.close()

    sync_result = sync_rows_to_sheet()
    if sync_result["ok"]:
        flash("혈당 저장 완료", "info")
    else:
        flash(f"혈당은 저장 완료, 시트는 아직 안 붙음: {sync_result['message']}", "error")
    return redirect(url_for("home", tab="glucose"))


@app.post("/save_inbody")
def save_inbody():
    log_time = normalize_dt(request.form.get("log_time", ""))

    def f(name):
        raw = request.form.get(name, "").strip()
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    weight = f("weight")
    skeletal_muscle = f("skeletal_muscle")
    body_fat_mass = f("body_fat_mass")
    body_fat_percent = f("body_fat_percent")
    memo = request.form.get("memo", "").strip()

    if not log_time:
        flash("인바디 저장 실패: 시간은 필수야.", "error")
        return redirect(url_for("home", tab="inbody"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO inbody_logs (
            log_time, weight, skeletal_muscle, body_fat_mass, body_fat_percent, memo, created_at, synced_to_sheet, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)
        """,
        (log_time, weight, skeletal_muscle, body_fat_mass, body_fat_percent, memo, now_str()),
    )
    conn.commit()
    conn.close()

    sync_result = sync_rows_to_sheet()
    if sync_result["ok"]:
        flash("인바디 저장 완료", "info")
    else:
        flash(f"인바디는 저장 완료, 시트는 아직 안 붙음: {sync_result['message']}", "error")
    return redirect(url_for("home", tab="inbody"))


@app.post("/sync_now")
def sync_now_route():
    result = sync_rows_to_sheet()
    if result["ok"]:
        flash(result["message"], "info")
    else:
        flash(result["message"], "error")
    return redirect(url_for("home", tab="sync"))


@app.post("/test_google")
def test_google_route():
    ready, msg = check_google_ready()
    if not ready:
        flash(msg, "error")
        return redirect(url_for("home", tab="sync"))
    try:
        ss = open_or_create_spreadsheet()
        flash(f"구글 연결 성공 | 문서명: {ss.title}", "info")
    except Exception as e:
        flash(f"구글 연결 실패: {e}", "error")
    return redirect(url_for("home", tab="sync"))


@app.post("/reset_app_data")
def reset_app_data_route():
    reset_app_data()
    flash("앱 기록이 모두 초기화됐어. 구글시트 기록은 그대로야.", "info")
    return redirect(url_for("home", tab="sync"))


@app.route("/api/today_status")
def api_today_status():
    return jsonify({
        "today": get_today_summary(),
        "dashboard": get_dashboard_data(7),
        "has_fasting_today": has_fasting_today(),
        "server_now": now_str(),
        "sheet_sync": get_sync_status(),
        "settings": load_settings(),
    })


# 배포 환경에서도 import 즉시 초기화되게 처리
init_db()
if not SETTINGS_PATH.exists():
    chosen = service_account_path_from_settings()
    settings = load_settings()
    if chosen.exists():
        settings["service_account_file"] = chosen.name
    save_settings(settings)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claim_service.py — standalone expense-claim service.

Design: this service is completely independent of the WhatsApp layer.
- It is a small Flask process listening on 127.0.0.1:5005.
- A WhatsApp bridge (see bridge/whatsapp_bridge.js) forwards "claim-related"
  private messages here over HTTP.
- If this service is down, the bridge call simply fails and the message is
  left alone — so the claim feature can never take down your WhatsApp bot.

Conversation flow (employee, private chat, English prompts):
    claim/expense -> [first time] ask name -> pick type (1/2/3) -> amount
                  -> receipt photo -> (Other) note -> done
Commands: cancel (abort current claim); mine/history (this month's claims).
"""

import os
import re
import json
import sqlite3
import threading
import time
from datetime import datetime

from flask import Flask, request, jsonify

# -- Paths & configuration ------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "claim_config.json")

DEFAULT_CONFIG = {
    "finance_whatsapp": "",
    "finance_setup_code": "",
    "export_day": 1,
    "export_hour": 9,
    "currency": "USD",
    "currency_symbol": "$",
    "receipts_dir": "receipts",
    "db_path": "claims.db",
    "proactive_queue_path": "/tmp/wa_proactive_queue.json",
    "receipt_host_url": "",
    "receipt_upload_secret": "",
    "receipt_ttl_days": 60,
    "port": 5005,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        print(f"[Claim] {CONFIG_PATH} not found, using defaults (monthly report won't be sent)")
    except Exception as e:
        print(f"[Claim] Failed to read config: {e}, using defaults")
    return cfg


CONFIG = load_config()
DB_PATH = os.path.join(BASE_DIR, CONFIG["db_path"])
RECEIPTS_DIR = os.path.join(BASE_DIR, CONFIG["receipts_dir"])
os.makedirs(RECEIPTS_DIR, exist_ok=True)

CURRENCY = CONFIG.get("currency", "USD")
CURRENCY_SYMBOL = CONFIG.get("currency_symbol") or "$"

# Trigger words / commands
TRIGGERS = {"claim", "expense", "claims"}
CANCEL_WORDS = {"cancel", "quit", "exit"}
# Keep history words distinct to avoid clashing with your bot's normal messages
HISTORY_WORDS = {"mine", "history", "my claims"}

TYPE_LABELS = {"taxi": "🚕 Taxi", "food": "🍱 Food", "other": "📦 Other"}

TYPE_MENU = (
    "What type of claim?\n"
    "1. 🚕 Taxi\n"
    "2. 🍱 Food\n"
    "3. 📦 Other\n\n"
    "Reply 1, 2 or 3."
)

app = Flask(__name__)
_db_lock = threading.Lock()


# -- Database -------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wa_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wa_id TEXT NOT NULL,
                employee_name TEXT NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
                note TEXT,
                status TEXT NOT NULL DEFAULT 'submitted',
                receipt_path TEXT,
                claim_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_states (
                wa_id TEXT PRIMARY KEY,
                step TEXT NOT NULL,
                draft_json TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS export_log (
                period TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finance_users (
                wa_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipt_uploads (
                receipt_path TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );
            """
        )


def get_employee(wa_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM employees WHERE wa_id = ?", (wa_id,)
        ).fetchone()


def create_employee(wa_id, name):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO employees (wa_id, name, created_at) VALUES (?,?,?)",
            (wa_id, name, datetime.now().isoformat()),
        )


def get_state(wa_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM conversation_states WHERE wa_id = ?", (wa_id,)
        ).fetchone()


def set_state(wa_id, step, draft):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO conversation_states (wa_id, step, draft_json, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(wa_id) DO UPDATE SET
                   step=excluded.step,
                   draft_json=excluded.draft_json,
                   updated_at=excluded.updated_at""",
            (wa_id, step, json.dumps(draft or {}), datetime.now().isoformat()),
        )


def clear_state(wa_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM conversation_states WHERE wa_id = ?", (wa_id,))


def insert_claim(wa_id, name, ctype, amount, note, receipt_path):
    now = datetime.now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO claims
               (wa_id, employee_name, type, amount, currency, note, status,
                receipt_path, claim_date, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                wa_id, name, ctype, amount, CURRENCY, note, "submitted",
                receipt_path, now.strftime("%Y-%m-%d"), now.isoformat(),
            ),
        )


def month_claims(wa_id, now=None):
    now = now or datetime.now()
    prefix = now.strftime("%Y-%m")
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM claims WHERE wa_id = ? AND substr(claim_date,1,7) = ? "
            "ORDER BY claim_date",
            (wa_id, prefix),
        ).fetchall()


# -- Finance --------------------------------------------------------------
FINANCE_HELP = (
    "💼 Finance commands:\n"
    "• report <name> — this month's Excel for that employee\n"
    "• report <name> 2026-05 — a specific month\n"
    "• report all — everyone, this month\n"
    "• employees — list staff & their monthly totals"
)


def _is_finance(sender_number, wa_id):
    """Recognise the finance user by the finance_whatsapp number in config
    (no registration needed). sender_number is the real phone number parsed by
    the bridge; wa_id is a fallback (contains the number for @c.us ids)."""
    fin = re.sub(r"[^0-9]", "", str(CONFIG.get("finance_whatsapp", "")))
    if not fin:
        return False
    for cand in (sender_number, wa_id):
        d = re.sub(r"[^0-9]", "", cand or "")
        if d and (d == fin or d.endswith(fin) or fin.endswith(d)):
            return True
    return False


def list_employees_text():
    with get_conn() as conn:
        emps = conn.execute("SELECT name, wa_id FROM employees ORDER BY name").fetchall()
    if not emps:
        return "No employees registered yet."
    lines = []
    for e in emps:
        rows = month_claims(e["wa_id"])
        total = sum(r["amount"] for r in rows)
        lines.append(f"• {e['name']}: {len(rows)} claim(s), {fmt_money(total)} this month")
    return "👥 Employees:\n" + "\n".join(lines) + "\n\nSend 'report <name>' for their Excel."


def handle_finance_command(text):
    """Finance commands; 'report' runs outside the lock (Excel can be slow)."""
    low = text.strip().lower()
    if low in ("help", "?"):
        return reply(FINANCE_HELP)
    if low in ("employees", "list", "staff"):
        return reply(list_employees_text())
    if low.startswith("report"):
        parts = text.split()
        rest = parts[1:]
        # On-demand finance query defaults to the current month
        # (different from "auto-send previous month on the 1st").
        period = datetime.now().strftime("%Y-%m")
        if rest and re.match(r"^\d{4}-\d{2}$", rest[-1]):
            period = rest[-1]
            rest = rest[:-1]
        name = " ".join(rest).strip()
        target = None if (not name or name.lower() == "all") else name
        try:
            from claim_export import generate_and_send
            ok, msg = generate_and_send(period, target)
        except Exception as e:
            return reply(f"❌ Failed to generate: {e}")
        tip = "\n\nTip: open with a spreadsheet app and tap '📎 View receipt' to see each receipt." if ok else ""
        return reply(("📤 " if ok else "⚠️ ") + msg + tip)
    return reply(FINANCE_HELP)


# -- Helpers --------------------------------------------------------------
def parse_amount(text):
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        n = float(cleaned)
    except ValueError:
        return None
    if n <= 0 or n > 100000:
        return None
    return round(n, 2)


def parse_type(text):
    t = (text or "").strip().lower()
    if t in ("1", "taxi", "🚕"):
        return "taxi"
    if t in ("2", "food", "🍱"):
        return "food"
    if t in ("3", "other", "📦"):
        return "other"
    return None


def fmt_money(amount):
    return f"{CURRENCY_SYMBOL}{amount:.2f}"


def month_summary_text(wa_id, name):
    rows = month_claims(wa_id)
    if not rows:
        return f"No claims submitted yet this month, {name}."
    lines = [
        f"• {TYPE_LABELS.get(r['type'], r['type'])}  {fmt_money(r['amount'])}  ({r['claim_date']})"
        for r in rows
    ]
    total = sum(r["amount"] for r in rows)
    return (
        f"📋 Your claims this month ({len(rows)}):\n\n"
        + "\n".join(lines)
        + f"\n\nTotal: {fmt_money(total)}"
    )


def confirmation_text(wa_id, ctype, amount, note):
    rows = month_claims(wa_id)
    total = sum(r["amount"] for r in rows)
    msg = (
        "✅ Recorded\n"
        f"Type: {TYPE_LABELS[ctype]}  {fmt_money(amount)}\n"
        f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
    )
    if note:
        msg += f"Note: {note}\n"
    msg += (
        f"\nThis month: {len(rows)} claim(s), {fmt_money(total)} total\n"
        "Send 'claim' to submit another."
    )
    return msg


def reply(text, fetch_media=False):
    """Unified response to the bridge. handled=True means we claimed this message."""
    return jsonify({"handled": True, "reply": text, "fetch_media": fetch_media})


def not_handled():
    return jsonify({"handled": False})


# -- Main entry: handle one incoming message ------------------------------
@app.route("/claim/incoming", methods=["POST"])
def claim_incoming():
    data = request.get_json(silent=True) or {}
    wa_id = data.get("sender")
    text = (data.get("text") or "").strip()
    has_media = bool(data.get("has_media"))
    if not wa_id:
        return not_handled()

    low_all = text.lower().strip()
    sender_number = data.get("sender_number") or ""

    # -- Finance commands (recognised by finance_whatsapp, no registration) --
    # Run outside the lock: 'report' may build an Excel; don't block employees.
    if _is_finance(sender_number, wa_id) and (
        low_all.startswith("report")
        or low_all in ("employees", "list", "staff", "help", "?")
    ):
        return handle_finance_command(text)

    with _db_lock:
        state = get_state(wa_id)
        employee = get_employee(wa_id)
        low = text.lower()
        is_trigger = low in TRIGGERS
        # History command only works for already-registered employees, so we
        # don't accidentally swallow your bot's normal messages.
        is_history = (low in HISTORY_WORDS) and employee is not None

        # Not in a claim session and not a trigger/history word -> don't claim it.
        if state is None and not is_trigger and not is_history:
            return not_handled()

        # Global commands
        if low in CANCEL_WORDS:
            clear_state(wa_id)
            return reply("Cancelled. Send 'claim' to start a new claim.")

        if is_history:
            return reply(month_summary_text(wa_id, employee["name"]))

        # First use: register the name
        if employee is None:
            if state is not None and state["step"] == "awaiting_name" and text:
                name = text[:80]
                create_employee(wa_id, name)
                set_state(wa_id, "awaiting_type", {})
                return reply(f"Thanks {name}! You're registered. ✅\n\n" + TYPE_MENU)
            set_state(wa_id, "awaiting_name", {})
            return reply(
                "👋 Welcome to the Claim Bot!\n\n"
                "You're not registered yet — what's your name?"
            )

        step = state["step"] if state else "idle"
        draft = json.loads(state["draft_json"]) if state and state["draft_json"] else {}

        # Trigger word: (re)start
        if is_trigger and step in ("idle", "awaiting_type"):
            set_state(wa_id, "awaiting_type", {})
            return reply(f"Hi {employee['name']}! " + TYPE_MENU)

        if step == "awaiting_type":
            ctype = parse_type(text)
            if not ctype:
                return reply("Please reply 1 (Taxi), 2 (Food) or 3 (Other).")
            set_state(wa_id, "awaiting_amount", {"type": ctype})
            return reply(f"{TYPE_LABELS[ctype]} selected.\nPlease enter the amount ({CURRENCY}).")

        if step == "awaiting_amount":
            amount = parse_amount(text)
            if amount is None:
                return reply(f"Please enter a valid amount in {CURRENCY}, e.g. 12.50")
            draft["amount"] = amount
            set_state(wa_id, "awaiting_receipt", draft)
            return reply(
                f"Amount: {fmt_money(amount)}\n\nNow please send the receipt photo 📷"
            )

        if step == "awaiting_receipt":
            if has_media:
                # Ask the bridge to download the image and call /claim/media
                return reply(None, fetch_media=True)
            return reply("Please send a *photo* of the receipt 📷")

        if step == "awaiting_note":
            if not text:
                return reply("Please type a short note describing the expense.")
            draft["note"] = text[:300]
            ctype = draft["type"]
            insert_claim(
                wa_id, employee["name"], ctype, draft["amount"],
                draft.get("note"), draft.get("receipt_path"),
            )
            clear_state(wa_id)
            return reply(confirmation_text(wa_id, ctype, draft["amount"], draft.get("note")))

        # Fallback: unexpected state, reset.
        clear_state(wa_id)
        return reply("Let's start over. Send 'claim' to submit a claim.")


# -- Receipt image callback (bridge calls this after downloading) ---------
@app.route("/claim/media", methods=["POST"])
def claim_media():
    data = request.get_json(silent=True) or {}
    wa_id = data.get("sender")
    file_path = data.get("file_path")
    mime = data.get("mime") or "image/jpeg"
    if not wa_id or not file_path or not os.path.exists(file_path):
        return jsonify({"reply": "Sorry, the receipt didn't come through. Please send it again."})

    with _db_lock:
        state = get_state(wa_id)
        employee = get_employee(wa_id)
        if state is None or state["step"] != "awaiting_receipt" or employee is None:
            return jsonify({"reply": None})

        draft = json.loads(state["draft_json"]) if state["draft_json"] else {}

        # Save the receipt to the permanent directory
        ext = mime.split("/")[-1].split(";")[0] or "jpg"
        if ext == "jpeg":
            ext = "jpg"
        safe_id = re.sub(r"[^0-9A-Za-z]", "_", wa_id)
        dest_dir = os.path.join(RECEIPTS_DIR, safe_id)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")
        try:
            with open(file_path, "rb") as src, open(dest, "wb") as out:
                out.write(src.read())
        except Exception as e:
            print(f"[Claim] Failed to save receipt: {e}")
            return jsonify({"reply": "Sorry, couldn't save the receipt. Please try again."})
        finally:
            try:
                os.remove(file_path)  # clean up the bridge's temp file
            except OSError:
                pass

        draft["receipt_path"] = dest
        ctype = draft["type"]

        if ctype == "other":
            set_state(wa_id, "awaiting_note", draft)
            return jsonify({"reply": "Got it. Please add a short note describing this expense."})

        # taxi / food: complete immediately
        insert_claim(
            wa_id, employee["name"], ctype, draft["amount"], None, dest
        )
        clear_state(wa_id)
        return jsonify({"reply": confirmation_text(wa_id, ctype, draft["amount"], None)})


# -- Manually trigger the monthly report ----------------------------------
@app.route("/claim/export", methods=["POST"])
def claim_export_endpoint():
    period = (request.get_json(silent=True) or {}).get("period")
    try:
        from claim_export import generate_and_send
        ok, msg = generate_and_send(period)
        return jsonify({"ok": ok, "message": msg})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "claim", "db": DB_PATH})


# -- Monthly scheduled export (own background thread) ---------------------
def _already_exported(period):
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM export_log WHERE period = ?", (period,)
        ).fetchone() is not None


def _mark_exported(period):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO export_log (period, sent_at) VALUES (?,?)",
            (period, datetime.now().isoformat()),
        )


def _prev_month_period(now):
    y, m = now.year, now.month
    return f"{y-1}-12" if m == 1 else f"{y}-{m-1:02d}"


def scheduler_loop():
    export_day = int(CONFIG.get("export_day", 1))
    export_hour = int(CONFIG.get("export_hour", 9))
    print(f"[Claim] Monthly scheduler started: day {export_day} at {export_hour}:00, sends previous month")
    while True:
        try:
            now = datetime.now()
            if now.day == export_day and now.hour >= export_hour:
                period = _prev_month_period(now)
                if not _already_exported(period):
                    print(f"[Claim] Triggering monthly export: {period}")
                    try:
                        from claim_export import generate_and_send
                        ok, msg = generate_and_send(period)
                        print(f"[Claim] Export result: ok={ok} {msg}")
                        if ok:
                            _mark_exported(period)
                    except Exception as e:
                        print(f"[Claim] Export error: {e}")
        except Exception as e:
            print(f"[Claim] Scheduler loop error: {e}")
        time.sleep(3600)  # check hourly


def main():
    init_db()
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    port = int(CONFIG.get("port", 5005))
    print(f"[Claim] Claim service started: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()

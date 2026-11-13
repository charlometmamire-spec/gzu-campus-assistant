import os
import time
import sqlite3
import secrets
from functools import wraps
from pathlib import Path

from flask import (
    render_template, request, redirect, url_for,
    session, flash, jsonify
)
from werkzeug.security import check_password_hash


def register_admin_routes(app, database_path):
    """Register secure administrator and FAQ routes."""

    db_path = Path(database_path)

    app.secret_key = (
        os.environ.get("FLASK_SECRET_KEY")
        or app.secret_key
        or secrets.token_hex(32)
    )

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(
            os.environ.get("RENDER") is not None
        ),
    )

    # Create supporting security and activity tables.
    def connect_db():
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        return connection

    def initialise_tables():
        with connect_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS admin_login_attempts (
                    ip_address TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt REAL NOT NULL
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS admin_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

    initialise_tables()

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    def valid_csrf():
        supplied = (
            request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token")
        )
        expected = session.get("csrf_token")
        return bool(expected and supplied and secrets.compare_digest(
            expected, supplied
        ))

    def log_activity(action, details=""):
        with connect_db() as db:
            db.execute(
                "INSERT INTO admin_audit_logs (action, details) VALUES (?, ?)",
                (action, details[:500])
            )

    def admin_required(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            if not session.get("is_admin"):
                return jsonify({"error": "Administrator login required"}), 401
            return function(*args, **kwargs)
        return wrapper

    def login_limited(ip):
        with connect_db() as db:
            row = db.execute(
                "SELECT attempts, last_attempt FROM admin_login_attempts "
                "WHERE ip_address = ?", (ip,)
            ).fetchone()

        if not row:
            return False

        # Reset the limit after 15 minutes.
        if time.time() - row["last_attempt"] > 900:
            return False

        return row["attempts"] >= 5

    def record_failed_login(ip):
        now = time.time()
        with connect_db() as db:
            db.execute("""
                INSERT INTO admin_login_attempts
                    (ip_address, attempts, last_attempt)
                VALUES (?, 1, ?)
                ON CONFLICT(ip_address) DO UPDATE SET
                    attempts = CASE
                        WHEN ? - last_attempt > 900 THEN 1
                        ELSE attempts + 1
                    END,
                    last_attempt = ?
            """, (ip, now, now, now))

    # Public read-only FAQ endpoint for students.
    @app.route("/api/faqs", methods=["GET"])
    def public_faqs():
        with connect_db() as db:
            rows = db.execute(
                "SELECT rowid AS id, question, answer "
                "FROM faqs ORDER BY rowid DESC"
            ).fetchall()

        return jsonify([dict(row) for row in rows])

    # Administrator login page.
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if session.get("is_admin"):
            return redirect(url_for("admin_dashboard"))

        if request.method == "GET":
            return render_template(
                "admin_login.html",
                csrf_token=csrf_token()
            )

        if not valid_csrf():
            return "Invalid security token. Refresh and try again.", 400

        ip = request.remote_addr or "unknown"

        if login_limited(ip):
            return "Too many failed attempts. Please try again in 15 minutes.", 429

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        expected_username = os.environ.get("ADMIN_USERNAME", "")
        password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")

        if not expected_username or not password_hash:
            return "Admin credentials have not been configured in Render.", 503

        username_ok = secrets.compare_digest(
            username, expected_username
        )

        try:
            password_ok = check_password_hash(password_hash, password)
        except (ValueError, TypeError):
            password_ok = False

        if username_ok and password_ok:
            session.clear()
            session["is_admin"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)

            with connect_db() as db:
                db.execute(
                    "DELETE FROM admin_login_attempts WHERE ip_address = ?",
                    (ip,)
                )

            log_activity("LOGIN", "Administrator logged in")
            return redirect(url_for("admin_dashboard"))

        record_failed_login(ip)
        log_activity("FAILED_LOGIN", "Unsuccessful login attempt")
        flash("Incorrect username or password.", "error")

        return redirect(url_for("admin_login"))

    # Protected administrator dashboard.
    @app.route("/admin", methods=["GET"])
    @admin_required
    def admin_dashboard():
        return render_template(
            "admin_dashboard.html",
            csrf_token=csrf_token()
        )

    # Administrator logout.
    @app.route("/admin/logout", methods=["POST"])
    @admin_required
    def admin_logout():
        if not valid_csrf():
            return "Invalid security token.", 400

        log_activity("LOGOUT", "Administrator logged out")
        session.clear()
        return redirect(url_for("admin_login"))

    # Add, edit and delete FAQs.
    @app.route("/admin/api/faqs", methods=["POST", "PUT", "DELETE"])
    @admin_required
    def manage_faqs():
        if not valid_csrf():
            return jsonify({"error": "Invalid security token"}), 400

        data = request.get_json(silent=True) or {}

        if request.method == "POST":
            question = str(data.get("question", "")).strip()
            answer = str(data.get("answer", "")).strip()

            if not question or not answer:
                return jsonify({"error": "Question and answer are required"}), 400

            with connect_db() as db:
                cursor = db.execute(
                    "INSERT INTO faqs (question, answer) VALUES (?, ?)",
                    (question, answer)
                )
                faq_id = cursor.lastrowid

            log_activity("ADD_FAQ", f"Added FAQ {faq_id}")
            return jsonify({"message": "FAQ added successfully", "id": faq_id}), 201

        faq_id = data.get("id")

        try:
            faq_id = int(faq_id)
        except (TypeError, ValueError):
            return jsonify({"error": "A valid FAQ ID is required"}), 400

        if request.method == "PUT":
            question = str(data.get("question", "")).strip()
            answer = str(data.get("answer", "")).strip()

            if not question or not answer:
                return jsonify({"error": "Question and answer are required"}), 400

            with connect_db() as db:
                cursor = db.execute(
                    "UPDATE faqs SET question = ?, answer = ? WHERE rowid = ?",
                    (question, answer, faq_id)
                )

            if cursor.rowcount == 0:
                return jsonify({"error": "FAQ not found"}), 404

            log_activity("EDIT_FAQ", f"Updated FAQ {faq_id}")
            return jsonify({"message": "FAQ updated successfully"})

        with connect_db() as db:
            cursor = db.execute(
                "DELETE FROM faqs WHERE rowid = ?", (faq_id,)
            )

        if cursor.rowcount == 0:
            return jsonify({"error": "FAQ not found"}), 404

        log_activity("DELETE_FAQ", f"Deleted FAQ {faq_id}")
        return jsonify({"message": "FAQ deleted successfully"})

    # View recent administrator activity.
    @app.route("/admin/api/logs", methods=["GET"])
    @admin_required
    def admin_logs():
        with connect_db() as db:
            rows = db.execute(
                "SELECT action, details, created_at "
                "FROM admin_audit_logs ORDER BY id DESC LIMIT 100"
            ).fetchall()

        return jsonify([dict(row) for row in rows])

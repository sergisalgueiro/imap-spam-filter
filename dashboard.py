#!/usr/bin/env python3
"""
Dashboard web per al IMAP Spam Filter (v2.0 Multi-Compte).
Mostra mètriques globals i per bústia, llistat de correus filtrats,
i permet rescatar falsos positius utilitzant les credencials del compte respectiu.
Exposa endpoint /metrics per a Prometheus amb etiquetes de compte.
"""

import os
import sys
import json
import imaplib
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify, request, redirect, url_for
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(path, override=False):
        if not os.path.exists(path):
            return
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if override or k not in os.environ:
                    os.environ[k] = v

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ENV_PATH = os.environ.get("ENV_FILE", "/opt/imap-spam-filter/.env")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)
elif os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from imap_bridge import (
    Database,
    IMAP_HOST,
    IMAP_PORT,
    IMAP_SSL,
    FOLDER_SPAM_DEST,
    FOLDER_INBOX,
    rspamd_learn,
    load_accounts,
)

DB_PATH = os.environ.get("DB_PATH", "/opt/imap-spam-filter/data/spam_filter.db")
DASH_PORT = int(os.environ.get("DASHBOARD_PORT", "8025"))

templates_dir = "/opt/imap-spam-filter/templates"
if not os.path.exists(templates_dir):
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

app = Flask(__name__, template_folder=templates_dir)
db = Database(DB_PATH)


@app.context_processor
def inject_global_data():
    acc = request.args.get("account", "").strip() or None
    accounts = db.get_configured_or_seen_accounts()
    t = db.get_totals(account=acc)
    return {
        "current_account": acc or "",
        "accounts_list": accounts,
        "count_all": t.get("scanned", 0),
        "count_spam": t.get("spam", 0),
        "count_ham": t.get("ham", 0),
        "count_rescued": t.get("rescued", 0),
    }


@app.route("/")
def index():
    acc = request.args.get("account", "").strip() or None
    totals = db.get_totals(account=acc)
    daily = db.get_daily_stats(days=30, account=acc)
    recent_spam = db.get_emails(filter_type="spam", account=acc, limit=20)
    account_totals = db.get_account_totals()
    return render_template(
        "dashboard.html",
        totals=totals,
        daily=daily,
        recent_spam=recent_spam,
        account_totals=account_totals,
        active_tab="dashboard",
    )


@app.route("/emails")
def emails_list():
    ftype = request.args.get("filter", "all")
    acc = request.args.get("account", "").strip() or None
    page = int(request.args.get("page", 1))
    limit = 50
    offset = (page - 1) * limit
    emails = db.get_emails(filter_type=ftype, account=acc, limit=limit, offset=offset)
    return render_template(
        "emails.html",
        emails=emails,
        filter_type=ftype,
        page=page,
        active_tab=ftype,
    )


@app.route("/email/<int:eid>")
def email_detail(eid):
    em = db.get_email(eid)
    if not em:
        return "No trobat", 404
    return render_template("email_detail.html", email=em, active_tab="detail")


@app.route("/rescue/<int:eid>", methods=["POST"])
def rescue_email(eid):
    """Mou un email de SpamFilter a INBOX del compte corresponent i entrena Rspamd com ham."""
    em = db.get_email(eid)
    if not em:
        return jsonify({"error": "No trobat"}), 404

    target_acc = em.get("account", "").strip()
    accounts = load_accounts()
    account_cfg = next((a for a in accounts if a["user"] == target_acc), None)

    # Si no es troba pel nom exacte, fallback al primer compte configurat
    if not account_cfg and accounts:
        account_cfg = accounts[0]

    if not account_cfg:
        return jsonify({"error": f"Credencials no trobades per al compte {target_acc}"}), 500

    user = account_cfg["user"]
    passwd = account_cfg["pass"]

    try:
        if IMAP_SSL:
            c = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        else:
            c = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)

        c.login(user, passwd)

        dest_folder = FOLDER_SPAM_DEST
        typ, _ = c.select(f'"{dest_folder}"')
        if typ != "OK":
            dest_folder = f"INBOX.{FOLDER_SPAM_DEST}"
            c.select(f'"{dest_folder}"')

        uids = []
        if em.get("uid"):
            uids = [em["uid"]]
        else:
            typ, data = c.uid("search", None, f'HEADER Message-ID "{em["message_id"]}"')
            if typ == "OK" and data[0]:
                uids = [u.decode() if isinstance(u, bytes) else str(u) for u in data[0].split()]

        if uids:
            uid_str = uids[0]
            typ, fetch_data = c.uid("fetch", uid_str, "(RFC822)")
            if typ == "OK" and fetch_data and fetch_data[0]:
                raw = fetch_data[0][1]
                rspamd_learn(raw, spam=False, recipient=user)
                db.mark_learned(em["message_id"], "ham", account=user)

            # Moure a INBOX
            moved = False
            try:
                typ, _ = c.uid("move", uid_str, f'"{FOLDER_INBOX}"')
                if typ == "OK":
                    moved = True
            except Exception:
                pass

            if not moved:
                c.uid("copy", uid_str, f'"{FOLDER_INBOX}"')
                c.uid("store", uid_str, "+FLAGS", "(\\Deleted)")
                c.expunge()

        c.logout()

        db.update_status(em["message_id"], "rescued", "rescued", account=user)
        db.bump_stats(account=user, rescued=1, learned_ham=1)
        db.event("INFO", f"Correu rescatat i mogut a INBOX: {em.get('subject', '')[:50]}", account=user)

        return jsonify({"ok": True, "message": f"Email rescatat i mogut a INBOX de {user}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/logs")
def logs():
    acc = request.args.get("account", "").strip() or None
    events = db.get_events(limit=200, account=acc)
    return render_template("logs.html", events=events, active_tab="logs")


@app.route("/api/stats")
def api_stats():
    acc = request.args.get("account", "").strip() or None
    return jsonify({
        "totals": db.get_totals(account=acc),
        "daily": db.get_daily_stats(days=30, account=acc),
        "accounts": db.get_account_totals(),
    })


@app.route("/api/health")
def api_health():
    """Health check per monitoritzar el servei."""
    try:
        sa = subprocess.run(["systemctl", "is-active", "imap-spam-filter"],
                            capture_output=True, text=True, timeout=5)
        bridge_status = sa.stdout.strip()
    except Exception:
        bridge_status = "unknown"

    try:
        import requests
        r = requests.get("http://localhost:11333/ping", timeout=3)
        rspamd_status = "active" if r.status_code == 200 else "error"
    except Exception:
        rspamd_status = "down"

    accounts = load_accounts()

    return jsonify({
        "bridge": bridge_status,
        "rspamd": rspamd_status,
        "accounts_count": len(accounts),
        "accounts": [a["user"] for a in accounts],
        "db_emails": db.get_totals(),
    })


@app.route("/metrics")
def prometheus_metrics():
    """Mètriques en format Prometheus per scrape amb suport per compte."""
    t = db.get_totals()
    scanned = t.get("scanned", 0)
    spam = t.get("spam", 0)
    ham = t.get("ham", 0)
    rescued = t.get("rescued", 0)
    learned_spam = t.get("learned_spam", 0)
    learned_ham = t.get("learned_ham", 0)

    lines = [
        "# HELP imap_spam_scanned_total Total correus escanejats des de les busties IMAP",
        "# TYPE imap_spam_scanned_total counter",
        f"imap_spam_scanned_total {scanned}",
        "# HELP imap_spam_spam_total Total correus filtrats com a spam",
        "# TYPE imap_spam_spam_total counter",
        f"imap_spam_spam_total {spam}",
        "# HELP imap_spam_ham_total Total correus legitims deixats a l inbox",
        "# TYPE imap_spam_ham_total counter",
        f"imap_spam_ham_total {ham}",
        "# HELP imap_spam_rescued_total Total correus rescatats de la carpeta SpamFilter",
        "# TYPE imap_spam_rescued_total counter",
        f"imap_spam_rescued_total {rescued}",
        "# HELP imap_spam_learned_total Total correus apresos pel filtre bayesia",
        "# TYPE imap_spam_learned_total counter",
        f"imap_spam_learned_total {learned_spam + learned_ham}",
    ]

    # Mètriques per compte
    acc_totals = db.get_account_totals()
    if acc_totals:
        lines.append("# HELP imap_spam_account_scanned_total Correus escanejats per bustia")
        lines.append("# TYPE imap_spam_account_scanned_total counter")
        for at in acc_totals:
            acc_name = at["account"]
            lines.append(f'imap_spam_account_scanned_total{{account="{acc_name}"}} {at["scanned"]}')

        lines.append("# HELP imap_spam_account_spam_total Correus spam per bustia")
        lines.append("# TYPE imap_spam_account_spam_total counter")
        for at in acc_totals:
            acc_name = at["account"]
            lines.append(f'imap_spam_account_spam_total{{account="{acc_name}"}} {at["spam"]}')

        lines.append("# HELP imap_spam_account_ham_total Correus legitims per bustia")
        lines.append("# TYPE imap_spam_account_ham_total counter")
        for at in acc_totals:
            acc_name = at["account"]
            lines.append(f'imap_spam_account_ham_total{{account="{acc_name}"}} {at["ham"]}')

        lines.append("# HELP imap_spam_account_rescued_total Correus rescatats per bustia")
        lines.append("# TYPE imap_spam_account_rescued_total counter")
        for at in acc_totals:
            acc_name = at["account"]
            lines.append(f'imap_spam_account_rescued_total{{account="{acc_name}"}} {at["rescued"]}')

    return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; version=0.0.4"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=DASH_PORT, debug=False)

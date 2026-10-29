#!/usr/bin/env python3
"""
IMAP Spam Filter Bridge v2.0 (Suport Multi-Compte Concurrent)
Connecta múltiples bústies IMAP d'un mateix domini amb Rspamd local de forma concurrent.
Filtra spam automàticament i entrena el filtre bayesià en ambdues direccions:
- Si mous un correu de SpamFilter a INBOX -> aprèn HAM automàticament.
- Si mous un correu a Spam -> aprèn SPAM automàticament (llegit o no llegit).
- Manté un registre a SQLite de missatges apresos per no repetir-los mai.
- Cada bústia s'executa en el seu propi fil mantenint la connexió IMAP IDLE.
"""

import os
import sys
import time
import signal
import socket
import sqlite3
import hashlib
import logging
import logging.handlers
import threading
import re
import ipaddress
import email
import email.header
import email.utils
import imaplib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

import requests
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

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "/opt/imap-spam-filter/data")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except (PermissionError, OSError):
    LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("bridge")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "bridge.log"),
    maxBytes=5_000_000,  # 5MB
    backupCount=3,
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# ──────────────────────────────────────────────
# Carregar configuració d'entorn
# ──────────────────────────────────────────────
ENV_PATH = os.environ.get("ENV_FILE", "/opt/imap-spam-filter/.env")

def reload_env():
    if os.path.exists(ENV_PATH):
        load_dotenv(ENV_PATH, override=True)
    elif os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

reload_env()

IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_SSL = os.environ.get("IMAP_SSL", "true").lower() == "true"

FOLDER_INBOX = os.environ.get("FOLDER_INBOX", "INBOX")
FOLDER_SPAM_DEST = os.environ.get("FOLDER_SPAM_DEST", "SpamFilter")
FOLDER_SPAM_MANUAL = os.environ.get("FOLDER_SPAM_MANUAL", "Spam")

RSPAMD_URL = os.environ.get("RSPAMD_URL", "http://localhost:11333")
RSPAMD_CONTROLLER_URL = os.environ.get("RSPAMD_CONTROLLER_URL", "http://localhost:11334")
SPAM_THRESHOLD = float(os.environ.get("SPAM_THRESHOLD", "6.0"))

DB_PATH = os.environ.get("DB_PATH", "/opt/imap-spam-filter/data/spam_filter.db")

IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "1740"))  # 29 min
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
LEARN_INTERVAL = int(os.environ.get("LEARN_INTERVAL", "3600"))  # 1 hora
MAX_EMAIL_SIZE = int(os.environ.get("MAX_EMAIL_SIZE", "524288"))  # 512KB


def load_accounts() -> List[Dict[str, str]]:
    """Carrega la llista de comptes configurats al fitxer d'entorn."""
    accounts = []
    idx = 1
    while True:
        user = os.environ.get(f"ACCOUNT_{idx}_USER", "").strip()
        pwd = os.environ.get(f"ACCOUNT_{idx}_PASS", "").strip()
        if not user:
            # Comprovem si hi ha algun forat a la numeració (fins a 4 posicions més enllà)
            has_more = any(os.environ.get(f"ACCOUNT_{n}_USER", "").strip() for n in range(idx + 1, idx + 5))
            if not has_more:
                break
            idx += 1
            continue
        if pwd:
            accounts.append({"user": user, "pass": pwd})
        idx += 1

    # Fallback per a retrocompatibilitat amb config antiga (IMAP_USER / IMAP_PASS)
    if not accounts:
        legacy_u = os.environ.get("IMAP_USER", "").strip()
        legacy_p = os.environ.get("IMAP_PASS", "").strip()
        if legacy_u and legacy_p:
            accounts.append({"user": legacy_u, "pass": legacy_p})

    return accounts


# ──────────────────────────────────────────────
# Base de dades SQLite (Thread-Safe + Multi-compte)
# ──────────────────────────────────────────────
class Database:
    def __init__(self, path: str):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except (PermissionError, OSError):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "spam_filter.db")
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        with self.lock:
            c = self._conn()
            try:
                # Comprovació i migració de la taula 'emails' si no té la columna 'account'
                t_emails = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='emails'").fetchone()
                if t_emails:
                    cols = [row["name"] for row in c.execute("PRAGMA table_info(emails)").fetchall()]
                    if "account" not in cols:
                        logger.info("Migrant taula 'emails' a suport multi-compte...")
                        legacy_acc = ""
                        accs = load_accounts()
                        if accs:
                            legacy_acc = accs[0]["user"]

                        c.execute("""
                            CREATE TABLE emails_new (
                                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                                account     TEXT DEFAULT '',
                                message_id  TEXT,
                                uid         TEXT,
                                subject     TEXT,
                                sender      TEXT,
                                recipient   TEXT,
                                date_recv   TEXT,
                                date_scan   TEXT,
                                score       REAL,
                                threshold   REAL,
                                is_spam     INTEGER,
                                action      TEXT,
                                status      TEXT DEFAULT 'active',
                                report      TEXT,
                                size_bytes  INTEGER
                            )
                        """)
                        c.execute("""
                            INSERT INTO emails_new 
                            (id, account, message_id, uid, subject, sender, recipient, date_recv, date_scan, score, threshold, is_spam, action, status, report, size_bytes)
                            SELECT id, ?, message_id, uid, subject, sender, recipient, date_recv, date_scan, score, threshold, is_spam, action, status, report, size_bytes
                            FROM emails
                        """, (legacy_acc,))
                        c.execute("DROP TABLE emails")
                        c.execute("ALTER TABLE emails_new RENAME TO emails")
                        logger.info("Migració de 'emails' completada.")

                # Comprovació i migració de 'daily_stats'
                t_stats = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_stats'").fetchone()
                if t_stats:
                    cols = [row["name"] for row in c.execute("PRAGMA table_info(daily_stats)").fetchall()]
                    if "account" not in cols:
                        logger.info("Migrant taula 'daily_stats' a suport multi-compte...")
                        legacy_acc = ""
                        accs = load_accounts()
                        if accs:
                            legacy_acc = accs[0]["user"]

                        c.execute("""
                            CREATE TABLE daily_stats_new (
                                date            TEXT,
                                account         TEXT DEFAULT '',
                                scanned         INTEGER DEFAULT 0,
                                spam            INTEGER DEFAULT 0,
                                ham             INTEGER DEFAULT 0,
                                rescued         INTEGER DEFAULT 0,
                                reported        INTEGER DEFAULT 0,
                                learned_spam    INTEGER DEFAULT 0,
                                learned_ham     INTEGER DEFAULT 0,
                                PRIMARY KEY (date, account)
                            )
                        """)
                        c.execute("""
                            INSERT INTO daily_stats_new
                            (date, account, scanned, spam, ham, rescued, reported, learned_spam, learned_ham)
                            SELECT date, ?, scanned, spam, ham, rescued, 0, learned_spam, learned_ham
                            FROM daily_stats
                        """, (legacy_acc,))
                        c.execute("DROP TABLE daily_stats")
                        c.execute("ALTER TABLE daily_stats_new RENAME TO daily_stats")
                        logger.info("Migració de 'daily_stats' completada.")
                    elif "reported" not in cols:
                        c.execute("ALTER TABLE daily_stats ADD COLUMN reported INTEGER DEFAULT 0")
                        c.execute("""
                            UPDATE daily_stats
                            SET reported = (
                                SELECT COUNT(*)
                                FROM emails
                                WHERE emails.account = daily_stats.account
                                  AND emails.status = 'reported'
                            )
                        """)
                        logger.info("Columna 'reported' afegida i sincronitzada a 'daily_stats'.")

                # Comprovació i addició de columna account a 'events' i 'learned_emails'
                t_events = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
                if t_events:
                    cols = [row["name"] for row in c.execute("PRAGMA table_info(events)").fetchall()]
                    if "account" not in cols:
                        c.execute("ALTER TABLE events ADD COLUMN account TEXT DEFAULT ''")

                t_learned = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learned_emails'").fetchone()
                if t_learned:
                    cols = [row["name"] for row in c.execute("PRAGMA table_info(learned_emails)").fetchall()]
                    if "account" not in cols:
                        c.execute("ALTER TABLE learned_emails ADD COLUMN account TEXT DEFAULT ''")

                # Creació de les taules i índexs si no existeixen
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS emails (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        account     TEXT DEFAULT '',
                        message_id  TEXT,
                        uid         TEXT,
                        subject     TEXT,
                        sender      TEXT,
                        recipient   TEXT,
                        date_recv   TEXT,
                        date_scan   TEXT,
                        score       REAL,
                        threshold   REAL,
                        is_spam     INTEGER,
                        action      TEXT,
                        status      TEXT DEFAULT 'active',
                        report      TEXT,
                        size_bytes  INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS daily_stats (
                        date            TEXT,
                        account         TEXT DEFAULT '',
                        scanned         INTEGER DEFAULT 0,
                        spam            INTEGER DEFAULT 0,
                        ham             INTEGER DEFAULT 0,
                        rescued         INTEGER DEFAULT 0,
                        reported        INTEGER DEFAULT 0,
                        learned_spam    INTEGER DEFAULT 0,
                        learned_ham     INTEGER DEFAULT 0,
                        PRIMARY KEY (date, account)
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        account   TEXT DEFAULT '',
                        ts        TEXT,
                        level     TEXT,
                        message   TEXT
                    );
                    CREATE TABLE IF NOT EXISTS learned_emails (
                        message_id  TEXT PRIMARY KEY,
                        learn_type  TEXT,
                        ts          TEXT,
                        account     TEXT DEFAULT ''
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_acc_msgid ON emails(account, message_id);
                    CREATE INDEX IF NOT EXISTS idx_emails_acc ON emails(account);
                    CREATE INDEX IF NOT EXISTS idx_emails_scan ON emails(date_scan);
                    CREATE INDEX IF NOT EXISTS idx_emails_spam ON emails(is_spam);
                    CREATE INDEX IF NOT EXISTS idx_emails_msgid ON emails(message_id);
                """)
                c.commit()
            finally:
                c.close()

    def log_email(self, account: str, **kw):
        with self.lock:
            c = self._conn()
            try:
                kw["account"] = account
                c.execute("""
                    INSERT OR IGNORE INTO emails
                    (account, message_id, uid, subject, sender, recipient, date_recv,
                     date_scan, score, threshold, is_spam, action, status, report, size_bytes)
                    VALUES (:account, :message_id, :uid, :subject, :sender, :recipient,
                            :date_recv, :date_scan, :score, :threshold,
                            :is_spam, :action, :status, :report, :size_bytes)
                """, kw)
                c.commit()
            finally:
                c.close()

    def get_email_by_msgid(self, account: str, message_id: str) -> Optional[dict]:
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM emails WHERE account=? AND message_id=?", (account, message_id)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def is_learned(self, message_id: str) -> bool:
        c = self._conn()
        try:
            r = c.execute("SELECT 1 FROM learned_emails WHERE message_id=?", (message_id,)).fetchone()
            return r is not None
        finally:
            c.close()

    def mark_learned(self, message_id: str, learn_type: str, account: str = ""):
        with self.lock:
            c = self._conn()
            try:
                c.execute("INSERT OR IGNORE INTO learned_emails (message_id, learn_type, ts, account) VALUES (?, ?, ?, ?)",
                          (message_id, learn_type, datetime.now().isoformat(), account))
                c.commit()
            finally:
                c.close()

    def bump_stats(self, account: str = "", spam=0, ham=0, rescued=0, reported=0, learned_spam=0, learned_ham=0):
        today = datetime.now().strftime("%Y-%m-%d")
        with self.lock:
            c = self._conn()
            try:
                c.execute("""
                    INSERT INTO daily_stats (date, account, scanned, spam, ham, rescued, reported, learned_spam, learned_ham)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, account) DO UPDATE SET
                        scanned = scanned + excluded.scanned,
                        spam = spam + excluded.spam,
                        ham = ham + excluded.ham,
                        rescued = rescued + excluded.rescued,
                        reported = reported + excluded.reported,
                        learned_spam = learned_spam + excluded.learned_spam,
                        learned_ham = learned_ham + excluded.learned_ham
                """, (today, account, spam + ham, spam, ham, rescued, reported, learned_spam, learned_ham))
                c.commit()
            finally:
                c.close()

    def event(self, level: str, msg: str, account: str = ""):
        with self.lock:
            c = self._conn()
            try:
                c.execute("INSERT INTO events (ts, level, message, account) VALUES (?, ?, ?, ?)",
                          (datetime.now().isoformat(), level, msg, account))
                c.commit()
            finally:
                c.close()

    def update_status(self, message_id: str, status: str, action: str, account: str = ""):
        with self.lock:
            c = self._conn()
            try:
                if account:
                    c.execute("UPDATE emails SET status=?, action=? WHERE message_id=? AND account=?",
                              (status, action, message_id, account))
                else:
                    c.execute("UPDATE emails SET status=?, action=? WHERE message_id=?",
                              (status, action, message_id))
                c.commit()
            finally:
                c.close()

    def get_totals(self, account: Optional[str] = None) -> dict:
        c = self._conn()
        try:
            if account:
                r = c.execute("""
                    SELECT COALESCE(SUM(scanned),0) as scanned,
                           COALESCE(SUM(spam),0) as spam,
                           COALESCE(SUM(ham),0) as ham,
                           COALESCE(SUM(rescued),0) as rescued,
                           COALESCE(SUM(reported),0) as reported,
                           COALESCE(SUM(learned_spam),0) as learned_spam,
                           COALESCE(SUM(learned_ham),0) as learned_ham
                    FROM daily_stats
                    WHERE account = ?
                """, (account,)).fetchone()
            else:
                r = c.execute("""
                    SELECT COALESCE(SUM(scanned),0) as scanned,
                           COALESCE(SUM(spam),0) as spam,
                           COALESCE(SUM(ham),0) as ham,
                           COALESCE(SUM(rescued),0) as rescued,
                           COALESCE(SUM(reported),0) as reported,
                           COALESCE(SUM(learned_spam),0) as learned_spam,
                           COALESCE(SUM(learned_ham),0) as learned_ham
                    FROM daily_stats
                """).fetchone()
            return dict(r) if r else {}
        finally:
            c.close()

    def get_account_totals(self) -> List[dict]:
        c = self._conn()
        try:
            rows = c.execute("""
                SELECT account,
                       COALESCE(SUM(scanned),0) as scanned,
                       COALESCE(SUM(spam),0) as spam,
                       COALESCE(SUM(ham),0) as ham,
                       COALESCE(SUM(rescued),0) as rescued,
                       COALESCE(SUM(reported),0) as reported,
                       COALESCE(SUM(learned_spam),0) as learned_spam,
                       COALESCE(SUM(learned_ham),0) as learned_ham
                FROM daily_stats
                WHERE account != ''
                GROUP BY account
                ORDER BY account
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def get_configured_or_seen_accounts(self) -> List[str]:
        configured = [a["user"] for a in load_accounts()]
        c = self._conn()
        try:
            seen_stats = [r[0] for r in c.execute("SELECT DISTINCT account FROM daily_stats WHERE account != ''").fetchall()]
            seen_emails = [r[0] for r in c.execute("SELECT DISTINCT account FROM emails WHERE account != ''").fetchall()]
            all_accs = sorted(list(set(configured + seen_stats + seen_emails)))
            return all_accs
        finally:
            c.close()

    def get_daily_stats(self, days=30, account: Optional[str] = None) -> List[dict]:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        c = self._conn()
        try:
            if account:
                rows = c.execute("""
                    SELECT date, scanned, spam, ham, rescued,
                           reported, learned_spam, learned_ham
                    FROM daily_stats
                    WHERE date >= ? AND account = ?
                    ORDER BY date
                """, (since, account)).fetchall()
            else:
                rows = c.execute("""
                    SELECT date,
                           COALESCE(SUM(scanned),0) as scanned,
                           COALESCE(SUM(spam),0) as spam,
                           COALESCE(SUM(ham),0) as ham,
                           COALESCE(SUM(rescued),0) as rescued,
                           COALESCE(SUM(reported),0) as reported,
                           COALESCE(SUM(learned_spam),0) as learned_spam,
                           COALESCE(SUM(learned_ham),0) as learned_ham
                    FROM daily_stats
                    WHERE date >= ?
                    GROUP BY date
                    ORDER BY date
                """, (since,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def get_emails(self, filter_type="all", account: Optional[str] = None, limit=100, offset=0) -> List[dict]:
        clauses = []
        params = []
        if filter_type == "spam":
            clauses.append("is_spam=1 AND status='active'")
        elif filter_type == "ham":
            clauses.append("is_spam=0 AND status!='reported'")
        elif filter_type == "rescued":
            clauses.append("status='rescued'")
        elif filter_type == "reported":
            clauses.append("status='reported'")

        if account:
            clauses.append("account=?")
            params.append(account)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])

        c = self._conn()
        try:
            rows = c.execute(
                f"SELECT * FROM emails {where} ORDER BY date_scan DESC LIMIT ? OFFSET ?",
                tuple(params)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def get_email(self, eid: int) -> Optional[dict]:
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def get_events(self, limit=200, account: Optional[str] = None) -> List[dict]:
        c = self._conn()
        try:
            if account:
                rows = c.execute(
                    "SELECT * FROM events WHERE account=? ORDER BY id DESC LIMIT ?",
                    (account, limit)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


# ──────────────────────────────────────────────
# Helpers email
# ──────────────────────────────────────────────
def decode_hdr(value) -> str:
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
        out = []
        for data, charset in parts:
            if isinstance(data, bytes):
                out.append(data.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(str(data))
        return " ".join(out)
    except Exception:
        return str(value)


def email_info(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw)
    mid = msg.get("Message-ID") or f"<gen-{hashlib.md5(raw[:500]).hexdigest()}@local>"
    return {
        "message_id": mid.strip(),
        "subject": decode_hdr(msg.get("Subject", "(sense assumpte)")),
        "from": decode_hdr(msg.get("From", "")),
        "to": decode_hdr(msg.get("To", "")),
        "date": msg.get("Date", ""),
        "size": len(raw),
    }


# ──────────────────────────────────────────────
# Rspamd client & Extracció de Context SMTP
# ──────────────────────────────────────────────
def extract_sender_context(raw: bytes) -> Tuple[Optional[str], Optional[str]]:
    """
    Extreu la IP pública del client remitent i el Return-Path / Envelope From
    a partir del correu en brut i la cadena de capçaleres Received.
    """
    try:
        msg = email.message_from_bytes(raw)

        # 1. Envelope From / Return-Path
        rp = msg.get("Return-Path", "")
        from_hdr = msg.get("From", "")
        env_from = email.utils.parseaddr(rp)[1] or email.utils.parseaddr(from_hdr)[1]
        if not env_from:
            env_from = None

        # 2. IP d'origen a partir de les capçaleres Received
        received = msg.get_all("Received", [])
        client_ip = None
        ip_regex = re.compile(r"\[([0-9a-fA-F:\.]+)\]")

        for h in received:
            matches = ip_regex.findall(h)
            for m in matches:
                clean_m = m.lower().replace("ipv6:", "").strip()
                try:
                    ip = ipaddress.ip_address(clean_m)
                    if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                        continue
                    h_lower = h.lower()
                    if "entorno.es" in h_lower and ("from se01.entorno.es" in h_lower or ip == ipaddress.ip_address("194.36.122.200")):
                        continue
                    client_ip = str(ip)
                    break
                except ValueError:
                    continue
            if client_ip:
                break

        return client_ip, env_from
    except Exception as e:
        logger.warning(f"Error extraient context de xarxa de l'email: {e}")
        return None, None


def rspamd_check(raw: bytes, recipient: str = "") -> dict:
    """Envia email a Rspamd amb context SMTP real (IP i From) i retorna score + report."""
    headers = {"Pass": "all"}
    if recipient:
        headers["Deliver-To"] = recipient
        headers["Rcpt"] = recipient

    client_ip, env_from = extract_sender_context(raw)
    if client_ip:
        headers["Ip"] = client_ip
    if env_from:
        headers["From"] = env_from

    try:
        r = requests.post(
            f"{RSPAMD_URL}/checkv2",
            data=raw,
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        score = data.get("score", 0.0)
        symbols = data.get("symbols", {})
        report_lines = []
        for name, info in sorted(symbols.items(), key=lambda x: abs(x[1].get("score", 0)), reverse=True):
            s = info.get("score", 0)
            desc = info.get("description", "")
            if s != 0:
                report_lines.append(f"{s:+6.1f}  {name}: {desc}")
        report = "\n".join(report_lines[:30])
        return {
            "score": score,
            "is_spam": score >= SPAM_THRESHOLD,
            "report": report,
            "client_ip": client_ip,
            "env_from": env_from,
        }
    except Exception as e:
        logger.error(f"Rspamd error: {e}")
        return {"score": 0.0, "is_spam": False, "report": f"ERROR: {e}", "client_ip": None, "env_from": None}


def rspamd_learn(raw: bytes, spam: bool, recipient: str = "") -> bool:
    """Entrena Rspamd amb un email (spam o ham) al port del controller (11334)."""
    endpoint = "learnspam" if spam else "learnham"
    headers = {}
    if recipient:
        headers["Deliver-To"] = recipient
        headers["Rcpt"] = recipient

    try:
        r = requests.post(f"{RSPAMD_CONTROLLER_URL}/{endpoint}", data=raw, headers=headers, timeout=30)
        if r.status_code in [200, 204, 208]:
            logger.info(f"Rspamd learn {'spam' if spam else 'ham'}{f' [{recipient}]' if recipient else ''}: OK (HTTP {r.status_code})")
            return True
        else:
            logger.error(f"Rspamd learn returned HTTP {r.status_code}: {r.text}")
    except Exception as e:
        logger.error(f"Rspamd learn error: {e}")
    return False


# ──────────────────────────────────────────────
# Worker per bústia d'usuari (IMAP IDLE concurrent)
# ──────────────────────────────────────────────
class AccountWorker(threading.Thread):
    def __init__(self, account_config: dict, db: Database, stop_event: threading.Event):
        super().__init__(name=f"Worker-{account_config['user']}")
        self.user = account_config["user"]
        self.password = account_config["pass"]
        self.db = db
        self.stop_event = stop_event
        self.imap: Optional[imaplib.IMAP4] = None
        self.dest_folder = FOLDER_SPAM_DEST
        self.manual_folders = [FOLDER_SPAM_MANUAL]
        self.daemon = True

    def stop(self):
        """Interromp qualsevol lectura de socket pendent."""
        try:
            if self.imap and hasattr(self.imap, "sock") and self.imap.sock:
                self.imap.sock.shutdown(socket.SHUT_RDWR)
                self.imap.sock.close()
        except Exception:
            pass

    def _get_available_folders(self) -> List[str]:
        try:
            typ, folder_list = self.imap.list()
            if typ == "OK":
                folders = []
                for f in folder_list:
                    txt = f.decode() if isinstance(f, bytes) else str(f)
                    for delim in ['"." ', '"/" ', ' "." ', ' "/" ']:
                        if delim in txt:
                            folders.append(txt.split(delim)[-1].strip().strip('"'))
                            break
                    else:
                        folders.append(txt.split()[-1].strip().strip('"'))
                return folders
        except Exception as e:
            logger.warning(f"[{self.user}] Error llistant carpetes: {e}")
        return []

    def _resolve_or_create(self, folder_name: str) -> str:
        available = self._get_available_folders()

        if folder_name in available:
            return folder_name
        if f"INBOX.{folder_name}" in available:
            return f"INBOX.{folder_name}"

        has_inbox_prefix = any(f.startswith("INBOX.") for f in available)
        target = f"INBOX.{folder_name}" if (has_inbox_prefix and not folder_name.startswith("INBOX.")) else folder_name

        try:
            self.imap.create(f'"{target}"')
            self.imap.subscribe(f'"{target}"')
            logger.info(f"[{self.user}] 📁 Carpeta '{target}' creada i subscrita")
            return target
        except Exception as e:
            logger.debug(f"[{self.user}] Info creació carpeta '{target}': {e}")
            return target

    def _connect(self) -> imaplib.IMAP4:
        logger.info(f"[{self.user}] Connectant a {IMAP_HOST}:{IMAP_PORT}...")
        if IMAP_SSL:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        else:
            imap = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)

        imap.login(self.user, self.password)
        logger.info(f"[{self.user}] ✅ Connectat correctament")
        self.db.event("INFO", f"Connexió IMAP establerta", account=self.user)
        self.imap = imap

        # Assegurar carpeta de destinació per a l'spam
        self.dest_folder = self._resolve_or_create(FOLDER_SPAM_DEST)

        # Carpetes d'aprenentatge (on l'usuari mou spam manualment)
        manual_resolved = self._resolve_or_create(FOLDER_SPAM_MANUAL)
        avail = self._get_available_folders()
        self.manual_folders = [manual_resolved]
        for extra in ["INBOX.Spam", "Spam", "INBOX.Junk", "Junk"]:
            if extra in avail and extra not in self.manual_folders:
                self.manual_folders.append(extra)

        logger.info(f"[{self.user}] 📂 Carpeta destí spam: '{self.dest_folder}'")
        logger.info(f"[{self.user}] 📚 Carpetes aprenentatge spam: {self.manual_folders}")
        return imap

    def _ensure_conn(self):
        try:
            if self.imap:
                self.imap.noop()
                return
        except Exception:
            logger.warning(f"[{self.user}] Connexió perduda, reconnectant...")

        for attempt in range(1, 11):
            if self.stop_event.is_set():
                return
            try:
                self._connect()
                return
            except Exception as e:
                wait = min(30 * attempt, 300)
                logger.error(f"[{self.user}] Connexió fallida (intent {attempt}/10): {e}. Esperant {wait}s...")
                self.db.event("ERROR", f"Connexió fallida: {e}", account=self.user)
                self.stop_event.wait(wait)

        logger.critical(f"[{self.user}] No es pot connectar al servidor IMAP")
        self.db.event("CRITICAL", "Impossible reconnectar després de 10 intents", account=self.user)
        self.stop_event.wait(60)

    def _move_email(self, uid_str: str, dest_folder: str) -> bool:
        """Mou un correu per UID provant MOVE o COPY+DELETE."""
        try:
            typ, _ = self.imap.uid("move", uid_str, f'"{dest_folder}"')
            if typ == "OK":
                return True
        except Exception:
            pass

        try:
            typ, _ = self.imap.uid("copy", uid_str, f'"{dest_folder}"')
            if typ == "OK":
                self.imap.uid("store", uid_str, "+FLAGS", "(\\Deleted)")
                self.imap.expunge()
                return True
        except Exception as e:
            logger.error(f"[{self.user}] Error movent correu UID {uid_str}: {e}")
        return False

    def _scan_inbox(self) -> int:
        if not self.imap:
            return 0

        try:
            self.imap.select(f'"{FOLDER_INBOX}"')
            typ, data = self.imap.uid("search", None, "UNSEEN")
            if typ != "OK" or not data[0]:
                return 0

            uids = data[0].split()[:50]
            spam_n = ham_n = 0

            for uid_bytes in uids:
                if self.stop_event.is_set():
                    break
                uid = uid_bytes.decode() if isinstance(uid_bytes, bytes) else str(uid_bytes)
                try:
                    typ, fetch_data = self.imap.uid("fetch", uid, "(BODY.PEEK[])")
                    if typ != "OK" or not fetch_data or not fetch_data[0]:
                        continue

                    raw = fetch_data[0][1]
                    if not isinstance(raw, bytes):
                        continue

                    info = email_info(raw)

                    # Comprovar si aquest compte ja havia vist aquest email abans
                    prev = self.db.get_email_by_msgid(self.user, info["message_id"])
                    if prev:
                        # Si l'usuari l'ha mogut de SpamFilter a INBOX manualment
                        if prev["is_spam"] == 1 and prev["status"] == "active":
                            logger.info(f"[{self.user}] 🔄 Rescat manual detectat a l'INBOX: {info['subject'][:50]}")
                            if rspamd_learn(raw, spam=False, recipient=self.user):
                                self.db.mark_learned(info["message_id"], "ham", account=self.user)
                                self.db.update_status(info["message_id"], "rescued", "rescued_manually", account=self.user)
                                self.db.bump_stats(account=self.user, rescued=1, learned_ham=1)
                                self.db.event("INFO", f"Rescatat manualment a l'INBOX: {info['subject'][:50]}", account=self.user)
                        continue

                    if len(raw) > MAX_EMAIL_SIZE:
                        logger.info(f"[{self.user}] ⏭️  Email massa gran ({len(raw)} bytes), omès")
                        self.db.log_email(
                            account=self.user,
                            message_id=info["message_id"], uid=uid,
                            subject=info["subject"][:200], sender=info["from"][:200],
                            recipient=info["to"][:200], date_recv=info["date"],
                            date_scan=datetime.now().isoformat(),
                            score=0.0, threshold=SPAM_THRESHOLD, is_spam=0,
                            action="skipped_too_large", status="active",
                            report="", size_bytes=len(raw),
                        )
                        ham_n += 1
                        continue

                    result = rspamd_check(raw, recipient=self.user)
                    is_spam = result["is_spam"]
                    action = "kept_in_inbox"

                    cip_str = f" [IP: {result.get('client_ip')}]" if result.get("client_ip") else ""
                    if is_spam:
                        if self._move_email(uid, self.dest_folder):
                            action = "moved_to_spam"
                            spam_n += 1
                            logger.info(
                                f"[{self.user}] 🚫 SPAM [{result['score']:.1f}]{cip_str} "
                                f"De: {info['from'][:50]} | {info['subject'][:60]}"
                            )
                        else:
                            action = "move_failed"
                            logger.error(f"[{self.user}] Error movent correu spam a {self.dest_folder}")
                    else:
                        ham_n += 1
                        logger.info(
                            f"[{self.user}] ✅ OK [{result['score']:.1f}]{cip_str} "
                            f"De: {info['from'][:50]} | {info['subject'][:60]}"
                        )

                    self.db.log_email(
                        account=self.user,
                        message_id=info["message_id"], uid=uid,
                        subject=info["subject"][:200], sender=info["from"][:200],
                        recipient=info["to"][:200], date_recv=info["date"],
                        date_scan=datetime.now().isoformat(),
                        score=result["score"], threshold=SPAM_THRESHOLD,
                        is_spam=1 if is_spam else 0, action=action, status="active",
                        report=result["report"][:2000], size_bytes=info["size"],
                    )

                except Exception as e:
                    logger.error(f"[{self.user}] Error processant UID {uid}: {e}")
                    self.db.event("ERROR", f"Error UID {uid}: {e}", account=self.user)

            if spam_n or ham_n:
                self.db.bump_stats(account=self.user, spam=spam_n, ham=ham_n)
                self.db.event("INFO", f"Escanejats: {spam_n} spam + {ham_n} legítims", account=self.user)

            return spam_n + ham_n

        except Exception as e:
            logger.error(f"[{self.user}] Error a _scan_inbox: {e}")
            return 0

    def _learn_from_manual_spam(self):
        """Aprèn dels correus que l'usuari mou manualment a carpetes de Spam."""
        if not self.imap:
            return
        learned = 0
        reported = 0

        for folder in self.manual_folders:
            try:
                typ, _ = self.imap.select(f'"{folder}"')
                if typ != "OK":
                    continue

                typ, data = self.imap.uid("search", None, "UNSEEN")
                uids = data[0].split() if (typ == "OK" and data[0]) else []

                if not uids:
                    typ_all, data_all = self.imap.uid("search", None, "ALL")
                    if typ_all == "OK" and data_all[0]:
                        uids = data_all[0].split()[-50:]

                for uid_bytes in uids:
                    if self.stop_event.is_set():
                        break
                    uid = uid_bytes.decode() if isinstance(uid_bytes, bytes) else str(uid_bytes)
                    try:
                        typ, fetch_data = self.imap.uid("fetch", uid, "(BODY.PEEK[])")
                        if typ == "OK" and fetch_data and fetch_data[0]:
                            raw = fetch_data[0][1]
                            if not isinstance(raw, bytes):
                                continue
                            info = email_info(raw)

                            # Ja s'havia après aquest missatge prèviament? (Bayes compartit)
                            if self.db.is_learned(info["message_id"]):
                                continue

                            if rspamd_learn(raw, spam=True, recipient=self.user):
                                self.db.mark_learned(info["message_id"], "spam", account=self.user)
                                self.imap.uid("store", uid, "+FLAGS", "(\\Seen)")
                                learned += 1

                                prev = self.db.get_email_by_msgid(self.user, info["message_id"])
                                if prev:
                                    if prev.get("is_spam") == 0:
                                        # Era un correu legítim que el filtre no va detectar: FALS NEGATIU (Reportat)
                                        self.db.update_status(info["message_id"], "reported", "reported_manually", account=self.user)
                                        reported += 1
                                        logger.info(f"[{self.user}] 📚 Fals negatiu reportat com a SPAM ({folder}): {info['from'][:40]} | {info['subject'][:50]}")
                                    else:
                                        logger.info(f"[{self.user}] 📚 Re-après SPAM existent ({folder}): {info['from'][:40]} | {info['subject'][:50]}")
                                else:
                                    # Correu històric (ex: des de Paperera/Spam per entrenar Bayes)
                                    logger.info(f"[{self.user}] 📚 Entrenament SPAM històric ({folder}): {info['from'][:40]} | {info['subject'][:50]}")
                    except Exception as e:
                        logger.error(f"[{self.user}] Error aprenent UID {uid} a {folder}: {e}")
            except Exception as e:
                logger.debug(f"[{self.user}] Carpeta {folder} no accessible: {e}")

        if learned or reported:
            self.db.bump_stats(account=self.user, reported=reported, learned_spam=learned)
            if reported:
                self.db.event("INFO", f"Reportats: {reported} falsos negatius apresos com a spam", account=self.user)
            if learned - reported > 0:
                self.db.event("INFO", f"Entrenament Bayes: {learned - reported} correus històrics apresos com a spam", account=self.user)

    def run(self):
        logger.info(f"[{self.user}] Fil de monitorització IMAP iniciat")
        last_learn = 0

        while not self.stop_event.is_set():
            try:
                self._ensure_conn()
                if self.stop_event.is_set():
                    break

                self._scan_inbox()

                # Entrenament bayesià periòdic
                now = time.time()
                if now - last_learn > LEARN_INTERVAL:
                    self._learn_from_manual_spam()
                    last_learn = now

                # IMAP IDLE
                self.imap.select(f'"{FOLDER_INBOX}"')
                tag = self.imap._new_tag()
                self.imap.send(b"%s IDLE\r\n" % tag)
                resp = self.imap.readline()
                if b"+" in resp:
                    self.imap.sock.settimeout(IDLE_TIMEOUT)
                    try:
                        line = self.imap.readline()
                        if b"EXISTS" in line:
                            logger.info(f"[{self.user}] 📬 Correu nou detectat via IDLE!")
                    except socket.timeout:
                        pass
                    except Exception as e:
                        if not self.stop_event.is_set():
                            logger.debug(f"[{self.user}] IDLE interrupted: {e}")

                    try:
                        self.imap.send(b"DONE\r\n")
                        self.imap.readline()
                    except Exception:
                        pass
                    try:
                        self.imap.sock.settimeout(None)
                    except Exception:
                        pass
                else:
                    self.stop_event.wait(POLL_INTERVAL)

            except Exception as e:
                if not self.stop_event.is_set():
                    logger.error(f"[{self.user}] Error al bucle worker: {e}")
                    self.db.event("ERROR", f"Error worker: {e}", account=self.user)
                    self.imap = None
                    self.stop_event.wait(10)

        # Finalització neta
        try:
            if self.imap:
                self.imap.logout()
        except Exception:
            pass
        logger.info(f"[{self.user}] Fil aturat correctament.")


# ──────────────────────────────────────────────
# IMAP Bridge Principal (Orquestrador Multi-Compte)
# ──────────────────────────────────────────────
class IMAPBridge:
    def __init__(self):
        self.db = Database(DB_PATH)
        self.stop_event = threading.Event()
        self.workers: List[AccountWorker] = []
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

    def _stop(self, *_):
        logger.info("Senyal d'aturada rebut. Aturant tots els comptes...")
        self.stop_event.set()
        for w in self.workers:
            w.stop()

    def run(self):
        logger.info("=" * 60)
        logger.info("  🛡️  IMAP Spam Filter Bridge v2.0 (Multi-Compte)")
        logger.info("=" * 60)

        accounts = load_accounts()
        while not accounts and not self.stop_event.is_set():
            logger.warning("=" * 60)
            logger.warning("⚠️  CONFIGURACIÓ PENDENT:")
            logger.warning("Edita el fitxer /opt/imap-spam-filter/.env amb les credencials dels comptes:")
            logger.warning("ACCOUNT_1_USER=..., ACCOUNT_1_PASS=..., ACCOUNT_2_USER=..., etc.")
            logger.warning("=" * 60)
            self.db.event("WARNING", "Falta configurar credencials de comptes a .env")
            self.stop_event.wait(20)
            reload_env()
            accounts = load_accounts()

        if self.stop_event.is_set():
            return

        acc_names = [a["user"] for a in accounts]
        logger.info(f"Iniciant supervisió concurrent per a {len(accounts)} comptes: {', '.join(acc_names)}")
        self.db.event("INFO", f"Servei iniciat amb {len(accounts)} comptes: {', '.join(acc_names)}")

        for acc in accounts:
            w = AccountWorker(acc, self.db, self.stop_event)
            w.start()
            self.workers.append(w)

        # Bucle de supervisió del procés principal
        while not self.stop_event.is_set():
            for i, w in enumerate(list(self.workers)):
                if not w.is_alive() and not self.stop_event.is_set():
                    logger.warning(f"Worker [{w.user}] ha finalitzat inesperadament. Reiniciant fil...")
                    self.db.event("WARNING", f"Reiniciant worker", account=w.user)
                    new_w = AccountWorker({"user": w.user, "pass": w.password}, self.db, self.stop_event)
                    new_w.start()
                    self.workers[i] = new_w
            self.stop_event.wait(5)

        for w in self.workers:
            w.join(timeout=10)

        self.db.event("INFO", "Servei aturat")
        logger.info("Servei aturat completament")


if __name__ == "__main__":
    IMAPBridge().run()

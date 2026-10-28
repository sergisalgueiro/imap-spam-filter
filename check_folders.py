#!/usr/bin/env python3
"""
Utilitat per comprovar connexió i llistar carpetes de tots els comptes IMAP configurats a .env.
"""

import os
import sys
import imaplib
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

env_file = os.environ.get("ENV_FILE", "/opt/imap-spam-filter/.env")
if os.path.exists(env_file):
    load_dotenv(env_file)
elif os.path.exists(".env"):
    load_dotenv(".env")

from imap_bridge import load_accounts

host = os.environ.get("IMAP_HOST", "")
port = int(os.environ.get("IMAP_PORT", 993))
use_ssl = os.environ.get("IMAP_SSL", "true").lower() == "true"

accounts = load_accounts()

if not accounts:
    print("❌ No s'ha trobat cap compte configurat a .env (ACCOUNT_1_USER/PASS o IMAP_USER/PASS)")
    sys.exit(1)

print(f"🔍 Comprovant {len(accounts)} compte(s) a {host}:{port} (SSL: {use_ssl})...\n")

for i, acc in enumerate(accounts, 1):
    user = acc["user"]
    pwd = acc["pass"]
    print(f"[{i}/{len(accounts)}] 👤 Compte: {user}")
    try:
        if use_ssl:
            c = imaplib.IMAP4_SSL(host, port)
        else:
            c = imaplib.IMAP4(host, port)
        c.login(user, pwd)
        print("  ✅ Login correcte!")

        typ, folder_list = c.list()
        if typ == "OK":
            for f in folder_list:
                txt = f.decode() if isinstance(f, bytes) else str(f)
                for delim in ['"." ', '"/" ', ' "." ', ' "/" ']:
                    if delim in txt:
                        folder = txt.split(delim)[-1].strip().strip('"')
                        break
                else:
                    folder = txt.split()[-1].strip().strip('"')

                try:
                    c.select(f'"{folder}"')
                    _, all_uids = c.uid('search', None, 'ALL')
                    tot = len(all_uids[0].split()) if all_uids and all_uids[0] else 0
                    _, uns = c.uid('search', None, 'UNSEEN')
                    unseen = len(uns[0].split()) if uns and uns[0] else 0
                    print(f"    - {folder:25}: {tot:4d} totals ({unseen:2d} no llegits)")
                except Exception as fe:
                    print(f"    - {folder:25}: (no seleccionable: {fe})")

        c.logout()
    except Exception as e:
        print(f"  ❌ Error connectant amb {user}: {e}")
    print("-" * 50)

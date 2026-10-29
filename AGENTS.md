# 🤖 Instruccions per a Agents d'IA (AGENTS.md)

Aquest fitxer conté les directrius i l'arquitectura del projecte per a assistents d'IA (Antigravity, Cursor, etc.).

---

## 📌 Regla Primordial: Configuració Local i Desplegament Privat
> [!IMPORTANT]
> **Si existeix el fitxer `AGENTS.local.md` a l'arrel d'aquest repositori, l'agent l'ha de llegir obligatòriament a l'inici de la sessió.**
> Aquest fitxer conté el flux de desplegament en calent, connexions remotes (SSH), serveis systemd i integració amb infraestructura privada (com Grafana o Prometheus) que no s'han de commetre al repositori públic.

---

## 🏛️ Descripció del Projecte
**IMAP Spam Filter Bridge** és un servei antispam ultralleuger basat en Python, Rspamd i Redis per a múltiples bústies IMAP concurrents mitjançant `IMAP IDLE`.

* **Filtre principal ([imap_bridge.py](imap_bridge.py))**:
  * Fils independents per compte (`AccountWorker`).
  * Connexió IMAP IDLE en temps real.
  * Anàlisi contra l'API HTTP de Rspamd (`/checkv2`).
  * Moure correus brossa a la carpeta `SpamFilter` (o `INBOX.SpamFilter`).
  * Entrenament bayesià automàtic bidireccional (`/learnspam` i `/learnham`).
* **Panell Web i Mètriques ([dashboard.py](dashboard.py))**:
  * Dashboard Flask al port 8025 (per defecte).
  * Estadístiques globals i desglossades per bústia.
  * Botons d'acció en 1 clic:
    * **Rescatar** (moure de `SpamFilter` a `INBOX` i entrenar ham).
    * **Reportar spam** (moure d'`INBOX` a `SpamFilter` i entrenar spam).
  * Endpoint natiu `/metrics` compatible amb Prometheus.
* **Persistència SQLite ([Database](imap_bridge.py))**:
  * Mode WAL actiu.
  * Taules principals: `emails`, `daily_stats`, `learned_emails`, `events`.

---

## 🛠️ Directrius de Desenvolupament
1. **Llenguatge de la UI i Terminologia**:
   * La interfície web i els missatges a l'usuari s'expressen en **català**.
   * Els falsos positius es denominen **«Rescatats»** (mai *fals positiu* a la UI).
   * Els falsos negatius es denominen **«Reportats»** (mai *fals negatiu* a la UI).
2. **Validació abans de desplegar**:
   * Comprovar la sintaxi Python amb `python3 -m py_compile imap_bridge.py dashboard.py check_folders.py`.
3. **Control de canvis i Git**:
   * No commetre fitxers amb credencials (`.env`, `*.db`).
   * Mai fer `git push` sense la confirmació explícita de l'usuari.
4. **Privadesa i Confidencialitat de Dades**:
   * **Prohibició estricta de lectura de contingut d'emails**: Cap agent d'IA no ha d'inspeccionar, extreure, bolcar a consola ni emmagatzemar mai el cos (*body*), text pla/HTML ni fitxers adjunts dels correus (ja sigui mitjançant comandes IMAP FETCH `RFC822`/`BODY[]`, scripts temporals o consultes a l'API).
   * L'agent només té permès treballar amb metadades tècniques imprescindibles per al filtre (Message-ID, puntuacions i símbols de Rspamd, adreces d'origen/destí i assumpte).


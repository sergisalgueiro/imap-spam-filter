# 🛡️ IMAP Spam Filter Bridge

> Un pont antispam intel·ligent i ultralleuger per a múltiples bústies IMAP basat en **Rspamd**, **Redis** i **Python**, amb connexió en temps real via **IMAP IDLE**, entrenament bayesià bidireccional automàtic, **panell web de control** i mètriques natives per a **Prometheus / Grafana**.

Dissenyat específicament per funcionar 24/7 en dispositius de baix consum (com una **Raspberry Pi 3B+**, servidors domèstics o petits VPS) amb un consum total inferior a **65 MB de RAM**.

---

## 🚀 Característiques Principals

* **⚡ Suport Multi-Compte Concurrent (IMAP IDLE)**:
  * Monitoritza múltiples bústies d'un mateix domini en paral·lel utilitzant fils independents (`threading.Thread`).
  * Recepció i anàlisi de correus en temps real mitjançant `IMAP IDLE` (sense necessitat de fer *polling* periòdic que saturi el servidor).
  * Consum de CPU del 0% en repòs i reconnexió automàtica resilient amb *backoff* exponencial per compte.

* **🧠 Filtre Avançat amb Rspamd + Redis**:
  * Anàlisi de missatges a través de l'API HTTP moderna de Rspamd (`/checkv2`).
  * Suport de regles heurístiques, SPF, DKIM, DMARC, llistes negres DNS (DNSBL) i models estadístics.
  * Moure automàticament els missatges brossa a una carpeta dedicada (`SpamFilter`) mitjançant `IMAP MOVE` o `COPY+EXPUNGE`.

* **🔄 Entrenament Bayesià Bidireccional Automàtic**:
  * **Detecció de falsos negatius**: Si un usuari mou manualment un correu a la seva carpeta `Spam`, el servei l'envia automàticament a Rspamd (`/learnspam`) per entrenar el filtre de tot el domini.
  * **Rescat de falsos positius**: Si un correu es mou de la carpeta `SpamFilter` de tornada a l'`INBOX` (sigui des del client de correu o amb un clic des del Dashboard web), s'entrena automàticament com a legítim (`/learnham`).
  * Registre intern a SQLite per evitar sobre-entrenar missatges duplicats.

* **🖥️ Dashboard Web de Gestió (Port 8025)**:
  * Interfície web fosca, ràpida i adaptable a mòbil.
  * Resum d'activitat global i desglossament per bústia d'usuari.
  * Gràfics d'historial dels últims 30 dies.
  * Llistat detallat de correus filtrats amb l'informe complet de símbols de Rspamd.
  * Botó de **Rescat en 1 Clic**: connecta automàticament amb la bústia afectada, retorna el correu a la safata d'entrada i entrena el model com a ham.

* **📈 Mètriques per a Prometheus & Grafana**:
  * Endpoint natiu `/metrics` compatible amb Prometheus.
  * Mètriques globals i desglossades amb etiquetes per compte (`imap_spam_account_scanned_total{account="..."}`).
  * Health check a `/api/health` per a monitors externs (ex: Blackbox Exporter).

* **💾 Persistència Robusta a SQLite**:
  * Mode `WAL` (*Write-Ahead Logging*) per a consultes concurrents sense bloquejos.
  * Migracions automàtiques d'esquema sense pèrdua de dades històriques.

---

## 🏛️ Arquitectura del Sistema

```text
               +--------------------------------------------------+
               |            Servidor de Correu Remot              |
               |             (Ex: IMAP extern, cPanel, etc.)      |
               +--------------------------------------------------+
                                ▲               ▲
                     IMAP SSL   |               |   IMAP SSL
                     (IDLE)     |               |   (IDLE)
                                ▼               ▼
               +--------------------------------------------------+
               |        IMAP Spam Filter Bridge (Python)          |
               |                                                  |
               |  +--------------------+  +--------------------+  |
               |  | Worker Bústia 1    |  | Worker Bústia 2    |  |
               |  +--------------------+  +--------------------+  |
               +--------------------------------------------------+
                        |                          |
           POST /checkv2|                          | SQLite WAL
           POST /learn  |                          v
                        v                 +------------------+
               +-----------------+        | spam_filter.db   |
               | Rspamd Filter   |        +------------------+
               +-----------------+                 ^
                        |                          |
                        v                          v
               +-----------------+        +------------------+
               | Redis Storage   |        |  Dashboard Web   |
               | (Bayes Tokens)  |        |  i /metrics      |
               +-----------------+        +------------------+
                                                   |
                                                   v
                                        +---------------------+
                                        | Prometheus / Grafana|
                                        +---------------------+
```

---

## 📋 Requisits

* **Sistema Operatiu**: Linux (Debian / Raspberry Pi OS / Ubuntu / Alpine).
* **Python**: 3.9 o superior.
* **Serveis de suport**:
  * [Rspamd](https://rspamd.com/) (`sudo apt install rspamd`)
  * [Redis](https://redis.io/) (`sudo apt install redis-server`)

---

## 🔧 Instal·lació i Desplegament

### 1. Clonar el repositori
```bash
git clone https://github.com/el-teu-usuari/imap-spam-filter.git /opt/imap-spam-filter
cd /opt/imap-spam-filter
```

### 2. Instal·lar dependències Python
```bash
sudo pip3 install -r requirements.txt
```

### 3. Configurar l'entorn (`.env`)
Copia la plantilla d'exemple i defineix les teves dades:
```bash
sudo cp .env.example /opt/imap-spam-filter/.env
sudo chmod 600 /opt/imap-spam-filter/.env
sudo nano /opt/imap-spam-filter/.env
```

Exemple de configuració per a 3 bústies del mateix domini:
```env
# Servidor IMAP comú
IMAP_HOST="imap.domini.cat"
IMAP_PORT=993
IMAP_SSL=true

# Carpetes
FOLDER_INBOX="INBOX"
FOLDER_SPAM_DEST="SpamFilter"
FOLDER_SPAM_MANUAL="Spam"

# Bústies concurrents
ACCOUNT_1_USER="usuari1@domini.cat"
ACCOUNT_1_PASS="contrasenya1"

ACCOUNT_2_USER="usuari2@domini.cat"
ACCOUNT_2_PASS="contrasenya2"

ACCOUNT_3_USER="usuari3@domini.cat"
ACCOUNT_3_PASS="contrasenya3"

# Filtre i Rspamd
RSPAMD_URL="http://localhost:11333"
RSPAMD_CONTROLLER_URL="http://localhost:11334"
SPAM_THRESHOLD=6.0

# Dashboard
DASHBOARD_PORT=8025
DB_PATH="/opt/imap-spam-filter/data/spam_filter.db"
```

### 4. Verificar carpetes i connexió
Abans d'arrencar els dimonis, pots comprovar la connectivitat i veure les carpetes remotes:
```bash
python3 check_folders.py
```

### 5. Instal·lar i activar serveis systemd
Executa l'script de desplegament per activar els dos serveis (`imap-spam-filter` i `imap-spam-dashboard`):
```bash
sudo ./install.sh
```

Pots comprovar l'estat dels serveis en qualsevol moment:
```bash
sudo systemctl status imap-spam-filter
sudo systemctl status imap-spam-dashboard
sudo journalctl -u imap-spam-filter -f
```

---

## 🌐 Dashboard Web i Mètriques

* **Panell de Control**: `http://<ip-del-servidor>:8025/`
* **Mètriques de Prometheus**: `http://<ip-del-servidor>:8025/metrics`
* **Health Check**: `http://<ip-del-servidor>:8025/api/health`

### Mètriques exposades per Prometheus:
```text
# Comptadors totals (compatibilitat Grafana existent)
imap_spam_scanned_total 150
imap_spam_spam_total 12
imap_spam_ham_total 138
imap_spam_rescued_total 1
imap_spam_learned_total 20

# Comptadors desglossats per bústia
imap_spam_account_scanned_total{account="usuari1@domini.cat"} 90
imap_spam_account_scanned_total{account="usuari2@domini.cat"} 60
imap_spam_account_spam_total{account="usuari1@domini.cat"} 8
imap_spam_account_spam_total{account="usuari2@domini.cat"} 4
```

---

## 📄 Llicència

Aquest projecte es distribueix sota la llicència [MIT](LICENSE).

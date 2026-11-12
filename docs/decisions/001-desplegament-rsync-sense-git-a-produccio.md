# ADR 001: Desplegament mitjançant rsync des de l'estació de desenvolupament en lloc de Git Pull a producció

* **Data**: 2026-10-28
* **Estat**: Acceptat
* **Context**: Entorn de producció a la Raspberry Pi (`/opt/imap-spam-filter`)

---

## 🏛️ Context i Problema

El servei **IMAP Spam Filter Bridge** s'executa 24/7 en una Raspberry Pi domèstica sota systemd. Es va plantejar com gestionar els canvis de codi i desplegaments:
1. Clonar el repositori Git a la Raspberry Pi i fer `git pull` per actualitzar.
2. Mantenir la Raspberry Pi com a entorn pur d'execució i desplegar els canvis de forma atòmica des de la màquina de desenvolupament local (Mac) mitjançant `rsync` i `ssh`.

---

## 🎯 Decisió

S'ha decidit **no tenir cap repositori Git a `/opt/imap-spam-filter` a la Raspberry Pi**. Els desplegaments es fan exclusivament des de la màquina local de desenvolupament sincronitzant els fitxers modificats amb `rsync`, copiant-los amb `sudo` al directori final i reiniciant els serveis systemd.

El flux està automatitzat i documentat a `AGENTS.local.md`:
```bash
# 1. Pujar a staging temporal a la Raspi
ssh raspi "mkdir -p /tmp/imap_deploy/templates"
rsync -av imap_bridge.py dashboard.py check_folders.py raspi:/tmp/imap_deploy/
rsync -av templates/ raspi:/tmp/imap_deploy/templates/

# 2. Instal·lar a /opt/imap-spam-filter i reiniciar
ssh raspi "sudo cp /tmp/imap_deploy/imap_bridge.py /opt/imap-spam-filter/ && \
           sudo cp /tmp/imap_deploy/dashboard.py /opt/imap-spam-filter/ && \
           sudo cp /tmp/imap_deploy/templates/* /opt/imap-spam-filter/templates/ && \
           rm -rf /tmp/imap_deploy && \
           sudo systemctl restart imap-spam-filter imap-spam-dashboard"
```

---

## ⚖️ Conseqüències i Justificació

### Avantatges:
1. **Seguretat de credencials de GitHub**:
   La Raspberry Pi no necessita claus SSH privades ni tokens personals per accedir a GitHub. Si el dispositiu es veiés compromès, l'accés a GitHub està fora de perill.
2. **Zero risc de conflictes de Git a producció**:
   A `/opt/imap-spam-filter` conviuen fitxers d'execució crítics com el fitxer de credencials `.env` i la base de dades activa `data/spam_filter.db`. L'ús de `git pull` podria provocar errors de merge, sobrescriptures accidentals o bloquejos si hi ha fitxers no ignorats o modificats localment.
3. **Desplegaments en calent immediats**:
   Qualsevol canvi provat en local es posa en funcionament en menys de 3 segons, mantenint el cicle de feedback molt curt.
4. **Verificació immediata**:
   El mateix procés comprova automàticament que els daemons s'hagin aixecat correctament (`systemctl is-active`) i que l'endpoint `/metrics` respongui.

### Inconvenients:
* Requereix connexió SSH directa a la Raspberry Pi (fàcilment resolt amb Tailscale o la xarxa local).

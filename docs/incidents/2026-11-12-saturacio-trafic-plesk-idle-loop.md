# Post-Mortem: Saturació de la Quota de Tràfic a Plesk per Bucle d'IDLE i Re-descàrrega de Correus no Llegits

* **Data de l'incident**: 12 de novembre de 2026
* **Impacte**: Avís de superació del límit de tràfic mensual a Plesk (11.807 MB usats sobre un límit de 10.000 MB / 115%).
* **Servei afectat**: `imap-spam-filter` / Bústies IMAP de `salgueiro.com`.
* **Estat**: Resolt i verificat en producció.

---

## 🚨 Símptomes Inicials

L'usuari va rebre una notificació automàtica del panell de hosting Plesk:
> *"The subscription, owned by user, is approaching or already exceeded the resource usage limits:*  
> *Espacio en disco: 5 GB Límite / 2313.3 MB (45%) Usado / 2806.7 MB (55%) Pendiente*  
> *Tráfico: 10 GB/mes Límite / 11807.2 MB/mes (115%) Usado / 0 MB/mes (0%) Pendiente"*

El dubte principal era si el filtre antispam autònom (`imap-spam-filter`) podia ser el responsable d'aquest consum sobtat de més d'11 GB de tràfic en només 12 dies de mes.

---

## 🕵️ Diagnòstic i Evidències Tècniques

A la Raspberry Pi de producció es van recollir dades que confirmaven la hipòtesi:
1. **Mètriques d'E/S del procés**:
   A `/proc/<pid>/io`, el camp `rchar` mostrava **17.003.462.714 bytes (~17 GB)** llegits des del darrer reinici del servei fa 10 dies.
2. **Tràfic de xarxa a la interfície `eth0`**:
   L'eina `ip -s link` mostrava **14,7 GB de recepció (RX)** acumulats.
3. **Peticions massives per socket IMAP**:
   L'eina `ss -tip` per al port `993` va revelar més de **480.000 paquets de dades i desenes de milers de comandes IMAP** enviades des de la mateixa connexió persistent.
4. **Ritme de consum continu**:
   El procés consumia aproximadament **1 MB cada 50 segons** en segon pla, fins i tot sense l'arribada de correus nous.

---

## 🔍 Causa Arrel (*Root Cause*)

La fuita de tràfic es devia a una combinació de tres factors en el disseny original d'[`imap_bridge.py`](file:///Users/sergi.salgueiro/personal/imap-spam-filter/imap_bridge.py):

### 1. El bucle d'IMAP IDLE es trencava cada 56 segons pel keep-alive del servidor
El servidor IMAP de Plesk (Dovecot/Courier) envia de forma rutinària un missatge no etiquetat (*untagged*) cada ~56 segons:
```text
* OK Still here
```
Aquest paquet és estàndard en IMAP IDLE (RFC 2177) per mantenir la sessió TCP viva a través de NAT i tallafocs. Tanmateix, el codi del pont llegia aquesta línia, interpretava que l'IDLE havia finalitzat, enviava `DONE` i tornava a començar tot el bucle principal de control. Això feia que el pont executés **més de 1.500 cicles d'escaneig complets al dia per compte**.

### 2. Descàrrega repetida del cos sencer (`BODY.PEEK[]`) per a correus `UNSEEN`
Quan un correu legítim arriba a l'INBOX, el filtre no el marca com a llegit (`\Seen`), per tal que l'usuari el vegi en negreta al seu client (Thunderbird, Mail, Outlook).  
A cada cicle (cada 56 segons), la funció `_scan_inbox()` feia:
```python
typ, data = self.imap.uid("search", None, "UNSEEN")
for uid in uids:
    typ, fetch_data = self.imap.uid("fetch", uid, "(BODY.PEEK[])") # Descarrega tot l'email!
    info = email_info(raw)
    if self.db.get_email_by_msgid(self.user, info["message_id"]):
        continue # El descarta després d'haver-lo descarregat sencer!
```
Si una bústia tenia 1 o 2 correus pendents de llegir amb fitxers adjunts (per exemple, 1 MB en total), aquests mateixos correus es descarregaven **1.500 vegades al dia**, generant **1,5 GB de tràfic per dia per compte**.

### 3. Cerques massives de rescats a cada cicle
A cada volta de 56 segons, `_check_manual_rescues()` llançava desenes de cerques `HEADER Message-ID` per cadascun dels correus actius de la carpeta `SpamFilter`, multiplicant innecessàriament les comandes IMAP enviades al servidor.

---

## 🛠️ Solució Aplicada

Es van implementar millores estructurals a [`imap_bridge.py`](file:///Users/sergi.salgueiro/personal/imap-spam-filter/imap_bridge.py):

1. **Consulta prèvia i exclusiva de capçaleres (`Message-ID`)**:
   Abans de demanar el cos complet d'un correu `UNSEEN`, es demana exclusivament la capçalera de Message-ID:
   ```python
   self.imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
   ```
   Aquesta petició només transfereix uns **85 bytes**. Si el `Message-ID` ja existeix a la base de dades local SQLite, el correu s'omet a l'acte **sense descarregar mai el cos ni els adjunts**.
2. **El bucle IDLE ara manté el repòs passiu**:
   El bucle ignora els paquets de keep-alive del servidor (`Still here`, `heartbeat`) i es manté en espera passiva real fins a l'expiració de l'interval (29 minuts) o l'arribada d'un esdeveniment real de bústia (`EXISTS`, `EXPUNGE`, `FETCH`).
3. **Temporitzador separat per a la detecció de rescats manuals**:
   `_check_manual_rescues()` s'ha desacoblat de l'IDLE i ara s'executa amb un temporitzador periòdic propi (`RESCUE_INTERVAL = 600`, cada 10 minuts).
4. **Optimització d'aprenentatge a les carpetes d'Spam**:
   A `_learn_from_manual_spam()` també es consulta primer només la capçalera `Message-ID` per saber si un correu ja havia estat après prèviament abans de descarregar-ne el cos.

---

## 📈 Resultats i Verificació

* **Consum en repòs**: El tràfic del procés ha passat de **~1.000.000 bytes/minut a només 117 bytes/minut** (el pes exacte dels paquets de keep-alive de Dovecot).
* **Consum mensual estimat**: Amb la nova implementació, el tràfic mensual complet del filtre (inclosos tots els correus nous analitzats i keep-alives) passarà de **~35-40 GB/mes a menys de 200 MB/mes**, és a dir, **una reducció de més del 99%**.
* **Impacte en l'usuari**: Els correus s'han continuat rebent sense cap tall ni pèrdua de missatges durant ni després de la resolució.

# 📚 Documentació i Decisions Tècniques (Architecture Knowledge Base)

Aquest directori recull les decisions d'arquitectura (ADRs), post-mortems d'incidents i especificacions tècniques del projecte **IMAP Spam Filter Bridge**.

Els fitxers estan escrits en Markdown estàndard i són 100% compatibles amb eines de coneixement com **Obsidian**, editors de codi i la interfície web de GitHub.

---

## 📂 Estructura

* [decisions/](decisions/): Decisions d'arquitectura i disseny (ADRs).
  * [001-desplegament-rsync-sense-git-a-produccio.md](decisions/001-desplegament-rsync-sense-git-a-produccio.md): Raons per desplegar mitjançant `rsync` i no tenir un repositori Git a la Raspberry Pi.
* [incidents/](incidents/): Anàlisis post-mortem d'incidències tècniques i solucions aplicades.
  * [2026-11-12-saturacio-trafic-plesk-idle-loop.md](incidents/2026-11-12-saturacio-trafic-plesk-idle-loop.md): Anàlisi i resolució del bucle IMAP IDLE i la saturació de la quota de transferència mensual a Plesk.

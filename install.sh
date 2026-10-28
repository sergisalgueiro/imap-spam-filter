#!/usr/bin/env bash
set -e

INSTALL_DIR="/opt/imap-spam-filter"

echo "=== Desplegant fitxers a ${INSTALL_DIR} ==="
mkdir -p "${INSTALL_DIR}"/{data,templates}

# Copiar fitxers de codi
cp -v imap_bridge.py "${INSTALL_DIR}/imap_bridge.py"
cp -v dashboard.py "${INSTALL_DIR}/dashboard.py"
cp -v check_folders.py "${INSTALL_DIR}/check_folders.py" 2>/dev/null || true
cp -v templates/*.html "${INSTALL_DIR}/templates/"

# Si no existeix .env, crear-ne un a partir de .env.example
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    echo "Creant plantilla ${INSTALL_DIR}/.env..."
    cp -v .env.example "${INSTALL_DIR}/.env"
    chmod 600 "${INSTALL_DIR}/.env"
fi

# Instal·lar serveis systemd
echo "Instal·lant serveis systemd..."
cp -v imap-spam-filter.service /etc/systemd/system/
cp -v imap-spam-dashboard.service /etc/systemd/system/

systemctl daemon-reload
systemctl enable imap-spam-filter.service
systemctl enable imap-spam-dashboard.service

# Reiniciar serveis
echo "Reiniciant serveis..."
systemctl restart imap-spam-filter.service || true
systemctl restart imap-spam-dashboard.service || true

echo "=== Desplegament completat correctament ==="

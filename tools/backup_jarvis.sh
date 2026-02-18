#!/bin/bash
# JARVIS Automated Backup Script

BACKUP_DIR="/mnt/models/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="jarvis_backup_${TIMESTAMP}"

echo "=========================================="
echo "JARVIS Backup - $(date)"
echo "=========================================="

# Create backup directory structure
mkdir -p "${BACKUP_DIR}/${BACKUP_NAME}"/{core,config,skills,docs,root}

echo ""
echo "1. Backing up core code..."
cp -r ~/jarvis/core "${BACKUP_DIR}/${BACKUP_NAME}/"
cp -r ~/jarvis/tools "${BACKUP_DIR}/${BACKUP_NAME}/"
echo "✓ Core backed up"

echo ""
echo "2. Backing up configuration..."
cp ~/jarvis/config.yaml "${BACKUP_DIR}/${BACKUP_NAME}/config/"
cp ~/jarvis/.gitignore "${BACKUP_DIR}/${BACKUP_NAME}/config/"
echo "✓ Config backed up"

echo ""
echo "3. Backing up skills..."
cp -r /mnt/storage/jarvis/skills "${BACKUP_DIR}/${BACKUP_NAME}/"
echo "✓ Skills backed up"

echo ""
echo "4. Backing up documentation..."
cp -r ~/jarvis/docs "${BACKUP_DIR}/${BACKUP_NAME}/"
cp ~/jarvis/README.md "${BACKUP_DIR}/${BACKUP_NAME}/docs/"
cp ~/jarvis/CHANGELOG.md "${BACKUP_DIR}/${BACKUP_NAME}/docs/" 2>/dev/null
cp ~/jarvis/PROJECT_OVERVIEW.md "${BACKUP_DIR}/${BACKUP_NAME}/docs/" 2>/dev/null
echo "✓ Docs backed up"

echo ""
echo "5. Backing up root-level files..."
cp ~/jarvis/jarvis_continuous.py "${BACKUP_DIR}/${BACKUP_NAME}/root/"
cp ~/jarvis/jarvis_console.py "${BACKUP_DIR}/${BACKUP_NAME}/root/" 2>/dev/null
cp ~/jarvis/__init__.py "${BACKUP_DIR}/${BACKUP_NAME}/root/" 2>/dev/null
cp ~/jarvis/metadata.yaml "${BACKUP_DIR}/${BACKUP_NAME}/root/" 2>/dev/null
echo "✓ Root files backed up"

echo ""
echo "6. Backing up project context..."
mkdir -p "${BACKUP_DIR}/${BACKUP_NAME}/claude"
cp -r ~/jarvis/.claude "${BACKUP_DIR}/${BACKUP_NAME}/claude/" 2>/dev/null
echo "✓ Project context backed up"

echo ""
echo "7. Backing up service files..."
cp ~/jarvis/jarvis-backup.service "${BACKUP_DIR}/${BACKUP_NAME}/config/" 2>/dev/null
cp ~/jarvis/jarvis-backup.timer "${BACKUP_DIR}/${BACKUP_NAME}/config/" 2>/dev/null
cp ~/.config/systemd/user/jarvis.service "${BACKUP_DIR}/${BACKUP_NAME}/config/jarvis.service" 2>/dev/null
echo "✓ Service files backed up"

echo ""
echo "8. Creating tarball..."
cd "${BACKUP_DIR}"
tar -czf "${BACKUP_NAME}.tar.gz" "${BACKUP_NAME}"
rm -rf "${BACKUP_NAME}"
echo "✓ Compressed backup created"

echo ""
echo "9. Cleanup old backups (keep last 7)..."
ls -t ${BACKUP_DIR}/jarvis_backup_*.tar.gz | tail -n +8 | xargs -r rm
echo "✓ Old backups cleaned"

echo ""
BACKUP_SIZE=$(du -h "${BACKUP_DIR}/${BACKUP_NAME}.tar.gz" | cut -f1)
echo "=========================================="
echo "✅ Backup complete!"
echo "Location: ${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
echo "Size: ${BACKUP_SIZE}"
echo "=========================================="

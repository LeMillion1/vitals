#!/bin/bash
# Vitals Deploy Script — runs on VPS (ssh vitals "cd /root/vitals && ./deploy.sh")
set -e

cd /root/vitals
# GitHub is the source of truth — hard-sync so the VPS deploy target always
# matches origin, discarding any local drift.
git fetch origin
git reset --hard origin/feat/ia-today
docker compose up -d --build
echo "✓ Vitals deployed"

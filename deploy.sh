#!/bin/bash
# Vitals Deploy Script — runs on VPS (ssh vitals "cd /root/vitals && ./deploy.sh")
set -e

cd /root/vitals
# GitHub is the source of truth — hard-sync so the VPS deploy target always
# matches origin, discarding any local drift.
#
# This copy of the script lives on the `feature/proactive` branch and deliberately
# points at that branch instead of master: master's deploy.sh resets to
# origin/master, which would wipe the branch off the server on the first deploy.
# master's own copy is left untouched — when the feature merges, this file goes
# back to origin/master with it.
git fetch origin
git reset --hard origin/feature/proactive
docker compose up -d --build
echo "✓ Vitals deployed"

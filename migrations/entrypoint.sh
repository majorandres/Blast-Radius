#!/bin/sh
set -e
echo "[migrate] alembic upgrade head"
alembic upgrade head
echo "[migrate] roles and grants"
python -m bootstrap.roles
echo "[migrate] seed"
python -m bootstrap.seed
echo "[migrate] done"

#!/bin/sh
set -e

# データベースマイグレーションの自動実行
python manage.py migrate --noinput

exec "$@"

#!/bin/bash
set -e

echo "Running database migrations..."
flask db upgrade

echo "Seeding base data + super admin (idempotent)..."
flask seed

echo "Starting gunicorn..."
exec gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 60 --access-logfile - app:app

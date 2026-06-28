-- Run once as a PostgreSQL superuser on the Raspberry Pi:
--   sudo -u postgres psql -f deploy/setup_postgres.sql
CREATE USER stock WITH PASSWORD 'stockpass';
CREATE DATABASE stocktrading OWNER stock;
GRANT ALL PRIVILEGES ON DATABASE stocktrading TO stock;

"""Version 1 initializer. Future schema changes must use explicit migrations."""
from sqlalchemy import text
from app.db import engine
from app.models import Base


def main():
    with engine().begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(73462001)'))
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS timescaledb'))
        Base.metadata.create_all(conn)
        conn.execute(text("""SELECT create_hypertable(
            'historical_data', 'timestamp', if_not_exists => TRUE,
            chunk_time_interval => INTERVAL '7 days')"""))
        conn.execute(text("SELECT create_hypertable('live_ticks', 'timestamp', if_not_exists => TRUE, chunk_time_interval => INTERVAL '1 day')"))
    print('Schema v1 and historical_data hypertable ready')


if __name__ == '__main__':
    main()

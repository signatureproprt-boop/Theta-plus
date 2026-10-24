from functools import lru_cache
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import Session
from app.config import settings, read_secret


@lru_cache
def engine():
    s = settings()
    url = URL.create('postgresql+psycopg', username=s.db_user,
                     password=read_secret(s.db_password_file), host=s.db_host,
                     database=s.db_name)
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5,
                         connect_args={'connect_timeout': 5,
                                       'options': '-c timezone=UTC -c statement_timeout=30000'})


def session():
    return Session(engine(), expire_on_commit=False)

"""验收测试环境：独立测试库 coldchain_test + 独立端口 8010 的真实服务进程。

发送走真实 HTTP（sender 场景库），断言走 API 与库内核查。
"""
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sender"))
sys.path.insert(0, os.path.join(ROOT, "backend"))

TEST_DB = "coldchain_test"
os.environ["DATABASE_URL"] = f"postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/{TEST_DB}"
BASE = "http://127.0.0.1:8010"

from scenarios import Client  # noqa: E402


def _psql(dbname: str, sql: str):
    import psycopg2
    conn = psycopg2.connect(host="127.0.0.1", user="ccdev", password="ccdev",
                            dbname=dbname)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.close()


@pytest.fixture(scope="session")
def server():
    _psql("postgres", f"DROP DATABASE IF EXISTS {TEST_DB}")
    _psql("postgres", f"CREATE DATABASE {TEST_DB} OWNER ccdev")
    env = dict(os.environ)
    proc = subprocess.Popen(
        [os.path.join(ROOT, ".venv/bin/python"), "-m", "uvicorn",
         "app.main:app", "--host", "127.0.0.1", "--port", "8010"],
        cwd=os.path.join(ROOT, "backend"), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        c = Client(BASE)
        for _ in range(60):
            try:
                if c.get("/api/health").get("ok"):
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("测试服务未能启动")
        yield c
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        _psql("postgres",
              f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
              f"WHERE datname='{TEST_DB}' AND pid <> pg_backend_pid()")
        _psql("postgres", f"DROP DATABASE IF EXISTS {TEST_DB}")


@pytest.fixture(scope="session")
def client(server):
    return server

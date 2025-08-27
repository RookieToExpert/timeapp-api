from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel, EmailStr
from datetime import datetime
from zoneinfo import ZoneInfo
import os, bcrypt, jwt, psycopg2, redis, logging
from dotenv import load_dotenv

load_dotenv()

# ---- 日志 ----
log = logging.getLogger("timeapp")  
logging.basicConfig(level=logging.INFO)

# ---- 配置（通过环境变量）----
JWT_SECRET      = os.getenv("JWT_SECRET", "change_me")
SECURE_COOKIES  = os.getenv("SECURE_COOKIES", "false").lower() == "true"
PG_DSN          = os.getenv("PG_DSN")     # 例：host=...postgres.database.azure.com dbname=appdb user=ray password=*** sslmode=require
REDIS_URL       = os.getenv("REDIS_URL")  # 例：rediss://default:<key>@xxx.privatelink.redis.azure.net:10000/0

# Azure PG 常见：若是 *.postgres.database.azure.com 且未包含 sslmode，自动补上
if PG_DSN and "postgres.database.azure.com" in PG_DSN and "sslmode=" not in PG_DSN:
    PG_DSN = PG_DSN.strip() + " sslmode=require"

app = FastAPI()

# ---- 可选依赖：PG / Redis（允许缺省）----
pg = None
r  = None
memory_counters = {"visits_total": 0}  # 无 Redis 时的进程内计数（重启丢失）


def try_connect_pg():
    """尝试连接 PG 并确保表存在（不使用 citext）。"""
    global pg
    if not PG_DSN:
        return None
    try:
        pg = psycopg2.connect(PG_DSN)
        with pg.cursor() as cur:
            # users.email 用 TEXT，读写时用 LOWER() 保证大小写不敏感
            cur.execute("""
              CREATE TABLE IF NOT EXISTS users(
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
              );
            """)
            cur.execute("""
              CREATE TABLE IF NOT EXISTS site_counters(
                id INT PRIMARY KEY DEFAULT 1,
                total BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ DEFAULT now()
              );
            """)
            cur.execute("INSERT INTO site_counters(id,total) VALUES (1,0) ON CONFLICT (id) DO NOTHING;")
        pg.commit()
        log.info("PG connected and schema ensured")
        return pg
    except Exception as e:
        log.error("PG connect/init failed: %s", e)
        pg = None
        return None


def try_connect_redis():
    """尝试连接 Redis。"""
    global r
    if not REDIS_URL:
        return None
    try:
        r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        log.info("Redis connected")
        return r
    except Exception as e:
        log.error("Redis connect failed: %s", e)
        r = None
        return None


try_connect_pg()
try_connect_redis()

# ---- 模型 ----
class Register(BaseModel):
    email: EmailStr
    password: str

class Login(BaseModel):
    email: EmailStr
    password: str

# ---- 健康检查 ----
@app.get("/healthz")
def healthz():
    return {"ok": True, "pg": bool(pg), "redis": bool(r)}

# ---- 业务：时间 ----
@app.get("/time/now")
def time_now():
    cities = [
        ("New York", "America/New_York"),
        ("Beijing",  "Asia/Shanghai"),
        ("Sydney",   "Australia/Sydney"),
        ("Delhi",    "Asia/Kolkata"),
    ]
    return {"times": [
        {"label": label, "tz": tz, "iso": datetime.now(ZoneInfo(tz)).isoformat()}
        for label, tz in cities
    ]}

# ---- PG 依赖 ----
def require_pg():
    global pg
    if not pg:
        try_connect_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="PostgreSQL is not configured/available.")

# ---- 注册 / 登录（使用 LOWER(email)）----
@app.post("/auth/register")
def register(body: Register):
    require_pg()
    pw = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO users(email,password_hash) VALUES(LOWER(%s), %s)",
            (body.email, pw)
        )
    pg.commit()
    return {"ok": True}

@app.post("/auth/login")
def login(body: Login, response: Response):
    require_pg()
    with pg.cursor() as cur:
        cur.execute("SELECT id, password_hash FROM users WHERE email=LOWER(%s)", (body.email,))
        row = cur.fetchone()
    if not row:
        return {"ok": False}
    uid, hashv = row
    if not bcrypt.checkpw(body.password.encode(), hashv.encode()):
        return {"ok": False}
    token = jwt.encode({"uid": uid}, JWT_SECRET, algorithm="HS256")
    response.set_cookie("access_token", token, httponly=True, samesite="Lax", secure=SECURE_COOKIES)
    return {"ok": True}

# ---- 访问计数（Redis 优先；否则内存；若 PG 可用也累计 PG.total）----
@app.post("/metrics/visit")
def visit(req: Request):
    if r:
        r.incr("visits:total")
    else:
        memory_counters["visits_total"] += 1

    if pg:
        with pg.cursor() as cur:
            cur.execute("UPDATE site_counters SET total=total+1, updated_at=now() WHERE id=1")
        pg.commit()
    return {"ok": True}

@app.get("/metrics/total")
def total():
    if r:
        val = r.get("visits:total")
        if val is not None:
            return {"total": int(val)}
    if pg:
        with pg.cursor() as cur:
            cur.execute("SELECT total FROM site_counters WHERE id=1")
            row = cur.fetchone()
            if row:
                return {"total": int(row[0])}
    return {"total": int(memory_counters["visits_total"])}

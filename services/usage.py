# services/usage.py — SQLite: лимиты free/trial/sub/credit + события и отчёты (месячной учёт подписки)
import os, sqlite3, time
from typing import Optional, Tuple

DB = os.getenv("SQLITE_PATH", "/data/app.db")
FREEMIUM_DAILY      = int(os.getenv("FREEMIUM_DAILY", "3"))
PRO_TRIAL_DAILY     = int(os.getenv("PRO_TRIAL_DAILY", "1"))
SUBSCRIPTION_LIMIT  = int(os.getenv("SUBSCRIPTION_LIMIT", "600"))

def _db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        credits INTEGER DEFAULT 0,
        sub_until INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events(
        ts INTEGER, day INTEGER, ym TEXT, user_id INTEGER,
        type TEXT,           -- 'query'|'purchase'
        mode TEXT,           -- 'free'|'trial'|'credit'|'sub'
        model TEXT,          -- '4o-mini'|'o4-mini'|'4o'|NULL
        amount INTEGER       -- для purchase (кол-во кредитов)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_events_day ON events(day)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_events_ym ON events(ym)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sub_usage(
        user_id INTEGER, ym TEXT, used INTEGER,
        PRIMARY KEY(user_id, ym)
    )""")
    return conn

def _now_day_ym():
    now = int(time.time())
    day = now // 86400
    ym = time.strftime("%Y%m", time.gmtime(now))
    return now, day, ym

def add_credits(uid: int, n: int):
    with _db() as db:
        db.execute("""INSERT INTO users(user_id,credits) VALUES(?,?)
                      ON CONFLICT(user_id) DO UPDATE SET credits=credits+?""",(uid,n,n))
        ts, day, ym = _now_day_ym()
        db.execute("INSERT INTO events(ts,day,ym,user_id,type,mode,model,amount) VALUES(?,?,?,?,?,?,?,?)",
                   (ts,day,ym,uid,"purchase",None,None,n))

def activate_sub(uid: int, days=31):
    until = int(time.time()) + days*86400
    with _db() as db:
        db.execute("""INSERT INTO users(user_id,sub_until) VALUES(?,?)
                      ON CONFLICT(user_id) DO UPDATE SET sub_until=?""",(uid,until,until))
        ts, day, ym = _now_day_ym()
        db.execute("INSERT INTO events(ts,day,ym,user_id,type,mode,model,amount) VALUES(?,?,?,?,?,?,?,?)",
                   (ts,day,ym,uid,"purchase","sub",None,None))

def _used_today(uid: int):
    now, day, ym = _now_day_ym()
    with _db() as db:
        rows = db.execute("""SELECT mode, COUNT(*) FROM events
                             WHERE user_id=? AND type='query' AND day=?
                             GROUP BY mode""",(uid, day)).fetchall()
    d = {"free":0,"trial":0,"credit":0,"sub":0}
    for m,c in rows:
        if m in d: d[m] = c or 0
    return d

def _sub_used_ym(uid: int, ym: str):
    with _db() as db:
        row = db.execute("SELECT used FROM sub_usage WHERE user_id=? AND ym=?", (uid, ym)).fetchone()
    return (row[0] if row else 0)

def get_user_plan(uid: int):
    now, day, ym = _now_day_ym()
    with _db() as db:
        row = db.execute("SELECT credits, sub_until FROM users WHERE user_id=?", (uid,)).fetchone()
    credits, sub_until = (row or (0,0))
    sub_active = now < sub_until
    today = _used_today(uid)
    sub_used = _sub_used_ym(uid, ym)
    return dict(
        credits=credits,
        sub_active=sub_active,
        free_left_today=max(0, FREEMIUM_DAILY - today["free"]),
        trial_left_today=max(0, PRO_TRIAL_DAILY - today["trial"]),
        sub_left_month=max(0, SUBSCRIPTION_LIMIT - sub_used)
    )

def _log_query(uid: int, mode: str, model: str|None):
    ts, day, ym = _now_day_ym()
    with _db() as db:
        db.execute("INSERT INTO events(ts,day,ym,user_id,type,mode,model,amount) VALUES(?,?,?,?,?,?,?,?)",
                   (ts,day,ym,uid,"query",mode,model,None))

def consume_request(uid: int, need_pro: bool, allow_trial: bool) -> Tuple[bool,str,str]:
    """
    (ok, mode, reason), где mode ∈ {'free','trial','credit','sub'}.
    NB: здесь мы только «резервируем» лимит (подписка/кредит), а событие логируем уже после успешного ответа модели.
    """
    now, day, ym = _now_day_ym()
    plan = get_user_plan(uid)
    with _db() as db:
        # 1) Подписка (месячный лимит)
        if plan["sub_active"] and plan["sub_left_month"] > 0:
            used = _sub_used_ym(uid, ym)
            if used < SUBSCRIPTION_LIMIT:
                db.execute("""INSERT INTO sub_usage(user_id,ym,used)
                              VALUES(?,?,1)
                              ON CONFLICT(user_id,ym) DO UPDATE SET used=used+1""", (uid, ym))
                return True, "sub", ""

        # 2) Free (если не требуем Pro)
        if not need_pro and plan["free_left_today"] > 0:
            return True, "free", ""

        # 3) Trial Pro
        if allow_trial and plan["trial_left_today"] > 0:
            return True, "trial", ""

        # 4) Кредиты
        if plan["credits"] > 0:
            db.execute("UPDATE users SET credits=credits-1 WHERE user_id=? AND credits>0", (uid,))
            return True, "credit", ""

    if need_pro:
        return False, "", "нужен Pro (подписка/кредиты) или нет Trial на сегодня"
    if plan["free_left_today"] <= 0:
        return False, "", "исчерпан Free. Включи Pro/Trial или купи кредиты"
    return False, "", "недоступно"

def my_stats(uid: int):
    now, day, ym = _now_day_ym()
    with _db() as db:
        def agg(days: int):
            d0 = day - days
            row = db.execute("""
                SELECT
                  SUM(mode='free')   FILTER (WHERE type='query' AND day>=?),
                  SUM(mode='trial')  FILTER (WHERE type='query' AND day>=?),
                  SUM(mode='credit') FILTER (WHERE type='query' AND day>=?),
                  SUM(mode='sub')    FILTER (WHERE type='query' AND day>=?)
                FROM events WHERE user_id=?""",(d0,d0,d0,d0,uid)).fetchone() or (0,0,0,0)
            return dict(free=row[0] or 0, trial=row[1] or 0, credit=row[2] or 0, sub=row[3] or 0)
        today = agg(0); last7 = agg(7); last30 = agg(30)
    return dict(today=today, last7=last7, last30=last30)

def daily_summary(day_value: Optional[int]=None):
    now, day, ym = _now_day_ym()
    d = day if day_value is None else day_value
    with _db() as db:
        row = db.execute("""
          SELECT
            COUNT(DISTINCT user_id) FILTER (WHERE type='query' AND day=?) as dau,
            SUM(mode='free')   FILTER (WHERE type='query' AND day=?) as free_cnt,
            SUM(mode='trial')  FILTER (WHERE type='query' AND day=?) as trial_cnt,
            SUM(mode='credit') FILTER (WHERE type='query' AND day=?) as credit_cnt,
            SUM(mode='sub')    FILTER (WHERE type='query' AND day=?) as sub_cnt
          FROM events
        """,(d,d,d,d,d)).fetchone()
    dau = row[0] or 0
    free = (row[1] or 0); trial = (row[2] or 0)
    credit = (row[3] or 0); sub = (row[4] or 0)
    return dict(day=d, dau=dau, free_total=free+trial, paid=credit+sub, credit=credit, sub=sub)

def stats_export_json():
    now, day, ym = _now_day_ym()
    with _db() as db:
        ev = db.execute("SELECT ts,day,ym,user_id,type,mode,model,amount FROM events WHERE day>=? ORDER BY ts DESC LIMIT 10000",(day-30,)).fetchall()
        users = db.execute("SELECT user_id,credits,sub_until FROM users").fetchall()
    return {"events":[{"ts":r[0],"day":r[1],"ym":r[2],"user_id":r[3],"type":r[4],"mode":r[5],"model":r[6],"amount":r[7]} for r in ev],
            "users":[{"user_id":r[0],"credits":r[1],"sub_until":r[2]} for r in users]}

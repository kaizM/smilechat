from __future__ import annotations
import hashlib, hmac, json, os, re, secrets, sqlite3, time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
PG = os.getenv('DATABASE_URL', '')
LOCAL = os.getenv('SQLITE_PATH', str(ROOT / 'local.sqlite3'))
PROD = bool(os.getenv('RENDER'))
if PROD and not PG:
    raise RuntimeError('DATABASE_URL is required for durable production storage')

def hash_secret(value: str) -> str:
    salt = secrets.token_bytes(16)
    return salt.hex() + ':' + hashlib.scrypt(value.encode(), salt=salt, n=16384, r=8, p=1).hex()

def verify(value: str, stored: str) -> bool:
    try:
        salt, expected = stored.split(':', 1)
        actual = hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False

def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

class DB:
    def __init__(self, conn: Any): self.conn = conn
    def execute(self, query: str, values: tuple[Any, ...] = ()) -> Any:
        return self.conn.execute(query.replace('?', '%s') if PG else query, values)
    def one(self, query: str, values: tuple[Any, ...] = ()) -> Any:
        return self.execute(query, values).fetchone()
    def all(self, query: str, values: tuple[Any, ...] = ()) -> list[Any]:
        return self.execute(query, values).fetchall()

@contextmanager
def transaction() -> Iterator[DB]:
    if PG:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(PG, row_factory=dict_row)
    else:
        conn = sqlite3.connect(LOCAL, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
    try:
        if not PG: conn.execute('BEGIN IMMEDIATE')
        yield DB(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

def initialize() -> None:
    with transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS event (id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0, admin_hash TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS employee (username TEXT PRIMARY KEY, name TEXT NOT NULL, pin_hash TEXT NOT NULL, wish TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, recipient TEXT UNIQUE REFERENCES employee(username), draw_number INTEGER UNIQUE CHECK(draw_number BETWEEN 1 AND 200), CHECK(recipient IS NULL OR recipient <> username))')
        db.execute('CREATE TABLE IF NOT EXISTS session (token TEXT PRIMARY KEY, username TEXT NOT NULL, expires BIGINT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS limiter (bucket TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires BIGINT NOT NULL)')
        if not db.one('SELECT id FROM event WHERE id=1'):
            secret = os.getenv('ADMIN_PASSWORD')
            if not secret: raise RuntimeError('Set ADMIN_PASSWORD before starting')
            db.execute('INSERT INTO event(id,admin_hash) VALUES(1,?) ON CONFLICT(id) DO NOTHING', (hash_secret(secret),))

@app.on_event('startup')
def startup() -> None: initialize()

@app.middleware('http')
async def headers(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response

@app.get('/')
def home() -> FileResponse: return FileResponse(ROOT / 'static' / 'index.html')
@app.get('/health')
def health() -> dict[str, bool]:
    with transaction() as db: db.one('SELECT id FROM event WHERE id=1')
    return {'ok': True}

@app.exception_handler(HTTPException)
async def denied(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({'error': exc.detail}, status_code=exc.status_code)

def reject(message: str, status: int = 400) -> None: raise HTTPException(status, message)
def string(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum: reject('Invalid information / Datos inválidos')
    return value

def clean_wish(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict): reject('Invalid wishlist / Lista inválida')
    choices = value.get('choices', [])
    if not isinstance(choices, list) or len(choices) > 80: reject('Invalid choices')
    other = value.get('other', {})
    if not isinstance(other, dict) or not set(other).issubset({'0','1','2','3','4'}): reject('Invalid custom choices')
    return {'choices': sorted(set(string(v, 120) for v in choices)), 'other': {k:string(v,300) for k,v in other.items()}, **{k:string(value.get(k,''),1000) for k in ('custom','sizes','avoid','links')}, 'completed': True}

EMPTY = {'choices': [], 'other': {}, 'custom': '', 'sizes': '', 'avoid': '', 'links': '', 'completed': False}

def lock(db: DB) -> Any: return db.one('SELECT * FROM event WHERE id=1' + (' FOR UPDATE' if PG else ''))
def remaining(db: DB) -> list[int]:
    rows = db.all('SELECT draw_number FROM employee')
    used = {r['draw_number'] for r in rows if r['draw_number'] is not None}
    return [i for i in range(1, len(rows)+1) if i not in used]

def rate_limit(bucket: str, max_attempts: int) -> None:
    now = int(time.time())
    with transaction() as db:
        lock(db)
        db.execute('DELETE FROM limiter WHERE expires < ?', (now,))
        db.execute('INSERT INTO limiter VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET attempts=limiter.attempts+1', (bucket,now+900))
        attempts = db.one('SELECT attempts FROM limiter WHERE bucket=?',(bucket,))['attempts']
    if attempts > max_attempts: reject('Too many attempts / Demasiados intentos',429)

@app.post('/api')
async def api(request: Request) -> JSONResponse:
    if request.headers.get('content-type','').split(';')[0] != 'application/json': reject('Invalid content type',415)
    if request.headers.get('origin') and request.headers['origin'].split('://',1)[-1] != request.headers.get('host'): reject('Invalid origin',403)
    raw = await request.body()
    if len(raw) > 20000: reject('Request too large',413)
    try: data = json.loads(raw)
    except (ValueError, UnicodeError): reject('Invalid JSON')
    if not isinstance(data,dict): reject('Invalid request')
    action = data.get('action')
    if action in ('register','login','adminLogin'):
        username = string(data.get('username',''),24).strip().lower()
        if not re.fullmatch(r'[a-z0-9_.-]{3,24}',username): reject('Invalid username / Usuario inválido')
        ip = request.client.host if request.client else 'unknown'
        rate_limit('ip:'+digest(ip),80)
        if action != 'register': rate_limit('account:'+username,8)
    token = request.cookies.get('santa','')
    fresh = None
    with transaction() as db:
        event = lock(db)
        session = db.one('SELECT username FROM session WHERE token=? AND expires>?',(digest(token),int(time.time()))) if token else None
        user = session['username'] if session else None
        active = bool(event['active'])
        if action in ('register','login','adminLogin'):
            if action == 'adminLogin':
                if username != 'admin' or not verify(string(data.get('password',''),200),event['admin_hash']): reject('Incorrect login / Datos incorrectos',401)
            else:
                pin = string(data.get('pin',''),4)
                if not re.fullmatch(r'[0-9]{4}',pin) or username=='admin': reject('Invalid login / Datos inválidos')
                existing = db.one('SELECT * FROM employee WHERE username=?',(username,))
                if action=='register':
                    name=string(data.get('name',''),80).strip()
                    if active or not name or existing or len(db.all('SELECT username FROM employee'))>=200: reject('Registration unavailable / Registro no disponible')
                    db.execute('INSERT INTO employee(username,name,pin_hash,wish) VALUES(?,?,?,?)',(username,name,hash_secret(pin),json.dumps(EMPTY)))
                elif not existing or not verify(pin,existing['pin_hash']): reject('Incorrect login / Datos incorrectos',401)
            if token: db.execute('DELETE FROM session WHERE token=?',(digest(token),))
            user=username; fresh=secrets.token_hex(32)
            db.execute('INSERT INTO session VALUES(?,?,?)',(digest(fresh),user,int(time.time())+7*86400))
        elif action=='logout':
            if token: db.execute('DELETE FROM session WHERE token=?',(digest(token),))
            user=None
        elif action in ('resetDraw','resetEvent'):
            if user != 'admin': reject('Administrator only',403)
            expected = 'RESET DRAW' if action == 'resetDraw' else 'RESET EVENT'
            if data.get('confirmation') != expected: reject('Reset confirmation required',400)
            db.execute('UPDATE employee SET recipient=NULL, draw_number=NULL')
            if action == 'resetEvent':
                db.execute("DELETE FROM session WHERE username <> 'admin'")
                db.execute('DELETE FROM employee')
            db.execute('UPDATE event SET active=0 WHERE id=1')
            active = False
        elif action in ('approve','remove','activate'):
            if user!='admin': reject('Administrator only',403)
            if active: reject('Draw locked / Sorteo cerrado')
            if action=='activate':
                rows=db.all('SELECT username,approved FROM employee')
                if len(rows)<2 or any(not r['approved'] for r in rows): reject('Approve all participants first / Aprueba a todos primero')
                order=[r['username'] for r in rows]; secrets.SystemRandom().shuffle(order)
                for i,giver in enumerate(order): db.execute('UPDATE employee SET recipient=? WHERE username=?',(order[(i+1)%len(order)],giver))
                db.execute('UPDATE event SET active=1 WHERE id=1'); active=True
            else:
                target=string(data.get('target',''),24)
                if action=='remove':
                    db.execute('DELETE FROM session WHERE username=?',(target,));db.execute('DELETE FROM employee WHERE username=?',(target,))
                else: db.execute('UPDATE employee SET approved=1 WHERE username=?',(target,))
        elif action in ('save','reveal'):
            if not user or user=='admin': reject('Employee sign-in required',401)
            employee=db.one('SELECT * FROM employee WHERE username=?',(user,))
            if not employee: reject('Employee not found',401)
            if action=='save': db.execute('UPDATE employee SET wish=? WHERE username=?',(json.dumps(clean_wish(data.get('wish'))),user))
            else:
                if not active or not employee['approved'] or not employee['recipient']: reject('Draw unavailable / Sorteo no disponible',403)
                if employee['draw_number'] is None:
                    pool=remaining(db)
                    if not pool: reject('No numbers remaining',409)
                    db.execute('UPDATE employee SET draw_number=? WHERE username=?',(secrets.choice(pool),user))
        elif action!='status': reject('Unknown action')
        result: dict[str,Any]={'active':active,'admin':user=='admin'}
        if user=='admin': result['participants']=[dict(r) for r in db.all('SELECT username,name,approved,CASE WHEN draw_number IS NULL THEN 0 ELSE 1 END AS revealed FROM employee ORDER BY name')]
        elif user:
            e=db.one('SELECT * FROM employee WHERE username=?',(user,))
            if e:
                result['me']={'name':e['name'],'username':user,'approved':bool(e['approved']),'wish':json.loads(e['wish'])}
                if active and e['approved']:
                    result['availableNumbers']=remaining(db)
                    if e['draw_number'] is not None:
                        recipient=db.one('SELECT name,wish FROM employee WHERE username=?',(e['recipient'],))
                        result['me']['drawNumber']=e['draw_number'];result['me']['recipient']={'name':recipient['name'],'wish':json.loads(recipient['wish'])}
    response=JSONResponse(result)
    if fresh: response.set_cookie('santa',fresh,httponly=True,secure=PROD,samesite='strict',max_age=7*86400)
    elif action=='logout': response.delete_cookie('santa')
    return response

app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
